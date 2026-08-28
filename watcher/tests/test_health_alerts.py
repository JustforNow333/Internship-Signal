from __future__ import annotations

import sqlite3
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from watcher.config import CompanyCfg
from watcher.config import WatcherConfig
from watcher.health_alerts import (
    DEFAULT_FEED_STALE_HOURS,
    GITHUB_EVIDENCE_HORIZON_DAYS,
    MAX_COVERAGE_SNAPSHOTS,
    MAX_DIGEST_CATCHUP_DAYS,
    MODE_DAILY_SUMMARY,
    MODE_FAILURE_ONLY,
    MODE_OFF,
    MODE_TRANSITIONS_ONLY,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_MEDIUM,
    SYSTEMIC_GROUP_MIN_COMPANIES,
    HealthAlertCandidate,
    HealthAlertPolicy,
    HealthAlertStore,
    _merge_candidates,
    build_alert_candidates,
    evaluate_and_send_health_alerts,
    group_systemic_incidents,
    is_minor_degradation,
    load_health_alert_policy,
    render_alert_email,
    resolve_digest_window,
)
from watcher.seen_store import SeenStore
from watcher.run import RUN_MODE_LIVE, run_once
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT,
    COVERAGE_FAILING_BACKSTOP,
    COVERAGE_UNCOVERED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    STATUS_FAILING,
    STATUS_HEALTHY,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceHealthState,
    SourceAttempt,
    SourceHealthStore,
    direct_health_key,
)
from watcher.sources import SourceFetchError
from watcher.sources.base import make_row


NOW = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
BEFORE_DIGEST_HOUR = NOW.replace(hour=6)
DIGEST_SUBJECT_MARKER = "Daily Source Health"


def _state(
    *,
    key="company:test:direct:greenhouse",
    company="Test Co",
    source_kind=SOURCE_KIND_DIRECT,
    status=STATUS_HEALTHY,
    previous_status="healthy",
    failures=0,
    empty=0,
    last_nonzero=NOW,
    last_success=NOW,
    rows=10,
    error=None,
    feed_label=None,
    adapter=None,
    malformed=None,
    schema_errors=None,
    duplicates=None,
    failed_requests=None,
    reason_codes=(),
    incomplete=None,
    truncated=None,
    degraded=None,
    complete=None,
):
    return SourceHealthState(
        health_key=key,
        source_kind=source_kind,
        company=company,
        adapter=adapter or (
            "greenhouse"
            if source_kind == SOURCE_KIND_DIRECT
            else "simplify_json"
        ),
        feed_label=feed_label,
        unsupported_reason=None,
        status=status,
        previous_status=previous_status,
        total_attempts=10,
        total_successes=8,
        consecutive_failures=failures,
        consecutive_zero_successes=empty,
        last_attempt_at=NOW,
        last_success_at=last_success,
        last_nonzero_at=last_nonzero,
        last_rows_returned=rows,
        last_error_kind=error,
        last_error_message="token=secret https://user:pass@example.com?q=secret"
        if error
        else None,
        last_malformed_row_count=malformed,
        last_schema_error_row_count=schema_errors,
        last_duplicate_row_count=duplicates,
        last_failed_request_count=failed_requests,
        last_incomplete=incomplete,
        last_truncated=truncated,
        last_reason_codes=tuple(reason_codes),
        last_degraded=(
            status == DIRECT_STATUS_DEGRADED
            if degraded is None
            else degraded
        ),
        last_complete=complete,
    )


def _summary(**overrides):
    values = {field.name: 0 for field in fields(HealthSummary)}
    values.update(overrides)
    return HealthSummary(**values)


def _coverage(
    state=COVERAGE_DIRECT,
    *,
    direct_status="healthy",
    github=True,
):
    return CompanyCoverage(
        company="Test Co",
        adapter="greenhouse",
        state=state,
        direct_status=direct_status,
        direct_attempt_succeeded=direct_status == "healthy",
        direct_rows_returned=10 if direct_status == "healthy" else None,
        github_backstop_available=github,
    )


def _transition(from_status, to_status, *, recovery=False):
    return HealthTransition(
        health_key="company:test:direct:greenhouse",
        source_kind=SOURCE_KIND_DIRECT,
        company="Test Co",
        adapter="greenhouse",
        feed_label=None,
        from_status=from_status,
        to_status=to_status,
        recovery=recovery,
    )


def _evaluate(
    tmp_path,
    *,
    state,
    transition=(),
    coverage=None,
    policy=None,
    now=NOW,
    sender=None,
):
    calls = []

    def default_sender(subject, body):
        calls.append((subject, body))
        return True

    result = evaluate_and_send_health_alerts(
        db_path=tmp_path / "state.sqlite",
        policy=policy or HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id=f"run-{now.hour}-{now.day}",
        observed_at=now,
        states={state.health_key: state},
        transitions=tuple(transition),
        coverage=tuple(coverage or (_coverage(),)),
        summary=_summary(),
        comparison=None,
        sender=sender or default_sender,
    )
    return result, calls


def _evaluate_states(
    tmp_path,
    *,
    states,
    transitions=(),
    now=NOW,
    sender=None,
):
    calls = []

    def default_sender(subject, body):
        calls.append((subject, body))
        return True

    result = evaluate_and_send_health_alerts(
        db_path=tmp_path / "state.sqlite",
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id=f"run-{now.isoformat()}",
        observed_at=now,
        states={state.health_key: state for state in states},
        transitions=tuple(transitions),
        coverage=(_coverage(),),
        summary=_summary(),
        comparison=None,
        sender=sender or default_sender,
    )
    return result, calls


@pytest.mark.parametrize("from_status", ["healthy", "degraded"])
def test_failure_transition_sends(from_status, tmp_path):
    state = _state(
        status=STATUS_FAILING,
        previous_status=from_status,
        failures=3,
        rows=None,
        error="schema_failure",
    )
    result, calls = _evaluate(
        tmp_path,
        state=state,
        transition=(_transition(from_status, STATUS_FAILING),),
    )
    assert result.sent is True
    assert result.candidates == 1
    assert "Source Alert" in calls[0][0]


