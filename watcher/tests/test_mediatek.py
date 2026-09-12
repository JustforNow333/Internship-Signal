"""MediaTek's official careers tRPC inventory is direct-complete."""

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
from watcher.sources.mediatek import (
    LOCALE,
    PAGE_SIZE,
    SOURCE_URL,
    MediaTekSource,
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
        (FIXTURES / "mediatek_trpc_jobs.json").read_text(encoding="utf-8")
    )


def body(payload: dict) -> dict:
    return payload["result"]["data"]["json"]


def company(source_url: str = SOURCE_URL) -> CompanyCfg:
    return CompanyCfg(name="MediaTek", ats="mediatek", source_url=source_url)


def terminal_payload(*, page: int = 2, total: int = 2) -> dict:
    payload = fixture()
    listing = body(payload)
    listing["jobs"] = []
    listing["pagination"] = {
        "current_page": page,
        "total_pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
        "total_items": total,
    }
    return payload


def source(*, mutate=None, max_snapshot_passes: int = 3, max_pages: int = 100):
    calls: list[str] = []

    def request_json(url: str, name: str):
        calls.append(url)
        assert name == "mediatek"
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        page = json.loads(query["input"][0])["json"]["page"]
        response = fixture() if page == 1 else terminal_payload(page=page)
        return mutate(url, response, len(calls)) if mutate else response

    return (
        MediaTekSource(
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
    assert len({row["extra"]["source_id"] for row in rows}) == 2
    assert len({row["source_url"] for row in rows}) == 2
    assert rows[0]["company"] == "MediaTek"
    assert rows[0]["title"] == "2027 Campus_Software & Firmware Engineer"
    assert rows[0]["location"] == "HsinChu"
    assert rows[0]["date_posted"] == "2026-09-03"
    assert rows[0]["source_url"] == (
        "https://careers.mediatek.com/en/jobs/MTK120260731012"
    )
    assert rows[0]["extra"]["source_adapter"] == "mediatek"
    assert rows[0]["extra"]["source_requisition_id"] == "mediatek:MTK120260731012"
    assert rows[0]["extra"]["source_system"] == "mediatek_careers_trpc"
    assert rows[0]["extra"]["category"] == "Software"
    assert rows[0]["extra"]["work_experience"] == "No Work Expe."
    assert rows[0]["extra"]["program"] == "RDSS"
    assert rows[0]["extra"]["education"] == ["Master's Degree Computer Science"]
    assert rows[0]["extra"]["active"] is True
    # A posting with no program publishes an empty one rather than failing.
    assert rows[1]["extra"]["program"] == ""
    assert rows[1]["extra"]["education"] == []
    assert rows[1]["location"] == "Austin, TX"

    diagnostics = src.last_health_diagnostics
    assert diagnostics.succeeded is True
    assert diagnostics.complete is True
    assert diagnostics.degraded is False
    assert diagnostics.incomplete is False
    assert diagnostics.truncated is False
    assert diagnostics.retained_row_count == 2
    assert diagnostics.duplicate_row_count == 0
    assert diagnostics.malformed_row_count == 0
    assert diagnostics.schema_error_row_count == 0
    assert diagnostics.failed_request_count == 0
    # Two complete snapshots of one listing page plus its terminal page.
    assert src.snapshot_passes_requested == 2
    assert len(calls) == 4


def test_requests_are_the_unfiltered_first_party_listing_contract():
    src, calls = source()

    src.fetch(company())

    payload = json.loads(parse_qs(urlsplit(calls[0]).query)["input"][0])["json"]
    assert urlsplit(calls[0]).netloc == "careers.mediatek.com"
    assert urlsplit(calls[0]).path == "/api/trpc/job.getJobs"
    assert payload["locales"] == LOCALE
    assert payload["page"] == 1
    assert payload["limit"] == PAGE_SIZE
    assert payload["jobQueryInfo"] == {}
    assert payload["sortBy"] == "publishedDate"
    assert payload["order"] == "DESC"
    assert payload["filters"] == {
        "categorys": [],
        "workExperiences": [],
        "locations": [],
        "programs": [],
    }
    pages = [
        json.loads(parse_qs(urlsplit(url).query)["input"][0])["json"]["page"]
        for url in calls
    ]
    assert pages == [1, 2, 1, 2]


def test_endpoint_rejects_out_of_contract_paging():
    with pytest.raises(ValueError):
        MediaTekSource.endpoint(page=0)
    with pytest.raises(ValueError):
        MediaTekSource.endpoint(page=True)
    with pytest.raises(ValueError):
        MediaTekSource.endpoint(limit=PAGE_SIZE + 1)


def test_non_official_source_url_is_rejected():
    src, _calls = source()

    with pytest.raises(SourceError):
        src.fetch(company("https://careers.mediatek.com/zh-tw/jobs"))


def test_empty_inventory_is_a_clean_terminal_result():
    def mutate(_url, payload, _call):
        return terminal_payload(page=1, total=0)

    src, calls = source(mutate=mutate)

    assert src.fetch(company()) == []
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.retained_row_count == 0
    assert len(calls) == 2


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(lambda listing: listing.update(status="partial"), id="status"),
        pytest.param(
            lambda listing: listing.update(message="degraded"), id="message"
        ),
        pytest.param(lambda listing: listing.pop("pagination"), id="no_pagination"),
        pytest.param(
            lambda listing: listing["pagination"].update(total_items="2"),
            id="total_not_int",
        ),
        pytest.param(
            lambda listing: listing["pagination"].update(total_items=-1),
            id="total_negative",
        ),
        pytest.param(
            lambda listing: listing["pagination"].update(current_page=7),
            id="page_echo",
        ),
        pytest.param(
            lambda listing: listing["pagination"].update(total_pages=0),
            id="total_pages",
        ),
        pytest.param(lambda listing: listing.update(jobs={}), id="jobs_not_list"),
        pytest.param(lambda listing: listing.update(jobs=["x"]), id="job_not_object"),
        pytest.param(
            lambda listing: listing["jobs"][0].update(id="mtk120260731012"),
            id="lowercase_id",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0].update(id="MTK1202607310"),
            id="short_id",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0].update(jobPostStatus="draft"),
            id="unposted",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0].update(title="  "), id="blank_title"
        ),
        pytest.param(
            lambda listing: listing["jobs"][0].update(description=None),
            id="no_description",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0].update(publishedDate="soon"),
            id="bad_date",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0].update(properties=[]),
            id="bad_properties",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0]["properties"].update(location=None),
            id="no_location",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0]["properties"].update(
                category={"label": "9020", "code": "9021"}
            ),
            id="two_codes",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0]["properties"].update(
                category={"label": "Software", "code": "Software"}
            ),
            id="two_labels",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0]["properties"].update(
                category={"label": "Software"}
            ),
            id="partial_vocabulary",
        ),
        pytest.param(
            lambda listing: listing["jobs"][0]["properties"].update(
                jobEducationInfos=["Master"]
            ),
            id="bad_education",
        ),
    ],
)
def test_malformed_listing_payloads_fail_closed(corrupt):
    def mutate(_url, payload, call):
        if call == 1:
            corrupt(body(payload))
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="not_an_object"),
        pytest.param({"error": {"json": {"message": "bad"}}}, id="trpc_error"),
        pytest.param({"result": {}}, id="no_data"),
        pytest.param({"result": {"data": {}}}, id="no_json_body"),
    ],
)
def test_non_result_envelopes_fail_closed(payload):
    def mutate(_url, _payload, call):
        return copy.deepcopy(payload) if call == 1 else _payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_duplicate_posting_ids_fail_closed():
    def mutate(_url, payload, _call):
        listing = body(payload)
        if listing["jobs"]:
            listing["jobs"][1]["id"] = listing["jobs"][0]["id"]
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_page_shorter_than_the_advertised_total_fails_closed():
    def mutate(_url, payload, _call):
        listing = body(payload)
        if listing["jobs"]:
            listing["jobs"] = listing["jobs"][:1]
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_terminal_page_with_rows_fails_closed():
    def mutate(_url, payload, call):
        if call == 2:
            body(payload)["jobs"] = fixture()["result"]["data"]["json"]["jobs"]
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_total_drift_between_snapshots_fails_closed():
    def mutate(_url, payload, call):
        if call > 2:
            listing = body(payload)
            listing["pagination"]["total_items"] = 3
            listing["pagination"]["total_pages"] = 1
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_identity_drift_that_never_settles_fails_closed():
    def mutate(_url, payload, call):
        listing = body(payload)
        if listing["jobs"]:
            listing["jobs"][0]["id"] = f"MTK12026073{call:04d}"
        return payload

    src, _calls = source(mutate=mutate)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_inventory_that_settles_after_one_change_is_accepted():
    def mutate(_url, payload, call):
        listing = body(payload)
        if call > 2 and listing["jobs"]:
            listing["jobs"][0]["id"] = "MTK120260731099"
        return payload

    src, calls = source(mutate=mutate)

    rows = src.fetch(company())

    # The first pass disagreed, so two further matching passes were required.
    assert [row["extra"]["source_id"] for row in rows] == [
        "MTK120260731099",
        "MUSA20260812004",
    ]
    assert src.snapshot_passes_requested == 3
    assert len(calls) == 6
    assert src.last_health_diagnostics.complete is True


