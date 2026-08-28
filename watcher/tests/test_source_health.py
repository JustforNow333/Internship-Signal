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
    GITHUB_PRIMARY_ATS,
    MAX_ERROR_LENGTH,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_DEGRADED,
    STATUS_EMPTY,
    STATUS_FAILING,
    STATUS_HEALTHY,
    STATUS_UNSUPPORTED,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_NOT_CONFIGURED,
    DIRECT_STATUS_UNKNOWN,
    CompanyCoverage,
    HealthSummary,
    SourceAttempt,
    SourceHealthStore,
    calculate_company_coverage,
    calculate_next_state,
    count_github_rows_by_company,
    direct_health_key,
    github_feed_health_key,
    render_final_heartbeat,
    render_github_actions_report,
    safe_error_kind,
    safe_run_id,
    safe_token,
    sanitize_error,
    sanitize_feed_label,
    sanitize_plain,
    summarize_health,
    transition_for,
    write_health_report,
)
from watcher.sources.base import make_row

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
    **diagnostics,
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
        **diagnostics,
    )


def next_state(previous, **kwargs):
    return calculate_next_state(previous, attempt(**kwargs))


def diagnostic_attempt(*, rows=1, succeeded=True, **overrides):
    values = {
        "run_id": "diagnostic-run",
        "rows": rows,
        "succeeded": succeeded,
        "malformed_row_count": 0,
        "schema_error_row_count": 0,
        "duplicate_row_count": 0,
        "failed_request_count": 0,
        "incomplete": False,
        "truncated": False,
        "reason_codes": (),
        "degraded": False,
        "complete": True,
    }
    values.update(overrides)
    return attempt(**values)


def test_direct_diagnostic_states_are_per_attempt_and_listing_count_is_separate():
    listed = calculate_next_state(None, diagnostic_attempt(rows=2))
    empty = calculate_next_state(None, diagnostic_attempt(rows=0))
    degraded = calculate_next_state(
        None,
        diagnostic_attempt(
            rows=1,
            malformed_row_count=1,
            reason_codes=("malformed_records_skipped",),
            degraded=True,
            complete=False,
        ),
    )
    failed = calculate_next_state(
        None,
        diagnostic_attempt(
            rows=None,
            succeeded=False,
            failed_request_count=1,
            reason_codes=("fetch_failure",),
            complete=False,
        ),
    )
    not_configured = calculate_next_state(
        None,
        attempt(
            attempted=False,
            succeeded=None,
            rows=None,
            adapter="github_only",
            unsupported_reason="github_only",
        ),
    )

    assert listed.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert empty.status == DIRECT_STATUS_HEALTHY_EMPTY
    assert degraded.status == DIRECT_STATUS_DEGRADED
    assert failed.status == DIRECT_STATUS_FAILED
    assert not_configured.status == DIRECT_STATUS_NOT_CONFIGURED


def test_direct_success_without_sufficient_diagnostics_is_unknown():
    state = calculate_next_state(None, attempt(rows=4))
    assert state.status == DIRECT_STATUS_UNKNOWN


def test_duplicates_and_optional_enrichment_failure_do_not_force_degradation():
    duplicates = calculate_next_state(
        None,
        diagnostic_attempt(rows=2, duplicate_row_count=3),
    )
    optional_enrichment = calculate_next_state(
        None,
        diagnostic_attempt(
            rows=2,
            failed_request_count=1,
            reason_codes=("optional_enrichment_failed",),
        ),
    )
    material_enrichment = calculate_next_state(
        None,
        diagnostic_attempt(
            rows=2,
            failed_request_count=1,
            reason_codes=("material_enrichment_failed",),
            degraded=True,
            complete=False,
        ),
    )

    assert duplicates.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert optional_enrichment.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert material_enrichment.status == DIRECT_STATUS_DEGRADED


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


def test_transport_subtype_is_persisted_without_raw_html_or_metadata(tmp_path):
    raw_marker = "<html><input value=PRIVATE_CHALLENGE_TOKEN></html>"
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        states, _ = store.record_attempts(
            [
                attempt(
                    succeeded=False,
                    rows=None,
                    error_kind="fetch_failure/html_challenge",
                    error_message="SourceFetchError: workday non-JSON code=html_challenge",
                )
            ]
        )
        state = next(iter(states.values()))
        stored = store._conn.execute(
            "select error_kind, error_message from source_health_attempts"
        ).fetchone()

    assert stored[0] == "fetch_failure/html_challenge"
    assert state.last_error_kind == "fetch_failure/html_challenge"
    assert state.status == DIRECT_STATUS_FAILED
    assert state.consecutive_failures == 1
    assert raw_marker not in stored[1]
    assert "PRIVATE_CHALLENGE_TOKEN" not in stored[1]