def test_continued_failure_respects_cooldown_and_resends_afterward(tmp_path):
    policy = HealthAlertPolicy(mode=MODE_FAILURE_ONLY, cooldown_hours=24)
    failing = _state(
        status=STATUS_FAILING,
        failures=6,
        rows=None,
        error="fetch_failure",
    )
    first, _ = _evaluate(tmp_path, state=failing, policy=policy)
    second, _ = _evaluate(
        tmp_path,
        state=replace(failing, consecutive_failures=7),
        policy=policy,
        now=NOW + timedelta(hours=1),
    )
    third, _ = _evaluate(
        tmp_path,
        state=replace(failing, consecutive_failures=8),
        policy=policy,
        now=NOW + timedelta(hours=25),
    )
    assert first.sent is True
    assert second.sent is False
    assert second.suppressed_by_cooldown == 1
    assert third.sent is True


def test_high_failure_then_recovery_reports_recovery_in_digest_only(tmp_path):
    failing = _state(
        status=STATUS_FAILING,
        failures=3,
        rows=None,
        error="schema_failure",
    )
    _evaluate(
        tmp_path,
        state=failing,
        transition=(_transition("healthy", STATUS_FAILING),),
        now=BEFORE_DIGEST_HOUR,
    )
    recovered = _state(
        status=STATUS_HEALTHY,
        previous_status=STATUS_FAILING,
        failures=0,
        rows=142,
    )
    result, calls = _evaluate(
        tmp_path,
        state=recovered,
        transition=(_transition(STATUS_FAILING, STATUS_HEALTHY, recovery=True),),
        now=BEFORE_DIGEST_HOUR + timedelta(hours=1),
    )
    digest, digest_calls = _evaluate(
        tmp_path,
        state=replace(recovered, previous_status=STATUS_HEALTHY),
        now=NOW,
    )

    assert result.sent is False
    assert result.recovery_alerts == 1
    assert calls == []
    assert digest.daily_digest_sent is True
    assert DIGEST_SUBJECT_MARKER in digest_calls[0][0]
    assert "INFO" in digest_calls[0][1]
    assert "recovered" in digest_calls[0][1]


def test_medium_and_info_incidents_are_digest_only(tmp_path):
    medium = _state(
        status=DIRECT_STATUS_DEGRADED,
        rows=10,
        incomplete=True,
        complete=False,
        reason_codes=("pagination_ended_early",),
    )
    info = _successfactors_recovery_state()

    for state, severity in ((medium, SEVERITY_MEDIUM), (info, SEVERITY_INFO)):
        candidate = _degradation_candidate(state)
        result, calls = _evaluate(
            tmp_path / severity,
            state=state,
            now=BEFORE_DIGEST_HOUR,
        )
        assert candidate.severity == severity
        assert result.sent is False
        assert calls == []


def test_backstop_only_daily_summary_does_not_spam_hourly(tmp_path):
    policy = HealthAlertPolicy(mode=MODE_DAILY_SUMMARY, hour_utc=12)
    coverage = (_coverage(COVERAGE_BACKSTOP_ONLY, direct_status="unsupported"),)
    healthy = _state()
    first, calls = _evaluate(
        tmp_path,
        state=healthy,
        coverage=coverage,
        policy=policy,
    )
    second, second_calls = _evaluate(
        tmp_path,
        state=healthy,
        coverage=coverage,
        policy=policy,
        now=NOW + timedelta(hours=1),
    )
    assert first.daily_summary_sent is True
    assert "backstop-only companies: 1" in calls[0][1]
    assert second.sent is False
    assert second_calls == []


def test_stale_feed_requires_prior_nonzero_activity(tmp_path):
    policy = HealthAlertPolicy(
        mode=MODE_FAILURE_ONLY,
        feed_stale_hours=48,
    )
    stale = _state(
        key="github_feed:abc",
        company=None,
        source_kind=SOURCE_KIND_GITHUB_FEED,
        feed_label="Simplify",
        last_nonzero=NOW - timedelta(hours=49),
        rows=0,
    )
    result, calls = _evaluate(
        tmp_path,
        state=stale,
        coverage=(),
        policy=policy,
    )
    never_productive = replace(stale, last_nonzero_at=None)
    second, second_calls = _evaluate(
        tmp_path,
        state=never_productive,
        coverage=(),
        policy=policy,
        now=NOW + timedelta(hours=1),
    )
    assert result.sent is False
    assert result.daily_digest_sent is True
    assert "feed_stale" in calls[0][1]
    assert "MEDIUM" in calls[0][1]
    assert second.sent is False
    assert second_calls == []


def test_valid_zero_role_feed_is_not_a_failure(tmp_path):
    healthy = _state(
        key="github_feed:abc",
        company=None,
        source_kind=SOURCE_KIND_GITHUB_FEED,
        feed_label="Simplify",
        last_nonzero=NOW,
        rows=0,
    )
    result, calls = _evaluate(
        tmp_path,
        state=healthy,
        coverage=(),
        policy=HealthAlertPolicy(mode=MODE_FAILURE_ONLY),
    )
    assert result.candidates == 0
    assert result.sent is False
    assert calls == []


def test_previously_productive_direct_source_silence_reaches_digest(tmp_path):
    silent = _state(
        status="degraded",
        previous_status="empty",
        empty=3,
        last_nonzero=NOW - timedelta(days=2),
        rows=0,
    )
    result, calls = _evaluate(
        tmp_path,
        state=silent,
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
    )
    assert result.sent is False
    assert result.daily_digest_sent is True
    assert "direct_source_degraded" in calls[0][1]


def _successfactors_recovery_state(**overrides):
    values = {
        "key": "company:test:direct:successfactors",
        "adapter": "successfactors",
        "status": DIRECT_STATUS_DEGRADED,
        "previous_status": "healthy_with_listings",
        "rows": 118,
        "reason_codes": ("pagination_restart_recovered",),
        "incomplete": False,
        "truncated": False,
        "complete": True,
    }
    values.update(overrides)
    return _state(**values)


