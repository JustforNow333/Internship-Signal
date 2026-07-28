"""Independent source-health alert policy, persistence, rendering, and SMTP."""

from __future__ import annotations

import json
import logging
import os
import smtplib
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Mapping, Sequence

from watcher.source_comparison import SourceComparisonReport
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_UNCOVERED,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_DEGRADED,
    STATUS_FAILING,
    STATUS_HEALTHY,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceHealthState,
    iso_utc,
    sanitize_error,
    safe_error_kind,
    safe_run_id,
    utc_datetime,
)

LOGGER = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_TRANSITIONS_ONLY = "transitions_only"
MODE_FAILURE_ONLY = "failure_only"
MODE_DAILY_SUMMARY = "daily_summary"
HEALTH_EMAIL_MODES = frozenset(
    {
        MODE_OFF,
        MODE_TRANSITIONS_ONLY,
        MODE_FAILURE_ONLY,
        MODE_DAILY_SUMMARY,
    }
)
DEFAULT_MODE = MODE_TRANSITIONS_ONLY
DEFAULT_HOUR_UTC = 12
DEFAULT_COOLDOWN_HOURS = 24
DEFAULT_FEED_STALE_HOURS = 48

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class HealthAlertPolicy:
    mode: str = DEFAULT_MODE
    hour_utc: int = DEFAULT_HOUR_UTC
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS
    feed_stale_hours: int = DEFAULT_FEED_STALE_HOURS


@dataclass(frozen=True)
class HealthAlertCandidate:
    fingerprint: str
    alert_type: str
    severity: str
    health_key: str
    source_kind: str
    company: str | None
    source_label: str
    previous_status: str | None
    current_status: str
    consecutive_failures: int
    consecutive_empty: int
    last_success_at: str | None
    rows_returned: int | None
    error_kind: str | None
    direct_fallback_available: bool | None
    github_fallback_available: bool | None
    recommended_action: str
    run_id: str


@dataclass(frozen=True)
class HealthAlertResult:
    mode: str
    candidates: int
    sent: bool
    suppressed_by_cooldown: int
    recovery_alerts: int
    subject: str
    error: str | None
    daily_summary_sent: bool


def load_health_alert_policy(
    environ: Mapping[str, str] | None = None,
) -> HealthAlertPolicy:
    env = os.environ if environ is None else environ
    mode = str(env.get("WATCHER_HEALTH_EMAIL_MODE", DEFAULT_MODE)).strip().casefold()
    if mode not in HEALTH_EMAIL_MODES:
        raise ValueError(
            "WATCHER_HEALTH_EMAIL_MODE must be one of "
            + ", ".join(sorted(HEALTH_EMAIL_MODES))
        )
    return HealthAlertPolicy(
        mode=mode,
        hour_utc=_bounded_int(
            env.get("WATCHER_HEALTH_EMAIL_HOUR_UTC"),
            default=DEFAULT_HOUR_UTC,
            minimum=0,
            maximum=23,
            name="WATCHER_HEALTH_EMAIL_HOUR_UTC",
        ),
        cooldown_hours=_bounded_int(
            env.get("WATCHER_HEALTH_ALERT_COOLDOWN_HOURS"),
            default=DEFAULT_COOLDOWN_HOURS,
            minimum=1,
            maximum=24 * 30,
            name="WATCHER_HEALTH_ALERT_COOLDOWN_HOURS",
        ),
        feed_stale_hours=_bounded_int(
            env.get("WATCHER_FEED_STALE_HOURS"),
            default=DEFAULT_FEED_STALE_HOURS,
            minimum=1,
            maximum=24 * 90,
            name="WATCHER_FEED_STALE_HOURS",
        ),
    )


