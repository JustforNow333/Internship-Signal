from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.company_matching import company_matches
from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, WatcherConfig, load_watchlist
from watcher.sources.bloomberg import BloombergSource
from watcher.sources.contracts import SourceFetchError, SourceSchemaError
from watcher.sources.diagnostics import DirectSourceDiagnostics
from watcher.sources.registry import DIRECT_ATS, build_direct_sources


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def company() -> CompanyCfg:
    return CompanyCfg(
        name="Bloomberg",
        ats="bloomberg",
        aliases=("Bloomberg L.P.",),
        alumni_match=("bloomberg",),
        source_url="https://bloomberg.avature.net/careers/SearchJobs",
    )


def listing(
    posting_id: int,
    *,
    title: str | None = None,
    location: str = "New York, New York, United States of America",
    slug: str | None = None,
    second_id: int | None = None,
) -> str:
    title = title or f"Software Engineer Intern {posting_id}"
    slug = slug or f"Software-Engineer-Intern-{posting_id}"
    detail_url = (
        "https://bloomberg.avature.net/careers/JobDetail/"
        f"{slug}/{posting_id}"
    )
    apply_id = posting_id if second_id is None else second_id
    apply_url = (
        "https://bloomberg.avature.net/careers/JobDetail/"
        f"{slug}/{apply_id}"
    )
    return (
        '<article class="article article--result">'
        '<h3 class="article__header__text__title title title--04">'
        f'<a class="link" href="{detail_url}">{title}</a></h3>'
        f'<span class="list-item-location">{location}</span>'
        f'<a class="button button--primary" href="{apply_url}">Apply</a>'
        "</article>"
    )


def search_page(
    *records: str,
    total: int | None = None,
    first: int = 1,
    total_label: str | None = None,
    range_label: str | None = None,
    explicit_empty: bool = False,
) -> str:
    total = len(records) if total is None else total
    total_label = total_label if total_label is not None else f"{total} results"
    if total == 0:
        range_label = "" if range_label is None else range_label
    else:
        last = first + len(records) - 1
        range_label = range_label or f"{first}-{last} of {total} results"
    empty = (
        '<article class="article article--result"><h3 '
        'class="article__header__text__title">No jobs found - There are '
        "currently no open roles matching your search.</h3></article>"
        if explicit_empty
        else ""
    )
    legend = (
        '<div class="list-controls__text__legend" '
        f'aria-label="{total_label}">{range_label}</div>'
    )
    return f"{legend}<div class=\"results__panel\">{''.join(records)}{empty}</div>{legend}"


def source_for_pages(pages: list[str], *, max_snapshot_passes: int = 3):
    queued = iter(pages)

    def request(url: str, source_name: str):
        assert source_name == "bloomberg"
        assert "/SearchJobs" in url, "listing-only source must not open postings"
        return next(queued)

    return BloombergSource(
        request_text=request,
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0,
        max_snapshot_passes=max_snapshot_passes,
    )


def test_fixture_maps_every_canonical_listing_field():
    pages = [fixture("bloomberg_search_page.html")] * 2

    def request(url: str, _source_name: str):
        assert "/SearchJobs" in url
        return pages.pop(0)

    source = BloombergSource(request_text=request)

    rows = source.fetch(company())

    assert len(rows) == 2
    assert rows[0]["company"] == "Bloomberg"
    assert rows[0]["title"] == "Software Engineer Intern"
    assert rows[0]["location"] == "New York, New York, United States of America"
    # Bloomberg result cards carry no description or posting date; the pipeline
    # gates on title and location, which are present, and scores an absent
    # description neutrally rather than dropping or excluding the row.
    assert rows[0]["description"] == ""
    assert rows[0]["date_posted"] == ""
    assert rows[0]["source_url"].endswith("/Software-Engineer-Intern/21001")
    assert rows[0]["extra"]["source_requisition_id"] == "bloomberg:21001"
    assert rows[0]["extra"]["bloomberg_avature_id"] == "21001"
    assert source.last_health_diagnostics == DirectSourceDiagnostics(
        succeeded=True,
        retained_row_count=2,
        complete=True,
    )


def test_normal_multi_page_enumeration_has_exact_final_short_page():
    first = search_page(*(listing(value) for value in range(101, 113)), total=13)
    final = search_page(listing(113), total=13, first=13)
    source = source_for_pages([first, final, first, final])

    rows = source.fetch(company())

    assert [row["extra"]["bloomberg_avature_id"] for row in rows] == [
        str(value) for value in range(101, 114)
    ]
    assert source.last_diagnostics.snapshot_passes_requested == 2
    assert source.last_diagnostics.listing_pages_requested == 4
    assert source.last_diagnostics.stable_snapshot_rows == 13
    assert source.last_diagnostics.raw_listing_records_seen == 26


def test_listing_requests_use_fixed_page_size_and_expected_offsets():
    first = search_page(*(listing(value) for value in range(101, 113)), total=13)
    final = search_page(listing(113), total=13, first=13)
    pages = iter([first, final, first, final])
    listing_urls: list[str] = []

    def request(url: str, _source_name: str):
        assert "/SearchJobs" in url
        listing_urls.append(url)
        return next(pages)

    BloombergSource(request_text=request).fetch(company())

    queries = [parse_qs(urlsplit(url).query) for url in listing_urls]
    assert [query["jobOffset"] for query in queries] == [
        ["0"], ["12"], ["0"], ["12"]
    ]
    assert [query["jobRecordsPerPage"] for query in queries] == [["12"]] * 4


