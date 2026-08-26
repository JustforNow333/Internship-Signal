from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.config import (
    DEFAULT_WATCHLIST_PATH,
    CompanyCfg,
    ConfigError,
    WatcherConfig,
    load_watchlist,
)
from watcher.health_alerts import is_minor_degradation
from watcher.run import CollectionStats, _default_direct_sources, collect_rows
from watcher.source_health import DIRECT_STATUS_DEGRADED, calculate_next_state
from watcher.sources import (
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    SuccessFactorsSource,
)
from watcher.sources.successfactors import DEFAULT_MAX_CRAWL_RETRIES


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def company(
    host: str = "careers.example.test",
    prefix: str = "brand",
    locale: str = "",
) -> CompanyCfg:
    root = f"/{prefix}/" if prefix else "/"
    return CompanyCfg(
        name="SuccessFactors Example",
        ats="successfactors",
        successfactors_host=host,
        successfactors_site_prefix=prefix,
        successfactors_locale=locale,
        source_url=f"https://{host}{root}",
    )


def page_html(
    *,
    first: int,
    last: int,
    total: int,
    page: int,
    pages: int,
    ids: tuple[int, ...],
    prefix: str = "brand",
    host: str = "",
) -> str:
    root = f"/{prefix}" if prefix else ""
    rows = []
    for posting_id in ids:
        href = f"{root}/job/City-Role-State/{posting_id}/"
        if host:
            href = f"https://{host}{href}"
        rows.append(
            '<tr class="data-row"><td><span class="jobTitle hidden-phone">'
            f'<a class="jobTitle-link" href="{href}">Role {posting_id}</a>'
            '</span></td><td><span class="jobLocation">City, ST, US</span>'
            "</td></tr>"
        )
    return (
        '<html><body><a href="/search/">Generic search navigation</a>'
        '<table class="searchResults"><tbody>'
        + "".join(rows)
        + "</tbody></table>"
        f'<span class="paginationLabel">Results {first} – {last} of {total}</span>'
        f'<span class="srHelp">Page {page} of {pages}</span>'
        "</body></html>"
    )


def test_multi_page_board_maps_canonical_fields_and_identity():
    pages = iter((fixture("successfactors_page_1.html"), fixture("successfactors_page_2.html")))
    urls = []

    def request_text(url, source_name):
        urls.append(url)
        assert source_name == "successfactors"
        return next(pages)

    source = SuccessFactorsSource(request_text=request_text)
    rows = source.fetch(company())

    assert [
        parse_qs(urlsplit(url).query, keep_blank_values=True) for url in urls
    ] == [
        {"q": [""], "locationsearch": [""], "startrow": ["0"]},
        {"q": [""], "locationsearch": [""], "startrow": ["2"]},
    ]
    assert len(rows) == 3
    assert rows[0]["company"] == "SuccessFactors Example"
    assert rows[0]["title"] == "Software Engineer Intern"
    assert rows[0]["location"] == "Austin, TX, US"
    assert rows[0]["date_posted"] == "2026-08-01"
    assert rows[0]["source_url"] == (
        "https://careers.example.test/brand/job/"
        "Austin-Software-Engineer-Intern-TX-78701/1001/"
    )
    assert rows[0]["extra"]["native_posting_id"] == "1001"
    assert rows[0]["extra"]["source_requisition_id"] == (
        "careers.example.test:brand:1001"
    )
    assert rows[2]["source_url"].endswith("/1003/www.blank.com")
    assert source.request_count == 2
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.degraded is False
    assert source.last_health_diagnostics.reason_codes == ()


def test_root_site_single_page_and_locale_query_are_supported():
    html = page_html(first=1, last=1, total=1, page=1, pages=1, ids=(2001,), prefix="")
    urls = []
    rows = SuccessFactorsSource(
        request_text=lambda url, _name: urls.append(url) or html
    ).fetch(company(prefix="", locale="en_US"))

    assert len(rows) == 1
    assert rows[0]["source_url"] == (
        "https://careers.example.test/job/City-Role-State/2001/"
    )
    assert parse_qs(urlsplit(urls[0]).query, keep_blank_values=True) == {
        "q": [""],
        "locationsearch": [""],
        "startrow": ["0"],
        "locale": ["en_US"],
    }