def evaluate_and_send_health_alerts(
    *,
    db_path: str | Path,
    policy: HealthAlertPolicy,
    run_id: str,
    observed_at: datetime,
    states: Mapping[str, SourceHealthState],
    transitions: Sequence[HealthTransition],
    coverage: Sequence[CompanyCoverage],
    summary: HealthSummary,
    comparison: SourceComparisonReport | None,
    sender: Callable[[str, str], bool] | None = None,
) -> HealthAlertResult:
    """Evaluate and optionally send health mail without touching posting state."""

    now = utc_datetime(observed_at)
    if policy.mode == MODE_OFF:
        # Initialize only the dedicated alert tables so workflow persistence
        # remains schema-stable even while alert delivery is disabled.
        with HealthAlertStore(db_path):
            pass
        return HealthAlertResult(
            mode=policy.mode,
            candidates=0,
            sent=False,
            suppressed_by_cooldown=0,
            recovery_alerts=0,
            subject="",
            error=None,
            daily_summary_sent=False,
        )

    with HealthAlertStore(db_path) as store:
        previous_coverage = store.latest_coverage_snapshot()
        candidates = build_alert_candidates(
            policy=policy,
            run_id=run_id,
            observed_at=now,
            states=states,
            transitions=transitions,
            coverage=coverage,
            previous_coverage=previous_coverage,
        )
        store.record_coverage_snapshot(
            run_id=run_id,
            observed_at=now,
            coverage=coverage,
        )
        for candidate in candidates:
            store.record_detected(candidate, detected_at=now)
            if candidate.alert_type == "recovery":
                store.resolve_source_failures(
                    candidate.health_key,
                    resolved_at=now,
                )
            elif candidate.alert_type == "new_failure":
                store.resolve_source_recoveries(
                    candidate.health_key,
                    resolved_at=now,
                )

        if policy.mode == MODE_DAILY_SUMMARY:
            due = (
                now.hour >= policy.hour_utc
                and not store.daily_summary_sent(now.date().isoformat())
            )
            sendable: list[HealthAlertCandidate] = []
            suppressed = 0
            if not due:
                return HealthAlertResult(
                    mode=policy.mode,
                    candidates=len(candidates),
                    sent=False,
                    suppressed_by_cooldown=0,
                    recovery_alerts=sum(
                        item.alert_type == "recovery" for item in candidates
                    ),
                    subject="",
                    error=None,
                    daily_summary_sent=False,
                )
            subject, body = render_daily_summary(
                run_id=run_id,
                observed_at=now,
                policy=policy,
                coverage=coverage,
                summary=summary,
                comparison=comparison,
                candidates=candidates,
                recent_candidates=store.recent_candidates(
                    since=now - timedelta(days=1)
                ),
            )
        else:
            candidates = _merge_candidates(
                candidates,
                store.pending_unsent_recoveries(),
            )
            policy_candidates = [
                candidate
                for candidate in candidates
                if _allowed_by_mode(candidate, policy.mode)
            ]
            sendable = []
            suppressed = 0
            cooldown = timedelta(hours=policy.cooldown_hours)
            for candidate in policy_candidates:
                if store.should_send(
                    candidate,
                    now=now,
                    cooldown=cooldown,
                ):
                    sendable.append(candidate)
                else:
                    suppressed += 1
            if not sendable:
                return HealthAlertResult(
                    mode=policy.mode,
                    candidates=len(candidates),
                    sent=False,
                    suppressed_by_cooldown=suppressed,
                    recovery_alerts=sum(
                        item.alert_type == "recovery" for item in candidates
                    ),
                    subject="",
                    error=None,
                    daily_summary_sent=False,
                )
            subject, body = render_alert_email(sendable)

        active_sender = sender or send_health_email
        try:
            sent = bool(active_sender(subject, body))
        except Exception as exc:  # SMTP must not affect internship state or run success
            error = sanitize_error(exc)
            LOGGER.error("Source-health email failed: %s", error)
            return HealthAlertResult(
                mode=policy.mode,
                candidates=len(candidates),
                sent=False,
                suppressed_by_cooldown=suppressed,
                recovery_alerts=sum(
                    item.alert_type == "recovery" for item in candidates
                ),
                subject=subject,
                error=error,
                daily_summary_sent=False,
            )

        if sent:
            if policy.mode == MODE_DAILY_SUMMARY:
                store.mark_daily_summary_sent(
                    summary_date=now.date().isoformat(),
                    sent_at=now,
                    run_id=run_id,
                )
            else:
                for candidate in sendable:
                    store.record_sent(candidate, sent_at=now)
        return HealthAlertResult(
            mode=policy.mode,
            candidates=len(candidates),
            sent=sent,
            suppressed_by_cooldown=suppressed,
            recovery_alerts=sum(
                item.alert_type == "recovery" for item in candidates
            ),
            subject=subject,
            error=None if sent else "health_sender_returned_false",
            daily_summary_sent=bool(sent and policy.mode == MODE_DAILY_SUMMARY),
        )


