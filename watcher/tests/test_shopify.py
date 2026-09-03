"""Offline contract, completeness, and diagnostics tests for Shopify.

The fixtures build the same flattened React Router payload shape Shopify serves:
one JSON array where objects are ``{"_<key index>": <value index>}``, strings are
deduplicated, and negative indices are null sentinels.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from watcher.config import CompanyCfg, load_watchlist
from watcher.sources import ShopifySource, SourceSchemaError
from watcher.sources.contracts import SourceFetchError, TextHttpResponse


ROOT = Path(__file__).resolve().parents[2]


def _company(**overrides) -> CompanyCfg:
    values = {
        "name": "Shopify",
        "ats": "shopify",
        "source_url": "https://www.shopify.com/careers",
    }
    values.update(overrides)
    return CompanyCfg(**values)


class _Flat:
    """Build a flattened single-fetch payload the way Shopify serves one."""

    def __init__(self) -> None:
        self.values: list = [None]
        self._strings: dict[str, int] = {}

    def string(self, text: str) -> int:
        if text not in self._strings:
            self.values.append(text)
            self._strings[text] = len(self.values) - 1
        return self._strings[text]

    def literal(self, value) -> int:
        self.values.append(value)
        return len(self.values) - 1

    def obj(self, mapping: dict) -> int:
        encoded = {}
        for key, index in mapping.items():
            encoded[f"_{self.string(key)}"] = index
        self.values.append(encoded)
        return len(self.values) - 1

    def value(self, value) -> int:
        if value is None:
            return -5  # the payload's null sentinel
        if isinstance(value, str):
            return self.string(value)
        if isinstance(value, dict):
            return self.obj({k: self.value(v) for k, v in value.items()})
        if isinstance(value, list):
            self.values.append([self.value(v) for v in value])
            return len(self.values) - 1
        return self.literal(value)

    def render(self, inventory: list, *, route: bool = True, inventory_key: str = "jobPostingsWithJobs") -> str:
        data = self.obj({inventory_key: self.value(inventory)})
        if route:
            holder = self.obj({"data": data})
            self.obj({"($locale)/careers": holder})
        self.values[0] = {"_1": 2} if len(self.values) > 2 else {}
        return json.dumps(self.values) + "\n"


def _posting(index: int, *, posting_id: str | None = None, title: str | None = None, **overrides):
    pid = posting_id or f"{index:08x}-1111-4222-8333-444455556666"
    posting = {
        "id": pid,
        "title": f"Engineer {index}" if title is None else title,
        "jobId": f"{index:08x}-9999-4222-8333-444455556666",
        "status": "Published",
        "departmentName": "Engineering",
        "teamName": "Core",
        "locationName": "Americas",
        "locationExternalName": None,
        "workplaceType": "Remote",
        "employmentType": "FullTime",
        "isListed": True,
        "publishedDate": "2026-09-02",
        "applicationDeadline": None,
    }
    posting.update(overrides)
    return {"jobPosting": posting, "job": {"id": posting["jobId"], "title": posting["title"]}}


def _payload(inventory: list, **kwargs) -> str:
    return _Flat().render(inventory, **kwargs)


def _source(text, **kwargs):
    calls = []

    def request_text(url, name):
        calls.append((url, name))
        if isinstance(text, BaseException):
            raise text
        return TextHttpResponse(text=text, metadata={"status": 200})

    source = ShopifySource(
        request_text=request_text,
        sleeper=lambda _d: None,
        jitter=lambda _l, _h: 0.0,
        **kwargs,
    )
    return source, calls


# --- normal decode --------------------------------------------------------


def test_normal_listing_decodes_into_canonical_rows():
    source, calls = _source(_payload([_posting(1), _posting(2)]))

    rows = source.fetch(_company())

    assert calls == [("https://www.shopify.com/careers.data", "shopify")]
    assert len(rows) == 2
    assert rows[0]["title"] == "Engineer 1"
    assert rows[0]["location"] == "Americas"
    assert rows[0]["date_posted"] == "2026-09-02"
    assert rows[0]["source_url"] == (
        "https://www.shopify.com/careers/engineer-1"
        "_00000001-1111-4222-8333-444455556666"
    )
    assert rows[0]["extra"]["source_requisition_id"] == (
        "shopify:00000001-1111-4222-8333-444455556666"
    )
    assert rows[0]["extra"]["source_adapter"] == "shopify"
    assert rows[0]["extra"]["department"] == "Engineering"
    assert rows[0]["extra"]["workplace_type"] == "Remote"
    diagnostics = source.last_health_diagnostics
    assert diagnostics.complete is True
    assert diagnostics.degraded is False
    assert diagnostics.retained_row_count == 2


def test_canonical_route_matches_the_slug_shopify_redirects_to():
    assert ShopifySource.posting_url(
        "Account Executive, SMB", "1affb074-f055-4f5a-a97e-80d97d77da6e"
    ) == (
        "https://www.shopify.com/careers/account-executive-smb"
        "_1affb074-f055-4f5a-a97e-80d97d77da6e"
    )


def test_null_sentinels_are_absent_values_not_numbers():
    source, _ = _source(_payload([_posting(1, locationName=None, publishedDate=None)]))

    rows = source.fetch(_company())

    assert rows[0]["location"] == ""
    assert rows[0]["date_posted"] == ""


# --- empty vs unusable ----------------------------------------------------


def test_explicit_empty_inventory_is_a_trustworthy_empty_board():
    source, _ = _source(_payload([]))

    assert source.fetch(_company()) == []
    diagnostics = source.last_health_diagnostics
    assert diagnostics.complete is True
    assert diagnostics.degraded is False
    assert diagnostics.retained_row_count == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "listing response was empty"),
        ("   ", "listing response was empty"),
        ("<html>nope</html>", "not a decodable route payload"),
        ("{}", "not the expected flattened array"),
        ("[]", "not the expected flattened array"),
    ],
)
def test_missing_or_undecodable_payloads_fail_closed(text, expected):
    source, _ = _source(text)
    with pytest.raises(SourceSchemaError, match=expected):
        source.fetch(_company())


def test_payload_without_the_inventory_key_is_not_treated_as_empty():
    source, _ = _source(_payload([_posting(1)], inventory_key="jobPostingsRenamed"))
    with pytest.raises(SourceSchemaError, match="expected job inventory key"):
        source.fetch(_company())


def test_payload_without_the_careers_route_is_not_treated_as_empty():
    source, _ = _source(_payload([_posting(1)], route=False))
    with pytest.raises(SourceSchemaError, match="expected careers route"):
        source.fetch(_company())


def test_out_of_range_index_reference_fails_closed():
    flat = json.loads(_payload([_posting(1)]).split("\n")[0])
    key_index = flat.index("jobPostingsWithJobs")
    owner = next(v for v in flat if isinstance(v, dict) and f"_{key_index}" in v)
    owner[f"_{key_index}"] = len(flat) + 500
    source, _ = _source(json.dumps(flat) + "\n")
    with pytest.raises(SourceSchemaError, match="out-of-range index"):
        source.fetch(_company())


def test_inventory_that_is_not_a_list_fails_closed():
    source, _ = _source(_payload({"not": "a list"}))
    with pytest.raises(SourceSchemaError, match="was not a list"):
        source.fetch(_company())


# --- identity invariants --------------------------------------------------


def test_duplicate_posting_id_fails_closed():
    source, _ = _source(_payload([_posting(1), _posting(1)]))
    with pytest.raises(SourceSchemaError, match="duplicate posting id"):
        source.fetch(_company())


def test_conflicting_canonical_route_fails_closed():
    # Two distinct ids whose title and id slug collide would share one route.
    same_route = _posting(
        2,
        posting_id="00000001-1111-4222-8333-444455556666",
        title="Engineer 1",
    )
    duplicate_id_guard = _posting(1)
    source, _ = _source(_payload([duplicate_id_guard, same_route]))
    with pytest.raises(SourceSchemaError, match="duplicate posting id"):
        source.fetch(_company())


def test_every_row_keeps_a_unique_id_and_route():
    source, _ = _source(_payload([_posting(i) for i in range(1, 26)]))

    rows = source.fetch(_company())

    assert len({r["extra"]["shopify_posting_id"] for r in rows}) == 25
    assert len({r["source_url"] for r in rows}) == 25


# --- record quality -------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "not-a-uuid"},
        {"title": ""},
        {"isListed": False},
        {"isListed": None},
        {"status": "Draft"},
        {"status": None},
    ],
)
def test_unlisted_or_invalid_records_are_rejected(overrides):
    source, _ = _source(_payload([_posting(1, **overrides)]))
    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(_company())


def test_mixed_invalid_records_are_skipped_and_diagnosed():
    source, _ = _source(
        _payload([_posting(1), _posting(2, isListed=False), _posting(3)])
    )

    rows = source.fetch(_company())

    assert [r["title"] for r in rows] == ["Engineer 1", "Engineer 3"]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.schema_error_row_count == 1
    assert diagnostics.reason_codes == ("schema_invalid_records_skipped",)
    assert diagnostics.incomplete is True
    assert diagnostics.complete is False


def test_all_invalid_records_fail_rather_than_reporting_completeness():
    source, _ = _source(_payload([_posting(1, status="Draft")]))
    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(_company())


def test_transient_request_failure_retries_within_the_bounded_policy():
    calls = {"n": 0}

    def request_text(url, name):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SourceFetchError("temporary", retryable=True)
        return TextHttpResponse(text=_payload([_posting(1)]), metadata={})

    source = ShopifySource(
        request_text=request_text, sleeper=lambda _d: None, jitter=lambda _l, _h: 0.0
    )
    rows = source.fetch(_company())

    assert len(rows) == 1
    assert source.retry_attempts == 1
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.complete is True


# --- architecture and integration ----------------------------------------


def test_registry_builds_shopify_without_extra_construction_arguments():
    from watcher.sources.registry import DIRECT_ATS, build_direct_sources

    assert "shopify" in DIRECT_ATS
    built = build_direct_sources()
    assert isinstance(built["shopify"], ShopifySource)
    assert built["shopify"].name == "shopify"


def test_shopify_shares_the_first_party_origin_not_an_ats_host():
    from watcher.collection_concurrency import direct_origin_key

    assert direct_origin_key("shopify") == "https://www.shopify.com"


def test_shopify_is_not_an_ashby_variant_and_uses_canonical_owners():
    module = ROOT / "watcher/sources/shopify.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "watcher.sources.base" not in imported
    assert "watcher.sources.ashby" not in imported
    assert {
        "watcher.sources.contracts",
        "watcher.sources.direct",
        "watcher.sources.retry",
        "watcher.sources.rows",
        "watcher.sources.transport",
    } <= imported
    assert not any(name.startswith("watcher.collection") for name in imported)

    from watcher.sources.direct import DirectRecordAdapter, SinglePayloadDirectAdapter

    assert issubclass(ShopifySource, DirectRecordAdapter)
    assert not issubclass(ShopifySource, SinglePayloadDirectAdapter)


def test_real_watchlist_builds_shopify_on_its_first_party_source():
    config = load_watchlist()
    shopify = next(c for c in config.companies if c.name == "Shopify")

    assert shopify.ats == "shopify"
    assert shopify.token == ""
    assert shopify.module == ""
