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
from watcher.config import (
    COVERAGE_STATUS_NO_SOURCE_FOUND,
    CompanyCfg,
    WatcherConfig,
)

SOURCE_KIND_DIRECT = "direct"
SOURCE_KIND_GITHUB_FEED = "github_feed"

STATUS_HEALTHY = "healthy"
STATUS_EMPTY = "empty"
STATUS_DEGRADED = "degraded"
STATUS_FAILING = "failing"
STATUS_UNSUPPORTED = "unsupported"
STATUS_UNKNOWN = "unknown"

# Direct-source status is an observation about the current collection attempt,
# not an inference from posting count history.  The legacy constants above
# remain for GitHub-feed history and serialized compatibility.
DIRECT_STATUS_NOT_CONFIGURED = "not_configured"
DIRECT_STATUS_HEALTHY_WITH_LISTINGS = "healthy_with_listings"
DIRECT_STATUS_HEALTHY_EMPTY = "healthy_empty"
DIRECT_STATUS_DEGRADED = "degraded"
DIRECT_STATUS_FAILED = "failed"
DIRECT_STATUS_UNKNOWN = "unknown"

COVERAGE_DIRECT = "direct_covered"
COVERAGE_DIRECT_EMPTY = "direct_empty_but_responding"
COVERAGE_DIRECT_DEGRADED = "direct_degraded"
COVERAGE_BACKSTOP_ONLY = "backstop_only"
COVERAGE_DEGRADED_BACKSTOP = "direct_degraded_backstop_available"
COVERAGE_FAILING_BACKSTOP = "direct_failing_backstop_available"
COVERAGE_UNKNOWN_BACKSTOP = "direct_unknown_backstop_available"
COVERAGE_UNCOVERED = "uncovered_for_run"

COVERAGE_AUDIT_DIRECT_VERIFIED = "direct_verified"
COVERAGE_AUDIT_DIRECT_DEGRADED = "direct_degraded"
COVERAGE_AUDIT_BACKSTOP_ONLY = "backstop_only"
COVERAGE_AUDIT_NO_SOURCE_FOUND = "no_source_found"
COVERAGE_AUDIT_NEEDS_INVESTIGATION = "needs_investigation"
COVERAGE_AUDIT_STATES = (
    COVERAGE_AUDIT_DIRECT_VERIFIED,
    COVERAGE_AUDIT_DIRECT_DEGRADED,
    COVERAGE_AUDIT_BACKSTOP_ONLY,
    COVERAGE_AUDIT_NO_SOURCE_FOUND,
    COVERAGE_AUDIT_NEEDS_INVESTIGATION,
)

ERROR_FETCH = "fetch_failure"
ERROR_SCHEMA = "schema_failure"
ERROR_MISSING_ADAPTER = "missing_adapter_registration"
ERROR_UNEXPECTED = "unexpected_exception"
ERROR_SOURCE = "source_failure"

MAX_ERROR_LENGTH = 320
MAX_FEED_LABEL_LENGTH = 180
MAX_DIAGNOSTIC_COUNT = 1_000_000_000
MAX_REASON_CODES = 12


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
    malformed_row_count: int | None = None
    schema_error_row_count: int | None = None
    duplicate_row_count: int | None = None
    failed_request_count: int | None = None
    incomplete: bool | None = None
    truncated: bool | None = None
    reason_codes: tuple[str, ...] = ()
    degraded: bool | None = None
    complete: bool | None = None


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
    last_malformed_row_count: int | None = None
    last_schema_error_row_count: int | None = None
    last_duplicate_row_count: int | None = None
    last_failed_request_count: int | None = None
    last_incomplete: bool | None = None
    last_truncated: bool | None = None
    last_reason_codes: tuple[str, ...] = ()
    last_degraded: bool | None = None
    last_complete: bool | None = None


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
class CompanyCoverageAudit:
    """One configuration-and-health coverage classification."""

    company: str
    ats: str
    state: str
    direct_health_status: str | None
    platform_family: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PlatformCoverageGap:
    platform_family: str
    companies: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "platform_family": self.platform_family,
            "companies": list(self.companies),
        }


