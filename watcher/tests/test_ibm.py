from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, WatcherConfig, load_watchlist
from watcher.run import CollectionStats, _default_direct_sources, collect_rows
from watcher.sources import IbmSource, SourceFetchError, SourceSchemaError


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def company() -> CompanyCfg:
    return CompanyCfg(
        name="IBM",
        ats="ibm",
        alumni_match=("ibm",),
        source_url="https://www.ibm.com/careers/search",
    )


def page(*documents: object, total: int | None = None, start: int = 0) -> dict:
    return {
        "resultset": {
            "version": "1",
            "searchresults": {
                "totalresults": len(documents) if total is None else total,
                "startindex": start,
                "numresults": len(documents),
                "smresults": 0,
                "searchresultlist": list(documents),
            },
        }
    }


def document(job_id: str, *, title: str = "Software Developer Intern") -> dict:
    return {
        "resultnum": 0,
        "id": (job_id[-1:] or "a") * 64,
        "title": title,
        "description": "<p>Build reliable software.</p>",
        "language": "en",
        "summary": "",
        "url": f"https://careers.ibm.com/careers/JobDetail?jobId={job_id}",
        "docattributes": [
            {"country": "us"},
            {"effectivedate": "2026-08-01"},
            {"field_keyword_05": "United States"},
            {"field_keyword_08": "Software Engineering"},
            {"field_keyword_17": "Hybrid"},
            {"field_keyword_18": "Intern"},
            {"field_keyword_19": "Austin, TX"},
            {"field_text_01": job_id},
        ],
        "highlightedtext": {},
    }


def set_attribute(item: dict, name: str, value: object) -> None:
    item["docattributes"] = [
        {name: value} if name in attribute else attribute
        for attribute in item["docattributes"]
    ]


def pass_pages(*job_ids: str, page_size: int = 2) -> list[dict]:
    documents = [document(job_id) for job_id in job_ids]
    for result_number, item in enumerate(documents):
        item["resultnum"] = result_number
    return [
        page(
            *documents[start : start + page_size],
            total=len(documents),
            start=start,
        )
        for start in range(0, len(documents), page_size)
    ]


def test_single_page_maps_trustworthy_fields_and_native_identity():
    source = IbmSource(request_json=lambda *_: page(document("101")))

    rows = source.fetch(company())

    assert len(rows) == 1
    row = rows[0]
    assert row["company"] == "IBM"
    assert row["title"] == "Software Developer Intern"
    assert row["description"] == "Build reliable software."
    assert row["location"] == "Austin, TX; United States"
    assert row["date_posted"] == "2026-08-01"
    assert row["deadline"] == ""
    assert row["remote_status"] == "Hybrid"
    assert row["internship_type"] == "Intern"
    assert row["source_url"] == (
        "https://careers.ibm.com/careers/JobDetail?jobId=101"
    )
    assert row["extra"]["source_requisition_id"] == "ibm:101"
    assert row["extra"]["ibm_native_id"] == "101"
    assert row["extra"]["country_code"] == "us"
    assert row["extra"]["team"] == "Software Engineering"


def test_fixture_pages_parse_without_network_and_preserve_optional_fields():
    one_pass = [fixture("ibm_page_1.json"), fixture("ibm_page_2.json")]
    payloads = iter(one_pass + one_pass)
    source = IbmSource(request_json=lambda *_: next(payloads), page_size=2)

    rows = source.fetch(company())

    assert [row["extra"]["ibm_native_id"] for row in rows] == ["101", "102", "103"]
    assert rows[0]["extra"].get("opaque_search_rank") is None
    assert rows[1]["location"] == "Toronto, ON; Canada"


def test_multi_page_uses_fr_nr_and_one_based_page_parameters():
    one_pass = [fixture("ibm_page_1.json"), fixture("ibm_page_2.json")]
    payloads = iter(one_pass + one_pass)
    urls = []

    def request_json(url: str, source_name: str):
        urls.append(url)
        assert source_name == "ibm"
        return next(payloads)

    source = IbmSource(request_json=request_json, page_size=2)
    source.fetch(company())

    queries = [parse_qs(urlsplit(url).query, keep_blank_values=True) for url in urls]
    assert [query["fr"] for query in queries] == [["0"], ["2"], ["0"], ["2"]]
    assert [query["nr"] for query in queries] == [["2"]] * 4
    assert [query["page"] for query in queries] == [["1"], ["2"], ["1"], ["2"]]
    assert all(query["appid"] == ["careers"] for query in queries)
    assert all(query["scope"] == ["careers2"] for query in queries)
    assert source.pages_requested == 4
    assert source.documents_seen == 6
    assert source.snapshot_passes_requested == 2


def test_endpoint_uses_deterministic_posting_url_sort():
    query = parse_qs(
        urlsplit(IbmSource.endpoint(start=0, results=100, page=1)).query,
        keep_blank_values=True,
    )

    assert query["sortby"] == ["url"]


