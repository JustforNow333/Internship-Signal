"""Collection mechanics: adapter resolution, fetch outcomes, and counters."""

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from watcher import collection as collection_module
from watcher.collection import (
    CollectionStats,
    WorkdayTransportSummary,
    _direct_diagnostics_from_source,
    collect_rows,
    summarize_workday_transport,
)
from watcher.config import CompanyCfg, GitHubListingSourceCfg, WatcherConfig
from watcher.source_health import (
    ERROR_MISSING_ADAPTER,
    ERROR_SCHEMA,
    ERROR_SOURCE,
    ERROR_UNEXPECTED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
)
from watcher.sources.base import (
    DirectSourceDiagnostics,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
)
from watcher.sources.registry import DIRECT_ATS
from watcher.tests.run_helpers import (
    CountingGithub,
    DiagnosticFakeSource,
    FakeGithub,
    FakeSource,
    row,
)

# Provider-specific diagnostic members that belong to one adapter's own
# dataclass. Collection must never read any of them: the adapter translates
# them into `DirectSourceDiagnostics` itself.
ADAPTER_DIAGNOSTIC_MEMBERS = frozenset(
    {
        "detail_enrichment_degraded",
        "detail_failure_reasons",
        "detail_failures",
        "disappeared_postings",
        "duplicate_postings_skipped",
        "listing_incomplete",
        "listing_incomplete_reasons",
        "listing_request_failures",
        "malformed_postings_skipped",
        "schema_error_postings_skipped",
        "skip_reasons",
    }
)


def test_direct_diagnostics_use_the_contract_the_adapter_published():
    published = DirectSourceDiagnostics(
        succeeded=True,
        retained_row_count=4,
        duplicate_row_count=1,
        failed_request_count=2,
        reason_codes=("request_retry_recovered",),
        degraded=True,
        complete=True,
    )
    source = SimpleNamespace(
        name="workday",
        last_health_diagnostics=published,
        # An adapter-owned dataclass collection must not interpret.
        last_diagnostics=SimpleNamespace(retry_attempts=9),
    )

    assert (
        _direct_diagnostics_from_source(source, succeeded=True, error_kind="")
        is published
    )


def test_direct_diagnostics_report_nothing_without_the_shared_contract():
    """A legacy or injected source that publishes nothing reports nothing."""

    source = SimpleNamespace(
        name="talentbrew",
        last_diagnostics=SimpleNamespace(
            retry_attempts=1,
            duplicate_postings_skipped=2,
        ),
    )

    assert (
        _direct_diagnostics_from_source(source, succeeded=True, error_kind="")
        is None
    )


def test_failed_direct_fetch_reports_the_shared_failure_contract():
    diagnostics = _direct_diagnostics_from_source(
        FakeSource(),
        succeeded=False,
        error_kind=ERROR_SCHEMA,
    )

    assert diagnostics.succeeded is False
    assert diagnostics.failed_request_count == 1
    assert diagnostics.incomplete is True
    assert diagnostics.complete is False
    assert diagnostics.reason_codes == (ERROR_SCHEMA,)


def test_unclassified_failure_still_reports_a_reason_code():
    diagnostics = _direct_diagnostics_from_source(
        FakeSource(),
        succeeded=False,
        error_kind="",
    )

    assert diagnostics.reason_codes == (ERROR_SOURCE,)


def test_collection_holds_no_adapter_specific_diagnostic_translation():
    """The diagnostics seam must stay free of adapter names and internals."""

    translation = inspect.getsource(_direct_diagnostics_from_source)
    for ats in DIRECT_ATS:
        assert f'"{ats}"' not in translation
        assert f"'{ats}'" not in translation
    for member in ADAPTER_DIAGNOSTIC_MEMBERS:
        assert member not in translation

    tree = ast.parse(
        Path(collection_module.__file__).read_text(encoding="utf-8")
    )
    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            referenced.add(node.attr)
        elif isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            referenced.add(node.value)
        elif isinstance(node, ast.Call):
            referenced.update(keyword.arg for keyword in node.keywords)
    assert not (referenced & ADAPTER_DIAGNOSTIC_MEMBERS)


def test_collect_rows_logs_source_failure_and_keeps_going():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="BrokenCo", ats="greenhouse", token="broken"),
            CompanyCfg(name="GitHub", ats="github_only"),
        )
    )
    github_rows = [row("GitHub", "Software Engineering Intern", source="github")]

    rows, errors = collect_rows(
        config,
        direct_sources={"greenhouse": FakeSource(error=SourceError("boom"))},
        github_source=FakeGithub(github_rows),
    )

    assert rows == github_rows
    assert errors == ["BrokenCo: boom"]


