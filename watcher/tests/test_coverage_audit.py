from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.app.hosted.catalog import CompanyCatalog
from watcher.audit import main as audit_main
from watcher.config import CompanyCfg, GitHubListingSourceCfg, WatcherConfig
from watcher.health.coverage import build_coverage_audit
from watcher.health.models import (
    COVERAGE_AUDIT_BACKSTOP_ONLY,
    COVERAGE_AUDIT_DIRECT_DEGRADED,
    COVERAGE_AUDIT_DIRECT_UNVERIFIED,
    COVERAGE_AUDIT_DIRECT_VERIFIED,
    COVERAGE_AUDIT_NEEDS_INVESTIGATION,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    SourceAttempt,
)
from watcher.health.state import (
    calculate_next_state,
    direct_health_key,
    github_feed_health_key,
)
from watcher.health.store import SourceHealthStore


NOW = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _direct_state(company: str, status: str, *, adapter: str = "greenhouse"):
    succeeded = status != "failed"
    degraded = status == "degraded"
    rows = 0 if status == "healthy_empty" else 2 if succeeded else None
    attempt = SourceAttempt(
        health_key=direct_health_key(company, adapter),
        run_id=f"run-{company}",
        observed_at=NOW,
        source_kind=SOURCE_KIND_DIRECT,
        company=company,
        adapter=adapter,
        attempted=True,
        succeeded=succeeded,
        rows_returned=rows,
        error_kind="fetch_failure" if not succeeded else None,
        error_message="bounded failure" if not succeeded else None,
        malformed_row_count=1 if degraded else 0,
        schema_error_row_count=0,
        duplicate_row_count=0,
        failed_request_count=1 if not succeeded else 0,
        incomplete=degraded or not succeeded,
        truncated=False,
        reason_codes=("malformed_records_skipped",) if degraded else (),
        degraded=degraded,
        complete=not degraded and succeeded,
    )
    return calculate_next_state(None, attempt)


def _config(tmp_path: Path, *, github_backstop: bool = True) -> WatcherConfig:
    github_sources = (
        (
            GitHubListingSourceCfg(
                name="backstop",
                format="simplify_json",
                url="https://example.test/listings.json",
            ),
        )
        if github_backstop
        else ()
    )
    return WatcherConfig(
        companies=(
            CompanyCfg(name="Zulu Missing", ats="greenhouse", token="zulu"),
            CompanyCfg(name="Alpha Healthy", ats="greenhouse", token="alpha"),
            CompanyCfg(name="Empty Healthy", ats="greenhouse", token="empty"),
            CompanyCfg(name="Delta Degraded", ats="greenhouse", token="delta"),
            CompanyCfg(name="Foxtrot Failed", ats="greenhouse", token="foxtrot"),
            CompanyCfg(name="Bravo Backstop", ats="github_only"),
        ),
        terms=("Summer 2027",),
        github_listing_sources=github_sources,
        seen_db_path=tmp_path / "seen.sqlite",
    )


def test_coverage_audit_classifies_every_product_state(tmp_path):
    config = _config(tmp_path)
    states = {
        state.health_key: state
        for state in (
            _direct_state("Alpha Healthy", "healthy"),
            _direct_state("Empty Healthy", "healthy_empty"),
            _direct_state("Delta Degraded", "degraded"),
            _direct_state("Foxtrot Failed", "failed"),
        )
    }

    report = build_coverage_audit(
        config,
        states,
        state_database_present=True,
    )
    by_company = {item.company: item for item in report.companies}

    assert by_company["Alpha Healthy"].state == COVERAGE_AUDIT_DIRECT_VERIFIED
    assert by_company["Empty Healthy"].state == COVERAGE_AUDIT_DIRECT_VERIFIED
    assert by_company["Delta Degraded"].state == COVERAGE_AUDIT_DIRECT_DEGRADED
    assert by_company["Foxtrot Failed"].state == COVERAGE_AUDIT_DIRECT_DEGRADED
    assert by_company["Zulu Missing"].state == COVERAGE_AUDIT_DIRECT_UNVERIFIED
    assert by_company["Bravo Backstop"].state == COVERAGE_AUDIT_BACKSTOP_ONLY
    assert [item.company for item in report.companies] == sorted(
        (company.name for company in config.companies),
        key=lambda value: (value.casefold(), value),
    )
    assert report.state_counts == {
        "direct_verified": 2,
        "direct_degraded": 2,
        "direct_unverified": 1,
        "backstop_only": 1,
        "needs_investigation": 0,
    }


def test_no_direct_source_without_a_backstop_needs_investigation(tmp_path):
    config = WatcherConfig(
        companies=(CompanyCfg(name="No Source", ats="github_only"),),
        terms=("Summer 2027",),
        seen_db_path=tmp_path / "seen.sqlite",
    )

    report = build_coverage_audit(
        config,
        {},
        state_database_present=False,
    )

    assert report.companies[0].state == COVERAGE_AUDIT_NEEDS_INVESTIGATION
    assert report.state_database_present is False


