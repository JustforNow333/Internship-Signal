from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from watcher.audit import main as audit_main
from watcher.config import (
    CompanyCfg,
    GitHubListingSourceCfg,
    WatcherConfig,
    load_watchlist,
)
from watcher.source_health import (
    COVERAGE_AUDIT_BACKSTOP_ONLY,
    COVERAGE_AUDIT_DIRECT_DEGRADED,
    COVERAGE_AUDIT_DIRECT_VERIFIED,
    COVERAGE_AUDIT_NEEDS_INVESTIGATION,
    COVERAGE_AUDIT_NO_SOURCE_FOUND,
    SOURCE_KIND_DIRECT,
    SourceAttempt,
    SourceHealthStore,
    build_coverage_audit,
    calculate_next_state,
    direct_health_key,
)


NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)


def _direct_state(company: str, *, status: str):
    succeeded = status != "failed"
    degraded = status == "degraded"
    attempt = SourceAttempt(
        health_key=direct_health_key(company, "greenhouse"),
        run_id=f"run-{company}",
        observed_at=NOW,
        source_kind=SOURCE_KIND_DIRECT,
        company=company,
        adapter="greenhouse",
        attempted=True,
        succeeded=succeeded,
        rows_returned=2 if succeeded else None,
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


def _coverage_config(tmp_path) -> WatcherConfig:
    return WatcherConfig(
        companies=(
            CompanyCfg(name="Alpha", ats="greenhouse", token="alpha"),
            CompanyCfg(name="Beta", ats="greenhouse", token="beta"),
            CompanyCfg(name="Gamma", ats="greenhouse", token="gamma"),
            CompanyCfg(name="Backstop", ats="github_only"),
            CompanyCfg(
                name="Explicit None",
                ats="github_only",
                coverage_status="no_source_found",
            ),
            CompanyCfg(name="Ambiguous", ats="greenhouse", token="ambiguous"),
            CompanyCfg(
                name="Arm",
                ats="bespoke",
                module="arm",
                platform_family="iCIMS",
            ),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(
            GitHubListingSourceCfg(
                name="backstop",
                format="simplify_json",
                url="https://example.test/listings.json",
            ),
        ),
        seen_db_path=tmp_path / "seen.sqlite",
    )


def test_coverage_audit_classifies_health_configuration_and_totals(tmp_path):
    config = _coverage_config(tmp_path)
    states = {
        state.health_key: state
        for state in (
            _direct_state("Alpha", status="healthy"),
            _direct_state("Beta", status="degraded"),
            _direct_state("Gamma", status="failed"),
        )
    }

    report = build_coverage_audit(config, states)
    by_company = {item.company: item for item in report.companies}

    assert by_company["Alpha"].state == COVERAGE_AUDIT_DIRECT_VERIFIED
    assert by_company["Beta"].state == COVERAGE_AUDIT_DIRECT_DEGRADED
    assert by_company["Gamma"].state == COVERAGE_AUDIT_DIRECT_DEGRADED
    assert by_company["Backstop"].state == COVERAGE_AUDIT_BACKSTOP_ONLY
    assert by_company["Explicit None"].state == COVERAGE_AUDIT_NO_SOURCE_FOUND
    assert by_company["Ambiguous"].state == COVERAGE_AUDIT_NEEDS_INVESTIGATION
    assert by_company["Arm"].state == COVERAGE_AUDIT_BACKSTOP_ONLY

    assert report.total_companies == 7
    assert report.state_counts == {
        "direct_verified": 1,
        "direct_degraded": 2,
        "backstop_only": 2,
        "no_source_found": 1,
        "needs_investigation": 1,
    }
    assert report.state_percentages == {
        "direct_verified": 14.3,
        "direct_degraded": 28.6,
        "backstop_only": 28.6,
        "no_source_found": 14.3,
        "needs_investigation": 14.3,
    }
    assert report.direct_coverage_percentage == 14.3
    assert report.accounted_coverage_percentage == 85.7
    assert report.needs_investigation == ("Ambiguous",)
    assert [item.company for item in report.degraded_direct_sources] == [
        "Beta",
        "Gamma",
    ]
    assert report.platform_gaps[0].platform_family == "iCIMS"
    assert report.platform_gaps[0].companies == ("Arm",)


@pytest.mark.parametrize("status", ["degraded", "failed"])
def test_untrustworthy_persisted_direct_health_is_degraded(tmp_path, status):
    config = WatcherConfig(
        companies=(CompanyCfg(name="Alpha", ats="greenhouse", token="alpha"),),
        terms=("Summer 2027",),
        seen_db_path=tmp_path / "seen.sqlite",
    )
    state = _direct_state("Alpha", status=status)

    report = build_coverage_audit(config, {state.health_key: state})

    assert report.companies[0].state == COVERAGE_AUDIT_DIRECT_DEGRADED


def test_coverage_json_is_deterministic_bounded_and_cli_is_read_only(
    tmp_path, monkeypatch, capsys
):
    config = _coverage_config(tmp_path)
    healthy = _direct_state("Alpha", status="healthy")
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
    before = config.seen_db_path.read_bytes()

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
    assert payload["report_type"] == "company_source_coverage"
    assert set(payload) == {
        "accounted_coverage_percentage",
        "companies",
        "degraded_direct_sources",
        "direct_coverage_percentage",
        "needs_investigation",
        "platform_gaps",
        "report_type",
        "schema_version",
        "state_counts",
        "state_percentages",
        "total_companies",
    }
    assert len(payload["companies"]) == payload["total_companies"]
    assert config.seen_db_path.read_bytes() == before


def test_coverage_audit_does_not_create_a_missing_state_database(
    tmp_path, monkeypatch
):
    config = _coverage_config(tmp_path)
    missing = tmp_path / "missing" / "seen.sqlite"
    config = WatcherConfig(
        companies=config.companies,
        terms=config.terms,
        github_listing_sources=config.github_listing_sources,
        seen_db_path=missing,
    )
    monkeypatch.setattr("watcher.audit.load_watchlist", lambda _path: config)

    assert audit_main(["--coverage"]) == 0
    assert not missing.exists()
    assert not missing.parent.exists()


def test_watchlist_coverage_metadata_is_optional_and_backward_compatible(tmp_path):
    legacy = tmp_path / "legacy.yml"
    legacy.write_text(
        "defaults:\n"
        '  terms: ["Summer 2027"]\n'
        "companies:\n"
        '  - name: "Legacy"\n'
        "    ats: github_only\n",
        encoding="utf-8",
    )
    current = tmp_path / "current.yml"
    current.write_text(
        "defaults:\n"
        '  terms: ["Summer 2027"]\n'
        "companies:\n"
        '  - name: "Investigated"\n'
        "    ats: github_only\n"
        "    coverage_status: no_source_found\n"
        "    platform_family: iCIMS\n",
        encoding="utf-8",
    )

    legacy_company = load_watchlist(legacy).companies[0]
    current_company = load_watchlist(current).companies[0]

    assert legacy_company.coverage_status == ""
    assert legacy_company.platform_family == ""
    assert current_company.coverage_status == "no_source_found"
    assert current_company.platform_family == "iCIMS"
