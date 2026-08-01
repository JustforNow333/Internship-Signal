"""Offline coverage for the staged collection-concurrency canary harness."""

import os
from datetime import datetime, timezone

import pytest

# The canary module deliberately pins safe environment defaults at import time
# so no watcher default can ever resolve to production state. Restore the
# process environment afterwards so importing it cannot leak into other tests.
_ENV_KEYS = (
    "WATCHER_SEND_EMAIL",
    "WATCHER_HEALTH_EMAIL_MODE",
    "WATCHER_PRIME_SEEN",
    "WATCHER_SEEN_DB",
    "WATCHER_ANALYSIS_CACHE_PATH",
)
_SAVED_ENV = {key: os.environ.get(key) for key in _ENV_KEYS}

from scripts.canary_collection_concurrency import (  # noqa: E402
    classify_attempt,
    fingerprint_production_state,
    select_companies,
    summarize_run,
)

for _key, _value in _SAVED_ENV.items():
    if _value is None:
        os.environ.pop(_key, None)
    else:
        os.environ[_key] = _value

from watcher.collection_concurrency import CollectionConcurrencyMetrics  # noqa: E402
from watcher.collection_snapshot import CollectionBatch  # noqa: E402
from watcher.config import (  # noqa: E402
    COLLECTION_MODE_CONCURRENT,
    CollectionConcurrencyCfg,
    CompanyCfg,
    WatcherConfig,
)
from watcher.run import CollectionStats  # noqa: E402
from watcher.source_health import (  # noqa: E402
    SOURCE_KIND_DIRECT,
    SourceAttempt,
)
from watcher.sources.workday import (  # noqa: E402
    WorkdayStartRecord,
    summarize_workday_starts,
)

OBSERVED_AT = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def attempt(
    company,
    *,
    adapter="greenhouse",
    succeeded=True,
    rows=3,
    error_kind=None,
    error_message=None,
):
    return SourceAttempt(
        health_key=f"key-{company}",
        run_id="canary-run",
        observed_at=OBSERVED_AT,
        source_kind=SOURCE_KIND_DIRECT,
        company=company,
        adapter=adapter,
        attempted=True,
        succeeded=succeeded,
        rows_returned=rows if succeeded else None,
        error_kind=error_kind,
        error_message=error_message,
    )


def config(*companies):
    return WatcherConfig(
        companies=companies,
        terms=("Summer 2027",),
        collection_concurrency=CollectionConcurrencyCfg(
            mode=COLLECTION_MODE_CONCURRENT
        ),
    )


def test_limited_stage_samples_one_source_per_adapter_and_caps_workday():
    selected = select_companies(
        config(
            CompanyCfg(name="GreenA", ats="greenhouse", token="a"),
            CompanyCfg(name="GreenB", ats="greenhouse", token="b"),
            CompanyCfg(name="LeverA", ats="lever", token="c"),
            CompanyCfg(
                name="WorkdayA", ats="workday", token="d", workday_shard="wd5",
                workday_site="Site",
            ),
            CompanyCfg(
                name="WorkdayB", ats="workday", token="e", workday_shard="wd5",
                workday_site="Site",
            ),
            CompanyCfg(name="BespokeCo", ats="bespoke"),
            CompanyCfg(name="BackstopCo", ats="github_only"),
        ),
        stage="limited",
        allowlist=(),
        max_workday=1,
        max_sources=6,
        blocked=set(),
    )

    assert [company.name for company in selected] == ["GreenA", "LeverA", "WorkdayA"]


def test_blocked_sources_are_excluded_from_later_canary_runs():
    selected = select_companies(
        config(
            CompanyCfg(name="GreenA", ats="greenhouse", token="a"),
            CompanyCfg(name="LeverA", ats="lever", token="b"),
        ),
        stage="full",
        allowlist=(),
        max_workday=5,
        max_sources=None,
        blocked={"GreenA"},
    )

    assert [company.name for company in selected] == ["LeverA"]


def test_unknown_allowlist_entries_fail_loudly():
    with pytest.raises(SystemExit, match="not fetchable configured companies"):
        select_companies(
            config(CompanyCfg(name="GreenA", ats="greenhouse", token="a")),
            stage="limited",
            allowlist=("Missing Co",),
            max_workday=1,
            max_sources=6,
            blocked=set(),
        )


@pytest.mark.parametrize(
    "error_kind, message, expected_status, expected_reason",
    [
        ("fetch/rate_limited", "SourceFetchError: HTTP 429", 429, "http_429"),
        ("fetch", "SourceFetchError: fetch failed with HTTP 401", 401, "http_401"),
        ("fetch", "SourceFetchError: fetch failed with HTTP 403", 403, "http_403"),
        ("fetch/html_challenge", "SourceFetchError: code=html_challenge", None, "html_challenge"),
        ("fetch/network_failure", "SourceFetchError: code=network_failure", None, ""),
    ],
)
def test_blocked_responses_are_recorded_not_retried(
    error_kind, message, expected_status, expected_reason
):
    classified = classify_attempt(
        attempt(
            "BlockedCo",
            succeeded=False,
            error_kind=error_kind,
            error_message=message,
        )
    )

    assert classified["outcome"] == "failure"
    assert classified["status"] == expected_status
    assert classified["blocked_reason"] == expected_reason