def test_collect_rows_logs_successful_and_failed_source_timings(caplog):
    caplog.set_level("INFO", logger="watcher.run")
    successful_github = GitHubListingSourceCfg(
        name="safe_feed",
        format="simplify_json",
        url="https://example.test/safe.json",
    )
    failed_github = GitHubListingSourceCfg(
        name="failed_feed",
        format="github_markdown_table",
        url="https://example.test/failed.md",
        default_term="Summer 2027",
    )
    direct_row = row("Timed Direct", "Software Engineer Intern")
    github_row_value = row(
        "Timed Direct",
        "Backend Intern",
        source="github",
    )
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="Timed Direct", ats="workday", token="timed"),
            CompanyCfg(name="Broken Direct", ats="greenhouse", token="broken"),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(successful_github, failed_github),
    )

    rows, errors = collect_rows(
        config,
        direct_sources={
            "workday": DiagnosticFakeSource(
                {"Timed Direct": [direct_row]},
                requests=3,
                retries=1,
            ),
            "greenhouse": FakeSource(error=SourceError("fetch failed")),
        },
        github_source=[
            CountingGithub(successful_github.url, [github_row_value]),
            CountingGithub(failed_github.url, error=SourceFetchError("backstop failed")),
        ],
    )

    assert rows == [direct_row, github_row_value]
    assert errors == [
        "Broken Direct: fetch failed",
        "github listings (failed_feed [https://example.test/failed.md]): backstop failed",
    ]
    timing_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("SOURCE-TIMING ")
    ]
    assert len(timing_lines) == 4
    assert any(
        line.startswith(
            "SOURCE-TIMING company=Timed_Direct adapter=workday "
            "success=true "
        )
        and " rows=1 requests=3 retries=1" in line
        for line in timing_lines
    )
    assert any(
        line.startswith(
            "SOURCE-TIMING company=Broken_Direct adapter=greenhouse "
            "success=false "
        )
        and " rows=0" in line
        for line in timing_lines
    )
    assert any(
        "company=all adapter=simplify_json source=safe_feed success=true "
        in line
        and " rows=1" in line
        for line in timing_lines
    )
    assert any(
        "company=all adapter=github_markdown_table source=failed_feed "
        "success=false "
        in line
        and " rows=0" in line
        for line in timing_lines
    )
    assert all("https://" not in line for line in timing_lines)
    assert all(
        len(line.split(" seconds=", 1)[1].split(" ", 1)[0].split(".")[1]) == 3
        for line in timing_lines
    )


def test_collect_rows_skips_bespoke_and_github_only_for_direct_fetch():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="BespokeCo", ats="bespoke"),
            CompanyCfg(name="GitHub", ats="github_only"),
        )
    )
    github_rows = [row("GitHub", "Software Engineering Intern", source="github")]

    rows, errors = collect_rows(
        config,
        direct_sources={},
        github_source=FakeGithub(github_rows),
    )

    assert rows == github_rows
    assert errors == []


def test_collect_rows_records_exactly_one_direct_outcome_per_company_and_one_per_feed():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="HealthyCo", ats="greenhouse", token="healthy"),
            CompanyCfg(name="BespokeCo", ats="bespoke"),
            CompanyCfg(name="GitHubOnlyCo", ats="github_only"),
        ),
        github_listing_urls=("https://example.test/listings.json",),
    )
    stats = CollectionStats()
    observed = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)

    collect_rows(
        config,
        direct_sources={"greenhouse": FakeSource({"HealthyCo": [row("HealthyCo", "Intern")]})},
        github_source=CountingGithub("https://example.test/listings.json"),
        stats=stats,
        run_id="fixed-run",
        observed_at=observed,
    )

    direct_attempts = [item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_DIRECT]
    github_attempts = [item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_GITHUB_FEED]
    assert [item.company for item in direct_attempts] == ["HealthyCo", "BespokeCo", "GitHubOnlyCo"]
    assert len(github_attempts) == 1
    assert {item.run_id for item in stats.source_attempts} == {"fixed-run"}
    assert {item.observed_at for item in stats.source_attempts} == {observed}
    assert direct_attempts[0].rows_returned == 1
    assert direct_attempts[1].unsupported_reason == "bespoke"
    assert direct_attempts[2].unsupported_reason == "github_only"