def test_global_github_health_never_proves_company_direct_coverage(tmp_path):
    config = _config(tmp_path)
    feed_attempt = SourceAttempt(
        health_key=github_feed_health_key("backstop"),
        run_id="feed-run",
        observed_at=NOW,
        source_kind=SOURCE_KIND_GITHUB_FEED,
        company=None,
        adapter="simplify_json",
        feed_label="backstop",
        attempted=True,
        succeeded=True,
        rows_returned=100,
    )
    feed_state = calculate_next_state(None, feed_attempt)

    report = build_coverage_audit(
        config,
        {feed_state.health_key: feed_state},
        state_database_present=True,
    )
    by_company = {item.company: item for item in report.companies}

    assert by_company["Zulu Missing"].state == COVERAGE_AUDIT_DIRECT_UNVERIFIED
    assert by_company["Bravo Backstop"].state == COVERAGE_AUDIT_BACKSTOP_ONLY


def test_coverage_categories_match_hosted_catalog_visibility(tmp_path):
    config = _config(tmp_path)
    report = build_coverage_audit(config, {}, state_database_present=False)
    catalog = CompanyCatalog.from_watcher_config(config)

    by_name = {company.name: company for company in catalog.companies}
    for item in report.companies:
        assert by_name[item.company].selectable is True
        assert by_name[item.company].coverage == (
            "backstop" if item.state == COVERAGE_AUDIT_BACKSTOP_ONLY else "direct"
        )


def test_coverage_json_is_deterministic_and_cli_is_read_only(
    tmp_path, monkeypatch, capsys
):
    config = _config(tmp_path)
    healthy = _direct_state("Alpha Healthy", "healthy")
    with SourceHealthStore(config.seen_db_path) as store:
        store.record_attempts(
            [
                SourceAttempt(
                    health_key=healthy.health_key,
                    run_id="seed",
                    observed_at=NOW,
                    source_kind=healthy.source_kind,
                    company=healthy.company,
                    adapter=healthy.adapter,
                    attempted=True,
                    succeeded=True,
                    rows_returned=2,
                    malformed_row_count=0,
                    schema_error_row_count=0,
                    duplicate_row_count=0,
                    failed_request_count=0,
                    incomplete=False,
                    truncated=False,
                    degraded=False,
                    complete=True,
                )
            ]
        )
    before_bytes = config.seen_db_path.read_bytes()
    before_mtime = config.seen_db_path.stat().st_mtime_ns
    monkeypatch.setattr("watcher.audit.load_watchlist", lambda _path: config)

    def explode(*_args, **_kwargs):
        raise AssertionError("coverage audit must not collect")

    monkeypatch.setattr("watcher.audit.collect_rows", explode)

    assert audit_main(["--coverage", "--json"]) == 0
    first = capsys.readouterr().out
    assert audit_main(["--coverage", "--json"]) == 0
    second = capsys.readouterr().out

    assert first == second
    payload = json.loads(first)
    assert payload["schema_version"] == 1
    assert payload["report_type"] == "product_source_coverage"
    assert payload["state_database_present"] is True
    assert len(payload["companies"]) == payload["total_companies"]
    assert config.seen_db_path.read_bytes() == before_bytes
    assert config.seen_db_path.stat().st_mtime_ns == before_mtime


def test_coverage_audit_does_not_create_a_missing_state_database(
    tmp_path, monkeypatch, capsys
):
    missing = tmp_path / "missing" / "seen.sqlite"
    config = WatcherConfig(
        companies=(CompanyCfg(name="Missing", ats="greenhouse", token="missing"),),
        terms=("Summer 2027",),
        seen_db_path=missing,
    )
    monkeypatch.setattr("watcher.audit.load_watchlist", lambda _path: config)

    assert audit_main(["--coverage", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["state_database_present"] is False
    assert payload["companies"][0]["state"] == "direct_unverified"
    assert not missing.exists()
    assert not missing.parent.exists()


def test_read_only_health_store_rejects_mutation(tmp_path):
    path = tmp_path / "seen.sqlite"
    with SourceHealthStore(path, read_only=True) as store:
        assert store.source_database_present is False
        with pytest.raises(RuntimeError, match="source-health store is read-only"):
            store.record_attempts([])
    assert not path.exists()


def test_coverage_cannot_be_combined_with_live(capsys):
    with pytest.raises(SystemExit) as raised:
        audit_main(["--coverage", "--live"])

    assert raised.value.code == 2
    assert "--coverage is read-only" in capsys.readouterr().err


def test_coverage_cannot_be_combined_with_comparison(capsys):
    with pytest.raises(SystemExit) as raised:
        audit_main(["--coverage", "--comparison"])

    assert raised.value.code == 2
    assert "--coverage cannot be combined" in capsys.readouterr().err


def test_coverage_owner_does_not_import_orchestration_or_facades():
    import watcher.health.coverage as coverage

    tree = ast.parse(Path(coverage.__file__).read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not imported & {
        "backend.app.hosted.catalog",
        "watcher.collection",
        "watcher.pipeline",
        "watcher.run",
        "watcher.source_health",
    }
