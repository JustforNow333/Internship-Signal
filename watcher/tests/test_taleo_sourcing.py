"""Offline contract, completeness, and diagnostics tests for Taleo Sourcing."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.config import CompanyCfg, load_watchlist
from watcher.sources import SourceSchemaError, TaleoSourcingSource


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
BOOTSTRAP = (FIXTURES / "taleo_sourcing_bootstrap.html").read_text(encoding="utf-8")
RESULTS = (FIXTURES / "taleo_sourcing_results_page.html").read_text(encoding="utf-8")
EMPTY = (FIXTURES / "taleo_sourcing_empty_page.html").read_text(encoding="utf-8")
SEARCH_ID = 20882388


def _company(**overrides) -> CompanyCfg:
    values = {
        "name": "Example",
        "ats": "taleo_sourcing",
        "taleo_sourcing_host": "jobs.example.test",
        "taleo_sourcing_site": "default657",
        "source_url": "https://jobs.example.test/",
    }
    values.update(overrides)
    return CompanyCfg(**values)


def _row(native_id: int | str, *, url: str | None = None) -> str:
    native = str(native_id)
    href = f"https://jobs.example.test/jobs/role-{native}" if url is None else url
    return f"""
        <div id="job_list_{native}" class="job_list_row jlr_Odd ">
          <div class="jlr_right_hldr">
            <div class="jlr_title">
              <p><a href="{href}" class="job_link font_bold">Role {native}</a></p>
              <p class="jlr_company">Europe Region</p>
              <p class="jlr_cat_loc">
                <span class="font_bold">Location:</span>
                <span class="location">London, -, United Kingdom</span>
              </p>
              <p class="jlr_cat_loc">
                <span class="font_bold">Category:</span>
                <span class="category">Structural Engineering</span>
              </p>
            </div>
            <div class="jlr_content">
              <p class="jlr_description">Excerpt {native}.....</p>
            </div>
          </div>
        </div>
    """


def _listing(
    rows: list[str],
    *,
    total: int,
    pages: str,
    current: int,
    extra: str = "",
) -> dict:
    body = "".join(rows)
    return {
        "Status": "OK",
        "UserMessage": "",
        "Result": f"""
          <div class="results_content jResultsContent">
            <div class="number_of_results">
              <span class="total_results">{total}</span> results
            </div>
            {body}{extra}
            <div id="jPaginateNumPages" class="ghost">{pages}</div>
            <div id="jPaginateCurrPage" class="ghost">{current}</div>
          </div>
        """,
    }


def _created(search_id: int | str = SEARCH_ID) -> dict:
    return {"Status": "OK", "UserMessage": "", "Result": {"JobSearch.id": search_id}}


def _pages(total: int, per_page: int) -> list[dict]:
    """Build one complete crawl of `total` postings at `per_page` per page."""

    page_count = max(1, -(-total // per_page))
    built = []
    for index in range(page_count):
        start = index * per_page + 1
        stop = min(total, start + per_page - 1)
        built.append(
            _listing(
                [_row(native) for native in range(start, stop + 1)],
                total=total,
                pages=f"{page_count}.0",
                current=index + 1,
            )
        )
    return built


class _Response:
    def __init__(self, body: str | dict, url: str, *, html: bool = False) -> None:
        self._body = (
            body.encode("utf-8")
            if isinstance(body, str)
            else json.dumps(body).encode("utf-8")
        )
        self._url = url
        self.status = 200
        self.headers = {
            "Content-Type": (
                "text/html; charset=utf-8" if html else "text/plain;charset=UTF-8"
            )
        }

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class _Opener:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout: int):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return _Response(
            response,
            request.full_url,
            html=request.get_method() == "GET",
        )


def _source(responses: list[object], **kwargs) -> tuple[TaleoSourcingSource, _Opener]:
    opener = _Opener([BOOTSTRAP, _created(), *responses])
    session = SimpleNamespace(opener=opener, cookies=(object(),))
    kwargs.setdefault("page_delay_seconds", 0.0)
    source = TaleoSourcingSource(
        session_factory=lambda: session,
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0.0,
        **kwargs,
    )
    return source, opener


# --- session, token, and search creation ---------------------------------


def test_bootstrap_token_site_and_search_id_drive_every_listing_request():
    source, opener = _source(_pages(25, 10))

    rows = source.fetch(_company())

    assert len(rows) == 25
    assert source.bootstrap_requests == 1
    assert source.search_requests == 1
    assert source.pages_requested == 3

    home, create, *pages = opener.requests
    assert home.get_method() == "GET"
    assert home.full_url == "https://jobs.example.test/"
    assert create.get_method() == "POST"
    assert create.full_url == "https://jobs.example.test/ajax/jobs/search/create"
    assert create.data == b"keywords="
    assert create.get_header("Content-type") == (
        "application/x-www-form-urlencoded; charset=UTF-8"
    )
    for index, request in enumerate(pages, start=1):
        parsed = urlsplit(request.full_url)
        assert request.get_method() == "POST"
        assert parsed.path == "/ajax/content/job_results"
        assert parse_qs(parsed.query) == {
            "JobSearch.id": [str(SEARCH_ID)],
            "page_index": [str(index)],
            "site-name": ["default657"],
            "include_site": ["true"],
        }
    for request in (create, *pages):
        assert request.get_header("Tss-token") == "fixture-request-token"
        assert request.get_header("X-requested-with") == "XMLHttpRequest"
        assert request.get_header("Referer") == "https://jobs.example.test/"


def test_bootstrap_requires_cookies_one_token_and_a_matching_site():
    opener = _Opener([BOOTSTRAP])
    no_cookie = TaleoSourcingSource(
        session_factory=lambda: SimpleNamespace(opener=opener, cookies=()),
        sleeper=lambda _delay: None,
    )
    with pytest.raises(SourceSchemaError, match="session cookie"):
        no_cookie.fetch(_company())

    for html, expected in (
        (BOOTSTRAP.replace("default657", "other999"), "site identifier did not match"),
        (BOOTSTRAP.replace('value ="fixture-request-token"', 'value =""'), "request token"),
        (BOOTSTRAP + BOOTSTRAP, "exactly one request token"),
        ("   ", "bootstrap response was empty"),
    ):
        source = TaleoSourcingSource(
            session_factory=lambda html=html: SimpleNamespace(
                opener=_Opener([html]), cookies=(object(),)
            ),
            sleeper=lambda _delay: None,
        )
        with pytest.raises(SourceSchemaError, match=expected):
            source.fetch(_company())


def test_site_name_and_host_configuration_are_required():
    from watcher.sources.contracts import SourceError

    for overrides, expected in (
        ({"taleo_sourcing_host": ""}, "taleo_sourcing_host"),
        ({"taleo_sourcing_site": ""}, "taleo_sourcing_site"),
        ({"taleo_sourcing_site": "bad site"}, "taleo_sourcing_site"),
        ({"taleo_sourcing_site": "x" * 100}, "taleo_sourcing_site"),
    ):
        source, _ = _source([])
        with pytest.raises(SourceError, match=expected):
            source.fetch(_company(**overrides))


def test_invalid_search_creation_responses_fail_closed():
    for created, expected in (
        ({"Status": "OK"}, "missing Result"),
        ({"Result": {"JobSearch.id": 0}}, "positive JobSearch.id"),
        ({"Result": {"JobSearch.id": "20882388"}}, "positive JobSearch.id"),
        ({"Result": {"JobSearch.id": True}}, "positive JobSearch.id"),
        ({"Result": []}, "missing Result"),
    ):
        opener = _Opener([BOOTSTRAP, created])
        source = TaleoSourcingSource(
            session_factory=lambda opener=opener: SimpleNamespace(
                opener=opener, cookies=(object(),)
            ),
            sleeper=lambda _delay: None,
        )
        with pytest.raises(SourceSchemaError, match=expected):
            source.fetch(_company())


# --- enumeration and completeness ----------------------------------------


def test_multi_page_enumeration_ends_on_a_short_final_page():
    source, _ = _source(_pages(23, 10))

    rows = source.fetch(_company())

    assert [row["extra"]["taleo_sourcing_native_id"] for row in rows] == [
        str(native) for native in range(1, 24)
    ]
    assert source.pages_requested == 3
    assert len(rows) == 23
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.retained_row_count == 23


def test_sanitized_fixture_maps_canonical_fields_without_inventing_a_date():
    opener = _Opener([BOOTSTRAP, _created(), {"Result": RESULTS}])
    source = TaleoSourcingSource(
        session_factory=lambda: SimpleNamespace(opener=opener, cookies=(object(),)),
        sleeper=lambda _delay: None,
        page_delay_seconds=0.0,
    )

    rows = source.fetch(_company())

    assert len(rows) == 2
    assert rows[0]["title"] == "Graduate Engineer"
    assert rows[0]["location"] == "London, -, United Kingdom"
    assert rows[0]["description"] == "Join our team as a graduate engineer....."
    assert rows[0]["date_posted"] == ""
    assert rows[0]["source_url"] == (
        "https://jobs.example.test/jobs/graduate-engineer-34280"
    )
    assert rows[0]["extra"]["source_requisition_id"] == (
        "taleo_sourcing:jobs.example.test:default657:34280"
    )
    assert rows[0]["extra"]["source_adapter"] == "taleo_sourcing"
    assert rows[0]["extra"]["category"] == "Structural Engineering"
    assert rows[0]["extra"]["region"] == "Europe Region"
    assert rows[1]["title"] == "Summer Intern"
    assert rows[1]["location"] == (
        "New York, -, United States and 4 additional locations"
    )
    assert all(row["date_posted"] == "" for row in rows)


def test_explicit_zero_result_board_succeeds_without_rows():
    opener = _Opener([BOOTSTRAP, _created(), {"Result": EMPTY}])
    source = TaleoSourcingSource(
        session_factory=lambda: SimpleNamespace(opener=opener, cookies=(object(),)),
        sleeper=lambda _delay: None,
    )

    assert source.fetch(_company()) == []
    assert source.pages_requested == 1
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.retained_row_count == 0


def test_zero_total_with_rows_or_pages_is_inconsistent():
    inconsistent, _ = _source([_listing([_row(1)], total=0, pages="0.0", current=1)])
    with pytest.raises(SourceSchemaError, match="zero-result response was inconsistent"):
        inconsistent.fetch(_company())

    pages, _ = _source([_listing([], total=0, pages="1.0", current=1)])
    with pytest.raises(SourceSchemaError, match="zero-result response was inconsistent"):
        pages.fetch(_company())


def _second_page(*, total: int = 20, pages: str = "2.0", current: int = 2) -> dict:
    return _listing(
        [_row(native) for native in range(11, 21)],
        total=total,
        pages=pages,
        current=current,
    )


def test_changing_total_or_page_count_fails_closed():
    first, _second = _pages(20, 10)

    total, _ = _source([first, _second_page(total=21)])
    with pytest.raises(SourceSchemaError, match="total changed during pagination"):
        total.fetch(_company())

    count, _ = _source([first, _second_page(pages="3.0")])
    with pytest.raises(SourceSchemaError, match="page count changed during pagination"):
        count.fetch(_company())


def test_unexpected_current_page_metadata_fails_closed():
    first, _second = _pages(20, 10)

    stalled, _ = _source([first, _second_page(current=1)])
    with pytest.raises(SourceSchemaError, match="returned page 1; expected 2"):
        stalled.fetch(_company())

    beyond, _ = _source([first, _second_page(current=3)])
    with pytest.raises(SourceSchemaError, match="pagination metadata is invalid"):
        beyond.fetch(_company())

    zero, _ = _source([_listing([_row(1)], total=1, pages="1.0", current=0)])
    with pytest.raises(SourceSchemaError, match="current page index must be positive"):
        zero.fetch(_company())


def test_page_metadata_must_agree_with_the_reported_total():
    source, _ = _source([_listing([_row(1)], total=25, pages="1.0", current=1)])
    with pytest.raises(SourceSchemaError, match="disagrees with its reported total"):
        source.fetch(_company())


def test_repeated_pagination_page_fails_closed():
    first, _second = _pages(20, 10)
    repeated = _listing(
        [_row(native) for native in range(1, 11)],
        total=20,
        pages="2.0",
        current=2,
    )
    source, _ = _source([first, repeated])
    with pytest.raises(SourceSchemaError, match="repeated pagination page"):
        source.fetch(_company())


def test_premature_empty_and_short_pages_fail_closed():
    first, _second = _pages(20, 10)
    empty_second = _listing([], total=20, pages="2.0", current=2)
    empty, _ = _source([first, empty_second])
    with pytest.raises(SourceSchemaError, match="ended before the reported total"):
        empty.fetch(_company())

    # A short middle page cannot be the last page of a three-page crawl.
    full = _listing(
        [_row(native) for native in range(1, 11)],
        total=25,
        pages="3.0",
        current=1,
    )
    short_middle = _listing(
        [_row(native) for native in range(11, 20)],
        total=25,
        pages="3.0",
        current=2,
    )
    middle, _ = _source([full, short_middle])
    with pytest.raises(SourceSchemaError, match="ended prematurely"):
        middle.fetch(_company())

    # A final page that leaves the reported total unmet is never complete.
    second = _listing(
        [_row(native) for native in range(11, 21)],
        total=25,
        pages="3.0",
        current=2,
    )
    final = _listing(
        [_row(native) for native in range(21, 25)],
        total=25,
        pages="3.0",
        current=3,
    )
    incomplete, _ = _source([full, second, final])
    with pytest.raises(SourceSchemaError, match="final page did not complete"):
        incomplete.fetch(_company())


def test_more_records_than_the_reported_total_fails_closed():
    over = _listing(
        [_row(native) for native in range(1, 12)],
        total=10,
        pages="1.0",
        current=1,
    )
    source, _ = _source([over])
    with pytest.raises(SourceSchemaError, match="more records than the reported total"):
        source.fetch(_company())


def test_bounded_maximum_page_safeguard_stops_an_endless_crawl():
    pages = _pages(30, 10)
    source, _ = _source(pages, max_pages=2)
    with pytest.raises(SourceSchemaError, match="maximum page safeguard"):
        source.fetch(_company())

    with pytest.raises(ValueError, match="max_pages"):
        TaleoSourcingSource(max_pages=0)
    with pytest.raises(ValueError, match="page_delay_seconds"):
        TaleoSourcingSource(page_delay_seconds=60.0)


# --- identity and record quality -----------------------------------------


def test_duplicate_posting_ids_fail_closed():
    duplicate = _listing([_row(1), _row(1)], total=2, pages="1.0", current=1)
    source, _ = _source([duplicate])
    with pytest.raises(SourceSchemaError, match="duplicate posting ID"):
        source.fetch(_company())


@pytest.mark.parametrize(
    "url",
    (
        "https://jobs.example.test/jobs/role-99",
        "https://jobs.other.test/jobs/role-1",
        "http://jobs.example.test/jobs/role-1",
        "https://user:secret@jobs.example.test/jobs/role-1",
        "https://jobs.example.test/jobs/role-1?utm=x",
        "https://jobs.example.test/jobs/nested/role-1",
        "https://jobs.example.test/other/role-1",
        "",
    ),
)
def test_conflicting_or_foreign_posting_urls_are_rejected(url):
    page = _listing([_row(1, url=url)], total=1, pages="1.0", current=1)
    source, _ = _source([page])
    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(_company())


def test_malformed_records_are_skipped_and_diagnosed():
    mixed = _listing(
        [
            _row(1),
            '<div id="job_list_" class="job_list_row">no id</div>',
            _row(3),
        ],
        total=3,
        pages="1.0",
        current=1,
    )
    source, _ = _source([mixed])

    rows = source.fetch(_company())

    assert [row["extra"]["taleo_sourcing_native_id"] for row in rows] == ["1", "3"]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.schema_error_row_count == 1
    assert diagnostics.malformed_row_count == 0
    assert diagnostics.reason_codes == ("schema_invalid_records_skipped",)
    assert diagnostics.incomplete is True
    assert diagnostics.degraded is True
    assert diagnostics.complete is False


def test_all_invalid_records_fail_rather_than_reporting_completeness():
    broken = _listing(
        ['<div id="job_list_" class="job_list_row">no id</div>'],
        total=1,
        pages="1.0",
        current=1,
    )
    source, _ = _source([broken])
    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(_company())


def test_ambiguous_listing_structure_fails_closed():
    nested = _listing(
        ['<div id="job_list_1" class="job_list_row"><div id="job_list_2" '
         'class="job_list_row"></div></div>'],
        total=1,
        pages="1.0",
        current=1,
    )
    source, _ = _source([nested])
    with pytest.raises(SourceSchemaError, match="listing structure was ambiguous"):
        source.fetch(_company())


def test_missing_listing_html_or_metadata_fails_closed():
    for payload, expected in (
        ({"Result": ""}, "did not contain listing HTML"),
        ({"Result": 5}, "did not contain listing HTML"),
        ([], "expected a JSON object"),
        (
            {"Result": '<div class="number_of_results"></div>'},
            "explicit total result count",
        ),
        (
            {
                "Result": '<span class="total_results">1</span>'
                '<div id="jPaginateCurrPage">1</div>'
            },
            "explicit page count",
        ),
    ):
        source, _ = _source([payload])
        with pytest.raises(SourceSchemaError, match=expected):
            source.fetch(_company())


# --- retry, pacing, and diagnostics --------------------------------------


def test_transient_failures_retry_within_a_bounded_crawl_budget():
    first, second = _pages(20, 10)
    source, _ = _source([URLError("temporary"), first, second])

    rows = source.fetch(_company())

    assert len(rows) == 20
    assert source.retry_attempts == 1
    assert source.last_health_diagnostics.failed_request_count == 1
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.complete is True


def test_pages_are_paced_conservatively_between_requests():
    delays: list[float] = []
    opener = _Opener([BOOTSTRAP, _created(), *_pages(30, 10)])
    source = TaleoSourcingSource(
        session_factory=lambda: SimpleNamespace(opener=opener, cookies=(object(),)),
        sleeper=delays.append,
        jitter=lambda _low, _high: 0.0,
    )

    source.fetch(_company())

    assert delays == [source.page_delay_seconds] * 2
    assert 0 < source.page_delay_seconds <= 1.0


# --- configuration, registry, and boundary integration -------------------


def test_real_watchlist_builds_exact_arup_taleo_sourcing_configuration():
    config = load_watchlist()
    arup = next(company for company in config.companies if company.name == "Arup")

    assert arup.ats == "taleo_sourcing"
    assert arup.taleo_sourcing_host == "jobs.arup.com"
    assert arup.taleo_sourcing_site == "default657"
    assert arup.source_url == "https://jobs.arup.com/"
    assert arup.module == ""


def test_registry_builds_the_adapter_without_extra_construction_arguments():
    from watcher.sources.registry import DIRECT_ATS, build_direct_sources

    assert "taleo_sourcing" in DIRECT_ATS
    built = build_direct_sources()
    assert isinstance(built["taleo_sourcing"], TaleoSourcingSource)
    assert built["taleo_sourcing"].name == "taleo_sourcing"


def test_config_fingerprint_and_origin_key_use_the_configured_portal():
    from watcher.collection_concurrency import direct_origin_key

    assert direct_origin_key(
        "taleo_sourcing", taleo_sourcing_host="jobs.arup.com"
    ) == "https://jobs.arup.com"
    assert direct_origin_key("taleo_sourcing") == "adapter:taleo_sourcing"


def test_adapter_reuses_canonical_owners_and_avoids_the_base_facade():
    module = ROOT / "watcher/sources/taleo_sourcing.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "watcher.sources.base" not in imported
    assert {
        "watcher.sources.contracts",
        "watcher.sources.direct",
        "watcher.sources.parsing",
        "watcher.sources.retry",
        "watcher.sources.rows",
        "watcher.sources.transport",
    } <= imported
    assert not any(name.startswith("watcher.collection") for name in imported)

    from watcher.sources.direct import DirectRecordAdapter, SinglePayloadDirectAdapter

    assert issubclass(TaleoSourcingSource, DirectRecordAdapter)
    assert not issubclass(TaleoSourcingSource, SinglePayloadDirectAdapter)
