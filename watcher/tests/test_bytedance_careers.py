from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from watcher.collection_concurrency import (
    BYTEDANCE_CAREERS_PORTAL_HOSTS,
    direct_origin_key,
)
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.company_matching import company_matches
from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, WatcherConfig, load_watchlist
from watcher.config.validation import BYTEDANCE_CAREERS_PORTAL_SITES
from watcher.sources.bytedance_careers import (
    PORTAL_BYTEDANCE,
    PORTAL_TIKTOK,
    PORTALS,
    ByteDanceCareersSource,
)
from watcher.sources.contracts import SourceFetchError, SourceSchemaError
from watcher.sources.diagnostics import DirectSourceDiagnostics
from watcher.sources.registry import DIRECT_ATS, build_direct_sources


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def company(name: str = "TikTok", portal: str = PORTAL_TIKTOK) -> CompanyCfg:
    site = BYTEDANCE_CAREERS_PORTAL_SITES[portal]
    return CompanyCfg(
        name=name,
        ats="bytedance_careers",
        bytedance_careers_portal=portal,
        aliases=(),
        alumni_match=(name.casefold(),),
        source_url=f"https://{site}/search",
    )


def record(posting_id: int, *, title: str | None = None, code: str | None = None,
           city: str = "Singapore", category: str = "R&D",
           recruit: str = "Regular") -> dict:
    return {
        "id": str(posting_id),
        "code": code or f"A{posting_id}",
        "title": title or f"Backend Engineer {posting_id}",
        "description": "About the team\nBuild things.",
        "requirement": "Minimum Qualifications:\n1. BS degree.",
        "city_info": {"code": "CT_1", "name": None, "en_name": city},
        "job_category": {"id": "1", "name": None, "en_name": category},
        "recruit_type": {"id": "101", "name": None, "en_name": recruit},
        "job_post_info": {"min_salary": None, "expiry_time": None},
    }


def payload(*records: dict, count: int | None = None, code: int = 0) -> dict:
    return {
        "code": code,
        "message": "success",
        "data": {
            "count": len(records) if count is None else count,
            "job_post_list": list(records),
        },
    }


def responder(pages: list[dict]):
    """Serve queued payloads, asserting the portal headers on every call."""

    queued = iter(pages)
    seen: list[dict] = []

    def request(url: str, source_name: str, body: dict, headers: dict):
        assert source_name == "bytedance_careers"
        assert headers["website-path"] in PORTALS
        assert body["portal_type"] == 6
        seen.append({"url": url, "body": body, "headers": headers})
        return next(queued)

    request.seen = seen  # type: ignore[attr-defined]
    return request


def source(pages: list[dict], *, max_snapshot_passes: int = 3) -> ByteDanceCareersSource:
    return ByteDanceCareersSource(
        request_json=responder(pages),
        sleeper=lambda _d: None,
        jitter=lambda _l, _h: 0,
        max_snapshot_passes=max_snapshot_passes,
    )


def clean_pass(*records: dict) -> list[dict]:
    """One accepted pass: count probe, full inventory, terminal boundary."""

    n = len(records)
    return [
        payload(records[0], count=n),
        payload(*records, count=n),
        payload(count=n),
    ]


# --- happy path -------------------------------------------------------------


@pytest.mark.parametrize(
    ("fixture_name", "portal", "company_name"),
    [
        ("bytedance_careers_tiktok_page.json", PORTAL_TIKTOK, "TikTok"),
        ("bytedance_careers_bytedance_page.json", PORTAL_BYTEDANCE, "ByteDance"),
    ],
)
def test_captured_fixture_maps_every_canonical_field(
    fixture_name: str, portal: str, company_name: str
):
    captured = fixture(fixture_name)
    records = captured["data"]["job_post_list"]
    pages = clean_pass(*records)
    src = source(pages * 2)

    rows = src.fetch(company(company_name, portal))

    assert len(rows) == len(records)
    row, raw = rows[0], records[0]
    assert row["company"] == company_name
    assert row["title"] == raw["title"]
    assert row["location"] == raw["city_info"]["en_name"]
    assert row["description"] and row["requirements"]
    assert row["extra"]["bytedance_posting_id"] == raw["id"]
    assert row["extra"]["bytedance_careers_portal"] == portal
    assert row["extra"]["source_requisition_id"] == (
        f"bytedance_careers:{portal}:{raw['id']}"
    )
    assert row["source_url"].endswith(f"/position/{raw['id']}/detail")
    # Posting-date fields are always null in this contract, so none is claimed.
    assert row["date_posted"] == ""