def _degradation_candidate(state):
    candidates = build_alert_candidates(
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id="pagination-restart-policy",
        observed_at=NOW,
        states={state.health_key: state},
        transitions=(),
        coverage=(_coverage(),),
        previous_coverage=None,
    )
    return next(item for item in candidates if item.health_key == state.health_key)


def test_successful_successfactors_pagination_restart_is_minor_info(tmp_path):
    state = _successfactors_recovery_state()

    candidate = _degradation_candidate(state)
    result, calls = _evaluate(tmp_path, state=state)

    assert is_minor_degradation(state) is True
    assert candidate.alert_type == "minor_degradation"
    assert candidate.severity == "info"
    assert result.candidates == 1
    assert result.sent is False
    assert result.daily_digest_sent is True
    assert "INFO" in calls[0][1]


@pytest.mark.parametrize("adapter", ["bain", "epic", "ibm"])
def test_complete_recovered_request_retry_is_minor_info(adapter):
    state = _successfactors_recovery_state(
        key=f"company:test:direct:{adapter}",
        adapter=adapter,
        reason_codes=("request_retry_recovered",),
    )

    candidate = _degradation_candidate(state)

    assert is_minor_degradation(state) is True
    assert candidate.alert_type == "minor_degradation"
    assert candidate.severity == "info"


@pytest.mark.parametrize("adapter", ["bain", "epic", "ibm"])
def test_recovered_retry_with_any_untrusted_collection_signal_is_not_minor(adapter):
    state = _successfactors_recovery_state(
        key=f"company:test:direct:{adapter}",
        adapter=adapter,
        reason_codes=("request_retry_recovered",),
        incomplete=True,
    )

    assert is_minor_degradation(state) is False
    assert _degradation_candidate(state).severity == "medium"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"complete": False}, id="not-complete"),
        pytest.param({"incomplete": True}, id="incomplete"),
        pytest.param({"truncated": True}, id="truncated"),
    ],
)
def test_untrustworthy_successfactors_restart_is_not_minor(overrides):
    state = _successfactors_recovery_state(**overrides)

    candidate = _degradation_candidate(state)

    assert is_minor_degradation(state) is False
    assert candidate.alert_type == "direct_source_degraded"
    assert candidate.severity == "medium"


def test_failed_successfactors_restart_keeps_failure_severity():
    state = _state(
        key="company:test:direct:successfactors",
        adapter="successfactors",
        status=DIRECT_STATUS_FAILED,
        rows=None,
        failures=2,
        error="schema_failure",
        reason_codes=("schema_failure",),
        incomplete=True,
        truncated=False,
        complete=False,
    )

    candidate = _degradation_candidate(state)

    assert is_minor_degradation(state) is False
    assert candidate.error_kind == "schema_failure"
    assert candidate.severity == "high"


def test_tiny_schema_loss_is_info_and_reaches_digest(tmp_path):
    state = _state(
        status=DIRECT_STATUS_DEGRADED,
        previous_status="healthy_with_listings",
        rows=412,
        schema_errors=1,
        malformed=0,
        reason_codes=("schema_invalid_records_skipped",),
        incomplete=True,
        truncated=False,
        complete=False,
    )

    quiet, quiet_calls = _evaluate(
        tmp_path,
        state=state,
        now=BEFORE_DIGEST_HOUR,
    )
    digest, digest_calls = _evaluate(tmp_path, state=state, now=NOW)

    assert is_minor_degradation(state) is True
    assert quiet.sent is False
    assert quiet_calls == []
    assert digest.daily_digest_sent is True
    assert "schema_invalid_records_skipped" in digest_calls[0][1]
    assert "schema=1" in digest_calls[0][1]


@pytest.mark.parametrize(
    "overrides",
    [
        {"schema_errors": 6},
        {"schema_errors": 1, "rows": 10},
        {"schema_errors": 0},
        {"reason_codes": ("unmapped_future_anomaly",)},
    ],
)
def test_untrusted_record_loss_remains_medium(overrides):
    values = {
        "status": DIRECT_STATUS_DEGRADED,
        "rows": 412,
        "schema_errors": 1,
        "malformed": 0,
        "reason_codes": ("schema_invalid_records_skipped",),
        "incomplete": True,
        "truncated": False,
        "complete": False,
    }
    values.update(overrides)
    state = _state(**values)

    assert is_minor_degradation(state) is False
    assert _degradation_candidate(state).severity == SEVERITY_MEDIUM


def test_coverage_regression_and_both_tiers_unavailable_are_high_severity(tmp_path):
    healthy = _state()
    _evaluate(tmp_path, state=healthy, coverage=(_coverage(),))
    result, calls = _evaluate(
        tmp_path,
        state=healthy,
        coverage=(_coverage(COVERAGE_UNCOVERED, direct_status="failing", github=False),),
        now=NOW + timedelta(hours=1),
    )
    assert result.sent is True
    body = calls[0][1]
    assert "both_tiers_unavailable" in body
    assert "CRITICAL" not in body
    assert body.count("HIGH:") == 2
    assert "coverage_regression" in body


def test_no_active_candidate_uses_critical_severity():
    candidates = build_alert_candidates(
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id="severity-set",
        observed_at=NOW,
        states={
            "failed": _state(
                key="failed",
                status=DIRECT_STATUS_FAILED,
                rows=None,
                error="fetch_failure",
            ),
            "medium": _state(
                key="medium",
                status=DIRECT_STATUS_DEGRADED,
                incomplete=True,
                complete=False,
                reason_codes=("pagination_ended_early",),
            ),
            "info": _successfactors_recovery_state(key="info"),
        },
        transitions=(),
        coverage=(
            _coverage(COVERAGE_UNCOVERED, direct_status="failing", github=False),
        ),
        previous_coverage=None,
    )

    assert {candidate.severity for candidate in candidates} <= {
        SEVERITY_HIGH,
        SEVERITY_MEDIUM,
        SEVERITY_INFO,
    }


