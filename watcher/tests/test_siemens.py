"""Siemens fixtures come from its official Avature Internship search."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.config.models import CompanyCfg
from watcher.sources.contracts import SourceError, SourceFetchError, SourceSchemaError
from watcher.sources.siemens import PAGE_SIZE, SOURCE_URL, SiemensSource

FIXTURES = Path(__file__).parent / "fixtures"


def real_page() -> str:
    return (FIXTURES / "siemens_search.html").read_text(encoding="utf-8")


def card(job_id: int, title: str = "Software Engineering Intern") -> str:
    return f"""
    <article class="article article--result">
      <h3 class="article__header__text__title">
        <a class="link" href="https://jobs.siemens.com/en_US/externaljobs/JobDetail/{job_id}">
          {title}
        </a>
      </h3>
      <div class="article__header__text__subtitle">
        <span class="list-item-location">
          <span class="list-item-jobCity">Munich</span>
          <span class="list-item-jobState">Bavaria</span>
          <span class="list-item-jobCountry">Germany</span>
        </span>
        <span class="list-item-jobId">Job ID: {job_id}</span>
        <span class="list-item-family">Engineering</span>
      </div>
    </article>
    """


def page(ids, *, first, last, total, has_next, label=None):
    nav = (
        '<li class="list-controls__pagination__item paginationNextLink">'
        '<a href="#">Next</a></li>'
        if has_next
        else ""
    )
    legend = label if label is not None else f"{total} results"
    return f"""
    <html><body>
      {"".join(card(job_id) for job_id in ids)}
      <div class="list-controls">
        <div class="list-controls__text">
          <div class="list-controls__text__legend" aria-label="{legend}">
            {first} - {last} of {total} results
          </div>
        </div>
        <nav><ul>{nav}</ul></nav>
      </div>
    </body></html>
    """


def empty_page():
    return "<html><body><p>Sorry, no jobs were found.</p></body></html>"


def source(pages, **kwargs):
    calls = []

    def request(url, _name):
        calls.append(url)
        index = len(calls) - 1
        if index >= len(pages):
            raise AssertionError("requested more pages than the fixture provides")
        return pages[index]

    return SiemensSource(request_text=request, **kwargs), calls


def company(source_url: str = SOURCE_URL) -> CompanyCfg:
    return CompanyCfg(name="Siemens", ats="siemens", source_url=source_url)


# --- the partial-coverage contract -------------------------------------------------


def test_fully_enumerated_slice_is_still_never_complete():
    """Even a clean, exhaustive pass must publish complete=False."""

    src, calls = source(
        [
            page([1, 2, 3], first=1, last=3, total=6, has_next=True),
            page([4, 5, 6], first=4, last=6, total=6, has_next=False),
        ]
    )
    rows = src.fetch(company())
    assert len(rows) == 6
    diagnostics = src.last_health_diagnostics
    assert diagnostics.complete is False
    assert diagnostics.degraded is True
    assert diagnostics.incomplete is True
    assert diagnostics.truncated is False
    assert "scope_not_completeness_proven" in diagnostics.reason_codes
    assert src.request_count == 2
    # The walk advances by the server's own reported range position.
    assert [parse_qs(urlsplit(url).query)["folderOffset"] for url in calls] == [
        ["0"],
        ["3"],
    ]


def test_walk_follows_the_portals_six_per_page_stride():
    pages = [
        page(range(1, 7), first=1, last=6, total=9, has_next=True),
        page(range(7, 10), first=7, last=9, total=9, has_next=False),
    ]
    src, calls = source(pages)
    assert len(src.fetch(company())) == 9
    assert [parse_qs(urlsplit(url).query)["folderOffset"] for url in calls] == [
        ["0"],
        ["6"],
    ]
    assert src.last_health_diagnostics.complete is False


def test_every_row_is_marked_practical_partial():
    src, _ = source([page([11], first=1, last=1, total=1, has_next=False)])
    row = src.fetch(company())[0]
    assert row["extra"]["source_completeness"] == "practical_partial"
    assert row["extra"]["official_search_keyword"] == "Internship"
    assert row["extra"]["source_scope"].endswith("keyword:Internship")
    assert row["extra"]["source_requisition_id"] == "11"
    assert row["source_url"] == (
        "https://jobs.siemens.com/en_US/externaljobs/JobDetail/11"
    )
    assert row["extra"]["source_adapter"] == "siemens"


def test_reordering_shortfall_is_published_not_hidden():
    """A duplicate across pages implies an omission; it must stay visible."""

    src, _ = source(
        [
            page([1, 2, 3], first=1, last=3, total=6, has_next=True),
            page([3, 4, 5], first=4, last=6, total=6, has_next=False),
        ]
    )
    rows = src.fetch(company())
    assert len(rows) == 5
    diagnostics = src.last_health_diagnostics
    assert diagnostics.truncated is True
    assert diagnostics.duplicate_row_count == 1
    assert diagnostics.complete is False
    assert "listing_enumeration_shortfall" in diagnostics.reason_codes
    assert "scope_not_completeness_proven" in diagnostics.reason_codes


def test_capped_total_fails_closed():
    """The unfiltered 999+ board must never be accepted as a bounded slice."""

    src, _ = source(
        [page([1], first=1, last=1, total=999, has_next=True, label="999+ results")]
    )
    with pytest.raises(SourceSchemaError):
        src.fetch(company())
    assert not src.last_health_diagnostics.complete


def test_real_official_page_parses_with_its_exact_total():
    src, _ = source([real_page()], max_pages=1)
    with pytest.raises(SourceSchemaError):
        # max_pages=1 stops before the slice completes, which must fail closed
        # rather than publish a silently short result.
        src.fetch(company())


def test_explicit_empty_slice_is_empty_and_incomplete():
    src, _ = source([empty_page()])
    assert src.fetch(company()) == []
    diagnostics = src.last_health_diagnostics
    assert diagnostics.complete is False
    assert diagnostics.degraded is True


@pytest.mark.parametrize(
    "pages",
    [
        # total drifts between pages
        [
            page([1, 2, 3], first=1, last=3, total=6, has_next=True),
            page([4, 5, 6], first=4, last=6, total=7, has_next=False),
        ],
        # range does not start where the offset says it should
        [
            page([1, 2, 3], first=1, last=3, total=6, has_next=True),
            page([4, 5, 6], first=5, last=7, total=6, has_next=False),
        ],
        # pagination stops before the slice total
        [page([1, 2, 3], first=1, last=3, total=6, has_next=False)],
        # another page is offered past the total
        [page([1, 2, 3], first=1, last=3, total=3, has_next=True)],
        # the range disagrees with the number of returned cards
        [page([1, 2], first=1, last=3, total=3, has_next=False)],
        # a populated page carries no legend at all
        ["<html><body>" + card(1) + "</body></html>"],
    ],
)
def test_inconsistent_pagination_fails_closed(pages):
    src, _ = source(pages)
    with pytest.raises(SourceSchemaError):
        src.fetch(company())
    assert not src.last_health_diagnostics.complete


def test_posting_id_must_agree_with_its_url():
    broken = page([1], first=1, last=1, total=1, has_next=False).replace(
        "Job ID: 1", "Job ID: 2"
    )
    src, _ = source([broken])
    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_transport_failure_is_bounded_and_incomplete():
    def fail(*_):
        raise SourceFetchError("unavailable", status_code=503)

    src = SiemensSource(request_text=fail)
    with pytest.raises(SourceFetchError):
        src.fetch(company())
    assert src.request_count == 1
    assert not src.last_health_diagnostics.complete


def test_configuration_is_pinned_to_the_official_search():
    src, _ = source([page([1], first=1, last=1, total=1, has_next=False)])
    with pytest.raises(SourceError):
        src.fetch(company("https://jobs.siemens.com/en_US/externaljobs/SearchJobs"))


def test_endpoint_rejects_invalid_offsets():
    for bad in (-1, 1.5, "0", True):
        with pytest.raises(ValueError):
            SiemensSource.endpoint(offset=bad)
    assert f"folderRecordsPerPage={PAGE_SIZE}" in SiemensSource.endpoint(offset=0)


# --- the source can never be promoted into complete coverage ------------------------


def test_registry_marks_siemens_practical_partial_only():
    from watcher.collection_concurrency import direct_origin_key
    from watcher.sources.registry import (
        DIRECT_COMPLETE_ATS,
        DIRECT_PRACTICAL_PARTIAL_ATS,
        build_direct_sources,
    )

    assert isinstance(build_direct_sources()["siemens"], SiemensSource)
    assert "siemens" in DIRECT_PRACTICAL_PARTIAL_ATS
    assert "siemens" not in DIRECT_COMPLETE_ATS
    assert direct_origin_key("siemens") == "https://jobs.siemens.com"


def test_catalog_never_reports_siemens_as_direct_complete():
    from backend.app.hosted.catalog import CompanyCatalog
    from watcher.config.loader import load_watchlist

    entry = CompanyCatalog.from_watcher_config(load_watchlist()).resolve("Siemens")
    assert entry.coverage == "direct_practical_partial"
    assert entry.coverage != "direct"


def test_watchlist_entry_is_validated_against_the_official_search():
    from watcher.config.loader import load_watchlist

    entry = next(
        company
        for company in load_watchlist().companies
        if company.name == "Siemens"
    )
    assert entry.ats == "siemens"
    assert entry.source_url == SOURCE_URL
