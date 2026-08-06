"""Deterministic Workday listing pagination and total-reconciliation tests.

Every test drives the real ``WorkdaySource.fetch`` loop with an offline fake
transport, so pacing, retry, schema validation, and repeated-page safety all run
exactly as they do in production. No test performs network I/O.
"""

from __future__ import annotations

import pytest

from watcher.config import CompanyCfg
from watcher.sources.base import SourceFetchError, SourceSchemaError
from watcher.sources.workday import MAX_LISTING_PAGES, WorkdaySource

PAGE_SIZE = WorkdaySource.page_size


def workday_company(name="Merck"):
    return CompanyCfg(
        name=name,
        ats="workday",
        token="merck",
        workday_shard="wd5",
        workday_site="Search_Jobs",
        workday_detail_policy="none",
    )


def posting(index: int) -> dict:
    """Return a unique valid posting so pages never collide by fingerprint."""

    return {
        "title": f"Software Engineer Intern {index}",
        "externalPath": f"/job/Test/Software-Engineer_R{index}",
        "locationsText": "Rahway, NJ",
        "postedOn": "Posted Today",
        "jobDescription": "Build software.",
        "bulletFields": [f"R{index}"],
    }


def board(total_postings: int) -> list[dict]:
    return [posting(index) for index in range(total_postings)]


class FakeBoard:
    """Serves one deterministic board and records every requested offset."""

    def __init__(self, postings: list[dict], totals):
        self.postings = postings
        self.totals = totals
        self.offsets: list[int] = []

    def total_for(self, page_index: int):
        if callable(self.totals):
            return self.totals(page_index)
        if isinstance(self.totals, (list, tuple)):
            if page_index < len(self.totals):
                return self.totals[page_index]
            return self.totals[-1] if self.totals else None
        return self.totals

    def __call__(self, _url, request, _source_name):
        page_index = len(self.offsets)
        offset = int(request["offset"])
        limit = int(request["limit"])
        self.offsets.append(offset)
        payload = {"jobPostings": self.postings[offset : offset + limit]}
        total = self.total_for(page_index)
        if total is not None:
            payload["total"] = total
        return payload


def run_fetch(monkeypatch, transport, company=None):
    monkeypatch.setattr("watcher.sources.workday.post_json", transport)
    source = WorkdaySource()
    rows = source.fetch(company or workday_company())
    return source, rows


def anomalies(source: WorkdaySource) -> dict[str, int]:
    return dict(source.last_diagnostics.pagination_total_anomalies)


def test_stable_total_on_every_page_collects_whole_board(monkeypatch):
    fake = FakeBoard(board(112), totals=112)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 112
    assert fake.offsets == [0, 20, 40, 60, 80, 100]
    assert source.last_diagnostics.listing_pages == 6
    assert source.last_diagnostics.listing_incomplete is False
    assert anomalies(source) == {}


def test_total_present_only_on_first_page_collects_whole_board(monkeypatch):
    fake = FakeBoard(board(112), totals=lambda page: 112 if page == 0 else None)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 112
    assert fake.offsets == [0, 20, 40, 60, 80, 100]
    assert source.last_diagnostics.listing_incomplete is False
    assert anomalies(source) == {"total_missing": 5}


def test_later_page_zero_total_does_not_truncate_at_forty(monkeypatch):
    """The observed production pattern: page one 112, later pages ``total: 0``."""

    fake = FakeBoard(board(112), totals=lambda page: 112 if page == 0 else 0)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 112
    assert len(rows) > 40
    assert fake.offsets == [0, 20, 40, 60, 80, 100]
    assert source.last_diagnostics.listing_incomplete is False
    assert anomalies(source) == {"total_zero": 5}


def test_page_one_over_forty_then_page_two_zero_total_keeps_paging(monkeypatch):
    """Page one reports more than 40 jobs; page two returns 20 with total 0."""

    fake = FakeBoard(board(60), totals=lambda page: 60 if page == 0 else 0)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 60
    assert fake.offsets == [0, 20, 40]
    assert source.last_diagnostics.listing_rows == 60
    assert source.last_diagnostics.listing_incomplete is False


def test_later_page_missing_total_keeps_first_reference(monkeypatch):
    fake = FakeBoard(board(45), totals=[45, None, None])

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 45
    assert fake.offsets == [0, 20, 40]
    assert anomalies(source) == {"total_missing": 2}
    assert source.last_diagnostics.listing_incomplete is False