def build_alert_candidates(
    *,
    policy: HealthAlertPolicy,
    run_id: str,
    observed_at: datetime,
    states: Mapping[str, SourceHealthState],
    transitions: Sequence[HealthTransition],
    coverage: Sequence[CompanyCoverage],
    previous_coverage: Mapping[str, str] | None,
) -> tuple[HealthAlertCandidate, ...]:
    transition_by_key = {transition.health_key: transition for transition in transitions}
    coverage_by_company = {item.company: item for item in coverage}
    candidates: list[HealthAlertCandidate] = []
    for state in states.values():
        transition = transition_by_key.get(state.health_key)
        company_coverage = coverage_by_company.get(state.company or "")
        if transition and transition.recovery:
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type="recovery",
                    severity="info",
                    run_id=run_id,
                    coverage=company_coverage,
                    action="No action required; verify the next scheduled run remains healthy.",
                )
            )
            continue
        if state.status == STATUS_FAILING:
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type=(
                        "new_failure"
                        if transition and transition.to_status == STATUS_FAILING
                        else "continued_failure"
                    ),
                    severity="high",
                    run_id=run_id,
                    coverage=company_coverage,
                    action="Inspect the sanitized source-health report and adapter endpoint.",
                )
            )
        elif (
            state.source_kind != SOURCE_KIND_GITHUB_FEED
            and state.status == STATUS_DEGRADED
            and state.consecutive_zero_successes >= 2
            and state.last_nonzero_at is not None
        ):
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type="direct_source_silence",
                    severity="medium",
                    run_id=run_id,
                    coverage=company_coverage,
                    action="Check whether the board is genuinely empty or its response shape changed.",
                )
            )

        if (
            state.source_kind == SOURCE_KIND_GITHUB_FEED
            and state.status == STATUS_HEALTHY
            and state.last_nonzero_at is not None
            and observed_at - state.last_nonzero_at
            >= timedelta(hours=policy.feed_stale_hours)
        ):
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type="feed_stale",
                    severity="medium",
                    run_id=run_id,
                    coverage=None,
                    action="Verify the feed is still publishing postings for the configured season.",
                )
            )

    for item in coverage:
        if item.state != COVERAGE_UNCOVERED:
            continue
        candidates.append(
            HealthAlertCandidate(
                fingerprint=f"both_tiers_unavailable|{_fingerprint_token(item.company)}",
                alert_type="both_tiers_unavailable",
                severity="critical",
                health_key=f"coverage:{_fingerprint_token(item.company)}",
                source_kind="company_coverage",
                company=sanitize_error(item.company),
                source_label=sanitize_error(item.company),
                previous_status=(
                    previous_coverage.get(item.company)
                    if previous_coverage
                    else None
                ),
                current_status=item.state,
                consecutive_failures=0,
                consecutive_empty=0,
                last_success_at=None,
                rows_returned=item.direct_rows_returned,
                error_kind=None,
                direct_fallback_available=False,
                github_fallback_available=False,
                recommended_action="Restore either the direct source or a healthy GitHub backstop.",
                run_id=safe_run_id(run_id),
            )
        )

    if previous_coverage:
        became_backstop = sorted(
            item.company
            for item in coverage
            if item.state == COVERAGE_BACKSTOP_ONLY
            and previous_coverage.get(item.company) != COVERAGE_BACKSTOP_ONLY
        )
        previous_direct = sum(
            state not in {COVERAGE_BACKSTOP_ONLY, COVERAGE_UNCOVERED}
            for state in previous_coverage.values()
        )
        current_direct = sum(
            item.state not in {COVERAGE_BACKSTOP_ONLY, COVERAGE_UNCOVERED}
            for item in coverage
        )
        if current_direct < previous_direct or became_backstop:
            label = ", ".join(became_backstop[:10]) or "direct coverage"
            candidates.append(
                HealthAlertCandidate(
                    fingerprint=(
                        "coverage_regression|"
                        + _fingerprint_token(",".join(became_backstop))
                        + f"|{previous_direct}|{current_direct}"
                    ),
                    alert_type="coverage_regression",
                    severity="high",
                    health_key="coverage:aggregate",
                    source_kind="company_coverage",
                    company=None,
                    source_label=sanitize_error(label),
                    previous_status=f"direct_covered={previous_direct}",
                    current_status=f"direct_covered={current_direct}",
                    consecutive_failures=0,
                    consecutive_empty=0,
                    last_success_at=None,
                    rows_returned=None,
                    error_kind=None,
                    direct_fallback_available=None,
                    github_fallback_available=True,
                    recommended_action="Review companies that became backstop-only and restore direct coverage.",
                    run_id=safe_run_id(run_id),
                )
            )
    return _merge_candidates(candidates)


