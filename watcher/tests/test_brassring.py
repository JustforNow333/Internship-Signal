"""Offline contract, completeness, and diagnostics tests for BrassRing."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from watcher.config import CompanyCfg, load_watchlist
from watcher.sources import BrassRingSource, SourceSchemaError


FIXTURES = Path(__file__).with_name("fixtures")
BOOTSTRAP = (FIXTURES / "brassring_bootstrap.html").read_text(encoding="utf-8")
LISTING = json.loads(
    (FIXTURES / "brassring_listing_page.json").read_text(encoding="utf-8")
)


def _company(**overrides) -> CompanyCfg:
    values = {
        "name": "UBS",
        "ats": "brassring",
        "brassring_host": "jobs.ubs.com",
        "brassring_partner_id": "25008",
        "brassring_site_id": "5131",
        "source_url": (
            "https://jobs.ubs.com/TGnewUI/Search/Home/Home"
            "?partnerid=25008&siteid=5131"
        ),
    }
    values.update(overrides)
    return CompanyCfg(**values)


def _posting(
    native_id: int | str,
    *,
    link_id: int | str | None = None,
    link: str | None = None,
) -> dict:
    native = str(native_id)
    link_native = native if link_id is None else str(link_id)
    return {
        "Link": link
        or (
            "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
            "?partnerid=25008&siteid=5131&PageType=JobDetails"
            f"&jobid={link_native}"
        ),
        "Questions": [
            {"QuestionName": "reqid", "Value": native},
            {"QuestionName": "jobtitle", "Value": f"Job {native}"},
            {"QuestionName": "formtext23", "Value": "New York, United States"},
            {"QuestionName": "jobdescription", "Value": f"Description {native}"},
            {"QuestionName": "formtext21", "Value": "Technology"},
            {"QuestionName": "department", "Value": "Engineering"},
            {"QuestionName": "lastupdated", "Value": "31-Aug-2026"},
        ],
    }


def _page(records: list, total: int, *, total_jobs_count: int = 0) -> dict:
    # The live board always reports TotalJobsCount as 0; JobsCount is the total.
    return {
        "JobsCount": total,
        "TotalJobsCount": total_jobs_count,
        "Jobs": {"Job": records},
    }


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
            "Content-Type": "text/html; charset=utf-8" if html else "application/json"
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


def _source(responses: list[object], **kwargs) -> tuple[BrassRingSource, _Opener]:
    opener = _Opener([BOOTSTRAP, *responses])
    session = SimpleNamespace(opener=opener, cookies=(object(),))
    source = BrassRingSource(
        session_factory=lambda: session,
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0.0,
        **kwargs,
    )
    return source, opener


def test_bootstrap_headers_config_and_two_stable_50_36_snapshots():
    first = [_posting(index) for index in range(1, 51)]
    second = [_posting(index) for index in range(51, 87)]
    source, opener = _source(
        [
            _page(first, 86),
            _page(second, 86),
            _page(first, 86),
            _page(second, 86),
        ]
    )

    rows = source.fetch(_company())

    assert len(rows) == 86
    assert source.bootstrap_requests == 1
    assert source.snapshot_passes_requested == 2
    assert source.pages_requested == 4
    assert opener.requests[0].get_method() == "GET"
    assert opener.requests[0].full_url == _company().source_url
    for page_number, request in enumerate(opener.requests[1:], start=1):
        payload = json.loads(request.data.decode("utf-8"))
        assert request.get_method() == "POST"
        assert request.full_url == (
            "https://jobs.ubs.com/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
        )
        assert request.get_header("Rft") == "fixture-request-token"
        assert request.get_header("Referer") == _company().source_url
        assert payload["partnerId"] == 25008
        assert payload["siteId"] == 5131
        assert payload["SortType"] == "JobTitle"
        assert payload["pageNumber"] == ((page_number - 1) % 2) + 1
        assert payload["encryptedSessionValue"] == "fixture-encrypted-session"
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.retained_row_count == 86


def test_sanitized_fixture_maps_rows_without_treating_lastupdated_as_posted_date():
    source, _opener = _source([LISTING, LISTING])

    rows = source.fetch(_company())

    assert rows[0]["title"] == "Technology Intern"
    assert rows[0]["location"] == "New York, United States"
    assert rows[0]["description"] == "Build reliable systems."
    assert rows[0]["date_posted"] == ""
    assert rows[0]["source_url"] == (
        "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
        "?partnerid=25008&siteid=5131&PageType=JobDetails&jobid=100001"
    )
    assert rows[0]["extra"]["source_requisition_id"] == (
        "brassring:jobs.ubs.com:25008:5131:100001"
    )
    assert rows[0]["extra"]["source_adapter"] == "brassring"
    assert rows[0]["extra"]["business_area"] == "Technology"
    assert rows[0]["extra"]["brassring_posting_site_id"] == "5131"
    assert len(rows) == 3


def test_jobscount_is_the_only_total_and_totaljobscount_is_ignored():
    page = _page([_posting(1)], 1, total_jobs_count=999)
    source, _ = _source([page, page])

    assert len(source.fetch(_company())) == 1

    missing = {"JobsCount": 1, "Jobs": {"Job": [_posting(1)]}}
    without_metadata, _ = _source([missing, missing])
    assert len(without_metadata.fetch(_company())) == 1

    no_total, _ = _source([{"Jobs": {"Job": []}}])
    with pytest.raises(SourceSchemaError, match="JobsCount"):
        no_total.fetch(_company())


def test_unstable_identity_snapshots_fail_after_bounded_passes():
    source, _opener = _source(
        [_page([_posting(1)], 1), _page([_posting(2)], 1), _page([_posting(1)], 1)]
    )

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(_company())

    assert source.snapshot_passes_requested == 3
    assert source.last_health_diagnostics.succeeded is None


def test_total_changes_fail_closed_within_or_between_snapshots():
    within, _ = _source([_page([_posting(i) for i in range(50)], 51), _page([], 52)])
    with pytest.raises(SourceSchemaError, match="during pagination"):
        within.fetch(_company())

    between, _ = _source([_page([_posting(1)], 1), _page([_posting(1), _posting(2)], 2)])
    with pytest.raises(SourceSchemaError, match="between complete snapshots"):
        between.fetch(_company())


def test_repeated_and_premature_pages_fail_closed():
    page = [_posting(index) for index in range(1, 51)]
    repeated, _ = _source([_page(page, 100), _page(page, 100)])
    with pytest.raises(SourceSchemaError, match="repeated pagination page"):
        repeated.fetch(_company())

    empty, _ = _source([_page(page, 51), _page([], 51)])
    with pytest.raises(SourceSchemaError, match="ended before"):
        empty.fetch(_company())

    short, _ = _source([_page(page[:49], 51)])
    with pytest.raises(SourceSchemaError, match="ended prematurely"):
        short.fetch(_company())


def test_localized_sibling_site_postings_keep_their_own_reachable_url():
    localized = _posting(
        7,
        link=(
            "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
            "?partnerid=25008&siteid=5132&PageType=JobDetails&jobid=7"
            "&frmSiteId=5131"
        ),
    )
    page = _page([localized], 1)
    source, _ = _source([page, page])

    rows = source.fetch(_company())

    assert rows[0]["source_url"] == (
        "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
        "?partnerid=25008&siteid=5132&PageType=JobDetails&jobid=7&frmSiteId=5131"
    )
    assert rows[0]["extra"]["brassring_posting_site_id"] == "5132"
    assert rows[0]["extra"]["brassring_site_id"] == "5131"
    assert rows[0]["extra"]["source_requisition_id"] == (
        "brassring:jobs.ubs.com:25008:5131:7"
    )
    assert source.last_health_diagnostics.complete is True


@pytest.mark.parametrize(
    "link",
    (
        # Another board's posting that was never reached from the configured one.
        "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
        "?partnerid=25008&siteid=5132&PageType=JobDetails&jobid=7",
        # Reached from some other board.
        "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
        "?partnerid=25008&siteid=5132&PageType=JobDetails&jobid=7&frmSiteId=9999",
        # Another partner entirely.
        "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
        "?partnerid=99999&siteid=5131&PageType=JobDetails&jobid=7",
        # Unexpected extra parameter.
        "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
        "?partnerid=25008&siteid=5131&PageType=JobDetails&jobid=7&keyword=intern",
        # Non-numeric site.
        "https://jobs.ubs.com/TGnewUI/Search/home/HomeWithPreLoad"
        "?partnerid=25008&siteid=abc&PageType=JobDetails&jobid=7&frmSiteId=5131",
        # Off-host posting.
        "https://jobs.example.test/TGnewUI/Search/home/HomeWithPreLoad"
        "?partnerid=25008&siteid=5131&PageType=JobDetails&jobid=7",
    ),
)
def test_unreachable_or_foreign_posting_urls_are_rejected(link):
    source, _ = _source([_page([_posting(7, link=link)], 1)])

    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(_company())


def test_duplicate_requisition_ids_fail_closed():
    duplicate, _ = _source(
        [_page([_posting(1), _posting(1)], 2)]
    )
    with pytest.raises(SourceSchemaError, match="duplicate requisition ID"):
        duplicate.fetch(_company())


def test_link_disagreeing_with_reqid_is_rejected_as_schema_invalid():
    mixed = _page([_posting(1), _posting(2, link_id=3)], 2)
    source, _ = _source([mixed, mixed])

    rows = source.fetch(_company())

    assert [row["extra"]["brassring_native_id"] for row in rows] == ["1"]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.schema_error_row_count == 1
    assert diagnostics.malformed_row_count == 0
    assert diagnostics.reason_codes == ("schema_invalid_records_skipped",)
    assert diagnostics.complete is False

    only_conflict, _ = _source([_page([_posting(1, link_id=2)], 1)])
    with pytest.raises(SourceSchemaError, match="none were valid"):
        only_conflict.fetch(_company())


def test_malformed_records_are_degraded_and_all_malformed_fails():
    mixed = _page([_posting(1), "not-an-object"], 2)
    source, _ = _source([mixed, mixed])

    rows = source.fetch(_company())

    assert len(rows) == 1
    diagnostics = source.last_health_diagnostics
    assert diagnostics.malformed_row_count == 1
    assert diagnostics.schema_error_row_count == 0
    assert diagnostics.incomplete is True
    assert diagnostics.degraded is True
    assert diagnostics.complete is False
    assert diagnostics.reason_codes == ("malformed_records_skipped",)

    all_bad, _ = _source([_page(["not-an-object"], 1)])
    with pytest.raises(SourceSchemaError, match="none were valid"):
        all_bad.fetch(_company())


def test_explicit_zero_board_requires_two_stable_snapshots():
    empty = _page([], 0)
    source, _ = _source([empty, empty])

    assert source.fetch(_company()) == []
    assert source.snapshot_passes_requested == 2
    assert source.last_health_diagnostics.complete is True


def test_bootstrap_requires_cookie_and_matching_required_values():
    opener = _Opener([BOOTSTRAP])
    no_cookie = BrassRingSource(
        session_factory=lambda: SimpleNamespace(opener=opener, cookies=()),
        sleeper=lambda _delay: None,
    )
    with pytest.raises(SourceSchemaError, match="session cookie"):
        no_cookie.fetch(_company())

    wrong_site = BOOTSTRAP.replace('value="5131"', 'value="9999"')
    source, _ = _source([])
    source._session_factory = lambda: SimpleNamespace(
        opener=_Opener([wrong_site]), cookies=(object(),)
    )
    with pytest.raises(SourceSchemaError, match="site ID"):
        source.fetch(_company())


def test_transient_request_retry_is_bounded_and_reported():
    page = _page([_posting(1)], 1)
    source, _ = _source([URLError("temporary"), page, page])

    assert len(source.fetch(_company())) == 1
    assert source.retry_attempts == 1
    assert source.last_health_diagnostics.failed_request_count == 1
    assert source.last_health_diagnostics.reason_codes == (
        "request_retry_recovered",
    )
    assert source.last_health_diagnostics.complete is True


def test_real_watchlist_builds_exact_ubs_brassring_configuration():
    config = load_watchlist()
    ubs = next(company for company in config.companies if company.name == "UBS")

    assert ubs.ats == "brassring"
    assert ubs.brassring_host == "jobs.ubs.com"
    assert ubs.brassring_partner_id == "25008"
    assert ubs.brassring_site_id == "5131"
    assert ubs.module == ""