def test_root_site_accepts_one_same_host_detail_brand_prefix():
    html = page_html(
        first=1,
        last=1,
        total=1,
        page=1,
        pages=1,
        ids=(2002,),
        prefix="",
    ).replace(
        "/job/City-Role-State/2002/",
        "/ExxonMobil/job/City-Role-State/2002/",
    )

    rows = SuccessFactorsSource(request_text=lambda *_: html).fetch(
        company(prefix="")
    )

    assert rows[0]["source_url"] == (
        "https://careers.example.test/ExxonMobil/job/City-Role-State/2002/"
    )


def test_root_site_rejects_multiple_unconfigured_detail_prefix_segments():
    html = page_html(
        first=1,
        last=1,
        total=1,
        page=1,
        pages=1,
        ids=(2003,),
        prefix="",
    ).replace(
        "/job/City-Role-State/2003/",
        "/one/two/job/City-Role-State/2003/",
    )

    with pytest.raises(SourceSchemaError, match="none were valid"):
        SuccessFactorsSource(request_text=lambda *_: html).fetch(company(prefix=""))


def test_explicit_zero_board_succeeds_without_rows():
    source = SuccessFactorsSource(
        request_text=lambda *_: fixture("successfactors_zero.html")
    )

    assert source.fetch(company()) == []
    assert source.last_health_diagnostics.complete is True


def test_malformed_posting_is_skipped_but_valid_neighbor_is_retained():
    html = page_html(first=1, last=2, total=2, page=1, pages=1, ids=(3001, 3002))
    html = html.replace(
        "/brand/job/City-Role-State/3002/",
        "/brand/search/",
    )
    source = SuccessFactorsSource(request_text=lambda *_: html)

    rows = source.fetch(company())

    assert [row["extra"]["native_posting_id"] for row in rows] == ["3001"]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.schema_error_row_count == 1
    assert diagnostics.reason_codes == ("schema_invalid_records_skipped",)
    assert diagnostics.degraded is True
    assert diagnostics.complete is False


def test_nonempty_all_malformed_page_fails():
    html = page_html(first=1, last=1, total=1, page=1, pages=1, ids=(3001,))
    html = html.replace("/brand/job/City-Role-State/3001/", "/brand/search/")

    with pytest.raises(SourceSchemaError, match="none were valid"):
        SuccessFactorsSource(request_text=lambda *_: html).fetch(company())


def test_repeated_page_is_rejected():
    first = page_html(first=1, last=2, total=4, page=1, pages=2, ids=(1, 2))
    repeated = first.replace("Results 1 – 2", "Results 3 – 4").replace(
        "Page 1 of 2", "Page 2 of 2"
    )
    pages = iter((first, repeated))
    offsets = []

    def request(url, _name):
        offsets.append(int(parse_qs(urlsplit(url).query)["startrow"][0]))
        return next(pages)

    with pytest.raises(SourceSchemaError, match="repeated"):
        SuccessFactorsSource(request_text=request).fetch(company())

    assert offsets == [0, 2]


def test_premature_pagination_end_is_rejected():
    html = page_html(first=1, last=2, total=3, page=1, pages=1, ids=(1, 2))

    with pytest.raises(SourceSchemaError, match="pagination"):
        SuccessFactorsSource(request_text=lambda *_: html).fetch(company())


def test_second_changing_total_fails_closed_after_only_one_restart():
    offsets = []
    pages = iter(
        (
            page_html(first=1, last=2, total=4, page=1, pages=2, ids=(1, 2)),
            page_html(first=3, last=4, total=5, page=2, pages=3, ids=(3, 4)),
            page_html(first=1, last=2, total=6, page=1, pages=3, ids=(11, 12)),
            page_html(first=3, last=4, total=7, page=2, pages=4, ids=(13, 14)),
        )
    )

    def request(url, _name):
        offsets.append(int(parse_qs(urlsplit(url).query)["startrow"][0]))
        return next(pages)

    source = SuccessFactorsSource(request_text=request)
    with pytest.raises(
        SourceSchemaError,
        match=r"total changed.*expected=6 observed=7 page=2 startrow=2",
    ):
        source.fetch(company())

    assert offsets == [0, 2, 0, 2]
    assert source.request_count == 4
    assert source.last_health_diagnostics.succeeded is None


