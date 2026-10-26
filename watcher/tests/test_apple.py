"""Offline regression coverage for the Apple careers direct source.

Apple's completeness argument is one ``newest`` walk whose every response
reports the same authoritative total and whose unique posting ids reach that
total. These tests replay the live behaviors that broke the earlier two-sort
crawl -- the total moving mid-walk, transient empty envelopes for in-range
pages, and tied sort keys that repeat one posting across a page boundary
while displacing another -- and the bounded recovery for each.
"""

import json
from pathlib import Path

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.config import CompanyCfg, load_watchlist
from watcher.sources.apple import (
    CRAWL_SORT,
    MAX_CRAWL_PASSES,
    MAX_EMPTY_RETRIES_PER_PAGE,
    MAX_REPAIR_READS,
    PAGE_SIZE,
    SORT_NEWEST,
    AppleSource,
)
from watcher.sources.contracts import SourceFetchError, SourceSchemaError
from watcher.sources.registry import DIRECT_ATS, build_direct_sources

FIXTURE = Path(__file__).parent / "fixtures" / "apple_search_newest.json"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def company(name="Apple"):
    return CompanyCfg(name=name, ats="apple")


def record(index, *, posting_id=None, title=None, location="Cupertino", **overrides):
    """One search record shaped like Apple's own response."""

    raw = {
        "id": posting_id or f"20000{index}-0836",
        "positionId": str(200000 + index),
        "reqId": posting_id or f"20000{index}-0836",
        "postingTitle": title or f"Software Engineer {index}",
        "transformedPostingTitle": f"software-engineer-{index}",
        "jobSummary": f"Build things {index}.",
        "locations": [
            {"name": location, "countryName": "United States of America"}
        ],
        "team": {"teamName": "Software and Services", "teamCode": "SFTWR"},
        "homeOffice": False,
        "standardWeeklyHours": 40,
        "postDateInGMT": "2026-08-20T03:41:29.629Z",
        "postingDate": "Aug 20, 2026",
    }
    raw.update(overrides)
    return raw


def page(records, total):
    return {"res": {"totalRecords": total, "searchResults": list(records)}}


def empty_envelope():
    """Apple's exact past-end answer, also served transiently in range."""

    return load_fixture()["empty_envelope"]


def paginate(records, total=None):
    """Split records into Apple-sized pages, all reporting one total."""

    total = len(records) if total is None else total
    if not records:
        return [page([], total)]
    return [
        page(records[i : i + PAGE_SIZE], total)
        for i in range(0, len(records), PAGE_SIZE)
    ]


class FakeApple:
    """Serve a scripted board and record every request.

    ``boards`` is a list of ``(first_call, pages)``: from that 1-based request
    number on, the later board answers, which models the inventory changing
    mid-collection. ``queued`` holds per-page responses served, in order,
    before the board answers that page again, which models one request seeing
    a different tie order or a transient envelope.
    """

    def __init__(self, pages, *, token="tok0123456789", boards=(), queued=None):
        self.boards = [(1, pages), *boards]
        self.queued = {number: list(items) for number, items in (queued or {}).items()}
        self.token = token
        self.calls = []
        self.token_fetches = 0

    def fetch_token(self):
        self.token_fetches += 1
        return self.token

    def request(self, url, source_name, body, headers):
        self.calls.append(
            {"url": url, "source": source_name, "body": body, "headers": headers}
        )
        number = body["page"]
        if self.queued.get(number):
            return self.queued[number].pop(0)
        pages = [board for start, board in self.boards if start <= len(self.calls)][-1]
        if number > len(pages):
            return empty_envelope()
        return pages[number - 1]

    def pages_requested(self):
        return [call["body"]["page"] for call in self.calls]


def source(fake, **kwargs):
    return AppleSource(
        request_json=fake.request,
        fetch_csrf_token=fake.fetch_token,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        **kwargs,
    )


def ids(rows):
    return {row["extra"]["apple_posting_id"] for row in rows}


# --- contract surface ------------------------------------------------------


def test_the_crawl_uses_only_the_verified_newest_sort():
    assert CRAWL_SORT == SORT_NEWEST == "newest"

    everything = [record(i) for i in range(1, 41)]
    fake = FakeApple(paginate(everything))
    source(fake).fetch(company())

    assert {call["body"]["sort"] for call in fake.calls} == {SORT_NEWEST}