@pytest.mark.parametrize("max_snapshot_passes", [1, 4])
def test_snapshot_pass_bound_is_strict(max_snapshot_passes):
    with pytest.raises(ValueError, match="max_snapshot_passes"):
        IbmSource(max_snapshot_passes=max_snapshot_passes)


def test_one_unstable_pass_then_two_matching_passes_converges():
    unstable = [
        page(document("101"), document("102"), total=3, start=0),
        page(document("103"), total=4, start=2),
    ]
    stable = pass_pages("101", "102", "103")
    responses = iter(unstable + stable + stable)
    source = IbmSource(
        request_json=lambda *_: next(responses),
        page_size=2,
        max_snapshot_passes=3,
    )

    rows = source.fetch(company())

    assert [row["extra"]["ibm_native_id"] for row in rows] == ["101", "102", "103"]
    assert source.snapshot_passes_requested == 3


def test_complete_job_set_change_then_consecutive_convergence_succeeds():
    first = pass_pages("101", "102", "103")
    stable = pass_pages("101", "102", "104")
    responses = iter(first + stable + stable)
    source = IbmSource(
        request_json=lambda *_: next(responses),
        page_size=2,
        max_snapshot_passes=3,
    )

    rows = source.fetch(company())

    assert [row["extra"]["ibm_native_id"] for row in rows] == ["101", "102", "104"]


def test_persistent_complete_job_set_instability_fails():
    responses = iter(
        pass_pages("101", "102", "103")
        + pass_pages("101", "102", "104")
        + pass_pages("101", "102", "105")
    )
    source = IbmSource(
        request_json=lambda *_: next(responses),
        page_size=2,
        max_snapshot_passes=3,
    )

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())


def test_page_boundary_movement_violating_url_order_fails():
    ordered = pass_pages("101", "102", "103", "104")
    moved = [
        page(document("101"), document("103"), total=4, start=0),
        page(document("102"), document("104"), total=4, start=2),
    ]
    responses = iter(ordered + moved)
    source = IbmSource(
        request_json=lambda *_: next(responses),
        page_size=2,
        max_snapshot_passes=3,
    )

    with pytest.raises(SourceSchemaError, match="URL ordering"):
        source.fetch(company())


def test_page_limit_fails_closed_before_an_incomplete_snapshot_can_compare():
    source = IbmSource(
        request_json=lambda *_: page(document("101"), total=2, start=0),
        page_size=1,
        max_pages=1,
    )

    with pytest.raises(SourceSchemaError, match="maximum page safeguard"):
        source.fetch(company())
    assert source.last_health_diagnostics.succeeded is None


def test_explicit_zero_result_is_successful_and_healthy():
    source = IbmSource(request_json=lambda *_: fixture("ibm_zero.json"))
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"ibm": source},
        stats=stats,
    )

    assert rows == []
    assert errors == []
    assert stats.source_attempts[0].succeeded is True
    assert stats.source_attempts[0].rows_returned == 0
    assert source.last_health_diagnostics.complete is True


def test_exact_duplicate_index_document_is_counted_and_collapsed():
    original = document("101")
    duplicate = deepcopy(original)
    duplicate["resultnum"] = 1
    source = IbmSource(request_json=lambda *_: page(original, duplicate))

    rows = source.fetch(company())

    assert len(rows) == 1
    assert source.last_health_diagnostics.duplicate_row_count == 1


def test_same_job_id_with_changed_document_is_a_conflict():
    changed = document("101", title="Different title")

    with pytest.raises(SourceSchemaError, match="conflicting jobId"):
        IbmSource(request_json=lambda *_: page(document("101"), changed)).fetch(company())


def test_same_job_id_with_changed_opaque_attribute_is_not_an_exact_duplicate():
    original = document("101")
    changed = deepcopy(original)
    changed["docattributes"].append({"opaque_rank": "changed"})

    with pytest.raises(SourceSchemaError, match="conflicting jobId"):
        IbmSource(request_json=lambda *_: page(original, changed)).fetch(company())


def test_same_index_document_id_cannot_identify_distinct_jobs():
    first = document("101")
    second = document("102")
    second["id"] = first["id"]

    with pytest.raises(SourceSchemaError, match="index document ID"):
        IbmSource(request_json=lambda *_: page(first, second)).fetch(company())


def test_malformed_record_is_skipped_but_valid_neighbor_is_retained():
    malformed = document("102")
    malformed["title"] = ""
    source = IbmSource(request_json=lambda *_: page(document("101"), malformed))

    rows = source.fetch(company())

    assert [row["extra"]["ibm_native_id"] for row in rows] == ["101"]
    assert source.last_health_diagnostics.schema_error_row_count == 1
    assert source.last_health_diagnostics.degraded is True


def test_nonempty_all_malformed_response_fails():
    malformed = document("101")
    malformed["docattributes"] = []

    with pytest.raises(SourceSchemaError, match="none were valid"):
        IbmSource(request_json=lambda *_: page(malformed)).fetch(company())


