"""Guard the ``watcher.source_health`` and ``watcher.health_alerts`` facades.

The health implementation lives in :mod:`watcher.health`, but the two original
module paths remain the import surface for tests, scripts, the scheduled
workflow, and production callers. These tests fail if a symbol stops resolving
through a facade, or if a facade export drifts away from the module that owns
it.
"""

import importlib
import pkgutil

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

# Every name production code, scripts, or tests import from the facades today.
SOURCE_HEALTH_EXPORTS = (
    "COVERAGE_AUDIT_BACKSTOP_ONLY",
    "COVERAGE_AUDIT_DIRECT_DEGRADED",
    "COVERAGE_AUDIT_DIRECT_VERIFIED",
    "COVERAGE_AUDIT_NEEDS_INVESTIGATION",
    "COVERAGE_AUDIT_NO_SOURCE_FOUND",
    "COVERAGE_BACKSTOP_ONLY",
    "COVERAGE_DEGRADED_BACKSTOP",
    "COVERAGE_DIRECT",
    "COVERAGE_DIRECT_EMPTY",
    "COVERAGE_FAILING_BACKSTOP",
    "COVERAGE_UNCOVERED",
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
    "MAX_ERROR_LENGTH",
    "SOURCE_KIND_DIRECT",
    "SOURCE_KIND_GITHUB_FEED",
    "STATUS_DEGRADED",
    "STATUS_EMPTY",
    "STATUS_FAILING",
    "STATUS_HEALTHY",
    "STATUS_UNSUPPORTED",
    "SourceAttempt",
    "SourceHealthState",
    "SourceHealthStore",
    "build_coverage_audit",
    "calculate_company_coverage",
    "calculate_next_state",
    "count_github_rows_by_company",
    "direct_health_key",
    "github_feed_health_key",
    "iso_utc",
    "new_run_id",
    "render_coverage_audit",
    "render_final_heartbeat",
    "render_github_actions_report",
    "safe_error_kind",
    "safe_run_id",
    "sanitize_error",
    "sanitize_feed_label",
    "summarize_health",
    "transition_for",
    "utc_datetime",
    "write_health_report",
)

HEALTH_ALERT_EXPORTS = (
    "DEFAULT_FEED_STALE_HOURS",
    "FLAP_LOOKBACK_HOURS",
    "FLAP_REPEAT_THRESHOLD",
    "GITHUB_EVIDENCE_HORIZON_DAYS",
    "HealthAlertPolicy",
    "HealthAlertResult",
    "HealthAlertStore",
    "MAX_COVERAGE_SNAPSHOTS",
    "MAX_DIGEST_CATCHUP_DAYS",
    "MODE_DAILY_SUMMARY",
    "MODE_FAILURE_ONLY",
    "MODE_OFF",
    "MODE_TRANSITIONS_ONLY",
    "SEVERITY_HIGH",
    "SEVERITY_INFO",
    "SEVERITY_MEDIUM",
    "SYSTEMIC_GROUP_MIN_COMPANIES",
    "build_alert_candidates",
    "evaluate_and_send_health_alerts",
    "group_systemic_incidents",
    "is_minor_degradation",
    "load_health_alert_policy",
    "render_alert_email",
    "repeat_flap_deferrable",
    "resolve_digest_window",
)

# Private seams kept re-exported because probes, benchmarks, and regression
# tests reach for them by their historical facade names.
PRIVATE_SEAMS = (
    (source_health, "_bounded_optional_count"),
    (source_health, "_bounded_reason_codes"),
    (source_health, "_direct_attempt_status"),
    (source_health, "_json_safe"),
    (source_health, "_main"),
    (source_health, "_sanitize_url_match"),
    (source_health, "_state_from_row"),
    (source_health, "_workflow_detail_rows"),
    (health_alerts, "_allowed_by_mode"),
    (health_alerts, "_candidate"),
    (health_alerts, "_candidate_from_payload"),
    (health_alerts, "_candidate_lines"),
    (health_alerts, "_coverage_snapshot_entries"),
    (health_alerts, "_evaluate_immediate_alerts"),
    (health_alerts, "_health_email_env"),
    (health_alerts, "_maybe_send_daily_digest"),
    (health_alerts, "_merge_candidates"),
    (health_alerts, "_parse_datetime"),
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


def test_facade_exports_are_the_objects_their_modules_own():
    from watcher.health import coverage, models, policy, rendering
    from watcher.health import report, sanitize, service, state, store

    assert source_health.calculate_next_state is state.calculate_next_state
    assert source_health.sanitize_error is sanitize.sanitize_error
    assert source_health.SourceHealthStore is store.SourceHealthStore
    assert source_health.SourceAttempt is models.SourceAttempt
    assert source_health.build_coverage_audit is coverage.build_coverage_audit
    assert source_health.write_health_report is report.write_health_report
    assert health_alerts.HealthAlertStore is store.HealthAlertStore
    assert health_alerts.is_minor_degradation is policy.is_minor_degradation
    assert health_alerts.render_alert_email is rendering.render_alert_email
    assert (
        health_alerts.evaluate_and_send_health_alerts
        is service.evaluate_and_send_health_alerts
    )


def test_the_health_alerts_logger_name_is_pinned():
    """The console format prints the logger name; operators grep for this one."""

    assert health_alerts.LOGGER.name == "watcher.health_alerts"
