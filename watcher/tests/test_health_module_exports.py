"""Guard the ``watcher.source_health`` and ``watcher.health_alerts`` facades.

The health implementation lives in :mod:`watcher.health`, but the two original
module paths remain the import surface for tests, scripts, the scheduled
workflow, and production callers. These tests fail if a symbol stops resolving
through a facade, if a facade export drifts away from the module that owns it,
or if the one-way dependency direction inside the package is broken.
"""

import ast
import importlib
import pkgutil
from pathlib import Path

import pytest

import watcher.health
from watcher import health_alerts, source_health

HEALTH_MODULES = (
    "watcher.health.coverage",
    "watcher.health.models",
    "watcher.health.policy",
    "watcher.health.rendering",
    "watcher.health.report",
    "watcher.health.sanitize",
    "watcher.health.service",
    "watcher.health.state",
    "watcher.health.store",
)

# What each module may import from inside the package. Lower layers never reach
# upward, persistence never depends on delivery, policy performs no database or
# email work, and rendering never touches the stores.
ALLOWED_PACKAGE_IMPORTS = {
    "watcher.health.models": set(),
    "watcher.health.sanitize": set(),
    "watcher.health.state": {"models", "sanitize"},
    "watcher.health.coverage": {"models", "sanitize", "state"},
    "watcher.health.store": {"models", "sanitize", "state"},
    "watcher.health.policy": {"models", "sanitize", "state", "coverage"},
    "watcher.health.rendering": {"models", "sanitize", "policy"},
    "watcher.health.service": {
        "models", "sanitize", "state", "coverage", "store", "policy", "rendering",
    },
    "watcher.health.report": {"models", "sanitize", "state", "coverage"},
}

# Modules the health package must never import: health is a leaf of the watcher
# run architecture, never a consumer of it.
FORBIDDEN_IMPORTS = (
    "watcher.pipeline",
    "watcher.collection",
    "watcher.reporting",
    "watcher.cli",
    "watcher.run",
    "watcher.notify",
)

# Every name production code, scripts, or tests import from the facades today.
SOURCE_HEALTH_EXPORTS = (
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
)

HEALTH_ALERT_EXPORTS = (
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
    "DigestIncident",
    "FAILURE_ALERT_TYPES",
    "FLAP_LOOKBACK_HOURS",
    "FLAP_REPEAT_THRESHOLD",
    "GITHUB_EVIDENCE_HORIZON_DAYS",
    "HEALTH_EMAIL_MODES",
    "HealthAlertCandidate",
    "HealthAlertPolicy",
    "HealthAlertResult",
    "HealthAlertStore",
    "LOGGER",
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
)

# Private seams kept re-exported because probes, benchmarks, and regression
# tests reach for them by their historical facade names.
PRIVATE_SEAMS = (
    (source_health, "_attempt_dict"),
    (source_health, "_bounded_optional_count"),
    (source_health, "_bounded_reason_codes"),
    (source_health, "_coverage_dict"),
    (source_health, "_direct_attempt_status"),
    (source_health, "_heartbeat_workflow_value"),
    (source_health, "_json_safe"),
    (source_health, "_json_source_label"),
    (source_health, "_main"),
    (source_health, "_markdown_row"),
    (source_health, "_optional_bool_int"),
    (source_health, "_row_bool"),
    (source_health, "_row_reason_codes"),
    (source_health, "_row_value"),
    (source_health, "_sanitize_url_match"),
    (source_health, "_state_dict"),
    (source_health, "_state_from_row"),
    (source_health, "_status_count"),
    (source_health, "_transition_dict"),
    (source_health, "_workflow_detail_rows"),
    (health_alerts, "_allowed_by_mode"),
    (health_alerts, "_append_bounded"),
    (health_alerts, "_bounded_int"),
    (health_alerts, "_candidate"),
    (health_alerts, "_candidate_from_payload"),
    (health_alerts, "_candidate_lines"),
    (health_alerts, "_coverage_snapshot_entries"),
    (health_alerts, "_coverage_snapshot_value"),
    (health_alerts, "_empty_alert_result"),
    (health_alerts, "_evaluate_immediate_alerts"),
    (health_alerts, "_failure_action"),
    (health_alerts, "_fallback_regressed"),
    (health_alerts, "_fingerprint_token"),
    (health_alerts, "_group_lines"),
    (health_alerts, "_grouping_error_kind"),
    (health_alerts, "_health_email_env"),
    (health_alerts, "_maybe_send_daily_digest"),
    (health_alerts, "_merge_candidates"),
    (health_alerts, "_parse_datetime"),
    (health_alerts, "_recovery_state"),
    (health_alerts, "_state_diagnostic_summary"),
    (health_alerts, "_yes_no_unknown"),
)


