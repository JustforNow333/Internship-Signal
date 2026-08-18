from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from watcher.config import CompanyCfg
from watcher.config import WatcherConfig
from watcher.health_alerts import (
    DEFAULT_FEED_STALE_HOURS,
    FLAP_LOOKBACK_HOURS,
    FLAP_REPEAT_THRESHOLD,
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
    HealthAlertPolicy,
    HealthAlertStore,
    build_alert_candidates,
    evaluate_and_send_health_alerts,
    group_systemic_incidents,
    is_minor_degradation,
    load_health_alert_policy,
    render_alert_email,
    repeat_flap_deferrable,
    resolve_digest_window,
)
from watcher.seen_store import SeenStore
from watcher.run import RUN_MODE_LIVE, run_once
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT,
    COVERAGE_FAILING_BACKSTOP,
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


# --- Phase 2B: per-company fallback evidence and systemic grouping ---------

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
    """The other backstop feed, which never lists the test company."""

    overrides.setdefault("key", SECOND_GITHUB_FEED_KEY)
    overrides.setdefault(
        "feed_label", "listings [https://example.com/other-listings.md]"
    )
    return _feed_state(**overrides)


def _failed_direct_state(*, company="Test Co", adapter="greenhouse", failures=1, error="fetch_failure"):
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
    backstop_available=True,
):
    return CompanyCoverage(
        company=company,
        adapter=adapter,
        state=COVERAGE_FAILING_BACKSTOP,
        direct_status=DIRECT_STATUS_FAILED,
        direct_attempt_succeeded=False,
        direct_rows_returned=None,
        github_backstop_available=backstop_available,
        github_rows_returned=github_rows,
        github_fallback_configured=fallback_configured,
    )


def _failure_candidate(
    *,
    github_rows=None,
    fallback_configured=True,
    backstop_available=True,
    feed=None,
    feeds=None,
    evidence=frozenset(),
    failures=1,
    error="fetch_failure",
    history=None,
    now=NOW,
):
    """Build the direct-source failure candidate for one configured scenario."""

    direct = _failed_direct_state(failures=failures, error=error)
    states = {direct.health_key: direct}
    if feeds is None:
        feeds = (_feed_state() if feed is None else feed,)
    for feed_state in feeds:
        if feed_state is not None:
            states[feed_state.health_key] = feed_state
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
                backstop_available=backstop_available,
            ),
        ),
        previous_coverage=None,
        github_evidence_companies=evidence,
        failure_history=history or {},
    )
    return next(
        item for item in candidates if item.health_key == direct.health_key
    )


