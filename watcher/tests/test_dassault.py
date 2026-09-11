"""Dassault fixtures and contract tests derive from its official 3ds.com index."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.config.models import CompanyCfg
from watcher.sources.contracts import SourceSchemaError
from watcher.sources.dassault import DassaultSource


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_payload() -> dict:
    return json.loads(
        (FIXTURES / "dassault_search.json").read_text(encoding="utf-8")
    )


def page(hits: list[dict], *, total: int | None = None, estimated: bool = False) -> dict:
    count = len(hits) if total is None else total
    return {
        "autocorrected": False,
        "estimated": estimated,
        "nhits": count,
        "nmatches": count,
        "start": 0,
        "hits": copy.deepcopy(hits),
    }


def terminal(payload: dict, *, estimated: bool = False) -> dict:
    result = page([], total=payload["nhits"], estimated=estimated)
    result["start"] = payload["nhits"]
    return result


def company() -> CompanyCfg:
    return CompanyCfg(name="Dassault Systèmes", ats="dassault")


def source(responses: list[dict], **kwargs) -> tuple[DassaultSource, list[dict]]:
    calls: list[dict] = []

    def request(url: str, name: str) -> dict:
        assert name == "dassault"
        calls.append(parse_qs(urlsplit(url).query))
        return copy.deepcopy(responses[len(calls) - 1])

    return DassaultSource(request_json=request, **kwargs), calls


def stable_responses(payload: dict) -> list[dict]:
    return [
        payload,
        terminal(payload),
        {**payload, "hits": list(reversed(payload["hits"]))},
        terminal(payload),
    ]


def test_atomic_inventory_maps_fields_and_uses_the_official_contract():
    payload = fixture_payload()
    src, calls = source(stable_responses(payload))

    rows = src.fetch(company())

    assert [row["extra"]["source_requisition_id"] for row in rows] == [
        "549677",
        "549700",
    ]
    assert rows[0]["date_posted"] == "2026-09-10"
    assert rows[0]["location"] == "France, Plouzane"
    assert rows[0]["internship_type"] == "Internship"
    assert rows[0]["description"] == "Develop a customer services platform."
    assert rows[0]["extra"]["source_adapter"] == "dassault"
    assert rows[1]["source_url"].endswith("/ing%C3%A9nieur-software-549700")
    assert src.last_health_diagnostics.complete
    assert src.request_count == 4
    assert [call["b"] for call in calls] == [["0"], ["2"], ["0"], ["2"]]
    assert [call["hf"] for call in calls] == [["1000"], ["1"], ["1000"], ["1"]]
    assert all(call["output_format"] == ["json"] for call in calls)
    assert all(call["s"] == ["desc(card_content_start_datetime)"] for call in calls)
    assert all('card_content_type="career"' in call["q"][0] for call in calls)


def test_approximate_atomic_total_is_allowed_only_with_an_exact_terminal_probe():
    payload = fixture_payload()
    responses = [
        {**payload, "estimated": True},
        terminal(payload, estimated=True),
        terminal(payload),
        payload,
        terminal(payload),
    ]
    src, calls = source(responses)

    assert len(src.fetch(company())) == 2
    assert len(calls) == 5


def test_repeated_multivalue_facets_do_not_conflict_with_required_metadata():
    payload = fixture_payload()
    for hit in payload["hits"]:
        hit["metas"].extend(
            [
                {"name": "meta_cat", "value": "Type/Internship"},
                {"name": "meta_cat", "value": "Country/France"},
                {"name": "content_categories_facet", "value": "Cards Language/en"},
                {"name": "content_categories_facet", "value": "Cards type/career"},
            ]
        )
    src, _ = source(stable_responses(payload))

    assert len(src.fetch(company())) == 2


def test_explicit_exact_zero_must_repeat_before_being_accepted():
    empty = page([])
    src, calls = source([empty, empty])

    assert src.fetch(company()) == []
    assert len(calls) == 2
    assert src.last_health_diagnostics.complete


def test_unstable_complete_snapshots_fail_closed():
    payload = fixture_payload()
    changed = copy.deepcopy(payload)
    changed["hits"][0]["metas"][2]["value"] = "Changed title"
    src, _ = source(
        [payload, terminal(payload), changed, terminal(changed)],
        max_snapshot_passes=2,
    )

    with pytest.raises(SourceSchemaError, match="stable complete inventory"):
        src.fetch(company())
    assert not src.last_health_diagnostics.complete


def test_only_approximate_terminal_probes_fail_closed():
    payload = fixture_payload()
    src, calls = source(
        [payload, terminal(payload, estimated=True), terminal(payload, estimated=True)],
        max_terminal_probes=2,
    )

    with pytest.raises(SourceSchemaError, match="exact terminal total"):
        src.fetch(company())
    assert len(calls) == 3


@pytest.mark.parametrize("kind", [
    "over_limit",
    "omission",
    "duplicate_id",
    "duplicate_url",
    "total_drift",
    "outside_scope",
    "bad_url",
    "bad_date",
    "autocorrected",
])
def test_inventory_contract_violations_publish_no_partial_result(kind: str):
    payload = fixture_payload()
    full = copy.deepcopy(payload)
    end = terminal(payload)
    if kind == "over_limit":
        full["nhits"] = full["nmatches"] = 1001
    elif kind == "omission":
        full["hits"].pop()
    elif kind == "duplicate_id":
        full["hits"][1]["metas"][0]["value"] = "549677"
        full["hits"][1]["metas"][1]["value"] = "549677"
    elif kind == "duplicate_url":
        full["hits"][1]["metas"][7]["value"] = full["hits"][0]["metas"][7]["value"]
    elif kind == "total_drift":
        end["nhits"] = end["nmatches"] = 3
    elif kind == "outside_scope":
        full["hits"][0]["metas"][8]["value"] = "article"
    elif kind == "bad_url":
        full["hits"][0]["metas"][7]["value"] = "https://example.com/jobs/549677"
    elif kind == "bad_date":
        full["hits"][0]["metas"][5]["value"] = "September 10"
    else:
        full["autocorrected"] = True
    src, _ = source([full, end])

    with pytest.raises(SourceSchemaError):
        src.fetch(company())
    assert not src.last_health_diagnostics.complete


def test_registry_catalog_origin_and_replay_are_aligned():
    from dataclasses import replace

    from backend.app.hosted.catalog import CompanyCatalog
    from watcher.collection_concurrency import direct_origin_key
    from watcher.collection_snapshot import collection_config_fingerprint
    from watcher.config.loader import load_watchlist
    from watcher.sources.registry import build_direct_sources

    cfg = load_watchlist()
    assert isinstance(build_direct_sources()["dassault"], DassaultSource)
    assert direct_origin_key("dassault") == "https://www.3ds.com"
    catalog = CompanyCatalog.from_watcher_config(cfg)
    assert catalog.resolve("Dassault Systems").coverage == "direct"
    without = replace(
        cfg,
        companies=tuple(c for c in cfg.companies if c.name != "Dassault Systèmes"),
    )
    assert collection_config_fingerprint(cfg) != collection_config_fingerprint(without)