def test_full_inventory_is_requested_from_the_advertised_count():
    records = [record(100 + v) for v in range(5)]
    request = responder(clean_pass(*records) * 2)
    src = ByteDanceCareersSource(request_json=request)

    rows = src.fetch(company())

    assert len(rows) == 5
    calls = request.seen  # type: ignore[attr-defined]
    # count probe, sized inventory request, terminal boundary - per pass
    assert [c["body"]["limit"] for c in calls] == [1, 5, 1, 1, 5, 1]
    assert [c["body"]["offset"] for c in calls] == [0, 0, 5, 0, 0, 5]
    assert all(c["url"].startswith("https://api.lifeattiktok.com") for c in calls)
    assert all(c["headers"]["website-path"] == "tiktok" for c in calls)
    d = src.last_diagnostics
    assert d.portal == PORTAL_TIKTOK
    assert d.advertised_count == 5
    assert d.raw_records_seen == 5
    assert d.retained_rows == 5
    assert d.requests_made == 6
    assert d.snapshot_passes_requested == 2


def test_each_portal_uses_its_own_host_and_header():
    records = [record(200)]
    request = responder(clean_pass(*records) * 2)
    src = ByteDanceCareersSource(request_json=request)

    src.fetch(company("ByteDance", PORTAL_BYTEDANCE))

    calls = request.seen  # type: ignore[attr-defined]
    assert all(c["url"].startswith("https://jobs.bytedance.com") for c in calls)
    assert all(c["headers"]["website-path"] == "en" for c in calls)
    assert all(c["headers"]["origin"] == "https://joinbytedance.com" for c in calls)


def test_clean_pass_reports_complete_and_non_degraded():
    src = source(clean_pass(record(101)) * 2)
    rows = src.fetch(company())
    assert src.last_health_diagnostics == DirectSourceDiagnostics(
        succeeded=True, retained_row_count=len(rows), complete=True
    )


def test_structurally_valid_empty_portal_is_healthy():
    empty = [payload(count=0)]
    src = source(empty * 2)

    assert src.fetch(company()) == []
    assert src.last_diagnostics.advertised_count == 0
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.degraded is False


def test_zero_count_with_records_fails_closed():
    bad = [payload(record(101), count=0)]
    src = source(bad * 2)
    with pytest.raises(SourceSchemaError, match="empty portal while returning records"):
        src.fetch(company())


# --- cross-listed requisitions ---------------------------------------------


def test_cross_listed_requisition_is_kept_under_each_portal_company():
    """One requisition published on both portals stays scoped to each company.

    The portal is authoritative scope, so neither row is dropped, reassigned, or
    deduplicated inside the source; downstream identity rules decide the rest.
    """

    shared = record(7385557673204402483, title="Backend Software Engineer")
    tiktok_rows = source(clean_pass(shared, record(101)) * 2).fetch(
        company("TikTok", PORTAL_TIKTOK)
    )
    bytedance_rows = source(clean_pass(shared, record(202)) * 2).fetch(
        company("ByteDance", PORTAL_BYTEDANCE)
    )

    tik = next(r for r in tiktok_rows if r["extra"]["bytedance_posting_id"] == shared["id"])
    byt = next(r for r in bytedance_rows if r["extra"]["bytedance_posting_id"] == shared["id"])
    assert tik["company"] == "TikTok"
    assert byt["company"] == "ByteDance"
    assert tik["title"] == byt["title"] == "Backend Software Engineer"
    # Portal scope keeps the two rows distinguishable rather than collapsing them.
    assert tik["extra"]["source_requisition_id"] != byt["extra"]["source_requisition_id"]
    assert tik["extra"]["bytedance_careers_portal"] == PORTAL_TIKTOK
    assert byt["extra"]["bytedance_careers_portal"] == PORTAL_BYTEDANCE
    assert tik["source_url"] != byt["source_url"]


