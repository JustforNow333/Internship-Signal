from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.company_matching import company_matches
from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, WatcherConfig, load_watchlist
from watcher.sources.contracts import SourceFetchError, SourceSchemaError
from watcher.sources.diagnostics import DirectSourceDiagnostics
from watcher.sources.kpmg import KpmgSource
from watcher.sources.registry import DIRECT_ATS, build_direct_sources


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def company() -> CompanyCfg:
    return CompanyCfg(
        name="KPMG",
        ats="kpmg",
        aliases=("KPMG US",),
        alumni_match=("kpmg", "kpmg us"),
        source_url="https://www.kpmguscareers.com/job-search/",
    )


def card(
    job_id: int,
    *,
    card_id: int | None = None,
    title: str | None = None,
    area: str = "Audit and Assurance",
    locations: str = "Dallas, TX; New York, NY",
    grid_count: int | None = None,
) -> str:
    """Render one card in KPMG's observed markup shape."""

    card_id = card_id if card_id is not None else 90000000000000000 + job_id
    title = title or f"Senior Associate {job_id}"
    grid = grid_count if grid_count is not None else len(locations.split(";"))
    return (
        '<div class="search--item mb-2 mb-md-3 search--experienced ">'
        f'<a href="/jobdetail/?jobId={job_id}" data-id="{card_id}"'
        ' class="box-shadow d-block">'
        '<div class="grid-view">'
        '<div class="px-3 py-1 eyebrow bg-search eyebrow text-white">Experienced</div>'
        f'<div class="p-3"><div><div class="eyebrow text-blue mb-1">{area}'
        '<svg class="heart"><path d="M14.9,23"/></svg></div>'
        f'<div class="h4 mb-4">{title}</div></div>'
        f'<div class="text-xs text-dark-grey">{grid} Locations</div></div></div>'
        '<div class="list-view d-flex justify-content-between">'
        '<div class="p-2 ps-4">'
        f'<div class="h5 text-dark-grey">{title}</div>'
        f'<div class="text-xs text-dark-grey">{area} | {locations}</div>'
        '</div><div class="d-flex">'
        '<svg class="heart"><path d="M14.9,23"/></svg>'
        "</div></div></a></div>"
    )


def search_page(
    *cards: str,
    total: int,
    size: object | None = None,
    showing: str | None = None,
) -> str:
    if showing is None:
        showing = f'<span data-action="count">{total}</span> Results'
    return json.dumps(
        {
            "showing": showing,
            "pagination": (
                '<ul class="pagination"><li class="page-item">'
                '<a class="page-link" data-href="2">2</a></li></ul>'
            ),
            "postings": {
                "size": total if size is None else size,
                "jobs": "".join(cards),
            },
        }
    )


def source_for_pages(pages: list[str], *, max_snapshot_passes: int = 3) -> KpmgSource:
    queued = iter(pages)

    def request(url: str, source_name: str):
        assert source_name == "kpmg"
        assert "get-jobs.php" in url, "listing-only source must not open postings"
        return next(queued)

    return KpmgSource(
        request_text=request,
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0,
        max_snapshot_passes=max_snapshot_passes,
    )


def test_fixture_maps_every_canonical_listing_field():
    pages = [fixture("kpmg_search_page.json")]
    payload = json.loads(pages[0])
    # The fixture is a real page-1 capture; trim its total to what it carries.
    payload["postings"]["size"] = 2
    payload["showing"] = '<span data-action="count">2</span> Results'
    trimmed = json.dumps(payload)

    source = source_for_pages([trimmed, trimmed])
    rows = source.fetch(company())

    assert len(rows) == 2
    assert rows[0]["company"] == "KPMG"
    assert rows[0]["title"] == "AI Engineer- Senior Associate"
    assert rows[0]["location"] == (
        "Dallas, TX; Montvale, NJ; New York, NY; Orlando, FL; "
        "Philadelphia, PA; Washington, DC"
    )
    # KPMG cards carry no description or posting date; the pipeline gates on
    # title and location, which the listing provides in full.
    assert rows[0]["description"] == ""
    assert rows[0]["date_posted"] == ""
    assert rows[0]["source_url"] == (
        "https://www.kpmguscareers.com/jobdetail/?jobId=133741"
    )
    assert rows[0]["extra"]["source_requisition_id"] == "kpmg:133741"
    assert rows[0]["extra"]["kpmg_job_id"] == "133741"
    assert rows[0]["extra"]["kpmg_card_ids"] == ["91987313839481542"]
    assert rows[0]["extra"]["practice_area"] == "Audit and Assurance"
    assert source.last_health_diagnostics == DirectSourceDiagnostics(
        succeeded=True, retained_row_count=2, complete=True
    )