def test_feed_keys_and_labels_do_not_expose_query_strings():
    raw = "https://user:secret@example.test/listings.json?temporary_token=private#fragment"
    label = sanitize_feed_label(raw)
    key = github_feed_health_key(raw)
    assert label == "https://example.test/listings.json"
    assert "private" not in key
    assert "temporary_token" not in key
    assert "?" not in key


@pytest.mark.parametrize(
    "raw, expected",
    (
        ("https://example.test:99999/listings.json", "https://example.test/listings.json"),
        ("https://example.test:port/listings.json", "https://example.test/listings.json"),
        ("https://user:secret@example.test:99999/x?t=1", "https://example.test/x"),
        ("https://[::1]:70000/listings.json", "https://::1/listings.json"),
        ("http://[unterminated/listings.json?t=1", "http://[unterminated/listings.json"),
    ),
)
def test_feed_labels_sanitize_malformed_authorities_without_raising(raw, expected):
    assert sanitize_feed_label(raw) == expected
    assert "secret" not in sanitize_feed_label(raw)


def test_sanitize_error_survives_a_malformed_url_in_failure_text():
    message = sanitize_error("workday POST failed: https://tenant.test:99999/wday/cxs/jobs?q=1")
    assert message == "workday POST failed: https://tenant.test/wday/cxs/jobs"


def test_health_sanitizers_are_total_for_unprintable_failure_values():
    class Unprintable:
        def __bool__(self):
            raise RuntimeError("broken truth conversion")

        def __str__(self):
            raise RuntimeError("broken text conversion")

    value = Unprintable()

    assert sanitize_error(value) == ""
    assert sanitize_feed_label(value) == "injected"
    assert safe_token(value) == ""
    assert safe_error_kind(value) == ""
    assert safe_run_id(value) == "unknown"
    assert sanitize_plain(value) == ""


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


def test_direct_success_and_zero_status_do_not_depend_on_history():
    healthy = calculate_next_state(None, diagnostic_attempt(rows=2))
    assert healthy.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    first_zero = calculate_next_state(None, diagnostic_attempt(rows=0))
    second_zero = calculate_next_state(
        first_zero, diagnostic_attempt(run_id="run-2", rows=0)
    )
    zero_after_nonzero = calculate_next_state(
        healthy, diagnostic_attempt(run_id="run-3", rows=0)
    )
    assert first_zero.status == DIRECT_STATUS_HEALTHY_EMPTY
    assert second_zero.status == DIRECT_STATUS_HEALTHY_EMPTY
    assert zero_after_nonzero.status == DIRECT_STATUS_HEALTHY_EMPTY


def test_direct_failure_is_immediate_and_nonzero_recovery():
    first = next_state(None, succeeded=False, rows=None, error_kind=ERROR_FETCH)
    second = next_state(first, run_id="run-2", succeeded=False, rows=None, error_kind=ERROR_FETCH)
    third = next_state(second, run_id="run-3", succeeded=False, rows=None, error_kind=ERROR_FETCH)
    recovered = calculate_next_state(
        third, diagnostic_attempt(run_id="run-4", rows=3)
    )
    assert [first.status, second.status, third.status, recovered.status] == [
        DIRECT_STATUS_FAILED,
        DIRECT_STATUS_FAILED,
        DIRECT_STATUS_FAILED,
        DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    ]
    transition = transition_for(third, recovered)
    assert transition.recovery is True