def _evaluate_alert_run(
    tmp_path,
    *,
    states,
    coverage,
    transitions=(),
    policy=None,
    now=NOW,
    sender=None,
    db_name="state.sqlite",
    run_id=None,
):
    calls = []

    def default_sender(subject, body):
        calls.append((subject, body))
        return True

    result = evaluate_and_send_health_alerts(
        db_path=tmp_path / db_name,
        policy=policy or HealthAlertPolicy(mode=MODE_TRANSITIONS_ONLY),
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


def test_global_github_success_alone_keeps_a_first_failure_high():
    """A feed succeeding somewhere is not evidence that it covers a company."""

    candidate = _failure_candidate(github_rows=0)
    assert candidate.github_fallback_available is True
    assert candidate.github_fallback_usable is False
    assert candidate.severity == SEVERITY_HIGH


def test_current_run_github_rows_defer_a_first_failure():
    candidate = _failure_candidate(github_rows=3)
    assert candidate.github_fallback_usable is True
    assert candidate.severity == SEVERITY_MEDIUM
    assert "GitHub fallback" in candidate.recommended_action


def test_recent_persisted_github_rows_defer_a_first_failure():
    candidate = _failure_candidate(
        github_rows=0,
        evidence=frozenset({"Test Co"}),
    )
    assert candidate.severity == SEVERITY_MEDIUM


def test_missing_company_evidence_keeps_a_first_failure_high():
    """Cold start: no counts threaded yet is unknown, never proven fallback."""

    assert _failure_candidate(github_rows=None).severity == SEVERITY_HIGH
    assert _failure_candidate(github_rows=0).severity == SEVERITY_HIGH


def test_stale_github_feed_keeps_a_first_failure_high():
    stale = _feed_state(
        last_nonzero=NOW - timedelta(hours=DEFAULT_FEED_STALE_HOURS + 1),
    )
    candidate = _failure_candidate(github_rows=8, feed=stale)
    assert candidate.github_fallback_usable is False
    assert candidate.severity == SEVERITY_HIGH


def test_never_populated_github_feed_keeps_a_first_failure_high():
    unproven = _feed_state(last_nonzero=None)
    assert _failure_candidate(github_rows=8, feed=unproven).severity == SEVERITY_HIGH


def test_failed_github_feed_keeps_a_first_failure_high():
    failing = _feed_state(status=STATUS_FAILING)
    assert _failure_candidate(github_rows=8, feed=failing).severity == SEVERITY_HIGH


def test_github_primary_company_is_never_downgraded():
    """GitHub is not a fallback where it is the company's primary source."""

    candidate = _failure_candidate(github_rows=12, fallback_configured=False)
    assert candidate.github_fallback_usable is False
    assert candidate.severity == SEVERITY_HIGH


def test_second_consecutive_failure_is_high_despite_proven_fallback():
    candidate = _failure_candidate(github_rows=25, failures=2)
    assert candidate.github_fallback_usable is True
    assert candidate.severity == SEVERITY_HIGH


@pytest.mark.parametrize(
    "broken_feed",
    [
        pytest.param(_feed_state(status=STATUS_FAILING), id="failing"),
        pytest.param(
            _feed_state(last_nonzero=NOW - timedelta(hours=DEFAULT_FEED_STALE_HOURS + 1)),
            id="stale",
        ),
    ],
)
def test_one_broken_github_feed_keeps_a_first_failure_high(broken_feed):
    """D1: historical evidence does not record which feed supplied the rows.

    Company X was only ever carried by feed A. Feed A is now broken, feed B is
    healthy but lists nothing for the company on this run, and the company's
    direct source just failed for the first time. Whether the fallback still
    covers the company is unknown, so it must not be treated as proven.
    """

    candidate = _failure_candidate(
        github_rows=0,
        evidence=frozenset({"Test Co"}),
        feeds=(broken_feed, _second_feed_state()),
    )
    assert candidate.github_fallback_usable is False
    assert candidate.severity == SEVERITY_HIGH


def test_all_healthy_github_feeds_still_defer_a_first_failure():
    """Control: with the whole backstop intact, evidence still downgrades."""

    candidate = _failure_candidate(
        github_rows=0,
        evidence=frozenset({"Test Co"}),
        feeds=(_feed_state(), _second_feed_state()),
    )
    assert candidate.github_fallback_usable is True
    assert candidate.severity == SEVERITY_MEDIUM


def test_second_failure_is_high_with_every_github_feed_healthy():
    """Control: escalation ignores fallback evidence entirely."""

    candidate = _failure_candidate(
        github_rows=25,
        evidence=frozenset({"Test Co"}),
        feeds=(_feed_state(), _second_feed_state()),
        failures=2,
    )
    assert candidate.severity == SEVERITY_HIGH


def test_first_failure_with_a_broken_feed_emails_immediately(tmp_path):
    """The HIGH from a half-broken backstop is delivered, not deferred."""

    company = "Company X"
    coverage = (_failed_coverage(company=company, github_rows=0),)
    with HealthAlertStore(tmp_path / "state.sqlite") as store:
        store.record_coverage_snapshot(
            run_id="seed-history",
            observed_at=NOW - timedelta(days=2),
            coverage=(_failed_coverage(company=company, github_rows=4),),
        )

    failed = _failed_direct_state(company=company, failures=1)
    broken = _feed_state(status=STATUS_FAILING)
    healthy = _second_feed_state()
    result, calls = _evaluate_alert_run(
        tmp_path,
        states={
            failed.health_key: failed,
            broken.health_key: broken,
            healthy.health_key: healthy,
        },
        coverage=coverage,
        transitions=(_failed_transition(failed),),
        now=BEFORE_DIGEST_HOUR,
    )
    assert result.sent is True
    assert "HIGH" in calls[0][1]
    assert company in calls[0][1]


def test_ibm_first_failure_defers_and_second_failure_escalates(tmp_path):
    """The reported IBM incident: one covered failure waits, two do not."""

    db = tmp_path / "state.sqlite"
    coverage_with_rows = _failed_coverage(company="IBM", adapter="workday", github_rows=4)
    with HealthAlertStore(db) as store:
        store.record_coverage_snapshot(
            run_id="seed-history",
            observed_at=NOW - timedelta(days=2),
            coverage=(coverage_with_rows,),
        )

    first = _failed_direct_state(company="IBM", adapter="workday", failures=1)
    feed = _feed_state()
    # No IBM rows on this run; only the persisted week-old evidence applies.
    coverage = (_failed_coverage(company="IBM", adapter="workday", github_rows=0),)
    deferred, deferred_calls = _evaluate_alert_run(
        tmp_path,
        states={first.health_key: first, feed.health_key: feed},
        coverage=coverage,
        transitions=(_failed_transition(first),),
        now=BEFORE_DIGEST_HOUR,
    )
    assert deferred.sent is False
    assert deferred_calls == []

    second = _failed_direct_state(company="IBM", adapter="workday", failures=2)
    escalated, escalated_calls = _evaluate_alert_run(
        tmp_path,
        states={second.health_key: second, feed.health_key: feed},
        coverage=coverage,
        now=BEFORE_DIGEST_HOUR + timedelta(hours=1),
    )
    assert escalated.sent is True
    assert "IBM" in escalated_calls[0][1]

    # Phase 2A lifecycle collapsing: the earlier MEDIUM must not resurface as
    # an unresolved duplicate once the incident escalated.
    third = _failed_direct_state(company="IBM", adapter="workday", failures=3)
    digest, digest_calls = _evaluate_alert_run(
        tmp_path,
        states={third.health_key: third, feed.health_key: feed},
        coverage=coverage,
        now=NOW,
    )
    assert digest.daily_digest_sent is False
    assert digest_calls == []


def test_deferred_first_failure_is_reported_by_the_daily_digest(tmp_path):
    db = tmp_path / "state.sqlite"
    coverage = (_failed_coverage(company="IBM", adapter="workday", github_rows=6),)
    failed = _failed_direct_state(company="IBM", adapter="workday", failures=1)
    feed = _feed_state()
    deferred, deferred_calls = _evaluate_alert_run(
        tmp_path,
        states={failed.health_key: failed, feed.health_key: feed},
        coverage=coverage,
        transitions=(_failed_transition(failed),),
        now=BEFORE_DIGEST_HOUR,
    )
    assert deferred.sent is False
    assert deferred_calls == []

    recovered = replace(
        _state(
            key=failed.health_key,
            company="IBM",
            status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            previous_status=DIRECT_STATUS_FAILED,
        ),
        adapter="workday",
    )
    result, calls = _evaluate_alert_run(
        tmp_path,
        states={recovered.health_key: recovered, feed.health_key: feed},
        coverage=coverage,
        transitions=(
            HealthTransition(
                health_key=failed.health_key,
                source_kind=SOURCE_KIND_DIRECT,
                company="IBM",
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
    subject, body = calls[0]
    assert DIGEST_SUBJECT_MARKER in subject
    assert "IBM" in body
    assert db.is_file()


def test_legacy_coverage_snapshots_stay_readable(tmp_path):
    """A pre-change production database keeps working without a wipe."""

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
                (NOW - timedelta(hours=1)).isoformat(),
                '{"Legacy Co": "direct_covered"}',
            ),
        )
        store._conn.commit()
        assert store.latest_coverage_snapshot() == {"Legacy Co": "direct_covered"}
        # A legacy entry carries no company-level evidence, so it proves nothing.
        assert store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        ) == frozenset()


def test_new_snapshots_persist_company_github_evidence(tmp_path):
    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        store.record_coverage_snapshot(
            run_id="run-1",
            observed_at=NOW - timedelta(hours=2),
            coverage=(
                _failed_coverage(company="Covered Co", github_rows=3),
                _failed_coverage(company="Quiet Co", github_rows=0),
                _failed_coverage(company="Unknown Co", github_rows=None),
            ),
        )
        # States still read back exactly as before.
        assert store.latest_coverage_snapshot() == {
            "Covered Co": COVERAGE_FAILING_BACKSTOP,
            "Quiet Co": COVERAGE_FAILING_BACKSTOP,
            "Unknown Co": COVERAGE_FAILING_BACKSTOP,
        }
        # Only a positive count is evidence.
        assert store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        ) == frozenset({"Covered Co"})