def test_normal_multi_page_crawl_has_exact_final_short_page():
    first = search_page(*(card(v) for v in range(101, 113)), total=14)
    final = search_page(card(113), card(114), total=14)
    source = source_for_pages([first, final, first, final])

    rows = source.fetch(company())

    assert [row["extra"]["kpmg_job_id"] for row in rows] == [
        str(v) for v in range(101, 115)
    ]
    diagnostics = source.last_diagnostics
    assert diagnostics.snapshot_passes_requested == 2
    assert diagnostics.listing_pages_requested == 4
    assert diagnostics.authoritative_total == 14
    assert diagnostics.raw_cards_seen == 14
    assert diagnostics.unique_card_ids == 14
    assert diagnostics.retained_requisitions == 14
    assert diagnostics.reconciled_requisitions == 0


def test_listing_requests_use_documented_query_and_1_based_pages():
    first = search_page(*(card(v) for v in range(101, 113)), total=13)
    final = search_page(card(113), total=13)
    pages = iter([first, final, first, final])
    urls: list[str] = []

    def request(url: str, _source_name: str):
        urls.append(url)
        return next(pages)

    KpmgSource(request_text=request).fetch(company())

    queries = [parse_qs(urlsplit(url).query) for url in urls]
    assert [query["spage"] for query in queries] == [["1"], ["2"], ["1"], ["2"]]
    assert all(query["ajax"] == ["1"] for query in queries)
    assert all(query["page_type"] == ["search"] for query in queries)
    assert all(urlsplit(url).netloc == "www.kpmguscareers.com" for url in urls)


def test_explicit_empty_board_is_trustworthy_and_complete():
    empty = search_page(total=0)
    source = source_for_pages([empty, empty])

    assert source.fetch(company()) == []
    assert source.last_diagnostics.authoritative_total == 0
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.degraded is False


def test_past_end_page_preserving_nonzero_total_is_not_an_empty_board():
    """A page past the end returns no cards while still reporting the total.

    That is the board's truncation signal, not an empty result, so it must not
    end a pass early or be mistaken for a healthy empty board.
    """

    first = search_page(*(card(v) for v in range(101, 113)), total=14)
    past_end = search_page(total=14)
    source = source_for_pages([first, past_end, first, past_end])

    with pytest.raises(SourceSchemaError, match="final page did not match"):
        source.fetch(company())


def test_changed_total_between_pages_discards_pass_then_converges():
    unstable_first = search_page(*(card(v) for v in range(101, 113)), total=14)
    unstable_second = search_page(card(113), card(114), total=15)
    good_first = search_page(*(card(v) for v in range(101, 113)), total=14)
    good_final = search_page(card(113), card(114), total=14)
    source = source_for_pages(
        [unstable_first, unstable_second, good_first, good_final,
         good_first, good_final]
    )

    rows = source.fetch(company())

    assert len(rows) == 14
    assert source.last_diagnostics.snapshot_passes_requested == 3


def test_changed_total_between_whole_passes_requires_matching_pass():
    page_a = search_page(card(101), total=1)
    page_b = search_page(card(102), total=1)
    source = source_for_pages([page_a, page_b, page_b])

    rows = source.fetch(company())

    assert [row["extra"]["kpmg_job_id"] for row in rows] == ["102"]
    assert source.last_diagnostics.snapshot_passes_requested == 3


def test_repeated_page_fails_after_bounded_whole_pass_retries():
    repeated = search_page(*(card(v) for v in range(101, 113)), total=24)
    source = source_for_pages([repeated] * 6)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())
    assert source.last_diagnostics.snapshot_passes_requested == 3


def test_premature_short_page_fails_after_bounded_snapshot_retries():
    short = search_page(card(101), total=24)
    source = source_for_pages([short] * 6)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())


def test_duplicate_card_id_is_rejected():
    page = search_page(
        card(101, card_id=5001), card(102, card_id=5001), total=2
    )
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="duplicate card id"):
        source.fetch(company())


def test_benign_repeated_requisition_unions_its_split_location_halves():
    """KPMG splits a long location list across two cards of one requisition.

    Both halves are real location evidence, so the retained requisition carries
    their deterministic union rather than either half alone.
    """

    page = search_page(
        card(
            101,
            card_id=5001,
            title="Manager, Cyber Security",
            locations="Albany, NY; Boston, MA",
            grid_count=2,
        ),
        card(
            101,
            card_id=5002,
            title="Manager, Cyber Security",
            locations="Omaha, NE; Winston-Salem, NC",
            grid_count=2,
        ),
        total=2,
    )
    source = source_for_pages([page, page])

    rows = source.fetch(company())

    assert len(rows) == 1
    assert rows[0]["location"] == (
        "Albany, NY; Boston, MA; Omaha, NE; Winston-Salem, NC"
    )
    assert rows[0]["extra"]["kpmg_card_ids"] == ["5001", "5002"]
    diagnostics = source.last_diagnostics
    # Raw completeness is proven against the board's total before reconciliation.
    assert diagnostics.authoritative_total == 2
    assert diagnostics.raw_cards_seen == 2
    assert diagnostics.unique_card_ids == 2
    assert diagnostics.retained_requisitions == 1
    assert diagnostics.reconciled_requisitions == 1


