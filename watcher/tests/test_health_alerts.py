"""Health-alert orchestration: immediate delivery, cooldown, and run isolation.

Covers :func:`watcher.health.service.evaluate_and_send_health_alerts` end to
end, including the guarantee that source-health email never touches internship
notification state.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from watcher.config import CompanyCfg
from watcher.config import WatcherConfig
from watcher.health_alerts import (
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
)
from watcher.seen_store import SeenStore
from watcher.run import RUN_MODE_LIVE, run_once
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT,
    COVERAGE_UNCOVERED,
    DIRECT_STATUS_UNKNOWN,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_FAILING,
    STATUS_HEALTHY,
    SourceAttempt,
    SourceHealthStore,
    direct_health_key,
)
from watcher.sources import SourceFetchError
from watcher.sources.base import make_row
from watcher.tests.health_alert_helpers import (
    BEFORE_DIGEST_HOUR,
    DIGEST_SUBJECT_MARKER,
    NOW,
    _coverage,
    _evaluate,
    _minor_state,
    _state,
    _summary,
    _transition,
)


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
        "watcher.pipeline.SourceComparisonStore.save",
        fail_save,
    )

    def fail_health_evaluation(**kwargs):
        raise RuntimeError("health alert storage unavailable")

    monkeypatch.setattr(
        "watcher.pipeline.evaluate_and_send_health_alerts",
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
