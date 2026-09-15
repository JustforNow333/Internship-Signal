"""Optiver's complete first-party paginated jobs inventory contract."""

from __future__ import annotations

import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.config import CompanyCfg, ConfigError, load_watchlist
from watcher.sources.contracts import SourceError, SourceFetchError, SourceSchemaError
from watcher.sources.optiver import (
    API_URL,
    MAX_API_RESPONSE_BYTES,
    MAX_JOBS_PAGE_BYTES,
    SOURCE_URL,
    OptiverSource,
)
from watcher.sources.registry import DIRECT_ATS, DIRECT_COMPLETE_ATS, build_direct_sources


def company(source_url: str = SOURCE_URL) -> CompanyCfg:
    return CompanyCfg(name="Optiver", ats="optiver", source_url=source_url)


def record(identity: int, *, title: str | None = None, href: str | None = None) -> dict:
    name = title or f"Software Engineer {identity}"
    return {
        "reactComponentName": "Components.JobsListItem",
        "serverOnlyRender": False,
        "clientOnlyRender": False,
        "title": name,
        "location": "Amsterdam",
        "experience": "Internship",
        "domain": "Technology",
        "href": href or f"/join-us/jobs/technology/amsterdam/job-{identity}/",
        "jobClickTracking": {
            "vacancyName": name,
            "vacancyOffice": "amsterdam",
            "vacancyDomain": "technology",
            "vacancyLevel": "internship",
        },
        "componentID": identity,
        "culture": "en",
        "anchorId": None,
    }


def rendered_page(records: list[dict], total: int, *, endpoint: str = "/en/api/v1/jobs") -> str:
    props = {
        "reactComponentName": "Components.JobsFiltered",
        "serverOnlyRender": False,
        "clientOnlyRender": False,
        "items": records,
        "totalCount": total,
        "apiEndpoint": endpoint,
        "filterConfig": {"id": "jobs-filter", "filterGroups": []},
        "labels": {},
    }
    return (
        "<html><body><script>window.ReactJsAsyncInit=function(){"
        "ReactDOM.hydrateRoot(root,React.createElement(Components.JobsFiltered,"
        f"{json.dumps(props, separators=(',', ':'))}));"
        "};</script></body></html>"
    )


def source(
    records: list[dict] | None = None,
    *,
    page_size: int = 2,
    max_pages: int = 20,
    max_snapshot_passes: int = 2,
    mutate_page=None,
    mutate_html=None,
):
    values = copy.deepcopy(records if records is not None else [record(i) for i in range(1, 6)])
    calls: list[str] = []

    def request_text(url: str, name: str):
        assert name == "optiver"
        assert url == SOURCE_URL
        calls.append(url)
        initial = copy.deepcopy(values[:page_size])
        html = rendered_page(initial, len(values))
        return mutate_html(html, len(calls)) if mutate_html else html

    def request_json(url: str, name: str):
        assert name == "optiver"
        calls.append(url)
        query = parse_qs(urlsplit(url).query)
        offset = int(query["from"][0])
        requested_size = int(query["size"][0])
        payload = {
            "items": copy.deepcopy(values[offset : offset + requested_size]),
            "totalCount": len(values),
        }
        return mutate_page(payload, offset, len(calls)) if mutate_page else payload

    return (
        OptiverSource(
            request_json=request_json,
            request_text=request_text,
            sleeper=lambda _delay: None,
            jitter=lambda _low, _high: 0,
            page_size=page_size,
            max_pages=max_pages,
            max_snapshot_passes=max_snapshot_passes,
            page_delay_seconds=0,
        ),
        calls,
    )


def test_multi_page_collection_requires_final_partial_and_empty_terminal_pages():
    src, calls = source()

    rows = src.fetch(company())

    assert len(rows) == 5
    assert [row["extra"]["source_id"] for row in rows] == [
        "optiver:1",
        "optiver:2",
        "optiver:3",
        "optiver:4",
        "optiver:5",
    ]
    assert len({row["source_url"] for row in rows}) == 5
    assert rows[0]["source_url"] == (
        "https://www.optiver.com/join-us/jobs/technology/amsterdam/job-1/"
    )
    assert rows[0]["date_posted"] == ""
    assert rows[0]["deadline"] == ""
    assert rows[0]["internship_type"] == "Internship"
    assert src.snapshot_passes_requested == 2
    assert src.pages_requested == 8
    assert sum(url == SOURCE_URL for url in calls) == 2
    assert [parse_qs(urlsplit(url).query)["from"][0] for url in calls if url != SOURCE_URL] == [
        "0", "2", "4", "5", "0", "2", "4", "5"
    ]
    assert src.last_health_diagnostics.complete is True
    assert src.last_health_diagnostics.degraded is False


def test_rendered_inventory_must_match_the_first_api_page_and_total():
    def mismatch(html: str, _call: int) -> str:
        return html.replace("Software Engineer 1", "Different title", 2)

    src, _calls = source(mutate_html=mismatch)

    with pytest.raises(SourceSchemaError, match="rendered inventory"):
        src.fetch(company())


def test_repeated_page_fails_to_make_progress():
    first = [record(1), record(2)]

    def repeat(payload: dict, offset: int, _call: int) -> dict:
        if offset == 2:
            payload["items"] = copy.deepcopy(first)
        return payload

    src, _calls = source(mutate_page=repeat)

    with pytest.raises(SourceSchemaError, match="repeated pagination page"):
        src.fetch(company())


