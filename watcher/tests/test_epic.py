import json
from pathlib import Path

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, WatcherConfig, load_watchlist
from watcher.run import CollectionStats, _default_direct_sources, collect_rows
from watcher.sources import EpicSource, SourceFetchError, SourceSchemaError


FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def company() -> CompanyCfg:
    return CompanyCfg(
        name="Epic",
        ats="epic",
        alumni_match=("epic",),
        source_url="https://careers.epic.com/jobs/",
    )


def posting(
    title: str = "Software Developer Intern - Summer 2027",
    *,
    summary: object = "An internship that makes an impact.",
    background: object = "Working toward a bachelor's degree",
    is_open: object = True,
    is_published: object = True,
    reference_number: object = 362331,
) -> dict:
    return {
        "externalName": title,
        "shortSummary": summary,
        "background": background,
        "isOpen": is_open,
        "isPublished": is_published,
        "referenceNumber": reference_number,
    }


def next_page(open_jobs: list[object], positions: dict[str, object]) -> str:
    payload = {
        "allOpenJobs": open_jobs,
        "avaturePositions": positions,
        "educationTypes": [],
        "positionTypes": [],
    }
    flight = "7:" + json.dumps(payload, separators=(",", ":"))
    return (
        "<!doctype html><html><body><script>self.__next_f.push("
        + json.dumps([1, flight], separators=(",", ":"))
        + ")</script></body></html>"
    )


def search_ids(*job_ids: str) -> list[dict[str, str]]:
    return [{"id": job_id} for job_id in job_ids]


def source_for(html: str, search: object) -> EpicSource:
    return EpicSource(
        request_text=lambda *_: html,
        request_json=lambda *_: search,
    )


def test_saved_official_contract_maps_canonical_fields_and_native_identity():
    source = source_for(
        fixture_text("epic_jobs_complete.html"),
        fixture_json("epic_search_complete.json"),
    )

    rows = source.fetch(company())

    assert [row["extra"]["epic_native_id"] for row in rows] == ["740", "30318"]
    intern = rows[1]
    assert intern["company"] == "Epic"
    assert intern["title"] == "Software Developer Intern - Summer 2027"
    assert intern["location"] == ""
    assert intern["description"] == "An internship that makes an impact."
    assert intern["requirements"] == "Working toward a bachelor's degree"
    assert intern["source_url"] == (
        "https://epic.avature.net/Careers/FolderDetail/"
        "Software-Developer-Intern---Summer-2027/30318"
    )
    assert intern["extra"]["source_requisition_id"] == "epic:30318"
    assert intern["extra"]["epic_reference_number"] == "362331"
    assert source.last_health_diagnostics.complete is True


def test_official_page_and_search_contracts_are_both_required():
    requests: list[tuple[str, str]] = []

    def request_text(url: str, source_name: str) -> str:
        requests.append((url, source_name))
        return next_page([{"id": "1"}], {"1": posting()})

    def request_json(url: str, source_name: str) -> object:
        requests.append((url, source_name))
        return search_ids("1")

    EpicSource(request_text=request_text, request_json=request_json).fetch(company())

    assert requests == [
        ("https://careers.epic.com/jobs/", "epic"),
        ("https://careers.epic.com/cached-api/jobs/search/", "epic"),
    ]


def test_explicit_empty_result_requires_both_official_contracts_to_be_empty():
    source = source_for(next_page([], {}), [])
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"epic": source},
        stats=stats,
    )

    assert rows == []
    assert errors == []
    assert stats.source_attempts[0].succeeded is True
    assert stats.source_attempts[0].rows_returned == 0
    assert source.last_health_diagnostics.complete is True


def test_malformed_record_is_skipped_but_valid_neighbor_is_retained():
    source = source_for(
        next_page(
            [{"id": "1"}, {"id": "2"}],
            {"1": posting(), "2": posting(title="")},
        ),
        search_ids("1", "2"),
    )

    rows = source.fetch(company())

    assert [row["extra"]["epic_native_id"] for row in rows] == ["1"]
    assert source.last_health_diagnostics.schema_error_row_count == 1
    assert source.last_health_diagnostics.degraded is True


def test_nonempty_all_malformed_result_fails():
    source = source_for(
        next_page([{"id": "1"}], {"1": posting(title="")}),
        search_ids("1"),
    )

    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(company())


def test_exact_duplicate_ids_are_collapsed_and_counted():
    source = source_for(
        next_page([{"id": "1"}, {"id": "1"}], {"1": posting()}),
        [{"id": "1"}, {"id": "1"}],
    )

    rows = source.fetch(company())

    assert len(rows) == 1
    assert source.last_health_diagnostics.duplicate_row_count == 2