@pytest.mark.parametrize(
    "conflicting",
    [
        {"title": "A Different Title"},
        {"area": "Tax"},
    ],
)
def test_conflicting_repeated_requisition_fails_closed(conflicting: dict):
    page = search_page(
        card(101, card_id=5001, locations="Albany, NY"),
        card(101, card_id=5002, locations="Omaha, NE", **conflicting),
        total=2,
    )
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="conflicting posting fields"):
        source.fetch(company())


def test_malformed_json_is_rejected():
    source = source_for_pages(["not json at all", "not json at all"])

    with pytest.raises(SourceSchemaError, match="not decodable JSON"):
        source.fetch(company())


@pytest.mark.parametrize(
    "broken_card",
    [
        # no posting link or card id
        '<div class="search--item"><a class="box-shadow"><div class="h5'
        ' text-dark-grey">Title</div></a></div>',
        # list view present but no title
        '<div class="search--item"><a href="/jobdetail/?jobId=1" data-id="2">'
        '<div class="list-view"><div class="text-xs text-dark-grey">Tax | NY'
        "</div></div></a></div>",
    ],
)
def test_malformed_card_html_is_rejected(broken_card: str):
    page = search_page(broken_card, total=1)
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError):
        source.fetch(company())


def test_location_count_summary_is_never_accepted_as_a_location():
    """The grid view's "N Locations" summary must not stand in for locations."""

    page = search_page(
        card(101, locations="6 Locations", grid_count=6), total=1
    )
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="location count instead"):
        source.fetch(company())


@pytest.mark.parametrize(
    "page",
    [
        search_page(card(101), total=1, size="not-a-number"),
        search_page(card(101), total=1, showing="<span>no count here</span>"),
        # the two representations of the total must agree
        search_page(card(101), total=1, showing='<span data-action="count">9</span> Results'),
    ],
)
def test_missing_or_invalid_exact_total_is_rejected(page: str):
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError):
        source.fetch(company())


def test_total_beyond_the_page_safeguard_is_rejected():
    page = search_page(card(101), total=10_000_000)
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="pagination safeguard"):
        source.fetch(company())


def test_max_page_bound_is_enforced():
    page = search_page(*(card(v) for v in range(101, 113)), total=120)
    source = KpmgSource(
        request_text=lambda _url, _name: page,
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0,
        max_pages=2,
    )

    with pytest.raises(SourceSchemaError):
        source.fetch(company())


def test_transient_request_retry_uses_shared_bound_and_degrades_result():
    page = search_page(card(101), total=1)
    calls = 0
    responses = iter([page, page])
    delays: list[float] = []

    def request(url: str, _source_name: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SourceFetchError("temporary", retryable=True)
        return next(responses)

    source = KpmgSource(
        request_text=request, sleeper=delays.append, jitter=lambda _l, _h: 0
    )

    assert len(source.fetch(company())) == 1
    assert delays == [1.0]
    assert source.last_health_diagnostics.failed_request_count == 1
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.degraded is True
    assert source.last_health_diagnostics.complete is True


def test_non_retryable_request_failure_fails_the_source():
    def request(_url: str, _name: str):
        raise SourceFetchError("board unavailable", retryable=False)

    source = KpmgSource(request_text=request)
    with pytest.raises(SourceFetchError, match="board unavailable"):
        source.fetch(company())
    assert source.last_health_diagnostics.succeeded is None


def test_registry_lazy_construction_origin_matching_and_fingerprint():
    configured = next(
        item
        for item in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
        if item.name == "KPMG"
    )

    assert configured.ats == "kpmg"
    assert configured.module == ""
    assert configured.aliases == ("KPMG US",)
    assert configured.alumni_match == ("kpmg", "kpmg us")
    assert "kpmg" in DIRECT_ATS
    assert isinstance(build_direct_sources()["kpmg"], KpmgSource)
    assert direct_origin_key("kpmg") == "https://www.kpmguscareers.com"
    # "KPMG LLP" resolves through legal-suffix normalization, not an alias.
    assert company_matches("KPMG LLP", configured)
    assert company_matches("KPMG US", configured)
    assert not company_matches("KPMG International", configured)

    baseline = WatcherConfig(companies=(configured,))
    old = WatcherConfig(
        companies=(replace(configured, ats="bespoke", module="kpmg"),)
    )
    assert collection_config_fingerprint(baseline) != collection_config_fingerprint(old)


def test_lazy_export_does_not_import_eagerly():
    import watcher.sources as sources

    assert "KpmgSource" in sources.__all__
    assert sources.KpmgSource is KpmgSource
