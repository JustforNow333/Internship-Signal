"""Offline coverage for the hosted collection's single-copy memory invariants.

The scheduled run keeps no unnecessary full copies of the collected rows:

* the collected batch is released once its snapshot is saved, because the
  import replays only that snapshot (M1); and
* cached analysis keeps no pristine deep copy for its fallback. A part-way
  cache failure rebuilds fresh rows from the immutable replayed snapshot only
  when the fallback actually runs (M2).

Both must leave every analyzed job byte-for-byte identical. PostgreSQL-backed
import, match, and notification parity is covered in
``test_hosted_collect_and_import.py``.
"""

from __future__ import annotations

import copy
import gc
import json
import uuid
import weakref
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.hosted import analysis_cache as hosted_cache
from app.hosted import collect_and_import
from app.hosted.catalog import CompanyCatalog
from app.hosted.collect_and_import import main as collect_main
from app.hosted.job_import import ImportCounters, JobImportResult
from app.hosted.snapshot_jobs import replay_snapshot_jobs
from app.hosted.timing import HostedTiming

from backend.app.dedupe import dedupe
from backend.app.ingest import analyze_rows
from watcher.collection_snapshot import (
    CollectionBatch,
    collection_config_fingerprint,
    save_collection_snapshot,
)
from watcher.config import load_watchlist
from watcher.sources.base import make_row

CAPTURED_AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
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


@pytest.fixture(autouse=True)
def cache_disabled_by_default(monkeypatch):
    monkeypatch.delenv(hosted_cache.CACHE_PATH_ENV, raising=False)


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "volume" / "analysis-cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    return path


@pytest.fixture
def forbid_pristine_copy(monkeypatch):
    """Fail if the hosted analyzer deep-copies the full row list."""

    def forbidden(_value, *_args, **_kwargs):
        raise AssertionError("cached analysis must not hold a pristine deep copy")

    monkeypatch.setattr(hosted_cache, "copy", SimpleNamespace(deepcopy=forbidden))


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


def _batch(watcher_config, rows) -> CollectionBatch:
    return CollectionBatch.create(
        captured_at=CAPTURED_AT,
        collection_config_fingerprint=collection_config_fingerprint(watcher_config),
        rows=rows,
        errors=[],
        source_attempts=[],
    )


def _snapshot(tmp_path, watcher_config, rows, name="snapshot.json.gz"):
    path = tmp_path / name
    save_collection_snapshot(_batch(watcher_config, rows), path)
    return path


def _replay(path, timing=None):
    return replay_snapshot_jobs(
        path,
        analyzer=hosted_cache.build_hosted_analyzer(timing),
    )


def _serialized(jobs) -> str:
    return json.dumps(list(jobs), sort_keys=True, ensure_ascii=False, default=str)


def _uncached_jobs(rows) -> str:
    return _serialized(analyze_rows(copy.deepcopy(rows), today=CAPTURED_AT.date()))


def _cache_fields(timing: HostedTiming) -> dict[str, str]:
    lines = [line for line in timing.lines(exit_code=0) if "kind=cache" in line]
    assert len(lines) == 1
    return dict(part.split("=", 1) for part in lines[0].split()[2:])


def _mutating_failure(rows, **_kwargs):
    dedupe(rows)  # the real cached path mutates rows before it can fail
    for row in rows:
        row["title"] = "mutated"
        row["description"] = "mutated"
    raise RuntimeError("unexpected cache failure")


def _count_mutable_rows(monkeypatch) -> list[int]:
    calls = [0]
    original = CollectionBatch.mutable_rows

    def counting(self):
        calls[0] += 1
        return original(self)

    monkeypatch.setattr(CollectionBatch, "mutable_rows", counting)
    return calls


# --- M2: no pristine deep copy -----------------------------------------------