def test_endpoints_and_posting_url_are_apple_hosted():
    assert AppleSource.endpoint() == "https://jobs.apple.com/api/v1/search"
    assert AppleSource.csrf_endpoint() == "https://jobs.apple.com/api/v1/CSRFToken"
    assert AppleSource.posting_url("200678192-0836", "ios-engineer") == (
        "https://jobs.apple.com/en-us/details/200678192-0836/ios-engineer"
    )
    assert AppleSource.posting_url("200678192-0836", "") == (
        "https://jobs.apple.com/en-us/details/200678192-0836"
    )


def test_request_body_matches_the_verified_search_contract():
    body = AppleSource.request_body(page=3, sort=SORT_NEWEST)
    assert body["page"] == 3
    assert body["sort"] == SORT_NEWEST
    assert body["query"] == ""
    assert body["filters"] == {}
    assert body["locale"] == "en-us"
    # Page size is fixed by the service and must never become a request field.
    assert "pageSize" not in body and "numberOfResults" not in body


# --- captured responses ----------------------------------------------------


def test_captured_first_page_parses_into_canonical_rows():
    captured = load_fixture()["first_page"]
    records = captured["res"]["searchResults"]
    fake = FakeApple([page(records, len(records))])

    rows = source(fake).fetch(company())

    assert len(rows) == len(records) == PAGE_SIZE
    assert ids(rows) == {item["id"] for item in records}
    for row, raw in zip(rows, records):
        # Live titles carry stray whitespace; it is normalized, never kept.
        assert row["title"] == " ".join(raw["postingTitle"].split())
        assert row["source_url"] == AppleSource.posting_url(
            raw["id"], raw["transformedPostingTitle"]
        )
        # Request-time evergreen dates are never published as posting dates.
        assert row["date_posted"] == ""
    assert any(item["id"].startswith("PIPE-") for item in records)


def test_captured_empty_envelope_is_the_past_end_shape():
    envelope = empty_envelope()
    assert envelope["res"]["totalRecords"] == 0
    assert envelope["res"]["searchResults"] == []


def test_captured_tie_repeat_is_repaired_without_degrading_the_collection():
    """Two family members share a timestamp and swap across a boundary."""

    first, second = load_fixture()["tied_family"]
    assert first["postDateInGMT"] == second["postDateInGMT"]
    assert first["id"] != second["id"]

    leading = [record(i) for i in range(1, PAGE_SIZE)]
    trailing = [record(i) for i in range(100, 100 + PAGE_SIZE - 1)]
    total = 2 * PAGE_SIZE
    stable = [
        page(leading + [first], total),
        page([second] + trailing, total),
    ]
    # The walk's page 2 reorders the tie, repeating `first` and hiding `second`.
    fake = FakeApple(stable, queued={2: [page([first] + trailing, total)]})

    src = source(fake)
    rows = src.fetch(company())

    assert len(rows) == total
    assert {first["id"], second["id"]} <= ids(rows)
    diagnostics = src.last_diagnostics
    assert diagnostics.walk_duplicate_ids == 1
    assert diagnostics.repair_reads >= 1
    assert diagnostics.repaired_ids == 1
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.degraded is False


# --- stable pagination -----------------------------------------------------


def test_stable_walk_is_complete_with_exactly_one_request_per_page():
    everything = [record(i) for i in range(1, 46)]  # 20 + 20 + 5
    fake = FakeApple(paginate(everything))

    src = source(fake)
    rows = src.fetch(company())

    assert ids(rows) == {item["id"] for item in everything}
    # Page count is arithmetic, so nothing past the end is ever requested.
    assert fake.pages_requested() == [1, 2, 3]
    diagnostics = src.last_diagnostics
    assert diagnostics.authoritative_total == 45
    assert diagnostics.passes_attempted == 1
    assert diagnostics.restarts == 0
    assert diagnostics.repair_reads == 0
    assert diagnostics.retained_rows == 45
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.degraded is False


def test_final_page_arithmetic_accepts_a_partial_last_page():
    everything = [record(i) for i in range(1, 26)]

    rows = source(FakeApple(paginate(everything))).fetch(company())

    assert len(rows) == 25


# --- changing totals -------------------------------------------------------


def test_total_change_mid_walk_restarts_and_completes_on_a_stable_pass():
    """The production failure: a posting closes while the walk is underway."""

    before = [record(i) for i in range(1, 61)]
    after = before[:10] + before[11:]  # one posting closed near the top
    # Calls 1-2 see the old board; call 3 onward sees the new total.
    fake = FakeApple(paginate(before), boards=[(3, paginate(after))])

    src = source(fake)
    rows = src.fetch(company())

    assert ids(rows) == {item["id"] for item in after}
    diagnostics = src.last_diagnostics
    assert diagnostics.passes_attempted == 2
    assert diagnostics.restarts == 1
    assert diagnostics.authoritative_total == len(after)
    health = src.last_health_diagnostics
    assert health.complete is True
    assert health.degraded is True
    assert "pagination_restart_recovered" in health.reason_codes


