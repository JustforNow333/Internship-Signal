"""Offline regression coverage for the Apple careers direct source.

Apple's completeness argument is the union of two official sorts measured
against its own authoritative total, so these tests exercise the union rule and
its failure modes rather than a single happy-path page walk.
"""

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.config import CompanyCfg, load_watchlist
from watcher.sources.apple import (
    PAGE_SIZE,
    REQUIRED_SORTS,
    SORT_LOCATION_ASC,
    SORT_NEWEST,
    AppleSource,
)
from watcher.sources.contracts import SourceFetchError, SourceSchemaError
from watcher.sources.registry import DIRECT_ATS, build_direct_sources


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


def paginate(records, total=None):
    """Split records into Apple-sized pages."""

    total = len(records) if total is None else total
    if not records:
        return [page([], total)]
    return [
        page(records[i : i + PAGE_SIZE], total)
        for i in range(0, len(records), PAGE_SIZE)
    ]


class FakeApple:
    """Serve scripted pages per sort and record every request."""

    def __init__(self, pages_by_sort, *, token="tok0123456789"):
        self.pages_by_sort = pages_by_sort
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
        sort = body["sort"]
        pages = self.pages_by_sort[sort]
        index = body["page"] - 1
        if index >= len(pages):
            # Apple answers past-end with a zero total and no rows.
            return page([], 0)
        return pages[index]


def source(fake, **kwargs):
    return AppleSource(
        request_json=fake.request,
        fetch_csrf_token=fake.fetch_token,
        sleeper=lambda _seconds: None,
        jitter=lambda _low, _high: 0.0,
        **kwargs,
    )


def both_sorts(newest_records, location_records, total):
    return {
        SORT_NEWEST: paginate(newest_records, total),
        SORT_LOCATION_ASC: paginate(location_records, total),
    }


# --- contract surface ------------------------------------------------------


def test_required_sorts_are_the_two_verified_official_values():
    assert REQUIRED_SORTS == (SORT_NEWEST, SORT_LOCATION_ASC)


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
    assert body["locale"] == "en-us"
    # Page size is fixed by the service and must never become a request field.
    assert "pageSize" not in body and "numberOfResults" not in body


# --- the union rule --------------------------------------------------------


def test_two_sort_union_reaching_the_total_is_complete():
    """Each sort drops a different posting; together they recover the total."""

    everything = [record(i) for i in range(1, 26)]
    total = len(everything)
    # `newest` repeats one row and omits the last; `locationAsc` omits the first.
    newest = everything[:-1] + [everything[0]]
    location = everything[1:] + [everything[5]]

    rows = source(FakeApple(both_sorts(newest, location, total))).fetch(company())

    assert len(rows) == total
    ids = {row["extra"]["apple_posting_id"] for row in rows}
    assert ids == {item["id"] for item in everything}


def test_single_sort_duplicate_and_omission_still_yields_a_complete_union():
    everything = [record(i) for i in range(1, 21)]
    total = len(everything)
    newest = everything[:-1] + [everything[3]]

    src = source(FakeApple(both_sorts(newest, everything, total)))
    rows = src.fetch(company())
    diagnostics = src.last_diagnostics

    assert len(rows) == total
    assert diagnostics.authoritative_total == total
    assert dict(diagnostics.raw_rows_per_sort) == {
        SORT_NEWEST: total,
        SORT_LOCATION_ASC: total,
    }
    # Raw counts and the retained unique count stay separate signals.
    assert dict(diagnostics.unique_ids_per_sort)[SORT_NEWEST] == total - 1
    assert dict(diagnostics.duplicate_ids_per_sort)[SORT_NEWEST] == 1
    assert diagnostics.union_unique_ids == total
    assert diagnostics.retained_rows == total


