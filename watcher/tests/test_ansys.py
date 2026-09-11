"""Ansys uses the official Synopsys search only as practical partial coverage."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.app.hosted.catalog import CompanyCatalog
from watcher.collection_concurrency import direct_origin_key
from watcher.config import CompanyCfg, WatcherConfig
from watcher.health.coverage import build_coverage_audit
from watcher.health.models import (
    COVERAGE_AUDIT_DIRECT_DEGRADED,
    COVERAGE_AUDIT_DIRECT_UNVERIFIED,
    DIRECT_STATUS_DEGRADED,
    SOURCE_KIND_DIRECT,
    SourceAttempt,
)
from watcher.health.state import calculate_next_state, direct_health_key
from watcher.sources.ansys import AnsysSource
from watcher.sources.contracts import SourceSchemaError
from watcher.sources.registry import (
    DIRECT_ATS,
    DIRECT_COMPLETE_ATS,
    DIRECT_PRACTICAL_PARTIAL_ATS,
    build_direct_sources,
)


FIXTURES = Path(__file__).parent / "fixtures"


def company() -> CompanyCfg:
    return CompanyCfg(
        name="Ansys",
        ats="ansys",
        talentbrew_host="careers.synopsys.com",
        talentbrew_site_id="44408",
        source_url="https://careers.synopsys.com/search-jobs/ansys/44408/1",
    )


def search_payload() -> dict:
    return json.loads((FIXTURES / "ansys_search.json").read_text(encoding="utf-8"))


def detail(url: str, _name: str) -> str:
    posting_id = urlsplit(url).path.rstrip("/").split("/")[-1]
    reference = {
        "98682479840": "18046",
        "99719320320": "17564",
    }[posting_id]
    return (
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"JobPosting",'
        f'"title":"Ansys role {reference}","identifier":"{reference}",'
        f'"url":{json.dumps(url)},"datePosted":"2026-09-10",'
        '"hiringOrganization":{"@type":"Organization","name":"Synopsys"},'
        '"description":"<p>Work on Ansys simulation products.</p>"}'
        "</script>"
    )


def test_official_keyword_inventory_is_usable_but_never_complete():
    calls: list[str] = []

    def request_json(url: str, name: str) -> dict:
        assert name == "ansys"
        calls.append(url)
        return copy.deepcopy(search_payload())

    source = AnsysSource(request_json=request_json, request_text=detail)
    rows = source.fetch(company())

    assert len(rows) == 2
    assert len({row["extra"]["source_requisition_id"] for row in rows}) == 2
    assert len({row["source_url"] for row in rows}) == 2
    assert all(row["company"] == "Ansys" for row in rows)
    assert all(row["extra"]["source_adapter"] == "ansys" for row in rows)
    assert all(row["extra"]["official_search_keyword"] == "ansys" for row in rows)
    assert all(
        row["extra"]["source_completeness"] == "practical_partial"
        for row in rows
    )
    assert all(
        row["extra"]["source_scope"]
        == "careers.synopsys.com:44408:keyword:ansys"
        for row in rows
    )

    query = parse_qs(urlsplit(calls[0]).query, keep_blank_values=True)
    assert query["Keywords"] == ["ansys"]
    assert query["OrganizationIds"] == ["44408"]
    assert query["SearchType"] == ["1"]
    assert query["RecordsPerPage"] == ["100"]
    assert not any(key.startswith("FacetFilters[") for key in query)

    diagnostics = source.last_health_diagnostics
    assert diagnostics.succeeded is True
    assert diagnostics.retained_row_count == 2
    assert diagnostics.incomplete is True
    assert diagnostics.degraded is True
    assert diagnostics.complete is False
    assert diagnostics.reason_codes == ("scope_not_completeness_proven",)
    assert source.last_diagnostics.listing_pages_requested == 1
    assert source.last_diagnostics.detail_pages_requested == 2
    assert source.last_diagnostics.request_attempts == 3


def test_wrong_keyword_or_organization_response_fails_closed():
    for attribute, wrong_value in (
        ('data-keywords="ansys"', 'data-keywords="synopsys"'),
        ('data-organization-ids="44408"', 'data-organization-ids="other"'),
        ('data-search-type="1"', 'data-search-type="6"'),
    ):
        payload = search_payload()
        payload["results"] = payload["results"].replace(attribute, wrong_value)
        source = AnsysSource(
            request_json=lambda _url, _name, value=payload: copy.deepcopy(value),
            request_text=detail,
        )

        with pytest.raises(SourceSchemaError, match="Ansys-targeted search scope"):
            source.fetch(company())
        assert source.last_health_diagnostics.complete is False


def test_practical_partial_diagnostics_become_degraded_not_verified_direct():
    source = AnsysSource(
        request_json=lambda _url, _name: copy.deepcopy(search_payload()),
        request_text=detail,
    )
    rows = source.fetch(company())
    diagnostics = source.last_health_diagnostics
    attempt = SourceAttempt(
        health_key=direct_health_key("Ansys", "ansys"),
        run_id="ansys-test",
        observed_at=datetime.now(timezone.utc),
        source_kind=SOURCE_KIND_DIRECT,
        company="Ansys",
        adapter="ansys",
        attempted=True,
        succeeded=True,
        rows_returned=len(rows),
        malformed_row_count=diagnostics.malformed_row_count,
        schema_error_row_count=diagnostics.schema_error_row_count,
        duplicate_row_count=diagnostics.duplicate_row_count,
        failed_request_count=diagnostics.failed_request_count,
        incomplete=diagnostics.incomplete,
        truncated=diagnostics.truncated,
        reason_codes=diagnostics.reason_codes,
        degraded=diagnostics.degraded,
        complete=diagnostics.complete,
    )

    state = calculate_next_state(None, attempt)

    assert state.status == DIRECT_STATUS_DEGRADED
    assert state.last_complete is False
    assert state.last_incomplete is True
    report = build_coverage_audit(
        WatcherConfig(companies=(company(),)),
        {state.health_key: state},
        state_database_present=True,
    )
    assert report.companies[0].state == COVERAGE_AUDIT_DIRECT_DEGRADED

    inconsistent_healthy = calculate_next_state(
        None,
        replace(
            attempt,
            incomplete=False,
            degraded=False,
            complete=True,
            reason_codes=(),
        ),
    )
    guarded = build_coverage_audit(
        WatcherConfig(companies=(company(),)),
        {inconsistent_healthy.health_key: inconsistent_healthy},
        state_database_present=True,
    )
    assert guarded.companies[0].state == COVERAGE_AUDIT_DIRECT_UNVERIFIED


def test_registry_origin_and_catalog_keep_partial_distinct_from_complete():
    source = build_direct_sources()["ansys"]
    catalog = CompanyCatalog.from_watcher_config()
    ansys = catalog.resolve("Ansys")

    assert isinstance(source, AnsysSource)
    assert "ansys" in DIRECT_ATS
    assert DIRECT_PRACTICAL_PARTIAL_ATS == frozenset({"ansys"})
    assert "ansys" not in DIRECT_COMPLETE_ATS
    assert direct_origin_key("ansys") == "https://careers.synopsys.com"
    assert ansys is not None
    assert ansys.coverage == "direct_practical_partial"
    assert ansys.selectable is True