@pytest.mark.parametrize(
    "payloads",
    [
        [
            page(document("101"), document("102"), total=4, start=0),
            page(document("101"), document("102"), total=4, start=2),
        ],
        [
            page(document("101"), document("102"), total=3, start=0),
            page(total=3, start=2),
        ],
        [
            page(document("101"), document("102"), total=3, start=0),
            page(document("103"), total=4, start=2),
        ],
    ],
)
def test_repeated_premature_or_changing_pagination_fails(payloads):
    responses = iter(payloads * 3)
    source = IbmSource(request_json=lambda *_: next(responses), page_size=2)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())


def test_short_nonfinal_page_is_premature():
    source = IbmSource(
        request_json=lambda *_: page(document("101"), total=3, start=0),
        page_size=2,
    )

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())


@pytest.mark.parametrize(
    "url",
    [
        "https://www.ibm.com/careers/search",
        "https://careers.ibm.com/careers/JobDetail",
        "https://careers.ibm.com/careers/JobDetail?jobId=999",
        "https://example.com/careers/JobDetail?jobId=101",
        "https://careers.ibm.com/careers/JobDetail?jobId=101&source=search",
        "https://careers.ibm.com/",
    ],
)
def test_invalid_generic_or_mismatched_posting_urls_fail(url):
    item = document("101")
    item["url"] = url

    with pytest.raises(SourceSchemaError, match="none were valid"):
        IbmSource(request_json=lambda *_: page(item)).fetch(company())


def test_optional_and_opaque_location_metadata_is_not_guessed():
    item = document("101")
    set_attribute(item, "field_keyword_19", {"opaque": "location"})
    set_attribute(item, "field_keyword_05", ["opaque-country"])
    item["docattributes"].append({"field_keyword_30": "mystery"})

    row = IbmSource(request_json=lambda *_: page(item)).fetch(company())[0]

    assert row["location"] == ""
    assert "field_keyword_30" not in row["extra"]


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"resultset": []},
        {"resultset": {"searchresults": []}},
        page(total=-1),
    ],
)
def test_malformed_page_schema_fails(payload):
    with pytest.raises(SourceSchemaError):
        IbmSource(request_json=lambda *_: payload).fetch(company())


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("startindex", 1, "startindex"),
        ("numresults", 0, "numresults"),
    ],
)
def test_exact_page_metadata_is_required(field, value, message):
    payload = page(document("101"))
    payload["resultset"]["searchresults"][field] = value

    with pytest.raises(SourceSchemaError, match=message):
        IbmSource(request_json=lambda *_: payload).fetch(company())


def test_only_transient_fetch_errors_use_bounded_retries():
    calls = 0
    delays: list[float] = []

    def request_json(*_):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise SourceFetchError("temporary", retryable=True)
        return fixture("ibm_zero.json")

    source = IbmSource(
        request_json=request_json,
        sleeper=delays.append,
        jitter=lambda _low, _high: 0,
    )

    assert source.fetch(company()) == []
    assert calls == 4
    assert delays == [1.0, 3.0]
    assert source.request_attempts == 4
    assert source.retry_attempts == 2
    assert source.last_health_diagnostics.failed_request_count == 2
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.degraded is True
    assert source.last_health_diagnostics.complete is True


def test_exhausting_the_attempt_bound_fails_without_success_diagnostics():
    calls = 0
    delays: list[float] = []

    def request_json(*_):
        nonlocal calls
        calls += 1
        raise SourceFetchError("temporary", retryable=True)

    source = IbmSource(
        request_json=request_json,
        sleeper=delays.append,
        jitter=lambda _low, _high: 0,
    )

    with pytest.raises(SourceFetchError) as raised:
        source.fetch(company())

    assert calls == 3
    assert delays == [1.0, 3.0]
    assert source.request_attempts == 3
    assert source.retry_attempts == 2
    assert raised.value.attempt_count == 3
    assert raised.value.response_metadata == {"attempt": 3, "max_attempts": 3}
    assert source.last_health_diagnostics.succeeded is None


def test_permanent_fetch_error_is_not_retried():
    calls = 0
    delays: list[float] = []

    def request_json(*_):
        nonlocal calls
        calls += 1
        raise SourceFetchError("forbidden", retryable=False)

    source = IbmSource(request_json=request_json, sleeper=delays.append)
    with pytest.raises(SourceFetchError) as raised:
        source.fetch(company())
    assert calls == 1
    assert delays == []
    assert source.retry_attempts == 0
    assert raised.value.attempt_count == 1


def test_ibm_watchlist_migration_and_runtime_registration():
    ibm = next(
        item for item in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
        if item.name == "IBM"
    )

    assert ibm.ats == "ibm"
    assert ibm.module == ""
    assert ibm.alumni_match == ("ibm",)
    assert ibm.source_url == "https://www.ibm.com/careers/search"
    assert isinstance(_default_direct_sources()["ibm"], IbmSource)
    assert direct_origin_key("ibm") == "https://www-api.ibm.com"