def test_explicit_zero_result_board_is_trustworthy_and_complete():
    empty = search_page(total=0, explicit_empty=True)
    source = source_for_pages([empty, empty])

    assert source.fetch(company()) == []
    assert source.last_diagnostics.listing_pages_requested == 2
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.degraded is False


def test_changed_total_between_pages_discards_pass_then_converges():
    unstable_first = search_page(
        *(listing(value) for value in range(101, 113)), total=13
    )
    unstable_second = search_page(listing(113), total=14, first=13)
    stable_first = search_page(
        *(listing(value) for value in range(101, 113)), total=13
    )
    stable_second = search_page(listing(113), total=13, first=13)
    source = source_for_pages(
        [unstable_first, unstable_second, stable_first, stable_second, stable_first, stable_second]
    )

    rows = source.fetch(company())

    assert len(rows) == 13
    assert source.last_diagnostics.snapshot_passes_requested == 3


def test_changed_total_between_whole_passes_requires_third_matching_pass():
    one = search_page(listing(101), total=1)
    two = search_page(listing(101), listing(102), total=2)
    source = source_for_pages([one, two, two])

    rows = source.fetch(company())

    assert [row["extra"]["bloomberg_avature_id"] for row in rows] == ["101", "102"]
    assert source.last_diagnostics.snapshot_passes_requested == 3


def test_repeated_page_fails_after_bounded_whole_pass_retries():
    first = search_page(*(listing(value) for value in range(101, 113)), total=24)
    repeated = search_page(
        *(listing(value) for value in range(101, 113)), total=24, first=13
    )
    source = source_for_pages(
        [first, repeated, first, repeated], max_snapshot_passes=2
    )

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())

    assert source.last_diagnostics.snapshot_passes_requested == 2
    assert source.last_diagnostics.listing_pages_requested == 4


def test_duplicate_posting_id_is_rejected():
    page = search_page(
        listing(101),
        listing(101, slug="Conflicting-Slug"),
        total=2,
    )
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="duplicate posting ID"):
        source.fetch(company())


def test_conflicting_detail_links_inside_one_card_are_rejected():
    page = search_page(listing(101, second_id=102), total=1)
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="conflicting detail links"):
        source.fetch(company())


def test_premature_short_page_fails_after_bounded_snapshot_retries():
    short = search_page(*(listing(value) for value in range(101, 112)), total=13)
    source = source_for_pages([short, short], max_snapshot_passes=2)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())


def test_malformed_listing_html_is_rejected():
    malformed = (
        '<div class="list-controls__text__legend" aria-label="1 results">'
        "1-1 of 1 results</div>"
        '<article class="article article--result"><h3 '
        'class="article__header__text__title"><a class="link" '
        'href="https://bloomberg.avature.net/careers/JobDetail/Intern/101">Intern'
    )
    source = source_for_pages([malformed, malformed])

    with pytest.raises(SourceSchemaError, match="malformed listing HTML"):
        source.fetch(company())


@pytest.mark.parametrize(
    "page",
    [
        '<article class="article article--result"></article>',
        search_page(listing(101), total=1, total_label="999+ results"),
        search_page(listing(101), total=1, range_label="results unavailable"),
    ],
)
def test_missing_or_invalid_exact_total_is_rejected(page: str):
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="exact result total"):
        source.fetch(company())


def test_snapshot_instability_is_bounded_and_never_unioned():
    first = search_page(listing(101), total=1)
    second = search_page(listing(102), total=1)
    third = search_page(listing(103), total=1)
    source = source_for_pages([first, second, third])

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())

    assert source.last_diagnostics.snapshot_passes_requested == 3
    assert source.last_diagnostics.stable_snapshot_rows == 0


def test_transient_request_retry_uses_shared_bound_and_degrades_recovered_result():
    page = search_page(listing(101), total=1)
    calls = 0
    listing_responses = iter([page, page])
    delays: list[float] = []

    def request(url: str, _source_name: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SourceFetchError("temporary", retryable=True)
        assert "/SearchJobs" in url
        return next(listing_responses)

    source = BloombergSource(
        request_text=request,
        sleeper=delays.append,
        jitter=lambda _low, _high: 0,
    )

    assert len(source.fetch(company())) == 1
    assert delays == [1.0]
    assert source.last_health_diagnostics.failed_request_count == 1
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.degraded is True
    assert source.last_health_diagnostics.complete is True


def test_registry_lazy_config_origin_matching_coverage_and_fingerprint():
    configured = next(
        item
        for item in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
        if item.name == "Bloomberg"
    )

    assert configured.ats == "bloomberg"
    assert configured.module == ""
    assert configured.aliases == ("Bloomberg L.P.",)
    assert configured.alumni_match == ("bloomberg",)
    assert configured.source_url == (
        "https://bloomberg.avature.net/careers/SearchJobs"
    )
    assert "bloomberg" in DIRECT_ATS
    assert isinstance(build_direct_sources()["bloomberg"], BloombergSource)
    assert direct_origin_key("bloomberg") == "https://bloomberg.avature.net"
    assert company_matches("Bloomberg L.P.", configured)
    assert not company_matches("BloombergNEF", configured)

    baseline = WatcherConfig(companies=(configured,))
    old = WatcherConfig(companies=(replace(configured, ats="bespoke", module="bloomberg"),))
    assert collection_config_fingerprint(baseline) != collection_config_fingerprint(old)