def test_inconsistent_total_never_publishes_a_snapshot():
    def changed_total(payload: dict, offset: int, _call: int) -> dict:
        if offset == 2:
            payload["totalCount"] += 1
        return payload

    src, _calls = source(mutate_page=changed_total)

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        src.fetch(company())


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"items": []},
        {"items": {}, "totalCount": 0},
        {"items": [], "totalCount": -1},
        {"items": [], "totalCount": True},
        {"items": [], "totalCount": 0, "extra": "drift"},
    ],
)
def test_malformed_top_level_schema_fails_closed(payload):
    src, _calls = source(mutate_page=lambda _value, _offset, _call: payload)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda item: item.pop("title"),
        lambda item: item.update(title=""),
        lambda item: item.update(location=[]),
        lambda item: item.update(culture="nl"),
        lambda item: item.update(anchorId="hidden"),
        lambda item: item.update(extra="drift"),
        lambda item: item.update(jobClickTracking=[]),
    ],
)
def test_malformed_records_and_schema_drift_fail_closed(corrupt):
    records = [record(1), record(2)]
    corrupt(records[1])
    src, _calls = source(records)

    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_missing_stable_id_fails_closed():
    records = [record(1), record(2)]
    records[1]["componentID"] = None
    src, _calls = source(records)

    with pytest.raises(SourceSchemaError, match="posting ID"):
        src.fetch(company())


def test_exact_duplicate_record_fails_closed():
    records = [record(1), copy.deepcopy(record(1))]
    src, _calls = source(records)

    with pytest.raises(SourceSchemaError, match="duplicate posting ID"):
        src.fetch(company())


def test_conflicting_duplicate_id_fails_closed():
    records = [record(1), record(1, title="Conflicting title", href="/join-us/jobs/technology/london/other/")]
    src, _calls = source(records)

    with pytest.raises(SourceSchemaError, match="duplicate posting ID"):
        src.fetch(company())


def test_normalized_url_conflict_fails_closed():
    records = [
        record(1, href="/join-us/jobs/technology/amsterdam/same/"),
        record(2, href="/join-us/jobs/technology/amsterdam/same"),
    ]
    src, _calls = source(records)

    with pytest.raises(SourceSchemaError, match="duplicate posting URL"):
        src.fetch(company())


def test_request_failure_on_any_page_fails_closed():
    src, _calls = source()
    original = src._request_json

    def fail(url: str, name: str):
        if parse_qs(urlsplit(url).query)["from"] == ["2"]:
            raise SourceFetchError("failed", error_code="forbidden")
        return original(url, name)

    src._request_json = fail

    with pytest.raises(SourceFetchError):
        src.fetch(company())
    assert src.last_health_diagnostics.complete is False


def test_empty_inventory_must_be_consistent_in_rendering_and_api_twice():
    src, _calls = source([])

    rows = src.fetch(company())

    assert rows == []
    assert src.snapshot_passes_requested == 2
    assert src.pages_requested == 2
    assert src.last_health_diagnostics.complete is True


def test_advertised_total_cannot_exceed_page_safeguard():
    def oversized_total(payload: dict, _offset: int, _call: int) -> dict:
        payload["totalCount"] = 999
        return payload

    src, _calls = source(max_pages=2, mutate_page=oversized_total)

    with pytest.raises(SourceSchemaError, match="maximum page safeguard"):
        src.fetch(company())


@pytest.mark.parametrize("surface", ["html", "api"])
def test_oversized_transport_response_fails_closed(surface):
    src, _calls = source()
    error = SourceFetchError("too large", error_code="response_too_large")
    if surface == "html":
        src._request_text = lambda _url, _name: (_ for _ in ()).throw(error)
    else:
        src._request_json = lambda _url, _name: (_ for _ in ()).throw(error)

    with pytest.raises(SourceFetchError, match="too large"):
        src.fetch(company())
    assert MAX_JOBS_PAGE_BYTES < 16 * 1024 * 1024
    assert MAX_API_RESPONSE_BYTES < 16 * 1024 * 1024


def test_two_complete_but_different_snapshots_do_not_settle():
    state = {"pass": 0}

    def request_text(_url: str, _name: str):
        state["pass"] += 1
        values = [record(1, title=f"Title pass {state['pass']}")]
        return rendered_page(values, 1)

    def request_json(url: str, _name: str):
        offset = int(parse_qs(urlsplit(url).query)["from"][0])
        values = [record(1, title=f"Title pass {state['pass']}")]
        return {"items": values[offset : offset + 1], "totalCount": 1}

    src = OptiverSource(
        request_json=request_json,
        request_text=request_text,
        sleeper=lambda _delay: None,
        page_size=1,
        max_snapshot_passes=2,
        page_delay_seconds=0,
    )

    with pytest.raises(SourceSchemaError, match="did not stabilize"):
        src.fetch(company())


def test_only_the_official_unfiltered_source_url_is_accepted():
    src, _calls = source()

    with pytest.raises(SourceError, match="official jobs inventory"):
        src.fetch(company("https://www.optiver.com/join-us/students/"))


def test_registry_package_concurrency_and_watchlist_integration():
    from watcher.sources import OptiverSource as ExportedOptiverSource

    cfg = next(c for c in load_watchlist().companies if c.name == "Optiver")

    assert cfg.ats == "optiver"
    assert cfg.source_url == SOURCE_URL
    assert "optiver" in DIRECT_ATS
    assert "optiver" in DIRECT_COMPLETE_ATS
    assert isinstance(build_direct_sources()["optiver"], OptiverSource)
    assert ExportedOptiverSource is OptiverSource
    assert direct_origin_key("optiver") == "https://www.optiver.com"
    assert OptiverSource.endpoint(offset=32, size=16) == (
        f"{API_URL}?from=32&size=16"
    )


def test_invalid_optiver_source_url_is_rejected_during_config_load(tmp_path):
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
        '  - name: "Optiver"\n    ats: optiver\n'
        '    source_url: "https://example.test/jobs"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="official jobs inventory"):
        load_watchlist(path)