def test_snapshot_retention_covers_the_evidence_horizon(tmp_path):
    """Hourly runs must not evict evidence inside the seven-day horizon."""

    assert MAX_COVERAGE_SNAPSHOTS >= GITHUB_EVIDENCE_HORIZON_DAYS * 24
    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
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
        assert retained == GITHUB_EVIDENCE_HORIZON_DAYS * 24 + 1
        assert store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        ) == frozenset({"Covered Co"})


def test_replaying_a_run_id_does_not_duplicate_or_corrupt_evidence(tmp_path):
    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        for github_rows in (5, 0):
            store.record_coverage_snapshot(
                run_id="repeated-run",
                observed_at=NOW - timedelta(hours=1),
                coverage=(_failed_coverage(company="Covered Co", github_rows=github_rows),),
            )
        stored = store._conn.execute(
            "select count(*) from source_health_coverage_snapshots"
        ).fetchone()[0]
        assert stored == 1
        # The replayed run replaced its own entry rather than adding one.
        assert store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        ) == frozenset()


def _family_candidates(adapter, error_kinds, *, start=0):
    """Build one HIGH failure candidate per supplied error kind."""

    states = {}
    coverage = []
    for index, error_kind in enumerate(error_kinds):
        company = f"{adapter} co {start + index}"
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


