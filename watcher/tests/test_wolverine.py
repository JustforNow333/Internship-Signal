"""Wolverine's complete first-party Pinpoint inventory contract."""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from backend.app.hosted.catalog import CompanyCatalog
from watcher.collection_concurrency import direct_origin_key
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.config import CompanyCfg, load_watchlist
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    TextHttpResponse,
)
from watcher.sources.direct import SinglePayloadDirectAdapter
from watcher.sources.registry import DIRECT_ATS, DIRECT_COMPLETE_ATS, build_direct_sources
from watcher.sources.wolverine import (
    BOARD_URL,
    CAREERS_METADATA_URL,
    INVENTORY_URL,
    MAX_CAREERS_BYTES,
    MAX_INVENTORY_BYTES,
    MAX_SITEMAP_BYTES,
    SITEMAP_URL,
    SOURCE_URL,
    WolverineSource,
)


FIXTURES = Path(__file__).parent / "fixtures"


def inventory() -> dict:
    return json.loads(
        (FIXTURES / "wolverine_postings.json").read_text(encoding="utf-8")
    )


def careers_html() -> str:
    return (FIXTURES / "wolverine_careers.html").read_text(encoding="utf-8")


def sitemap_xml() -> str:
    return (FIXTURES / "wolverine_sitemap.xml").read_text(encoding="utf-8")


def company(source_url: str = SOURCE_URL) -> CompanyCfg:
    return CompanyCfg(
        name="Wolverine Trading",
        ats="wolverine",
        source_url=source_url,
        aliases=("Wolverine", "Wolverine Holdings"),
    )


def source(*, mutate=None, max_snapshot_passes: int = 3):
    calls: list[str] = []

    def changed(url: str, value):
        calls.append(url)
        copied = copy.deepcopy(value)
        return mutate(url, copied, len(calls)) if mutate else copied

    def request_json(url: str, name: str):
        assert name == "wolverine"
        assert url == INVENTORY_URL
        return changed(url, inventory())

    def request_text(url: str, name: str):
        assert name == "wolverine"
        assert url in {CAREERS_METADATA_URL, SITEMAP_URL}
        value = careers_html() if url == CAREERS_METADATA_URL else sitemap_xml()
        return changed(url, value)

    return (
        WolverineSource(
            request_json=request_json,
            request_text=request_text,
            sleeper=lambda _delay: None,
            snapshot_delay_seconds=0,
            max_snapshot_passes=max_snapshot_passes,
        ),
        calls,
    )


def test_complete_inventory_maps_canonical_rows_and_stabilizes_twice():
    src, calls = source()

    rows = src.fetch(company())

    assert len(rows) == 2
    assert [row["extra"]["source_id"] for row in rows] == [
        "wolverine:101",
        "wolverine:202",
    ]
    assert len({row["extra"]["source_id"] for row in rows}) == 2
    assert len({row["source_url"] for row in rows}) == 2
    first = rows[0]
    assert first["company"] == "Wolverine Trading"
    assert first["title"] == "C++ Software Engineer"
    assert first["location"] == "Chicago, IL"
    assert first["description"] == (
        "Build latency-sensitive trading systems.\n\n"
        "Design and test reliable software."
    )
    assert first["requirements"] == "Strong C++ and systems knowledge."
    assert first["compensation"] == "$100,000 - $150,000 / year"
    assert first["remote_status"] == "Onsite"
    assert first["internship_type"] == "Full Time"
    assert first["date_posted"] == ""
    assert first["deadline"] == ""
    assert first["extra"]["source_requisition_id"] == "wolverine:101"
    assert first["extra"]["pinpoint_requisition_id"] == "RID-0101"
    assert first["extra"]["pinpoint_posting_uuid"] == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert first["extra"]["department"] == "Technology"
    assert first["extra"]["division"] == "Wolverine Trading"
    assert first["extra"]["active"] is True

    assert src.snapshot_passes_requested == 2
    assert calls == [
        CAREERS_METADATA_URL,
        INVENTORY_URL,
        SITEMAP_URL,
        CAREERS_METADATA_URL,
        INVENTORY_URL,
        SITEMAP_URL,
    ]
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.degraded is False
    assert src.last_health_diagnostics.retained_row_count == 2