@dataclass(frozen=True)
class CoverageAuditReport:
    """Deterministic, bounded company-source coverage report."""

    companies: tuple[CompanyCoverageAudit, ...]

    @property
    def total_companies(self) -> int:
        return len(self.companies)

    @property
    def state_counts(self) -> dict[str, int]:
        return {
            state: sum(company.state == state for company in self.companies)
            for state in COVERAGE_AUDIT_STATES
        }

    @property
    def state_percentages(self) -> dict[str, float]:
        return {
            state: _coverage_percentage(count, self.total_companies)
            for state, count in self.state_counts.items()
        }

    @property
    def direct_coverage_percentage(self) -> float:
        return _coverage_percentage(
            self.state_counts[COVERAGE_AUDIT_DIRECT_VERIFIED],
            self.total_companies,
        )

    @property
    def accounted_coverage_percentage(self) -> float:
        investigated = (
            self.total_companies
            - self.state_counts[COVERAGE_AUDIT_NEEDS_INVESTIGATION]
        )
        return _coverage_percentage(investigated, self.total_companies)

    @property
    def needs_investigation(self) -> tuple[str, ...]:
        return tuple(
            company.company
            for company in self.companies
            if company.state == COVERAGE_AUDIT_NEEDS_INVESTIGATION
        )

    @property
    def degraded_direct_sources(self) -> tuple[CompanyCoverageAudit, ...]:
        return tuple(
            company
            for company in self.companies
            if company.state == COVERAGE_AUDIT_DIRECT_DEGRADED
        )

    @property
    def platform_gaps(self) -> tuple[PlatformCoverageGap, ...]:
        grouped: dict[str, list[str]] = {}
        for company in self.companies:
            if not company.platform_family:
                continue
            grouped.setdefault(company.platform_family, []).append(company.company)
        return tuple(
            PlatformCoverageGap(
                platform_family=platform,
                companies=tuple(sorted(companies, key=_coverage_sort_key)),
            )
            for platform, companies in sorted(
                grouped.items(), key=lambda item: _coverage_sort_key(item[0])
            )
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "report_type": "company_source_coverage",
            "total_companies": self.total_companies,
            "state_counts": self.state_counts,
            "state_percentages": self.state_percentages,
            "direct_coverage_percentage": self.direct_coverage_percentage,
            "accounted_coverage_percentage": self.accounted_coverage_percentage,
            "needs_investigation": list(self.needs_investigation),
            "degraded_direct_sources": [
                {
                    "company": company.company,
                    "ats": company.ats,
                    "direct_health_status": company.direct_health_status,
                }
                for company in self.degraded_direct_sources
            ],
            "platform_gaps": [gap.as_dict() for gap in self.platform_gaps],
            "companies": [company.as_dict() for company in self.companies],
        }


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
    direct_healthy_with_listings: int = 0
    direct_healthy_empty: int = 0
    direct_failed: int = 0
    direct_not_configured: int = 0


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
    # A malformed authority (bad IPv6 bracket, out-of-range port) must never
    # raise out of a sanitizer: sanitize_error() runs over arbitrary failure
    # text, so one bad URL would otherwise abort the whole run.
    try:
        parsed = urlsplit(raw)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
        host = parsed.hostname
        try:
            if parsed.port:
                host = f"{host}:{parsed.port}"
        except ValueError:
            pass
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
    last_malformed_row_count = previous.last_malformed_row_count if previous else None
    last_schema_error_row_count = previous.last_schema_error_row_count if previous else None
    last_duplicate_row_count = previous.last_duplicate_row_count if previous else None
    last_failed_request_count = previous.last_failed_request_count if previous else None
    last_incomplete = previous.last_incomplete if previous else None
    last_truncated = previous.last_truncated if previous else None
    last_reason_codes = previous.last_reason_codes if previous else ()
    last_degraded = previous.last_degraded if previous else None
    last_complete = previous.last_complete if previous else None

    if not attempt.attempted:
        status = (
            DIRECT_STATUS_NOT_CONFIGURED
            if attempt.source_kind == SOURCE_KIND_DIRECT
            else STATUS_UNKNOWN
        )
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
            last_malformed_row_count=last_malformed_row_count,
            last_schema_error_row_count=last_schema_error_row_count,
            last_duplicate_row_count=last_duplicate_row_count,
            last_failed_request_count=last_failed_request_count,
            last_incomplete=last_incomplete,
            last_truncated=last_truncated,
            last_reason_codes=last_reason_codes,
            last_degraded=last_degraded,
            last_complete=last_complete,
        )

    total_attempts += 1
    last_attempt_at = observed_at
    last_malformed_row_count = attempt.malformed_row_count
    last_schema_error_row_count = attempt.schema_error_row_count
    last_duplicate_row_count = attempt.duplicate_row_count
    last_failed_request_count = attempt.failed_request_count
    last_incomplete = attempt.incomplete
    last_truncated = attempt.truncated
    last_reason_codes = attempt.reason_codes
    last_degraded = attempt.degraded
    last_complete = attempt.complete
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
            status = STATUS_EMPTY
        if attempt.source_kind == SOURCE_KIND_DIRECT:
            status = _direct_attempt_status(attempt, rows)
    else:
        consecutive_failures += 1
        consecutive_zero_successes = 0
        last_rows_returned = None
        last_error_kind = attempt.error_kind
        last_error_message = attempt.error_message
        status = (
            DIRECT_STATUS_FAILED
            if attempt.source_kind == SOURCE_KIND_DIRECT
            else STATUS_FAILING if consecutive_failures >= 3 else STATUS_DEGRADED
        )

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
        last_malformed_row_count=last_malformed_row_count,
        last_schema_error_row_count=last_schema_error_row_count,
        last_duplicate_row_count=last_duplicate_row_count,
        last_failed_request_count=last_failed_request_count,
        last_incomplete=last_incomplete,
        last_truncated=last_truncated,
        last_reason_codes=last_reason_codes,
        last_degraded=last_degraded,
        last_complete=last_complete,
    )