def _prior_occurrences(
    count,
    *,
    error_kind="fetch_failure",
    fallback_available=True,
    fallback_usable=False,
):
    """Build stored prior failure events for the default failing source."""

    template = replace(
        _failure_candidate(),
        error_kind=error_kind,
        github_fallback_available=fallback_available,
        github_fallback_usable=fallback_usable,
    )
    return {
        (template.health_key, error_kind): tuple(
            replace(template, run_id=f"prior-{index}") for index in range(count)
        )
    }


def test_a_first_failure_without_repeat_history_stays_high():
    """DoorDash's case: nothing recurring to compare against, so it interrupts."""

    candidate = _failure_candidate(github_rows=0, history={})

    assert candidate.consecutive_failures == 1
    assert candidate.github_fallback_usable is False
    assert candidate.severity == SEVERITY_HIGH


def test_repeated_isolated_failures_on_one_error_defer_to_the_digest():
    candidate = _failure_candidate(
        github_rows=0,
        history=_prior_occurrences(FLAP_REPEAT_THRESHOLD),
    )

    assert candidate.severity == SEVERITY_MEDIUM
    assert "keeps failing and recovering" in candidate.recommended_action
    # Deferral is about repetition, not about coverage, so the unproven
    # fallback verdict is reported exactly as it was calculated.
    assert candidate.github_fallback_usable is False


def test_one_occurrence_below_the_threshold_stays_high():
    candidate = _failure_candidate(
        github_rows=0,
        history=_prior_occurrences(FLAP_REPEAT_THRESHOLD - 1),
    )

    assert candidate.severity == SEVERITY_HIGH


def test_second_consecutive_failure_stays_high_despite_flap_history():
    """A source that is down now is not a source that keeps bouncing back."""

    candidate = _failure_candidate(
        github_rows=0,
        failures=2,
        history=_prior_occurrences(FLAP_REPEAT_THRESHOLD * 3),
    )

    assert candidate.severity == SEVERITY_HIGH
    assert "Inspect the sanitized source-health report" in candidate.recommended_action


def test_a_new_error_kind_stays_high_despite_flap_history():
    """The stored fingerprint omits the error kind, so the rule cannot use it."""

    candidate = _failure_candidate(
        github_rows=0,
        error="unexpected_exception",
        history=_prior_occurrences(
            FLAP_REPEAT_THRESHOLD * 2,
            error_kind="schema_failure",
        ),
    )

    assert candidate.error_kind == "unexpected_exception"
    assert candidate.severity == SEVERITY_HIGH


