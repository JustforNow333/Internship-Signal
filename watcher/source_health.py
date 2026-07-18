"""Persistent, deterministic source-health monitoring for watcher runs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence, TextIO
from urllib.parse import urlsplit, urlunsplit

from backend.app.dedupe import norm_company
from watcher.config import CompanyCfg

SOURCE_KIND_DIRECT = "direct"
SOURCE_KIND_GITHUB_FEED = "github_feed"

STATUS_HEALTHY = "healthy"
STATUS_EMPTY = "empty"
STATUS_DEGRADED = "degraded"
STATUS_FAILING = "failing"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNKNOWN = "unknown"

COVERAGE_DIRECT = "direct_covered"
COVERAGE_DIRECT_EMPTY = "direct_empty_but_responding"
COVERAGE_BACKSTOP_ONLY = "backstop_only"
COVERAGE_DEGRADED_BACKSTOP = "direct_degraded_backstop_available"
COVERAGE_FAILING_BACKSTOP = "direct_failing_backstop_available"
COVERAGE_UNCOVERED = "uncovered_for_run"

ERROR_FETCH = "fetch_failure"
ERROR_SCHEMA = "schema_failure"
ERROR_MISSING_ADAPTER = "missing_adapter_registration"
ERROR_UNEXPECTED = "unexpected_exception"
ERROR_SOURCE = "source_failure"

MAX_ERROR_LENGTH = 320
MAX_FEED_LABEL_LENGTH = 180


@dataclass(frozen=True)
class SourceAttempt:
    health_key: str
    run_id: str
    observed_at: datetime
    source_kind: str
    company: str | None
    adapter: str
    attempted: bool
    succeeded: bool | None
    rows_returned: int | None
    error_kind: str | None = None
    error_message: str | None = None
    feed_label: str | None = None
    unsupported_reason: str | None = None


@dataclass(frozen=True)
class SourceHealthState:
    health_key: str
    source_kind: str
    company: str | None
    adapter: str
    feed_label: str | None
    unsupported_reason: str | None
    status: str
    previous_status: str | None
    total_attempts: int
    total_successes: int
    consecutive_failures: int
    consecutive_zero_successes: int
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_nonzero_at: datetime | None
    last_rows_returned: int | None
    last_error_kind: str | None
    last_error_message: str | None


@dataclass(frozen=True)
class HealthTransition:
    health_key: str
    source_kind: str
    company: str | None
    adapter: str
    feed_label: str | None
    from_status: str
    to_status: str
    recovery: bool


@dataclass(frozen=True)
class CompanyCoverage:
    company: str
    adapter: str
    state: str
    direct_status: str
    direct_attempt_succeeded: bool | None
    direct_rows_returned: int | None
    github_backstop_available: bool


@dataclass(frozen=True)
class HealthSummary:
    companies_configured: int
    direct_attempts: int
    direct_successes: int
    direct_zero_successes: int
    direct_failures: int
    direct_healthy: int
    direct_empty: int
    direct_degraded: int
    direct_failing: int
    direct_unsupported: int
    direct_unknown: int
    github_feeds_configured: int
    github_feeds_healthy: int
    github_feeds_degraded: int
    github_feeds_failing: int
    backstop_only_companies: int
    uncovered_companies: int
    health_transitions: int
    health_recoveries: int


def new_run_id(observed_at: datetime | None = None) -> str:
    timestamp = utc_datetime(observed_at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:12]}"


def direct_health_key(company: str, adapter: str) -> str:
    company_part = re.sub(r"[^a-z0-9]+", "", norm_company(company).casefold()) or "unknown"
    adapter_part = safe_token(adapter) or "unknown"
    return f"company:{company_part}:direct:{adapter_part}"


def github_feed_health_key(url: str) -> str:
    sanitized = sanitize_feed_label(url)
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:16]
    return f"github_feed:{digest}"


def sanitize_feed_label(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "injected"
    parsed = urlsplit(raw)
    if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
        host = parsed.hostname
        if parsed.port:
            host = f"{host}:{parsed.port}"
        raw = urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", "", ""))
    else:
        raw = re.sub(r"[?#].*$", "", raw)
    raw = re.sub(r"[\x00-\x1f\x7f]+", " ", raw)
    return raw[:MAX_FEED_LABEL_LENGTH]


def sanitize_error(value: object) -> str:
    message = str(value or "")
    message = re.sub(
        r"https?://[^\s]+",
        _sanitize_url_match,
        message,
    )
    message = re.sub(
        r"(?i)\b([a-z0-9_-]*(?:password|passwd|token|secret|api[_-]?key|authorization))\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    message = re.sub(r"\s+", " ", message).strip()
    return message[:MAX_ERROR_LENGTH]


def calculate_next_state(
    previous: SourceHealthState | None,
    attempt: SourceAttempt,
) -> SourceHealthState:
    """Purely calculate the current state after one normalized attempt."""

    observed_at = utc_datetime(attempt.observed_at)
    previous_status = previous.status if previous else None
    total_attempts = previous.total_attempts if previous else 0
    total_successes = previous.total_successes if previous else 0
    consecutive_failures = previous.consecutive_failures if previous else 0
    consecutive_zero_successes = previous.consecutive_zero_successes if previous else 0
    last_attempt_at = previous.last_attempt_at if previous else None
    last_success_at = previous.last_success_at if previous else None
    last_nonzero_at = previous.last_nonzero_at if previous else None
    last_rows_returned = previous.last_rows_returned if previous else None
    last_error_kind = previous.last_error_kind if previous else None
    last_error_message = previous.last_error_message if previous else None

    if not attempt.attempted:
        status = STATUS_UNSUPPORTED if attempt.source_kind == SOURCE_KIND_DIRECT else STATUS_UNKNOWN
        return SourceHealthState(
            health_key=attempt.health_key,
            source_kind=attempt.source_kind,
            company=attempt.company,
            adapter=attempt.adapter,
            feed_label=attempt.feed_label,
            unsupported_reason=attempt.unsupported_reason,
            status=status,
            previous_status=previous_status,
            total_attempts=total_attempts,
            total_successes=total_successes,
            consecutive_failures=consecutive_failures,
            consecutive_zero_successes=consecutive_zero_successes,
            last_attempt_at=last_attempt_at,
            last_success_at=last_success_at,
            last_nonzero_at=last_nonzero_at,
            last_rows_returned=last_rows_returned,
            last_error_kind=last_error_kind,
            last_error_message=last_error_message,
        )

    total_attempts += 1
    last_attempt_at = observed_at
    if attempt.succeeded is True:
        rows = max(0, int(attempt.rows_returned or 0))
        total_successes += 1
        consecutive_failures = 0
        last_success_at = observed_at
        last_rows_returned = rows
        last_error_kind = None
        last_error_message = None
        if rows > 0:
            last_nonzero_at = observed_at
            consecutive_zero_successes = 0
            status = STATUS_HEALTHY
        elif attempt.source_kind == SOURCE_KIND_GITHUB_FEED:
            consecutive_zero_successes = 0
            status = STATUS_HEALTHY
        else:
            consecutive_zero_successes += 1
            status = (
                STATUS_DEGRADED
                if consecutive_zero_successes >= 2 and last_nonzero_at is not None
                else STATUS_EMPTY
            )
    else:
        consecutive_failures += 1
        consecutive_zero_successes = 0
        last_rows_returned = None
        last_error_kind = attempt.error_kind
        last_error_message = attempt.error_message
        status = STATUS_FAILING if consecutive_failures >= 3 else STATUS_DEGRADED

    return SourceHealthState(
        health_key=attempt.health_key,
        source_kind=attempt.source_kind,
        company=attempt.company,
        adapter=attempt.adapter,
        feed_label=attempt.feed_label,
        unsupported_reason=attempt.unsupported_reason,
        status=status,
        previous_status=previous_status,
        total_attempts=total_attempts,
        total_successes=total_successes,
        consecutive_failures=consecutive_failures,
        consecutive_zero_successes=consecutive_zero_successes,
        last_attempt_at=last_attempt_at,
        last_success_at=last_success_at,
        last_nonzero_at=last_nonzero_at,
        last_rows_returned=last_rows_returned,
        last_error_kind=last_error_kind,
        last_error_message=last_error_message,
    )


def transition_for(
    previous: SourceHealthState | None,
    current: SourceHealthState,
) -> HealthTransition | None:
    if previous is None or previous.status == current.status:
        return None
    recovery = previous.status in {STATUS_DEGRADED, STATUS_FAILING} and current.status in {
        STATUS_HEALTHY,
        STATUS_EMPTY,
    }
    return HealthTransition(
        health_key=current.health_key,
        source_kind=current.source_kind,
        company=current.company,
        adapter=current.adapter,
        feed_label=current.feed_label,
        from_status=previous.status,
        to_status=current.status,
        recovery=recovery,
    )


def calculate_company_coverage(
    companies: Sequence[CompanyCfg],
    attempts: Sequence[SourceAttempt],
    states: Mapping[str, SourceHealthState],
) -> tuple[CompanyCoverage, ...]:
    direct_attempts = {
        attempt.company: attempt
        for attempt in attempts
        if attempt.source_kind == SOURCE_KIND_DIRECT and attempt.company is not None
    }
    github_available = any(
        attempt.source_kind == SOURCE_KIND_GITHUB_FEED
        and attempt.attempted
        and attempt.succeeded is True
        for attempt in attempts
    )
    coverage = []
    for company in companies:
        attempt = direct_attempts.get(company.name)
        key = direct_health_key(company.name, company.ats)
        state = states.get(key)
        direct_status = state.status if state else STATUS_UNKNOWN
        succeeded = attempt.succeeded if attempt else None
        rows = attempt.rows_returned if attempt else None
        if attempt and attempt.attempted and succeeded is True:
            coverage_state = COVERAGE_DIRECT if (rows or 0) > 0 else COVERAGE_DIRECT_EMPTY
        elif company.ats in {"bespoke", "github_only"} and github_available:
            coverage_state = COVERAGE_BACKSTOP_ONLY
        elif attempt and attempt.attempted and succeeded is False and github_available:
            coverage_state = (
                COVERAGE_FAILING_BACKSTOP
                if direct_status == STATUS_FAILING
                else COVERAGE_DEGRADED_BACKSTOP
            )
        else:
            coverage_state = COVERAGE_UNCOVERED
        coverage.append(
            CompanyCoverage(
                company=company.name,
                adapter=company.ats,
                state=coverage_state,
                direct_status=direct_status,
                direct_attempt_succeeded=succeeded,
                direct_rows_returned=rows,
                github_backstop_available=github_available,
            )
        )
    return tuple(coverage)


def summarize_health(
    companies: Sequence[CompanyCfg],
    attempts: Sequence[SourceAttempt],
    states: Mapping[str, SourceHealthState],
    transitions: Sequence[HealthTransition],
    coverage: Sequence[CompanyCoverage],
) -> HealthSummary:
    direct_states = [
        states[direct_health_key(company.name, company.ats)]
        for company in companies
        if direct_health_key(company.name, company.ats) in states
    ]
    github_states = [state for state in states.values() if state.source_kind == SOURCE_KIND_GITHUB_FEED]
    direct_attempts = [
        attempt for attempt in attempts if attempt.source_kind == SOURCE_KIND_DIRECT and attempt.attempted
    ]
    return HealthSummary(
        companies_configured=len(companies),
        direct_attempts=len(direct_attempts),
        direct_successes=sum(attempt.succeeded is True for attempt in direct_attempts),
        direct_zero_successes=sum(
            attempt.succeeded is True and attempt.rows_returned == 0 for attempt in direct_attempts
        ),
        direct_failures=sum(attempt.succeeded is False for attempt in direct_attempts),
        direct_healthy=_status_count(direct_states, STATUS_HEALTHY),
        direct_empty=_status_count(direct_states, STATUS_EMPTY),
        direct_degraded=_status_count(direct_states, STATUS_DEGRADED),
        direct_failing=_status_count(direct_states, STATUS_FAILING),
        direct_unsupported=_status_count(direct_states, STATUS_UNSUPPORTED),
        direct_unknown=_status_count(direct_states, STATUS_UNKNOWN),
        github_feeds_configured=sum(
            attempt.source_kind == SOURCE_KIND_GITHUB_FEED for attempt in attempts
        ),
        github_feeds_healthy=_status_count(github_states, STATUS_HEALTHY),
        github_feeds_degraded=_status_count(github_states, STATUS_DEGRADED),
        github_feeds_failing=_status_count(github_states, STATUS_FAILING),
        backstop_only_companies=sum(item.state == COVERAGE_BACKSTOP_ONLY for item in coverage),
        uncovered_companies=sum(item.state == COVERAGE_UNCOVERED for item in coverage),
        health_transitions=len(transitions),
        health_recoveries=sum(transition.recovery for transition in transitions),
    )


class SourceHealthStore:
    """Persist health attempts and current state in the watcher's SQLite file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def current_state(self, health_key: str) -> SourceHealthState | None:
        row = self._conn.execute(
            "select * from source_health_current where health_key = ?",
            (health_key,),
        ).fetchone()
        return _state_from_row(row) if row else None

    def all_current_states(self) -> dict[str, SourceHealthState]:
        rows = self._conn.execute(
            "select * from source_health_current order by health_key"
        ).fetchall()
        return {row["health_key"]: _state_from_row(row) for row in rows}

    def record_attempts(
        self,
        attempts: Iterable[SourceAttempt],
    ) -> tuple[dict[str, SourceHealthState], tuple[HealthTransition, ...]]:
        normalized = tuple(normalize_attempt(attempt) for attempt in attempts)
        states: dict[str, SourceHealthState] = {}
        transitions: list[HealthTransition] = []
        with self._conn:
            for attempt in normalized:
                previous = self.current_state(attempt.health_key)
                current = calculate_next_state(previous, attempt)
                self._insert_attempt(attempt)
                self._upsert_state(current)
                states[current.health_key] = current
                transition = transition_for(previous, current)
                if transition:
                    transitions.append(transition)
        return states, tuple(transitions)

    def attempt_count(self, *, run_id: str | None = None) -> int:
        if run_id is None:
            row = self._conn.execute("select count(*) from source_health_attempts").fetchone()
        else:
            row = self._conn.execute(
                "select count(*) from source_health_attempts where run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0])

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            create table if not exists source_health_attempts(
              attempt_id integer primary key autoincrement,
              run_id text not null,
              health_key text not null,
              observed_at text not null,
              source_kind text not null,
              company text,
              adapter text not null,
              feed_label text,
              unsupported_reason text,
              attempted integer not null,
              succeeded integer,
              rows_returned integer,
              error_kind text,
              error_message text,
              unique(run_id, health_key)
            );
            create index if not exists source_health_attempts_run_id_idx
              on source_health_attempts(run_id);
            create index if not exists source_health_attempts_key_idx
              on source_health_attempts(health_key, attempt_id);
            create table if not exists source_health_current(
              health_key text primary key,
              source_kind text not null,
              company text,
              adapter text not null,
              feed_label text,
              unsupported_reason text,
              status text not null,
              previous_status text,
              total_attempts integer not null,
              total_successes integer not null,
              consecutive_failures integer not null,
              consecutive_zero_successes integer not null,
              last_attempt_at text,
              last_success_at text,
              last_nonzero_at text,
              last_rows_returned integer,
              last_error_kind text,
              last_error_message text
            );
            """
        )
        self._conn.commit()

    def _insert_attempt(self, attempt: SourceAttempt) -> None:
        self._conn.execute(
            """
            insert into source_health_attempts(
              run_id, health_key, observed_at, source_kind, company, adapter,
              feed_label, unsupported_reason, attempted, succeeded, rows_returned,
              error_kind, error_message
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.run_id,
                attempt.health_key,
                iso_utc(attempt.observed_at),
                attempt.source_kind,
                attempt.company,
                attempt.adapter,
                attempt.feed_label,
                attempt.unsupported_reason,
                int(attempt.attempted),
                None if attempt.succeeded is None else int(attempt.succeeded),
                attempt.rows_returned,
                attempt.error_kind,
                attempt.error_message,
            ),
        )

    def _upsert_state(self, state: SourceHealthState) -> None:
        self._conn.execute(
            """
            insert into source_health_current(
              health_key, source_kind, company, adapter, feed_label,
              unsupported_reason, status, previous_status, total_attempts,
              total_successes, consecutive_failures, consecutive_zero_successes,
              last_attempt_at, last_success_at, last_nonzero_at, last_rows_returned,
              last_error_kind, last_error_message
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(health_key) do update set
              source_kind=excluded.source_kind,
              company=excluded.company,
              adapter=excluded.adapter,
              feed_label=excluded.feed_label,
              unsupported_reason=excluded.unsupported_reason,
              status=excluded.status,
              previous_status=excluded.previous_status,
              total_attempts=excluded.total_attempts,
              total_successes=excluded.total_successes,
              consecutive_failures=excluded.consecutive_failures,
              consecutive_zero_successes=excluded.consecutive_zero_successes,
              last_attempt_at=excluded.last_attempt_at,
              last_success_at=excluded.last_success_at,
              last_nonzero_at=excluded.last_nonzero_at,
              last_rows_returned=excluded.last_rows_returned,
              last_error_kind=excluded.last_error_kind,
              last_error_message=excluded.last_error_message
            """,
            (
                state.health_key,
                state.source_kind,
                state.company,
                state.adapter,
                state.feed_label,
                state.unsupported_reason,
                state.status,
                state.previous_status,
                state.total_attempts,
                state.total_successes,
                state.consecutive_failures,
                state.consecutive_zero_successes,
                iso_utc(state.last_attempt_at) if state.last_attempt_at else None,
                iso_utc(state.last_success_at) if state.last_success_at else None,
                iso_utc(state.last_nonzero_at) if state.last_nonzero_at else None,
                state.last_rows_returned,
                state.last_error_kind,
                state.last_error_message,
            ),
        )