def test_health_smtp_failure_does_not_change_seen_state(tmp_path):
    db = tmp_path / "state.sqlite"
    job = {
        "id": "job-1",
        "company": "Test Co",
        "title": "Software Intern",
        "location": "US",
        "source_url": "https://example.com/jobs/1",
        "extra": {},
    }
    with SeenStore(db) as seen:
        seen.mark_emailed(job, emailed_at=NOW)
        before = seen.records()

    def fail(subject, body):
        raise RuntimeError(
            "password=hunter2 https://user:pass@example.com/path?token=abc"
        )

    state = _state(
        status=STATUS_FAILING,
        failures=3,
        rows=None,
        error="fetch_failure",
    )
    result = evaluate_and_send_health_alerts(
        db_path=db,
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id="run-failure",
        observed_at=NOW,
        states={state.health_key: state},
        transitions=(_transition("healthy", STATUS_FAILING),),
        coverage=(_coverage(),),
        summary=_summary(),
        comparison=None,
        sender=fail,
    )
    with SeenStore(db) as seen:
        after = seen.records()
    assert result.sent is False
    assert "[redacted]" in result.error
    assert "hunter2" not in result.error
    assert before == after


def test_health_alerts_never_populate_emailed_or_primed(tmp_path):
    db = tmp_path / "state.sqlite"
    with SeenStore(db) as seen:
        assert seen.records() == []
    state = _state(
        status=STATUS_FAILING,
        failures=3,
        rows=None,
        error="fetch_failure",
    )
    evaluate_and_send_health_alerts(
        db_path=db,
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id="run-1",
        observed_at=NOW,
        states={state.health_key: state},
        transitions=(_transition("healthy", STATUS_FAILING),),
        coverage=(_coverage(),),
        summary=_summary(),
        comparison=None,
        sender=lambda subject, body: True,
    )
    with SeenStore(db) as seen:
        assert seen.records() == []


def test_match_and_health_email_failures_are_reported_separately(tmp_path):
    db = tmp_path / "state.sqlite"
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Test Co",
                ats="greenhouse",
                token="test",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        seen_db_path=db,
    )
    key = direct_health_key("Test Co", "greenhouse")
    with SourceHealthStore(db) as health:
        for index in (1, 2):
            health.record_attempts(
                [
                    SourceAttempt(
                        health_key=key,
                        run_id=f"prior-{index}",
                        observed_at=NOW - timedelta(hours=3 - index),
                        source_kind=SOURCE_KIND_DIRECT,
                        company="Test Co",
                        adapter="greenhouse",
                        attempted=True,
                        succeeded=False,
                        rows_returned=None,
                        error_kind="fetch_failure",
                        error_message="safe failure",
                    )
                ]
            )

    class FailingDirect:
        def fetch(self, company):
            raise SourceFetchError("board unavailable")

    class HealthyBackstop:
        def fetch_many(self, companies):
            return [
                make_row(
                    source="github",
                    source_adapter="feed",
                    company="Test Co",
                    title="Software Engineering Intern",
                    location="United States",
                    source_url="https://example.com/jobs/1",
                    internship_type="Summer 2027 Internship",
                    description="Build Python APIs and software services.",
                    requirements="Pursuing a bachelor's degree.",
                    extra={
                        "source_name": "feed",
                        "source_priority": 10,
                        "active": True,
                        "terms": ["Summer 2027"],
                    },
                )
            ]

    with SeenStore(db) as seen:
        result = run_once(
            config,
            seen_store=seen,
            direct_sources={"greenhouse": FailingDirect()},
            github_source=HealthyBackstop(),
            alumni_index={},
            digest_sender=lambda matches: False,
            notification_mode=RUN_MODE_LIVE,
            health_observed_at=NOW,
            run_id="separate-email-outcomes",
            health_alert_policy=HealthAlertPolicy(mode=MODE_FAILURE_ONLY),
            health_alert_sender=lambda subject, body: False,
        )
        records = seen.records()
    assert result.digest_sent is False
    assert result.health_alert_result.sent is False
    assert result.health_alert_result.error == "health_sender_returned_false"
    assert len(result.new_matches) == 1
    assert records == []


def test_observability_failures_do_not_undo_match_email_state(
    tmp_path,
    monkeypatch,
):
    db = tmp_path / "state.sqlite"
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Test Co",
                ats="github_only",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        seen_db_path=db,
    )

    class HealthyBackstop:
        def fetch_many(self, companies):
            return [
                make_row(
                    source="github",
                    source_adapter="feed",
                    company="Test Co",
                    title="Software Engineering Intern",
                    location="United States",
                    source_url="https://example.com/jobs/2",
                    internship_type="Summer 2027 Internship",
                    description="Build Python APIs and software services.",
                    requirements="Pursuing a bachelor's degree.",
                    extra={
                        "source_name": "feed",
                        "source_priority": 10,
                        "active": True,
                        "terms": ["Summer 2027"],
                    },
                )
            ]

    def fail_save(self, report):
        raise RuntimeError("comparison storage unavailable")

    monkeypatch.setattr(
        "watcher.run.SourceComparisonStore.save",
        fail_save,
    )

    def fail_health_evaluation(**kwargs):
        raise RuntimeError("health alert storage unavailable")

    monkeypatch.setattr(
        "watcher.run.evaluate_and_send_health_alerts",
        fail_health_evaluation,
    )
    with SeenStore(db) as seen:
        result = run_once(
            config,
            seen_store=seen,
            direct_sources={},
            github_source=HealthyBackstop(),
            alumni_index={},
            digest_sender=lambda matches: True,
            notification_mode=RUN_MODE_LIVE,
            health_observed_at=NOW,
            run_id="comparison-failure",
            health_alert_policy=HealthAlertPolicy(mode=MODE_OFF),
        )
        records = seen.records()
    assert result.digest_sent is True
    assert result.source_comparison_persisted is False
    assert result.health_alert_result.sent is False
    assert "health alert storage unavailable" in result.health_alert_result.error
    assert len(records) == 1
    assert records[0]["emailed_at"] is not None


def test_transport_probe_mode_off_never_calls_sender(tmp_path):
    called = False

    def sender(subject, body):
        nonlocal called
        called = True
        return True

    result, _ = _evaluate(
        tmp_path,
        state=_state(status=STATUS_FAILING, failures=3, rows=None),
        policy=HealthAlertPolicy(mode=MODE_OFF),
        sender=sender,
    )
    assert result.sent is False
    assert called is False


