"""Paylocity public-board contract and canonical parsing tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import watcher.sources.paylocity as paylocity_module
from watcher.config import CompanyCfg
from watcher.run import _default_direct_sources
from watcher.sources import PaylocitySource as ExportedPaylocitySource
from watcher.sources.base import SourceError, SourceSchemaError, TextHttpResponse
from watcher.sources.paylocity import PaylocitySource


FIXTURES = Path(__file__).parent / "fixtures"
PROCURE_GUID = "37f1fc46-3c9a-4802-995e-eebd78e096d7"
PROCURE = CompanyCfg(
    name="Procure Analytics",
    ats="paylocity",
    paylocity_company_id=PROCURE_GUID,
    paylocity_module_id="11566",
    paylocity_slug="Procurement-Advisors-LLC",
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _job(job_id: int, title: str = "Software Intern") -> dict[str, Any]:
    return {
        "Description": "<p>Build software.</p>",
        "HiringDepartment": "Engineering",
        "IndeedRemoteType": 2,
        "IsInternal": False,
        "IsRemote": False,
        "JobId": job_id,
        "JobLocation": {
            "City": "Atlanta",
            "Country": "USA",
            "ModuleId": 11566,
            "Name": "Atlanta HQ",
            "State": "GA",
        },
        "JobTitle": title,
        "LocationName": "Atlanta HQ",
        "PublishedDate": "2026-08-04T10:30:00-05:00",
        "ShouldDisplayLocation": True,
    }


def _page_data(jobs: Any) -> dict[str, Any]:
    return {
        "Departments": ["All Departments", "Engineering"],
        "Jobs": jobs,
        "LeadJoinUrl": f"/Recruiting/PublicLeads/New/{PROCURE_GUID}",
        "Locations": ["All Locations", "Atlanta HQ"],
        "ModuleId": "11566",
        "ModuleTitle": "Procure Analytics",
        "ShowInternal": False,
    }


def _html(jobs: Any, *, page_data: dict[str, Any] | None = None) -> str:
    data = _page_data(jobs) if page_data is None else page_data
    return (
        "<!doctype html><html><body><script>"
        "window.ATSJobDetailsBaseUrl = '/Recruiting/Jobs/Details/';"
        f"window.pageData = {json.dumps(data, separators=(',', ':'))};"
        "</script></body></html>"
    )


def test_procure_fixture_produces_canonical_posting():
    source = PaylocitySource()

    rows = source.parse(_fixture("paylocity_procure_jobs.html"), PROCURE)

    assert len(rows) == 1
    row = rows[0]
    assert row["company"] == "Procure Analytics"
    assert row["title"] == "Operations Analyst"
    assert row["location"] == "Atlanta HQ"
    assert row["description"] == "Analyze operations and deliver clear recommendations."
    assert row["date_posted"] == "2026-08-04"
    assert row["source_url"] == (
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/4076299"
    )
    assert row["extra"]["source_adapter"] == "paylocity"
    assert row["extra"]["paylocity_native_id"] == "4076299"
    assert row["extra"]["source_requisition_id"] == (
        f"paylocity:{PROCURE_GUID}:4076299"
    )
    assert row["extra"]["department"] == "Operations"
    assert source.last_health_diagnostics.complete is True


def test_second_populated_tenant_uses_the_same_contract():
    company = CompanyCfg(
        name="Spartan Logistics",
        ats="paylocity",
        paylocity_company_id="0b09c4ef-9cba-498c-adeb-923f4e37eb6c",
        paylocity_module_id="24077",
        paylocity_slug="Spartan-Logistics",
    )

    rows = PaylocitySource().parse(
        _fixture("paylocity_spartan_jobs.html"), company
    )

    assert [row["extra"]["paylocity_native_id"] for row in rows] == [
        "3555878",
        "3594746",
    ]
    assert len({row["source_url"] for row in rows}) == 2


def test_verified_empty_tenant_is_a_successful_empty_board():
    company = CompanyCfg(
        name="Central Indiana Community Foundation",
        ats="paylocity",
        paylocity_company_id="85980df1-c115-49cf-b7d4-5516ba076751",
        paylocity_module_id="1732",
        paylocity_slug="Central-Indiana-Community-Foundation-Inc",
    )
    source = PaylocitySource()

    assert source.parse(_fixture("paylocity_cicf_empty.html"), company) == []
    assert source.last_health_diagnostics.complete is True


def test_fetch_uses_one_complete_board_request_and_retains_safe_metadata():
    calls: list[tuple[str, str]] = []

    def request_text(url: str, source_name: str):
        calls.append((url, source_name))
        return TextHttpResponse(
            text=_fixture("paylocity_procure_jobs.html"),
            metadata={"status_code": 200, "body_bytes": 100},
        )

    source = PaylocitySource(request_text=request_text)

    rows = source.fetch(PROCURE)

    assert len(rows) == 1
    assert calls == [(PaylocitySource.endpoint(PROCURE), "paylocity")]
    assert source.request_count == 1
    assert source.last_response_metadata == {"status_code": 200, "body_bytes": 100}


def test_default_source_construction_includes_paylocity():
    assert isinstance(_default_direct_sources()["paylocity"], PaylocitySource)
    assert ExportedPaylocitySource is PaylocitySource


@pytest.mark.parametrize(
    "company",
    [
        CompanyCfg(
            name="Bad",
            paylocity_company_id="bad",
            paylocity_module_id="1",
            paylocity_slug="Example",
        ),
        CompanyCfg(
            name="Bad",
            paylocity_company_id=PROCURE_GUID.upper(),
            paylocity_module_id="1",
            paylocity_slug="Example",
        ),
        CompanyCfg(
            name="Bad",
            paylocity_company_id=PROCURE_GUID,
            paylocity_module_id="0",
            paylocity_slug="Example",
        ),
        CompanyCfg(
            name="Bad",
            paylocity_company_id=PROCURE_GUID,
            paylocity_module_id="1",
            paylocity_slug="../Example",
        ),
    ],
)
def test_endpoint_rejects_invalid_direct_configuration(company):
    with pytest.raises(SourceError):
        PaylocitySource.endpoint(company)


def test_mixed_malformed_records_are_retained_with_bounded_degradation():
    source = PaylocitySource()

    rows = source.parse(_html([_job(101), {"JobId": 102}, "bad"]), PROCURE)

    assert [row["extra"]["paylocity_native_id"] for row in rows] == ["101"]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.malformed_row_count == 1
    assert diagnostics.schema_error_row_count == 1
    assert diagnostics.reason_codes == (
        "malformed_records_skipped",
        "schema_invalid_records_skipped",
    )
    assert diagnostics.degraded is True
    assert diagnostics.complete is False


def test_nonempty_all_malformed_board_fails():
    with pytest.raises(SourceSchemaError, match="none were valid"):
        PaylocitySource().parse(_html([{"JobId": 102}]), PROCURE)


def test_exact_duplicate_is_counted_but_not_returned_twice():
    posting = _job(101)
    source = PaylocitySource()

    rows = source.parse(_html([posting, posting.copy()]), PROCURE)

    assert len(rows) == 1
    assert source.last_health_diagnostics.duplicate_row_count == 1
    assert source.last_health_diagnostics.complete is True


def test_conflicting_duplicate_id_fails_closed():
    with pytest.raises(SourceSchemaError, match="conflicting posting ID"):
        PaylocitySource().parse(
            _html([_job(101), _job(101, "Different title")]), PROCURE
        )


def test_conflicting_ids_for_one_url_fail_closed(monkeypatch):
    original = paylocity_module._parse_posting

    def same_url(posting, company, company_id, module_id):
        row = original(posting, company, company_id, module_id)
        row["source_url"] = "https://recruiting.paylocity.com/Recruiting/Jobs/Details/101"
        return row

    monkeypatch.setattr(paylocity_module, "_parse_posting", same_url)

    with pytest.raises(SourceSchemaError, match="conflicting IDs"):
        PaylocitySource().parse(_html([_job(101), _job(102)]), PROCURE)


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ("", "empty"),
        ("<html><body>outer shell</body></html>", "pageData"),
        (
            _html([]) + "<script>window.pageData = {};</script>",
            "exactly one",
        ),
        (
            "<script>window.ATSJobDetailsBaseUrl = '/Recruiting/Jobs/Details/';"
            "window.pageData = {bad};</script>",
            "malformed",
        ),
        (
            "<script>window.ATSJobDetailsBaseUrl = '/Recruiting/Jobs/Details/';"
            "window.pageData = [];</script>",
            "object",
        ),
        (
            _html(
                [],
                page_data={
                    **_page_data([]),
                    "LeadJoinUrl": (
                        "/Recruiting/PublicLeads/New/"
                        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                    ),
                },
            ),
            "company",
        ),
        (
            _html([], page_data={**_page_data([]), "ModuleId": "99999"}),
            "module identity",
        ),
        (
            _html([]).replace("/Recruiting/Jobs/Details/", "/Recruiting/Jobs/"),
            "detail URL contract",
        ),
        (
            _html([]).replace(
                "'/Recruiting/Jobs/Details/'",
                "'https://evil.example/Recruiting/Jobs/Details/'",
            ),
            "detail URL contract",
        ),
        (
            _html([], page_data={**_page_data([]), "Jobs": {"bad": "shape"}}),
            "Jobs",
        ),
    ],
)
def test_untrusted_outer_contracts_fail_closed(html, message):
    with pytest.raises(SourceSchemaError, match=message):
        PaylocitySource().parse(html, PROCURE)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"ModuleTitle": ""}, "module title"),
        ({"Departments": {}}, "filter metadata"),
        ({"Locations": None}, "filter metadata"),
        ({"ShowInternal": 0}, "visibility metadata"),
    ],
)
def test_malformed_board_metadata_fails_closed(updates, message):
    page_data = {**_page_data([]), **updates}

    with pytest.raises(SourceSchemaError, match=message):
        PaylocitySource().parse(_html([], page_data=page_data), PROCURE)


@pytest.mark.parametrize(
    "updates",
    [
        {"JobId": True},
        {"JobId": "101"},
        {"JobId": 0},
        {"JobTitle": ""},
        {"JobLocation": "Atlanta"},
        {"JobLocation": {"ModuleId": 99999}},
        {"PublishedDate": "not-a-date"},
        {"IsRemote": "yes"},
        {"IndeedRemoteType": True},
    ],
)
def test_schema_invalid_jobs_are_never_fabricated(updates):
    posting = {**_job(101), **updates}

    with pytest.raises(SourceSchemaError, match="none were valid"):
        PaylocitySource().parse(_html([posting]), PROCURE)


@pytest.mark.parametrize("field", ["Name", "City", "State", "Country"])
def test_non_string_location_metadata_is_not_fabricated(field):
    posting = _job(101)
    posting["LocationName"] = None
    posting["JobLocation"] = {
        **posting["JobLocation"],
        "Name": "",
        field: {"unexpected": "object"},
    }

    with pytest.raises(SourceSchemaError, match="none were valid"):
        PaylocitySource().parse(_html([posting]), PROCURE)


def test_distinct_native_ids_always_generate_distinct_posting_urls():
    rows = PaylocitySource().parse(_html([_job(101), _job(102)]), PROCURE)

    assert [row["source_url"] for row in rows] == [
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/101",
        "https://recruiting.paylocity.com/Recruiting/Jobs/Details/102",
    ]
