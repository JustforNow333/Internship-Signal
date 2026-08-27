from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from watcher.config import CompanyCfg
from watcher.config import WatcherConfig
from watcher.health_alerts import (
    MODE_DAILY_SUMMARY,
    MODE_FAILURE_ONLY,
    MODE_OFF,
    MODE_TRANSITIONS_ONLY,
    HealthAlertPolicy,
    build_alert_candidates,
    evaluate_and_send_health_alerts,
    is_minor_degradation,
    load_health_alert_policy,
)
from watcher.seen_store import SeenStore
from watcher.run import RUN_MODE_LIVE, run_once
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT,
    COVERAGE_UNCOVERED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
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
    reason_codes=(),
    incomplete=None,
    truncated=None,
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
        last_incomplete=incomplete,
        last_truncated=truncated,
        last_reason_codes=tuple(reason_codes),
        last_degraded=status == DIRECT_STATUS_DEGRADED,
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


def test_recovery_sends_once_after_failure(tmp_path):
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
        now=NOW + timedelta(hours=1),
    )
    assert result.sent is True
    assert result.recovery_alerts == 1
    assert "Source Recovery" in calls[0][0]
    assert "rows returned: 142" in calls[0][1]


def test_failed_recovery_delivery_retries_once_while_source_remains_healthy(
    tmp_path,
):
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
    )
    recovered = _state(
        status=STATUS_HEALTHY,
        previous_status=STATUS_FAILING,
        failures=0,
        rows=142,
    )

    def fail_recovery_delivery(_subject, _body):
        raise RuntimeError("temporary SMTP outage")

    failed, _ = _evaluate(
        tmp_path,
        state=recovered,
        transition=(
            _transition(STATUS_FAILING, STATUS_HEALTHY, recovery=True),
        ),
        now=NOW + timedelta(hours=1),
        sender=fail_recovery_delivery,
    )
    retried, retry_calls = _evaluate(
        tmp_path,
        state=replace(recovered, previous_status=STATUS_HEALTHY),
        now=NOW + timedelta(hours=2),
    )
    settled, settled_calls = _evaluate(
        tmp_path,
        state=replace(recovered, previous_status=STATUS_HEALTHY),
        now=NOW + timedelta(hours=3),
    )

    assert failed.sent is False
    assert failed.error == "temporary SMTP outage"
    assert retried.sent is True
    assert retried.recovery_alerts == 1
    assert "Source Recovery" in retry_calls[0][0]
    assert settled.sent is False
    assert settled_calls == []


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
    assert result.sent is True
    assert "feed_stale" in calls[0][1]
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


def test_previously_productive_direct_source_silence_alerts(tmp_path):
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
    assert result.sent is True
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
    assert calls == []


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
    assert "CRITICAL" in body
    assert "coverage_regression" in body


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
