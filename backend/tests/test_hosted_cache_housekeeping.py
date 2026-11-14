"""Bounded pruning of obsolete hosted analysis-cache versions (C1).

Every cache file here is written by the deployed watcher cache code
(``watcher.analysis_cache.AnalysisCache``), so the tests exercise the exact
on-disk schema and transaction behavior production uses.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.hosted import analysis_cache as hosted_cache
from app.hosted import cache_housekeeping as hk
from app.hosted.timing import PREFIX, STAGE_ORDER, HostedTiming

from backend.app import config as backend_config
from backend.app.ingest import analyze_static_row, deduplicate_rows
from backend.app.profile import load_profile
from watcher.analysis_cache import AnalysisCache
from watcher.source_health import iso_utc
from watcher.sources.base import make_row

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
CURRENT = 7_000_000_000_000_001
OBSOLETE = 7_000_000_000_000_002
OLDER = 7_000_000_000_000_003
LINE_RE = re.compile(r"^HOSTED-TIMING kind=[a-z]+( [a-z_]+=[a-z0-9_.]+)+$")
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv(hk.MODE_ENV, raising=False)
    monkeypatch.delenv(hosted_cache.CACHE_PATH_ENV, raising=False)


@pytest.fixture(scope="module")
def pool() -> list[dict]:
    """A few real static-analysis artifacts, reused under unique fingerprints."""

    rows = [
        make_row(
            source="direct",
            source_adapter="workday",
            company="Example Co",
            title=title,
            location="New York, NY",
            description="Build Python services. " * 40 + str(index),
            requirements="Python and SQL. Pursuing a bachelor's degree.",
            source_url=f"https://careers.example.test/jobs/{index}",
            date_posted="2026-10-01",
            internship_type="Summer 2027 Internship",
        )
        for index, title in enumerate(
            ["Software Engineering Intern", "Data Science Intern", "Account Executive"]
        )
    ]
    unique, _ = deduplicate_rows(rows)
    profile, known = load_profile(), backend_config.load_known_companies()
    return [analyze_static_row(row, profile=profile, known=known) for row in unique]


def fp(version: int, index: int) -> str:
    return hashlib.sha256(f"{version}:{index}".encode()).hexdigest()


def write(path: Path, pool, version: int, indexes, accessed_at: datetime) -> None:
    with AnalysisCache(path, cache_version=version) as cache:
        stored = cache.store_many(
            {fp(version, i): pool[i % len(pool)] for i in indexes},
            accessed_at=accessed_at,
        )
    assert stored == len(list(indexes))


def build(path: Path, pool, *, obsolete=300, current=200, recent_obsolete=0, older=0):
    """Obsolete rows past the grace period, current rows (some very old)."""

    if older:
        write(path, pool, OLDER, range(older), NOW - timedelta(days=20))
    write(path, pool, OBSOLETE, range(obsolete), NOW - timedelta(days=19))
    if recent_obsolete:
        write(
            path,
            pool,
            OBSOLETE,
            range(obsolete, obsolete + recent_obsolete),
            NOW - timedelta(hours=2),
        )
    # Current rows older than the grace period must never be pruned.
    write(path, pool, CURRENT, range(current // 2), NOW - timedelta(days=25))
    write(path, pool, CURRENT, range(current // 2, current), NOW - timedelta(hours=1))
    return path


def versions(path: Path) -> dict[int, int]:
    with sqlite3.connect(path) as connection:
        return dict(
            connection.execute(
                "select cache_version, count(*) from analysis_cache group by 1"
            ).fetchall()
        )


def current_rows(path: Path) -> list[tuple]:
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "select fingerprint, cache_version, artifact_json, created_at, "
            "last_accessed_at from analysis_cache where cache_version = ? "
            "order by fingerprint",
            (CURRENT,),
        ).fetchall()


def integrity(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        return connection.execute("pragma integrity_check").fetchone()[0]


JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")


def hot_journal(path: Path) -> bool:
    """A rollback journal SQLite would replay (valid magic header)."""

    journal = Path(f"{path}-journal")
    if not journal.exists():
        return False
    with journal.open("rb") as stream:
        return stream.read(8) == JOURNAL_MAGIC


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def limits(**overrides) -> hk.PruneLimits:
    return hk.PruneLimits(**{**hk.DEFAULT_LIMITS.__dict__, **overrides})


def prune(path, mode="on", **kwargs):
    kwargs.setdefault("current_version", CURRENT)
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("disk_free", lambda _path: 50 * 2**30)
    return hk.run_cache_housekeeping(path, mode=mode, **kwargs)


# --- mode flag -------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ("off", "")),
        ("", ("off", "")),
        ("off", ("off", "")),
        (" OFF ", ("off", "")),
        ("report", ("report", "")),
        ("Report", ("report", "")),
        ("on", ("on", "")),
        ("ON\n", ("on", "")),
        ("true", ("off", "invalid_mode")),
        ("1", ("off", "invalid_mode")),
        ("prune", ("off", "invalid_mode")),
    ],
)
def test_mode_defaults_off_and_rejects_unknown_values(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv(hk.MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(hk.MODE_ENV, value)
    assert hk.housekeeping_mode() == expected


def test_off_mode_never_opens_the_cache(monkeypatch, tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    before = digest(path)
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("off mode must not touch SQLite")

    monkeypatch.setattr(hk.sqlite3, "connect", forbidden)
    timing = HostedTiming()
    assert hk.run_for_collection(timing, now=NOW) is None
    assert digest(path) == before
    assert not [line for line in timing.lines(exit_code=0) if "housekeeping" in line]


def test_invalid_mode_is_reported_but_never_prunes(monkeypatch, tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    before = digest(path)
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(hk.MODE_ENV, "yes")
    result = hk.run_for_collection(HostedTiming(), now=NOW)
    assert (result.mode, result.status, result.reason) == ("off", "skipped", "invalid_mode")
    assert digest(path) == before


# --- report mode -----------------------------------------------------------


def test_report_mode_counts_eligible_rows_without_writing(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300, recent_obsolete=40)
    before = digest(path)
    mtime = path.stat().st_mtime_ns

    result = prune(path, mode="report")

    assert (result.status, result.reason) == ("ok", "")
    assert result.eligible == 300  # recent obsolete rows are inside the grace period
    assert result.deleted == 0 and result.batches == 0
    assert result.freelist_after == result.freelist_before
    assert digest(path) == before and path.stat().st_mtime_ns == mtime
    assert not Path(f"{path}-journal").exists()


def test_report_mode_is_bounded_by_the_examination_limit(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300, current=0)
    result = prune(path, mode="report", limits=limits(max_examined=120, batch_rows=50))
    assert (result.status, result.reason) == ("partial", "max_examined")
    assert result.examined == 120 and result.eligible == 120


# --- on mode: what is pruned and what is preserved -------------------------


def test_on_mode_prunes_only_obsolete_rows_past_the_grace_period(tmp_path, pool):
    path = build(
        tmp_path / "cache.sqlite", pool, obsolete=300, current=200, recent_obsolete=40, older=60
    )
    current_before = current_rows(path)
    size_before = path.stat().st_size

    result = prune(path, limits=limits(batch_rows=64))

    assert (result.status, result.reason) == ("ok", "")
    assert result.deleted == result.eligible == 360
    # The index range holds 100 old current rows, 60 + 300 obsolete rows:
    # 460 examined in windows of 64, and only windows with an obsolete row
    # write (the first window is all current rows).
    assert result.examined == 460
    assert result.batches == 7
    assert versions(path) == {OBSOLETE: 40, CURRENT: 200}
    # Every current-version row, including ones untouched for 25 days, is
    # byte-for-byte unchanged (no timestamp rewrite either).
    assert current_rows(path) == current_before
    assert integrity(path) == "ok"
    assert not Path(f"{path}-journal").exists()
    # Deleting rows frees pages for reuse; it never shrinks the file.
    assert path.stat().st_size == size_before
    assert result.freelist_after > result.freelist_before
    assert result.reusable_bytes == result.freelist_after * result.page_size


def test_current_version_is_never_deleted_even_with_zero_grace(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=50, current=80)
    future = NOW + timedelta(days=400)
    result = prune(path, now=future, limits=limits(grace=timedelta(0)))
    assert result.status == "ok"
    assert versions(path) == {CURRENT: 80}


def test_many_rows_sharing_one_timestamp_are_each_handled_once(tmp_path, pool):
    # The deployed lookup stamps every hit of a run with one timestamp, so
    # ties far larger than a batch are the normal case.
    path = tmp_path / "cache.sqlite"
    stamp = NOW - timedelta(days=10)
    write(path, pool, OBSOLETE, range(0, 500, 2), stamp)
    write(path, pool, CURRENT, range(1, 500, 2), stamp)
    result = prune(path, limits=limits(batch_rows=7))
    assert result.examined == 500
    assert result.deleted == result.eligible == 250
    assert versions(path) == {CURRENT: 250}


def test_cache_hits_are_identical_after_pruning(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300, current=200)
    keys = [fp(CURRENT, i) for i in range(200)]
    with AnalysisCache(path, cache_version=CURRENT) as cache:
        before = cache.lookup_many(keys, accessed_at=NOW).artifacts
    prune(path)
    with AnalysisCache(path, cache_version=CURRENT) as cache:
        after = cache.lookup_many(keys, accessed_at=NOW).artifacts
    assert len(before) == 200
    assert after == before


def test_freed_pages_are_reused_by_later_cache_writes(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=400, current=100)
    result = prune(path)
    assert result.deleted == 400
    pages_before = hk.read_header(path)["page_count"]
    write(path, pool, CURRENT, range(1000, 1300), NOW)
    header = hk.read_header(path)
    assert header["page_count"] == pages_before  # new rows fit in freed pages
    assert header["freelist_count"] < result.freelist_after


def test_warm_hosted_replay_after_pruning_hits_every_row(tmp_path, pool, monkeypatch):
    """End to end: prune with the real version, then the real hosted analyzer."""

    from test_hosted_analysis_cache import (  # noqa: PLC0415 - reuse fixtures
        _replay,
        _rows,
        _serialized,
        _snapshot,
        _uncached_jobs,
    )
    from watcher.config import load_watchlist  # noqa: PLC0415
    from app.hosted.catalog import CompanyCatalog  # noqa: PLC0415

    config = load_watchlist()
    names = [c.name for c in CompanyCatalog.from_watcher_config(config).companies if c.selectable][:2]
    rows = _rows(names)
    path = tmp_path / "volume" / "analysis-cache.sqlite"
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    snapshot = _snapshot(tmp_path, config, copy.deepcopy(rows))
    _replay(snapshot)  # cold: writes current-version entries
    write(path, pool, OBSOLETE, range(200), NOW - timedelta(days=19))
    monkeypatch.setenv(hk.MODE_ENV, "on")

    result = hk.run_for_collection(HostedTiming(), now=NOW)
    assert (result.status, result.deleted) == ("ok", 200)
    assert set(versions(path)) == {hosted_cache.hosted_static_cache_version()}

    timing = HostedTiming()
    jobs = _replay(snapshot, timing).jobs
    cache_line = next(line for line in timing.lines(exit_code=0) if "kind=cache" in line)
    assert " misses=0 " in cache_line and " invalid=0 " in cache_line
    assert _serialized(jobs) == _uncached_jobs(rows)


# --- bounds ----------------------------------------------------------------


def test_per_run_row_limit_then_later_runs_finish_the_work(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300, current=100)
    first = prune(path, limits=limits(batch_rows=40, max_rows=100))
    assert (first.status, first.reason, first.deleted) == ("partial", "max_rows", 100)
    # Windows: 40 old current rows (no write), then 30 + 40 + 30 obsolete
    # deletions; the last window shrinks so the limit is never exceeded.
    assert first.batches == 3
    assert versions(path)[OBSOLETE] == 200
    second = prune(path, limits=limits(batch_rows=40, max_rows=100))
    third = prune(path, limits=limits(batch_rows=40, max_rows=100))
    fourth = prune(path, limits=limits(batch_rows=40, max_rows=100))
    assert [second.deleted, third.deleted, fourth.deleted] == [100, 100, 0]
    assert (fourth.status, fourth.reason) == ("ok", "")
    assert versions(path) == {CURRENT: 100}


def test_time_budget_stops_between_batches(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300)
    ticks = iter([0.0, 0.0, 1.0, 2.0, 100.0, 100.0, 100.0, 100.0])
    result = prune(
        path,
        limits=limits(batch_rows=50, time_budget_seconds=10.0),
        clock=lambda: next(ticks),
    )
    assert (result.status, result.reason) == ("partial", "time_budget")
    assert result.deleted % 50 == 0 and 0 < result.deleted < 300
    assert versions(path)[OBSOLETE] == 300 - result.deleted
    assert integrity(path) == "ok"


def test_each_batch_journal_is_measured_and_bounded(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300)
    result = prune(path, limits=limits(batch_rows=100))
    assert result.status == "ok"
    assert 0 < result.journal_peak_bytes <= hk.DEFAULT_LIMITS.max_journal_bytes


def test_a_batch_whose_journal_exceeds_the_cap_is_rolled_back(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300, current=100)
    before = versions(path)
    result = prune(path, limits=limits(batch_rows=100, max_journal_bytes=1))
    assert (result.status, result.reason) == ("partial", "journal_cap")
    assert result.deleted == 0 and result.batches == 0
    assert versions(path) == before
    assert integrity(path) == "ok"
    assert not Path(f"{path}-journal").exists()


def test_low_disk_skips_before_any_write(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    before = digest(path)
    result = prune(path, disk_free=lambda _path: 10 * 2**20)
    assert (result.status, result.reason) == ("skipped", "low_disk")
    assert result.deleted == 0
    assert digest(path) == before


def test_disk_running_low_mid_run_stops_after_committed_batches(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300)
    free = iter([50 * 2**30, 50 * 2**30, 1, 1, 1])
    result = prune(path, limits=limits(batch_rows=100), disk_free=lambda _p: next(free))
    assert (result.status, result.reason) == ("partial", "low_disk")
    assert result.deleted == 200
    assert versions(path)[OBSOLETE] == 100


# --- failures never propagate; partial failures resume ---------------------


def test_failure_in_a_batch_keeps_committed_batches_and_rolls_back_the_rest(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300, current=100)
    calls = []

    def fail_third(batch_number):
        calls.append(batch_number)
        if batch_number == 3:
            raise sqlite3.OperationalError("disk I/O error")

    result = prune(path, limits=limits(batch_rows=50), before_commit=fail_third)
    assert (result.status, result.reason) == ("failed", "operationalerror")
    assert result.deleted == 100 and result.batches == 2
    assert versions(path) == {OBSOLETE: 200, CURRENT: 100}
    assert integrity(path) == "ok"
    assert not Path(f"{path}-journal").exists()

    retry = prune(path, limits=limits(batch_rows=50))
    assert (retry.status, retry.deleted) == ("ok", 200)
    assert versions(path) == {CURRENT: 100}


def test_unexpected_exception_is_contained(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)

    def explode(_batch_number):
        raise RuntimeError("boom")

    result = prune(path, before_commit=explode)
    assert (result.status, result.reason) == ("failed", "runtimeerror")
    assert integrity(path) == "ok"


CRASH_SCRIPT = r"""
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
# A one-page cache forces SQLite to sync the journal and write changed pages
# into the database file mid-transaction, so the crash leaves a genuinely hot
# journal and a half-modified database file.
connection.execute("pragma cache_size = 1")
connection.execute("begin immediate")
connection.execute("delete from analysis_cache where cache_version = ?", (int(sys.argv[2]),))
os._exit(9)  # crash with an uncommitted delete and a hot journal
"""


def test_crash_mid_batch_leaves_a_hot_journal_that_the_next_run_recovers(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=300, current=100)
    before = versions(path)
    completed = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(path), str(OBSOLETE)], check=False
    )
    assert completed.returncode == 9
    assert hot_journal(path)

    # Report mode opens the file read-only, so it cannot roll the hot journal
    # back: it fails cleanly and leaves both files exactly as they were.
    crashed = (digest(path), Path(f"{path}-journal").read_bytes())
    report = prune(path, mode="report")
    assert report.status == "failed" and report.deleted == 0
    assert hot_journal(path)
    assert (digest(path), Path(f"{path}-journal").read_bytes()) == crashed

    # On mode opens read-write: SQLite rolls the crashed transaction back
    # first, restoring every row, and pruning then proceeds normally.
    result = prune(path)
    assert (result.status, result.deleted) == ("ok", before[OBSOLETE])
    assert versions(path) == {CURRENT: 100}
    assert integrity(path) == "ok"
    assert not Path(f"{path}-journal").exists()


def test_a_held_write_lock_skips_quickly(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("begin immediate")
    try:
        result = prune(path, limits=limits(busy_timeout_seconds=0.05))
    finally:
        holder.execute("rollback")
        holder.close()
    assert (result.status, result.reason) == ("skipped", "locked")
    assert result.deleted == 0
    assert result.seconds < 5


@pytest.mark.parametrize("mode", ["report", "on"])
def test_missing_file_is_skipped_and_never_created(tmp_path, mode):
    path = tmp_path / "absent" / "cache.sqlite"
    result = prune(path, mode=mode)
    assert (result.status, result.reason) == ("skipped", "cache_file_missing")
    assert not path.exists() and not path.parent.exists()


def test_unavailable_code_version_is_skipped(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    before = digest(path)
    result = prune(path, current_version=None)
    assert (result.status, result.reason) == ("skipped", "version_unavailable")
    assert digest(path) == before


def test_unexpected_schema_is_skipped(tmp_path):
    path = tmp_path / "cache.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("create table analysis_cache(fingerprint text primary key, blob text)")
    result = prune(path)
    assert (result.status, result.reason) == ("skipped", "schema_mismatch")


def test_missing_access_index_is_skipped(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    with sqlite3.connect(path) as connection:
        connection.execute("drop index analysis_cache_last_accessed_idx")
    before = versions(path)
    result = prune(path)
    assert (result.status, result.reason) == ("skipped", "index_missing")
    assert versions(path) == before


def test_corrupt_file_fails_without_raising(tmp_path):
    path = tmp_path / "cache.sqlite"
    path.write_bytes(b"not a sqlite database" * 1000)
    before = digest(path)
    for mode in ("report", "on"):
        result = prune(path, mode=mode)
        assert result.status == "failed"
    assert digest(path) == before


def test_collection_wrapper_never_raises(monkeypatch, tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(hk.MODE_ENV, "on")

    def explode(*_args, **_kwargs):
        raise MemoryError

    monkeypatch.setattr(hk, "run_cache_housekeeping", explode)
    result = hk.run_for_collection(HostedTiming(), now=NOW)
    assert (result.status, result.reason) == ("failed", "memoryerror")


def test_collection_wrapper_uses_the_analyzer_cache_version(monkeypatch, tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=30, current=20)
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(hk.MODE_ENV, "on")
    monkeypatch.setattr(hosted_cache, "hosted_static_cache_version", lambda: CURRENT)
    result = hk.run_for_collection(HostedTiming(), now=NOW)
    assert result.deleted == 30
    assert versions(path) == {CURRENT: 20}


def test_collection_wrapper_skips_without_a_cache_path(monkeypatch):
    monkeypatch.setenv(hk.MODE_ENV, "on")
    result = hk.run_for_collection(HostedTiming(), now=NOW)
    assert (result.status, result.reason) == ("skipped", "cache_path_unset")


# --- telemetry -------------------------------------------------------------


def test_housekeeping_line_is_bounded_tokens_and_stage_comes_first(tmp_path, pool, monkeypatch):
    path = build(tmp_path / "cache.sqlite", pool, obsolete=120)
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(hk.MODE_ENV, "on")
    monkeypatch.setattr(hosted_cache, "hosted_static_cache_version", lambda: CURRENT)
    timing = HostedTiming()
    hk.run_for_collection(timing, now=NOW)
    lines = timing.lines(exit_code=0)
    line = next(line for line in lines if "kind=housekeeping" in line)
    assert LINE_RE.match(line), line
    fields = dict(part.split("=", 1) for part in line.split()[1:])
    assert fields["cache"] == "analysis" and fields["mode"] == "on"
    assert fields["status"] == "ok" and fields["reason"] == "none"
    assert fields["deleted"] == "120" and fields["eligible"] == "120"
    for key in ("examined", "batches", "seconds", "page_size", "pages", "freelist_before",
                "freelist_after", "reusable_mb", "journal_peak_kb", "disk_free_mb"):
        assert key in fields
    assert STAGE_ORDER[0] == "cache_housekeeping"
    assert any(f"{PREFIX} kind=stage stage=cache_housekeeping " in l for l in lines)
    assert str(path) not in line and str(CURRENT) not in line


def test_read_header_matches_sqlite_pragmas(tmp_path, pool):
    path = build(tmp_path / "cache.sqlite", pool)
    prune(path)
    with sqlite3.connect(path) as connection:
        expected = {
            "page_size": connection.execute("pragma page_size").fetchone()[0],
            "page_count": connection.execute("pragma page_count").fetchone()[0],
            "freelist_count": connection.execute("pragma freelist_count").fetchone()[0],
        }
    assert hk.read_header(path) == expected


def test_grace_cutoff_uses_the_deployed_timestamp_format(tmp_path, pool):
    path = tmp_path / "cache.sqlite"
    edge = NOW - hk.DEFAULT_LIMITS.grace
    write(path, pool, OBSOLETE, range(10), edge)  # exactly at the cutoff: kept
    write(path, pool, OBSOLETE, range(10, 20), edge - timedelta(seconds=1))
    result = prune(path)
    assert result.deleted == 10
    with sqlite3.connect(path) as connection:
        kept = {row[0] for row in connection.execute("select last_accessed_at from analysis_cache")}
    assert kept == {iso_utc(edge)}


def test_module_is_not_part_of_the_cache_fingerprint():
    assert "app.hosted.cache_housekeeping" not in hosted_cache.STATIC_ANALYSIS_MODULES
    assert "backend.app.hosted.cache_housekeeping" not in hosted_cache.STATIC_ANALYSIS_MODULES


# --- entrypoint integration ------------------------------------------------

from test_hosted_timing import hosted_entrypoint  # noqa: E402,F401 - fixture


def test_entrypoint_prunes_before_collection(hosted_entrypoint, monkeypatch, tmp_path, pool, capsys):
    from app.hosted import collect_and_import  # noqa: PLC0415

    path = build(tmp_path / "cache.sqlite", pool, obsolete=50, current=20)
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(hk.MODE_ENV, "on")
    monkeypatch.setattr(hosted_cache, "hosted_static_cache_version", lambda: CURRENT)
    order = []
    real_prune, real_collect = hk.run_cache_housekeeping, collect_and_import.collect_batch

    def tracked_prune(*args, **kwargs):
        order.append("prune")
        return real_prune(*args, **kwargs)

    def tracked_collect(*args, **kwargs):
        order.append("collect")
        return real_collect(*args, **kwargs)

    monkeypatch.setattr(hk, "run_cache_housekeeping", tracked_prune)
    monkeypatch.setattr(collect_and_import, "collect_batch", tracked_collect)

    assert collect_and_import.main([]) == 0
    out = capsys.readouterr().out
    assert order == ["prune", "collect"]
    assert versions(path) == {CURRENT: 20}
    assert out.splitlines()[0].startswith("HOSTED-COLLECTION ")
    assert " kind=housekeeping cache=analysis mode=on status=ok " in out


@pytest.mark.parametrize("error", [RuntimeError, MemoryError, sqlite3.DatabaseError])
def test_entrypoint_housekeeping_failure_never_changes_the_run(
    hosted_entrypoint, monkeypatch, tmp_path, pool, capsys, error
):
    from app.hosted import collect_and_import  # noqa: PLC0415
    from watcher.collection_snapshot import save_collection_snapshot  # noqa: PLC0415

    path = build(tmp_path / "cache.sqlite", pool, obsolete=50, current=20)
    before = versions(path)
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    monkeypatch.setenv(hk.MODE_ENV, "on")

    def broken(*_args, **_kwargs):
        raise error("housekeeping broke")

    monkeypatch.setattr(hk, "run_cache_housekeeping", broken)
    assert collect_and_import.main([]) == 0
    out = capsys.readouterr().out
    assert versions(path) == before
    assert f"status=failed reason={error.__name__.lower()}" in out
    reference = tmp_path / "reference.json.gz"
    save_collection_snapshot(hosted_entrypoint["batch"], reference)
    assert hosted_entrypoint["snapshot_bytes"] == reference.read_bytes()


def test_entrypoint_off_by_default_prints_nothing_new(hosted_entrypoint, monkeypatch, tmp_path, pool, capsys):
    from app.hosted import collect_and_import  # noqa: PLC0415

    path = build(tmp_path / "cache.sqlite", pool)
    before = digest(path)
    monkeypatch.setenv(hosted_cache.CACHE_PATH_ENV, str(path))
    assert collect_and_import.main([]) == 0
    out = capsys.readouterr().out
    assert "housekeeping" not in out
    assert digest(path) == before
