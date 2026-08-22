from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, WatcherConfig, load_watchlist
from watcher.run import CollectionStats, _default_direct_sources, collect_rows
from watcher.sources import BainSource, SourceFetchError, SourceSchemaError


def company() -> CompanyCfg:
    return CompanyCfg(
        name="Bain & Company",
        ats="bain",
        aliases=("Bain",),
        alumni_match=("bain & company", "bain"),
        source_url="https://www.bain.com/careers/find-a-role/",
    )


def posting(job_id: str, *, title: str = "Associate Consultant Intern") -> dict:
    return {
        "JobId": job_id,
        "JobTitle": title,
        "Link": f"/careers/find-a-role/position/?jobid={job_id}",
        "Location": ["Boston", "New York"],
        "JobDescription": "<p>Work with a case team.</p>",
        "EmployeeType": "Temporary Full-Time",
        "Categories": ["Consulting"],
    }


def page(*records: object, total: int | None = None) -> dict:
    return {
        "results": list(records),
        "totalResults": len(records) if total is None else total,
        "filters": [],
        "ctaLink": "",
    }


def test_single_page_maps_canonical_fields_and_stable_identity():
    source = BainSource(request_json=lambda *_: page(posting("10397")))

    rows = source.fetch(company())

    assert len(rows) == 1
    assert rows[0]["company"] == "Bain & Company"
    assert rows[0]["title"] == "Associate Consultant Intern"
    assert rows[0]["location"] == "Boston; New York"
    assert rows[0]["description"] == "Work with a case team."
    assert rows[0]["source_url"] == (
        "https://www.bain.com/careers/find-a-role/position/?jobid=10397"
    )
    assert rows[0]["extra"]["source_requisition_id"] == "bain:10397"
    assert rows[0]["extra"]["bain_native_id"] == "10397"


def test_multi_page_uses_zero_based_page_number_and_required_referer():
    payloads = [
        page(posting("1"), posting("2"), total=3),
        page(posting("3"), total=3),
    ]
    requests = []

    def request_json(url: str, source_name: str, headers: dict[str, str]):
        requests.append((url, source_name, headers))
        return payloads[len(requests) - 1]

    source = BainSource(request_json=request_json, page_size=2)
    rows = source.fetch(company())

    assert [row["extra"]["bain_native_id"] for row in rows] == ["1", "2", "3"]
    assert [parse_qs(urlsplit(item[0]).query)["start"] for item in requests] == [
        ["0"],
        ["1"],
    ]
    assert all(parse_qs(urlsplit(item[0]).query)["results"] == ["2"] for item in requests)
    assert all(item[1] == "bain" for item in requests)
    assert all(
        item[2] == {"Referer": "https://www.bain.com/careers/find-a-role/"}
        for item in requests
    )
    assert source.pages_requested == 2


def test_explicit_zero_result_board_is_successful_and_healthy():
    source = BainSource(request_json=lambda *_: page(total=0))
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"bain": source},
        stats=stats,
    )

    assert rows == []
    assert errors == []
    assert stats.source_attempts[0].succeeded is True
    assert stats.source_attempts[0].rows_returned == 0
    assert source.last_health_diagnostics.complete is True


def test_missing_optional_location_and_description_are_not_fabricated():
    record = posting("10403")
    record.pop("Location")
    record["JobDescription"] = None

    row = BainSource(request_json=lambda *_: page(record)).fetch(company())[0]

    assert row["location"] == ""
    assert row["description"] == ""


def test_program_detail_url_is_accepted_as_posting_specific_identity():
    record = posting("10403")
    record["Link"] = (
        "/careers/work-with-us/internships-programs/"
        "associate-consultant-internship/"
    )

    row = BainSource(request_json=lambda *_: page(record)).fetch(company())[0]

    assert row["source_url"].endswith("/associate-consultant-internship/")


def test_malformed_record_is_skipped_but_valid_neighbor_is_retained():
    source = BainSource(request_json=lambda *_: page(posting("1"), {"JobId": "2"}))

    rows = source.fetch(company())

    assert [row["extra"]["bain_native_id"] for row in rows] == ["1"]
    assert source.last_health_diagnostics.schema_error_row_count == 1
    assert source.last_health_diagnostics.degraded is True


def test_nonempty_all_malformed_response_fails():
    with pytest.raises(SourceSchemaError, match="none were valid"):
        BainSource(request_json=lambda *_: page({"JobId": "2"})).fetch(company())