@pytest.mark.parametrize(
    "history_kwargs, candidate_kwargs",
    [
        ({"fallback_usable": True}, {"github_rows": 0}),
        ({"fallback_available": True}, {"backstop_available": False}),
    ],
    ids=["usable", "available"],
)
def test_worse_fallback_posture_keeps_a_repeated_failure_high(
    history_kwargs,
    candidate_kwargs,
):
    candidate = _failure_candidate(
        history=_prior_occurrences(
            FLAP_REPEAT_THRESHOLD * 2,
            **history_kwargs,
        ),
        **candidate_kwargs,
    )

    assert candidate.severity == SEVERITY_HIGH


def test_unproven_fallback_posture_cannot_regress():
    """Posture that was never proven has nothing to lose, so deferral holds."""

    candidate = _failure_candidate(
        github_rows=0,
        history=_prior_occurrences(
            FLAP_REPEAT_THRESHOLD,
            fallback_available=None,
            fallback_usable=None,
        ),
    )

    assert candidate.severity == SEVERITY_MEDIUM


def test_repeat_history_reads_only_the_bounded_window(tmp_path):
    """Older occurrences fall out of the lookback and stop counting."""

    db = tmp_path / "state.sqlite"
    candidate = _failure_candidate(github_rows=0)
    stale = NOW - timedelta(hours=FLAP_LOOKBACK_HOURS + 1)
    with HealthAlertStore(db) as store:
        for index in range(FLAP_REPEAT_THRESHOLD):
            store.record_detected(
                replace(candidate, run_id=f"stale-{index}"),
                detected_at=stale + timedelta(minutes=index),
            )
        for index in range(FLAP_REPEAT_THRESHOLD - 1):
            store.record_detected(
                replace(candidate, run_id=f"fresh-{index}"),
                detected_at=NOW - timedelta(hours=index + 1),
            )
        history = store.recent_failure_occurrences(
            since=NOW - timedelta(hours=FLAP_LOOKBACK_HOURS),
        )

    occurrences = history[(candidate.health_key, "fetch_failure")]
    assert len(occurrences) == FLAP_REPEAT_THRESHOLD - 1
    assert _failure_candidate(github_rows=0, history=history).severity == SEVERITY_HIGH


def test_repeat_history_ignores_recoveries_and_other_sources(tmp_path):
    db = tmp_path / "state.sqlite"
    failure = _failure_candidate(github_rows=0)
    recovery = replace(
        failure,
        fingerprint=f"recovery|{failure.health_key}",
        alert_type="recovery",
        severity=SEVERITY_INFO,
    )
    other = replace(
        failure,
        fingerprint="source_failure|company:other:direct:greenhouse",
        health_key="company:other:direct:greenhouse",
    )
    with HealthAlertStore(db) as store:
        for index in range(FLAP_REPEAT_THRESHOLD):
            store.record_detected(
                replace(recovery, run_id=f"recovery-{index}"),
                detected_at=NOW - timedelta(hours=index + 1),
            )
            store.record_detected(
                replace(other, run_id=f"other-{index}"),
                detected_at=NOW - timedelta(hours=index + 1),
            )
        history = store.recent_failure_occurrences(
            since=NOW - timedelta(hours=FLAP_LOOKBACK_HOURS),
        )

    assert (failure.health_key, "fetch_failure") not in history
    assert _failure_candidate(github_rows=0, history=history).severity == SEVERITY_HIGH


def test_repeated_failures_escalate_then_defer_and_reach_the_digest(tmp_path):
    """End to end: the mode alerts while it is news, then joins the digest."""

    failing = _state(
        status=DIRECT_STATUS_FAILED,
        previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        failures=1,
        rows=None,
        error="schema_failure",
    )
    recovered = _state(
        status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        previous_status=DIRECT_STATUS_FAILED,
        failures=0,
        rows=7,
    )
    recovery_transition = (
        _transition(
            DIRECT_STATUS_FAILED,
            DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            recovery=True,
        ),
    )
    results = []
    for hour in (1, 3, 5, 7):
        results.append(_evaluate(tmp_path, state=failing, now=NOW.replace(hour=hour)))
        _evaluate(
            tmp_path,
            state=recovered,
            transition=recovery_transition,
            now=NOW.replace(hour=hour + 1),
        )
    digest, digest_calls = _evaluate(tmp_path, state=recovered, now=NOW)

    # Each recovery reopens the incident, so the first three occurrences still
    # bypass the cooldown and interrupt. The fourth only repeats a mode already
    # alerted three times inside the window.
    assert [item[0].sent for item in results] == [True, True, True, False]
    assert results[-1][0].candidates == 1
    body = digest_calls[0][1]
    assert digest.digest_incidents_reported == 1
    assert body.count("occurrences:") == 1
    assert "occurrences: 1" in body
    assert "recovered later: yes" in body


