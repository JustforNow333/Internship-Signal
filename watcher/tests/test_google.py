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
from watcher.filters import is_open
from watcher.sources.contracts import SourceFetchError, SourceSchemaError
from watcher.sources.diagnostics import DirectSourceDiagnostics
from watcher.sources.google import ORGANIZATION, RPC_ID, GoogleSource
from watcher.sources.registry import DIRECT_ATS, build_direct_sources


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def company() -> CompanyCfg:
    return CompanyCfg(
        name="Google",
        ats="google",
        aliases=("Google LLC",),
        alumni_match=("google",),
        source_url="https://www.google.com/about/careers/applications/jobs/results",
    )


def record(
    posting_id: int,
    *,
    title: str | None = None,
    apply_url: str | None = "https://www.google.com/about/careers/applications/signin?jobId=x",
    org: str = ORGANIZATION,
    locations: list | None = None,
) -> list:
    """One record in the RPC's observed layout (11 leading elements)."""

    return [
        str(posting_id),
        title or f"Software Engineer {posting_id}",
        apply_url,
        [None, "<ul><li>Build things.</li></ul>"],
        [None, "<h3>Minimum qualifications:</h3><ul><li>BS degree.</li></ul>"],
        "projects/gweb-careers-proto/tenants/x/companies/y",
        None,
        org,
        "en-US",
        [["Mountain View, CA, USA"]] if locations is None else locations,
        [None, "<p>About the team.</p>"],
    ]


def rpc_page(*records: list, total: int, page_size: int = 20, payload=None) -> str:
    if payload is None:
        payload = json.dumps([list(records), None, total, page_size], separators=(",", ":"))
    envelope = [
        ["wrb.fr", RPC_ID, payload, None, None, None, "generic"],
        ["di", 156],
        ["af.httprm", 156, "-1", 7],
    ]
    return ")]}'\n\n" + json.dumps(envelope, separators=(",", ":"))


def source_for_pages(pages: list[str], *, max_snapshot_passes: int = 3) -> GoogleSource:
    queued = iter(pages)

    def request(url: str, source_name: str, fields: dict):
        assert source_name == "google"
        assert "batchexecute" in url
        assert "f.req" in fields
        return next(queued)

    return GoogleSource(
        request_form=request,
        sleeper=lambda _d: None,
        jitter=lambda _l, _h: 0,
        max_snapshot_passes=max_snapshot_passes,
    )


def two_page_pass(total: int = 21) -> list[str]:
    first = rpc_page(*(record(100 + v) for v in range(20)), total=total)
    final = rpc_page(record(120), total=total)
    terminal = rpc_page(total=total)
    return [first, final, terminal]


# --- parser -----------------------------------------------------------------


def test_fixture_maps_every_canonical_field():
    page = fixture("google_rpc_page.txt")
    # The captured fixture holds one real record; scope the pass to it.
    payload = json.loads(page.split("\n", 1)[1])
    inner = json.loads(payload[0][2])
    inner[2] = 1
    scoped = rpc_page(payload=json.dumps([inner[0], None, 1, inner[3]], separators=(",", ":")), total=1)
    terminal = rpc_page(total=1)
    source = source_for_pages([scoped, terminal, scoped, terminal])

    rows = source.fetch(company())

    assert len(rows) == 1
    row = rows[0]
    assert row["company"] == "Google"
    assert row["title"] == "Data Center Technician, Server Operations"
    assert row["location"] == "Mumbai, Maharashtra, India"
    assert row["source_url"] == (
        "https://www.google.com/about/careers/applications/jobs/results/107686025534284486"
    )
    assert row["extra"]["source_requisition_id"] == "google:107686025534284486"
    assert row["extra"]["google_organization"] == "Google"
    assert row["extra"]["active"] is True
    assert row["extra"]["application_url"].startswith("https://")
    assert row["description"] and "<" not in row["description"]
    assert row["requirements"] and "<" not in row["requirements"]
    # Timestamp semantics in the payload are unverified, so no date is claimed.
    assert row["date_posted"] == ""


def test_normal_multi_page_crawl_has_exact_final_short_page():
    source = source_for_pages(two_page_pass() * 2)

    rows = source.fetch(company())

    assert [r["extra"]["google_posting_id"] for r in rows] == [
        str(100 + v) for v in range(21)
    ]
    d = source.last_diagnostics
    assert d.advertised_total == 21
    assert d.raw_records_seen == 21
    assert d.retained_rows == 21
    assert d.non_actionable_rows == 0
    assert d.snapshot_passes_requested == 2
    # two pages plus the terminal probe, per pass
    assert d.listing_pages_requested == 6