def _direct_attempt_status(attempt: SourceAttempt, rows: int) -> str:
    required = (
        attempt.malformed_row_count,
        attempt.schema_error_row_count,
        attempt.duplicate_row_count,
        attempt.failed_request_count,
        attempt.incomplete,
        attempt.truncated,
        attempt.degraded,
        attempt.complete,
    )
    if any(value is None for value in required):
        return DIRECT_STATUS_UNKNOWN
    if (
        attempt.degraded
        or attempt.incomplete
        or attempt.truncated
        or not attempt.complete
        or int(attempt.malformed_row_count or 0) > 0
        or int(attempt.schema_error_row_count or 0) > 0
    ):
        return DIRECT_STATUS_DEGRADED
    return (
        DIRECT_STATUS_HEALTHY_WITH_LISTINGS
        if rows > 0
        else DIRECT_STATUS_HEALTHY_EMPTY
    )


def transition_for(
    previous: SourceHealthState | None,
    current: SourceHealthState,
) -> HealthTransition | None:
    if previous is None or previous.status == current.status:
        return None
    recovery = previous.status in {
        STATUS_DEGRADED,
        STATUS_FAILING,
        DIRECT_STATUS_FAILED,
        DIRECT_STATUS_UNKNOWN,
    } and current.status in {
        STATUS_HEALTHY,
        STATUS_EMPTY,
        DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        DIRECT_STATUS_HEALTHY_EMPTY,
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
        if direct_status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS:
            coverage_state = COVERAGE_DIRECT
        elif direct_status == DIRECT_STATUS_HEALTHY_EMPTY:
            coverage_state = COVERAGE_DIRECT_EMPTY
        elif direct_status == DIRECT_STATUS_DEGRADED:
            coverage_state = (
                COVERAGE_DEGRADED_BACKSTOP
                if github_available
                else COVERAGE_DIRECT_DEGRADED
            )
        elif direct_status == DIRECT_STATUS_UNKNOWN and github_available:
            coverage_state = COVERAGE_UNKNOWN_BACKSTOP
        elif company.ats in {"bespoke", "github_only"} and github_available:
            coverage_state = COVERAGE_BACKSTOP_ONLY
        elif direct_status == DIRECT_STATUS_FAILED and github_available:
            coverage_state = COVERAGE_FAILING_BACKSTOP
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


def build_coverage_audit(
    config: WatcherConfig,
    states: Mapping[str, SourceHealthState],
) -> CoverageAuditReport:
    """Classify configured companies without collection or state mutation.

    GitHub configuration establishes intentional backstop reliance only. Feed
    health and row counts are deliberately ignored because a global feed's
    success is not evidence that it currently lists any particular company.
    """

    companies = tuple(getattr(config, "companies", ()))
    has_github_backstop = bool(config.effective_github_listing_sources())
    audited: list[CompanyCoverageAudit] = []
    for company in companies:
        health = states.get(direct_health_key(company.name, company.ats))
        health_status = health.status if health is not None else None
        no_direct_source = company.ats in {"bespoke", "github_only"}
        if company.coverage_status == COVERAGE_STATUS_NO_SOURCE_FOUND:
            coverage_state = COVERAGE_AUDIT_NO_SOURCE_FOUND
        elif no_direct_source:
            coverage_state = (
                COVERAGE_AUDIT_BACKSTOP_ONLY
                if has_github_backstop
                else COVERAGE_AUDIT_NEEDS_INVESTIGATION
            )
        elif health is None:
            coverage_state = COVERAGE_AUDIT_NEEDS_INVESTIGATION
        elif health_status in {
            DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            DIRECT_STATUS_HEALTHY_EMPTY,
            # Persisted databases from before the explicit direct-state names
            # still contain trustworthy successful collection evidence.
            STATUS_HEALTHY,
            STATUS_EMPTY,
        }:
            coverage_state = COVERAGE_AUDIT_DIRECT_VERIFIED
        else:
            coverage_state = COVERAGE_AUDIT_DIRECT_DEGRADED

        platform_family = company.platform_family
        if no_direct_source and company.ats == "bespoke" and not platform_family:
            platform_family = "Bespoke / unspecified"
        audited.append(
            CompanyCoverageAudit(
                company=company.name,
                ats=company.ats,
                state=coverage_state,
                direct_health_status=health_status,
                platform_family=platform_family,
            )
        )
    return CoverageAuditReport(
        companies=tuple(sorted(audited, key=lambda item: _coverage_sort_key(item.company)))
    )


def render_coverage_audit(
    report: CoverageAuditReport,
    *,
    output: TextIO | None = None,
) -> None:
    """Render the bounded human-readable coverage audit."""

    stream = output or sys.stdout
    labels = {
        COVERAGE_AUDIT_DIRECT_VERIFIED: "Direct verified",
        COVERAGE_AUDIT_DIRECT_DEGRADED: "Direct degraded",
        COVERAGE_AUDIT_BACKSTOP_ONLY: "Backstop only",
        COVERAGE_AUDIT_NO_SOURCE_FOUND: "No source found",
        COVERAGE_AUDIT_NEEDS_INVESTIGATION: "Needs investigation",
    }
    print("Coverage Audit", file=stream)
    print("", file=stream)
    print(f"Total companies:        {report.total_companies}", file=stream)
    for state in COVERAGE_AUDIT_STATES:
        count = report.state_counts[state]
        percentage = report.state_percentages[state]
        print(f"{labels[state] + ':':24}{count:5d} ({percentage:.1f}%)", file=stream)
    print("", file=stream)
    print(f"Direct coverage:       {report.direct_coverage_percentage:.1f}%", file=stream)
    print(f"Accounted coverage:    {report.accounted_coverage_percentage:.1f}%", file=stream)
    print("", file=stream)
    _render_company_names(
        "Needs investigation",
        report.needs_investigation,
        stream,
    )
    print("", file=stream)
    print("Degraded direct sources:", file=stream)
    if not report.degraded_direct_sources:
        print("  (none)", file=stream)
    for company in report.degraded_direct_sources:
        print(
            f"  - {company.company} ({company.ats}: "
            f"{company.direct_health_status or 'unknown'})",
            file=stream,
        )
    print("", file=stream)
    print("Unsupported/platform gaps:", file=stream)
    if not report.platform_gaps:
        print("  (none)", file=stream)
    for gap in report.platform_gaps:
        print(f"  {gap.platform_family}:", file=stream)
        for company in gap.companies:
            print(f"    - {company}", file=stream)


def _render_company_names(label: str, companies: Sequence[str], output: TextIO) -> None:
    print(f"{label}:", file=output)
    if not companies:
        print("  (none)", file=output)
        return
    for company in companies:
        print(f"  - {company}", file=output)


def _coverage_percentage(count: int, total: int) -> float:
    return round((count * 100.0 / total) if total else 0.0, 1)


def _coverage_sort_key(value: str) -> tuple[str, str]:
    return value.casefold(), value


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
        # Legacy aggregate names remain serialized and in the heartbeat.  Their
        # values now alias the corresponding explicit direct states.
        direct_healthy=_status_count(
            direct_states, DIRECT_STATUS_HEALTHY_WITH_LISTINGS
        ),
        direct_empty=_status_count(direct_states, DIRECT_STATUS_HEALTHY_EMPTY),
        direct_degraded=_status_count(direct_states, DIRECT_STATUS_DEGRADED),
        direct_failing=_status_count(direct_states, DIRECT_STATUS_FAILED),
        direct_unsupported=_status_count(
            direct_states, DIRECT_STATUS_NOT_CONFIGURED
        ),
        direct_unknown=_status_count(direct_states, DIRECT_STATUS_UNKNOWN),
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
        direct_healthy_with_listings=_status_count(
            direct_states, DIRECT_STATUS_HEALTHY_WITH_LISTINGS
        ),
        direct_healthy_empty=_status_count(
            direct_states, DIRECT_STATUS_HEALTHY_EMPTY
        ),
        direct_failed=_status_count(direct_states, DIRECT_STATUS_FAILED),
        direct_not_configured=_status_count(
            direct_states, DIRECT_STATUS_NOT_CONFIGURED
        ),
    )