def test_adapter_reuses_the_single_payload_record_lifecycle():
    assert issubclass(WolverineSource, SinglePayloadDirectAdapter)


def test_non_official_source_url_is_rejected():
    src, _calls = source()

    with pytest.raises(SourceError, match="official open-positions"):
        src.fetch(company("https://careers.wolve.com/"))


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('"showPagination":false', '"showPagination":true', "pagination"),
        ('"pageSize":null', '"pageSize":10', "page size"),
        ('"url":"/postings.json"', '"url":"/postings.json?page=1"', "endpoint"),
        ('"enabledLocaleKeys":["en"]', '"enabledLocaleKeys":["en","fr"]', "locale"),
        ('"values":[]', '"values":["hidden"]', "filter"),
    ],
)
def test_listing_metadata_must_keep_one_unfiltered_non_paginated_scope(
    old, new, message
):
    def mutate(url, value, _call):
        return value.replace(old, new, 1) if url == CAREERS_METADATA_URL else value

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match=message):
        src.fetch(company())


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda payload: payload.update(meta={}), id="extra_envelope_key"),
        pytest.param(lambda payload: payload.update(data={}), id="data_not_list"),
        pytest.param(lambda payload: payload["data"][0].pop("title"), id="missing_title"),
        pytest.param(lambda payload: payload["data"][0].update(extra="drift"), id="record_drift"),
        pytest.param(lambda payload: payload["data"][0].update(id="0"), id="bad_id"),
        pytest.param(
            lambda payload: payload["data"][0].update(
                url="https://example.test/en/postings/11111111-1111-4111-8111-111111111111"
            ),
            id="foreign_url",
        ),
        pytest.param(
            lambda payload: payload["data"][0].update(path="/en/postings/not-a-uuid"),
            id="bad_path",
        ),
        pytest.param(
            lambda payload: payload["data"][0]["location"].pop("name"),
            id="bad_location",
        ),
        pytest.param(
            lambda payload: payload["data"][0]["job"].update(division=[]),
            id="bad_job_scope",
        ),
        pytest.param(
            lambda payload: payload["data"][0].update(compensation_visible="true"),
            id="bad_compensation_visibility",
        ),
        pytest.param(
            lambda payload: payload["data"][0].update(deadline_at="someday"),
            id="bad_deadline",
        ),
    ],
)
def test_schema_drift_or_malformed_records_fail_closed(corrupt):
    def mutate(url, value, _call):
        if url == INVENTORY_URL:
            corrupt(value)
        return value

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


def test_exact_duplicate_is_deduplicated_deterministically():
    def mutate(url, value, _call):
        if url == INVENTORY_URL:
            value["data"].append(copy.deepcopy(value["data"][0]))
        return value

    src, _calls = source(mutate=mutate)

    rows = src.fetch(company())

    assert [row["extra"]["source_id"] for row in rows] == [
        "wolverine:101",
        "wolverine:202",
    ]
    assert src.last_health_diagnostics.duplicate_row_count == 1
    assert src.last_health_diagnostics.complete is True


@pytest.mark.parametrize("kind", ["id", "url", "content"])
def test_conflicting_id_url_or_canonical_content_fails_closed(kind):
    def mutate(url, value, _call):
        if url != INVENTORY_URL:
            return value
        if kind == "id":
            value["data"][1]["id"] = value["data"][0]["id"]
        elif kind == "url":
            value["data"][1]["path"] = value["data"][0]["path"]
            value["data"][1]["url"] = value["data"][0]["url"]
        else:
            duplicate = copy.deepcopy(value["data"][0])
            duplicate["title"] = "Conflicting title"
            value["data"].append(duplicate)
        return value

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError, match="conflicting"):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


@pytest.mark.parametrize("kind", ["missing", "extra", "malformed"])
def test_sitemap_mismatch_or_malformed_index_never_publishes_partial_rows(kind):
    def mutate(url, value, _call):
        if url != SITEMAP_URL:
            return value
        if kind == "missing":
            return value.replace(
                "  <url>\n"
                "    <loc>https://careers.wolve.com/postings/"
                "22222222-2222-4222-8222-222222222222</loc>\n"
                "    <lastmod>2026-09-02</lastmod>\n"
                "  </url>\n",
                "",
            )
        if kind == "extra":
            return value.replace(
                "</urlset>",
                "<url><loc>https://careers.wolve.com/postings/"
                "33333333-3333-4333-8333-333333333333</loc></url></urlset>",
            )
        return "<urlset>"

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


