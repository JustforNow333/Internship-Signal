import copy
import json
import re
from pathlib import Path

import pytest

from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, WatcherConfig, load_watchlist
from watcher.run import CollectionStats, collect_rows
from watcher.sources import SourceFetchError, SourceSchemaError
from watcher.sources.ashby import AshbySource


FIXTURES = Path(__file__).parent / "fixtures"
APP_DATA = re.compile(r"window\.__appData\s*=\s*")


def fixture_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def whatnot_company():
    return CompanyCfg(
        name="Whatnot",
        ats="ashby",
        token="whatnot",
        aliases=("WhatNot",),
        alumni_match=("whatnot",),
        source_url="https://jobs.ashbyhq.com/whatnot",
        terms=("Summer 2027",),
    )


def fixture_app_data():
    html = fixture_text("ashby_hosted_whatnot.html")
    match = APP_DATA.search(html)
    assert match is not None
    return json.JSONDecoder().raw_decode(html[match.end() :])[0]


def hosted_html(data):
    return (
        "<!doctype html><html><body><script>window.__appData = "
        + json.dumps(data, separators=(",", ":"), sort_keys=True)
        + ";</script></body></html>"
    )


def test_hosted_board_contract_maps_canonical_rows_and_stable_identity():
    source = AshbySource()

    rows = source.parse_hosted(
        fixture_text("ashby_hosted_whatnot.html"),
        whatnot_company(),
    )

    assert len(rows) == 3
    first = rows[0]
    assert first["company"] == "Whatnot"
    assert first["title"] == "Software Engineer Intern"
    assert first["location"] == (
        "New York, New York, United States, "
        "San Francisco, California, United States"
    )
    assert first["source_url"] == (
        "https://jobs.ashbyhq.com/whatnot/"
        "11111111-1111-4111-8111-111111111111"
    )
    assert first["remote_status"] == "Hybrid"
    assert first["internship_type"] == "Intern"
    assert first["extra"]["source_adapter"] == "ashby"
    assert first["extra"]["source_id"] == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert first["extra"]["source_requisition_id"] == first["extra"]["source_id"]
    assert first["extra"]["job_url"] == first["source_url"]
    assert first["extra"]["team"] == "Engineering"
    assert [row["extra"]["source_id"] for row in rows[1:]] == [
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    assert rows[1]["title"] == rows[2]["title"]
    assert rows[1]["source_url"] != rows[2]["source_url"]
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.degraded is False


def test_fetch_uses_one_hosted_board_request_only_after_public_api_404(monkeypatch):
    calls = []

    def public_404(url, _source_name, **_kwargs):
        calls.append(("json", url))
        raise SourceFetchError(
            "not found",
            error_code="permanent_http_error",
            status_code=404,
        )

    def hosted(url, _source_name, **_kwargs):
        calls.append(("text", url))
        return fixture_text("ashby_hosted_whatnot.html")

    monkeypatch.setattr("watcher.sources.ashby.get_json_response", public_404, raising=False)
    monkeypatch.setattr("watcher.sources.ashby.fetch_json", public_404, raising=False)
    monkeypatch.setattr("watcher.sources.ashby.fetch_text", hosted, raising=False)

    rows = AshbySource().fetch(whatnot_company())

    assert len(rows) == 3
    assert calls == [
        ("json", "https://api.ashbyhq.com/posting-api/job-board/whatnot"),
        ("text", "https://jobs.ashbyhq.com/whatnot"),
    ]


def test_non_404_public_api_failure_does_not_fall_back(monkeypatch):
    def public_failure(*_args, **_kwargs):
        raise SourceFetchError(
            "unavailable",
            error_code="transient_http_error",
            status_code=503,
            retryable=True,
        )

    monkeypatch.setattr("watcher.sources.ashby.get_json_response", public_failure, raising=False)
    monkeypatch.setattr("watcher.sources.ashby.fetch_json", public_failure, raising=False)
    monkeypatch.setattr(
        "watcher.sources.ashby.fetch_text",
        lambda *_args, **_kwargs: pytest.fail("hosted fallback must not run"),
        raising=False,
    )

    with pytest.raises(SourceFetchError) as raised:
        AshbySource().fetch(whatnot_company())

    assert raised.value.status_code == 503


def test_hosted_board_exact_duplicates_are_bounded_but_conflicts_fail():
    data = fixture_app_data()
    duplicate = copy.deepcopy(data["jobBoard"]["jobPostings"][0])
    data["jobBoard"]["jobPostings"].append(duplicate)
    source = AshbySource()

    rows = source.parse_hosted(hosted_html(data), whatnot_company())

    assert len(rows) == 3
    assert source.last_health_diagnostics.duplicate_row_count == 1
    assert source.last_health_diagnostics.degraded is False

    duplicate["title"] = "Conflicting title"
    with pytest.raises(SourceSchemaError, match="conflicting duplicate"):
        AshbySource().parse_hosted(hosted_html(data), whatnot_company())


def test_hosted_board_malformed_neighbors_skip_but_all_malformed_fails():
    data = fixture_app_data()
    data["jobBoard"]["jobPostings"].append(
        {
            "id": "44444444-4444-4444-8444-444444444444",
            "title": "",
            "locationName": "New York, New York, United States",
        }
    )
    source = AshbySource()

    rows = source.parse_hosted(hosted_html(data), whatnot_company())

    assert len(rows) == 3
    assert source.last_health_diagnostics.schema_error_row_count == 1
    assert source.last_health_diagnostics.degraded is True
    assert source.last_health_diagnostics.complete is False

    data["jobBoard"]["jobPostings"] = data["jobBoard"]["jobPostings"][-1:]
    with pytest.raises(SourceSchemaError, match="none were valid"):
        AshbySource().parse_hosted(hosted_html(data), whatnot_company())


def test_hosted_board_empty_and_invalid_shapes_are_distinct():
    data = fixture_app_data()
    data["jobBoard"]["jobPostings"] = []
    source = AshbySource()

    assert source.parse_hosted(hosted_html(data), whatnot_company()) == []
    assert source.last_health_diagnostics.complete is True

    with pytest.raises(SourceSchemaError, match="app data"):
        AshbySource().parse_hosted("<html><body>no data</body></html>", whatnot_company())

    data["organization"]["hostedJobsPageSlug"] = "another-board"
    with pytest.raises(SourceSchemaError, match="slug"):
        AshbySource().parse_hosted(hosted_html(data), whatnot_company())


def test_successful_hosted_fallback_produces_normal_source_health(monkeypatch):
    monkeypatch.setattr(
        "watcher.sources.ashby.get_json_response",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SourceFetchError(
                "not found",
                error_code="permanent_http_error",
                status_code=404,
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "watcher.sources.ashby.fetch_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SourceFetchError(
                "not found",
                error_code="permanent_http_error",
                status_code=404,
            )
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "watcher.sources.ashby.fetch_text",
        lambda *_args, **_kwargs: fixture_text("ashby_hosted_whatnot.html"),
        raising=False,
    )
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(whatnot_company(),)),
        direct_sources={"ashby": AshbySource()},
        stats=stats,
    )

    assert len(rows) == 3
    assert errors == []
    assert stats.source_attempts[0].succeeded is True
    assert stats.source_attempts[0].rows_returned == 3
    assert stats.source_attempts[0].degraded is False
    assert stats.source_attempts[0].complete is True


def test_watchlist_registers_whatnot_official_ashby_board():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    whatnot = next(item for item in config.companies if item.name == "Whatnot")

    assert whatnot.ats == "ashby"
    assert whatnot.token == "whatnot"
    assert whatnot.aliases == ("WhatNot",)
    assert whatnot.alumni_match == ("whatnot",)
    assert whatnot.source_url == "https://jobs.ashbyhq.com/whatnot"
    assert whatnot.coverage_status == ""
    assert whatnot.platform_family == ""
