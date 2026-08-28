"""Shared source-health constants, dataclasses, and policy thresholds."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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


GITHUB_PRIMARY_ATS = frozenset({"bespoke", "github_only"})


ERROR_FETCH = "fetch_failure"


ERROR_SCHEMA = "schema_failure"


ERROR_MISSING_ADAPTER = "missing_adapter_registration"


ERROR_UNEXPECTED = "unexpected_exception"


ERROR_SOURCE = "source_failure"


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
    github_rows_returned: int | None = None
    github_fallback_configured: bool = False


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


ALERT_NEW_FAILURE = "new_failure"


ALERT_CONTINUED_FAILURE = "continued_failure"


ALERT_DIRECT_SOURCE_DEGRADED = "direct_source_degraded"


ALERT_UNKNOWN_DIAGNOSTICS = "unknown_diagnostics"


ALERT_FEED_STALE = "feed_stale"


ALERT_RECOVERY = "recovery"


ALERT_MINOR_DEGRADATION = "minor_degradation"


ALERT_MINOR_RECOVERY = "minor_recovery"


ALERT_BOTH_TIERS_UNAVAILABLE = "both_tiers_unavailable"


ALERT_COVERAGE_REGRESSION = "coverage_regression"


SEVERITY_HIGH = "high"


SEVERITY_MEDIUM = "medium"


SEVERITY_INFO = "info"


DIGEST_SEVERITIES = (SEVERITY_MEDIUM, SEVERITY_INFO)


# Legacy persisted payloads may still contain ``critical``.
SEVERITY_ORDER = {
    "critical": 0,
    SEVERITY_HIGH: 1,
    SEVERITY_MEDIUM: 2,
    SEVERITY_INFO: 3,
}


MINOR_DEGRADATION_REASONS = frozenset(
    {
        "schema_invalid_records_skipped",
        "malformed_records_skipped",
        "request_retry_recovered",
        "pagination_restart_recovered",
    }
)


DEGRADATION_ALERT_TYPES = (
    ALERT_NEW_FAILURE,
    ALERT_CONTINUED_FAILURE,
    "direct_source_silence",
    ALERT_DIRECT_SOURCE_DEGRADED,
    ALERT_UNKNOWN_DIAGNOSTICS,
    ALERT_FEED_STALE,
    ALERT_MINOR_DEGRADATION,
)


RECOVERY_ALERT_TYPES = frozenset({ALERT_RECOVERY, ALERT_MINOR_RECOVERY})


FAILURE_ALERT_TYPES = frozenset({ALERT_NEW_FAILURE, ALERT_CONTINUED_FAILURE})


MAX_MINOR_SKIPPED_ROWS = 5


MIN_RETAINED_ROWS_PER_SKIPPED_ROW = 20


DIGEST_WINDOW_HOURS = 24


MAX_DIGEST_INCIDENTS = 25


MAX_DIGEST_CATCHUP_DAYS = 7


MAX_DIGEST_EVENTS = 20_000


GITHUB_EVIDENCE_HORIZON_DAYS = 7


MAX_COVERAGE_SNAPSHOTS = 200


# A source that keeps failing and recovering on one error restates a mode that
# was already alerted. After this many prior occurrences of the *same* sanitized
# error kind inside the lookback window, one more isolated failure is a repeat
# rather than news, so it joins the daily digest instead of interrupting again.
# The window spans seven days of hourly runs, matching the alert-event history
# this reads; nothing new is persisted for it.
FLAP_LOOKBACK_HOURS = 168


FLAP_REPEAT_THRESHOLD = 3


# Defensive bound on how many stored failure events one lookback may load.
MAX_FLAP_HISTORY_EVENTS = 20_000


SYSTEMIC_GROUP_MIN_COMPANIES = 5


SYSTEMIC_GROUP_DOMINANCE_PERCENT = 60


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
    diagnostic_summary: str = ""
    reason_codes: tuple[str, ...] = ()
    adapter: str = ""
    github_fallback_usable: bool | None = None


@dataclass(frozen=True)
class DigestIncident:
    """One source's collapsed MEDIUM/INFO lifecycle in a reporting window."""

    health_key: str
    source_label: str
    severity: str
    alert_types: tuple[str, ...]
    occurrences: int
    first_detected_at: str
    last_detected_at: str
    retained_rows: int | None
    reason_codes: tuple[str, ...]
    diagnostic_summary: str
    recovered: str
    escalated: bool = False


@dataclass(frozen=True)
class SystemicIncidentGroup:
    """One run's shared same-family failure, for presentation only."""

    adapter_family: str
    error_kind: str
    companies: tuple[str, ...]
    run_id: str
    recommended_action: str

    @property
    def affected_companies(self) -> int:
        return len(self.companies)


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
    daily_digest_sent: bool = False
    digest_incidents_reported: int = 0
    digest_catchup_clamped: bool = False