def test_collect_rows_classifies_missing_schema_and_unexpected_failures():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="MissingCo", ats="lever", token="missing"),
            CompanyCfg(name="SchemaCo", ats="greenhouse", token="schema"),
            CompanyCfg(name="UnexpectedCo", ats="ashby", token="unexpected"),
        )
    )
    stats = CollectionStats()

    collect_rows(
        config,
        direct_sources={
            "greenhouse": FakeSource(error=SourceSchemaError("bad payload")),
            "ashby": FakeSource(error=ValueError("query_secret=hidden")),
        },
        github_source=FakeGithub([]),
        stats=stats,
        run_id="fixed-run",
        observed_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    errors = {item.company: item.error_kind for item in stats.source_attempts if item.company}
    assert errors == {
        "MissingCo": ERROR_MISSING_ADAPTER,
        "SchemaCo": ERROR_SCHEMA,
        "UnexpectedCo": ERROR_UNEXPECTED,
    }
    unexpected = next(item for item in stats.source_attempts if item.company == "UnexpectedCo")
    assert "hidden" not in unexpected.error_message


def test_collect_rows_preserves_an_explicitly_empty_source_registry(monkeypatch):
    config = WatcherConfig(
        companies=(CompanyCfg(name="NoAdapterCo", ats="greenhouse", token="unused"),)
    )
    def fail_if_defaults_are_built():
        raise AssertionError("default adapters should not be constructed")

    monkeypatch.setattr("watcher.collection._default_direct_sources", fail_if_defaults_are_built)

    rows, errors = collect_rows(
        config,
        direct_sources={},
        github_source=FakeGithub([]),
    )

    assert rows == []
    assert errors == ["NoAdapterCo: no source registered for ats 'greenhouse'"]


def test_collect_rows_fetches_and_aggregates_every_configured_github_feed_once(monkeypatch):
    duplicate = row("GitHub", "Software Engineering Intern", source="github")
    sources = {
        "https://example.test/one.json": CountingGithub("https://example.test/one.json", [duplicate]),
        "https://example.test/two.json": CountingGithub("https://example.test/two.json", [duplicate]),
    }
    config = WatcherConfig(
        companies=(CompanyCfg(name="GitHub", ats="github_only", terms=("Summer 2027",)),),
        terms=("Summer 2027",),
        github_listing_urls=tuple(sources),
    )
    monkeypatch.setattr(
        "watcher.collection.GitHubListingsSource",
        lambda url, **_kwargs: sources[url],
    )
    stats = CollectionStats()

    rows, errors = collect_rows(config, direct_sources={}, stats=stats)

    assert rows == [duplicate, duplicate]
    assert errors == []
    assert [source.calls for source in sources.values()] == [1, 1]
    assert stats.github_feeds_configured == 2
    assert stats.github_feeds_succeeded == 2


def test_one_failed_github_feed_keeps_successful_feed_rows_and_records_url(monkeypatch):
    good_row = row("GitHub", "Software Engineering Intern", source="github")
    sources = {
        "https://example.test/broken.json": CountingGithub(
            "https://example.test/broken.json",
            error=SourceError("request failed"),
        ),
        "https://example.test/good.json": CountingGithub("https://example.test/good.json", [good_row]),
    }
    config = WatcherConfig(
        companies=(CompanyCfg(name="GitHub", ats="github_only", terms=("Summer 2027",)),),
        terms=("Summer 2027",),
        github_listing_urls=tuple(sources),
    )
    monkeypatch.setattr(
        "watcher.collection.GitHubListingsSource",
        lambda url, **_kwargs: sources[url],
    )
    stats = CollectionStats()

    rows, errors = collect_rows(config, direct_sources={}, stats=stats)

    assert rows == [good_row]
    assert errors == ["github listings (https://example.test/broken.json): request failed"]
    assert stats.github_feeds_configured == 2
    assert stats.github_feeds_succeeded == 1
    github_attempts = [item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_GITHUB_FEED]
    assert len(github_attempts) == 2
    assert [item.succeeded for item in github_attempts] == [False, True]


def test_collect_rows_accepts_multiple_injected_github_sources():
    urls = ("https://example.test/one.json", "https://example.test/two.json")
    sources = [CountingGithub(url) for url in urls]
    config = WatcherConfig(
        companies=(CompanyCfg(name="GitHub", ats="github_only"),),
        github_listing_urls=urls,
    )
    stats = CollectionStats()

    collect_rows(config, direct_sources={}, github_source=sources, stats=stats, run_id="fixed-run")

    assert [source.calls for source in sources] == [1, 1]
    assert len([item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_GITHUB_FEED]) == 2


def test_all_github_feeds_failing_does_not_remove_direct_rows(monkeypatch):
    direct_row = row("DirectCo", "Software Engineer Intern", source="direct")
    urls = ("https://example.test/one.json", "https://example.test/two.json")
    sources = {url: CountingGithub(url, error=SourceError("boom")) for url in urls}
    config = WatcherConfig(
        companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="direct"),),
        terms=("Summer 2027",),
        github_listing_urls=urls,
    )
    monkeypatch.setattr(
        "watcher.collection.GitHubListingsSource",
        lambda url, **_kwargs: sources[url],
    )
    stats = CollectionStats()

    rows, errors = collect_rows(
        config,
        direct_sources={"greenhouse": FakeSource({"DirectCo": [direct_row]})},
        stats=stats,
    )

    assert rows == [direct_row]
    assert len(errors) == 2
    assert all(url in error for url, error in zip(urls, errors))
    assert stats.github_feeds_succeeded == 0