def test_increasing_total_extends_pagination(monkeypatch):
    fake = FakeBoard(board(60), totals=[40, 60, 60])

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 60
    assert fake.offsets == [0, 20, 40]
    assert anomalies(source) == {"total_increased": 1}
    assert source.last_diagnostics.listing_incomplete is False


def test_decreasing_total_does_not_end_pagination_early(monkeypatch):
    fake = FakeBoard(board(60), totals=[60, 40, 40])

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 60
    assert fake.offsets == [0, 20, 40]
    assert anomalies(source) == {"total_below_offset": 1, "total_decreased": 1}


def test_total_below_current_offset_is_ignored(monkeypatch):
    fake = FakeBoard(board(60), totals=[60, 5, 60])

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 60
    assert fake.offsets == [0, 20, 40]
    assert anomalies(source) == {"total_below_offset": 1}


def test_negative_total_is_ignored(monkeypatch):
    fake = FakeBoard(board(40), totals=[40, -7])

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 40
    assert fake.offsets == [0, 20]
    assert anomalies(source) == {"total_negative": 1}


def test_final_partial_page_ends_pagination(monkeypatch):
    fake = FakeBoard(board(45), totals=45)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 45
    assert fake.offsets == [0, 20, 40]
    assert source.last_diagnostics.listing_incomplete is False


def test_partial_page_before_reported_total_marks_incomplete(monkeypatch):
    fake = FakeBoard(board(35), totals=200)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 35
    assert fake.offsets == [0, 20]
    assert source.last_diagnostics.listing_incomplete is True
    assert source.last_diagnostics.listing_incomplete_reasons == (
        "pagination_ended_early",
    )


def test_empty_terminal_page_ends_pagination(monkeypatch):
    fake = FakeBoard(board(40), totals=40)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 40
    # Offset 40 equals the trustworthy total, so no empty page is requested.
    assert fake.offsets == [0, 20]
    assert source.last_diagnostics.listing_incomplete is False


def test_empty_page_after_full_page_without_total_ends_pagination(monkeypatch):
    fake = FakeBoard(board(40), totals=None)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 40
    assert fake.offsets == [0, 20, 40]
    assert source.last_diagnostics.listing_pages == 3
    assert source.last_diagnostics.listing_incomplete is False


def test_empty_page_before_reported_total_marks_incomplete(monkeypatch):
    fake = FakeBoard(board(40), totals=[100, 0, 0])

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 40
    assert fake.offsets == [0, 20, 40]
    assert source.last_diagnostics.listing_incomplete is True
    assert source.last_diagnostics.listing_incomplete_reasons == (
        "pagination_ended_early",
    )


def test_repeated_full_page_stops_as_incomplete_with_usable_rows(monkeypatch):
    repeated = board(PAGE_SIZE)
    offsets: list[int] = []

    def transport(_url, request, _source_name):
        offsets.append(int(request["offset"]))
        return {"jobPostings": repeated, "total": 500}

    monkeypatch.setattr("watcher.sources.workday.post_json", transport)
    source = WorkdaySource()

    rows = source.fetch(workday_company())

    assert offsets == [0, PAGE_SIZE]
    assert len(rows) == PAGE_SIZE
    assert source.last_diagnostics.listing_pages == 1
    assert source.last_diagnostics.listing_rows == PAGE_SIZE
    assert source.last_diagnostics.listing_incomplete is True
    assert source.last_diagnostics.listing_incomplete_reasons == (
        "pagination_repeated_page",
    )


def test_repeated_page_without_usable_rows_still_raises(monkeypatch):
    """With nothing trustworthy in hand the repeated page stays fatal."""

    repeated = [{"title": f"Bad {index}"} for index in range(PAGE_SIZE)]
    offsets: list[int] = []

    def transport(_url, request, _source_name):
        offsets.append(int(request["offset"]))
        return {"jobPostings": repeated, "total": 500}

    monkeypatch.setattr("watcher.sources.workday.post_json", transport)
    source = WorkdaySource()

    with pytest.raises(SourceSchemaError, match="repeated pagination page"):
        source.fetch(workday_company())

    assert offsets == [0, PAGE_SIZE]


