"""Persistent, deterministic source-health monitoring for watcher runs.

This module is the stable entry point (``python -m watcher.source_health``) and
the compatibility surface for everything that already imports from
``watcher.source_health``. The implementation lives in focused modules:

* :mod:`watcher.health.models` -- status names, coverage states, error kinds,
  and the shared dataclasses.
* :mod:`watcher.health.sanitize` -- the total sanitizers and UTC helpers.
* :mod:`watcher.health.state` -- health keys, ``calculate_next_state``,
  ``transition_for``, and the run summary.
* :mod:`watcher.health.coverage` -- per-run company coverage and GitHub row
  evidence.
* :mod:`watcher.health.store` -- :class:`SourceHealthStore`.
* :mod:`watcher.health.report` -- the JSON artifact, the GitHub Actions report,
  the final heartbeat, and the CLI.

Patch implementations where they are defined rather than through this module:
re-exported names are bound here at import time, so replacing one of them here
does not change what the owning module calls.
"""

from __future__ import annotations

from watcher.health.models import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DEGRADED_BACKSTOP,
    COVERAGE_DIRECT,
    COVERAGE_DIRECT_DEGRADED,
    COVERAGE_DIRECT_EMPTY,
    COVERAGE_FAILING_BACKSTOP,
    COVERAGE_UNCOVERED,
    COVERAGE_UNKNOWN_BACKSTOP,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_NOT_CONFIGURED,
    DIRECT_STATUS_UNKNOWN,
    ERROR_FETCH,
    ERROR_MISSING_ADAPTER,
    ERROR_SCHEMA,
    ERROR_SOURCE,
    ERROR_UNEXPECTED,
    GITHUB_PRIMARY_ATS,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_DEGRADED,
    STATUS_EMPTY,
    STATUS_FAILING,
    STATUS_HEALTHY,
    STATUS_UNKNOWN,
    STATUS_UNSUPPORTED,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceAttempt,
    SourceHealthState,
)
from watcher.health.sanitize import (
    MAX_DIAGNOSTIC_COUNT,
    MAX_ERROR_LENGTH,
    MAX_FEED_LABEL_LENGTH,
    MAX_REASON_CODES,
    _bounded_optional_count,
    _bounded_reason_codes,
    iso_utc,
    parse_utc,
    safe_error_kind,
    safe_run_id,
    safe_token,
    sanitize_error,
    sanitize_feed_label,
    sanitize_plain,
    _sanitize_url_match,
    utc_datetime,
)
from watcher.health.state import (
    calculate_next_state,
    _direct_attempt_status,
    direct_health_key,
    github_feed_health_key,
    new_run_id,
    normalize_attempt,
    _status_count,
    summarize_health,
    transition_for,
)
from watcher.health.coverage import (
    calculate_company_coverage,
    count_github_rows_by_company,
)
from watcher.health.store import (
    SourceHealthStore,
    _optional_bool_int,
    _row_bool,
    _row_reason_codes,
    _row_value,
    _state_from_row,
)
from watcher.health.report import (
    _attempt_dict,
    _coverage_dict,
    _heartbeat_workflow_value,
    _json_safe,
    _json_source_label,
    _main,
    _markdown_row,
    render_final_heartbeat,
    render_github_actions_report,
    _state_dict,
    _transition_dict,
    _workflow_detail_rows,
    write_health_report,
)

__all__ = [
    "COVERAGE_BACKSTOP_ONLY",
    "COVERAGE_DEGRADED_BACKSTOP",
    "COVERAGE_DIRECT",
    "COVERAGE_DIRECT_DEGRADED",
    "COVERAGE_DIRECT_EMPTY",
    "COVERAGE_FAILING_BACKSTOP",
    "COVERAGE_UNCOVERED",
    "COVERAGE_UNKNOWN_BACKSTOP",
    "CompanyCoverage",
    "DIRECT_STATUS_DEGRADED",
    "DIRECT_STATUS_FAILED",
    "DIRECT_STATUS_HEALTHY_EMPTY",
    "DIRECT_STATUS_HEALTHY_WITH_LISTINGS",
    "DIRECT_STATUS_NOT_CONFIGURED",
    "DIRECT_STATUS_UNKNOWN",
    "ERROR_FETCH",
    "ERROR_MISSING_ADAPTER",
    "ERROR_SCHEMA",
    "ERROR_SOURCE",
    "ERROR_UNEXPECTED",
    "GITHUB_PRIMARY_ATS",
    "HealthSummary",
    "HealthTransition",
    "MAX_DIAGNOSTIC_COUNT",
    "MAX_ERROR_LENGTH",
    "MAX_FEED_LABEL_LENGTH",
    "MAX_REASON_CODES",
    "SOURCE_KIND_DIRECT",
    "SOURCE_KIND_GITHUB_FEED",
    "STATUS_DEGRADED",
    "STATUS_EMPTY",
    "STATUS_FAILING",
    "STATUS_HEALTHY",
    "STATUS_UNKNOWN",
    "STATUS_UNSUPPORTED",
    "SourceAttempt",
    "SourceHealthState",
    "SourceHealthStore",
    "calculate_company_coverage",
    "calculate_next_state",
    "count_github_rows_by_company",
    "direct_health_key",
    "github_feed_health_key",
    "iso_utc",
    "new_run_id",
    "normalize_attempt",
    "parse_utc",
    "render_final_heartbeat",
    "render_github_actions_report",
    "safe_error_kind",
    "safe_run_id",
    "safe_token",
    "sanitize_error",
    "sanitize_feed_label",
    "sanitize_plain",
    "summarize_health",
    "transition_for",
    "utc_datetime",
    "write_health_report",
]


if __name__ == "__main__":
    raise SystemExit(_main())
