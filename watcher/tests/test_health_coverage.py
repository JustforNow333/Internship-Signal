"""Per-run company coverage, GitHub row attribution, and the run summary.

Owned by :mod:`watcher.health.coverage`; the configuration-only audit half
lives in ``test_coverage_audit.py``.
"""

import pytest

from watcher.config import CompanyCfg
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT,
    COVERAGE_DIRECT_EMPTY,
    COVERAGE_FAILING_BACKSTOP,
    COVERAGE_UNCOVERED,
    GITHUB_PRIMARY_ATS,
    SOURCE_KIND_GITHUB_FEED,
    calculate_company_coverage,
    calculate_next_state,
    count_github_rows_by_company,
    summarize_health,
)
from watcher.sources.base import make_row
from watcher.tests.health_state_helpers import (
    attempt,
    diagnostic_attempt,
)


def _coverage(companies, direct_attempt, direct_state, github_succeeded):
    attempts = [direct_attempt]
    states = {direct_state.health_key: direct_state}
    if github_succeeded is not None:
        github = attempt(
            run_id=direct_attempt.run_id,
            source_kind=SOURCE_KIND_GITHUB_FEED,
            succeeded=github_succeeded,
            rows=0 if github_succeeded else None,
        )
        attempts.append(github)
        states[github.health_key] = calculate_next_state(None, github)
    return calculate_company_coverage(companies, attempts, states)[0]


def test_company_coverage_states():
    direct_company = CompanyCfg(name="Example Co", ats="greenhouse")
    success = diagnostic_attempt(rows=2)
    success_state = calculate_next_state(None, success)
    assert _coverage((direct_company,), success, success_state, False).state == COVERAGE_DIRECT

    zero = diagnostic_attempt(rows=0)
    zero_state = calculate_next_state(None, zero)
    assert _coverage((direct_company,), zero, zero_state, False).state == COVERAGE_DIRECT_EMPTY

    failed = attempt(succeeded=False, rows=None)
    degraded = calculate_next_state(None, failed)
    assert _coverage((direct_company,), failed, degraded, True).state == COVERAGE_FAILING_BACKSTOP
    assert _coverage((direct_company,), failed, degraded, False).state == COVERAGE_UNCOVERED

    failed2 = calculate_next_state(degraded, attempt(run_id="run-2", succeeded=False, rows=None))
    failing = calculate_next_state(failed2, attempt(run_id="run-3", succeeded=False, rows=None))
    third_failure = attempt(run_id="run-3", succeeded=False, rows=None)
    assert _coverage((direct_company,), third_failure, failing, True).state == COVERAGE_FAILING_BACKSTOP


@pytest.mark.parametrize("adapter", ["bespoke", "github_only"])
def test_unsupported_coverage_uses_feed_availability_not_active_posting(adapter):
    company = CompanyCfg(name="Example Co", ats=adapter)
    unsupported = attempt(
        adapter=adapter,
        attempted=False,
        succeeded=None,
        rows=None,
        unsupported_reason=adapter,
    )
    state = calculate_next_state(None, unsupported)
    assert _coverage((company,), unsupported, state, True).state == COVERAGE_BACKSTOP_ONLY
    assert _coverage((company,), unsupported, state, False).state == COVERAGE_UNCOVERED


def test_github_row_counts_map_labels_and_aliases_onto_configured_companies():
    companies = (
        CompanyCfg(name="Example Co", ats="greenhouse", aliases=("Example Corp",)),
        CompanyCfg(name="Other Co", ats="workday"),
    )
    rows = [
        make_row(source="github", source_adapter="simplify_json", company="Example Co"),
        make_row(source="github", source_adapter="simplify_json", company="Example Corp"),
        # Direct rows are this company's own source, never fallback evidence.
        make_row(source="direct", source_adapter="greenhouse", company="Example Co"),
        # An unconfigured company is attributed to nobody.
        make_row(source="github", source_adapter="simplify_json", company="Unlisted Inc"),
    ]
    assert count_github_rows_by_company(rows, companies) == {
        "Example Co": 2,
        "Other Co": 0,
    }