def test_second_changing_total_exposes_no_partial_rows_to_collection():
    pages = iter(
        (
            page_html(first=1, last=2, total=4, page=1, pages=2, ids=(1, 2)),
            page_html(first=3, last=4, total=5, page=2, pages=3, ids=(3, 4)),
            page_html(first=1, last=2, total=6, page=1, pages=3, ids=(11, 12)),
            page_html(first=3, last=4, total=7, page=2, pages=4, ids=(13, 14)),
        )
    )
    source = SuccessFactorsSource(request_text=lambda *_: next(pages))
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"successfactors": source},
        github_source=[],
        stats=stats,
    )

    assert rows == []
    assert len(errors) == 1
    assert "successfactors total changed during pagination" in errors[0]
    attempt = stats.source_attempts[0]
    assert attempt.succeeded is False
    assert attempt.rows_returned is None
    assert attempt.reason_codes == ("schema_failure",)
    assert attempt.incomplete is True
    assert attempt.complete is False


def test_changing_total_restarts_whole_crawl_without_leaking_first_rows():
    offsets = []
    pages = iter(
        (
            page_html(first=1, last=2, total=4, page=1, pages=2, ids=(1, 2)),
            page_html(first=3, last=4, total=5, page=2, pages=3, ids=(3, 4)),
            page_html(first=1, last=2, total=4, page=1, pages=2, ids=(11, 12)),
            page_html(first=3, last=4, total=4, page=2, pages=2, ids=(13, 14)),
        )
    )

    def request(url, _name):
        offsets.append(int(parse_qs(urlsplit(url).query)["startrow"][0]))
        return next(pages)

    source = SuccessFactorsSource(request_text=request)
    rows = source.fetch(company())

    assert offsets == [0, 2, 0, 2]
    assert [row["extra"]["native_posting_id"] for row in rows] == [
        "11",
        "12",
        "13",
        "14",
    ]
    assert source.request_count == 4
    assert source.request_attempts == 4
    assert source.retry_attempts == 0
    diagnostics = source.last_health_diagnostics
    assert diagnostics.reason_codes == ("pagination_restart_recovered",)
    assert diagnostics.degraded is True
    assert diagnostics.complete is True
    assert diagnostics.incomplete is False


def test_restarted_crawl_keeps_the_existing_maximum_page_bound():
    offsets = []
    pages = iter(
        (
            page_html(first=1, last=1, total=2, page=1, pages=2, ids=(1,)),
            page_html(first=2, last=2, total=3, page=2, pages=3, ids=(2,)),
            page_html(first=1, last=1, total=3, page=1, pages=3, ids=(11,)),
            page_html(first=2, last=2, total=3, page=2, pages=3, ids=(12,)),
        )
    )

    def request(url, _name):
        offsets.append(int(parse_qs(urlsplit(url).query)["startrow"][0]))
        return next(pages)

    source = SuccessFactorsSource(request_text=request, max_pages=2)
    with pytest.raises(SourceSchemaError, match="maximum page safeguard"):
        source.fetch(company())

    assert offsets == [0, 1, 0, 1]
    assert source.request_count == 4


@pytest.mark.parametrize(
    "href",
    (
        "https://other.example.test/brand/job/City-Role-State/4001/",
        "/brand/search/",
        "/brand/job/",
    ),
)
def test_invalid_cross_host_and_generic_posting_links_fail(href):
    html = page_html(first=1, last=1, total=1, page=1, pages=1, ids=(4001,))
    html = html.replace("/brand/job/City-Role-State/4001/", href)

    with pytest.raises(SourceSchemaError, match="none were valid"):
        SuccessFactorsSource(request_text=lambda *_: html).fetch(company())


def test_duplicate_id_with_conflicting_url_is_rejected():
    html = page_html(first=1, last=2, total=2, page=1, pages=1, ids=(5001, 5002))
    html = html.replace("/5002/", "/5001/")

    with pytest.raises(SourceSchemaError, match="conflicting posting identity"):
        SuccessFactorsSource(request_text=lambda *_: html).fetch(company())


