"""Jane Street's official first-party JSON inventory is direct-complete."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from backend.app.hosted.catalog import CompanyCatalog
from watcher.collection_concurrency import direct_origin_key
from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceError, SourceSchemaError
from watcher.sources.direct import SinglePayloadDirectAdapter
from watcher.sources.jane_street import (
    CLOSED_INTERNSHIPS_URL,
    INVENTORY_URL,
    SOURCE_URL,
    JaneStreetSource,
)
from watcher.sources.registry import (
    DIRECT_ATS,
    DIRECT_COMPLETE_ATS,
    DIRECT_PRACTICAL_PARTIAL_ATS,
    build_direct_sources,
)


FIXTURES = Path(__file__).parent / "fixtures"


def inventory() -> list:
    return json.loads(
        (FIXTURES / "jane_street_main_jobs.json").read_text(encoding="utf-8")
    )


def closed() -> list:
    return json.loads(
        (FIXTURES / "jane_street_closed_internships.json").read_text(encoding="utf-8")
    )


def company(source_url: str = SOURCE_URL) -> CompanyCfg:
    return CompanyCfg(name="Jane Street", ats="jane_street", source_url=source_url)


def source(*, mutate=None, max_snapshot_passes: int = 3):
    calls: list[str] = []

    def request_json(url: str, name: str):
        calls.append(url)
        assert name == "jane_street"
        payload = closed() if url == CLOSED_INTERNSHIPS_URL else inventory()
        return mutate(url, payload, len(calls)) if mutate else payload

    return (
        JaneStreetSource(
            request_json=request_json,
            sleeper=lambda _delay: None,
            max_snapshot_passes=max_snapshot_passes,
            snapshot_delay_seconds=0,
        ),
        calls,
    )


def test_official_inventory_is_complete_and_canonical():
    src, calls = source()

    rows = src.fetch(company())

    assert len(rows) == 2
    assert len({row["extra"]["source_id"] for row in rows}) == 2
    assert len({row["source_url"] for row in rows}) == 2

    first = rows[0]
    assert first["company"] == "Jane Street"
    assert first["title"] == "Software Engineer"
    assert first["location"] == "NYC"
    assert first["internship_type"] == "Full-Time: Experienced"
    assert first["compensation"] == "250,000 - 300,000"
    assert first["source_url"] == (
        "https://www.janestreet.com/join-jane-street/position/8631912002/"
    )
    assert "Build systems that move markets." in first["description"]
    assert first["extra"]["source_id"] == "8631912002"
    assert first["extra"]["source_requisition_id"] == "jane_street:8631912002"
    assert first["extra"]["source_system"] == "jane_street_careers_json"
    assert first["extra"]["team"] == "Software Engineering"
    assert first["extra"]["category"] == "Technology"
    assert first["extra"]["active"] is True
    assert first["extra"]["closed_programme"] is False
    # The inventory publishes no posting date, so none is invented.
    assert first["date_posted"] == ""

    diagnostics = src.last_health_diagnostics
    assert diagnostics.succeeded is True
    assert diagnostics.complete is True
    assert diagnostics.degraded is False
    assert diagnostics.incomplete is False
    assert diagnostics.truncated is False
    assert diagnostics.retained_row_count == 2
    assert diagnostics.malformed_row_count == 0
    assert diagnostics.schema_error_row_count == 0
    assert src.snapshot_passes_requested == 2
    # Two passes, each reading the overlay and then the inventory.
    assert calls == [
        CLOSED_INTERNSHIPS_URL,
        INVENTORY_URL,
        CLOSED_INTERNSHIPS_URL,
        INVENTORY_URL,
    ]


def test_closed_internship_overlay_marks_rows_inactive_without_dropping_them():
    src, _calls = source()

    rows = src.fetch(company())
    intern = next(r for r in rows if r["internship_type"] == "Summer Internship")

    # The overlay narrows state only; the posting stays in the inventory.
    assert intern["extra"]["closed_programme"] is True
    assert intern["extra"]["active"] is False
    assert intern["source_url"].endswith("/4273643002/")
    assert intern["compensation"] == ""
    assert len(rows) == 2


def test_adapter_reuses_the_shared_single_payload_lifecycle():
    assert issubclass(JaneStreetSource, SinglePayloadDirectAdapter)


def test_non_official_source_url_is_rejected():
    src, _calls = source()

    with pytest.raises(SourceError):
        src.fetch(company("https://www.janestreet.com/join-jane-street/"))


def test_empty_inventory_is_a_clean_terminal_result():
    def mutate(url, payload, _call):
        return [] if url == INVENTORY_URL else payload

    src, _calls = source(mutate=mutate)

    assert src.fetch(company()) == []
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.retained_row_count == 0


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda rec: rec.update(id="8631912002"), id="string_id"),
        pytest.param(lambda rec: rec.update(id=0), id="zero_id"),
        pytest.param(lambda rec: rec.update(id=-5), id="negative_id"),
        pytest.param(lambda rec: rec.pop("id"), id="missing_id"),
        pytest.param(lambda rec: rec.update(position="  "), id="blank_position"),
        pytest.param(lambda rec: rec.update(category=None), id="no_category"),
        pytest.param(lambda rec: rec.update(availability=None), id="no_availability"),
        pytest.param(lambda rec: rec.update(city=""), id="blank_city"),
        pytest.param(lambda rec: rec.update(team=None), id="no_team"),
        pytest.param(lambda rec: rec.update(duration=None), id="no_duration"),
        pytest.param(lambda rec: rec.update(overview=""), id="blank_overview"),
        pytest.param(lambda rec: rec.update(min_salary=100000), id="numeric_salary"),
        pytest.param(lambda rec: rec.update(min_salary=None), id="one_sided_salary"),
        pytest.param(lambda rec: rec.update(max_salary="  "), id="blank_salary_bound"),
    ],
)
def test_malformed_inventory_records_fail_closed(corrupt):
    def mutate(url, payload, call):
        if url == INVENTORY_URL and call <= 2:
            corrupt(payload[0])
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="object_not_array"),
        pytest.param("[]", id="string"),
        pytest.param([["not", "an", "object"]], id="record_not_object"),
    ],
)
def test_non_array_inventory_payloads_fail_closed(payload):
    def mutate(url, original, _call):
        return copy.deepcopy(payload) if url == INVENTORY_URL else original

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda payload: payload.__setitem__(0, "closed"), id="not_object"),
        pytest.param(
            lambda payload: payload[0].update(status="open"), id="non_closed_entry"
        ),
        pytest.param(lambda payload: payload[0].pop("position"), id="no_position"),
        pytest.param(lambda payload: payload[0].pop("location"), id="no_location"),
        pytest.param(lambda payload: payload[0].pop("duration"), id="no_duration"),
        pytest.param(lambda payload: payload[0].pop("status"), id="no_status"),
    ],
)
def test_malformed_closed_overlay_fails_closed(corrupt):
    def mutate(url, payload, _call):
        if url == CLOSED_INTERNSHIPS_URL:
            corrupt(payload)
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_non_array_closed_overlay_fails_closed():
    def mutate(url, payload, _call):
        return {} if url == CLOSED_INTERNSHIPS_URL else payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_duplicate_posting_ids_fail_closed():
    def mutate(url, payload, _call):
        if url == INVENTORY_URL:
            payload[1]["id"] = payload[0]["id"]
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_inventory_that_never_settles_fails_closed():
    def mutate(url, payload, call):
        if url == INVENTORY_URL:
            payload[0]["id"] = 8631912000 + call
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_inventory_that_settles_after_one_change_is_accepted():
    def mutate(url, payload, call):
        if url == INVENTORY_URL and call > 2:
            payload[0]["id"] = 8631912999
        return payload

    src, calls = source(mutate=mutate)

    rows = src.fetch(company())

    assert {row["extra"]["source_id"] for row in rows} == {
        "8631912999",
        "4273643002",
    }
    assert src.snapshot_passes_requested == 3
    assert len(calls) == 6
    assert src.last_health_diagnostics.complete is True


def test_oversized_payloads_fail_closed():
    def mutate(url, payload, _call):
        if url == INVENTORY_URL:
            return [dict(payload[0], id=1_000_000 + n) for n in range(20_001)]
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_constructor_rejects_out_of_contract_bounds():
    with pytest.raises(ValueError):
        JaneStreetSource(max_snapshot_passes=1)
    with pytest.raises(ValueError):
        JaneStreetSource(snapshot_delay_seconds=6)
    with pytest.raises(ValueError):
        JaneStreetSource(snapshot_delay_seconds=True)


def test_registry_origin_and_catalog_classify_jane_street_as_direct_complete():
    src = build_direct_sources()["jane_street"]
    catalog = CompanyCatalog.from_watcher_config()
    jane = catalog.resolve("Jane Street")

    assert isinstance(src, JaneStreetSource)
    assert "jane_street" in DIRECT_ATS
    assert "jane_street" in DIRECT_COMPLETE_ATS
    assert "jane_street" not in DIRECT_PRACTICAL_PARTIAL_ATS
    assert direct_origin_key("jane_street") == "https://www.janestreet.com"
    assert jane is not None
    assert jane.coverage == "direct"
    assert jane.coverage != "direct_practical_partial"
    assert jane.selectable is True