def test_github_row_counts_ignore_rows_without_usable_source_metadata():
    companies = (CompanyCfg(name="Example Co", ats="greenhouse"),)
    rows = [
        {"company": "Example Co", "extra": None},
        {"company": "Example Co"},
        {"company": "", "extra": {"source": "github"}},
    ]
    assert count_github_rows_by_company(rows, companies) == {"Example Co": 0}


def test_coverage_reports_company_github_rows_and_fallback_configuration():
    company = CompanyCfg(name="Example Co", ats="greenhouse")
    failed = attempt(succeeded=False, rows=None)
    state = calculate_next_state(None, failed)
    github = attempt(source_kind=SOURCE_KIND_GITHUB_FEED, rows=7)
    states = {state.health_key: state, github.health_key: calculate_next_state(None, github)}

    covered = calculate_company_coverage(
        (company,),
        [failed, github],
        states,
        {"Example Co": 3},
    )[0]
    assert covered.github_rows_returned == 3
    assert covered.github_fallback_configured is True
    # The coarse global signal is untouched and still answers only "some feed
    # succeeded somewhere".
    assert covered.github_backstop_available is True

    unthreaded = calculate_company_coverage((company,), [failed, github], states)[0]
    assert unthreaded.github_rows_returned is None
    assert unthreaded.github_fallback_configured is True


def test_one_failed_github_feed_withdraws_the_backstop_for_the_run():
    """Aggregate evidence cannot say which feed covered a company.

    With two backstop feeds, a surviving feed does not show that the company
    the failed feed carried is still covered, so the run-level backstop signal
    fails closed.
    """

    company = CompanyCfg(name="Example Co", ats="greenhouse")
    failed = attempt(succeeded=False, rows=None)
    feed_a = attempt(
        source_kind=SOURCE_KIND_GITHUB_FEED,
        feed_label="https://example.test/feed-a.json",
        succeeded=False,
        rows=None,
        error_kind="fetch_failure",
    )
    feed_b = attempt(
        source_kind=SOURCE_KIND_GITHUB_FEED,
        feed_label="https://example.test/feed-b.json",
        rows=5,
    )
    states = {
        item.health_key: calculate_next_state(None, item)
        for item in (failed, feed_a, feed_b)
    }

    partial = calculate_company_coverage((company,), [failed, feed_a, feed_b], states)[0]
    assert partial.github_backstop_available is False
    assert partial.github_fallback_configured is False
    assert partial.state == COVERAGE_UNCOVERED

    whole = calculate_company_coverage((company,), [failed, feed_b], states)[0]
    assert whole.github_backstop_available is True
    assert whole.github_fallback_configured is True
    assert whole.state == COVERAGE_FAILING_BACKSTOP


@pytest.mark.parametrize("adapter", sorted(GITHUB_PRIMARY_ATS))
def test_github_primary_companies_never_report_github_as_a_fallback(adapter):
    company = CompanyCfg(name="Example Co", ats=adapter)
    unsupported = attempt(
        adapter=adapter,
        attempted=False,
        succeeded=None,
        rows=None,
        unsupported_reason=adapter,
    )
    state = calculate_next_state(None, unsupported)
    github = attempt(source_kind=SOURCE_KIND_GITHUB_FEED, rows=9)
    states = {
        state.health_key: state,
        github.health_key: calculate_next_state(None, github),
    }
    coverage = calculate_company_coverage(
        (company,),
        [unsupported, github],
        states,
        {"Example Co": 9},
    )[0]
    assert coverage.github_rows_returned == 9
    assert coverage.github_fallback_configured is False


def test_health_summary_counts_current_states_coverage_and_transitions():
    companies = (CompanyCfg(name="Example Co", ats="greenhouse"),)
    direct = diagnostic_attempt(rows=1)
    github = attempt(source_kind=SOURCE_KIND_GITHUB_FEED, rows=0)
    states = {
        direct.health_key: calculate_next_state(None, direct),
        github.health_key: calculate_next_state(None, github),
    }
    coverage = calculate_company_coverage(companies, [direct, github], states)
    summary = summarize_health(companies, [direct, github], states, (), coverage)
    assert summary.companies_configured == 1
    assert summary.direct_healthy == 1
    assert summary.github_feeds_healthy == 1
    assert summary.uncovered_companies == 0