def render_alert_email(
    candidates: Sequence[HealthAlertCandidate],
) -> tuple[str, str]:
    first = candidates[0]
    if len(candidates) == 1 and first.alert_type == "recovery":
        subject = f"Internship Watcher Source Recovery: {first.source_label}"
    elif len(candidates) == 1:
        subject = (
            f"Internship Watcher Source Alert: {first.source_label} "
            f"{first.current_status}"
        )
    else:
        subject = f"Internship Watcher Source Alert: {len(candidates)} source incidents"
    lines = [subject, ""]
    for candidate in candidates:
        lines.extend(_candidate_lines(candidate))
    return subject, "\n".join(lines).rstrip() + "\n"


def render_daily_summary(
    *,
    run_id: str,
    observed_at: datetime,
    policy: HealthAlertPolicy,
    coverage: Sequence[CompanyCoverage],
    summary: HealthSummary,
    comparison: SourceComparisonReport | None,
    candidates: Sequence[HealthAlertCandidate],
    recent_candidates: Sequence[HealthAlertCandidate] = (),
) -> tuple[str, str]:
    subject = "Internship Watcher Daily Source Health"
    backstop = sorted(
        item.company for item in coverage if item.state == COVERAGE_BACKSTOP_ONLY
    )
    uncovered = sorted(
        item.company for item in coverage if item.state == COVERAGE_UNCOVERED
    )
    stale = sorted(
        candidate.source_label
        for candidate in candidates
        if candidate.alert_type == "feed_stale"
    )
    recoveries = sorted(
        candidate.source_label
        for candidate in recent_candidates
        if candidate.alert_type == "recovery"
    )
    failures = sorted(
        candidate.source_label
        for candidate in recent_candidates
        if candidate.alert_type == "new_failure"
    )
    comparison_counts = comparison.counts if comparison else {}
    lines = [
        subject,
        "",
        f"Run ID: {safe_run_id(run_id)}",
        f"Observed at: {iso_utc(observed_at)}",
        f"Feed stale threshold: {policy.feed_stale_hours} hours",
        "",
        "Current source status",
        f"  healthy direct sources: {summary.direct_healthy}",
        f"  empty but responding direct sources: {summary.direct_empty}",
        f"  degraded direct sources: {summary.direct_degraded}",
        f"  failing direct sources: {summary.direct_failing}",
        f"  unsupported direct sources: {summary.direct_unsupported}",
        f"  healthy GitHub feeds: {summary.github_feeds_healthy}",
        f"  failing GitHub feeds: {summary.github_feeds_failing}",
        f"  backstop-only companies: {len(backstop)}",
        f"  uncovered companies: {len(uncovered)}",
        "",
        "Source comparison",
        f"  GitHub-only eligible: {comparison_counts.get('github_only', 0)}",
        f"  direct-only eligible: {comparison_counts.get('direct_only', 0)}",
        f"  both found and merged: {comparison_counts.get('both', 0)}",
        f"  companies with no postings: {comparison_counts.get('no_postings', 0)}",
    ]
    _append_bounded(lines, "New failures", failures)
    _append_bounded(lines, "Recoveries", recoveries)
    _append_bounded(lines, "Stale configured-season feeds", stale)
    _append_bounded(lines, "Backstop-only companies", backstop)
    _append_bounded(lines, "Uncovered companies", uncovered)
    return subject, "\n".join(lines).rstrip() + "\n"