def test_recovery_lifecycle_is_unchanged_without_repeat_history(tmp_path):
    """failure -> recovery -> failure still reopens and re-alerts on its own."""

    failing = _state(
        status=DIRECT_STATUS_FAILED,
        previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        failures=1,
        rows=None,
        error="schema_failure",
    )
    recovered = _state(
        status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        previous_status=DIRECT_STATUS_FAILED,
        failures=0,
        rows=7,
    )
    first, _ = _evaluate(tmp_path, state=failing, now=NOW.replace(hour=3))
    recovery, recovery_calls = _evaluate(
        tmp_path,
        state=recovered,
        transition=(
            _transition(
                DIRECT_STATUS_FAILED,
                DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
                recovery=True,
            ),
        ),
        now=NOW.replace(hour=4),
    )
    second, _ = _evaluate(tmp_path, state=failing, now=NOW.replace(hour=5))

    assert first.sent is True
    # The recovery stays INFO and is never an immediate email.
    assert recovery.sent is False
    assert recovery_calls == []
    # One intervening recovery still reopens the incident inside the cooldown.
    assert second.sent is True


def test_repeat_deferral_never_fires_without_stored_history(tmp_path):
    """Existing deployments with no retained events keep today's routing."""

    failing = _state(
        status=DIRECT_STATUS_FAILED,
        previous_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        failures=1,
        rows=None,
        error="schema_failure",
    )
    with HealthAlertStore(tmp_path / "state.sqlite") as store:
        store.record_detected(
            _failure_candidate(github_rows=0),
            detected_at=NOW - timedelta(hours=FLAP_LOOKBACK_HOURS * 2),
        )
    result, calls = _evaluate(tmp_path, state=failing, now=NOW.replace(hour=3))

    assert result.sent is True
    assert "HIGH" in calls[0][1]


def test_repeat_flap_rule_is_pure_and_source_agnostic():
    occurrences = _prior_occurrences(FLAP_REPEAT_THRESHOLD)[
        (direct_health_key("Test Co", "greenhouse"), "fetch_failure")
    ]

    assert (
        repeat_flap_deferrable(
            consecutive_failures=1,
            error_kind="fetch_failure",
            fallback_available=True,
            fallback_usable=False,
            occurrences=occurrences,
        )
        is True
    )
    # Every refusal path, independent of any company or adapter.
    assert (
        repeat_flap_deferrable(
            consecutive_failures=2,
            error_kind="fetch_failure",
            fallback_available=True,
            fallback_usable=False,
            occurrences=occurrences,
        )
        is False
    )
    assert (
        repeat_flap_deferrable(
            consecutive_failures=1,
            error_kind=None,
            fallback_available=True,
            fallback_usable=False,
            occurrences=occurrences,
        )
        is False
    )
    assert (
        repeat_flap_deferrable(
            consecutive_failures=1,
            error_kind="fetch_failure",
            fallback_available=False,
            fallback_usable=False,
            occurrences=occurrences,
        )
        is False
    )
    assert (
        repeat_flap_deferrable(
            consecutive_failures=1,
            error_kind="fetch_failure",
            fallback_available=True,
            fallback_usable=False,
            occurrences=occurrences[:-1],
        )
        is False
    )


def test_dominant_same_family_failures_group_into_one_high_section():
    candidates = _candidates_for(
        ("workday", ["fetch_failure/redirected_to_html"] * 20),
    )
    groups, remaining = group_systemic_incidents(candidates)
    assert len(groups) == 1
    assert groups[0].adapter_family == "workday"
    assert groups[0].error_kind == "fetch_failure/redirected_to_html"
    assert groups[0].affected_companies == 20
    assert remaining == ()

    subject, body = render_alert_email(candidates)
    assert "shared source incident" in subject
    assert body.count("HIGH:") == 1
    # Every affected company is still named in full.
    for candidate in candidates:
        assert candidate.source_label in body