def test_failed_continuation_request_keeps_earlier_pages_as_incomplete(monkeypatch):
    postings = board(PAGE_SIZE)
    offsets: list[int] = []

    def transport(_url, request, _source_name):
        offset = int(request["offset"])
        offsets.append(offset)
        if offset == 0:
            return {"jobPostings": postings, "total": 500}
        raise SourceFetchError(
            "workday POST failed", error_code="network_failure", retryable=False
        )

    monkeypatch.setattr("watcher.sources.workday.post_json", transport)
    source = WorkdaySource()

    rows = source.fetch(workday_company())

    assert len(rows) == PAGE_SIZE
    assert offsets == [0, PAGE_SIZE]
    assert source.last_diagnostics.listing_request_failures == 1
    assert source.last_diagnostics.listing_incomplete is True
    assert source.last_diagnostics.listing_incomplete_reasons == (
        "pagination_request_failed",
    )


def test_failed_first_request_remains_a_fatal_source_failure(monkeypatch):
    def transport(_url, _request, _source_name):
        raise SourceFetchError(
            "workday POST failed", error_code="network_failure", retryable=False
        )

    monkeypatch.setattr("watcher.sources.workday.post_json", transport)
    source = WorkdaySource()

    with pytest.raises(SourceFetchError):
        source.fetch(workday_company())


def test_schema_error_on_continuation_page_keeps_earlier_pages(monkeypatch):
    postings = board(PAGE_SIZE)

    def transport(_url, request, _source_name):
        if int(request["offset"]) == 0:
            return {"jobPostings": postings, "total": 500}
        return {"jobPostings": "not-a-list", "total": 500}

    monkeypatch.setattr("watcher.sources.workday.post_json", transport)
    source = WorkdaySource()

    rows = source.fetch(workday_company())

    assert len(rows) == PAGE_SIZE
    assert source.last_diagnostics.listing_incomplete is True
    assert source.last_diagnostics.listing_incomplete_reasons == (
        "pagination_schema_error",
    )


def test_safety_limit_terminates_endless_board(monkeypatch):
    """A board that never signals an end stops at the bounded page limit."""

    offsets: list[int] = []

    def transport(_url, request, _source_name):
        offset = int(request["offset"])
        limit = int(request["limit"])
        offsets.append(offset)
        return {
            "jobPostings": [posting(offset + index) for index in range(limit)],
            "total": 0,
        }

    monkeypatch.setattr("watcher.sources.workday.post_json", transport)
    source = WorkdaySource()

    rows = source.fetch(workday_company())

    assert len(offsets) == MAX_LISTING_PAGES
    assert len(rows) == MAX_LISTING_PAGES * PAGE_SIZE
    assert source.last_diagnostics.listing_pages == MAX_LISTING_PAGES
    assert source.last_diagnostics.listing_incomplete is True
    assert source.last_diagnostics.listing_incomplete_reasons == (
        "pagination_safety_limit_reached",
    )


def test_pages_contain_no_duplicate_or_skipped_postings(monkeypatch):
    fake = FakeBoard(board(112), totals=lambda page: 112 if page == 0 else 0)

    _source, rows = run_fetch(monkeypatch, fake)

    identifiers = [row["extra"]["source_requisition_id"] for row in rows]
    assert identifiers == [f"R{index}" for index in range(112)]
    assert len(set(identifiers)) == len(identifiers)
    assert fake.offsets == sorted(fake.offsets)
    assert all(
        later - earlier == PAGE_SIZE
        for earlier, later in zip(fake.offsets, fake.offsets[1:])
    )


def test_single_short_page_board_requests_one_page(monkeypatch):
    fake = FakeBoard(board(12), totals=12)

    source, rows = run_fetch(monkeypatch, fake)

    assert len(rows) == 12
    assert fake.offsets == [0]
    assert source.last_diagnostics.listing_incomplete is False


def test_empty_board_stays_successful_and_complete(monkeypatch):
    fake = FakeBoard([], totals=0)

    source, rows = run_fetch(monkeypatch, fake)

    assert rows == []
    assert fake.offsets == [0]
    assert source.last_diagnostics.listing_incomplete is False
    assert anomalies(source) == {}


def test_pagination_diagnostics_reset_between_fetches(monkeypatch):
    first = FakeBoard(board(60), totals=lambda page: 60 if page == 0 else 0)
    source, _rows = run_fetch(monkeypatch, first)
    assert anomalies(source) == {"total_zero": 2}

    second = FakeBoard(board(20), totals=20)
    monkeypatch.setattr("watcher.sources.workday.post_json", second)
    source.fetch(workday_company())

    assert anomalies(source) == {}
    assert source.last_diagnostics.listing_pages == 1
    assert source.last_diagnostics.listing_incomplete is False