def test_union_short_of_the_total_fails_closed():
    """Both sorts miss the same posting, so the union cannot be trusted."""

    everything = [record(i) for i in range(1, 21)]
    total = len(everything)
    short = everything[:-1] + [everything[0]]

    with pytest.raises(SourceSchemaError, match="union recovered 19 of 20"):
        source(FakeApple(both_sorts(short, short, total))).fetch(company())


def test_union_shortfall_is_never_repaired_by_a_third_request():
    everything = [record(i) for i in range(1, 21)]
    short = everything[:-1] + [everything[0]]
    fake = FakeApple(both_sorts(short, short, len(everything)))

    with pytest.raises(SourceSchemaError):
        source(fake).fetch(company())

    # Exactly the two sorts were crawled; nothing probed for the missing row.
    assert {call["body"]["sort"] for call in fake.calls} == set(REQUIRED_SORTS)


# --- reconciliation --------------------------------------------------------


def test_benign_post_date_differences_do_not_block_the_union():
    """`postDateInGMT` is request-time metadata on evergreen rows."""

    everything = [record(i, posting_id=f"PIPE-1144380{i:02d}") for i in range(1, 21)]
    drifted = [
        dict(item, postDateInGMT="2026-09-07T01:05:15.952Z", postingDate="Sep 7, 2026")
        for item in everything
    ]

    rows = source(
        FakeApple(both_sorts(everything, drifted, len(everything)))
    ).fetch(company())

    assert len(rows) == len(everything)


def test_posting_date_is_never_published_as_date_posted():
    everything = [record(i) for i in range(1, 3)]
    rows = source(
        FakeApple(both_sorts(everything, everything, len(everything)))
    ).fetch(company())

    assert all(row["date_posted"] == "" for row in rows)
    assert all("postDateInGMT" not in row["extra"] for row in rows)


def test_conflicting_invariant_fields_for_one_id_fail_closed():
    everything = [record(i) for i in range(1, 21)]
    conflicting = [dict(everything[0], postingTitle="Totally Different Role")]
    conflicting += everything[1:]

    with pytest.raises(SourceSchemaError, match="conflicting records"):
        source(
            FakeApple(both_sorts(everything, conflicting, len(everything)))
        ).fetch(company())


# --- page arithmetic and totals -------------------------------------------


def test_prematurely_short_page_is_truncation_not_a_terminal_signal():
    everything = [record(i) for i in range(1, 41)]
    pages = paginate(everything, len(everything))
    pages[0] = page(everything[:5], len(everything))

    with pytest.raises(SourceSchemaError, match="page 1 returned 5 of 20"):
        source(
            FakeApple({SORT_NEWEST: pages, SORT_LOCATION_ASC: paginate(everything)})
        ).fetch(company())


def test_empty_page_before_the_end_fails_closed():
    everything = [record(i) for i in range(1, 41)]
    pages = paginate(everything, len(everything))
    pages[1] = page([], len(everything))

    with pytest.raises(SourceSchemaError, match="page 2 returned 0 of 20"):
        source(
            FakeApple({SORT_NEWEST: pages, SORT_LOCATION_ASC: paginate(everything)})
        ).fetch(company())


def test_final_page_arithmetic_accepts_a_partial_last_page():
    everything = [record(i) for i in range(1, 26)]  # 20 + 5

    rows = source(
        FakeApple(both_sorts(everything, everything, len(everything)))
    ).fetch(company())

    assert len(rows) == 25


def test_total_drift_within_one_sort_fails_closed():
    everything = [record(i) for i in range(1, 41)]
    pages = paginate(everything, len(everything))
    pages[1] = page(everything[PAGE_SIZE:], len(everything) + 1)

    with pytest.raises(SourceSchemaError, match="total changed during a sort crawl"):
        source(
            FakeApple({SORT_NEWEST: pages, SORT_LOCATION_ASC: paginate(everything)})
        ).fetch(company())