# --- completeness failures --------------------------------------------------


def test_count_drift_between_probe_and_inventory_is_discarded_then_retried():
    drifting = [payload(record(101), count=2), payload(record(101), record(102), count=3)]
    good = clean_pass(record(101), record(102))
    src = source(drifting + good * 2)

    rows = src.fetch(company())

    assert len(rows) == 2
    assert src.last_diagnostics.snapshot_passes_requested == 3


def test_returned_rows_below_advertised_count_is_discarded():
    short = [payload(record(101), count=3), payload(record(101), count=3)]
    src = source(short * 3)
    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        src.fetch(company())


def test_changed_membership_between_passes_requires_a_matching_pass():
    a = clean_pass(record(101))
    b = clean_pass(record(102))
    src = source(a + b + b)

    rows = src.fetch(company())

    assert [r["extra"]["bytedance_posting_id"] for r in rows] == ["102"]
    assert src.last_diagnostics.snapshot_passes_requested == 3


def test_duplicate_posting_id_is_rejected():
    dup = [payload(record(101), count=2),
           payload(record(101), record(101, title="Other"), count=2)]
    src = source(dup * 2)
    with pytest.raises(SourceSchemaError, match="duplicate posting id"):
        src.fetch(company())


def test_records_past_the_advertised_count_fail_closed():
    bad = [payload(record(101), count=1), payload(record(101), count=1),
           payload(record(999), count=1)]
    src = source(bad * 3)
    with pytest.raises(SourceSchemaError, match="past its advertised count"):
        src.fetch(company())


def test_count_above_the_safeguard_is_rejected():
    huge = [payload(record(101), count=10_000_000)]
    src = source(huge * 2)
    with pytest.raises(SourceSchemaError, match="collection safeguard"):
        src.fetch(company())


@pytest.mark.parametrize(
    ("bad_payload", "match"),
    [
        ("not-an-object", "was not an object"),
        ({"data": {"count": 0, "job_post_list": []}}, "lacked a numeric code"),
        ({"code": "0", "data": {}}, "lacked a numeric code"),
        ({"code": 40000, "data": {}}, "reported an error code"),
        ({"code": 0}, "lacked its data object"),
        ({"code": 0, "data": {"job_post_list": []}}, "count was not an integer"),
        ({"code": 0, "data": {"count": -1}}, "count was negative"),
        ({"code": 0, "data": {"count": 1, "job_post_list": {}}}, "was not a list"),
    ],
)
def test_malformed_responses_fail_closed(bad_payload, match: str):
    src = source([bad_payload] * 3)
    with pytest.raises(SourceSchemaError, match=match):
        src.fetch(company())


@pytest.mark.parametrize(
    "broken",
    [
        "not-a-dict",
        {**record(1), "id": 12345},
        {**record(1), "id": "not-numeric"},
        {**record(1), "title": ""},
        {**record(1), "title": 5},
        {**record(1), "city_info": ["bad"]},
        {**record(1), "job_category": "bad"},
        {**record(1), "recruit_type": 7},
    ],
)
def test_malformed_records_fail_closed(broken):
    bad = [payload(record(101), count=1), payload(broken, count=1)]
    src = source(bad * 3)
    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_unsupported_portal_is_refused_before_any_request():
    def never(*_args, **_kwargs):
        raise AssertionError("no request should be made")

    src = ByteDanceCareersSource(request_json=never)
    bad = replace(company(), bytedance_careers_portal="lark")
    with pytest.raises(SourceSchemaError, match="not configured with a supported portal"):
        src.fetch(bad)