class SourceHealthStore:
    """Persist health attempts and current state in the watcher's SQLite file."""

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            self._conn = sqlite3.connect(":memory:")
            if self.path.is_file():
                source = sqlite3.connect(
                    f"{self.path.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
                try:
                    source.backup(self._conn)
                finally:
                    source.close()
        else:
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
        if self.read_only:
            raise RuntimeError("source-health store is read-only")
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
              malformed_row_count integer,
              schema_error_row_count integer,
              duplicate_row_count integer,
              failed_request_count integer,
              incomplete integer,
              truncated integer,
              reason_codes_json text,
              degraded integer,
              complete integer,
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
              last_error_message text,
              last_malformed_row_count integer,
              last_schema_error_row_count integer,
              last_duplicate_row_count integer,
              last_failed_request_count integer,
              last_incomplete integer,
              last_truncated integer,
              last_reason_codes_json text,
              last_degraded integer,
              last_complete integer
            );
            """
        )
        self._ensure_diagnostic_columns()
        self._conn.commit()

    def _ensure_diagnostic_columns(self) -> None:
        attempt_columns = {
            "malformed_row_count": "integer",
            "schema_error_row_count": "integer",
            "duplicate_row_count": "integer",
            "failed_request_count": "integer",
            "incomplete": "integer",
            "truncated": "integer",
            "reason_codes_json": "text",
            "degraded": "integer",
            "complete": "integer",
        }
        state_columns = {
            "last_malformed_row_count": "integer",
            "last_schema_error_row_count": "integer",
            "last_duplicate_row_count": "integer",
            "last_failed_request_count": "integer",
            "last_incomplete": "integer",
            "last_truncated": "integer",
            "last_reason_codes_json": "text",
            "last_degraded": "integer",
            "last_complete": "integer",
        }
        for table, expected in (
            ("source_health_attempts", attempt_columns),
            ("source_health_current", state_columns),
        ):
            existing = {
                str(row[1])
                for row in self._conn.execute(f"pragma table_info({table})")
            }
            for name, sql_type in expected.items():
                if name not in existing:
                    self._conn.execute(
                        f"alter table {table} add column {name} {sql_type}"
                    )

    def _insert_attempt(self, attempt: SourceAttempt) -> None:
        self._conn.execute(
            """
            insert into source_health_attempts(
              run_id, health_key, observed_at, source_kind, company, adapter,
              feed_label, unsupported_reason, attempted, succeeded, rows_returned,
              error_kind, error_message, malformed_row_count,
              schema_error_row_count, duplicate_row_count, failed_request_count,
              incomplete, truncated, reason_codes_json, degraded, complete
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                attempt.malformed_row_count,
                attempt.schema_error_row_count,
                attempt.duplicate_row_count,
                attempt.failed_request_count,
                _optional_bool_int(attempt.incomplete),
                _optional_bool_int(attempt.truncated),
                json.dumps(list(attempt.reason_codes), separators=(",", ":")),
                _optional_bool_int(attempt.degraded),
                _optional_bool_int(attempt.complete),
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
              last_error_kind, last_error_message, last_malformed_row_count,
              last_schema_error_row_count, last_duplicate_row_count,
              last_failed_request_count, last_incomplete, last_truncated,
              last_reason_codes_json, last_degraded, last_complete
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
              last_error_message=excluded.last_error_message,
              last_malformed_row_count=excluded.last_malformed_row_count,
              last_schema_error_row_count=excluded.last_schema_error_row_count,
              last_duplicate_row_count=excluded.last_duplicate_row_count,
              last_failed_request_count=excluded.last_failed_request_count,
              last_incomplete=excluded.last_incomplete,
              last_truncated=excluded.last_truncated,
              last_reason_codes_json=excluded.last_reason_codes_json,
              last_degraded=excluded.last_degraded,
              last_complete=excluded.last_complete
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
                state.last_malformed_row_count,
                state.last_schema_error_row_count,
                state.last_duplicate_row_count,
                state.last_failed_request_count,
                _optional_bool_int(state.last_incomplete),
                _optional_bool_int(state.last_truncated),
                json.dumps(list(state.last_reason_codes), separators=(",", ":")),
                _optional_bool_int(state.last_degraded),
                _optional_bool_int(state.last_complete),
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
        error_kind=safe_error_kind(attempt.error_kind) if attempt.error_kind else None,
        error_message=sanitize_error(attempt.error_message) if attempt.error_message else None,
        rows_returned=_bounded_optional_count(attempt.rows_returned),
        malformed_row_count=_bounded_optional_count(attempt.malformed_row_count),
        schema_error_row_count=_bounded_optional_count(attempt.schema_error_row_count),
        duplicate_row_count=_bounded_optional_count(attempt.duplicate_row_count),
        failed_request_count=_bounded_optional_count(attempt.failed_request_count),
        reason_codes=_bounded_reason_codes(attempt.reason_codes),
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
        "schema_version": 2,
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
    run = data.get("run", {})
    workday = run.get("workday_transport", {}) if isinstance(run, Mapping) else {}
    if isinstance(workday, Mapping) and workday.get("likely_shared_incident"):
        print(
            "::warning::WORKDAY TRANSPORT INCIDENT: "
            f"attempted={int(workday.get('attempted_tenants', 0) or 0)}, "
            f"failed={int(workday.get('failed_tenants', 0) or 0)}, "
            f"dominant_error={safe_error_kind(workday.get('dominant_error', 'unknown')) or 'unknown'}, "
            f"dominant_error_count={int(workday.get('dominant_error_count', 0) or 0)}",
            file=output,
        )
    for transition in data.get("transitions", []):
        label = _json_source_label(transition)
        if transition.get("recovery"):
            print(
                f"::warning::SOURCE HEALTH RECOVERY: {label}: "
                f"{transition.get('from_status')} -> {transition.get('to_status')}",
                file=output,
            )
        elif transition.get("to_status") in {
            STATUS_DEGRADED,
            STATUS_FAILING,
            DIRECT_STATUS_DEGRADED,
            DIRECT_STATUS_FAILED,
            DIRECT_STATUS_UNKNOWN,
        }:
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
        f"- Match email sent: `{run.get('digest_sent', False)}`",
        f"- Health email: mode `{run.get('health_email_mode', 'unknown')}`, sent `{run.get('health_alert_sent', False)}`, candidates {run.get('health_alert_candidates', 0)}, cooldown-suppressed {run.get('health_alert_suppressed_by_cooldown', 0)}",
        "",
        "### Source health",
        "",
        "| Metric | Count |",
        "|---|---:|",
    ]
    for label, key in (
        ("Companies configured", "companies_configured"),
        ("Direct degraded", "direct_degraded"),
        ("Direct healthy with listings", "direct_healthy_with_listings"),
        ("Direct healthy empty", "direct_healthy_empty"),
        ("Direct failed", "direct_failed"),
        ("Direct not configured", "direct_not_configured"),
        ("Direct unknown", "direct_unknown"),
        ("GitHub feeds healthy", "github_feeds_healthy"),
        ("Backstop-only companies", "backstop_only_companies"),
        ("Uncovered companies", "uncovered_companies"),
        ("Health transitions", "health_transitions"),
        ("Health recoveries", "health_recoveries"),
    ):
        lines.append(f"| {label} | {int(summary.get(key, 0) or 0)} |")
    workday = run.get("workday_transport", {})
    if isinstance(workday, Mapping):
        lines.extend(
            [
                "",
                "### Workday transport",
                "",
                f"- Attempted/succeeded/failed tenants: {int(workday.get('attempted_tenants', 0) or 0)} / {int(workday.get('successful_tenants', 0) or 0)} / {int(workday.get('failed_tenants', 0) or 0)}",
                f"- Retry attempts: {int(workday.get('retry_attempts', 0) or 0)}",
                f"- Dominant error: `{safe_error_kind(workday.get('dominant_error', 'none')) or 'none'}` ({int(workday.get('dominant_error_count', 0) or 0)})",
                f"- Likely shared incident: `{'yes' if workday.get('likely_shared_incident') else 'no'}`",
            ]
        )
    details = _workflow_detail_rows(states, transitions, coverage)
    lines.extend(["", "### Actionable source details", "", "| Category | Company/feed | Adapter | Detail |", "|---|---|---|---|"])
    lines.extend(details or ["| none | — | — | No degraded, failing, recovered, or uncovered sources |"])
    with Path(summary_path).open("a", encoding="utf-8") as summary_file:
        summary_file.write("\n".join(lines) + "\n")