def test_total_drift_between_sorts_fails_closed():
    everything = [record(i) for i in range(1, 21)]
    moved = [record(i) for i in range(1, 22)]

    with pytest.raises(SourceSchemaError, match="total changed between sorts"):
        source(
            FakeApple(
                {
                    SORT_NEWEST: paginate(everything, len(everything)),
                    SORT_LOCATION_ASC: paginate(moved, len(moved)),
                }
            )
        ).fetch(company())


def test_total_above_the_safeguard_fails_closed():
    with pytest.raises(SourceSchemaError, match="safeguard"):
        source(
            FakeApple({SORT_NEWEST: [page([record(1)], 5_000_000)]})
        ).fetch(company())


# --- empty and past-end semantics -----------------------------------------


def test_genuine_empty_search_is_complete_and_empty():
    src = source(FakeApple({SORT_NEWEST: [page([], 0)], SORT_LOCATION_ASC: [page([], 0)]}))

    rows = src.fetch(company())

    assert rows == []
    assert src.last_diagnostics.authoritative_total == 0
    assert src.last_health_diagnostics.complete is True


def test_zero_total_alongside_records_fails_closed():
    with pytest.raises(SourceSchemaError, match="empty search while returning records"):
        source(FakeApple({SORT_NEWEST: [page([record(1)], 0)]})).fetch(company())


def test_past_end_zero_response_is_never_requested_as_proof():
    """Page count is arithmetic, so no request is ever made past the end."""

    everything = [record(i) for i in range(1, 41)]
    fake = FakeApple(both_sorts(everything, everything, len(everything)))

    source(fake).fetch(company())

    requested = sorted(call["body"]["page"] for call in fake.calls)
    assert requested == [1, 1, 2, 2]  # exactly two pages per sort, nothing beyond


# --- CSRF and request failures --------------------------------------------


def test_csrf_token_is_sent_on_every_search_request():
    everything = [record(i) for i in range(1, 21)]
    fake = FakeApple(both_sorts(everything, everything, len(everything)))

    source(fake).fetch(company())

    assert fake.calls
    assert all(
        call["headers"]["X-Apple-CSRF-Token"] == fake.token for call in fake.calls
    )
    # One anonymous token is reused for the whole collection.
    assert fake.token_fetches == 1


@pytest.mark.parametrize("bad", ["", "   ", "tok with space", "x" * 300])
def test_malformed_csrf_token_fails_closed(bad):
    everything = [record(1)]
    fake = FakeApple(both_sorts(everything, everything, 1), token=bad)

    with pytest.raises(SourceFetchError, match="CSRF token"):
        source(fake).fetch(company())


def test_csrf_failure_message_never_echoes_the_token():
    fake = FakeApple(both_sorts([record(1)], [record(1)], 1), token="secret-token!!")

    with pytest.raises(SourceFetchError) as excinfo:
        source(fake).fetch(company())

    assert "secret-token" not in str(excinfo.value)


def test_transient_search_failure_is_retried_then_reported_as_recovered():
    everything = [record(i) for i in range(1, 21)]
    fake = FakeApple(both_sorts(everything, everything, len(everything)))
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

    fake = FakeApple(both_sorts([record(1)], [record(1)], 1))
    fake.request = broken

    with pytest.raises(SourceFetchError):
        source(fake).fetch(company())


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
    fake = FakeApple({SORT_NEWEST: [payload]})

    with pytest.raises(SourceSchemaError, match=match):
        source(fake).fetch(company())


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
    fake = FakeApple({SORT_NEWEST: [page([bad], 1)]})

    with pytest.raises(SourceSchemaError, match=match):
        source(fake).fetch(company())


# --- row shape -------------------------------------------------------------


def test_rows_carry_apple_identity_locations_and_canonical_url():
    raw = record(
        1,
        posting_id="200678192-0836",
        title="iOS Software Engineer",
        location="Cary",
    )
    fake = FakeApple(both_sorts([raw], [raw], 1))

    row = source(fake).fetch(company())[0]

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
    fake = FakeApple(both_sorts([raw], [raw], 1))

    row = source(fake).fetch(company())[0]

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