def test_rows_from_an_abandoned_pass_are_never_published():
    before = [record(i) for i in range(1, 41)]
    gone = before[25]
    after = [item for item in before if item is not gone]
    fake = FakeApple(paginate(before), boards=[(2, paginate(after))])

    rows = source(fake).fetch(company())

    assert gone["id"] not in ids(rows)
    assert len(rows) == len(after)


def test_total_changing_in_every_bounded_pass_fails_closed():
    boards = []
    call = 2
    for change in range(1, MAX_CRAWL_PASSES + 1):
        boards.append((call, paginate([record(i) for i in range(1, 41 + change)])))
        call += 2
    fake = FakeApple(paginate([record(i) for i in range(1, 41)]), boards=boards)

    with pytest.raises(SourceSchemaError, match="total changed"):
        source(fake).fetch(company())

    first_pages = [number for number in fake.pages_requested() if number == 1]
    assert len(first_pages) == MAX_CRAWL_PASSES


def test_total_change_during_repair_restarts_the_pass():
    everything = [record(i) for i in range(1, 41)]
    walk_page_two = page([everything[0]] + everything[21:40], 40)
    after = everything[:30] + everything[31:]
    fake = FakeApple(
        paginate(everything),
        queued={2: [walk_page_two]},
        # The repair re-read (call 3) already sees a changed total.
        boards=[(3, paginate(after))],
    )

    src = source(fake)
    rows = src.fetch(company())

    assert ids(rows) == {item["id"] for item in after}
    assert src.last_diagnostics.restarts == 1


def test_total_above_the_safeguard_fails_closed():
    with pytest.raises(SourceSchemaError, match="safeguard"):
        source(FakeApple([page([record(1)], 5_000_000)])).fetch(company())


# --- transient empty envelopes ---------------------------------------------


def test_transient_empty_page_is_retried_and_reported_as_recovered():
    everything = [record(i) for i in range(1, 61)]
    fake = FakeApple(paginate(everything), queued={2: [empty_envelope()]})

    src = source(fake)
    rows = src.fetch(company())

    assert len(rows) == 60
    assert fake.pages_requested() == [1, 2, 2, 3]
    assert src.last_diagnostics.empty_retries == 1
    health = src.last_health_diagnostics
    assert health.complete is True
    assert health.degraded is True
    assert "request_retry_recovered" in health.reason_codes


def test_persistently_empty_in_range_page_fails_closed():
    everything = [record(i) for i in range(1, 61)]
    fake = FakeApple(
        paginate(everything),
        queued={2: [empty_envelope()] * (MAX_EMPTY_RETRIES_PER_PAGE + 1)},
    )

    with pytest.raises(SourceSchemaError, match="empty result"):
        source(fake).fetch(company())


def test_an_empty_answer_is_never_accepted_as_an_empty_inventory():
    """A zero total is Apple's past-end shape, so it can never prove emptiness."""

    fake = FakeApple([empty_envelope()])

    with pytest.raises(SourceSchemaError, match="empty result"):
        source(fake).fetch(company())

    assert len(fake.calls) == MAX_EMPTY_RETRIES_PER_PAGE + 1


def test_zero_total_alongside_records_fails_closed():
    with pytest.raises(SourceSchemaError, match="empty search while returning records"):
        source(FakeApple([page([record(1)], 0)])).fetch(company())


# --- repeated listings -----------------------------------------------------


def test_repeat_with_a_displaced_posting_is_recovered_by_a_bounded_repair():
    everything = [record(i) for i in range(1, 61)]
    displaced = everything[20]
    # The walk's page 2 repeats page 1's last row in place of `displaced`.
    walk_page_two = page([everything[19]] + everything[21:40], 60)
    fake = FakeApple(paginate(everything), queued={2: [walk_page_two]})

    src = source(fake)
    rows = src.fetch(company())

    assert displaced["id"] in ids(rows)
    assert len(rows) == 60
    diagnostics = src.last_diagnostics
    assert diagnostics.walk_rows == 60
    assert diagnostics.walk_unique_ids == 59
    assert diagnostics.repaired_ids == 1
    assert diagnostics.repair_reads <= MAX_REPAIR_READS