def test_request_uses_verified_rpc_shape_with_one_based_pages():
    pages = iter(two_page_pass() * 2)
    seen: list[dict] = []

    def request(url: str, _name: str, fields: dict):
        seen.append({"url": url, "fields": fields})
        return next(pages)

    GoogleSource(request_form=request).fetch(company())

    query = parse_qs(urlsplit(seen[0]["url"]).query)
    assert query["rpcids"] == [RPC_ID]
    assert urlsplit(seen[0]["url"]).netloc == "www.google.com"
    pages_requested = []
    for call in seen:
        envelope = json.loads(call["fields"]["f.req"])
        assert envelope[0][0][0] == RPC_ID
        inner = json.loads(envelope[0][0][1])
        assert inner[0][1] == [ORGANIZATION]
        assert inner[0][4] == "en-US"
        pages_requested.append(inner[0][7])
    assert pages_requested == [1, 2, 3, 1, 2, 3]


def test_explicit_zero_total_is_a_trustworthy_empty_board():
    empty = rpc_page(total=0)
    source = source_for_pages([empty, empty])

    assert source.fetch(company()) == []
    assert source.last_diagnostics.advertised_total == 0
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.degraded is False


def test_zero_total_with_records_fails_closed():
    page = rpc_page(record(101), total=0)
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="empty board while returning records"):
        source.fetch(company())


def test_terminal_page_returning_records_fails_closed():
    first = rpc_page(*(record(100 + v) for v in range(20)), total=20)
    bad_terminal = rpc_page(record(999), total=20)
    source = source_for_pages([first, bad_terminal] * 3)

    with pytest.raises(SourceSchemaError, match="past its advertised final page"):
        source.fetch(company())


# --- completeness failures --------------------------------------------------


def test_premature_short_page_is_discarded_then_retried():
    short = rpc_page(record(101), total=41)
    source = source_for_pages([short] * 6)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())
    assert source.last_diagnostics.snapshot_passes_requested == 3


def test_changed_total_between_pages_discards_pass_then_converges():
    drifting_first = rpc_page(*(record(100 + v) for v in range(20)), total=21)
    drifting_second = rpc_page(record(120), total=22)
    source = source_for_pages([drifting_first, drifting_second] + two_page_pass() * 2)

    rows = source.fetch(company())

    assert len(rows) == 21
    assert source.last_diagnostics.snapshot_passes_requested == 3


def test_changed_membership_between_passes_requires_a_matching_pass():
    a = [rpc_page(record(101), total=1), rpc_page(total=1)]
    b = [rpc_page(record(102), total=1), rpc_page(total=1)]
    source = source_for_pages(a + b + b)

    rows = source.fetch(company())

    assert [r["extra"]["google_posting_id"] for r in rows] == ["102"]
    assert source.last_diagnostics.snapshot_passes_requested == 3


def test_repeated_page_fails_after_bounded_retries():
    repeated = rpc_page(*(record(100 + v) for v in range(20)), total=60)
    source = source_for_pages([repeated] * 9)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        source.fetch(company())


def test_duplicate_posting_id_is_rejected():
    page = rpc_page(record(101), record(101, title="Other"), total=2)
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="duplicate posting id"):
        source.fetch(company())


def test_total_beyond_the_safeguard_is_rejected():
    page = rpc_page(record(101), total=10_000_000)
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="pagination safeguard"):
        source.fetch(company())


def test_max_page_bound_is_enforced():
    page = rpc_page(*(record(100 + v) for v in range(20)), total=200)
    source = GoogleSource(
        request_form=lambda _u, _n, _f: page,
        sleeper=lambda _d: None,
        jitter=lambda _l, _h: 0,
        max_pages=2,
    )
    with pytest.raises(SourceSchemaError):
        source.fetch(company())


# --- defensive RPC decoding -------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("", "empty"),
        (")]}'", "xssi guard"),
        (")]}'\nnot json", "not a decodable batchexecute envelope"),
        (")]}'\n{\"a\": 1}", "not a list of messages"),
        (")]}'\n[[\"wrb.fr\",\"OTHER\",\"[[],null,0,20]\"]]", "exactly one response"),
        (")]}'\n[[\"er\",\"r06xKb\",null]]", "reported an error"),
        (")]}'\n[[\"wrb.fr\",\"r06xKb\",null]]", "empty payload"),
        (")]}'\n[[\"wrb.fr\",\"r06xKb\",\"nope\"]]", "not decodable JSON"),
    ],
)
def test_malformed_rpc_responses_fail_closed(payload: str, match: str):
    source = source_for_pages([payload, payload])
    with pytest.raises(SourceSchemaError, match=match):
        source.fetch(company())


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("[[],null]", "expected shape"),
        ('[[],null,"12",20]', "total was not an integer"),
        ("[[],null,-1,20]", "total was negative"),
        ("[[],null,0,0]", "zero page size"),
        ('[[],null,0,"20"]', "page size was not an integer"),
        ('[{"a":1},null,1,20]', "records were not a list"),
        ('[[{"a":1}],null,1,20]', "record did not carry the expected shape"),
        ('["x",null,1,20]', "records were not a list"),
    ],
)
def test_invalid_totals_and_payload_shapes_fail_closed(payload: str, match: str):
    page = rpc_page(payload=payload, total=0)
    source = source_for_pages([page, page])
    with pytest.raises(SourceSchemaError, match=match):
        source.fetch(company())