def test_transient_cross_signal_mismatch_must_settle_twice_before_publish():
    def mutate(url, value, call):
        if url == SITEMAP_URL and call == 3:
            return value.replace(
                "  <url>\n"
                "    <loc>https://careers.wolve.com/postings/"
                "22222222-2222-4222-8222-222222222222</loc>\n"
                "    <lastmod>2026-09-02</lastmod>\n"
                "  </url>\n",
                "",
            )
        return value

    src, calls = source(mutate=mutate)

    assert len(src.fetch(company())) == 2
    assert src.snapshot_passes_requested == 3
    assert len(calls) == 9


def test_non_settling_complete_snapshots_fail_closed():
    def mutate(url, value, call):
        if url == INVENTORY_URL:
            value["data"][0]["title"] = f"Changing title {call}"
        return value

    src, _calls = source(mutate=mutate, max_snapshot_passes=2)

    with pytest.raises(SourceSchemaError, match="stabilize"):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


def test_stable_explicitly_empty_inventory_is_complete():
    def mutate(url, value, _call):
        if url == INVENTORY_URL:
            value["data"] = []
        elif url == SITEMAP_URL:
            value = (
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "</urlset>"
            )
        return value

    src, _calls = source(mutate=mutate)

    assert src.fetch(company()) == []
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.retained_row_count == 0


def test_any_constituent_request_failure_aborts_the_source():
    def mutate(url, value, _call):
        if url == SITEMAP_URL:
            raise SourceFetchError("sitemap unavailable")
        return value

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceFetchError, match="unavailable"):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


def test_default_transport_applies_explicit_response_bounds(monkeypatch):
    calls: list[tuple[str, int]] = []

    def json_request(url, name, *, max_response_bytes):
        assert name == "wolverine"
        calls.append((url, max_response_bytes))
        return JsonHttpResponse(payload=inventory(), metadata={})

    def text_request(url, name, *, max_response_bytes):
        assert name == "wolverine"
        calls.append((url, max_response_bytes))
        text = careers_html() if url == CAREERS_METADATA_URL else sitemap_xml()
        return TextHttpResponse(text=text, metadata={})

    monkeypatch.setattr("watcher.sources.wolverine.get_json_response", json_request)
    monkeypatch.setattr("watcher.sources.wolverine.get_text_response", text_request)

    rows = WolverineSource(sleeper=lambda _delay: None).fetch(company())

    assert len(rows) == 2
    assert calls == [
        (CAREERS_METADATA_URL, MAX_CAREERS_BYTES),
        (INVENTORY_URL, MAX_INVENTORY_BYTES),
        (SITEMAP_URL, MAX_SITEMAP_BYTES),
    ] * 2


def test_registry_config_package_concurrency_and_catalog_are_aligned():
    config = load_watchlist()
    cfg = next(item for item in config.companies if item.name == "Wolverine Trading")

    assert isinstance(build_direct_sources()["wolverine"], WolverineSource)
    assert "wolverine" in DIRECT_ATS
    assert "wolverine" in DIRECT_COMPLETE_ATS
    assert cfg.ats == "wolverine"
    assert cfg.source_url == SOURCE_URL
    assert direct_origin_key(cfg.ats) == BOARD_URL
    assert CompanyCatalog.from_watcher_config(config).resolve("Wolverine").coverage == "direct"

    without = replace(
        config,
        companies=tuple(
            item for item in config.companies if item.name != "Wolverine Trading"
        ),
    )
    assert collection_config_fingerprint(config) != collection_config_fingerprint(without)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_snapshot_passes": 1}, "snapshot"),
        ({"max_snapshot_passes": 4}, "snapshot"),
        ({"snapshot_delay_seconds": -1}, "delay"),
        ({"snapshot_delay_seconds": 6}, "delay"),
    ],
)
def test_safeguard_configuration_is_bounded(kwargs, message):
    with pytest.raises(ValueError, match=message):
        WolverineSource(**kwargs)
