"""Alibaba's official campus batches provide practical-partial coverage only."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.app.hosted.catalog import CompanyCatalog
from watcher.collection_concurrency import direct_origin_key
from watcher.config import CompanyCfg
from watcher.sources.alibaba import (
    AlibabaSource,
    BATCH_ENDPOINT,
    PAGE_SIZE,
    SOURCE_URL,
)
from watcher.sources.contracts import SourceError, SourceSchemaError
from watcher.sources.registry import (
    DIRECT_ATS,
    DIRECT_COMPLETE_ATS,
    DIRECT_PRACTICAL_PARTIAL_ATS,
    build_direct_sources,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str):
    path = FIXTURES / name
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    return path.read_text(encoding="utf-8")


def company(source_url: str = SOURCE_URL) -> CompanyCfg:
    return CompanyCfg(name="Alibaba", ats="alibaba", source_url=source_url)


def terminal_payload() -> dict:
    return {
        "success": True,
        "errorMsg": None,
        "errorCode": None,
        "content": {
            "datas": None,
            "totalCount": 0,
            "pageSize": 0,
            "currentPage": 0,
        },
    }


def listing_payload(batch_id: int) -> dict:
    payload = fixture("alibaba_internships.json")
    record = payload["content"]["datas"][0]
    if batch_id == 100000560001:
        record.update(
            {
                "id": 199903980016,
                "name": "研究型实习生-多模态算法（音乐方向）-未来生活实验室",
                "batchName": "阿里巴巴研究型实习生",
                "batchId": batch_id,
                "circleNames": ["阿里巴巴控股集团"],
            }
        )
    return payload


def source(*, mutate=None, max_snapshot_passes: int = 3):
    calls = []

    def request_text(url: str, name: str):
        calls.append(("GET", url, None))
        assert name == "alibaba"
        return fixture("alibaba_bootstrap.html")

    def request_json(url: str, payload: dict, name: str, headers: dict):
        calls.append(("POST", url, copy.deepcopy(payload)))
        assert name == "alibaba"
        assert headers["Referer"] == SOURCE_URL
        if urlsplit(url).path == BATCH_ENDPOINT:
            response = fixture("alibaba_batches.json")
        elif payload["pageIndex"] == 2:
            response = terminal_payload()
        else:
            response = listing_payload(payload["batchId"])
        return mutate(url, payload, response, len(calls)) if mutate else response

    return (
        AlibabaSource(
            request_text=request_text,
            request_json=request_json,
            max_snapshot_passes=max_snapshot_passes,
        ),
        calls,
    )


def test_official_internship_batches_are_useful_but_never_complete():
    src, calls = source()

    rows = src.fetch(company())

    assert len(rows) == 2
    assert len({row["extra"]["source_requisition_id"] for row in rows}) == 2
    assert len({row["source_url"] for row in rows}) == 2
    assert all(row["company"] == "Alibaba" for row in rows)
    assert all(row["extra"]["source_adapter"] == "alibaba" for row in rows)
    assert all(
        row["extra"]["source_completeness"] == "practical_partial"
        for row in rows
    )
    assert rows[0]["source_url"].startswith(
        "https://campus-talent.alibaba.com/campus/position/"
    )
    assert rows[0]["date_posted"] == ""
    assert rows[0]["extra"]["source_modified_date"] == "2026-07-29"
    assert rows[0]["location"] == "北京, 杭州"
    assert rows[0]["extra"]["active"] is True

    diagnostics = src.last_health_diagnostics
    assert diagnostics.succeeded is True
    assert diagnostics.retained_row_count == 2
    assert diagnostics.incomplete is True
    assert diagnostics.degraded is True
    assert diagnostics.complete is False
    assert diagnostics.truncated is False
    assert diagnostics.reason_codes == ("scope_not_completeness_proven",)
    assert src.snapshot_passes_requested == 2
    assert src.request_count == 11

    posts = [call for call in calls if call[0] == "POST"]
    assert len(posts) == 10
    for _method, url, payload in posts:
        assert parse_qs(urlsplit(url).query).keys() == {"_csrf"}
        if urlsplit(url).path == BATCH_ENDPOINT:
            assert payload == {}
        else:
            assert payload["channel"] == "new_campus_group_official_site"
            assert payload["language"] == "zh"
            assert payload["pageSize"] == PAGE_SIZE
            assert payload["batchId"] in {100000560001, 100000560002}


def test_snapshot_membership_must_stabilize_without_unioning():
    def mutate(_url, payload, response, call_number):
        response = copy.deepcopy(response)
        if payload and payload.get("pageIndex") == 1 and call_number > 6:
            response["content"]["datas"][0]["id"] += call_number
        return response

    src, _ = source(mutate=mutate, max_snapshot_passes=3)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda _url, payload, response, _call: (
                {**response, "success": False}
                if payload and payload.get("pageIndex") == 1
                else response
            ),
            "reported failure",
        ),
        (
            lambda _url, payload, response, _call: (
                {
                    **response,
                    "content": {**response["content"], "totalCount": 2},
                }
                if payload and payload.get("pageIndex") == 1
                else response
            ),
            "ended before",
        ),
        (
            lambda _url, payload, response, _call: (
                {
                    **response,
                    "content": {**response["content"], "currentPage": 3},
                }
                if payload and payload.get("pageIndex") == 1
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


def test_malformed_posting_fails_the_slice_instead_of_silently_dropping_it():
    def mutate(_url, payload, response, _call):
        response = copy.deepcopy(response)
        if payload and payload.get("pageIndex") == 1:
            response["content"]["datas"][0].pop("id")
        return response

    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match="posting ID"):
        src.fetch(company())


def test_batch_discovery_must_explicitly_publish_the_internship_category():
    def mutate(url, _payload, response, _call):
        response = copy.deepcopy(response)
        if urlsplit(url).path == BATCH_ENDPOINT:
            response["content"].pop("internship")
        return response

    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match="internship batch category"):
        src.fetch(company())


def test_explicitly_empty_internship_category_remains_partial():
    def mutate(url, _payload, response, _call):
        response = copy.deepcopy(response)
        if urlsplit(url).path == BATCH_ENDPOINT:
            response["content"]["internship"] = []
        return response

    src, _ = source(mutate=mutate)

    assert src.fetch(company()) == []
    assert src.last_health_diagnostics.complete is False
    assert src.last_health_diagnostics.degraded is True


def test_wrong_configured_scope_fails_before_collection():
    src, calls = source()

    with pytest.raises(SourceError, match="official campus internship listing"):
        src.fetch(company("https://talent.alibaba.com/en/off-campus/position-list"))
    assert calls == []


def test_registry_origin_and_catalog_cannot_promote_alibaba_to_direct_complete():
    src = build_direct_sources()["alibaba"]
    catalog = CompanyCatalog.from_watcher_config()
    alibaba = catalog.resolve("Alibaba")

    assert isinstance(src, AlibabaSource)
    assert "alibaba" in DIRECT_ATS
    assert DIRECT_PRACTICAL_PARTIAL_ATS == frozenset(
        {"alibaba", "ansys", "siemens"}
    )
    assert "alibaba" not in DIRECT_COMPLETE_ATS
    assert direct_origin_key("alibaba") == "https://campus-talent.alibaba.com"
    assert alibaba is not None
    assert alibaba.coverage == "direct_practical_partial"
    assert alibaba.coverage != "direct"
    assert alibaba.selectable is True