def test_advertised_total_beyond_the_page_safeguard_fails_closed():
    def mutate(_url, payload, _call):
        listing = body(payload)
        listing["pagination"]["total_items"] = 10_000
        listing["pagination"]["total_pages"] = 100
        return payload

    src, _calls = source(mutate=mutate, max_pages=1)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_constructor_rejects_out_of_contract_bounds():
    with pytest.raises(ValueError):
        MediaTekSource(max_pages=0)
    with pytest.raises(ValueError):
        MediaTekSource(max_snapshot_passes=1)
    with pytest.raises(ValueError):
        MediaTekSource(page_delay_seconds=6)
    with pytest.raises(ValueError):
        MediaTekSource(page_delay_seconds=True)


def test_registry_origin_and_catalog_classify_mediatek_as_direct_complete():
    src = build_direct_sources()["mediatek"]
    catalog = CompanyCatalog.from_watcher_config()
    mediatek = catalog.resolve("MediaTek")

    assert isinstance(src, MediaTekSource)
    assert "mediatek" in DIRECT_ATS
    assert "mediatek" in DIRECT_COMPLETE_ATS
    assert "mediatek" not in DIRECT_PRACTICAL_PARTIAL_ATS
    assert direct_origin_key("mediatek") == "https://careers.mediatek.com"
    assert mediatek is not None
    assert mediatek.coverage == "direct"
    assert mediatek.coverage != "direct_practical_partial"
    assert mediatek.selectable is True
