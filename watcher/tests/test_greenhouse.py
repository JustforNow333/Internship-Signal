"""Focused Greenhouse single-board and composed-board contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from watcher.collection_snapshot import collection_config_fingerprint
from watcher.config import CompanyCfg, ConfigError, WatcherConfig, load_watchlist
from watcher.sources import SourceFetchError, SourceSchemaError
from watcher.sources.greenhouse import GreenhouseSource


def _company(*, tokens: tuple[str, ...] = ()) -> CompanyCfg:
    return CompanyCfg(
        name="Example Trading",
        ats="greenhouse",
        token="university",
        greenhouse_tokens=tokens,
    )


def _job(
    job_id: int,
    *,
    title: str = "Software Engineer",
    url: str | None = None,
    board_name: str = "Example Board",
) -> dict:
    return {
        "id": job_id,
        "title": title,
        "absolute_url": url or f"https://job-boards.greenhouse.io/example/jobs/{job_id}",
        "company_name": board_name,
        "location": {"name": "Chicago, IL"},
        "content": "Build reliable systems.",
        "first_published": "2026-09-01T12:00:00Z",
    }


def _payload(*jobs: dict) -> dict:
    return {"jobs": list(jobs)}


def _install_payloads(monkeypatch, payloads: dict[str, object]) -> list[str]:
    calls: list[str] = []

    def request(url: str, source_name: str):
        assert source_name == "greenhouse"
        calls.append(url)
        value = payloads[url]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("watcher.sources.greenhouse.fetch_json", request)
    return calls


def test_single_board_fetch_behavior_is_unchanged(monkeypatch):
    company = _company()
    payload = _payload(_job(101))
    endpoint = GreenhouseSource.endpoint(company.token)
    calls = _install_payloads(monkeypatch, {endpoint: payload})

    source = GreenhouseSource()
    rows = source.fetch(company)

    assert calls == [endpoint]
    assert rows == GreenhouseSource().parse(payload, company)
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.retained_row_count == 1


def test_two_board_fetch_returns_one_complete_ordered_union(monkeypatch):
    company = _company(tokens=("university", "experienced"))
    university = GreenhouseSource.endpoint("university")
    experienced = GreenhouseSource.endpoint("experienced")
    calls = _install_payloads(
        monkeypatch,
        {
            university: _payload(_job(101, title="University Engineer")),
            experienced: _payload(_job(202, title="Experienced Engineer")),
        },
    )

    source = GreenhouseSource()
    rows = source.fetch(company)

    assert calls == [university, experienced]
    assert [row["extra"]["source_id"] for row in rows] == ["101", "202"]
    assert source.last_health_diagnostics.retained_row_count == 2
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.degraded is False


@pytest.mark.parametrize(
    ("university_jobs", "experienced_jobs", "expected_ids"),
    [
        ((), (), []),
        ((), (_job(202),), ["202"]),
        ((_job(101),), (), ["101"]),
    ],
)
def test_composed_boards_preserve_explicit_empty_board_completeness(
    monkeypatch, university_jobs, experienced_jobs, expected_ids
):
    company = _company(tokens=("university", "experienced"))
    _install_payloads(
        monkeypatch,
        {
            GreenhouseSource.endpoint("university"): _payload(*university_jobs),
            GreenhouseSource.endpoint("experienced"): _payload(*experienced_jobs),
        },
    )

    source = GreenhouseSource()
    rows = source.fetch(company)

    assert [row["extra"]["source_id"] for row in rows] == expected_ids
    assert source.last_health_diagnostics.complete is True


def test_composed_board_failure_returns_no_partial_inventory(monkeypatch):
    company = _company(tokens=("university", "experienced"))
    calls = _install_payloads(
        monkeypatch,
        {
            GreenhouseSource.endpoint("university"): _payload(_job(101)),
            GreenhouseSource.endpoint("experienced"): SourceFetchError("unavailable"),
        },
    )

    source = GreenhouseSource()
    with pytest.raises(SourceFetchError, match="unavailable"):
        source.fetch(company)

    assert calls == [
        GreenhouseSource.endpoint("university"),
        GreenhouseSource.endpoint("experienced"),
    ]
    assert source.last_health_diagnostics.complete is False
    assert source.last_health_diagnostics.succeeded is None


def test_composed_board_schema_failure_returns_no_partial_inventory(monkeypatch):
    company = _company(tokens=("university", "experienced"))
    _install_payloads(
        monkeypatch,
        {
            GreenhouseSource.endpoint("university"): _payload(_job(101)),
            GreenhouseSource.endpoint("experienced"): {"openings": []},
        },
    )

    source = GreenhouseSource()
    with pytest.raises(SourceSchemaError, match="jobs"):
        source.fetch(company)

    assert source.last_health_diagnostics.complete is False


def test_each_board_contributes_to_the_normal_parse_completeness_diagnostics(
    monkeypatch,
):
    company = _company(tokens=("university", "experienced"))
    _install_payloads(
        monkeypatch,
        {
            GreenhouseSource.endpoint("university"): _payload(
                _job(101),
                {"id": 102, "title": "Missing URL"},
            ),
            GreenhouseSource.endpoint("experienced"): _payload(_job(202)),
        },
    )

    source = GreenhouseSource()
    rows = source.fetch(company)

    assert [row["extra"]["source_id"] for row in rows] == ["101", "202"]
    assert source.last_health_diagnostics.schema_error_row_count == 1
    assert source.last_health_diagnostics.degraded is True
    assert source.last_health_diagnostics.complete is False


def test_exact_cross_board_duplicate_is_deduplicated(monkeypatch):
    company = _company(tokens=("university", "experienced"))
    _install_payloads(
        monkeypatch,
        {
            GreenhouseSource.endpoint("university"): _payload(
                _job(101, board_name="University Board")
            ),
            GreenhouseSource.endpoint("experienced"): _payload(
                _job(101, board_name="Experienced Board")
            ),
        },
    )

    source = GreenhouseSource()
    rows = source.fetch(company)

    assert len(rows) == 1
    assert source.last_health_diagnostics.duplicate_row_count == 1
    assert source.last_health_diagnostics.complete is True


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (_job(101), _job(101, title="Conflicting title", url="https://example.test/other")),
        (_job(101), _job(202, url="https://job-boards.greenhouse.io/example/jobs/101")),
    ],
)
def test_conflicting_cross_board_id_or_url_collision_fails_closed(
    monkeypatch, first, second
):
    company = _company(tokens=("university", "experienced"))
    _install_payloads(
        monkeypatch,
        {
            GreenhouseSource.endpoint("university"): _payload(first),
            GreenhouseSource.endpoint("experienced"): _payload(second),
        },
    )

    source = GreenhouseSource()
    with pytest.raises(SourceSchemaError, match="conflicting duplicate"):
        source.fetch(company)

    assert source.last_health_diagnostics.complete is False


def test_composed_board_requires_stable_source_ids(monkeypatch):
    company = _company(tokens=("university", "experienced"))
    job = _job(101)
    job["id"] = ""
    _install_payloads(
        monkeypatch,
        {
            GreenhouseSource.endpoint("university"): _payload(job),
            GreenhouseSource.endpoint("experienced"): _payload(),
        },
    )

    with pytest.raises(SourceSchemaError, match="stable identity"):
        GreenhouseSource().fetch(company)


def test_greenhouse_multi_board_configuration_loads_and_affects_fingerprint(tmp_path):
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
        '  - name: "Example Trading"\n'
        '    ats: greenhouse\n'
        '    token: "university"\n'
        '    greenhouse_tokens: ["university", "experienced"]\n',
        encoding="utf-8",
    )

    company = load_watchlist(path).companies[0]
    baseline = collection_config_fingerprint(WatcherConfig(companies=(company,)))

    assert tuple(company.greenhouse_tokens) == ("university", "experienced")
    assert collection_config_fingerprint(
        WatcherConfig(
            companies=(replace(company, greenhouse_tokens=("university", "alumni")),)
        )
    ) != baseline


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        ('["university", "university"]', "unique"),
        ('["experienced"]', "include token"),
    ],
)
def test_greenhouse_multi_board_configuration_requires_safe_scope(
    tmp_path, tokens, message
):
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
        '  - name: "Example Trading"\n'
        '    ats: greenhouse\n'
        '    token: "university"\n'
        f"    greenhouse_tokens: {tokens}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_watchlist(path)