def test_unrecoverable_omission_fails_closed_after_bounded_work():
    everything = [record(i) for i in range(1, 61)]
    hidden = page([everything[19]] + everything[21:40], 60)
    # Every read of page 2 hides the same posting, so no pass can complete.
    pages = paginate(everything)
    pages[1] = hidden
    fake = FakeApple(pages)

    with pytest.raises(SourceSchemaError, match="59 of 60"):
        source(fake).fetch(company())

    walk_requests = MAX_CRAWL_PASSES * 3
    assert len(fake.calls) <= walk_requests + MAX_CRAWL_PASSES * MAX_REPAIR_READS


def test_more_unique_postings_than_the_total_is_never_complete():
    """Membership moved under an unchanged total, so the count proves nothing."""

    everything = [record(i) for i in range(1, 41)]
    intruder = record(999)
    walk_page_two = page([everything[19]] + everything[20:39], 40)
    repair_page_two = page([intruder] + everything[21:40], 40)
    fake = FakeApple(
        paginate(everything),
        queued={2: [walk_page_two, repair_page_two] * MAX_CRAWL_PASSES},
    )

    with pytest.raises(SourceSchemaError, match="more postings than"):
        source(fake).fetch(company())


def test_benign_post_date_differences_do_not_conflict():
    """`postDateInGMT` is request-time metadata on evergreen rows."""

    everything = [record(i, posting_id=f"PIPE-1144380{i:02d}") for i in range(1, 41)]
    redated = dict(everything[19], postDateInGMT="2026-09-07T01:05:15.952Z")
    walk_page_two = page([redated] + everything[21:40], 40)
    fake = FakeApple(paginate(everything), queued={2: [walk_page_two]})

    rows = source(fake).fetch(company())

    assert len(rows) == 40


def test_conflicting_invariant_fields_for_one_id_fail_closed():
    everything = [record(i) for i in range(1, 41)]
    conflicting = dict(everything[19], postingTitle="Totally Different Role")
    walk_page_two = page([conflicting] + everything[21:40], 40)
    fake = FakeApple(paginate(everything), queued={2: [walk_page_two]})

    with pytest.raises(SourceSchemaError, match="conflicting records"):
        source(fake).fetch(company())


def test_posting_date_is_never_published_as_date_posted():
    everything = [record(i) for i in range(1, 3)]
    rows = source(FakeApple(paginate(everything))).fetch(company())

    assert all(row["date_posted"] == "" for row in rows)
    assert all("postDateInGMT" not in row["extra"] for row in rows)


# --- page arithmetic -------------------------------------------------------


def test_prematurely_short_page_is_truncation_not_a_terminal_signal():
    everything = [record(i) for i in range(1, 41)]
    pages = paginate(everything)
    pages[0] = page(everything[:5], len(everything))

    with pytest.raises(SourceSchemaError, match="page 1 returned 5 of 20"):
        source(FakeApple(pages)).fetch(company())


def test_rowless_page_under_a_nonzero_total_fails_closed():
    everything = [record(i) for i in range(1, 41)]
    pages = paginate(everything)
    pages[1] = page([], len(everything))

    with pytest.raises(SourceSchemaError, match="page 2 returned 0 of 20"):
        source(FakeApple(pages)).fetch(company())


# --- CSRF and request failures --------------------------------------------


def test_csrf_token_is_sent_on_every_search_request():
    everything = [record(i) for i in range(1, 41)]
    fake = FakeApple(paginate(everything))

    source(fake).fetch(company())

    assert fake.calls
    assert all(
        call["headers"]["X-Apple-CSRF-Token"] == fake.token for call in fake.calls
    )
    # One anonymous token is reused for the whole collection.
    assert fake.token_fetches == 1


@pytest.mark.parametrize("bad", ["", "   ", "tok with space", "x" * 300])
def test_malformed_csrf_token_fails_closed(bad):
    fake = FakeApple(paginate([record(1)]), token=bad)

    with pytest.raises(SourceFetchError, match="CSRF token"):
        source(fake).fetch(company())


def test_csrf_failure_message_never_echoes_the_token():
    fake = FakeApple(paginate([record(1)]), token="secret-token!!")

    with pytest.raises(SourceFetchError) as excinfo:
        source(fake).fetch(company())

    assert "secret-token" not in str(excinfo.value)