def test_cold_warm_and_partial_hits_need_no_pristine_copy_and_stay_identical(
    tmp_path, watcher_config, company_names, cache_path, forbid_pristine_copy
) -> None:
    rows = _rows(company_names)
    subset = [rows[0], rows[3]]
    expected = _uncached_jobs(rows)

    cold_timing = HostedTiming()
    cold = _replay(_snapshot(tmp_path, watcher_config, subset, "subset.json.gz"), cold_timing)
    assert _serialized(cold.jobs) == _uncached_jobs(subset)
    assert _cache_fields(cold_timing)["hits"] == "0"

    partial_timing = HostedTiming()
    partial = _replay(_snapshot(tmp_path, watcher_config, rows), partial_timing)
    partial_fields = _cache_fields(partial_timing)
    assert int(partial_fields["hits"]) > 0
    assert int(partial_fields["misses"]) > 0
    assert _serialized(partial.jobs) == expected

    warm_timing = HostedTiming()
    warm = _replay(_snapshot(tmp_path, watcher_config, rows, "again.json.gz"), warm_timing)
    assert _cache_fields(warm_timing)["misses"] == "0"
    assert _serialized(warm.jobs) == expected


def test_a_part_way_cache_failure_rebuilds_fresh_rows_from_the_snapshot(
    tmp_path, watcher_config, company_names, cache_path, monkeypatch, forbid_pristine_copy
) -> None:
    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", _mutating_failure)
    calls = _count_mutable_rows(monkeypatch)
    rows = _rows(company_names)
    timing = HostedTiming()

    replayed = _replay(_snapshot(tmp_path, watcher_config, rows), timing)

    assert _serialized(replayed.jobs) == _uncached_jobs(rows)
    assert _cache_fields(timing)["reason"] == "cache_failed"
    # One copy for the cached attempt, one fresh copy only for the fallback.
    assert calls[0] == 2


def test_a_successful_cached_run_builds_the_mutable_rows_exactly_once(
    tmp_path, watcher_config, company_names, cache_path, monkeypatch
) -> None:
    calls = _count_mutable_rows(monkeypatch)

    _replay(_snapshot(tmp_path, watcher_config, _rows(company_names)))

    assert calls[0] == 1


def test_the_fallback_never_sees_rows_mutated_by_the_failed_attempt(
    tmp_path, watcher_config, company_names, cache_path, monkeypatch, forbid_pristine_copy
) -> None:
    seen: list[list[dict]] = []

    def failing(rows, **_kwargs):
        seen.append(rows)
        _mutating_failure(rows)

    def recording_uncached(rows, *, today=None):
        seen.append(rows)
        return analyze_rows(rows, today=today)

    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", failing)
    monkeypatch.setattr(hosted_cache, "analyze_rows", recording_uncached)
    rows = _rows(company_names)

    replayed = _replay(_snapshot(tmp_path, watcher_config, rows))

    attempted, fallback = seen
    assert fallback is not attempted
    assert all(row["title"] != "mutated" for row in fallback)
    assert _serialized(replayed.jobs) == _uncached_jobs(rows)
    assert replayed.batch.rows[0]["title"] == rows[0]["title"]


def test_an_unrecoverable_fallback_still_fails_closed_without_partial_output(
    tmp_path, watcher_config, company_names, cache_path, monkeypatch
) -> None:
    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", _mutating_failure)
    analyzer = hosted_cache.build_hosted_analyzer()

    def broken_rows():
        raise RuntimeError("snapshot rows unavailable")

    with pytest.raises(RuntimeError, match="snapshot rows unavailable"):
        analyzer(_rows(company_names), today=CAPTURED_AT.date(), fresh_rows=broken_rows)


def test_direct_callers_without_fresh_rows_keep_the_pristine_copy_fallback(
    company_names, cache_path, monkeypatch
) -> None:
    monkeypatch.setattr(hosted_cache, "analyze_rows_with_cache", _mutating_failure)
    rows = _rows(company_names)
    analyzer = hosted_cache.build_hosted_analyzer()

    jobs = analyzer(copy.deepcopy(rows), today=CAPTURED_AT.date())

    assert _serialized(jobs) == _uncached_jobs(rows)


