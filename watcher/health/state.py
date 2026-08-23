"""Health keys, per-attempt state transitions, and run-level status counts.

Pure functions: nothing here reads configuration, touches SQLite, or performs
I/O. ``calculate_next_state`` is the single place a status name is decided.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

from internship_signal.domain.identity import norm_company
from watcher.config import CompanyCfg
from watcher.health.models import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_UNCOVERED,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_NOT_CONFIGURED,
    DIRECT_STATUS_UNKNOWN,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_DEGRADED,
    STATUS_EMPTY,
    STATUS_FAILING,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceAttempt,
    SourceHealthState,
)
from watcher.health.sanitize import (
    _bounded_optional_count,
    _bounded_reason_codes,
    safe_error_kind,
    safe_token,
    sanitize_error,
    sanitize_feed_label,
    sanitize_plain,
    utc_datetime,
)


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


def _status_count(states: Iterable[SourceHealthState], status: str) -> int:
    return sum(state.status == status for state in states)
