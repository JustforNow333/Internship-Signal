"""Paylocity public-board contract and canonical parsing tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from watcher.config import CompanyCfg
from watcher.run import _default_direct_sources
from watcher.sources.base import SourceSchemaError
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


def _html(
    jobs: Any,
    *,
    company_id: str = PROCURE_GUID,
    module_id: str = "11566",
    detail_base: str = "/Recruiting/Jobs/Details/",
) -> str:
    page_data = {
        "Departments": ["All Departments", "Engineering"],
        "Jobs": jobs,
        "LeadJoinUrl": f"/Recruiting/PublicLeads/New/{company_id}",
        "Locations": ["All Locations", "Atlanta HQ"],
        "ModuleId": module_id,
        "ModuleTitle": "Procure Analytics",
        "ShowInternal": False,
    }
    return (
        "<!doctype html><html><body><script>"
        f"window.ATSJobDetailsBaseUrl = {detail_base!r};"
        f"window.pageData = {json.dumps(page_data, separators=(',', ':'))};"
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


def test_fetch_uses_one_complete_board_request():
    calls: list[tuple[str, str]] = []

    def request_text(url: str, source_name: str):
        calls.append((url, source_name))
        return _fixture("paylocity_procure_jobs.html")

    source = PaylocitySource(request_text=request_text)

    rows = source.fetch(PROCURE)

    assert len(rows) == 1
    assert calls == [(PaylocitySource.endpoint(PROCURE), "paylocity")]
    assert source.request_count == 1


def test_default_source_registry_includes_paylocity():
    assert isinstance(_default_direct_sources()["paylocity"], PaylocitySource)


def test_mixed_malformed_records_are_retained_with_degradation():
    source = PaylocitySource()

    rows = source.parse(_html([_job(101), {"JobId": 102}]), PROCURE)

    assert [row["extra"]["paylocity_native_id"] for row in rows] == ["101"]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.schema_error_row_count == 1
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


@pytest.mark.parametrize(
    ("html", "message"),
    [
        ("<html><body>outer shell</body></html>", "pageData"),
        (_html([], company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"), "company"),
        (_html([], module_id="99999"), "module"),
        (_html([], detail_base="/Recruiting/Jobs/"), "detail URL contract"),
        (
            _html([], detail_base="https://evil.example/Recruiting/Jobs/Details/"),
            "detail URL contract",
        ),
        (_html({"not": "a list"}), "Jobs"),
    ],
)
def test_outer_shell_wrong_tenant_and_invalid_contracts_fail(html, message):
    with pytest.raises(SourceSchemaError, match=message):
        PaylocitySource().parse(html, PROCURE)


def test_invalid_job_location_shape_is_malformed_not_fabricated():
    posting = _job(101)
    posting["JobLocation"] = "Atlanta"

    with pytest.raises(SourceSchemaError, match="none were valid"):
        PaylocitySource().parse(_html([posting]), PROCURE)