def test_twenty_four_identical_workday_transport_failures_are_shared_incident():
    stats = CollectionStats(
        workday_attempted=59,
        workday_succeeded=35,
        workday_failed=24,
        workday_retry_attempts=48,
    )
    stats.workday_failure_codes["html_challenge"] = 24

    summary = summarize_workday_transport(stats)

    assert summary == WorkdayTransportSummary(
        attempted_tenants=59,
        successful_tenants=35,
        failed_tenants=24,
        retry_attempts=48,
        dominant_error="html_challenge",
        dominant_error_count=24,
        likely_shared_incident=True,
    )


def test_non_workday_collection_does_not_invoke_workday_pacing(monkeypatch):
    def unexpected_pacing(self):
        pytest.fail("Workday pacing was used for a non-Workday adapter")

    monkeypatch.setattr(
        "watcher.sources.workday.WorkdayPacer.wait_for_tenant",
        unexpected_pacing,
    )
    config = WatcherConfig(
        companies=(CompanyCfg(name="Greenhouse Co", ats="greenhouse", token="board"),)
    )

    rows, errors = collect_rows(
        config,
        direct_sources={
            "greenhouse": FakeSource(
                {"Greenhouse Co": [row("Greenhouse Co", "Software Engineer Intern")]}
            )
        },
        github_source=FakeGithub([]),
    )

    assert len(rows) == 1
    assert errors == []


def test_isolated_or_mixed_workday_failures_do_not_create_false_incident():
    isolated = CollectionStats(workday_attempted=2, workday_failed=2)
    isolated.workday_failure_codes["html_challenge"] = 2
    assert summarize_workday_transport(isolated).likely_shared_incident is False

    mixed = CollectionStats(workday_attempted=10, workday_failed=10)
    mixed.workday_failure_codes.update(
        {"html_challenge": 5, "rate_limited": 3, "timeout": 2}
    )
    assert summarize_workday_transport(mixed).likely_shared_incident is False


def test_workday_shared_incident_rule_is_deterministic_at_sixty_percent():
    stats = CollectionStats(workday_attempted=10, workday_failed=10)
    stats.workday_failure_codes.update({"html_challenge": 6, "timeout": 4})

    first = summarize_workday_transport(stats)
    second = summarize_workday_transport(stats)

    assert first == second
    assert first.likely_shared_incident is True


def test_collect_rows_persists_each_workday_attempt_and_stable_transport_subtype():
    companies = tuple(
        CompanyCfg(
            name=f"Workday Co {index}",
            ats="workday",
            token=f"tenant{index}",
            workday_shard="wd5",
            workday_site="Site",
        )
        for index in range(5)
    )
    error = SourceFetchError(
        "challenge response",
        error_code="html_challenge",
        retryable=True,
        attempt_count=3,
    )
    stats = CollectionStats()

    collect_rows(
        WatcherConfig(companies=companies),
        direct_sources={"workday": FakeSource(error=error)},
        github_source=FakeGithub([]),
        stats=stats,
        run_id="workday-incident",
    )

    direct_attempts = [
        item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_DIRECT
    ]
    assert len(direct_attempts) == 5
    assert all(item.error_kind == "fetch_failure/html_challenge" for item in direct_attempts)
    assert all(item.succeeded is False for item in direct_attempts)
    assert summarize_workday_transport(stats).likely_shared_incident is True
