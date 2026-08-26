"""Shared state, coverage, candidate, and evaluation builders for the health
tests.

These are the doubles the alert, digest, policy, and store test modules all
need. They build real ``SourceHealthState``, ``CompanyCoverage``, and
``HealthAlertCandidate`` values rather than stubs, so every test still
exercises the production dataclasses.
"""

from dataclasses import fields, replace
from datetime import datetime, timezone

from watcher.health_alerts import (
    MODE_TRANSITIONS_ONLY,
    HealthAlertPolicy,
    build_alert_candidates,
    evaluate_and_send_health_alerts,
)
from watcher.source_health import (
    COVERAGE_DIRECT,
    COVERAGE_FAILING_BACKSTOP,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_HEALTHY,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceHealthState,
    direct_health_key,
)


NOW = datetime(2026, 7, 28, 12, tzinfo=timezone.utc)
# An hour before the digest hour, so an incident is recorded without triggering
# delivery. Every digest assertion then runs at NOW.
BEFORE_DIGEST_HOUR = NOW.replace(hour=6)
DIGEST_SUBJECT_MARKER = "Daily Source Health"
# Two stable feed keys, so a test can prove that one broken GitHub feed
# withdraws the fallback even while another stays healthy.
GITHUB_FEED_KEY = "github_feed:0123456789abcdef"
SECOND_GITHUB_FEED_KEY = "github_feed:fedcba9876543210"


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
    adapter="greenhouse",
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

    direct = _failed_direct_state(adapter=adapter, failures=failures, error=error)
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
                adapter=adapter,
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