# --- transport --------------------------------------------------------------


def test_transient_request_retry_uses_shared_bound_and_degrades_result():
    pages = iter(clean_pass(record(101)) * 2)
    calls = 0
    delays: list[float] = []

    def request(_u, _n, _b, _h):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SourceFetchError("temporary", retryable=True)
        return next(pages)

    src = ByteDanceCareersSource(
        request_json=request, sleeper=delays.append, jitter=lambda _l, _h: 0
    )

    assert len(src.fetch(company())) == 1
    assert delays == [1.0]
    assert src.last_health_diagnostics.failed_request_count == 1
    assert src.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert src.last_health_diagnostics.degraded is True
    assert src.last_health_diagnostics.complete is True


def test_non_retryable_request_failure_fails_the_source():
    def request(_u, _n, _b, _h):
        raise SourceFetchError("portal unavailable", retryable=False)

    src = ByteDanceCareersSource(request_json=request)
    with pytest.raises(SourceFetchError, match="portal unavailable"):
        src.fetch(company())
    assert src.last_health_diagnostics.succeeded is None


# --- integration surfaces ---------------------------------------------------


def test_config_and_concurrency_portal_tables_match_the_adapter():
    """The config and concurrency layers may not import an adapter, so their
    portal tables are pinned against the adapter's authoritative one here."""

    assert set(BYTEDANCE_CAREERS_PORTAL_SITES) == set(PORTALS)
    assert set(BYTEDANCE_CAREERS_PORTAL_HOSTS) == set(PORTALS)
    for portal, config in PORTALS.items():
        assert BYTEDANCE_CAREERS_PORTAL_HOSTS[portal] == config["host"]
        assert BYTEDANCE_CAREERS_PORTAL_SITES[portal] == config["origin"].removeprefix(
            "https://"
        )


def test_registry_lazy_construction_origin_matching_and_fingerprint():
    companies = {
        item.name: item
        for item in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
        if item.ats == "bytedance_careers"
    }
    assert set(companies) == {"TikTok", "ByteDance"}
    assert companies["TikTok"].bytedance_careers_portal == PORTAL_TIKTOK
    assert companies["ByteDance"].bytedance_careers_portal == PORTAL_BYTEDANCE

    assert "bytedance_careers" in DIRECT_ATS
    assert isinstance(
        build_direct_sources()["bytedance_careers"], ByteDanceCareersSource
    )

    # Each portal is its own origin, so they never share a per-origin limit.
    tiktok_origin = direct_origin_key(
        "bytedance_careers", bytedance_careers_portal=PORTAL_TIKTOK
    )
    bytedance_origin = direct_origin_key(
        "bytedance_careers", bytedance_careers_portal=PORTAL_BYTEDANCE
    )
    assert tiktok_origin == "https://api.lifeattiktok.com"
    assert bytedance_origin == "https://jobs.bytedance.com"
    assert tiktok_origin != bytedance_origin

    assert company_matches("TikTok Inc.", companies["TikTok"])
    assert not company_matches("ByteDance", companies["TikTok"])
    assert not company_matches("TikTok", companies["ByteDance"])

    baseline = WatcherConfig(companies=(companies["TikTok"],))
    swapped = WatcherConfig(
        companies=(replace(companies["TikTok"], bytedance_careers_portal=PORTAL_BYTEDANCE),)
    )
    assert collection_config_fingerprint(baseline) != collection_config_fingerprint(swapped)


def test_lazy_export_exposes_the_source_without_eager_import():
    import watcher.sources as sources

    assert "ByteDanceCareersSource" in sources.__all__
    assert sources.ByteDanceCareersSource is ByteDanceCareersSource