def send_health_email(subject: str, body: str) -> bool:
    """Send a source-health email using a separate renderer and call path."""

    env = _health_email_env()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = env["EMAIL_FROM"]
    message["To"] = env["EMAIL_TO"]
    message.set_content(body)
    LOGGER.info("Sending source-health email via %s:%s...", SMTP_HOST, SMTP_PORT)
    with smtplib.SMTP_SSL(
        SMTP_HOST,
        SMTP_PORT,
        timeout=SMTP_TIMEOUT_SECONDS,
    ) as smtp:
        smtp.login(env["SMTP_USER"], env["SMTP_APP_PASSWORD"])
        smtp.send_message(message)
    LOGGER.info("Source-health email sent.")
    return True


class HealthAlertStore:
    """Alert cooldown and daily-summary state, separate from ``seen``."""

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

    def record_detected(
        self,
        candidate: HealthAlertCandidate,
        *,
        detected_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_alert_state(
                  fingerprint, alert_type, health_key, current_status,
                  first_detected_at, last_detected_at, last_sent_at,
                  send_count, resolved_at
                ) values (?, ?, ?, ?, ?, ?, null, 0, null)
                on conflict(fingerprint) do update set
                  current_status=excluded.current_status,
                  last_detected_at=excluded.last_detected_at
                """,
                (
                    candidate.fingerprint,
                    candidate.alert_type,
                    candidate.health_key,
                    candidate.current_status,
                    iso_utc(detected_at),
                    iso_utc(detected_at),
                ),
            )
            self._conn.execute(
                """
                insert into source_health_alert_events(
                  run_id, fingerprint, detected_at, alert_type, payload_json
                ) values (?, ?, ?, ?, ?)
                on conflict(run_id, fingerprint) do nothing
                """,
                (
                    candidate.run_id,
                    candidate.fingerprint,
                    iso_utc(detected_at),
                    candidate.alert_type,
                    json.dumps(
                        asdict(candidate),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            self._conn.execute(
                """
                delete from source_health_alert_events
                where detected_at < ?
                """,
                (iso_utc(utc_datetime(detected_at) - timedelta(days=30)),),
            )

    def should_send(
        self,
        candidate: HealthAlertCandidate,
        *,
        now: datetime,
        cooldown: timedelta,
    ) -> bool:
        row = self._conn.execute(
            """
            select last_sent_at, resolved_at
            from source_health_alert_state
            where fingerprint = ?
            """,
            (candidate.fingerprint,),
        ).fetchone()
        if row is None or not row["last_sent_at"] or row["resolved_at"]:
            return True
        return now - _parse_datetime(row["last_sent_at"]) >= cooldown

    def record_sent(
        self,
        candidate: HealthAlertCandidate,
        *,
        sent_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set last_sent_at = ?,
                    send_count = send_count + 1,
                    resolved_at = null
                where fingerprint = ?
                """,
                (iso_utc(sent_at), candidate.fingerprint),
            )

    def resolve_source_failures(
        self,
        health_key: str,
        *,
        resolved_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set resolved_at = ?
                where health_key = ?
                  and alert_type in (
                    'new_failure', 'continued_failure',
                    'direct_source_silence', 'feed_stale'
                  )
                  and resolved_at is null
                """,
                (iso_utc(resolved_at), health_key),
            )

    def resolve_source_recoveries(
        self,
        health_key: str,
        *,
        resolved_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set resolved_at = ?
                where health_key = ?
                  and alert_type = 'recovery'
                  and resolved_at is null
                """,
                (iso_utc(resolved_at), health_key),
            )

    def daily_summary_sent(self, summary_date: str) -> bool:
        return (
            self._conn.execute(
                """
                select 1
                from source_health_daily_summary
                where summary_date = ?
                """,
                (summary_date,),
            ).fetchone()
            is not None
        )

    def mark_daily_summary_sent(
        self,
        *,
        summary_date: str,
        sent_at: datetime,
        run_id: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_daily_summary(
                  summary_date, sent_at, run_id
                ) values (?, ?, ?)
                on conflict(summary_date) do nothing
                """,
                (summary_date, iso_utc(sent_at), safe_run_id(run_id)),
            )

    def latest_coverage_snapshot(self) -> dict[str, str] | None:
        row = self._conn.execute(
            """
            select coverage_json
            from source_health_coverage_snapshots
            order by observed_at desc, run_id desc
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["coverage_json"])
        return {str(key): str(value) for key, value in data.items()}

    def recent_candidates(
        self,
        *,
        since: datetime,
    ) -> tuple[HealthAlertCandidate, ...]:
        rows = self._conn.execute(
            """
            select payload_json
            from source_health_alert_events
            where detected_at >= ?
            order by detected_at, run_id, fingerprint
            """,
            (iso_utc(since),),
        ).fetchall()
        return tuple(
            HealthAlertCandidate(**json.loads(row["payload_json"]))
            for row in rows
        )

    def pending_unsent_recoveries(
        self,
    ) -> tuple[HealthAlertCandidate, ...]:
        """Return recovery notices whose latest delivery never succeeded."""

        rows = self._conn.execute(
            """
            select events.payload_json
            from source_health_alert_state as state
            join source_health_alert_events as events
              on events.event_id = (
                select latest.event_id
                from source_health_alert_events as latest
                where latest.fingerprint = state.fingerprint
                order by latest.detected_at desc, latest.event_id desc
                limit 1
              )
            where state.alert_type = 'recovery'
              and (
                state.last_sent_at is null
                or events.detected_at > state.last_sent_at
              )
              and (
                state.resolved_at is null
                or events.detected_at > state.resolved_at
              )
            order by events.detected_at, state.fingerprint
            """
        ).fetchall()
        return tuple(
            HealthAlertCandidate(**json.loads(row["payload_json"]))
            for row in rows
        )

    def record_coverage_snapshot(
        self,
        *,
        run_id: str,
        observed_at: datetime,
        coverage: Sequence[CompanyCoverage],
    ) -> None:
        data = {
            sanitize_error(item.company): item.state
            for item in coverage
        }
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_coverage_snapshots(
                  run_id, observed_at, coverage_json
                ) values (?, ?, ?)
                on conflict(run_id) do update set
                  observed_at=excluded.observed_at,
                  coverage_json=excluded.coverage_json
                """,
                (
                    safe_run_id(run_id),
                    iso_utc(observed_at),
                    json.dumps(data, sort_keys=True, separators=(",", ":")),
                ),
            )
            self._conn.execute(
                """
                delete from source_health_coverage_snapshots
                where run_id not in (
                  select run_id
                  from source_health_coverage_snapshots
                  order by observed_at desc, run_id desc
                  limit 90
                )
                """
            )

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            create table if not exists source_health_alert_state(
              fingerprint text primary key,
              alert_type text not null,
              health_key text not null,
              current_status text not null,
              first_detected_at text not null,
              last_detected_at text not null,
              last_sent_at text,
              send_count integer not null,
              resolved_at text
            );
            create index if not exists source_health_alert_health_key_idx
              on source_health_alert_state(health_key);
            create table if not exists source_health_daily_summary(
              summary_date text primary key,
              sent_at text not null,
              run_id text not null
            );
            create table if not exists source_health_alert_events(
              event_id integer primary key autoincrement,
              run_id text not null,
              fingerprint text not null,
              detected_at text not null,
              alert_type text not null,
              payload_json text not null,
              unique(run_id, fingerprint)
            );
            create index if not exists source_health_alert_events_detected_idx
              on source_health_alert_events(detected_at);
            create table if not exists source_health_coverage_snapshots(
              run_id text primary key,
              observed_at text not null,
              coverage_json text not null
            );
            """
        )
        self._conn.commit()