def test_exact_duplicate_rows_are_counted_and_collapsed():
    html = page_html(first=1, last=2, total=2, page=1, pages=1, ids=(6001, 6001))
    source = SuccessFactorsSource(request_text=lambda *_: html)

    rows = source.fetch(company())

    assert len(rows) == 1
    assert source.last_health_diagnostics.duplicate_row_count == 1


def test_very_large_board_enumerates_every_explicit_page():
    total = 300

    def request(url, _name):
        start = int(parse_qs(urlsplit(url).query)["startrow"][0])
        posting_id = 10_000 + start
        return page_html(
            first=start + 1,
            last=start + 1,
            total=total,
            page=start + 1,
            pages=total,
            ids=(posting_id,),
        )

    source = SuccessFactorsSource(request_text=request)
    rows = source.fetch(company())

    assert len(rows) == total
    assert source.request_count == total
    assert len({row["extra"]["source_requisition_id"] for row in rows}) == total


def test_structurally_invalid_page_is_not_an_empty_board():
    with pytest.raises(SourceSchemaError, match="listing contract"):
        SuccessFactorsSource(
            request_text=lambda *_: "<html><body>Careers home</body></html>"
        ).fetch(company())


def test_mixed_record_loss_flows_into_existing_source_health_contract():
    html = page_html(first=1, last=2, total=2, page=1, pages=1, ids=(7001, 7002))
    html = html.replace("/brand/job/City-Role-State/7002/", "/brand/search/")
    source = SuccessFactorsSource(request_text=lambda *_: html)
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"successfactors": source},
        stats=stats,
    )

    assert len(rows) == 1
    assert errors == []
    attempt = stats.source_attempts[0]
    assert attempt.succeeded is True
    assert attempt.degraded is True
    assert attempt.complete is False
    assert attempt.schema_error_row_count == 1


def test_registry_origin_and_snapshot_include_successfactors_without_replacing_adapters():
    sources = _default_direct_sources()
    assert isinstance(sources["successfactors"], SuccessFactorsSource)
    assert {"greenhouse", "workday", "icims", "talentbrew"} <= set(sources)

    configured = company()
    config = WatcherConfig(companies=(configured,))
    assert direct_origin_key(
        "successfactors", successfactors_host=configured.successfactors_host
    ) == "https://careers.example.test"
    for changed in (
        replace(configured, successfactors_host="other.example.test"),
        replace(configured, successfactors_site_prefix="other"),
        replace(configured, successfactors_locale="en_GB"),
    ):
        assert collection_config_fingerprint(WatcherConfig(companies=(changed,))) != (
            collection_config_fingerprint(config)
        )


def test_constructor_validates_attempt_limit_before_crawl_retry_limit():
    with pytest.raises(ValueError) as raised:
        SuccessFactorsSource(max_attempts=0, max_crawl_retries=-1)

    assert str(raised.value) == "max_attempts must be between 1 and 3"


def test_default_watchlist_uses_verified_successfactors_configuration():
    companies = {
        item.name: item for item in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
    }
    expected = {
        "EY": ("careers.ey.com", "ey"),
        "Exxon Mobil": ("jobs.exxonmobil.com", ""),
        "MIT Lincoln Laboratory": ("careers.ll.mit.edu", ""),
        "Nomura": ("careers.nomura.com", "Nomura"),
        "Vaisala": ("careers.vaisala.com", ""),
    }
    for name, (host, prefix) in expected.items():
        configured = companies[name]
        assert configured.ats == "successfactors"
        assert configured.successfactors_host == host
        assert configured.successfactors_site_prefix == prefix
        assert configured.successfactors_locale == ""
        assert configured.module == ""
        assert configured.coverage_status == ""
        assert configured.platform_family == ""