def test_rendered_alert_redacts_sensitive_values(tmp_path):
    state = _state(
        status=STATUS_FAILING,
        failures=3,
        rows=None,
        error="schema_failure",
    )
    result, calls = _evaluate(
        tmp_path,
        state=state,
        transition=(_transition("healthy", STATUS_FAILING),),
    )
    subject, body = calls[0]
    assert result.sent is True
    assert "secret" not in body
    assert "user:pass" not in body
    assert "schema_failure" in body
    assert "new internship" not in subject.casefold()


def test_policy_defaults_and_validation():
    policy = load_health_alert_policy({})
    assert policy.mode == MODE_TRANSITIONS_ONLY
    assert policy.cooldown_hours == 24
    with pytest.raises(ValueError):
        load_health_alert_policy({"WATCHER_HEALTH_EMAIL_MODE": "sometimes"})


def _actionable_degradation(**overrides):
    state = _state(
        status=DIRECT_STATUS_DEGRADED,
        previous_status="healthy_with_listings",
        rows=10,
        reason_codes=("pagination_ended_early",),
        incomplete=True,
        truncated=False,
        complete=False,
    )
    return replace(state, **overrides)


def test_repeated_medium_events_and_recovery_collapse_into_one_incident(tmp_path):
    degraded = _actionable_degradation()
    for hour in (3, 4, 5):
        result, calls = _evaluate(tmp_path, state=degraded, now=NOW.replace(hour=hour))
        assert result.sent is False
        assert calls == []
    recovered = _state(
        status=STATUS_HEALTHY,
        previous_status=DIRECT_STATUS_DEGRADED,
        rows=142,
    )
    _evaluate(
        tmp_path,
        state=recovered,
        transition=(_transition(DIRECT_STATUS_DEGRADED, STATUS_HEALTHY, recovery=True),),
        now=NOW.replace(hour=6),
    )

    digest, calls = _evaluate(
        tmp_path,
        state=replace(recovered, previous_status=STATUS_HEALTHY),
        now=NOW,
    )

    assert digest.daily_digest_sent is True
    assert digest.digest_incidents_reported == 1
    body = calls[0][1]
    assert body.count("occurrences:") == 1
    assert "occurrences: 3" in body
    assert "recovered later: yes" in body


def test_high_escalation_suppresses_prior_medium_digest_noise(tmp_path):
    degraded = _actionable_degradation()
    _evaluate(tmp_path, state=degraded, now=NOW.replace(hour=3))
    failed = _state(
        status=DIRECT_STATUS_FAILED,
        previous_status=DIRECT_STATUS_DEGRADED,
        rows=None,
        failures=1,
        error="fetch_failure",
    )
    escalated, calls = _evaluate(
        tmp_path,
        state=failed,
        transition=(_transition(DIRECT_STATUS_DEGRADED, DIRECT_STATUS_FAILED),),
        now=NOW.replace(hour=4),
    )
    digest, digest_calls = _evaluate(tmp_path, state=failed, now=NOW)

    assert escalated.sent is True
    assert "HIGH" in calls[0][1]
    assert digest.daily_digest_sent is False
    assert digest.digest_incidents_reported == 0
    assert digest_calls == []


def test_recovery_after_high_escalation_can_reach_digest(tmp_path):
    failed = _state(
        status=DIRECT_STATUS_FAILED,
        previous_status=STATUS_HEALTHY,
        rows=None,
        failures=1,
        error="fetch_failure",
    )
    _evaluate(
        tmp_path,
        state=failed,
        transition=(_transition(STATUS_HEALTHY, DIRECT_STATUS_FAILED),),
        now=NOW.replace(hour=3),
    )
    recovered = _state(
        status=STATUS_HEALTHY,
        previous_status=DIRECT_STATUS_FAILED,
        rows=42,
    )
    quiet, quiet_calls = _evaluate(
        tmp_path,
        state=recovered,
        transition=(_transition(DIRECT_STATUS_FAILED, STATUS_HEALTHY, recovery=True),),
        now=NOW.replace(hour=4),
    )
    digest, calls = _evaluate(
        tmp_path,
        state=replace(recovered, previous_status=STATUS_HEALTHY),
        now=NOW,
    )

    assert quiet.sent is False
    assert quiet_calls == []
    assert digest.daily_digest_sent is True
    assert "recovered" in calls[0][1]


def test_digest_failed_delivery_retries_and_catches_up(tmp_path):
    first = _actionable_degradation()
    initial, _ = _evaluate(tmp_path, state=first, now=NOW)
    assert initial.daily_digest_sent is True

    second = replace(first, health_key="company:other:direct:lever", company="Other Co")

    def fail(_subject, _body):
        return False

    failed, _ = _evaluate(
        tmp_path,
        state=second,
        now=NOW + timedelta(days=1),
        sender=fail,
    )
    recovered, calls = _evaluate(
        tmp_path,
        state=_state(),
        now=NOW + timedelta(days=3),
    )

    assert failed.daily_digest_sent is False
    with HealthAlertStore(tmp_path / "state.sqlite") as store:
        assert store.digest_sent((NOW + timedelta(days=1)).date().isoformat()) is False
    assert recovered.daily_digest_sent is True
    assert "Other Co" in calls[0][1]


def test_digest_sends_at_most_once_per_utc_day_and_skips_empty_windows(tmp_path):
    degraded = _actionable_degradation()
    first, first_calls = _evaluate(tmp_path, state=degraded, now=NOW)
    second, second_calls = _evaluate(
        tmp_path,
        state=degraded,
        now=NOW + timedelta(hours=1),
    )
    empty, empty_calls = _evaluate(
        tmp_path / "empty",
        state=_state(),
        now=NOW,
    )

    assert first.daily_digest_sent is True
    assert len(first_calls) == 1
    assert second.daily_digest_sent is False
    assert second_calls == []
    assert empty.daily_digest_sent is False
    assert empty_calls == []