def _workflow_detail_rows(states: list[dict], transitions: list[dict], coverage: list[dict]) -> list[str]:
    rows = []
    for state in states:
        if state.get("status") not in {
            STATUS_DEGRADED,
            STATUS_FAILING,
            DIRECT_STATUS_DEGRADED,
            DIRECT_STATUS_FAILED,
            DIRECT_STATUS_UNKNOWN,
        }:
            continue
        label = _json_source_label(state)
        error_kind = safe_error_kind(state.get("last_error_kind"))
        detail = state.get("last_error_message") or f"rows={state.get('last_rows_returned')}"
        diagnostic_parts = []
        for diagnostic_label, key in (
            ("malformed", "last_malformed_row_count"),
            ("schema", "last_schema_error_row_count"),
            ("duplicates", "last_duplicate_row_count"),
            ("failed_requests", "last_failed_request_count"),
        ):
            if state.get(key) is not None:
                diagnostic_parts.append(f"{diagnostic_label}={int(state[key])}")
        reasons = state.get("last_reason_codes")
        if isinstance(reasons, (list, tuple)) and reasons:
            diagnostic_parts.append(
                "reasons=" + ",".join(safe_token(item) for item in reasons[:12])
            )
        if diagnostic_parts:
            detail = f"{detail}; {' '.join(diagnostic_parts)}"
        if error_kind:
            detail = f"{error_kind}: {detail}"
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
    data["last_error_kind"] = safe_error_kind(data["last_error_kind"]) if data["last_error_kind"] else None
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
    keys = set(row.keys())
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
        last_malformed_row_count=_row_value(row, keys, "last_malformed_row_count"),
        last_schema_error_row_count=_row_value(row, keys, "last_schema_error_row_count"),
        last_duplicate_row_count=_row_value(row, keys, "last_duplicate_row_count"),
        last_failed_request_count=_row_value(row, keys, "last_failed_request_count"),
        last_incomplete=_row_bool(row, keys, "last_incomplete"),
        last_truncated=_row_bool(row, keys, "last_truncated"),
        last_reason_codes=_row_reason_codes(row, keys),
        last_degraded=_row_bool(row, keys, "last_degraded"),
        last_complete=_row_bool(row, keys, "last_complete"),
    )