@pytest.mark.parametrize(
    ("lines", "message"),
    (
        ('    successfactors_site_prefix: "brand"\n', "successfactors_host"),
        (
            '    successfactors_host: "https://careers.example.test"\n',
            "hostname",
        ),
        (
            '    successfactors_host: "careers.example.test"\n'
            '    successfactors_site_prefix: "bad/path"\n',
            "site_prefix",
        ),
        (
            '    successfactors_host: "careers.example.test"\n'
            '    successfactors_locale: "english"\n',
            "locale",
        ),
    ),
)
def test_successfactors_configuration_requires_explicit_safe_scope(tmp_path, lines, message):
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
        '  - name: "SuccessFactors Example"\n    ats: successfactors\n'
        + lines
        + '    source_url: "https://careers.example.test/brand/"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_watchlist(path)


def test_runtime_rejects_invalid_config_without_request():
    requested = []
    source = SuccessFactorsSource(
        request_text=lambda url, _name: requested.append(url)
    )

    with pytest.raises(SourceError, match="valid successfactors_host"):
        source.fetch(company(host="-bad.example.test"))

    assert requested == []


# --- bounded transport retry ----------------------------------------------
#
# A very large board needs hundreds of sequential page requests, so a rare
# transient transport failure otherwise fails the whole crawl. Retries re-fetch
# the identical startrow and never relax a completeness check.


def _retryable() -> SourceFetchError:
    return SourceFetchError(
        "successfactors GET failed: code=timeout endpoint=https://careers.example.test/",
        error_code="timeout",
        retryable=True,
    )


def _two_page_board() -> tuple[str, str]:
    return (
        page_html(first=1, last=2, total=4, page=1, pages=2, ids=(8001, 8002)),
        page_html(first=3, last=4, total=4, page=2, pages=2, ids=(8003, 8004)),
    )


class _FlakyBoard:
    """Serve fixed pages, raising injected errors on chosen attempt numbers."""

    def __init__(
        self,
        pages: tuple[str, ...],
        failures: dict[int, Exception] | None = None,
    ) -> None:
        self._pages = pages
        self._failures = dict(failures or {})
        self.attempts = 0

    def __call__(self, url: str, _name: str) -> str:
        self.attempts += 1
        failure = self._failures.get(self.attempts)
        if failure is not None:
            raise failure
        start = int(parse_qs(urlsplit(url).query)["startrow"][0])
        return self._pages[start // 2]


def test_transient_page_failure_is_retried_and_matches_a_clean_crawl():
    expected = SuccessFactorsSource(
        request_text=_FlakyBoard(_two_page_board())
    ).fetch(company())

    delays: list[float] = []
    board = _FlakyBoard(_two_page_board(), {2: _retryable()})
    source = SuccessFactorsSource(
        request_text=board, sleeper=delays.append, jitter=lambda _a, _b: 0.0
    )

    rows = source.fetch(company())

    assert rows == expected
    assert source.retry_attempts == 1
    # One extra HTTP attempt, but still only two pages of the board.
    assert source.request_attempts == 3
    assert source.request_count == 2
    assert delays == [1.0]


def test_recovered_retry_is_reported_as_minor_degradation():
    board = _FlakyBoard(_two_page_board(), {2: _retryable()})
    source = SuccessFactorsSource(
        request_text=board, sleeper=lambda _delay: None, jitter=lambda _a, _b: 0.0
    )
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"successfactors": source},
        stats=stats,
    )

    assert len(rows) == 4
    assert errors == []
    attempt = stats.source_attempts[0]
    assert attempt.succeeded is True
    assert attempt.failed_request_count == 1
    assert attempt.reason_codes == ("request_retry_recovered",)
    assert attempt.degraded is True
    # The crawl still reached the exact expected total, so it stays whole.
    assert attempt.complete is True
    assert attempt.incomplete is False

    state = calculate_next_state(None, attempt)
    assert state.status == DIRECT_STATUS_DEGRADED
    assert is_minor_degradation(state) is True


def test_non_retryable_page_failure_is_not_retried():
    delays: list[float] = []
    board = _FlakyBoard(
        _two_page_board(),
        {2: SourceFetchError("successfactors GET failed: code=forbidden")},
    )
    source = SuccessFactorsSource(
        request_text=board, sleeper=delays.append, jitter=lambda _a, _b: 0.0
    )

    with pytest.raises(SourceFetchError):
        source.fetch(company())

    assert board.attempts == 2
    assert source.retry_attempts == 0
    assert delays == []


