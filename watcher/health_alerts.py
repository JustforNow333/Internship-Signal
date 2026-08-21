"""Independent source-health alert policy, persistence, rendering, and SMTP.

This module is the compatibility surface for everything that already imports
from ``watcher.health_alerts``. The implementation lives in focused modules:

* :mod:`watcher.health.models` -- alert types, severities, policy thresholds,
  and the alert/digest/incident dataclasses.
* :mod:`watcher.health.policy` -- severity assignment, minor-degradation and
  GitHub-fallback rules, flapping deferral, digest collapsing, and systemic
  grouping.
* :mod:`watcher.health.store` -- :class:`HealthAlertStore`.
* :mod:`watcher.health.rendering` -- immediate alert, daily-summary, and digest
  wording.
* :mod:`watcher.health.service` -- :func:`evaluate_and_send_health_alerts` and
  the SMTP boundary.

Patch implementations where they are defined rather than through this module:
re-exported names are bound here at import time, so replacing one of them here
does not change what the owning module calls.
"""

from __future__ import annotations

from watcher.health.models import (
    ALERT_BOTH_TIERS_UNAVAILABLE,
    ALERT_CONTINUED_FAILURE,
    ALERT_COVERAGE_REGRESSION,
    ALERT_DIRECT_SOURCE_DEGRADED,
    ALERT_FEED_STALE,
    ALERT_MINOR_DEGRADATION,
    ALERT_MINOR_RECOVERY,
    ALERT_NEW_FAILURE,
    ALERT_RECOVERY,
    ALERT_UNKNOWN_DIAGNOSTICS,
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_FEED_STALE_HOURS,
    DEFAULT_HOUR_UTC,
    DEFAULT_MODE,
    DEGRADATION_ALERT_TYPES,
    DIGEST_SEVERITIES,
    DIGEST_WINDOW_HOURS,
    FAILURE_ALERT_TYPES,
    FLAP_LOOKBACK_HOURS,
    FLAP_REPEAT_THRESHOLD,
    GITHUB_EVIDENCE_HORIZON_DAYS,
    HEALTH_EMAIL_MODES,
    MAX_COVERAGE_SNAPSHOTS,
    MAX_DIGEST_CATCHUP_DAYS,
    MAX_DIGEST_EVENTS,
    MAX_DIGEST_INCIDENTS,
    MAX_FLAP_HISTORY_EVENTS,
    MAX_MINOR_SKIPPED_ROWS,
    MIN_RETAINED_ROWS_PER_SKIPPED_ROW,
    MINOR_DEGRADATION_REASONS,
    MODE_DAILY_SUMMARY,
    MODE_FAILURE_ONLY,
    MODE_OFF,
    MODE_TRANSITIONS_ONLY,
    RECOVERY_ALERT_TYPES,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_MEDIUM,
    SEVERITY_ORDER,
    SYSTEMIC_GROUP_DOMINANCE_PERCENT,
    SYSTEMIC_GROUP_MIN_COMPANIES,
    DigestIncident,
    HealthAlertCandidate,
    HealthAlertPolicy,
    HealthAlertResult,
    SystemicIncidentGroup,
)
from watcher.health.policy import (
    _allowed_by_mode,
    _bounded_int,
    _candidate,
    _failure_action,
    _fallback_regressed,
    _fingerprint_token,
    _grouping_error_kind,
    _merge_candidates,
    _recovery_state,
    _state_diagnostic_summary,
    build_alert_candidates,
    build_digest_incidents,
    github_feed_fallback_usable,
    group_systemic_incidents,
    is_minor_degradation,
    load_health_alert_policy,
    repeat_flap_deferrable,
    resolve_digest_window,
    usable_github_fallback,
)
from watcher.health.rendering import (
    _append_bounded,
    _candidate_lines,
    _group_lines,
    _yes_no_unknown,
    render_alert_email,
    render_daily_summary,
    render_source_health_digest,
    summarize_digest_incident,
)
from watcher.health.service import (
    LOGGER,
    SMTP_HOST,
    SMTP_PORT,
    SMTP_TIMEOUT_SECONDS,
    _evaluate_immediate_alerts,
    _health_email_env,
    _maybe_send_daily_digest,
    evaluate_and_send_health_alerts,
    send_health_email,
)
from watcher.health.store import (
    _candidate_from_payload,
    _coverage_snapshot_entries,
    _coverage_snapshot_value,
    _parse_datetime,
    HealthAlertStore,
)

# The private names above are re-exported, not part of the public surface. They
# are kept because health-alert regression tests reach for the candidate,
# payload, and rendering seams by their historical `watcher.health_alerts`
# names.

__all__ = [
    "ALERT_BOTH_TIERS_UNAVAILABLE",
    "ALERT_CONTINUED_FAILURE",
    "ALERT_COVERAGE_REGRESSION",
    "ALERT_DIRECT_SOURCE_DEGRADED",
    "ALERT_FEED_STALE",
    "ALERT_MINOR_DEGRADATION",
    "ALERT_MINOR_RECOVERY",
    "ALERT_NEW_FAILURE",
    "ALERT_RECOVERY",
    "ALERT_UNKNOWN_DIAGNOSTICS",
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_FEED_STALE_HOURS",
    "DEFAULT_HOUR_UTC",
    "DEFAULT_MODE",
    "DEGRADATION_ALERT_TYPES",
    "DIGEST_SEVERITIES",
    "DIGEST_WINDOW_HOURS",
    "FAILURE_ALERT_TYPES",
    "FLAP_LOOKBACK_HOURS",
    "FLAP_REPEAT_THRESHOLD",
    "GITHUB_EVIDENCE_HORIZON_DAYS",
    "HEALTH_EMAIL_MODES",
    "LOGGER",
    "MAX_COVERAGE_SNAPSHOTS",
    "MAX_DIGEST_CATCHUP_DAYS",
    "MAX_DIGEST_EVENTS",
    "MAX_DIGEST_INCIDENTS",
    "MAX_FLAP_HISTORY_EVENTS",
    "MAX_MINOR_SKIPPED_ROWS",
    "MINOR_DEGRADATION_REASONS",
    "MIN_RETAINED_ROWS_PER_SKIPPED_ROW",
    "MODE_DAILY_SUMMARY",
    "MODE_FAILURE_ONLY",
    "MODE_OFF",
    "MODE_TRANSITIONS_ONLY",
    "RECOVERY_ALERT_TYPES",
    "SEVERITY_HIGH",
    "SEVERITY_INFO",
    "SEVERITY_MEDIUM",
    "SEVERITY_ORDER",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_TIMEOUT_SECONDS",
    "SYSTEMIC_GROUP_DOMINANCE_PERCENT",
    "SYSTEMIC_GROUP_MIN_COMPANIES",
    "DigestIncident",
    "HealthAlertCandidate",
    "HealthAlertPolicy",
    "HealthAlertResult",
    "HealthAlertStore",
    "SystemicIncidentGroup",
    "build_alert_candidates",
    "build_digest_incidents",
    "evaluate_and_send_health_alerts",
    "github_feed_fallback_usable",
    "group_systemic_incidents",
    "is_minor_degradation",
    "load_health_alert_policy",
    "render_alert_email",
    "render_daily_summary",
    "render_source_health_digest",
    "repeat_flap_deferrable",
    "resolve_digest_window",
    "send_health_email",
    "summarize_digest_incident",
    "usable_github_fallback",
]
