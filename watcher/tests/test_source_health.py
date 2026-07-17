import io
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from watcher.config import CompanyCfg
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DEGRADED_BACKSTOP,
    COVERAGE_DIRECT,
    COVERAGE_DIRECT_EMPTY,
    COVERAGE_FAILING_BACKSTOP,
    COVERAGE_UNCOVERED,
    ERROR_FETCH,
    MAX_ERROR_LENGTH,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_DEGRADED,
    STATUS_EMPTY,
    STATUS_FAILING,
    STATUS_HEALTHY,
    STATUS_UNSUPPORTED,
    CompanyCoverage,
    HealthSummary,
    SourceAttempt,
    SourceHealthStore,
    calculate_company_coverage,
    calculate_next_state,
    direct_health_key,
    github_feed_health_key,
    render_github_actions_report,
    sanitize_error,
    sanitize_feed_label,
    summarize_health,
    transition_for,
    write_health_report,
)

NOW = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)


def attempt(
    *,
    run_id="run-1",
    rows=1,
    succeeded=True,
    source_kind=SOURCE_KIND_DIRECT,
    company="Example Co",
    adapter="greenhouse",
    observed_at=NOW,
    attempted=True,
    error_kind=None,
    error_message=None,
    feed_label=None,
    unsupported_reason=None,
):
    if source_kind == SOURCE_KIND_GITHUB_FEED:
        company = None
        adapter = "github_listings"
        feed_label = feed_label or "https://example.test/listings.json"
        key = github_feed_health_key(feed_label)
    else:
        key = direct_health_key(company, adapter)
    return SourceAttempt(
        health_key=key,
        run_id=run_id,
        observed_at=observed_at,
        source_kind=source_kind,
        company=company,
        adapter=adapter,
        attempted=attempted,
        succeeded=succeeded,
        rows_returned=rows,
        error_kind=error_kind,
        error_message=error_message,
        feed_label=feed_label,
        unsupported_reason=unsupported_reason,
    )


def next_state(previous, **kwargs):
    return calculate_next_state(previous, attempt(**kwargs))


def test_legacy_seen_database_upgrades_without_changing_seen_rows(tmp_path):
    path = tmp_path / "seen.sqlite"
    expected = ("job-1", "Example", "Intern", "https://example.test/1", "direct", "old", None)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "create table seen(job_id text primary key, company text, title text, url text, first_source text, first_seen text, emailed_at text)"
        )
        conn.execute("insert into seen values (?, ?, ?, ?, ?, ?, ?)", expected)

    with SourceHealthStore(path) as store:
        assert store.attempt_count() == 0

    with sqlite3.connect(path) as conn:
        assert conn.execute("select * from seen").fetchone() == expected
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    assert {"seen", "source_health_attempts", "source_health_current"} <= tables


def test_attempt_history_appends_current_state_upserts_and_reopen_preserves_counters(tmp_path):
    path = tmp_path / "seen.sqlite"
    with SourceHealthStore(path) as store:
        first_states, first_transitions = store.record_attempts([attempt(run_id="run-1", succeeded=False, rows=None)])
        second_states, second_transitions = store.record_attempts([attempt(run_id="run-2", succeeded=False, rows=None)])
        assert first_transitions == ()
        assert second_transitions == ()
        assert first_states[next(iter(first_states))].consecutive_failures == 1
        assert second_states[next(iter(second_states))].consecutive_failures == 2
        assert store.attempt_count() == 2

    with SourceHealthStore(path) as reopened:
        state = reopened.current_state(direct_health_key("Example Co", "greenhouse"))
        assert state.total_attempts == 2
        assert state.total_successes == 0
        assert state.consecutive_failures == 2


def test_timestamps_are_normalized_to_utc(tmp_path):
    local_time = datetime(2026, 7, 16, 10, 30, tzinfo=timezone(timedelta(hours=-4)))
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        states, _ = store.record_attempts([attempt(observed_at=local_time)])
        state = next(iter(states.values()))
        assert state.last_attempt_at == NOW
        stored = store._conn.execute("select observed_at from source_health_attempts").fetchone()[0]
    assert stored == "2026-07-16T14:30:00+00:00"