def test_four_matching_failures_do_not_group():
    candidates = _candidates_for(
        ("workday", ["fetch_failure/redirected_to_html"] * (SYSTEMIC_GROUP_MIN_COMPANIES - 1)),
    )
    groups, remaining = group_systemic_incidents(candidates)
    assert groups == ()
    assert len(remaining) == SYSTEMIC_GROUP_MIN_COMPANIES - 1
    assert render_alert_email(candidates)[1].count("HIGH:") == 4


def test_failures_without_a_dominant_error_kind_do_not_group():
    # Five failures split across unrelated kinds: nothing reaches the floor.
    scattered = _candidates_for(
        (
            "workday",
            [
                "fetch_failure/redirected_to_html",
                "fetch_failure/redirected_to_html",
                "fetch_failure/timeout",
                "schema_failure",
                "unexpected_exception",
            ],
        ),
    )
    assert group_systemic_incidents(scattered)[0] == ()

    # Nine failures where the leading kind clears the floor but only holds
    # 5/9 = 55.6% of the family, below the dominance threshold.
    diluted = _candidates_for(
        (
            "workday",
            ["fetch_failure/redirected_to_html"] * 5 + ["schema_failure"] * 4,
        ),
    )
    assert group_systemic_incidents(diluted)[0] == ()


def test_same_error_across_families_groups_separately():
    candidates = _candidates_for(
        ("workday", ["fetch_failure/redirected_to_html"] * 6),
        ("icims", ["fetch_failure/redirected_to_html"] * 5),
    )
    groups, remaining = group_systemic_incidents(candidates)
    assert [group.adapter_family for group in groups] == ["icims", "workday"]
    assert [group.affected_companies for group in groups] == [5, 6]
    assert remaining == ()


def test_grouping_leaves_untouched_candidates_reported_individually():
    candidates = _candidates_for(
        ("workday", ["fetch_failure/redirected_to_html"] * 6),
        ("greenhouse", ["schema_failure"]),
    )
    groups, remaining = group_systemic_incidents(candidates)
    assert len(groups) == 1
    assert len(remaining) == 1
    assert remaining[0].adapter == "greenhouse"
    body = render_alert_email(candidates)[1]
    assert body.count("HIGH:") == 2


def test_grouping_does_not_alter_persisted_company_health_state(tmp_path):
    db = tmp_path / "state.sqlite"
    states, coverage = _family_candidates(
        "workday",
        ["fetch_failure/redirected_to_html"] * 6,
    )
    with SourceHealthStore(db) as health:
        for state in states.values():
            health.record_attempts(
                [
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
                ]
            )
    result, calls = _evaluate_alert_run(
        tmp_path,
        states=states,
        coverage=coverage,
        now=NOW,
    )
    assert result.sent is True
    assert "shared source incident" in calls[0][0]

    with SourceHealthStore(db) as health:
        stored = health.all_current_states()
    assert len(stored) == len(states)
    for state in states.values():
        persisted = stored[state.health_key]
        assert persisted.status == DIRECT_STATUS_FAILED
        assert persisted.consecutive_failures == 1
        assert persisted.company == state.company
        assert persisted.last_error_kind == "fetch_failure"


def test_recovery_after_a_grouped_outage_stays_company_specific(tmp_path):
    states, coverage = _family_candidates(
        "workday",
        ["fetch_failure/redirected_to_html"] * 6,
    )
    grouped, grouped_calls = _evaluate_alert_run(
        tmp_path,
        states=states,
        coverage=coverage,
        now=BEFORE_DIGEST_HOUR,
    )
    assert grouped.sent is True
    assert "shared source incident" in grouped_calls[0][0]

    recovering = next(iter(states.values()))
    recovered = replace(
        recovering,
        status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        previous_status=DIRECT_STATUS_FAILED,
        consecutive_failures=0,
        last_rows_returned=11,
        last_error_kind=None,
    )
    result, calls = _evaluate_alert_run(
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
    # The recovery is INFO and never interrupts; the remaining five companies
    # stay suppressed by their own cooldowns.
    assert result.sent is False
    assert result.daily_digest_sent is True
    subject, body = calls[0]
    assert DIGEST_SUBJECT_MARKER in subject
    assert recovered.company in body