def test_resolve_digest_window_resumes_and_clamps():
    default_start, inclusive, clamped = resolve_digest_window(
        now=NOW,
        last_sent_at=None,
    )
    assert default_start == NOW - timedelta(hours=24)
    assert inclusive is True
    assert clamped is False

    resumed, inclusive, clamped = resolve_digest_window(
        now=NOW,
        last_sent_at=NOW - timedelta(days=3),
    )
    assert resumed == NOW - timedelta(days=3)
    assert inclusive is False
    assert clamped is False

    bounded, inclusive, clamped = resolve_digest_window(
        now=NOW,
        last_sent_at=NOW - timedelta(days=30),
    )
    assert bounded == NOW - timedelta(days=MAX_DIGEST_CATCHUP_DAYS)
    assert inclusive is True
    assert clamped is True


def test_digest_catchup_clamp_reports_only_seven_retained_days(tmp_path):
    def fail(_subject, _body):
        return False

    start = NOW - timedelta(days=10)
    _evaluate(tmp_path, state=_actionable_degradation(), now=start)
    old = replace(
        _actionable_degradation(),
        health_key="company:old:direct:lever",
        company="Old Co",
    )
    recent = replace(
        _actionable_degradation(),
        health_key="company:recent:direct:ashby",
        company="Recent Co",
    )
    _evaluate(tmp_path, state=old, now=start + timedelta(days=1), sender=fail)
    _evaluate(
        tmp_path,
        state=recent,
        now=NOW - timedelta(days=2),
        sender=fail,
    )
    result, calls = _evaluate(tmp_path, state=_state(), now=NOW)

    assert result.daily_digest_sent is True
    assert result.digest_catchup_clamped is True
    body = calls[0][1]
    assert f"more than {MAX_DIGEST_CATCHUP_DAYS} days" in body
    assert "Recent Co" in body
    assert "Old Co" not in body


def test_legacy_critical_payload_remains_sortable_and_readable(tmp_path):
    high = _degradation_candidate(
        _state(
            status=DIRECT_STATUS_FAILED,
            rows=None,
            error="fetch_failure",
        )
    )
    legacy = replace(high, severity="critical", fingerprint="legacy-critical")
    assert _merge_candidates((high,), (legacy,))[0].severity == "critical"

    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        store.record_detected(legacy, detected_at=NOW)
    with sqlite3.connect(db) as connection:
        payload = connection.execute(
            "select payload_json from source_health_alert_events"
        ).fetchone()[0]
        payload = payload.replace(',"reason_codes":[]', "")
        connection.execute(
            "update source_health_alert_events set payload_json = ?",
            (payload,),
        )
    with HealthAlertStore(db) as store:
        restored = store.recent_candidates(since=NOW - timedelta(hours=1))
    assert restored[0].severity == "critical"
    assert restored[0].reason_codes == ()


def test_legacy_minor_digest_ledger_migrates_without_resending(tmp_path):
    db = tmp_path / "state.sqlite"
    digest_date = NOW.date().isoformat()
    with HealthAlertStore(db):
        pass
    with sqlite3.connect(db) as connection:
        connection.execute(
            """
            insert into source_health_minor_digest(
              digest_date, sent_at, run_id, incident_count
            ) values (?, ?, ?, ?)
            """,
            (digest_date, NOW.isoformat(), "legacy-run", 2),
        )
    with HealthAlertStore(db) as store:
        assert store.digest_sent(digest_date) is True


# Phase 2B: per-company fallback evidence and systemic presentation grouping.

GITHUB_FEED_KEY = "github_feed:0123456789abcdef"
SECOND_GITHUB_FEED_KEY = "github_feed:fedcba9876543210"


def _feed_state(
    *,
    last_nonzero=NOW,
    status=STATUS_HEALTHY,
    key=GITHUB_FEED_KEY,
    feed_label="simplify [https://example.com/listings.json]",
):
    return _state(
        key=key,
        company=None,
        source_kind=SOURCE_KIND_GITHUB_FEED,
        status=status,
        last_nonzero=last_nonzero,
        feed_label=feed_label,
    )


def _second_feed_state(**overrides):
    overrides.setdefault("key", SECOND_GITHUB_FEED_KEY)
    overrides.setdefault(
        "feed_label", "listings [https://example.com/other-listings.md]"
    )
    return _feed_state(**overrides)


def _failed_direct_state(
    *,
    company="Test Co",
    adapter="greenhouse",
    failures=1,
    error="fetch_failure",
):
    return replace(
        _state(
            key=direct_health_key(company, adapter),
            company=company,
            status=DIRECT_STATUS_FAILED,
            previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            failures=failures,
            rows=None,
            error=error,
        ),
        adapter=adapter,
    )


def _failed_transition(state):
    return HealthTransition(
        health_key=state.health_key,
        source_kind=SOURCE_KIND_DIRECT,
        company=state.company,
        adapter=state.adapter,
        feed_label=None,
        from_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        to_status=DIRECT_STATUS_FAILED,
        recovery=False,
    )


def _failed_coverage(
    *,
    company="Test Co",
    adapter="greenhouse",
    github_rows=None,
    fallback_configured=True,
):
    return CompanyCoverage(
        company=company,
        adapter=adapter,
        state=COVERAGE_FAILING_BACKSTOP,
        direct_status=DIRECT_STATUS_FAILED,
        direct_attempt_succeeded=False,
        direct_rows_returned=None,
        github_backstop_available=True,
        github_rows_returned=github_rows,
        github_fallback_configured=fallback_configured,
    )


def _failure_candidate(
    *,
    github_rows=None,
    fallback_configured=True,
    feeds=None,
    evidence=frozenset(),
    failures=1,
    now=NOW,
):
    direct = _failed_direct_state(failures=failures)
    states = {direct.health_key: direct}
    for feed in feeds or (_feed_state(),):
        states[feed.health_key] = feed
    candidates = build_alert_candidates(
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id="run-2b",
        observed_at=now,
        states=states,
        transitions=(_failed_transition(direct),),
        coverage=(
            _failed_coverage(
                github_rows=github_rows,
                fallback_configured=fallback_configured,
            ),
        ),
        previous_coverage=None,
        github_evidence_companies=evidence,
    )
    return next(item for item in candidates if item.health_key == direct.health_key)