def test_the_default_uncached_analyzer_is_called_exactly_as_before(
    tmp_path, watcher_config, company_names, monkeypatch
) -> None:
    received: list[dict] = []

    def plain(rows, *, today=None):
        received.append({"today": today})
        return analyze_rows(rows, today=today)

    rows = _rows(company_names)

    replayed = replay_snapshot_jobs(
        _snapshot(tmp_path, watcher_config, rows), analyzer=plain
    )

    assert received == [{"today": CAPTURED_AT.date()}]
    assert _serialized(replayed.jobs) == _uncached_jobs(rows)


def test_a_disabled_cache_needs_no_copy_and_matches_the_cached_paths(
    tmp_path, watcher_config, company_names, forbid_pristine_copy
) -> None:
    rows = _rows(company_names)

    replayed = _replay(_snapshot(tmp_path, watcher_config, rows))

    assert _serialized(replayed.jobs) == _uncached_jobs(rows)


# --- M1: the collected batch is released before import -----------------------


def _import_result() -> JobImportResult:
    return JobImportResult(
        run_id=uuid.uuid4(),
        source_fingerprint="a" * 64,
        outcome="imported",
        counters=ImportCounters(
            jobs_received=1,
            jobs_inserted=1,
            jobs_updated=0,
            jobs_unchanged=0,
            jobs_skipped=0,
        ),
        skipped_reasons={},
    )


@pytest.fixture
def offline_import(monkeypatch):
    monkeypatch.setenv(
        "HOSTED_DATABASE_URL", "postgresql+psycopg://user:pw@localhost:1/unused"
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(collect_and_import, "run_import_audit", lambda *_a, **_k: [])
    state: dict[str, object] = {}

    def fake_import(snapshot_path, **kwargs):
        gc.collect()
        state["alive"] = [ref() is not None for ref in state.get("refs", [])]
        state["snapshot"] = snapshot_path.read_bytes()
        state["replayed"] = replay_snapshot_jobs(
            snapshot_path,
            watchlist_path=kwargs["watchlist_path"],
            allow_collection_config_mismatch=True,
            timing=kwargs.get("timing"),
        )
        return _import_result()

    monkeypatch.setattr(collect_and_import, "import_snapshot_into_hosted", fake_import)
    return state


def test_the_collected_batch_is_released_before_the_import_starts(
    offline_import, watcher_config, company_names, tmp_path, capsys
) -> None:
    rows = _rows(company_names)
    expected_snapshot = tmp_path / "expected.json.gz"
    save_collection_snapshot(_batch(watcher_config, rows), expected_snapshot)
    refs: list[weakref.ref] = []
    offline_import["refs"] = refs

    def collector(_config):
        batch = _batch(watcher_config, rows)
        refs.append(weakref.ref(batch))
        return batch

    assert collect_main([], collector=collector) == 0

    assert offline_import["alive"] == [False]
    # The import still replays exactly what was collected.
    assert offline_import["snapshot"] == expected_snapshot.read_bytes()
    assert _serialized(offline_import["replayed"].jobs) == _uncached_jobs(rows)
    output = capsys.readouterr().out.splitlines()
    assert output[0].startswith("HOSTED-COLLECTION rows=5 ")


def test_a_failed_snapshot_save_keeps_its_exit_status(
    offline_import, watcher_config, company_names, monkeypatch, capsys
) -> None:
    def failing_save(_batch, _path):
        raise OSError("disk full")

    monkeypatch.setattr(collect_and_import, "save_collection_snapshot", failing_save)

    def collector(_config):
        return _batch(watcher_config, _rows(company_names))

    assert collect_main([], collector=collector) == 1
    captured = capsys.readouterr()
    assert captured.err == "Hosted collection failed: invalid_collection_result\n"
    assert "snapshot" not in offline_import