@pytest.mark.parametrize(
    ("payloads", "message"),
    [
        ([page(posting("1"), total=2), page(posting("1"), total=2)], "repeated"),
        ([page(posting("1"), total=2), page(total=2)], "ended before"),
        ([page(posting("1"), total=2), page(posting("2"), total=3)], "changed"),
    ],
)
def test_incomplete_or_inconsistent_pagination_fails(payloads, message):
    responses = iter(payloads)
    source = BainSource(request_json=lambda *_: next(responses), page_size=1)

    with pytest.raises(SourceSchemaError, match=message):
        source.fetch(company())


def test_exact_duplicate_is_counted_and_collapsed():
    source = BainSource(
        request_json=lambda *_: page(posting("1"), deepcopy(posting("1")))
    )

    rows = source.fetch(company())

    assert len(rows) == 1
    assert source.last_health_diagnostics.duplicate_row_count == 1


def test_conflicting_duplicate_id_fails():
    conflicting = posting("1", title="Different title")
    source = BainSource(request_json=lambda *_: page(posting("1"), conflicting))

    with pytest.raises(SourceSchemaError, match="conflicting posting ID"):
        source.fetch(company())


def test_different_ids_cannot_share_one_posting_url():
    shared = (
        "/careers/work-with-us/internships-programs/"
        "associate-consultant-internship/"
    )
    first = posting("1")
    first["Link"] = shared
    second = posting("2")
    second["Link"] = shared

    with pytest.raises(SourceSchemaError, match="posting URL"):
        BainSource(request_json=lambda *_: page(first, second)).fetch(company())


@pytest.mark.parametrize(
    "link",
    [
        "https://example.com/careers/find-a-role/position/?jobid=1",
        "/careers/",
        "/careers/find-a-role/",
        "/careers/work-with-us/internships-programs/",
        "https://careers.bain.com/recruits/signin?folderId=1",
        "/careers/find-a-role/position/?jobid=999",
    ],
)
def test_invalid_generic_or_mismatched_urls_are_rejected(link):
    record = posting("1")
    record["Link"] = link

    with pytest.raises(SourceSchemaError, match="none were valid"):
        BainSource(request_json=lambda *_: page(record)).fetch(company())


def test_only_transient_fetch_failures_are_retried_with_a_bound():
    calls = 0
    delays: list[float] = []

    def request_json(*_):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise SourceFetchError("temporary", retryable=True)
        return page(total=0)

    source = BainSource(
        request_json=request_json,
        sleeper=delays.append,
        jitter=lambda _low, _high: 0,
    )

    assert source.fetch(company()) == []
    assert calls == 3
    assert delays == [1.0, 3.0]
    assert source.request_attempts == 3
    assert source.retry_attempts == 2
    assert source.last_health_diagnostics.failed_request_count == 2
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.degraded is True
    assert source.last_health_diagnostics.succeeded is True


def test_exhausting_the_attempt_bound_fails_the_fetch():
    calls = 0
    delays: list[float] = []

    def request_json(*_):
        nonlocal calls
        calls += 1
        raise SourceFetchError("temporary", retryable=True)

    source = BainSource(
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
    assert raised.value.response_metadata["attempt"] == 3
    assert raised.value.response_metadata["max_attempts"] == 3
    # No partial result: success diagnostics were never published.
    assert source.last_health_diagnostics.succeeded is None


def test_permanent_fetch_failure_is_not_retried():
    calls = 0
    delays: list[float] = []

    def request_json(*_):
        nonlocal calls
        calls += 1
        raise SourceFetchError("forbidden", retryable=False)

    source = BainSource(request_json=request_json, sleeper=delays.append)
    with pytest.raises(SourceFetchError) as raised:
        source.fetch(company())
    assert calls == 1
    assert delays == []
    assert source.retry_attempts == 0
    assert raised.value.attempt_count == 1


def test_bain_watchlist_migration_and_runtime_registration():
    bain = next(
        item for item in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
        if item.name == "Bain & Company"
    )

    assert bain.ats == "bain"
    assert bain.module == ""
    assert bain.aliases == ("Bain",)
    assert bain.alumni_match == ("bain & company", "bain")
    assert bain.source_url == "https://www.bain.com/careers/find-a-role/"
    assert isinstance(_default_direct_sources()["bain"], BainSource)
    assert direct_origin_key("bain") == "https://www.bain.com"