def test_long_errors_and_sensitive_urls_are_bounded_and_sanitized(tmp_path):
    message = "token=supersecret https://user:pass@example.test/jobs?auth=private " + "x" * 1000
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        states, _ = store.record_attempts(
            [attempt(succeeded=False, rows=None, error_kind=ERROR_FETCH, error_message=message)]
        )
        state = next(iter(states.values()))
        stored = store._conn.execute("select error_message from source_health_attempts").fetchone()[0]
    assert stored == state.last_error_message
    assert len(stored) <= MAX_ERROR_LENGTH
    assert "supersecret" not in stored
    assert "private" not in stored
    assert "user:pass" not in stored


def test_feed_keys_and_labels_do_not_expose_query_strings():
    raw = "https://user:secret@example.test/listings.json?temporary_token=private#fragment"
    label = sanitize_feed_label(raw)
    key = github_feed_health_key(raw)
    assert label == "https://example.test/listings.json"
    assert "private" not in key
    assert "temporary_token" not in key
    assert "?" not in key


def test_transaction_failure_rolls_back_attempt_and_current_state(tmp_path):
    duplicate = attempt(run_id="same-run")
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.record_attempts([duplicate, duplicate])
        assert store.attempt_count() == 0
        assert store.current_state(duplicate.health_key) is None


def test_parameterized_values_accept_sql_metacharacters(tmp_path):
    quoted = attempt(company="O'Reilly; drop table seen; --", rows=1)
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        states, _ = store.record_attempts([quoted])
        assert next(iter(states.values())).company == "O'Reilly; drop table seen; --"
        assert store.attempt_count() == 1


def test_direct_success_and_zero_status_thresholds():
    healthy = next_state(None, rows=2)
    assert healthy.status == STATUS_HEALTHY
    first_zero_without_history = next_state(None, rows=0)
    second_zero_without_history = next_state(first_zero_without_history, run_id="run-2", rows=0)
    assert first_zero_without_history.status == STATUS_EMPTY
    assert second_zero_without_history.status == STATUS_EMPTY

    first_zero_after_nonzero = next_state(healthy, run_id="run-2", rows=0)
    second_zero_after_nonzero = next_state(first_zero_after_nonzero, run_id="run-3", rows=0)
    assert first_zero_after_nonzero.status == STATUS_EMPTY
    assert second_zero_after_nonzero.status == STATUS_DEGRADED


def test_direct_failure_threshold_and_nonzero_recovery():
    first = next_state(None, succeeded=False, rows=None, error_kind=ERROR_FETCH)
    second = next_state(first, run_id="run-2", succeeded=False, rows=None, error_kind=ERROR_FETCH)
    third = next_state(second, run_id="run-3", succeeded=False, rows=None, error_kind=ERROR_FETCH)
    recovered = next_state(third, run_id="run-4", rows=3)
    assert [first.status, second.status, third.status, recovered.status] == [
        STATUS_DEGRADED,
        STATUS_DEGRADED,
        STATUS_FAILING,
        STATUS_HEALTHY,
    ]
    transition = transition_for(third, recovered)
    assert transition.recovery is True


def test_failure_followed_by_zero_is_empty_recovery():
    failed = next_state(None, succeeded=False, rows=None, error_kind=ERROR_FETCH)
    responding = next_state(failed, run_id="run-2", rows=0)
    transition = transition_for(failed, responding)
    assert responding.status == STATUS_EMPTY
    assert transition.recovery is True


@pytest.mark.parametrize("adapter", ["bespoke", "github_only"])
def test_unsupported_does_not_accumulate_failures(adapter):
    unsupported_attempt = attempt(
        adapter=adapter,
        attempted=False,
        succeeded=None,
        rows=None,
        unsupported_reason=adapter,
    )
    first = calculate_next_state(None, unsupported_attempt)
    second = calculate_next_state(first, unsupported_attempt)
    assert second.status == STATUS_UNSUPPORTED
    assert second.total_attempts == 0
    assert second.consecutive_failures == 0


def test_github_zero_is_healthy_and_failure_threshold_and_recovery():
    healthy_zero = next_state(None, source_kind=SOURCE_KIND_GITHUB_FEED, rows=0)
    first = next_state(
        healthy_zero,
        run_id="run-2",
        source_kind=SOURCE_KIND_GITHUB_FEED,
        succeeded=False,
        rows=None,
        error_kind=ERROR_FETCH,
    )
    second = next_state(first, run_id="run-3", source_kind=SOURCE_KIND_GITHUB_FEED, succeeded=False, rows=None)
    third = next_state(second, run_id="run-4", source_kind=SOURCE_KIND_GITHUB_FEED, succeeded=False, rows=None)
    recovered = next_state(third, run_id="run-5", source_kind=SOURCE_KIND_GITHUB_FEED, rows=0)
    assert [healthy_zero.status, first.status, second.status, third.status, recovered.status] == [
        STATUS_HEALTHY,
        STATUS_DEGRADED,
        STATUS_DEGRADED,
        STATUS_FAILING,
        STATUS_HEALTHY,
    ]
    assert transition_for(third, recovered).recovery is True


