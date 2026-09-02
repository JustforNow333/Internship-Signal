"""Netflix legacy Eightfold contract and integration tests."""

from __future__ import annotations

import copy
from dataclasses import replace
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.company_matching import company_matches
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.config import CompanyCfg, ConfigError, WatcherConfig, load_watchlist
from watcher.sources import EightfoldSource, SourceSchemaError
from watcher.sources.contracts import JsonHttpResponse, SourceFetchError

FIXTURE = json.loads(
    (Path(__file__).with_name("fixtures") / "eightfold_legacy_page.json").read_text(
        encoding="utf-8"
    )
)
HOST = "explore.jobs.netflix.net"
DOMAIN = "netflix.com"


def _company(**overrides) -> CompanyCfg:
    values = {
        "name": "Netflix",
        "ats": "eightfold",
        "eightfold_host": HOST,
        "eightfold_domain": DOMAIN,
        "eightfold_variant": "legacy",
        "source_url": f"https://{HOST}/careers",
    }
    values.update(overrides)
    return CompanyCfg(**values)


def _posting(index: int, **overrides) -> dict:
    record = copy.deepcopy(FIXTURE["positions"][0])
    record.update(
        {
            "id": 790298014000 + index,
            "name": f"Engineer {index}",
            "ats_job_id": f"AJRT{index:05d}",
            "canonicalPositionUrl": f"https://{HOST}/careers/job/{790298014000 + index}",
        }
    )
    record.update(overrides)
    return record


def _page(records: list, total: int, *, domain: str = DOMAIN) -> dict:
    return {"domain": domain, "count": total, "positions": records}


class Recorder:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, url, source_name):
        assert source_name == "eightfold"
        self.urls.append(url)
        if not self.pages:
            raise AssertionError("unexpected request")
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return JsonHttpResponse(payload=value, metadata={"status_code": 200})


def _source(pages, **kwargs):
    recorder = Recorder(pages)
    source = EightfoldSource(
        request_json=recorder,
        sleeper=lambda _seconds: None,
        jitter=lambda _start, _end: 0,
        page_delay_seconds=0,
        **kwargs,
    )
    return source, recorder


def test_normal_multi_page_enumeration_and_explicit_terminal_page():
    records = [_posting(index) for index in range(1, 13)]
    source, recorder = _source(
        [_page(records[:10], 12), _page(records[10:], 12), _page([], 12)]
    )

    rows = source.fetch(_company())

    assert len(rows) == source.raw_count == source.unique_count == 12
    assert source.advertised_total == 12
    assert source.pages_requested == 3
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.degraded is False
    assert source.last_health_diagnostics.malformed_row_count == 0
    assert source.last_health_diagnostics.schema_error_row_count == 0
    assert rows[0]["company"] == "Netflix"
    assert rows[0]["description"] == "Build reliable systems."
    assert rows[0]["extra"]["source_adapter"] == "eightfold"
    starts = [parse_qs(urlsplit(url).query)["start"][0] for url in recorder.urls]
    assert starts == ["0", "10", "12"]


def test_explicit_empty_board_is_complete():
    source, _ = _source([_page([], 0)])
    assert source.fetch(_company()) == []
    assert source.last_health_diagnostics.complete is True


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([_page([_posting(i) for i in range(1, 11)], 11), _page([_posting(11)], 12)], "total changed"),
        ([_page([_posting(i) for i in range(1, 11)], 11), _page([], 11)], "ended before"),
        ([_page([_posting(1)], 2)], "invalid page arithmetic"),
    ],
)
def test_fails_closed_on_changed_total_premature_empty_and_raw_mismatch(pages, message):
    source, _ = _source(pages)
    with pytest.raises(SourceSchemaError, match=message):
        source.fetch(_company())


def test_fails_closed_on_repeated_page():
    page = _page([_posting(index) for index in range(1, 11)], 20)
    source, _ = _source([page, page])
    with pytest.raises(SourceSchemaError, match="repeated"):
        source.fetch(_company())


@pytest.mark.parametrize("conflicting", [False, True])
def test_fails_closed_on_duplicate_or_conflicting_ids(conflicting):
    first = [_posting(index) for index in range(1, 11)]
    duplicate = copy.deepcopy(first[0])
    if conflicting:
        duplicate["name"] = "Different title"
    source, _ = _source([_page(first, 11), _page([duplicate], 11)])
    with pytest.raises(SourceSchemaError, match="duplicate|conflicting"):
        source.fetch(_company())


@pytest.mark.parametrize(
    "record",
    [None, {}, _posting(1, id="bad"), _posting(1, name=""), _posting(1, locations=[1])],
)
def test_fails_closed_on_malformed_required_records(record):
    source, _ = _source([_page([record], 1)])
    with pytest.raises(SourceSchemaError):
        source.fetch(_company())


def test_fails_closed_on_request_failure_without_aggressive_retries():
    source, _ = _source(
        [SourceFetchError("failed", retryable=False)], max_attempts=1
    )
    with pytest.raises(SourceFetchError):
        source.fetch(_company())


def test_fails_closed_when_advertised_total_exceeds_safety_bound():
    source, recorder = _source([_page([_posting(1)], 11)], max_pages=1)
    with pytest.raises(SourceSchemaError, match="maximum page safeguard"):
        source.fetch(_company())
    assert len(recorder.urls) == 1


def test_registry_lazy_export_origin_and_watchlist_company_contract():
    from watcher.sources.registry import DIRECT_ATS, build_direct_sources

    netflix = next(c for c in load_watchlist().companies if c.name == "Netflix")
    assert "eightfold" in DIRECT_ATS
    assert isinstance(build_direct_sources()["eightfold"], EightfoldSource)
    assert netflix.eightfold_host == HOST
    assert netflix.eightfold_domain == DOMAIN
    assert netflix.eightfold_variant == "legacy"
    assert company_matches("Netflix, Inc.", netflix)
    assert direct_origin_key("eightfold", eightfold_host=HOST) == f"https://{HOST}"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("eightfold_host", "jobs.example.test"),
        ("eightfold_domain", "example.com"),
        ("eightfold_variant", "changed"),
    ],
)
def test_eightfold_config_fields_affect_replay_fingerprint(field, value):
    company = _company()
    baseline = collection_config_fingerprint(WatcherConfig(companies=(company,)))
    changed = replace(company, **{field: value})

    assert collection_config_fingerprint(WatcherConfig(companies=(changed,))) != baseline


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("eightfold_variant", "pcsx", "variant"),
        ("eightfold_host", "https://bad.test", "host"),
        ("eightfold_domain", "", "domain"),
        ("source_url", "http://explore.jobs.netflix.net/careers", "source_url"),
    ],
)
def test_watchlist_rejects_invalid_eightfold_config(tmp_path, field, value, message):
    values = {
        "eightfold_host": HOST,
        "eightfold_domain": DOMAIN,
        "eightfold_variant": "legacy",
        "source_url": f"https://{HOST}/careers",
    }
    values[field] = value
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
        '  - name: "Netflix"\n    ats: eightfold\n'
        + "".join(f'    {key}: "{item}"\n' for key, item in values.items()),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=message):
        load_watchlist(path)