def test_transient_search_failure_is_retried_then_reported_as_recovered():
    everything = [record(i) for i in range(1, 21)]
    fake = FakeApple(paginate(everything))
    calls = {"n": 0}
    original = fake.request

    def flaky(url, source_name, body, headers):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SourceFetchError("apple search failed", retryable=True)
        return original(url, source_name, body, headers)

    fake.request = flaky
    src = source(fake)
    rows = src.fetch(company())

    assert len(rows) == len(everything)
    assert src.retry_attempts == 1
    assert "request_retry_recovered" in src.last_health_diagnostics.reason_codes
    assert src.last_health_diagnostics.complete is True


def test_permanent_search_failure_propagates():
    def broken(url, source_name, body, headers):
        raise SourceFetchError("apple search failed", retryable=False)

    fake = FakeApple(paginate([record(1)]))
    fake.request = broken

    with pytest.raises(SourceFetchError):
        source(fake).fetch(company())


def test_each_fetch_starts_from_fresh_diagnostics():
    everything = [record(i) for i in range(1, 41)]
    fake = FakeApple(paginate(everything), queued={2: [empty_envelope()]})
    src = source(fake)
    src.fetch(company())
    assert src.last_diagnostics.empty_retries == 1

    src.fetch(company())

    assert src.last_diagnostics.empty_retries == 0
    assert src.last_health_diagnostics.degraded is False


# --- malformed payloads ----------------------------------------------------


@pytest.mark.parametrize(
    "payload, match",
    [
        ([], "not an object"),
        ({}, "lacked its result object"),
        ({"res": {"totalRecords": "12"}}, "totalRecords was not an integer"),
        ({"res": {"totalRecords": True}}, "totalRecords was not an integer"),
        ({"res": {"totalRecords": -1}}, "totalRecords was negative"),
        ({"res": {"totalRecords": 1, "searchResults": {}}}, "was not a list"),
    ],
)
def test_malformed_search_payloads_fail_closed(payload, match):
    with pytest.raises(SourceSchemaError, match=match):
        source(FakeApple([payload])).fetch(company())


@pytest.mark.parametrize(
    "override, match",
    [
        ({"id": ""}, "lacked a posting id"),
        ({"postingTitle": ""}, "lacked a title"),
        ({"postingTitle": 12}, "postingTitle was not a string"),
        ({"locations": [[]]}, "location entry was malformed"),
        ({"team": []}, "team was malformed"),
        ({"homeOffice": "yes"}, "homeOffice was not a boolean"),
    ],
)
def test_malformed_records_fail_closed(override, match):
    bad = record(1, **override)

    with pytest.raises(SourceSchemaError, match=match):
        source(FakeApple([page([bad], 1)])).fetch(company())


# --- row shape -------------------------------------------------------------


def test_rows_carry_apple_identity_locations_and_canonical_url():
    raw = record(
        1,
        posting_id="200678192-0836",
        title="iOS Software Engineer",
        location="Cary",
    )

    row = source(FakeApple([page([raw], 1)])).fetch(company())[0]

    assert row["company"] == "Apple"
    assert row["title"] == "iOS Software Engineer"
    assert row["location"] == "Cary, United States of America"
    assert row["source_url"] == (
        "https://jobs.apple.com/en-us/details/200678192-0836/software-engineer-1"
    )
    assert row["extra"]["source"] == "direct"
    assert row["extra"]["source_adapter"] == "apple"
    assert row["extra"]["source_system"] == "apple"
    assert row["extra"]["source_requisition_id"] == "200678192-0836"
    assert row["extra"]["apple_posting_id"] == "200678192-0836"
    assert row["extra"]["active"] is True


def test_multi_location_rows_keep_every_concrete_location():
    raw = record(
        1,
        locations=[
            {"name": "Austin", "countryName": "United States of America"},
            {"name": "Cupertino", "countryName": "United States of America"},
        ],
    )

    row = source(FakeApple([page([raw], 1)])).fetch(company())[0]

    assert row["location"] == (
        "Austin, United States of America; Cupertino, United States of America"
    )


# --- integration surfaces --------------------------------------------------


def test_apple_is_registered_and_constructible():
    assert "apple" in DIRECT_ATS
    assert isinstance(build_direct_sources()["apple"], AppleSource)


def test_apple_origin_key_is_its_own_host():
    assert direct_origin_key("apple") == "https://jobs.apple.com"
    assert direct_origin_key("apple") != direct_origin_key("greenhouse")


def test_watchlist_configures_apple_through_the_shared_loader():
    config = load_watchlist("watcher/watchlist.yml")
    companies = {item.name: item for item in config.companies if item.ats == "apple"}

    assert "Apple" in companies
    assert companies["Apple"].source_url.startswith("https://jobs.apple.com")