@pytest.mark.parametrize(
    "broken",
    [
        ["short", "record"],
        [123] + record(1)[1:],
        ["not-numeric"] + record(1)[1:],
        record(1)[:1] + [None] + record(1)[2:],
        record(1, apply_url=12345),
        record(1, apply_url="ftp://elsewhere.example"),
        record(1, locations=[{"bad": 1}]),
    ],
)
def test_malformed_records_fail_closed(broken: list):
    page = rpc_page(broken, total=1)
    source = source_for_pages([page, page])
    with pytest.raises(SourceSchemaError):
        source.fetch(company())


# --- organization scope and actionability -----------------------------------


def test_record_outside_the_requested_organization_fails_closed():
    page = rpc_page(record(101), record(102, org="YouTube"), total=2)
    source = source_for_pages([page, page])

    with pytest.raises(SourceSchemaError, match="outside the requested organization"):
        source.fetch(company())


def test_listing_without_an_application_link_is_retained_but_not_open():
    """Future openings and general career entries are inventory, not open roles."""

    page = rpc_page(
        record(101),
        record(102, title="Software Engineering BS/MS Intern, 2027", apply_url=None),
        record(103, title="Open Sales Career Opportunities", apply_url=""),
        total=3,
    )
    terminal = rpc_page(total=3)
    source = source_for_pages([page, terminal, page, terminal])

    rows = source.fetch(company())

    assert len(rows) == 3, "non-actionable listings still count toward completeness"
    by_id = {r["extra"]["google_posting_id"]: r for r in rows}
    assert by_id["101"]["extra"]["active"] is True
    assert is_open(by_id["101"]) is True
    for pid in ("102", "103"):
        row = by_id[pid]
        assert row["extra"]["active"] is False
        assert row["extra"]["google_non_actionable_reason"] == "no_application_link"
        assert "application_url" not in row["extra"]
        # No application URL is invented, and the canonical route still resolves.
        assert row["source_url"].endswith(f"/jobs/results/{pid}")
        assert is_open(row) is False
    assert source.last_diagnostics.raw_records_seen == 3
    assert source.last_diagnostics.non_actionable_rows == 2


# --- transport and integration ----------------------------------------------


def test_transient_request_retry_uses_shared_bound_and_degrades_result():
    one_pass = [rpc_page(record(101), total=1), rpc_page(total=1)]
    pages = iter(one_pass * 2)
    calls = 0
    delays: list[float] = []

    def request(_u, _n, _f):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SourceFetchError("temporary", retryable=True)
        return next(pages)

    source = GoogleSource(
        request_form=request, sleeper=delays.append, jitter=lambda _l, _h: 0
    )

    assert len(source.fetch(company())) == 1
    assert delays == [1.0]
    assert source.last_health_diagnostics.failed_request_count == 1
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.degraded is True
    assert source.last_health_diagnostics.complete is True


def test_non_retryable_request_failure_fails_the_source():
    def request(_u, _n, _f):
        raise SourceFetchError("rpc unavailable", retryable=False)

    source = GoogleSource(request_form=request)
    with pytest.raises(SourceFetchError, match="rpc unavailable"):
        source.fetch(company())
    assert source.last_health_diagnostics.succeeded is None


def test_clean_pass_reports_complete_and_non_degraded():
    source = source_for_pages(two_page_pass() * 2)
    rows = source.fetch(company())
    assert source.last_health_diagnostics == DirectSourceDiagnostics(
        succeeded=True, retained_row_count=len(rows), complete=True
    )


def test_registry_lazy_construction_origin_matching_and_fingerprint():
    configured = next(
        item
        for item in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
        if item.name == "Google"
    )

    assert configured.ats == "google"
    assert configured.module == ""
    assert "google" in DIRECT_ATS
    assert isinstance(build_direct_sources()["google"], GoogleSource)
    assert direct_origin_key("google") == "https://www.google.com"
    assert company_matches("Google LLC", configured)
    assert not company_matches("Google DeepMind", configured)
    assert not company_matches("YouTube", configured)

    baseline = WatcherConfig(companies=(configured,))
    old = WatcherConfig(companies=(replace(configured, ats="bespoke", module="google"),))
    assert collection_config_fingerprint(baseline) != collection_config_fingerprint(old)


def test_lazy_export_exposes_the_source_without_eager_import():
    import watcher.sources as sources

    assert "GoogleSource" in sources.__all__
    assert sources.GoogleSource is GoogleSource
