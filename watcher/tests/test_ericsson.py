"""Ericsson's official PCSX inventory is direct-complete."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.app.hosted.catalog import CompanyCatalog
from watcher.collection_concurrency import direct_origin_key
from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceError, SourceSchemaError
from watcher.sources.ericsson import PAGE_SIZE, SOURCE_URL, EricssonSource
from watcher.sources.registry import (
    DIRECT_ATS,
    DIRECT_COMPLETE_ATS,
    DIRECT_PRACTICAL_PARTIAL_ATS,
    build_direct_sources,
)


FIXTURES = Path(__file__).parent / "fixtures"


def fixture() -> dict:
    return json.loads(
        (FIXTURES / "ericsson_pcsx_jobs.json").read_text(encoding="utf-8")
    )


def company(source_url: str = SOURCE_URL) -> CompanyCfg:
    return CompanyCfg(name="Ericsson", ats="ericsson", source_url=source_url)


def terminal_payload(*, count: int = 2) -> dict:
    payload = fixture()
    payload["data"]["count"] = count
    payload["data"]["positions"] = []
    return payload


def source(*, mutate=None, max_snapshot_passes: int = 3, max_pages: int = 100):
    calls = []

    def request_json(url: str, name: str):
        calls.append(url)
        assert name == "ericsson"
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        start = int(query["start"][0])
        response = terminal_payload() if start == 2 else fixture()
        return mutate(url, response, len(calls)) if mutate else response

    return (
        EricssonSource(
            request_json=request_json,
            sleeper=lambda _delay: None,
            max_snapshot_passes=max_snapshot_passes,
            max_pages=max_pages,
            page_delay_seconds=0,
        ),
        calls,
    )


def test_official_global_inventory_is_complete_and_canonical():
    src, calls = source()

    rows = src.fetch(company())

    assert len(rows) == 2
    assert len({row["extra"]["source_requisition_id"] for row in rows}) == 2
    assert len({row["source_url"] for row in rows}) == 2
    assert rows[0]["company"] == "Ericsson"
    assert rows[0]["title"] == "Process Control Specialist"
    assert rows[0]["location"] == "Tallinn,Tallinn,Estonia"
    assert rows[0]["date_posted"] == "2026-09-11"
    assert rows[0]["source_url"] == (
        "https://jobs.ericsson.com/careers/job/563121776085293"
    )
    assert rows[0]["extra"]["source_adapter"] == "ericsson"
    assert rows[0]["extra"]["source_requisition_id"] == "ericsson:785483"
    assert rows[0]["extra"]["ericsson_pcsx_id"] == "563121776085293"
    assert rows[0]["extra"]["department"] == "83 Supply & Logistics"
    assert rows[0]["extra"]["job_functions"] == ["Supply & Logistics"]
    assert rows[0]["extra"]["active"] is True

    diagnostics = src.last_health_diagnostics
    assert diagnostics.succeeded is True
    assert diagnostics.retained_row_count == 2
    assert diagnostics.incomplete is False
    assert diagnostics.degraded is False
    assert diagnostics.complete is True
    assert diagnostics.truncated is False
    assert diagnostics.reason_codes == ()
    assert src.snapshot_passes_requested == 2
    assert src.pages_requested == 4
    assert src.request_attempts == 4

    assert len(calls) == 4
    for url in calls:
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        assert parsed.scheme == "https"
        assert parsed.hostname == "jobs.ericsson.com"
        assert parsed.path == "/api/pcsx/search"
        assert query == {
            "domain": ["ericsson.com"],
            "query": [""],
            "location": [""],
            "start": [query["start"][0]],
        }
        assert int(query["start"][0]) in {0, 2}
        assert "sort_by" not in query
        assert "num" not in query


def test_snapshot_membership_must_stabilize_without_unioning():
    def mutate(url, response, call_number):
        response = copy.deepcopy(response)
        start = int(parse_qs(urlsplit(url).query)["start"][0])
        if start == 0 and call_number > 2:
            response["data"]["positions"][0]["id"] += call_number
            response["data"]["positions"][0]["positionUrl"] = (
                f"/careers/job/{response['data']['positions'][0]['id']}"
            )
        return response

    src, _ = source(mutate=mutate, max_snapshot_passes=3)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


@pytest.mark.parametrize(
    "mutate, message",
    [
        (
            lambda _url, response, _call: {**response, "status": 500},
            "reported failure",
        ),
        (
            lambda _url, response, _call: {
                **response,
                "data": {**response["data"], "sortBy": "unexpected"},
            },
            "sort mode",
        ),
        (
            lambda _url, response, _call: {
                **response,
                "data": {**response["data"], "appliedFilters": {"skills": ["5G"]}},
            },
            "unexpected filters",
        ),
    ],
)
def test_failed_or_scope_changed_envelopes_fail_closed(mutate, message):
    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match=message):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


def test_malformed_posting_fails_the_inventory_instead_of_being_dropped():
    def mutate(url, response, _call):
        response = copy.deepcopy(response)
        if int(parse_qs(urlsplit(url).query)["start"][0]) == 0:
            response["data"]["positions"][0].pop("atsJobId")
        return response

    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match="ATS job ID"):
        src.fetch(company())


def test_duplicate_ids_fail_closed_instead_of_hiding_pagination_loss():
    def mutate(url, response, _call):
        response = copy.deepcopy(response)
        if int(parse_qs(urlsplit(url).query)["start"][0]) == 0:
            response["data"]["positions"][1] = copy.deepcopy(
                response["data"]["positions"][0]
            )
        return response

    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match="duplicate posting ID"):
        src.fetch(company())


def test_terminal_count_must_match_the_complete_snapshot():
    def mutate(url, response, _call):
        response = copy.deepcopy(response)
        if int(parse_qs(urlsplit(url).query)["start"][0]) == 2:
            response["data"]["count"] = 3
        return response

    src, _ = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match="total changed"):
        src.fetch(company())


def test_explicit_zero_inventory_is_complete_only_after_two_matching_passes():
    calls = []

    def request_json(url: str, name: str):
        calls.append(url)
        assert name == "ericsson"
        return terminal_payload(count=0)

    src = EricssonSource(request_json=request_json, sleeper=lambda _delay: None)

    assert src.fetch(company()) == []
    assert len(calls) == 2
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.degraded is False


def test_safety_cap_rejects_an_inventory_too_large_to_enumerate():
    def mutate(url, response, _call):
        response = copy.deepcopy(response)
        if int(parse_qs(urlsplit(url).query)["start"][0]) == 0:
            response["data"]["count"] = PAGE_SIZE + 1
        return response

    src, _ = source(mutate=mutate, max_pages=1)

    with pytest.raises(SourceSchemaError, match="maximum page safeguard"):
        src.fetch(company())


def test_wrong_configured_scope_fails_before_collection():
    src, calls = source()

    with pytest.raises(SourceError, match="official global careers listing"):
        src.fetch(company("https://jobs.ericsson.com/careers?query=internship"))
    assert calls == []


def test_registry_origin_and_catalog_classify_ericsson_as_direct_complete():
    src = build_direct_sources()["ericsson"]
    catalog = CompanyCatalog.from_watcher_config()
    ericsson = catalog.resolve("Ericsson")

    assert isinstance(src, EricssonSource)
    assert "ericsson" in DIRECT_ATS
    assert "ericsson" in DIRECT_COMPLETE_ATS
    assert "ericsson" not in DIRECT_PRACTICAL_PARTIAL_ATS
    assert direct_origin_key("ericsson") == "https://jobs.ericsson.com"
    assert ericsson is not None
    assert ericsson.coverage == "direct"
    assert ericsson.coverage != "direct_practical_partial"
    assert ericsson.selectable is True