def test_failure_followed_by_zero_is_empty_recovery():
    failed = next_state(None, succeeded=False, rows=None, error_kind=ERROR_FETCH)
    responding = calculate_next_state(
        failed, diagnostic_attempt(run_id="run-2", rows=0)
    )
    transition = transition_for(failed, responding)
    assert responding.status == DIRECT_STATUS_HEALTHY_EMPTY
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
    assert second.status == DIRECT_STATUS_NOT_CONFIGURED
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
    healthy = calculate_next_state(None, diagnostic_attempt(rows=1))
    failed = next_state(healthy, run_id="run-2", succeeded=False, rows=None)
    failed_again = next_state(failed, run_id="run-3", succeeded=False, rows=None)
    failing = next_state(failed_again, run_id="run-4", succeeded=False, rows=None)
    assert transition_for(None, healthy) is None
    assert transition_for(healthy, failed).to_status == DIRECT_STATUS_FAILED
    assert transition_for(failed, failed_again) is None
    assert transition_for(failed_again, failing) is None


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


def test_github_row_counts_map_names_and_aliases_to_configured_companies():
    companies = (
        CompanyCfg(name="Example Co", ats="greenhouse", aliases=("Example Corp",)),
        CompanyCfg(name="Other Co", ats="workday"),
    )
    rows = [
        make_row(source="github", source_adapter="simplify_json", company="Example Co"),
        make_row(source="github", source_adapter="simplify_json", company="Example Corp"),
        make_row(source="direct", source_adapter="greenhouse", company="Example Co"),
        make_row(source="github", source_adapter="simplify_json", company="Unlisted Inc"),
    ]

    assert count_github_rows_by_company(rows, companies) == {
        "Example Co": 2,
        "Other Co": 0,
    }


def test_github_row_counts_ignore_untrusted_source_metadata():
    companies = (CompanyCfg(name="Example Co", ats="greenhouse"),)
    rows = [
        {"company": "Example Co", "extra": None},
        {"company": "Example Co"},
        {"company": "", "extra": {"source": "github"}},
    ]

    assert count_github_rows_by_company(rows, companies) == {"Example Co": 0}


def test_coverage_tracks_company_rows_and_fallback_configuration():
    company = CompanyCfg(name="Example Co", ats="greenhouse")
    failed = attempt(succeeded=False, rows=None)
    github = attempt(source_kind=SOURCE_KIND_GITHUB_FEED, rows=7)
    states = {
        failed.health_key: calculate_next_state(None, failed),
        github.health_key: calculate_next_state(None, github),
    }

    covered = calculate_company_coverage(
        (company,),
        [failed, github],
        states,
        {"Example Co": 3},
    )[0]
    assert covered.github_rows_returned == 3
    assert covered.github_fallback_configured is True
    assert covered.github_backstop_available is True

    unthreaded = calculate_company_coverage((company,), [failed, github], states)[0]
    assert unthreaded.github_rows_returned is None
    assert unthreaded.github_fallback_configured is True


def test_one_failed_github_feed_withdraws_backstop_for_the_run():
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

    partial = calculate_company_coverage(
        (company,), [failed, feed_a, feed_b], states
    )[0]
    assert partial.github_backstop_available is False
    assert partial.github_fallback_configured is False
    assert partial.state == COVERAGE_UNCOVERED

    whole = calculate_company_coverage((company,), [failed, feed_b], states)[0]
    assert whole.github_backstop_available is True
    assert whole.github_fallback_configured is True
    assert whole.state == COVERAGE_FAILING_BACKSTOP