def test_conflicting_duplicate_id_entries_fail():
    source = source_for(
        next_page(
            [{"id": "1", "marker": "first"}, {"id": "1", "marker": "second"}],
            {"1": posting()},
        ),
        search_ids("1"),
    )

    with pytest.raises(SourceSchemaError, match="conflicting posting ID"):
        source.fetch(company())


@pytest.mark.parametrize(
    ("page_ids", "api_ids"),
    [
        (("1",), ("1", "2")),
        (("1", "2"), ("1",)),
        ((), ("1",)),
        (("1",), ()),
    ],
)
def test_incomplete_or_changing_official_job_set_fails(page_ids, api_ids):
    positions = {job_id: posting() for job_id in page_ids}
    source = source_for(
        next_page([{"id": job_id} for job_id in page_ids], positions),
        search_ids(*api_ids),
    )

    with pytest.raises(SourceSchemaError, match="job sets did not match"):
        source.fetch(company())


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>Available Positions</body></html>",
        (
            '<html><a href="https://epic.avature.net/Careers/FolderDetail/'
            'Software-Developer/740">Software Developer</a></html>'
        ),
        "<html><script>self.__next_f.push(not-json)</script></html>",
    ],
)
def test_missing_or_standard_avature_listing_is_not_authoritative(html):
    with pytest.raises(SourceSchemaError, match="Next.js jobs contract"):
        source_for(html, search_ids("740")).fetch(company())


@pytest.mark.parametrize(
    "record",
    [
        posting(title="Careers"),
        posting(title="Search Jobs"),
        posting(title="Sign in"),
    ],
)
def test_generic_navigation_titles_cannot_become_posting_urls(record):
    source = source_for(
        next_page([{"id": "1"}], {"1": record}),
        search_ids("1"),
    )

    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(company())


def test_closed_or_unpublished_open_id_is_invalid():
    closed = posting(is_open=False)
    source = source_for(
        next_page([{"id": "1"}], {"1": closed}),
        search_ids("1"),
    )

    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(company())


def test_optional_summary_background_and_reference_are_not_fabricated():
    item = posting(summary=None, background=None, reference_number=None)
    row = source_for(
        next_page([{"id": "1"}], {"1": item}),
        search_ids("1"),
    ).fetch(company())[0]

    assert row["description"] == ""
    assert row["requirements"] == ""
    assert "epic_reference_number" not in row["extra"]


def test_only_transient_fetch_failures_are_retried_with_a_bound():
    calls = 0
    delays: list[float] = []

    def request_text(*_):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise SourceFetchError("temporary", retryable=True)
        return next_page([], {})

    source = EpicSource(
        request_text=request_text,
        request_json=lambda *_: [],
        sleeper=delays.append,
        jitter=lambda _low, _high: 0,
    )

    assert source.fetch(company()) == []
    assert calls == 3
    assert delays == [1.0, 3.0]
    # The page request retried twice; the search request then succeeded once.
    assert source.request_attempts == 4
    assert source.retry_attempts == 2
    assert source.last_health_diagnostics.failed_request_count == 2
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.succeeded is True


def test_exhausting_the_attempt_bound_fails_the_fetch():
    calls = 0
    delays: list[float] = []

    def request_text(*_):
        nonlocal calls
        calls += 1
        raise SourceFetchError("temporary", retryable=True)

    source = EpicSource(
        request_text=request_text,
        request_json=lambda *_: [],
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
    assert source.last_health_diagnostics.succeeded is None


def test_permanent_fetch_failure_is_not_retried():
    calls = 0
    delays: list[float] = []

    def request_text(*_):
        nonlocal calls
        calls += 1
        raise SourceFetchError("forbidden", retryable=False)

    source = EpicSource(request_text=request_text, sleeper=delays.append)
    with pytest.raises(SourceFetchError) as raised:
        source.fetch(company())
    assert calls == 1
    assert delays == []
    assert source.retry_attempts == 0
    assert raised.value.attempt_count == 1


def test_epic_watchlist_migration_and_runtime_registration():
    epic = next(
        item for item in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
        if item.name == "Epic"
    )

    assert epic.ats == "epic"
    assert epic.module == ""
    assert epic.alumni_match == ("epic",)
    assert epic.source_url == "https://careers.epic.com/jobs/"
    assert isinstance(_default_direct_sources()["epic"], EpicSource)
    assert direct_origin_key("epic") == "https://careers.epic.com"