@pytest.mark.parametrize("name", SOURCE_HEALTH_EXPORTS)
def test_source_health_facade_still_exports(name):
    assert hasattr(source_health, name)
    assert name in source_health.__all__


@pytest.mark.parametrize("name", HEALTH_ALERT_EXPORTS)
def test_health_alerts_facade_still_exports(name):
    assert hasattr(health_alerts, name)
    assert name in health_alerts.__all__


@pytest.mark.parametrize("module, name", PRIVATE_SEAMS)
def test_private_seams_stay_importable(module, name):
    assert hasattr(module, name)


@pytest.mark.parametrize("name", HEALTH_MODULES)
def test_every_health_module_imports_on_its_own(name):
    """A module that only imports inside the package would hide a cycle."""

    assert importlib.import_module(name) is not None


def test_health_package_lists_exactly_the_expected_modules():
    found = {
        f"watcher.health.{info.name}"
        for info in pkgutil.iter_modules(watcher.health.__path__)
    }
    assert found == set(HEALTH_MODULES)


def _package_imports(name):
    module = importlib.import_module(name)
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    inside, outside = set(), set()
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        for imported in modules:
            if imported.startswith("watcher.health."):
                inside.add(imported.split(".")[2])
            elif imported.startswith("watcher.") or imported.startswith("backend."):
                outside.add(imported)
    return inside, outside


@pytest.mark.parametrize("name", HEALTH_MODULES)
def test_package_dependencies_flow_one_way(name):
    inside, _ = _package_imports(name)
    assert inside <= ALLOWED_PACKAGE_IMPORTS[name], name


@pytest.mark.parametrize("name", HEALTH_MODULES)
def test_health_modules_never_import_the_run_architecture(name):
    _, outside = _package_imports(name)
    assert not (outside & set(FORBIDDEN_IMPORTS)), name


def test_facade_exports_are_the_objects_their_modules_own():
    from watcher.health import coverage, models, policy, rendering
    from watcher.health import report, sanitize, service, state, store

    assert source_health.calculate_next_state is state.calculate_next_state
    assert source_health.sanitize_error is sanitize.sanitize_error
    assert source_health.SourceHealthStore is store.SourceHealthStore
    assert source_health.SourceAttempt is models.SourceAttempt
    assert (
        source_health.calculate_company_coverage is coverage.calculate_company_coverage
    )
    assert source_health.write_health_report is report.write_health_report
    assert health_alerts.HealthAlertStore is store.HealthAlertStore
    assert health_alerts.is_minor_degradation is policy.is_minor_degradation
    assert health_alerts.repeat_flap_deferrable is policy.repeat_flap_deferrable
    assert health_alerts.render_alert_email is rendering.render_alert_email
    assert (
        health_alerts.evaluate_and_send_health_alerts
        is service.evaluate_and_send_health_alerts
    )


def test_the_health_alerts_logger_name_is_pinned():
    """The console format prints the logger name; operators grep for this one."""

    assert health_alerts.LOGGER.name == "watcher.health_alerts"