def test_transport_failures_are_paused_but_not_treated_as_blocking():
    classified = classify_attempt(
        attempt(
            "FlakyCo",
            succeeded=False,
            error_kind="fetch/network_failure",
            error_message="SourceFetchError: code=network_failure",
        )
    )

    assert classified["transport_failure"] is True
    assert classified["blocked_reason"] == ""
    assert classified["challenge"] is False


def test_summary_reports_every_required_canary_field():
    batch = CollectionBatch.create(
        captured_at=OBSERVED_AT,
        collection_config_fingerprint="f" * 64,
        rows=[],
        errors=["BlockedCo: rate limited"],
        source_attempts=[
            attempt("HealthyCo", rows=12),
            attempt("EmptyCo", rows=0),
            attempt(
                "BlockedCo",
                succeeded=False,
                error_kind="fetch/rate_limited",
                error_message="SourceFetchError: HTTP 429",
            ),
        ],
        github_feeds_configured=2,
        github_feeds_succeeded=2,
        workday_attempted=3,
        workday_succeeded=1,
        workday_failed=2,
        workday_request_attempts=9,
        workday_retry_attempts=4,
        workday_failure_codes={"network_failure": 2},
    )
    stats = CollectionStats()
    stats.collection_concurrency = CollectionConcurrencyMetrics(
        mode=COLLECTION_MODE_CONCURRENT,
        max_workers=4,
        per_origin_limit=2,
        workday_limit=1,
        max_observed_global=4,
        max_observed_per_origin=2,
        max_observed_provider=2,
        max_observed_workday=1,
    )
    stats.workday_start_telemetry = summarize_workday_starts(
        0.5,
        (
            WorkdayStartRecord("Workday One", 4.0),
            WorkdayStartRecord("Workday Two", 4.6),
            WorkdayStartRecord("Workday Three", 5.2),
        ),
    )

    summary = summarize_run(batch, stats, 12.5)

    assert summary["sources_attempted"] == 3
    assert summary["sources_successful"] == 1
    assert summary["sources_empty"] == 1
    assert summary["sources_failed"] == 1
    assert summary["http_429"] == 1
    assert summary["http_401"] == 0
    assert summary["http_403"] == 0
    assert summary["challenge_responses"] == 0
    assert summary["workday_request_attempts"] == 9
    assert summary["workday_retry_attempts"] == 4
    assert summary["rows_by_source"] == {"HealthyCo": 12, "EmptyCo": 0, "BlockedCo": 0}
    assert summary["unexpected_exceptions"] == 0
    assert summary["observed_concurrency"] == {
        "max_global": 4,
        "max_per_origin": 2,
        "max_provider": 2,
        "max_workday": 1,
        "busiest_origin": "",
    }
    assert summary["limits_within_bounds"] is True
    assert summary["workday_start_telemetry"] == {
        "configured_start_interval_seconds": 0.5,
        "start_count": 3,
        "minimum_spacing_seconds": 0.6,
        "median_spacing_seconds": 0.6,
        "maximum_spacing_seconds": 0.6,
        "pacing_violation_count": 0,
        "start_offsets": [
            {
                "task_identifier": "workday-start-001",
                "company_identifier": "Workday One",
                "offset_seconds": 0.0,
            },
            {
                "task_identifier": "workday-start-002",
                "company_identifier": "Workday Two",
                "offset_seconds": 0.6,
            },
            {
                "task_identifier": "workday-start-003",
                "company_identifier": "Workday Three",
                "offset_seconds": 1.2,
            },
        ],
    }
    assert summary["blocked_sources"] == [
        {"label": "BlockedCo", "reason": "http_429"}
    ]
    assert summary["paused_sources"] == []


def test_escaped_worker_exceptions_are_counted_as_unexpected():
    batch = CollectionBatch.create(
        captured_at=OBSERVED_AT,
        collection_config_fingerprint="f" * 64,
        rows=[],
        errors=[],
        source_attempts=[
            attempt(
                "BuggyCo",
                succeeded=False,
                error_kind="unexpected_exception",
                error_message="TypeError: bad argument",
            )
        ],
    )
    stats = CollectionStats()
    stats.unexpected_task_exceptions = 1

    summary = summarize_run(batch, stats, 1.0)

    assert summary["unexpected_exceptions"] == 1
    assert summary["escaped_worker_exceptions"] == 1


def test_production_state_fingerprints_are_size_and_digest_only():
    fingerprints = fingerprint_production_state()

    assert fingerprints
    for path, value in fingerprints.items():
        assert path.endswith(".sqlite")
        assert value == "absent" or ":" in value