def _candidate(
    state: SourceHealthState,
    *,
    transition: HealthTransition | None,
    alert_type: str,
    severity: str,
    run_id: str,
    coverage: CompanyCoverage | None,
    action: str,
) -> HealthAlertCandidate:
    label = state.company or state.feed_label or state.adapter or state.health_key
    failure_family = (
        "source_failure"
        if alert_type in {"new_failure", "continued_failure"}
        else alert_type
    )
    return HealthAlertCandidate(
        fingerprint=f"{failure_family}|{state.health_key}",
        alert_type=alert_type,
        severity=severity,
        health_key=state.health_key,
        source_kind=state.source_kind,
        company=sanitize_error(state.company) if state.company else None,
        source_label=sanitize_error(label),
        previous_status=(
            transition.from_status
            if transition
            else state.previous_status
        ),
        current_status=state.status,
        consecutive_failures=state.consecutive_failures,
        consecutive_empty=state.consecutive_zero_successes,
        last_success_at=(
            iso_utc(state.last_success_at)
            if state.last_success_at
            else None
        ),
        rows_returned=state.last_rows_returned,
        error_kind=(
            safe_error_kind(state.last_error_kind)
            if state.last_error_kind
            else None
        ),
        direct_fallback_available=(
            coverage.direct_attempt_succeeded
            if coverage
            else None
        ),
        github_fallback_available=(
            coverage.github_backstop_available
            if coverage
            else None
        ),
        recommended_action=action,
        run_id=safe_run_id(run_id),
    )