def normalize_attempt(attempt: SourceAttempt) -> SourceAttempt:
    return replace(
        attempt,
        observed_at=utc_datetime(attempt.observed_at),
        company=sanitize_plain(attempt.company) if attempt.company is not None else None,
        adapter=safe_token(attempt.adapter) or "unknown",
        feed_label=sanitize_feed_label(attempt.feed_label) if attempt.feed_label else None,
        unsupported_reason=safe_token(attempt.unsupported_reason) if attempt.unsupported_reason else None,
        error_kind=safe_token(attempt.error_kind) if attempt.error_kind else None,
        error_message=sanitize_error(attempt.error_message) if attempt.error_message else None,
    )


def write_health_report(
    path: str | Path,
    *,
    run_id: str,
    observed_at: datetime,
    attempts: Sequence[SourceAttempt],
    states: Mapping[str, SourceHealthState],
    transitions: Sequence[HealthTransition],
    coverage: Sequence[CompanyCoverage],
    summary: HealthSummary,
    run_metadata: Mapping[str, object] | None = None,
) -> None:
    payload = {
        "schema_version": 1,
        "run_id": safe_run_id(run_id),
        "observed_at": iso_utc(observed_at),
        "run": _json_safe(dict(run_metadata or {})),
        "summary": asdict(summary),
        "attempts": [_attempt_dict(attempt) for attempt in attempts],
        "states": [_state_dict(state) for state in sorted(states.values(), key=lambda item: item.health_key)],
        "transitions": [_transition_dict(transition) for transition in transitions],
        "coverage": [_coverage_dict(item) for item in coverage],
    }
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_github_actions_report(
    report_path: str | Path,
    *,
    summary_path: str | Path | None,
    output: TextIO = sys.stdout,
    seen_loaded: str = "unknown",
    seen_saved: str = "unknown",
    load_status: str = "unknown",
    save_status: str = "unknown",
) -> None:
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    for transition in data.get("transitions", []):
        label = _json_source_label(transition)
        if transition.get("recovery"):
            print(
                f"::warning::SOURCE HEALTH RECOVERY: {label}: "
                f"{transition.get('from_status')} -> {transition.get('to_status')}",
                file=output,
            )
        elif transition.get("to_status") in {STATUS_DEGRADED, STATUS_FAILING}:
            print(
                f"::warning::SOURCE HEALTH: {label}: "
                f"{transition.get('from_status')} -> {transition.get('to_status')}",
                file=output,
            )
    for item in data.get("coverage", []):
        if item.get("state") == COVERAGE_UNCOVERED:
            print(
                f"::error::SOURCE COVERAGE: {sanitize_error(item.get('company'))} was uncovered for this run",
                file=output,
            )

    if not summary_path:
        return
    summary = data.get("summary", {})
    run = data.get("run", {})
    states = data.get("states", [])
    transitions = data.get("transitions", [])
    coverage = data.get("coverage", [])
    lines = [
        "## Internship watcher run",
        "",
        f"- Run ID: `{data.get('run_id', 'unknown')}`",
        f"- Active terms: {run.get('configured_terms', 'unknown')}",
        f"- Season status: `{run.get('season_status', 'unknown')}`",
        f"- Rows/jobs/matches/new/errors: {run.get('rows_fetched', 'unknown')} / {run.get('jobs_scored', 'unknown')} / {run.get('matches', 'unknown')} / {run.get('new_matches', 'unknown')} / {run.get('errors', 'unknown')}",
        f"- Seen store: loaded {seen_loaded} ({load_status}); saved {seen_saved} ({save_status})",
        "",
        "### Source health",
        "",
        "| Metric | Count |",
        "|---|---:|",
    ]
    for label, key in (
        ("Companies configured", "companies_configured"),
        ("Direct healthy", "direct_healthy"),
        ("Direct empty", "direct_empty"),
        ("Direct degraded", "direct_degraded"),
        ("Direct failing", "direct_failing"),
        ("Direct unsupported", "direct_unsupported"),
        ("GitHub feeds healthy", "github_feeds_healthy"),
        ("Backstop-only companies", "backstop_only_companies"),
        ("Uncovered companies", "uncovered_companies"),
        ("Health transitions", "health_transitions"),
        ("Health recoveries", "health_recoveries"),
    ):
        lines.append(f"| {label} | {int(summary.get(key, 0) or 0)} |")
    details = _workflow_detail_rows(states, transitions, coverage)
    lines.extend(["", "### Actionable source details", "", "| Category | Company/feed | Adapter | Detail |", "|---|---|---|---|"])
    lines.extend(details or ["| none | — | — | No degraded, failing, recovered, or uncovered sources |"])
    with Path(summary_path).open("a", encoding="utf-8") as summary_file:
        summary_file.write("\n".join(lines) + "\n")


