"""Huawei's official campus internship slices are practical-partial only."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backend.app.hosted.catalog import CompanyCatalog
from watcher.collection_concurrency import direct_origin_key
from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceError, SourceSchemaError
from watcher.sources.huawei import (
    API_URL,
    PAGE_SIZE,
    SOURCE_URL,
    HuaweiSource,
)
from watcher.sources.registry import (
    DIRECT_ATS,
    DIRECT_COMPLETE_ATS,
    DIRECT_PRACTICAL_PARTIAL_ATS,
    build_direct_sources,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture() -> dict:
    return json.loads(
        (FIXTURES / "huawei_internships.json").read_text(encoding="utf-8")
    )


def company(source_url: str = SOURCE_URL) -> CompanyCfg:
    return CompanyCfg(name="Huawei", ats="huawei", source_url=source_url)


def terminal_payload() -> dict:
    return {
        "status": "SUCCESS",
        "data": {"pageVO": None, "result": None},
        "errors": None,
    }


def source(*, mutate=None, max_snapshot_passes: int = 3):
    calls = []

    def request_json(url: str, payload: dict, name: str, headers: dict):
        calls.append((url, copy.deepcopy(payload), dict(headers)))
        assert url == API_URL
        assert name == "huawei"
        language = headers["x-language"]
        response = (
            terminal_payload()
            if payload["curPage"] == 2
            else fixture()[language]
        )
        return mutate(payload, headers, response, len(calls)) if mutate else response

    return (
        HuaweiSource(
            request_json=request_json,
            max_snapshot_passes=max_snapshot_passes,
        ),
        calls,
    )


def test_official_internship_locales_are_useful_but_never_complete():
    src, calls = source()

    rows = src.fetch(company())

    assert len(rows) == 2
    assert len({row["extra"]["source_requisition_id"] for row in rows}) == 2
    assert len({row["source_url"] for row in rows}) == 2
    assert {row["extra"]["source_locale"] for row in rows} == {"en", "cn"}
    assert all(row["company"] == "Huawei" for row in rows)
    assert all(row["extra"]["source_adapter"] == "huawei" for row in rows)
    assert all(
        row["extra"]["source_completeness"] == "practical_partial"
        for row in rows
    )
    assert rows[0]["source_url"] == (
        "https://career.huawei.com/en/job-details?advertisementId=41841"
    )
    assert rows[0]["date_posted"] == ""
    assert rows[0]["extra"]["source_modified_date"] == "2026-09-10"
    assert rows[0]["location"] == "Mexico/Mexico City"
    assert rows[0]["description"].startswith("Support the coordination")
    assert rows[1]["requirements"] == "具备良好的编程与团队协作能力。"

    diagnostics = src.last_health_diagnostics
    assert diagnostics.succeeded is True
    assert diagnostics.retained_row_count == 2
    assert diagnostics.incomplete is True
    assert diagnostics.degraded is True
    assert diagnostics.complete is False
    assert diagnostics.truncated is False
    assert diagnostics.reason_codes == ("scope_not_completeness_proven",)
    assert src.snapshot_passes_requested == 2
    assert src.request_count == 8

    assert len(calls) == 8
    for _url, payload, headers in calls:
        assert payload["pageSize"] == PAGE_SIZE
        assert payload["jobType"] == "CR"
        assert payload["recruitmentType"] == ["INTERN"]
        assert set(payload) == {"curPage", "pageSize", "jobType", "recruitmentType"}
        assert headers["X-HW-ID"] == "app_000000035886"
        assert headers["x-jalor-tenantAlias"] == "hcm"
        assert headers["x-alb-gray"] == "prod"
        assert headers["Origin"] == "https://career.huawei.com"
        assert headers["Referer"].startswith("https://career.huawei.com/")
        assert "Cookie" not in headers
        assert "Authorization" not in headers


def test_snapshot_membership_must_stabilize_without_unioning():
    def mutate(payload, _headers, response, call_number):
        response = copy.deepcopy(response)
        if payload["curPage"] == 1 and call_number > 4:
            response["data"]["result"][0]["advertisementId"] += call_number
        return response

    src, _ = source(mutate=mutate, max_snapshot_passes=3)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda payload, _headers, response, _call: (
                {**response, "status": "ERROR"}
                if payload["curPage"] == 1
                else response
            ),
            "reported failure",
        ),
        (
            lambda payload, _headers, response, _call: (
                {
                    **response,
                    "data": {
                        **response["data"],
                        "pageVO": {
                            **response["data"]["pageVO"],
                            "totalRows": 2,
                            "totalPages": 1,
                        },
                    },
                }
                if payload["curPage"] == 1
                else response
            ),
            "page count",
        ),
        (
            lambda payload, _headers, response, _call: (
                {
                    **response,
                    "data": {
                        **response["data"],
                        "pageVO": {
                            **response["data"]["pageVO"],
                            "curPage": 3,
                        },
                    },
                }
                if payload["curPage"] == 1
                else response
            ),
            "current page",
        ),
    ],
)
def test_listing_failures_and_inconsistent_pagination_fail_closed(mutate, message):
    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match=message):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


def test_malformed_posting_fails_the_slice_instead_of_dropping_it():
    def mutate(payload, _headers, response, _call):
        response = copy.deepcopy(response)
        if payload["curPage"] == 1:
            response["data"]["result"][0].pop("advertisementId")
        return response

    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match="posting ID"):
        src.fetch(company())


def test_cross_locale_posting_id_overlap_fails_closed():
    def mutate(payload, headers, response, _call):
        response = copy.deepcopy(response)
        if payload["curPage"] == 1 and headers["x-language"] == "zh_CN":
            response["data"]["result"][0]["advertisementId"] = 41841
        return response

    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match="duplicate posting ID across locales"):
        src.fetch(company())


def test_explicitly_empty_locale_slices_remain_partial():
    def mutate(_payload, _headers, _response, _call):
        return terminal_payload()

    src, _ = source(mutate=mutate)

    assert src.fetch(company()) == []
    assert src.request_count == 4
    assert src.last_health_diagnostics.complete is False
    assert src.last_health_diagnostics.degraded is True


def test_wrong_configured_scope_fails_before_collection():
    src, calls = source()

    with pytest.raises(SourceError, match="official campus internship listing"):
        src.fetch(company("https://career.huawei.com/en/social-recruitment-job-list"))
    assert calls == []


def test_registry_origin_and_catalog_cannot_promote_huawei_to_direct_complete():
    src = build_direct_sources()["huawei"]
    catalog = CompanyCatalog.from_watcher_config()
    huawei = catalog.resolve("Huawei")

    assert isinstance(src, HuaweiSource)
    assert "huawei" in DIRECT_ATS
    assert DIRECT_PRACTICAL_PARTIAL_ATS == frozenset(
        {"alibaba", "ansys", "huawei", "siemens"}
    )
    assert "huawei" not in DIRECT_COMPLETE_ATS
    assert direct_origin_key("huawei") == "https://apigw-dgg-b0.huawei.com"
    assert huawei is not None
    assert huawei.coverage == "direct_practical_partial"
    assert huawei.coverage != "direct"
    assert huawei.selectable is True