@pytest.mark.parametrize("adapter", sorted(GITHUB_PRIMARY_ATS))
def test_github_primary_modes_never_report_a_separate_fallback(adapter):
    company = CompanyCfg(name="Example Co", ats=adapter)
    unsupported = attempt(
        adapter=adapter,
        attempted=False,
        succeeded=None,
        rows=None,
        unsupported_reason=adapter,
    )
    github = attempt(source_kind=SOURCE_KIND_GITHUB_FEED, rows=9)
    states = {
        unsupported.health_key: calculate_next_state(None, unsupported),
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


def test_json_report_is_sanitized_and_github_annotations_use_transitions(tmp_path):
    company = CompanyCfg(name="Example Co", ats="greenhouse")
    healthy_attempt = diagnostic_attempt(run_id="run-1", rows=1)
    healthy = calculate_next_state(None, healthy_attempt)
    failed_attempt = attempt(
        run_id="run-2",
        succeeded=False,
        rows=None,
        error_kind=ERROR_FETCH,
        error_message="HTTP 503 https://example.test/jobs?token=secret",
    )
    failed = calculate_next_state(healthy, failed_attempt)
    transition = transition_for(healthy, failed)
    coverage = (
        CompanyCoverage(
            company=company.name,
            adapter=company.ats,
            state=COVERAGE_UNCOVERED,
            direct_status=failed.status,
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
        direct_degraded=0,
        direct_failing=1,
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
        direct_failed=1,
    )
    report = tmp_path / "health.json"
    write_health_report(
        report,
        run_id="fixed-run",
        observed_at=NOW,
        attempts=(failed_attempt,),
        states={failed.health_key: failed},
        transitions=(transition,),
        coverage=coverage,
        summary=summary,
        run_metadata={
            "configured_terms": "Summer_2027",
            "season_status": "ok",
            "workday_transport": {
                "attempted_tenants": 59,
                "successful_tenants": 35,
                "failed_tenants": 24,
                "retry_attempts": 48,
                "dominant_error": "html_challenge",
                "dominant_error_count": 24,
                "likely_shared_incident": True,
            },
        },
    )
    raw = report.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["run_id"] == "fixed-run"
    assert data["summary"]["direct_failed"] == 1
    assert data["run"]["workday_transport"]["dominant_error"] == "html_challenge"
    assert data["run"]["workday_transport"]["failed_tenants"] == 24
    assert "secret" not in raw

    output = io.StringIO()
    summary_path = tmp_path / "summary.md"
    render_github_actions_report(report, summary_path=summary_path, output=output)
    annotations = output.getvalue()
    assert "::warning::WORKDAY TRANSPORT INCIDENT:" in annotations
    assert "failed=24" in annotations
    assert "dominant_error=html_challenge" in annotations
    assert (
        "::warning::SOURCE HEALTH: Example Co: "
        "healthy_with_listings -> failed"
    ) in annotations
    assert "::error::SOURCE COVERAGE: Example Co was uncovered" in annotations
    assert "Internship watcher run" in summary_path.read_text(encoding="utf-8")
    assert "Likely shared incident: `yes`" in summary_path.read_text(encoding="utf-8")


def test_sanitizers_are_deterministic():
    assert sanitize_error("HTTP https://example.test/a?x=1") == "HTTP https://example.test/a"
    assert github_feed_health_key("https://example.test/a?x=1") == github_feed_health_key(
        "https://example.test/a?x=2"
    )


def test_final_heartbeat_forwards_every_application_field_and_appends_persistence():
    application = (
        "HEARTBEAT: ran, rows_fetched=16295, jobs_scored=15121, matches=68, new=0, errors=1, "
        "season_status=ok, configured_terms=Summer_2027, github_feeds_configured=1, "
        "github_feeds_succeeded=1, companies_configured=129, direct_healthy=57, direct_empty=1, "
        "direct_degraded=1, direct_failing=0, direct_unsupported=70, github_feeds_healthy=1, "
        "backstop_only_companies=70, uncovered_companies=0, health_transitions=0, "
        "health_recoveries=0, alumni_csv_status=loaded-json-map, alumni_records_loaded=150, "
        "alumni_employers_indexed=128, sent=no, seen_marked=0, future_metric=123"
    )

    final = render_final_heartbeat(
        application,
        seen_loaded=70,
        seen_saved=70,
        load_status="loaded",
        save_status="pushed",
        scheduled_email_enabled="no",
        pending_due_to_email_disabled=1,
        scheduled_email_config="recognized_false",
    )

    assert final.startswith(application)
    assert "future_metric=123" in final
    assert "season_status=ok" in final
    assert "github_feeds_succeeded=1" in final
    assert "direct_degraded=1" in final
    assert "alumni_records_loaded=150" in final
    assert "sent=no, seen_marked=0" in final
    assert "scheduled_email_enabled=no" in final
    assert "pending_due_to_email_disabled=1" in final
    assert "scheduled_email_config=recognized_false" in final
    assert final.endswith("seen_loaded=70, seen_saved=70, seen_store=loaded/pushed")
    assert "\n" not in final
    assert "\r" not in final


def test_final_heartbeat_represents_unknown_save_state_honestly():
    final = render_final_heartbeat(
        "HEARTBEAT: ran, errors=0",
        seen_loaded="unknown",
        seen_saved="unknown",
        load_status="new",
        save_status="skipped-or-failed",
    )

    assert final.endswith(
        "seen_loaded=unknown, seen_saved=unknown, seen_store=new/skipped-or-failed"
    )


@pytest.mark.parametrize(
    "application",
    ("", "ran, errors=0", "HEARTBEAT: ran, errors=0\nHEARTBEAT: injected"),
)
def test_final_heartbeat_rejects_missing_or_multiline_application_value(application):
    with pytest.raises(ValueError):
        render_final_heartbeat(application)


def _actionable_detail_rows(summary_text: str) -> list[list[str]]:
    """Return the parsed cells of the 'Actionable source details' table."""

    lines = summary_text.splitlines()
    start = lines.index("### Actionable source details")
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] in {"Category", "---"} or set(cells[0]) == {"-"}:
            continue
        rows.append(cells)
    return rows


def test_actionable_rows_render_company_not_diagnostic_label(tmp_path):
    """Regression: run 20260810T031240Z-e85ea6464f9f rendered a degraded Merck
    Workday source with `failed_requests` in the Company/feed column, because
    the diagnostic loop variable shadowed the source label."""

    summary_path = tmp_path / "summary.md"
    payload = {
        "run_id": "20260810T031240Z-e85ea6464f9f",
        "run": {},
        "summary": {},
        "states": [
            {
                "status": DIRECT_STATUS_DEGRADED,
                "company": "Merck",
                "adapter": "workday",
                "health_key": "company:merck:direct:workday",
                "source_kind": SOURCE_KIND_DIRECT,
                "last_rows_returned": 818,
                "last_malformed_row_count": 0,
                "last_schema_error_row_count": 2,
                "last_duplicate_row_count": 0,
                "last_failed_request_count": 0,
                "last_reason_codes": ["schema_invalid_records_skipped"],
            }
        ],
        "transitions": [],
        "coverage": [],
    }

    report_path = tmp_path / "health.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    render_github_actions_report(
        report_path, output=io.StringIO(), summary_path=summary_path
    )

    rows = _actionable_detail_rows(summary_path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    category, label, adapter, detail = rows[0]
    assert category == DIRECT_STATUS_DEGRADED
    assert label == "Merck"
    assert adapter == "workday"
    assert "rows=818" in detail
    assert "schema=2" in detail
    assert "reasons=schema_invalid_records_skipped" in detail


def test_actionable_rows_render_degraded_failed_and_recovered_labels(tmp_path):
    """Degraded, failed, recovered, and uncovered rows all keep their own
    company/feed label and adapter."""

    summary_path = tmp_path / "summary.md"
    payload = {
        "run_id": "run",
        "run": {},
        "summary": {},
        "states": [
            {
                "status": DIRECT_STATUS_DEGRADED,
                "company": "Merck",
                "adapter": "workday",
                "last_rows_returned": 818,
                "last_schema_error_row_count": 2,
            },
            {
                "status": DIRECT_STATUS_FAILED,
                "company": "Adobe",
                "adapter": "workday",
                "last_error_kind": "fetch_failure/redirected_to_html",
                "last_error_message": "workday returned non-JSON content",
                "last_failed_request_count": 1,
            },
            {
                "status": STATUS_FAILING,
                "feed_label": "github-internships",
                "adapter": "github_markdown_table",
                "last_rows_returned": 0,
            },
        ],
        "transitions": [
            {
                "recovery": True,
                "company": "Workday",
                "adapter": "workday",
                "from_status": DIRECT_STATUS_FAILED,
                "to_status": DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            }
        ],
        "coverage": [
            {
                "state": COVERAGE_UNCOVERED,
                "company": "Blackstone",
                "adapter": "workday",
            }
        ],
    }

    report_path = tmp_path / "health.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    render_github_actions_report(
        report_path, output=io.StringIO(), summary_path=summary_path
    )

    rows = _actionable_detail_rows(summary_path.read_text(encoding="utf-8"))
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (DIRECT_STATUS_DEGRADED, "Merck", "workday"),
        (DIRECT_STATUS_FAILED, "Adobe", "workday"),
        (STATUS_FAILING, "github-internships", "github_markdown_table"),
        ("recovered", "Workday", "workday"),
        ("uncovered", "Blackstone", "workday"),
    ]
    # No row may carry a diagnostic key where the label belongs.
    assert not any(
        row[1] in {"malformed", "schema", "duplicates", "failed_requests"} for row in rows
    )