def test_exhausting_per_page_attempts_fails_the_whole_crawl():
    delays: list[float] = []
    board = _FlakyBoard(
        _two_page_board(),
        {2: _retryable(), 3: _retryable(), 4: _retryable()},
    )
    source = SuccessFactorsSource(
        request_text=board, sleeper=delays.append, jitter=lambda _a, _b: 0.0
    )

    with pytest.raises(SourceFetchError):
        source.fetch(company())

    # One good page plus three attempts on the second page, then failure.
    assert board.attempts == 4
    assert source.retry_attempts == 2
    assert delays == [1.0, 3.0]
    # No partial result: success diagnostics were never published.
    assert source.last_health_diagnostics.succeeded is None


def test_crawl_wide_retry_budget_bounds_a_long_crawl_and_resets_per_fetch():
    total = 8
    delays: list[float] = []
    flaky = {"enabled": True}
    seen: dict[int, int] = {}

    def request(url: str, _name: str) -> str:
        start = int(parse_qs(urlsplit(url).query)["startrow"][0])
        if flaky["enabled"]:
            seen[start] = seen.get(start, 0) + 1
            if seen[start] == 1:
                raise _retryable()
        return page_html(
            first=start + 1,
            last=start + 1,
            total=total,
            page=start + 1,
            pages=total,
            ids=(20_000 + start,),
        )

    source = SuccessFactorsSource(
        request_text=request, sleeper=delays.append, jitter=lambda _a, _b: 0.0
    )

    with pytest.raises(SourceFetchError):
        source.fetch(company())

    # Every page failing once exhausts the crawl budget before the board ends.
    assert source.retry_attempts == DEFAULT_MAX_CRAWL_RETRIES
    assert len(delays) == DEFAULT_MAX_CRAWL_RETRIES
    # Bounded runtime: no real sleeping, and each delay is capped.
    assert max(delays) <= 5.0
    assert source.last_health_diagnostics.succeeded is None

    flaky["enabled"] = False
    rows = source.fetch(company())

    assert len(rows) == total
    assert source.retry_attempts == 0
    assert source.last_health_diagnostics.degraded is False


def test_whole_crawl_restart_gets_a_fresh_transport_retry_budget():
    first_page, second_page = _two_page_board()
    changed_page = page_html(
        first=3,
        last=4,
        total=5,
        page=2,
        pages=3,
        ids=(8003, 8004),
    )
    outcomes = iter(
        (
            first_page,
            _retryable(),
            changed_page,
            first_page,
            _retryable(),
            second_page,
        )
    )
    offsets = []
    delays = []

    def request(url, _name):
        offsets.append(int(parse_qs(urlsplit(url).query)["startrow"][0]))
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    source = SuccessFactorsSource(
        request_text=request,
        sleeper=delays.append,
        jitter=lambda _a, _b: 0.0,
        max_crawl_retries=1,
    )

    rows = source.fetch(company())

    assert len(rows) == 4
    assert offsets == [0, 2, 2, 0, 2, 2]
    assert delays == [1.0, 1.0]
    assert source.request_count == 4
    assert source.request_attempts == 6
    assert source.retry_attempts == 2
    diagnostics = source.last_health_diagnostics
    assert diagnostics.failed_request_count == 2
    assert diagnostics.reason_codes == (
        "request_retry_recovered",
        "pagination_restart_recovered",
    )
    assert diagnostics.degraded is True
    assert diagnostics.complete is True


def test_a_retried_page_returning_a_changed_total_still_fails_closed():
    first_page, _ = _two_page_board()
    changed = page_html(first=3, last=4, total=5, page=2, pages=3, ids=(8003, 8004))
    board = _FlakyBoard((first_page, changed), {2: _retryable()})
    source = SuccessFactorsSource(
        request_text=board, sleeper=lambda _delay: None, jitter=lambda _a, _b: 0.0
    )

    with pytest.raises(SourceSchemaError, match="total changed during pagination"):
        source.fetch(company())

    # The retry happened; the total-stability check still rejected the crawl.
    assert source.retry_attempts == 1