def _allowed_by_mode(
    candidate: HealthAlertCandidate,
    mode: str,
) -> bool:
    if mode == MODE_TRANSITIONS_ONLY:
        return candidate.alert_type in {
            "new_failure",
            "recovery",
            "direct_source_silence",
            "coverage_regression",
            "both_tiers_unavailable",
        }
    if mode == MODE_FAILURE_ONLY:
        return candidate.alert_type != "recovery"
    return False


def _merge_candidates(
    *groups: Sequence[HealthAlertCandidate],
) -> tuple[HealthAlertCandidate, ...]:
    by_fingerprint = {
        candidate.fingerprint: candidate
        for group in groups
        for candidate in group
    }
    return tuple(
        sorted(
            by_fingerprint.values(),
            key=lambda item: (
                {"critical": 0, "high": 1, "medium": 2, "info": 3}.get(
                    item.severity,
                    9,
                ),
                item.source_label.casefold(),
                item.alert_type,
            ),
        )
    )


def _candidate_lines(candidate: HealthAlertCandidate) -> list[str]:
    return [
        f"{candidate.severity.upper()}: {candidate.source_label}",
        f"  type: {candidate.alert_type}",
        f"  previous status: {candidate.previous_status or 'unknown'}",
        f"  current status: {candidate.current_status}",
        f"  consecutive failures: {candidate.consecutive_failures}",
        f"  consecutive empty successes: {candidate.consecutive_empty}",
        f"  last successful time: {candidate.last_success_at or 'unknown'}",
        f"  rows returned: {candidate.rows_returned if candidate.rows_returned is not None else 'unknown'}",
        f"  safe error category: {candidate.error_kind or 'none'}",
        f"  direct fallback available: {_yes_no_unknown(candidate.direct_fallback_available)}",
        f"  GitHub fallback available: {_yes_no_unknown(candidate.github_fallback_available)}",
        f"  recommended action: {candidate.recommended_action}",
        f"  run ID: {candidate.run_id}",
        "",
    ]


def _append_bounded(
    lines: list[str],
    label: str,
    values: Sequence[str],
    *,
    limit: int = 20,
) -> None:
    lines.extend(["", label])
    if not values:
        lines.append("  (none)")
        return
    lines.extend(f"  - {sanitize_error(value)}" for value in values[:limit])
    if len(values) > limit:
        lines.append(f"  - ... and {len(values) - limit} more")


def _health_email_env() -> dict[str, str]:
    values = {
        "SMTP_USER": os.getenv("SMTP_USER", "").strip(),
        "SMTP_APP_PASSWORD": os.getenv("SMTP_APP_PASSWORD", "").strip(),
        "EMAIL_TO": (
            os.getenv("WATCHER_HEALTH_EMAIL_TO", "").strip()
            or os.getenv("EMAIL_TO", "").strip()
        ),
    }
    values["EMAIL_FROM"] = (
        os.getenv("WATCHER_HEALTH_EMAIL_FROM", "").strip()
        or os.getenv("EMAIL_FROM", "").strip()
        or values["SMTP_USER"]
    )
    missing = [
        key
        for key in ("SMTP_USER", "SMTP_APP_PASSWORD", "EMAIL_TO")
        if not values[key]
    ]
    if missing:
        raise RuntimeError(
            "source-health email configuration missing: "
            + ", ".join(missing)
        )
    return values


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _fingerprint_token(value: object) -> str:
    return "_".join(
        token
        for token in "".join(
            char.casefold() if char.isalnum() else " "
            for char in str(value or "")
        ).split()
    )[:160] or "unknown"


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return utc_datetime(parsed)


def _yes_no_unknown(value: bool | None) -> str:
    return "unknown" if value is None else "yes" if value else "no"