def _workflow_detail_rows(states: list[dict], transitions: list[dict], coverage: list[dict]) -> list[str]:
    rows = []
    for state in states:
        if state.get("status") not in {STATUS_DEGRADED, STATUS_FAILING}:
            continue
        label = _json_source_label(state)
        detail = state.get("last_error_message") or f"rows={state.get('last_rows_returned')}"
        rows.append(_markdown_row(state.get("status"), label, state.get("adapter"), detail))
    for transition in transitions:
        if transition.get("recovery"):
            detail = f"{transition.get('from_status')} -> {transition.get('to_status')}"
            rows.append(_markdown_row("recovered", _json_source_label(transition), transition.get("adapter"), detail))
    for item in coverage:
        if item.get("state") == COVERAGE_UNCOVERED:
            rows.append(_markdown_row("uncovered", item.get("company"), item.get("adapter"), "No successful direct source or GitHub feed"))
    return rows


def _markdown_row(category: object, label: object, adapter: object, detail: object) -> str:
    values = [category, label, adapter, detail]
    clean = [sanitize_error(value).replace("|", "/") for value in values]
    return "| " + " | ".join(clean) + " |"


def _json_source_label(value: Mapping[str, object]) -> str:
    return sanitize_error(value.get("company") or value.get("feed_label") or value.get("health_key") or "unknown")