def _evaluate_alert_run(
    tmp_path,
    *,
    states,
    coverage,
    transitions=(),
    now=NOW,
    sender=None,
    run_id=None,
):
    calls = []

    def default_sender(subject, body):
        calls.append((subject, body))
        return True

    result = evaluate_and_send_health_alerts(
        db_path=tmp_path / "state.sqlite",
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id=run_id or f"run-{now.day}-{now.hour}-{now.minute}",
        observed_at=now,
        states=states,
        transitions=tuple(transitions),
        coverage=tuple(coverage),
        summary=_summary(),
        comparison=None,
        sender=sender or default_sender,
    )
    return result, calls


def test_first_failure_requires_company_specific_github_evidence():
    missing = _failure_candidate(github_rows=None)
    zero = _failure_candidate(github_rows=0)
    positive = _failure_candidate(github_rows=3)

    assert missing.severity == SEVERITY_HIGH
    assert zero.severity == SEVERITY_HIGH
    assert zero.github_fallback_available is True
    assert zero.github_fallback_usable is False
    assert positive.severity == SEVERITY_MEDIUM
    assert positive.github_fallback_usable is True


def test_recent_company_evidence_can_defer_only_the_first_failure():
    first = _failure_candidate(
        github_rows=0,
        evidence=frozenset({"Test Co"}),
    )
    second = _failure_candidate(
        github_rows=25,
        evidence=frozenset({"Test Co"}),
        failures=2,
    )

    assert first.severity == SEVERITY_MEDIUM
    assert second.github_fallback_usable is True
    assert second.severity == SEVERITY_HIGH


def test_github_primary_company_never_gets_fallback_downgrade():
    candidate = _failure_candidate(github_rows=12, fallback_configured=False)
    assert candidate.github_fallback_usable is False
    assert candidate.severity == SEVERITY_HIGH


@pytest.mark.parametrize(
    "broken_feed",
    [
        pytest.param(_feed_state(status=STATUS_FAILING), id="failing"),
        pytest.param(
            _feed_state(
                last_nonzero=NOW
                - timedelta(hours=DEFAULT_FEED_STALE_HOURS + 1)
            ),
            id="stale",
        ),
        pytest.param(_feed_state(last_nonzero=None), id="never-populated"),
    ],
)
def test_one_untrustworthy_github_feed_prevents_downgrade(broken_feed):
    candidate = _failure_candidate(
        github_rows=0,
        evidence=frozenset({"Test Co"}),
        feeds=(broken_feed, _second_feed_state()),
    )
    assert candidate.github_fallback_usable is False
    assert candidate.severity == SEVERITY_HIGH


def test_all_healthy_feeds_and_positive_evidence_permit_downgrade():
    candidate = _failure_candidate(
        github_rows=0,
        evidence=frozenset({"Test Co"}),
        feeds=(_feed_state(), _second_feed_state()),
    )
    assert candidate.github_fallback_usable is True
    assert candidate.severity == SEVERITY_MEDIUM


def test_recent_evidence_expires_after_seven_days(tmp_path):
    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        store.record_coverage_snapshot(
            run_id="recent",
            observed_at=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS),
            coverage=(_failed_coverage(company="Recent Co", github_rows=2),),
        )
        store.record_coverage_snapshot(
            run_id="expired",
            observed_at=NOW
            - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS, seconds=1),
            coverage=(_failed_coverage(company="Expired Co", github_rows=3),),
        )
        evidence = store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        )

    assert evidence == frozenset({"Recent Co"})


@pytest.mark.parametrize(
    ("age", "expected_sent"),
    [
        pytest.param(timedelta(days=2), False, id="recent-evidence"),
        pytest.param(timedelta(days=8), True, id="expired-evidence"),
    ],
)
def test_alert_evaluation_uses_only_recent_persisted_evidence(
    tmp_path,
    age,
    expected_sent,
):
    with HealthAlertStore(tmp_path / "state.sqlite") as store:
        store.record_coverage_snapshot(
            run_id="seed-history",
            observed_at=NOW - age,
            coverage=(_failed_coverage(github_rows=4),),
        )
    failed = _failed_direct_state(failures=1)
    feed = _feed_state()
    result, calls = _evaluate_alert_run(
        tmp_path,
        states={failed.health_key: failed, feed.health_key: feed},
        coverage=(_failed_coverage(github_rows=0),),
        transitions=(_failed_transition(failed),),
        now=BEFORE_DIGEST_HOUR,
    )

    assert result.sent is expected_sent
    assert bool(calls) is expected_sent


def test_legacy_and_current_coverage_snapshots_are_compatible(tmp_path):
    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        store._conn.execute(
            """
            insert into source_health_coverage_snapshots(
              run_id, observed_at, coverage_json
            ) values (?, ?, ?)
            """,
            (
                "legacy-run",
                (NOW - timedelta(hours=2)).isoformat(),
                '{"Legacy Co": "direct_covered"}',
            ),
        )
        store._conn.commit()
        store.record_coverage_snapshot(
            run_id="current-run",
            observed_at=NOW - timedelta(hours=1),
            coverage=(
                _failed_coverage(company="Covered Co", github_rows=3),
                _failed_coverage(company="Quiet Co", github_rows=0),
            ),
        )

        assert store.latest_coverage_snapshot() == {
            "Covered Co": COVERAGE_FAILING_BACKSTOP,
            "Quiet Co": COVERAGE_FAILING_BACKSTOP,
        }
        evidence = store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        )
    assert evidence == frozenset({"Covered Co"})


def test_snapshot_retention_covers_seven_day_evidence_horizon(tmp_path):
    assert MAX_COVERAGE_SNAPSHOTS >= GITHUB_EVIDENCE_HORIZON_DAYS * 24
    with HealthAlertStore(tmp_path / "state.sqlite") as store:
        oldest = NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS - 1)
        store.record_coverage_snapshot(
            run_id="oldest-run",
            observed_at=oldest,
            coverage=(_failed_coverage(company="Covered Co", github_rows=2),),
        )
        for index in range(GITHUB_EVIDENCE_HORIZON_DAYS * 24):
            store.record_coverage_snapshot(
                run_id=f"run-{index}",
                observed_at=oldest + timedelta(hours=index + 1),
                coverage=(_failed_coverage(company="Other Co", github_rows=0),),
            )
        retained = store._conn.execute(
            "select count(*) from source_health_coverage_snapshots"
        ).fetchone()[0]
        evidence = store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        )

    assert retained == GITHUB_EVIDENCE_HORIZON_DAYS * 24 + 1
    assert evidence == frozenset({"Covered Co"})


