from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from watcher.config import CompanyCfg
from watcher.config import WatcherConfig
from watcher.health_alerts import (
    MAX_DIGEST_CATCHUP_DAYS,
    MODE_DAILY_SUMMARY,
    MODE_FAILURE_ONLY,
    MODE_OFF,
    MODE_TRANSITIONS_ONLY,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_MEDIUM,
    HealthAlertPolicy,
    build_alert_candidates,
    evaluate_and_send_health_alerts,
    is_minor_degradation,
    load_health_alert_policy,
    resolve_digest_window,
)
from watcher.seen_store import SeenStore
from watcher.run import RUN_MODE_LIVE, run_once
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT,
    COVERAGE_UNCOVERED,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_UNKNOWN,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
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
# An hour before the digest hour, so an incident is recorded without triggering
# delivery. Every digest assertion then runs at NOW.
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
    reason_codes=(),
    malformed=None,
    schema_errors=None,
    incomplete=None,
    truncated=None,
    degraded=None,
    complete=None,
):
    return SourceHealthState(
        health_key=key,
        source_kind=source_kind,
        company=company,
        adapter="greenhouse" if source_kind == SOURCE_KIND_DIRECT else "simplify_json",
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
        last_incomplete=incomplete,
        last_truncated=truncated,
        last_reason_codes=tuple(reason_codes),
        last_degraded=degraded,
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


def test_high_failure_then_recovery_reports_recovery_in_the_digest_only(tmp_path):
    """HIGH keeps its immediate email; the recovery is INFO in the digest."""

    failing = _state(
        status=STATUS_FAILING,
        failures=3,
        rows=None,
        error="schema_failure",
    )
    alerted, alert_calls = _evaluate(
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

    assert alerted.sent is True
    assert "HIGH" in alert_calls[0][1]
    # The recovery itself never interrupts.
    assert result.sent is False
    assert result.recovery_alerts == 1
    assert calls == []
    assert digest.daily_digest_sent is True
    subject, body = digest_calls[0]
    assert DIGEST_SUBJECT_MARKER in subject
    assert "INFO" in body
    assert "recovered" in body


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
    # A stale feed is MEDIUM, so it reports in the digest rather than at once.
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


def test_previously_productive_direct_source_silence_reports_in_the_digest(tmp_path):
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
    assert "MEDIUM" in calls[0][1]


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
    # Losing both tiers is the most urgent condition, and it is HIGH: this
    # policy has no CRITICAL severity.
    assert "CRITICAL" not in body
    assert body.count("HIGH:") == 2
    assert "coverage_regression" in body


def test_no_active_candidate_uses_critical_severity(tmp_path):
    """Every emitted candidate must carry high, medium, or info."""

    seen_severities = set()

    def collect(candidates):
        seen_severities.update(candidate.severity for candidate in candidates)
        return tuple(candidates)

    states = (
        _state(status=STATUS_FAILING, failures=3, rows=None, error="fetch_failure"),
        _minor_state(),
        _minor_state(reason_codes=("pagination_ended_early",), schema_errors=0),
        _state(status=DIRECT_STATUS_UNKNOWN),
        _state(
            key="github_feed:abc",
            company=None,
            source_kind=SOURCE_KIND_GITHUB_FEED,
            feed_label="Simplify",
            last_nonzero=NOW - timedelta(hours=72),
            rows=0,
        ),
        _state(status=STATUS_HEALTHY, previous_status=STATUS_FAILING),
    )
    for index, state in enumerate(states):
        collect(
            build_alert_candidates(
                policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
                run_id=f"run-{index}",
                observed_at=NOW,
                states={state.health_key: state},
                transitions=(
                    (_transition(STATUS_FAILING, STATUS_HEALTHY, recovery=True),)
                    if state.previous_status == STATUS_FAILING
                    and state.status == STATUS_HEALTHY
                    else ()
                ),
                coverage=(
                    _coverage(COVERAGE_UNCOVERED, direct_status="failing", github=False),
                ),
                previous_coverage={"Test Co": COVERAGE_DIRECT},
            )
        )

    assert seen_severities
    assert "critical" not in seen_severities
    assert seen_severities <= {SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_INFO}


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
    # The shadow observation pass records sightings; no notification state may
    # be written when the digest was not sent.
    assert [
        record
        for record in records
        if record["emailed_at"] or record["primed_at"]
    ] == []


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


# Minor-degradation policy. Small record-level noise and recovered retries stay
# degraded and visible, and are reported by the same daily digest that carries
# every other MEDIUM and INFO incident.


def _minor_state(
    *,
    reason_codes=("schema_invalid_records_skipped",),
    schema_errors=1,
    malformed=0,
    rows=412,
    **overrides,
):
    """A degraded direct source carrying only tiny record-level loss.

    Skips set ``incomplete``/``complete`` on every adapter, so the defaults
    mirror what the real diagnostics publish for this case.
    """

    values = {
        "incomplete": True,
        "truncated": False,
        "degraded": True,
        "complete": False,
    }
    values.update(overrides)
    return _state(
        status=DIRECT_STATUS_DEGRADED,
        previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        rows=rows,
        reason_codes=reason_codes,
        malformed=malformed,
        schema_errors=schema_errors,
        **values,
    )


def _retry_state(**overrides):
    """A degraded direct source whose retry recovered and finished complete."""

    values = {
        "incomplete": False,
        "truncated": False,
        "degraded": True,
        "complete": True,
    }
    values.update(overrides)
    return _state(
        status=DIRECT_STATUS_DEGRADED,
        previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        rows=118,
        reason_codes=("request_retry_recovered",),
        malformed=0,
        schema_errors=0,
        **values,
    )


def _healthy_after_minor(rows=412):
    return _state(
        status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        previous_status=DIRECT_STATUS_DEGRADED,
        rows=rows,
        reason_codes=(),
        malformed=0,
        schema_errors=0,
        incomplete=False,
        truncated=False,
        degraded=False,
        complete=True,
    )


def _minor_recovery_transition():
    return _transition(
        DIRECT_STATUS_DEGRADED,
        DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        recovery=True,
    )


def test_tiny_schema_loss_is_degraded_without_an_immediate_email(tmp_path):
    state = _minor_state()
    quiet, quiet_calls = _evaluate(tmp_path, state=state, now=BEFORE_DIGEST_HOUR)
    digest, digest_calls = _evaluate(tmp_path, state=state, now=NOW)

    assert state.status == DIRECT_STATUS_DEGRADED
    assert is_minor_degradation(state) is True
    assert quiet.sent is False
    assert quiet_calls == []
    assert digest.sent is False
    assert digest.daily_digest_sent is True
    assert digest.digest_incidents_reported == 1
    subject, body = digest_calls[0]
    assert DIGEST_SUBJECT_MARKER in subject
    assert "Test Co" in body
    assert "schema_invalid_records_skipped" in body
    assert "retained rows: 412" in body
    assert "schema=1" in body


def test_tiny_malformed_loss_is_degraded_without_an_immediate_email(tmp_path):
    state = _minor_state(
        reason_codes=("malformed_records_skipped",),
        schema_errors=0,
        malformed=1,
    )
    quiet, quiet_calls = _evaluate(tmp_path, state=state, now=BEFORE_DIGEST_HOUR)
    digest, digest_calls = _evaluate(tmp_path, state=state, now=NOW)

    assert is_minor_degradation(state) is True
    assert quiet_calls == []
    assert digest.daily_digest_sent is True
    body = digest_calls[0][1]
    assert "malformed_records_skipped" in body
    assert "malformed=1" in body


def test_recovered_retry_with_complete_collection_is_digest_only(tmp_path):
    state = _retry_state()
    quiet, quiet_calls = _evaluate(tmp_path, state=state, now=BEFORE_DIGEST_HOUR)
    recovered, recovery_calls = _evaluate(
        tmp_path,
        state=_healthy_after_minor(rows=118),
        transition=(_minor_recovery_transition(),),
        now=BEFORE_DIGEST_HOUR + timedelta(hours=1),
    )
    digest, digest_calls = _evaluate(
        tmp_path,
        state=_healthy_after_minor(rows=118),
        now=NOW,
    )

    assert is_minor_degradation(state) is True
    assert quiet_calls == []
    assert recovery_calls == []
    assert recovered.recovery_alerts == 0
    assert digest.daily_digest_sent is True
    body = digest_calls[0][1]
    assert "request_retry_recovered" in body
    assert "recovered later: yes" in body


def test_recovered_retry_with_incomplete_collection_is_medium(tmp_path):
    state = _retry_state(incomplete=True, complete=False)
    quiet, quiet_calls = _evaluate(tmp_path, state=state, now=BEFORE_DIGEST_HOUR)
    digest, digest_calls = _evaluate(tmp_path, state=state, now=NOW)

    assert is_minor_degradation(state) is False
    assert quiet.sent is False
    assert quiet_calls == []
    assert digest.daily_digest_sent is True
    body = digest_calls[0][1]
    assert "MEDIUM" in body
    assert "direct_source_degraded" in body


def test_minor_degradation_followed_by_healthy_sends_no_recovery_email(tmp_path):
    _evaluate(tmp_path, state=_minor_state(), now=BEFORE_DIGEST_HOUR)
    recovery, recovery_calls = _evaluate(
        tmp_path,
        state=_healthy_after_minor(),
        transition=(_minor_recovery_transition(),),
        now=BEFORE_DIGEST_HOUR + timedelta(hours=1),
    )

    assert recovery.sent is False
    assert recovery_calls == []
    assert recovery.recovery_alerts == 0


def test_second_minor_cycle_still_sends_no_recovery_email(tmp_path):
    """A re-detected minor incident must stay minor when it clears again."""

    minor = _minor_state()
    healthy = _healthy_after_minor()
    recovery = (_minor_recovery_transition(),)

    _evaluate(tmp_path, state=minor, now=NOW.replace(hour=1))
    first_recovery, first_calls = _evaluate(
        tmp_path, state=healthy, transition=recovery, now=NOW.replace(hour=2)
    )
    _evaluate(tmp_path, state=minor, now=NOW.replace(hour=3))
    second_recovery, second_calls = _evaluate(
        tmp_path, state=healthy, transition=recovery, now=NOW.replace(hour=4)
    )
    digest, digest_calls = _evaluate(tmp_path, state=healthy, now=NOW)

    assert first_calls == []
    assert second_calls == []
    assert first_recovery.recovery_alerts == 0
    assert second_recovery.recovery_alerts == 0
    assert digest.digest_incidents_reported == 1
    assert "occurrences: 2" in digest_calls[0][1]


def test_repeated_minor_incidents_are_summarized_once_per_source(tmp_path):
    state = _minor_state()
    for hour in (3, 4, 5):
        _evaluate(tmp_path, state=state, now=NOW.replace(hour=hour))
    digest, digest_calls = _evaluate(tmp_path, state=state, now=NOW)

    body = digest_calls[0][1]
    assert digest.digest_incidents_reported == 1
    assert body.count("occurrences:") == 1
    assert "occurrences: 4" in body
    assert "recovered later: no" in body


def test_daily_minor_digest_is_sent_at_most_once_per_reporting_day(tmp_path):
    state = _minor_state()
    first, first_calls = _evaluate(tmp_path, state=state, now=NOW)
    second, second_calls = _evaluate(
        tmp_path, state=state, now=NOW + timedelta(hours=2)
    )
    next_day, next_day_calls = _evaluate(
        tmp_path, state=state, now=NOW + timedelta(days=1)
    )

    assert first.daily_digest_sent is True
    assert DIGEST_SUBJECT_MARKER in first_calls[0][0]
    assert second.daily_digest_sent is False
    assert second_calls == []
    assert next_day.daily_digest_sent is True
    assert DIGEST_SUBJECT_MARKER in next_day_calls[0][0]


def test_digest_window_excludes_incidents_already_reported(tmp_path):
    """Events recorded during a digest run are not repeated the next day."""

    first, first_calls = _evaluate(tmp_path, state=_minor_state(), now=NOW)
    next_day, next_day_calls = _evaluate(
        tmp_path,
        state=_healthy_after_minor(),
        transition=(_minor_recovery_transition(),),
        now=NOW + timedelta(days=1),
    )
    quiet_day, quiet_day_calls = _evaluate(
        tmp_path,
        state=_healthy_after_minor(),
        now=NOW + timedelta(days=2),
    )

    assert first.daily_digest_sent is True
    assert "occurrences: 1" in first_calls[0][1]
    # The recovery is new, so it reports; the degradation it ended was already
    # reported yesterday and is not counted again.
    assert next_day.daily_digest_sent is True
    assert "occurrences: 0" in next_day_calls[0][1]
    assert "recovered" in next_day_calls[0][1]
    # With nothing new left, the window is empty and nothing is sent.
    assert quiet_day.daily_digest_sent is False
    assert quiet_day_calls == []


def test_no_minor_incidents_sends_no_digest(tmp_path):
    result, calls = _evaluate(tmp_path, state=_state(), now=NOW)

    assert result.daily_digest_sent is False
    assert result.digest_incidents_reported == 0
    assert calls == []


def test_failed_digest_send_remains_retryable(tmp_path):
    state = _minor_state()

    def broken_sender(_subject, _body):
        raise RuntimeError("temporary SMTP outage")

    failed, _ = _evaluate(tmp_path, state=state, now=NOW, sender=broken_sender)
    retried, retried_calls = _evaluate(
        tmp_path, state=state, now=NOW + timedelta(hours=1)
    )

    assert failed.daily_digest_sent is False
    assert retried.daily_digest_sent is True
    assert DIGEST_SUBJECT_MARKER in retried_calls[0][0]


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    (
        (
            {
                "reason_codes": ("pagination_ended_early",),
                "schema_errors": 0,
                "malformed": 0,
            },
            "pagination_ended_early",
        ),
        (
            {
                "reason_codes": (
                    "schema_invalid_records_skipped",
                    "pagination_ended_early",
                )
            },
            "pagination_ended_early",
        ),
        ({"truncated": True}, "schema_invalid_records_skipped"),
        ({"schema_errors": 9, "rows": 30}, "schema_invalid_records_skipped"),
        (
            {"reason_codes": ("unmapped_future_anomaly",)},
            "unmapped_future_anomaly",
        ),
    ),
)
def test_actionable_degradation_is_medium_in_the_digest(
    overrides,
    expected_reason,
    tmp_path,
):
    """Degradation that could hide postings stays MEDIUM, not minor INFO.

    It no longer interrupts, because severity now routes every MEDIUM incident
    to the daily digest, but it must never be reclassified as informational.
    """

    state = _minor_state(**overrides)
    quiet, quiet_calls = _evaluate(tmp_path, state=state, now=BEFORE_DIGEST_HOUR)
    digest, digest_calls = _evaluate(tmp_path, state=state, now=NOW)

    assert is_minor_degradation(state) is False
    assert quiet.sent is False
    assert quiet_calls == []
    assert digest.daily_digest_sent is True
    body = digest_calls[0][1]
    assert "MEDIUM" in body
    assert "direct_source_degraded" in body
    assert expected_reason in body


def test_direct_source_failure_still_sends_a_high_alert(tmp_path):
    state = _state(
        status=DIRECT_STATUS_FAILED,
        previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        failures=1,
        rows=None,
        error="fetch_failure",
    )
    result, calls = _evaluate(
        tmp_path,
        state=state,
        transition=(
            _transition(DIRECT_STATUS_HEALTHY_WITH_LISTINGS, DIRECT_STATUS_FAILED),
        ),
        now=BEFORE_DIGEST_HOUR,
    )

    subject, body = calls[0]
    assert result.sent is True
    assert "HIGH" in body
    assert "Source Alert" in subject


def test_minor_digest_does_not_interfere_with_an_immediate_alert(tmp_path):
    minor = _minor_state()
    failing = _state(
        key="company:other:direct:lever",
        company="Other Co",
        status=DIRECT_STATUS_FAILED,
        failures=1,
        rows=None,
        error="fetch_failure",
    )
    _evaluate(tmp_path, state=minor, now=NOW.replace(hour=3))
    calls = []

    def sender(subject, body):
        calls.append((subject, body))
        return True

    result = evaluate_and_send_health_alerts(
        db_path=tmp_path / "state.sqlite",
        policy=HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id="run-both",
        observed_at=NOW,
        states={minor.health_key: minor, failing.health_key: failing},
        transitions=(
            HealthTransition(
                health_key=failing.health_key,
                source_kind=SOURCE_KIND_DIRECT,
                company="Other Co",
                adapter="lever",
                feed_label=None,
                from_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
                to_status=DIRECT_STATUS_FAILED,
                recovery=False,
            ),
        ),
        coverage=(_coverage(),),
        summary=_summary(),
        comparison=None,
        sender=sender,
    )

    assert result.sent is True
    assert result.daily_digest_sent is True
    assert any("Source Alert" in subject for subject, _ in calls)
    assert any(DIGEST_SUBJECT_MARKER in subject for subject, _ in calls)
    minor_body = next(
        body for subject, body in calls if DIGEST_SUBJECT_MARKER in subject
    )
    assert "Other Co" not in minor_body


def _evaluate_states(
    tmp_path,
    *,
    states,
    transitions=(),
    now=NOW,
    sender=None,
    policy=None,
):
    """Evaluate several sources in one run, as a real run does."""

    calls = []

    def default_sender(subject, body):
        calls.append((subject, body))
        return True

    result = evaluate_and_send_health_alerts(
        db_path=tmp_path / "state.sqlite",
        policy=policy or HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
        run_id=f"run-{now.hour}-{now.day}",
        observed_at=now,
        states={state.health_key: state for state in states},
        transitions=tuple(transitions),
        coverage=(_coverage(),),
        summary=_summary(),
        comparison=None,
        sender=sender or default_sender,
    )
    return result, calls


def _other_state(**overrides):
    values = {
        "key": "company:other:direct:lever",
        "company": "Other Co",
    }
    values.update(overrides)
    return _state(**values)


def test_unknown_diagnostics_is_medium_in_the_digest(tmp_path):
    state = _state(status=DIRECT_STATUS_UNKNOWN)
    quiet, quiet_calls = _evaluate(tmp_path, state=state, now=BEFORE_DIGEST_HOUR)
    digest, digest_calls = _evaluate(tmp_path, state=state, now=NOW)

    assert quiet.sent is False
    assert quiet_calls == []
    assert digest.daily_digest_sent is True
    body = digest_calls[0][1]
    assert "MEDIUM" in body
    assert "unknown_diagnostics" in body


def test_medium_then_recovery_collapses_into_one_recovered_entry(tmp_path):
    degraded = _minor_state(reason_codes=("pagination_ended_early",), schema_errors=0)
    _evaluate(tmp_path, state=degraded, now=NOW.replace(hour=3))
    _evaluate(
        tmp_path,
        state=_healthy_after_minor(),
        transition=(_minor_recovery_transition(),),
        now=NOW.replace(hour=4),
    )
    digest, digest_calls = _evaluate(
        tmp_path,
        state=_healthy_after_minor(),
        now=NOW,
    )

    body = digest_calls[0][1]
    assert digest.digest_incidents_reported == 1
    # One entry, not a separate failure entry and recovery entry.
    assert body.count("occurrences:") == 1
    assert "recovered; 1 degraded run, currently healthy." in body
    assert "recovered later: yes" in body


def test_medium_then_high_escalation_leaves_no_unresolved_digest_entry(tmp_path):
    degraded = _minor_state(reason_codes=("pagination_ended_early",), schema_errors=0)
    quiet, quiet_calls = _evaluate(
        tmp_path,
        state=degraded,
        now=NOW.replace(hour=3),
    )
    failed = _state(
        status=DIRECT_STATUS_FAILED,
        previous_status=DIRECT_STATUS_DEGRADED,
        failures=1,
        rows=None,
        error="fetch_failure",
    )
    escalated, escalation_calls = _evaluate(
        tmp_path,
        state=failed,
        transition=(
            _transition(DIRECT_STATUS_DEGRADED, DIRECT_STATUS_FAILED),
        ),
        now=NOW.replace(hour=4),
    )
    digest, digest_calls = _evaluate(tmp_path, state=failed, now=NOW)

    assert quiet.sent is False
    assert quiet_calls == []
    # The escalation is the immediate alert.
    assert escalated.sent is True
    assert "HIGH" in escalation_calls[0][1]
    # The superseded MEDIUM must not resurface as an open incident.
    assert digest.daily_digest_sent is False
    assert digest.digest_incidents_reported == 0
    assert digest_calls == []


def test_repeated_medium_events_collapse_with_an_occurrence_count(tmp_path):
    degraded = _minor_state(reason_codes=("pagination_ended_early",), schema_errors=0)
    for hour in (3, 4, 5):
        _evaluate(tmp_path, state=degraded, now=NOW.replace(hour=hour))
    digest, digest_calls = _evaluate(tmp_path, state=degraded, now=NOW)

    body = digest_calls[0][1]
    assert digest.digest_incidents_reported == 1
    assert body.count("occurrences:") == 1
    assert "occurrences: 4" in body


def test_one_digest_combines_every_source_with_medium_before_info(tmp_path):
    medium = _minor_state(reason_codes=("pagination_ended_early",), schema_errors=0)
    info = _other_state(
        status=DIRECT_STATUS_DEGRADED,
        previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        reason_codes=("schema_invalid_records_skipped",),
        schema_errors=1,
        malformed=0,
        rows=412,
        incomplete=True,
        truncated=False,
        degraded=True,
        complete=False,
    )
    _evaluate_states(
        tmp_path,
        states=(medium, info),
        now=BEFORE_DIGEST_HOUR,
    )
    digest, calls = _evaluate_states(tmp_path, states=(medium, info), now=NOW)

    assert digest.digest_incidents_reported == 2
    # One combined email, not one per source.
    assert len(calls) == 1
    subject, body = calls[0]
    assert subject == (
        "Internship Watcher Daily Source Health: 1 medium, 1 info"
    )
    assert body.index("MEDIUM:") < body.index("INFO:")
    assert "Test Co" in body
    assert "Other Co" in body


def test_replaying_the_same_run_id_does_not_duplicate_digest_events(tmp_path):
    degraded = _minor_state(reason_codes=("pagination_ended_early",), schema_errors=0)
    replayed = NOW.replace(hour=3)
    _evaluate(tmp_path, state=degraded, now=replayed)
    _evaluate(tmp_path, state=degraded, now=replayed)
    digest, digest_calls = _evaluate(tmp_path, state=degraded, now=NOW)

    body = digest_calls[0][1]
    assert digest.digest_incidents_reported == 1
    # Two evaluations of one run plus the digest run itself is two events.
    assert "occurrences: 2" in body


def test_catchup_window_resumes_at_the_last_successful_digest(tmp_path):
    """A multi-day delivery outage still reports its retained events."""

    def broken(_subject, _body):
        raise RuntimeError("temporary SMTP outage")

    degraded = _minor_state(reason_codes=("pagination_ended_early",), schema_errors=0)
    first, _ = _evaluate(tmp_path, state=degraded, now=NOW)
    outage_day_one, _ = _evaluate(
        tmp_path,
        state=_other_state(
            status=DIRECT_STATUS_DEGRADED,
            previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            reason_codes=("pagination_ended_early",),
            malformed=0,
            schema_errors=0,
            rows=10,
            incomplete=True,
            truncated=False,
            degraded=True,
            complete=False,
        ),
        now=NOW + timedelta(days=1),
        sender=broken,
    )
    outage_day_two, _ = _evaluate(
        tmp_path,
        state=_state(
            key="company:third:direct:ashby",
            company="Third Co",
            status=DIRECT_STATUS_DEGRADED,
            previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            reason_codes=("pagination_ended_early",),
            malformed=0,
            schema_errors=0,
            rows=10,
            incomplete=True,
            truncated=False,
            degraded=True,
            complete=False,
        ),
        now=NOW + timedelta(days=2),
        sender=broken,
    )
    recovered, recovered_calls = _evaluate(
        tmp_path,
        state=_state(),
        now=NOW + timedelta(days=3),
    )

    assert first.daily_digest_sent is True
    assert outage_day_one.daily_digest_sent is False
    assert outage_day_two.daily_digest_sent is False
    assert recovered.daily_digest_sent is True
    body = recovered_calls[0][1]
    # Both outage days are older than 24 hours and still reported.
    assert "Other Co" in body
    assert "Third Co" in body
    assert recovered.digest_catchup_clamped is False


def test_catchup_is_clamped_to_seven_days(tmp_path):
    def broken(_subject, _body):
        raise RuntimeError("temporary SMTP outage")

    def _degraded(key, company):
        return _state(
            key=key,
            company=company,
            status=DIRECT_STATUS_DEGRADED,
            previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            reason_codes=("pagination_ended_early",),
            malformed=0,
            schema_errors=0,
            rows=10,
            incomplete=True,
            truncated=False,
            degraded=True,
            complete=False,
        )

    start = NOW - timedelta(days=10)
    _evaluate(tmp_path, state=_degraded("company:a:direct:x", "Alpha Co"), now=start)
    _evaluate(
        tmp_path,
        state=_degraded("company:b:direct:x", "Beta Co"),
        now=start + timedelta(days=1),
        sender=broken,
    )
    _evaluate(
        tmp_path,
        state=_degraded("company:c:direct:x", "Gamma Co"),
        now=NOW - timedelta(days=2),
        sender=broken,
    )
    clamped, clamped_calls = _evaluate(tmp_path, state=_state(), now=NOW)

    assert clamped.daily_digest_sent is True
    assert clamped.digest_catchup_clamped is True
    body = clamped_calls[0][1]
    assert f"more than {MAX_DIGEST_CATCHUP_DAYS} days" in body
    # Inside the clamp is reported; older than the clamp is dropped.
    assert "Gamma Co" in body
    assert "Beta Co" not in body


def test_resolve_digest_window_bounds():
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
    # Exclusive, because that digest already reported its own timestamp.
    assert inclusive is False
    assert clamped is False

    bounded, inclusive, clamped = resolve_digest_window(
        now=NOW,
        last_sent_at=NOW - timedelta(days=30),
    )
    assert bounded == NOW - timedelta(days=MAX_DIGEST_CATCHUP_DAYS)
    assert inclusive is True
    assert clamped is True


def test_live_greenhouse_run_defers_one_invalid_record_to_the_digest(
    tmp_path,
    monkeypatch,
):
    """End-to-end proof that real adapter diagnostics classify as minor.

    The unit tests above build states directly, so this run exercises the
    actual reason codes the shared record parser publishes.
    """

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
    jobs = [
        {
            "id": index,
            "title": "Software Engineering Intern",
            "absolute_url": f"https://example.com/jobs/{index}",
            "location": {"name": "United States"},
            "content": "Build Python APIs and software services.",
            "updated_at": "2026-07-20T00:00:00Z",
        }
        for index in range(1, 41)
    ]
    jobs.append({"id": 999, "title": "", "absolute_url": ""})
    monkeypatch.setattr(
        "watcher.sources.greenhouse.fetch_json",
        lambda url, source_name: {"jobs": jobs},
    )

    calls = []

    def sender(subject, body):
        calls.append((subject, body))
        return True

    def run_at(observed_at, run_id):
        with SeenStore(db) as seen:
            return run_once(
                config,
                seen_store=seen,
                github_source=[],
                alumni_index={},
                digest_sender=lambda matches: True,
                notification_mode=RUN_MODE_LIVE,
                health_observed_at=observed_at,
                run_id=run_id,
                health_alert_policy=HealthAlertPolicy(
                    mode=MODE_TRANSITIONS_ONLY,
                    hour_utc=12,
                ),
                health_alert_sender=sender,
            )

    quiet = run_at(NOW.replace(hour=6), "minor-quiet")
    quiet_calls = list(calls)
    digested = run_at(NOW, "minor-digest")

    attempt = next(
        item
        for item in quiet.source_attempts
        if item.source_kind == SOURCE_KIND_DIRECT
    )
    assert attempt.succeeded is True
    assert attempt.rows_returned == 40
    assert attempt.reason_codes == ("schema_invalid_records_skipped",)
    assert attempt.schema_error_row_count == 1

    with SourceHealthStore(db) as health:
        stored = health.current_state(direct_health_key("Test Co", "greenhouse"))
    assert stored.status == DIRECT_STATUS_DEGRADED
    assert is_minor_degradation(stored) is True

    assert quiet.health_alert_result.sent is False
    assert quiet.health_alert_result.daily_digest_sent is False
    assert quiet_calls == []

    assert digested.health_alert_result.sent is False
    assert digested.health_alert_result.daily_digest_sent is True
    subject, body = calls[0]
    assert DIGEST_SUBJECT_MARKER in subject
    assert "Test Co" in body
    assert "schema_invalid_records_skipped" in body
    assert "retained rows: 40" in body
    assert "schema=1" in body