def _attempt_dict(attempt: SourceAttempt) -> dict:
    data = asdict(normalize_attempt(attempt))
    data["observed_at"] = iso_utc(attempt.observed_at)
    return data


def _state_dict(state: SourceHealthState) -> dict:
    data = asdict(state)
    for key in ("last_attempt_at", "last_success_at", "last_nonzero_at"):
        data[key] = iso_utc(data[key]) if data[key] else None
    data["feed_label"] = sanitize_feed_label(data["feed_label"]) if data["feed_label"] else None
    data["last_error_kind"] = safe_token(data["last_error_kind"]) if data["last_error_kind"] else None
    data["last_error_message"] = sanitize_error(data["last_error_message"]) if data["last_error_message"] else None
    data["company"] = sanitize_plain(data["company"]) if data["company"] else None
    return data


def _transition_dict(transition: HealthTransition) -> dict:
    data = asdict(transition)
    data["company"] = sanitize_plain(data["company"]) if data["company"] else None
    data["feed_label"] = sanitize_feed_label(data["feed_label"]) if data["feed_label"] else None
    data["adapter"] = safe_token(data["adapter"])
    return data


def _coverage_dict(coverage: CompanyCoverage) -> dict:
    data = asdict(coverage)
    data["company"] = sanitize_plain(data["company"])
    data["adapter"] = safe_token(data["adapter"])
    return data