def test_replayed_snapshot_replaces_evidence_deterministically(tmp_path):
    with HealthAlertStore(tmp_path / "state.sqlite") as store:
        for github_rows in (5, 0):
            store.record_coverage_snapshot(
                run_id="repeated-run",
                observed_at=NOW - timedelta(hours=1),
                coverage=(
                    _failed_coverage(
                        company="Covered Co",
                        github_rows=github_rows,
                    ),
                ),
            )
        stored = store._conn.execute(
            "select count(*) from source_health_coverage_snapshots"
        ).fetchone()[0]
        evidence = store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        )

    assert stored == 1
    assert evidence == frozenset()


def _family_candidates(adapter, error_kinds):
    states = {}
    coverage = []
    for index, error_kind in enumerate(error_kinds):
        company = f"{adapter} co {index}"
        state = _failed_direct_state(
            company=company,
            adapter=adapter,
            failures=3,
            error=error_kind,
        )
        states[state.health_key] = state
        coverage.append(
            _failed_coverage(
                company=company,
                adapter=adapter,
                github_rows=0,
                fallback_configured=False,
            )
        )
    return states, tuple(coverage)


def _candidates_for(*families):
    states = {}
    coverage = []
    for adapter, error_kinds in families:
        family_states, family_coverage = _family_candidates(adapter, error_kinds)
        states.update(family_states)
        coverage.extend(family_coverage)
    return build_alert_candidates(
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id="run-systemic",
        observed_at=NOW,
        states=states,
        transitions=(),
        coverage=tuple(coverage),
        previous_coverage=None,
    )


def test_dominant_same_family_failures_render_one_systemic_high_group():
    candidates = _candidates_for(
        ("workday", ["fetch_failure/redirected_to_html"] * 6),
    )
    groups, remaining = group_systemic_incidents(candidates)

    assert len(groups) == 1
    assert groups[0].adapter_family == "workday"
    assert groups[0].affected_companies == 6
    assert remaining == ()
    subject, body = render_alert_email(candidates)
    assert "shared source incident" in subject
    assert body.count("HIGH:") == 1
    for candidate in candidates:
        assert candidate.source_label in body


def test_systemic_grouping_requires_count_and_dominance_thresholds():
    below_count = _candidates_for(
        (
            "workday",
            ["fetch_failure/redirected_to_html"]
            * (SYSTEMIC_GROUP_MIN_COMPANIES - 1),
        ),
    )
    diluted = _candidates_for(
        (
            "workday",
            ["fetch_failure/redirected_to_html"] * 5
            + ["schema_failure"] * 4,
        ),
    )

    assert group_systemic_incidents(below_count)[0] == ()
    assert group_systemic_incidents(diluted)[0] == ()


def test_systemic_grouping_never_merges_adapter_families_or_other_errors():
    candidates = _candidates_for(
        ("workday", ["fetch_failure/redirected_to_html"] * 6),
        ("icims", ["fetch_failure/redirected_to_html"] * 5),
        ("greenhouse", ["schema_failure"]),
    )
    groups, remaining = group_systemic_incidents(candidates)

    assert [group.adapter_family for group in groups] == ["icims", "workday"]
    assert [group.affected_companies for group in groups] == [5, 6]
    assert len(remaining) == 1
    assert remaining[0].adapter == "greenhouse"
    assert render_alert_email(candidates)[1].count("HIGH:") == 3


def test_systemic_grouping_preserves_per_company_events_and_recovery(tmp_path):
    states, coverage = _family_candidates(
        "workday", ["fetch_failure/redirected_to_html"] * 6
    )
    db = tmp_path / "state.sqlite"
    with SourceHealthStore(db) as health:
        health.record_attempts(
            SourceAttempt(
                health_key=state.health_key,
                run_id="run-systemic",
                observed_at=NOW,
                source_kind=SOURCE_KIND_DIRECT,
                company=state.company,
                adapter="workday",
                attempted=True,
                succeeded=False,
                rows_returned=None,
                error_kind="fetch_failure",
                error_message="redirected to html",
            )
            for state in states.values()
        )
    grouped, calls = _evaluate_alert_run(
        tmp_path,
        states=states,
        coverage=coverage,
        now=BEFORE_DIGEST_HOUR,
    )
    assert grouped.sent is True
    assert "shared source incident" in calls[0][0]

    with SourceHealthStore(db) as health:
        stored_states = health.all_current_states()
    assert set(stored_states) == set(states)
    assert {state.company for state in stored_states.values()} == {
        state.company for state in states.values()
    }

    with sqlite3.connect(db) as connection:
        event_count = connection.execute(
            "select count(*) from source_health_alert_events"
        ).fetchone()[0]
        health_keys = {
            row[0]
            for row in connection.execute(
                "select distinct health_key from source_health_alert_state"
            )
        }
    assert event_count == len(states)
    assert health_keys == set(states)

    recovering = next(iter(states.values()))
    recovered = replace(
        recovering,
        status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        previous_status=DIRECT_STATUS_FAILED,
        consecutive_failures=0,
        last_rows_returned=11,
        last_error_kind=None,
    )
    result, recovery_calls = _evaluate_alert_run(
        tmp_path,
        states={**states, recovered.health_key: recovered},
        coverage=coverage,
        transitions=(
            HealthTransition(
                health_key=recovered.health_key,
                source_kind=SOURCE_KIND_DIRECT,
                company=recovered.company,
                adapter="workday",
                feed_label=None,
                from_status=DIRECT_STATUS_FAILED,
                to_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
                recovery=True,
            ),
        ),
        now=NOW,
    )
    assert result.sent is False
    assert result.daily_digest_sent is True
    assert recovered.company in recovery_calls[0][1]