def _row_value(row: sqlite3.Row, keys: set[str], name: str) -> object:
    return row[name] if name in keys else None


def _row_bool(row: sqlite3.Row, keys: set[str], name: str) -> bool | None:
    value = _row_value(row, keys, name)
    return None if value is None else bool(value)


def _row_reason_codes(row: sqlite3.Row, keys: set[str]) -> tuple[str, ...]:
    raw = _row_value(row, keys, "last_reason_codes_json")
    if not raw:
        return ()
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    return _bounded_reason_codes(value if isinstance(value, list) else ())


def _status_count(states: Iterable[SourceHealthState], status: str) -> int:
    return sum(state.status == status for state in states)


def _bounded_optional_count(value: object) -> int | None:
    if value is None:
        return None
    try:
        return min(MAX_DIAGNOSTIC_COUNT, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _bounded_reason_codes(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        code = safe_token(value)[:80]
        if code and code not in result:
            result.append(code)
        if len(result) >= MAX_REASON_CODES:
            break
    return tuple(result)


def _optional_bool_int(value: bool | None) -> int | None:
    return None if value is None else int(bool(value))


def safe_token(value: object) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", str(value or "").strip().casefold()).strip("_")


def safe_error_kind(value: object) -> str:
    """Normalize broad/subtype error kinds while preserving one slash."""

    parts = [safe_token(part) for part in str(value or "").split("/", 1)]
    return "/".join(part for part in parts if part)[:96]


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
    scheduled_email_enabled: object = "unknown",
    pending_due_to_email_disabled: object = "unknown",
    scheduled_email_config: object = "unknown",
) -> str:
    """Append workflow-only diagnostics to an exact application heartbeat."""

    if not application_heartbeat or not application_heartbeat.startswith("HEARTBEAT: "):
        raise ValueError("application heartbeat is missing or invalid")
    if "\n" in application_heartbeat or "\r" in application_heartbeat:
        raise ValueError("application heartbeat must be exactly one line")
    values = (
        _heartbeat_workflow_value(scheduled_email_enabled),
        _heartbeat_workflow_value(pending_due_to_email_disabled),
        _heartbeat_workflow_value(scheduled_email_config),
        _heartbeat_workflow_value(seen_loaded),
        _heartbeat_workflow_value(seen_saved),
        _heartbeat_workflow_value(load_status),
        _heartbeat_workflow_value(save_status),
    )
    return (
        f"{application_heartbeat}, scheduled_email_enabled={values[0]}, "
        f"pending_due_to_email_disabled={values[1]}, scheduled_email_config={values[2]}, "
        f"seen_loaded={values[3]}, seen_saved={values[4]}, "
        f"seen_store={values[5]}/{values[6]}"
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
                    scheduled_email_enabled=os.getenv(
                        "SCHEDULED_EMAIL_ENABLED",
                        "unknown",
                    ),
                    pending_due_to_email_disabled=os.getenv(
                        "PENDING_DUE_TO_EMAIL_DISABLED",
                        "unknown",
                    ),
                    scheduled_email_config=os.getenv(
                        "SCHEDULED_EMAIL_CONFIG",
                        "unknown",
                    ),
                )
            )
        except ValueError as exc:
            print(f"::error::WATCHER HEARTBEAT: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