def _sanitize_url_match(match: re.Match) -> str:
    raw = match.group(0)
    suffix = ""
    while raw and raw[-1] in ".,;:)":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    return sanitize_feed_label(raw) + suffix


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_error(value)
    if isinstance(value, Mapping):
        return {safe_token(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return sanitize_error(value)


def _state_from_row(row: sqlite3.Row) -> SourceHealthState:
    return SourceHealthState(
        health_key=row["health_key"],
        source_kind=row["source_kind"],
        company=row["company"],
        adapter=row["adapter"],
        feed_label=row["feed_label"],
        unsupported_reason=row["unsupported_reason"],
        status=row["status"],
        previous_status=row["previous_status"],
        total_attempts=int(row["total_attempts"]),
        total_successes=int(row["total_successes"]),
        consecutive_failures=int(row["consecutive_failures"]),
        consecutive_zero_successes=int(row["consecutive_zero_successes"]),
        last_attempt_at=parse_utc(row["last_attempt_at"]),
        last_success_at=parse_utc(row["last_success_at"]),
        last_nonzero_at=parse_utc(row["last_nonzero_at"]),
        last_rows_returned=row["last_rows_returned"],
        last_error_kind=row["last_error_kind"],
        last_error_message=row["last_error_message"],
    )


def _status_count(states: Iterable[SourceHealthState], status: str) -> int:
    return sum(state.status == status for state in states)


def safe_token(value: object) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", str(value or "").strip().casefold()).strip("_")


def safe_run_id(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value or "").strip())[:96] or "unknown"


def sanitize_plain(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:180]


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return utc_datetime(value).isoformat()


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def render_final_heartbeat(
    application_heartbeat: str,
    *,
    seen_loaded: object = "unknown",
    seen_saved: object = "unknown",
    load_status: object = "unknown",
    save_status: object = "unknown",
) -> str:
    """Append workflow persistence fields to an exact application heartbeat."""

    if not application_heartbeat or not application_heartbeat.startswith("HEARTBEAT: "):
        raise ValueError("application heartbeat is missing or invalid")
    if "\n" in application_heartbeat or "\r" in application_heartbeat:
        raise ValueError("application heartbeat must be exactly one line")
    values = (
        _heartbeat_workflow_value(seen_loaded),
        _heartbeat_workflow_value(seen_saved),
        _heartbeat_workflow_value(load_status),
        _heartbeat_workflow_value(save_status),
    )
    return (
        f"{application_heartbeat}, seen_loaded={values[0]}, seen_saved={values[1]}, "
        f"seen_store={values[2]}/{values[3]}"
    )


def _heartbeat_workflow_value(value: object) -> str:
    text = str(value or "unknown").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return "unknown"
    return text[:80]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render source-health GitHub Actions output.")
    parser.add_argument("command", choices=("workflow-report", "final-heartbeat"))
    parser.add_argument("report_path", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "workflow-report":
        if not args.report_path:
            parser.error("workflow-report requires report_path")
        render_github_actions_report(
            args.report_path,
            summary_path=os.getenv("GITHUB_STEP_SUMMARY"),
            seen_loaded=os.getenv("SEEN_LOADED", "unknown"),
            seen_saved=os.getenv("SEEN_SAVED", "unknown"),
            load_status=os.getenv("LOAD_STATUS", "unknown"),
            save_status=os.getenv("SAVE_STATUS", "unknown"),
        )
    else:
        try:
            print(
                render_final_heartbeat(
                    os.getenv("APPLICATION_HEARTBEAT", ""),
                    seen_loaded=os.getenv("SEEN_LOADED", "unknown"),
                    seen_saved=os.getenv("SEEN_SAVED", "unknown"),
                    load_status=os.getenv("LOAD_STATUS", "unknown"),
                    save_status=os.getenv("SAVE_STATUS", "unknown"),
                )
            )
        except ValueError as exc:
            print(f"::error::WATCHER HEARTBEAT: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
