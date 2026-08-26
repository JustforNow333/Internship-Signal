"""Alert policy: environment loading, fallback evidence, flapping, and grouping.

Covers what makes an incident HIGH rather than deferred: per-company GitHub
fallback evidence rather than a run-wide feed success, repeat-failure deferral
for a source that keeps flapping on one error, and the narrow case where
same-family failures are obviously one shared platform incident.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from watcher.health_alerts import (
    DEFAULT_FEED_STALE_HOURS,
    FLAP_LOOKBACK_HOURS,
    FLAP_REPEAT_THRESHOLD,
    MODE_TRANSITIONS_ONLY,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_MEDIUM,
    SYSTEMIC_GROUP_MIN_COMPANIES,
    HealthAlertStore,
    group_systemic_incidents,
    load_health_alert_policy,
    render_alert_email,
    repeat_flap_deferrable,
)
from watcher.source_health import (
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    SOURCE_KIND_DIRECT,
    STATUS_FAILING,
    HealthTransition,
    SourceAttempt,
    SourceHealthStore,
    direct_health_key,
)
from watcher.tests.health_alert_helpers import (
    BEFORE_DIGEST_HOUR,
    DIGEST_SUBJECT_MARKER,
    NOW,
    _candidates_for,
    _evaluate,
    _evaluate_alert_run,
    _failed_coverage,
    _failed_direct_state,
    _failed_transition,
    _failure_candidate,
    _family_candidates,
    _feed_state,
    _prior_occurrences,
    _second_feed_state,
    _state,
    _transition,
)


def test_policy_defaults_and_validation():
    policy = load_health_alert_policy({})
    assert policy.mode == MODE_TRANSITIONS_ONLY
    assert policy.cooldown_hours == 24
    with pytest.raises(ValueError):
        load_health_alert_policy({"WATCHER_HEALTH_EMAIL_MODE": "sometimes"})


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


def test_failed_successfactors_restart_keeps_schema_failure_escalation():
    first = _failure_candidate(
        adapter="successfactors",
        github_rows=3,
        error="schema_failure",
    )
    consecutive = _failure_candidate(
        adapter="successfactors",
        github_rows=3,
        failures=2,
        error="schema_failure",
    )

    assert first.health_key == direct_health_key("Test Co", "successfactors")
    assert first.error_kind == "schema_failure"
    assert first.severity == SEVERITY_MEDIUM
    assert consecutive.error_kind == "schema_failure"
    assert consecutive.severity == SEVERITY_HIGH


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
