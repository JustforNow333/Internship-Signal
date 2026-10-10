"""Offline coverage for the opt-in hosted static-analysis cache.

The cache must be invisible in hosted output: disabled, cold, and warm replay
produce identical analyzed and mapped jobs, the snapshot and its fingerprint
never change, and every cache or fingerprint failure falls back to the normal
uncached analyzer. PostgreSQL-backed import/match parity is covered in
``test_hosted_collect_and_import.py``.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
from datetime import UTC, datetime

import pytest
from app.hosted import analysis_cache as hosted_cache
from app.hosted.catalog import CompanyCatalog
from app.hosted.job_mapper import map_final_jobs
from app.hosted.snapshot_jobs import replay_snapshot_jobs, snapshot_sha256
from app.hosted.timing import PREFIX, HostedTiming

from backend.app.ingest import analyze_rows
from backend.app.dedupe import dedupe
from watcher.collection_snapshot import (
    CollectionBatch,
    collection_config_fingerprint,
    save_collection_snapshot,
)
from watcher.config import load_watchlist
from watcher.sources.base import make_row

CAPTURED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LINE_RE = re.compile(r"^HOSTED-TIMING kind=[a-z]+( [a-z_]+=[a-z0-9_.]+)+$")
URL_PREFIX = "https://careers.example.test/jobs/"


@pytest.fixture(scope="module")
def watcher_config():
    return load_watchlist()


@pytest.fixture(scope="module")
def company_names(watcher_config) -> list[str]:
    catalog = CompanyCatalog.from_watcher_config(watcher_config)
    names = [company.name for company in catalog.companies if company.selectable]
    assert len(names) >= 2
    return names[:2]


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "volume" / "analysis-cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def cache_disabled_by_default(monkeypatch):
    monkeypatch.delenv(hosted_cache.CACHE_PATH_ENV, raising=False)


def _row(company: str, title: str, identifier: str, **overrides) -> dict:
    extra = {"source_requisition_id": f"REQ-{identifier}", "active": True}
    extra.update(overrides.pop("extra", {}))
    fields = {
        "location": "New York, NY",
        "compensation": "$40/hr",
        "description": "Build Python services with mentorship and code review.",
        "requirements": "Python, SQL, Git. Pursuing a bachelor's degree.",
        "deadline": "2026-10-15",
    }
    fields.update(overrides)
    return make_row(
        source="direct",
        source_adapter="workday",
        company=company,
        title=title,
        source_url=f"{URL_PREFIX}{identifier}",
        date_posted="2026-08-30",
        internship_type="Summer 2027 Internship",
        extra=extra,
        **fields,
    )


def _rows(company_names: list[str]) -> list[dict]:
    first, second = company_names
    return [
        _row(first, "Software Engineering Intern", "1"),
        _row(first, "Data Science Intern", "2", extra={"student_status": "PhD only"}),
        # A duplicate of the first posting exercises dedupe on every path.
        _row(first, "Software Engineering Intern", "1"),
        _row(second, "Backend Engineering Intern", "3", location="Remote", remote_status="Remote"),
        _row(second, "Quant Research Intern", "4", deadline="2026-09-02"),
    ]


def _snapshot(tmp_path, watcher_config, rows, name="snapshot.json.gz"):
    batch = CollectionBatch.create(
        captured_at=CAPTURED_AT,
        collection_config_fingerprint=collection_config_fingerprint(watcher_config),
        rows=rows,
        errors=[],
        source_attempts=[],
    )
    path = tmp_path / name
    save_collection_snapshot(batch, path)
    return path


def _replay(path, timing=None):
    return replay_snapshot_jobs(
        path,
        analyzer=hosted_cache.build_hosted_analyzer(timing),
    )


def _serialized(jobs) -> str:
    return json.dumps(list(jobs), sort_keys=True, ensure_ascii=False, default=str)


def _cache_line(timing: HostedTiming) -> str | None:
    lines = [line for line in timing.lines(exit_code=0) if "kind=cache" in line]
    assert len(lines) <= 1
    return lines[0] if lines else None


def _cache_fields(timing: HostedTiming) -> dict[str, str]:
    line = _cache_line(timing)
    assert line is not None
    return dict(part.split("=", 1) for part in line.split()[2:])


def _uncached_jobs(rows) -> str:
    return _serialized(analyze_rows(copy.deepcopy(rows), today=CAPTURED_AT.date()))


def test_cache_is_disabled_by_default_and_uses_the_uncached_analyzer(
    tmp_path, watcher_config, company_names, monkeypatch
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("cache path must not run while disabled")

    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", forbidden)
    monkeypatch.setattr(hosted_cache, "hosted_static_cache_version", forbidden)
    rows = _rows(company_names)
    snapshot = _snapshot(tmp_path, watcher_config, rows)
    timing = HostedTiming()

    assert hosted_cache.hosted_analysis_cache_path() is None
    replayed = _replay(snapshot, timing)

    assert _serialized(replayed.jobs) == _uncached_jobs(rows)
    assert _cache_line(timing) is None
    assert not list(tmp_path.rglob("*.sqlite"))


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_cache_path_means_disabled(monkeypatch, value) -> None:
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, value)

    assert hosted_cache.hosted_analysis_cache_path() is None


def test_cold_miss_then_warm_hit_with_identical_replay_and_mapping(
    tmp_path, watcher_config, company_names, cache_path
) -> None:
    rows = _rows(company_names)
    snapshot = _snapshot(tmp_path, watcher_config, rows)
    snapshot_hash = snapshot_sha256(snapshot)
    catalog = CompanyCatalog.from_watcher_config(watcher_config)

    disabled = replay_snapshot_jobs(snapshot)
    cold_timing, warm_timing = HostedTiming(), HostedTiming()
    cold = _replay(snapshot, cold_timing)
    warm = _replay(snapshot, warm_timing)

    cold_fields = _cache_fields(cold_timing)
    warm_fields = _cache_fields(warm_timing)
    assert cold_fields["enabled"] == "true"
    assert (cold_fields["rows"], cold_fields["hits"], cold_fields["misses"]) == ("4", "0", "4")
    assert cold_fields["writes"] == "4"
    assert (warm_fields["hits"], warm_fields["misses"], warm_fields["writes"]) == ("4", "0", "0")
    assert warm_fields["hit_rate"] == "1.000"
    assert cache_path.is_file()

    expected = _serialized(disabled.jobs)
    assert _serialized(cold.jobs) == expected
    assert _serialized(warm.jobs) == expected
    assert _serialized(disabled.jobs) == _uncached_jobs(rows)
    mapped = map_final_jobs(disabled.jobs, catalog)
    assert mapped.jobs
    assert map_final_jobs(cold.jobs, catalog) == mapped
    assert map_final_jobs(warm.jobs, catalog) == mapped
    # The cache never touches the snapshot or its import fingerprint.
    assert snapshot_sha256(snapshot) == snapshot_hash
    assert {disabled.source_fingerprint, cold.source_fingerprint, warm.source_fingerprint} == {
        snapshot_hash
    }


def test_relevant_change_misses_while_volatile_change_still_hits(
    tmp_path, watcher_config, company_names, cache_path
) -> None:
    rows = _rows(company_names)
    _replay(_snapshot(tmp_path, watcher_config, rows, "first.json.gz"))

    changed = copy.deepcopy(rows)
    # Rows 0 and 2 are one deduplicated posting, so both carry the edit.
    for index in (0, 2):
        changed[index]["description"] = "Build Rust services for low-latency trading."
    changed[1]["extra"]["student_status"] = "Undergraduates only"
    changed[3]["extra"]["workday_detail_status"] = "skipped_budget"
    changed[4]["deadline"] = "2026-12-01"
    timing = HostedTiming()
    replayed = _replay(_snapshot(tmp_path, watcher_config, changed, "second.json.gz"), timing)

    fields = _cache_fields(timing)
    assert (fields["hits"], fields["misses"]) == ("2", "2")
    assert _serialized(replayed.jobs) == _uncached_jobs(changed)


def test_cache_version_is_stable_sqlite_safe_and_tracks_static_code(monkeypatch) -> None:
    version = hosted_cache.hosted_static_cache_version()

    assert isinstance(version, int)
    assert 0 <= version < 2**63
    assert hosted_cache.hosted_static_cache_version() == version

    original = hosted_cache._module_source
    monkeypatch.setattr(
        hosted_cache,
        "_module_source",
        lambda name: original(name) + (b"\n# changed\n" if name == "backend.app.scoring" else b""),
    )
    changed_code = hosted_cache.hosted_static_cache_version()
    monkeypatch.setattr(hosted_cache, "_module_source", original)
    monkeypatch.setattr(hosted_cache, "STATIC_ANALYSIS_CACHE_VERSION", 999)
    changed_constant = hosted_cache.hosted_static_cache_version()

    assert len({version, changed_code, changed_constant}) == 3


def test_every_static_module_resolves_to_python_source() -> None:
    for name in hosted_cache.STATIC_ANALYSIS_MODULES:
        assert hosted_cache._module_source(name)


def test_changed_code_fingerprint_invalidates_old_entries(
    tmp_path, watcher_config, company_names, cache_path, monkeypatch
) -> None:
    rows = _rows(company_names)
    snapshot = _snapshot(tmp_path, watcher_config, rows)
    _replay(snapshot)

    original = hosted_cache._module_source
    monkeypatch.setattr(
        hosted_cache,
        "_module_source",
        lambda name: original(name) + (b"\n# edited\n" if name == "backend.app.classify" else b""),
    )
    timing = HostedTiming()
    replayed = _replay(snapshot, timing)

    fields = _cache_fields(timing)
    assert (fields["hits"], fields["misses"], fields["writes"]) == ("0", "4", "4")
    assert _serialized(replayed.jobs) == _uncached_jobs(rows)
    with sqlite3.connect(cache_path) as connection:
        versions = connection.execute(
            "select count(distinct cache_version) from analysis_cache"
        ).fetchone()[0]
    assert versions == 2


def test_fingerprint_failure_runs_uncached_without_touching_the_cache(
    tmp_path, watcher_config, company_names, cache_path, monkeypatch
) -> None:
    def unreadable(_name):
        raise OSError("source unavailable")

    monkeypatch.setattr(hosted_cache, "_module_source", unreadable)
    rows = _rows(company_names)
    timing = HostedTiming()

    assert hosted_cache.hosted_static_cache_version() is None
    replayed = _replay(_snapshot(tmp_path, watcher_config, rows), timing)

    assert _serialized(replayed.jobs) == _uncached_jobs(rows)
    assert _cache_fields(timing) == {
        "cache": "analysis",
        "enabled": "false",
        "reason": "fingerprint_unavailable",
    }
    assert not cache_path.exists()


def test_corrupt_cache_file_falls_back_to_identical_output(
    tmp_path, watcher_config, company_names, cache_path
) -> None:
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"this is not a sqlite database" * 64)
    rows = _rows(company_names)

    replayed = _replay(_snapshot(tmp_path, watcher_config, rows))

    assert _serialized(replayed.jobs) == _uncached_jobs(rows)


def test_corrupt_artifacts_are_replaced_with_fresh_analysis(
    tmp_path, watcher_config, company_names, cache_path
) -> None:
    rows = _rows(company_names)
    snapshot = _snapshot(tmp_path, watcher_config, rows)
    _replay(snapshot)
    with sqlite3.connect(cache_path) as connection:
        connection.execute("update analysis_cache set artifact_json = '{\"broken\": true'")
    timing = HostedTiming()

    replayed = _replay(snapshot, timing)

    fields = _cache_fields(timing)
    assert (fields["hits"], fields["invalid"]) == ("0", "4")
    assert _serialized(replayed.jobs) == _uncached_jobs(rows)


@pytest.mark.parametrize("layout", ["path_is_directory", "parent_is_file"])
def test_unwritable_cache_location_falls_back_to_identical_output(
    tmp_path, watcher_config, company_names, monkeypatch, layout
) -> None:
    blocker = tmp_path / "blocked"
    if layout == "path_is_directory":
        blocker.mkdir()
        path = blocker
    else:
        blocker.write_text("not a directory")
        path = blocker / "analysis-cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    rows = _rows(company_names)

    replayed = _replay(_snapshot(tmp_path, watcher_config, rows))

    assert _serialized(replayed.jobs) == _uncached_jobs(rows)


def test_unexpected_cache_failure_falls_back_on_pristine_rows(
    tmp_path, watcher_config, company_names, cache_path, monkeypatch
) -> None:
    def fails_after_mutating(rows, **_kwargs):
        dedupe(rows)  # the real cached path mutates rows before it can fail
        for row in rows:
            row["title"] = "mutated"
        raise RuntimeError("unexpected cache failure")

    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", fails_after_mutating)
    rows = _rows(company_names)
    timing = HostedTiming()

    replayed = _replay(_snapshot(tmp_path, watcher_config, rows), timing)

    assert _serialized(replayed.jobs) == _uncached_jobs(rows)
    assert _cache_fields(timing)["reason"] == "cache_failed"


def test_cache_telemetry_is_bounded_and_sanitized(
    tmp_path, watcher_config, company_names, cache_path, capsys
) -> None:
    rows = _rows(company_names)
    timing = HostedTiming()
    _replay(_snapshot(tmp_path, watcher_config, rows), timing)
    version = str(hosted_cache.hosted_static_cache_version())

    timing.emit(exit_code=0)
    captured = capsys.readouterr()
    cache_lines = [line for line in captured.out.splitlines() if "kind=cache" in line]

    assert len(cache_lines) == 1
    line = cache_lines[0]
    assert line.startswith(f"{PREFIX} kind=cache cache=analysis enabled=true ")
    assert LINE_RE.fullmatch(line)
    output = captured.out + captured.err
    forbidden = [*company_names, *(row["title"] for row in rows), URL_PREFIX, str(cache_path), version]
    for value in forbidden:
        assert value not in output
    # No cache key or other digest-shaped value is ever printed.
    assert not re.search(r"[0-9a-f]{32,}", output)


def test_fallback_warning_names_only_a_reason_and_exception_type(
    tmp_path, watcher_config, company_names, cache_path, monkeypatch, caplog
) -> None:
    def fails(*_args, **_kwargs):
        raise RuntimeError(f"{cache_path} {company_names[0]}")

    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", fails)
    rows = _rows(company_names)

    with caplog.at_level("WARNING"):
        _replay(_snapshot(tmp_path, watcher_config, rows))

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "Hosted analysis cache unavailable (cache_failed: RuntimeError); "
        "using uncached analysis."
    ]


@pytest.mark.parametrize(
    "stats",
    [object(), type("Stats", (), {"rows": "x", "hits": None, "lookup_seconds": "nan?"})()],
)
def test_cache_telemetry_never_raises_on_malformed_stats(stats) -> None:
    timing = HostedTiming()

    timing.record_analysis_cache(stats)
    timing.record_analysis_cache(None, reason="Weird Reason/../path")

    for line in timing.lines(exit_code=0):
        assert LINE_RE.fullmatch(line)