def test_transitions_omit_initialization_and_unchanged_states():
    healthy = next_state(None, rows=1)
    failed = next_state(healthy, run_id="run-2", succeeded=False, rows=None)
    failed_again = next_state(failed, run_id="run-3", succeeded=False, rows=None)
    failing = next_state(failed_again, run_id="run-4", succeeded=False, rows=None)
    assert transition_for(None, healthy) is None
    assert transition_for(healthy, failed).to_status == STATUS_DEGRADED
    assert transition_for(failed, failed_again) is None
    assert transition_for(failed_again, failing).to_status == STATUS_FAILING


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
    success = attempt(rows=2)
    success_state = calculate_next_state(None, success)
    assert _coverage((direct_company,), success, success_state, False).state == COVERAGE_DIRECT

    zero = attempt(rows=0)
    zero_state = calculate_next_state(None, zero)
    assert _coverage((direct_company,), zero, zero_state, False).state == COVERAGE_DIRECT_EMPTY

    failed = attempt(succeeded=False, rows=None)
    degraded = calculate_next_state(None, failed)
    assert _coverage((direct_company,), failed, degraded, True).state == COVERAGE_DEGRADED_BACKSTOP
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


def test_health_summary_counts_current_states_coverage_and_transitions():
    companies = (CompanyCfg(name="Example Co", ats="greenhouse"),)
    direct = attempt(rows=1)
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


def test_json_report_is_sanitized_and_github_annotations_use_transitions(tmp_path):
    company = CompanyCfg(name="Example Co", ats="greenhouse")
    healthy_attempt = attempt(run_id="run-1", rows=1)
    healthy = calculate_next_state(None, healthy_attempt)
    failed_attempt = attempt(
        run_id="run-2",
        succeeded=False,
        rows=None,
        error_kind=ERROR_FETCH,
        error_message="HTTP 503 https://example.test/jobs?token=secret",
    )
    degraded = calculate_next_state(healthy, failed_attempt)
    transition = transition_for(healthy, degraded)
    coverage = (
        CompanyCoverage(
            company=company.name,
            adapter=company.ats,
            state=COVERAGE_UNCOVERED,
            direct_status=degraded.status,
            direct_attempt_succeeded=False,
            direct_rows_returned=None,
            github_backstop_available=False,
        ),
    )
    summary = HealthSummary(
        companies_configured=1,
        direct_attempts=1,
        direct_successes=0,
        direct_zero_successes=0,
        direct_failures=1,
        direct_healthy=0,
        direct_empty=0,
        direct_degraded=1,
        direct_failing=0,
        direct_unsupported=0,
        direct_unknown=0,
        github_feeds_configured=0,
        github_feeds_healthy=0,
        github_feeds_degraded=0,
        github_feeds_failing=0,
        backstop_only_companies=0,
        uncovered_companies=1,
        health_transitions=1,
        health_recoveries=0,
    )
    report = tmp_path / "health.json"
    write_health_report(
        report,
        run_id="fixed-run",
        observed_at=NOW,
        attempts=(failed_attempt,),
        states={degraded.health_key: degraded},
        transitions=(transition,),
        coverage=coverage,
        summary=summary,
        run_metadata={"configured_terms": "Summer_2027", "season_status": "ok"},
    )
    raw = report.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["run_id"] == "fixed-run"
    assert data["summary"]["direct_degraded"] == 1
    assert "secret" not in raw

    output = io.StringIO()
    summary_path = tmp_path / "summary.md"
    render_github_actions_report(report, summary_path=summary_path, output=output)
    annotations = output.getvalue()
    assert "::warning::SOURCE HEALTH: Example Co: healthy -> degraded" in annotations
    assert "::error::SOURCE COVERAGE: Example Co was uncovered" in annotations
    assert "Internship watcher run" in summary_path.read_text(encoding="utf-8")


def test_sanitizers_are_deterministic():
    assert sanitize_error("HTTP https://example.test/a?x=1") == "HTTP https://example.test/a"
    assert github_feed_health_key("https://example.test/a?x=1") == github_feed_health_key(
        "https://example.test/a?x=2"
    )
