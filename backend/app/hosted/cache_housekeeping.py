"""Bounded pruning of obsolete hosted analysis-cache versions (C1).

The hosted cache key embeds a version derived from the static-analysis source
(``analysis_cache.hosted_static_cache_version``), so every deploy that changes
that code orphans all existing rows: lookups filter on the current version and
never read them again, and the watcher-owned 30-day retention only removes
them a month later. This module deletes those orphaned rows early, in small
committed batches, before collection starts (the process's lowest-memory
point, long before the analysis opens the cache).

``HOSTED_ANALYSIS_CACHE_PRUNE`` selects the mode:

* ``off`` (default, also for blank or unknown values): never opens the file.
* ``report``: opens the file read-only and counts what ``on`` would delete.
  It cannot write; if a crashed writer left a hot journal it reports a failure
  and leaves the recovery to the next read-write opener.
* ``on``: deletes eligible rows.

A row is eligible only if its ``cache_version`` differs from the current code
version and it was last accessed before the grace period, so an immediate
rollback still finds its previous version warm. Current-version rows are never
deleted or modified, whatever their age (the deployed retention still owns
them). Deleting rows returns pages to SQLite's freelist for reuse by later
cache writes; it never shrinks the file (``auto_vacuum`` is off), which is the
separate C2 compaction proposal.

Every limit is checked between batches: rows per batch, rows per run, index
entries examined, wall time, free disk, and each batch's rollback-journal size
(a batch whose journal exceeds the cap is rolled back, not committed). Nothing
here ever raises into the collection: every failure becomes a bounded status
and reason, committed batches stay committed, the failing batch rolls back,
and the next run resumes where this one stopped.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from watcher.source_health import iso_utc

from . import analysis_cache as hosted_cache
from .timing import HostedTiming

MODE_ENV = "HOSTED_ANALYSIS_CACHE_PRUNE"
MODES = ("off", "report", "on")
TABLE = "analysis_cache"
ACCESS_INDEX = "analysis_cache_last_accessed_idx"
EXPECTED_COLUMNS = (
    "fingerprint",
    "cache_version",
    "artifact_json",
    "created_at",
    "last_accessed_at",
)
MIB = 2**20


@dataclass(frozen=True)
class PruneLimits:
    # Index entries examined per window; also the largest delete batch.
    batch_rows: int = 500
    # Rows deleted per run. Production keeps ~126k rows per version, so one
    # obsolete version clears in about five two-hourly runs.
    max_rows: int = 25_000
    # Index entries examined per run, bounding read I/O in either mode.
    max_examined: int = 250_000
    time_budget_seconds: float = 60.0
    # Obsolete rows accessed within this window are kept, so rolling back to
    # the previous deployment soon after a deploy still starts warm.
    grace: timedelta = timedelta(hours=48)
    # A batch starts only if this much disk stays free beyond the journal
    # cap, so pruning can never consume the headroom the deployed lookup's
    # rollback journal needs later in the run.
    min_free_bytes: int = 128 * MIB
    max_journal_bytes: int = 32 * MIB
    busy_timeout_seconds: float = 1.0


DEFAULT_LIMITS = PruneLimits()


@dataclass
class HousekeepingResult:
    mode: str
    status: str = "ok"  # disabled, skipped, ok, partial, failed
    reason: str = ""
    examined: int = 0
    eligible: int = 0
    deleted: int = 0
    batches: int = 0
    seconds: float = 0.0
    page_size: int = 0
    page_count: int = 0
    freelist_before: int = 0
    freelist_after: int = 0
    journal_peak_bytes: int = 0
    disk_free_bytes: int | None = None

    @property
    def reusable_bytes(self) -> int:
        return self.freelist_after * self.page_size

    def stop(self, status: str, reason: str) -> None:
        self.status, self.reason = status, reason


def housekeeping_mode(value: str | None = None) -> tuple[str, str]:
    """Return ``(mode, reason)``; anything unrecognised means ``off``."""

    raw = os.getenv(MODE_ENV, "") if value is None else value
    mode = raw.strip().lower()
    if not mode:
        return "off", ""
    if mode in MODES:
        return mode, ""
    return "off", "invalid_mode"


def read_header(path: str | Path) -> dict[str, int]:
    """Page size, page count, and freelist count from the database header."""

    with Path(path).open("rb") as stream:
        header = stream.read(100)
    page_size = int.from_bytes(header[16:18], "big")
    return {
        "page_size": 65536 if page_size == 1 else page_size,
        "page_count": int.from_bytes(header[28:32], "big"),
        "freelist_count": int.from_bytes(header[36:40], "big"),
    }


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path.parent).free


def _reason(exc: BaseException) -> str:
    return type(exc).__name__.lower()


def run_cache_housekeeping(
    path: str | Path,
    *,
    current_version: int | None,
    mode: str,
    now: datetime,
    limits: PruneLimits = DEFAULT_LIMITS,
    disk_free: Callable[[Path], int] = _disk_free,
    clock: Callable[[], float] = time.monotonic,
    before_commit: Callable[[int], None] | None = None,
) -> HousekeepingResult:
    """Run one bounded pass. Never raises; the result explains what happened."""

    result = HousekeepingResult(mode=mode)
    started = clock()
    try:
        _run(Path(path), result, current_version, now, limits, disk_free, clock,
             started, before_commit)
    except Exception as exc:  # noqa: BLE001 - housekeeping must never fail a run
        result.stop("failed", _reason(exc))
    result.seconds = max(0.0, clock() - started)
    return result


def _run(path, result, current_version, now, limits, disk_free, clock, started, before_commit):
    if result.mode not in ("report", "on"):
        result.stop("disabled", "")
        return
    if current_version is None:
        result.stop("skipped", "version_unavailable")
        return
    if not path.is_file():
        result.stop("skipped", "cache_file_missing")
        return
    access = "ro" if result.mode == "report" else "rw"
    # ``mode=ro``/``mode=rw`` never create a file; report mode can never write.
    connection = sqlite3.connect(
        f"file:{quote(str(path))}?mode={access}",
        uri=True,
        timeout=limits.busy_timeout_seconds,
        isolation_level=None,
    )
    try:
        _prune(connection, path, result, int(current_version), now, limits,
               disk_free, clock, started, before_commit)
    finally:
        try:
            result.freelist_after = connection.execute("pragma freelist_count").fetchone()[0]
        except sqlite3.Error:
            result.freelist_after = result.freelist_before
        connection.close()


def _schema_ok(connection: sqlite3.Connection) -> str:
    columns = tuple(row[1] for row in connection.execute(f"pragma table_info({TABLE})"))
    if columns != EXPECTED_COLUMNS:
        return "schema_mismatch"
    index = [row[2] for row in connection.execute(f"pragma index_info({ACCESS_INDEX})")]
    if index != ["last_accessed_at"]:
        return "index_missing"
    return ""


def _prune(connection, path, result, current, now, limits, disk_free, clock, started, before_commit):
    problem = _schema_ok(connection)
    result.page_size = connection.execute("pragma page_size").fetchone()[0]
    result.page_count = connection.execute("pragma page_count").fetchone()[0]
    result.freelist_before = connection.execute("pragma freelist_count").fetchone()[0]
    if problem:
        result.stop("skipped", problem)
        return
    if result.mode == "report":
        result.disk_free_bytes = disk_free(path)
    cutoff = iso_utc(now.astimezone(timezone.utc) - limits.grace)
    journal = Path(f"{path}-journal")
    # A (last_accessed_at, rowid) cursor walks the access index exactly once,
    # even though one run stamps every hit with the same timestamp.
    cursor: tuple[str, int] = ("", -1)
    window_sql = (
        f"select rowid, last_accessed_at, cache_version from {TABLE} "
        f"indexed by {ACCESS_INDEX} "
        "where last_accessed_at < ? and (last_accessed_at, rowid) > (?, ?) "
        "order by last_accessed_at, rowid limit ?"
    )
    while True:
        if clock() - started >= limits.time_budget_seconds:
            result.stop("partial", "time_budget")
            return
        if result.mode == "on" and result.deleted >= limits.max_rows:
            result.stop("partial", "max_rows")
            return
        if result.examined >= limits.max_examined:
            result.stop("partial", "max_examined")
            return
        size = min(limits.batch_rows, limits.max_examined - result.examined)
        if result.mode == "on":
            size = min(size, limits.max_rows - result.deleted)
        window = connection.execute(window_sql, (cutoff, *cursor, size)).fetchall()
        result.examined += len(window)
        candidates = [rowid for rowid, _accessed, version in window if version != current]
        if candidates and result.mode == "report":
            result.eligible += len(candidates)
        elif candidates:
            if not _delete_batch(connection, path, journal, result, candidates, current,
                                 limits, disk_free, before_commit):
                return
        if len(window) < size:
            return  # the eligible index range is exhausted
        cursor = (window[-1][1], window[-1][0])


def _delete_batch(connection, path, journal, result, candidates, current, limits,
                  disk_free, before_commit) -> bool:
    free = disk_free(path)
    result.disk_free_bytes = free
    if free < limits.min_free_bytes + limits.max_journal_bytes:
        result.stop("partial" if result.batches else "skipped", "low_disk")
        return False
    try:
        connection.execute("begin immediate")
    except sqlite3.OperationalError as exc:
        locked = "locked" in str(exc).lower() or "busy" in str(exc).lower()
        reason = "locked" if locked else _reason(exc)
        result.stop("partial" if result.batches else ("skipped" if locked else "failed"), reason)
        return False
    try:
        placeholders = ",".join("?" for _ in candidates)
        deleted = connection.execute(
            f"delete from {TABLE} where rowid in ({placeholders}) and cache_version <> ?",
            (*candidates, current),
        ).rowcount
        try:
            journal_bytes = journal.stat().st_size
        except FileNotFoundError:
            journal_bytes = 0
        result.journal_peak_bytes = max(result.journal_peak_bytes, journal_bytes)
        if journal_bytes > limits.max_journal_bytes:
            connection.execute("rollback")
            result.stop("partial", "journal_cap")
            return False
        if before_commit is not None:
            before_commit(result.batches + 1)
        connection.execute("commit")
    except Exception as exc:  # noqa: BLE001 - roll back only this batch
        try:
            if connection.in_transaction:
                connection.execute("rollback")
        except sqlite3.Error:
            pass
        result.stop("failed", _reason(exc))
        return False
    result.batches += 1
    result.deleted += max(0, deleted)
    result.eligible += max(0, deleted)
    return True


def run_for_collection(
    timing: HostedTiming | None = None,
    *,
    now: datetime | None = None,
) -> HousekeepingResult | None:
    """Collection entry point: read the flag, prune if asked, never raise.

    Returns ``None`` (and records nothing) in the default ``off`` mode.
    """

    try:
        mode, reason = housekeeping_mode()
    except Exception as exc:  # noqa: BLE001
        mode, reason = "off", _reason(exc)
    if mode == "off" and not reason:
        return None
    if reason:
        result = HousekeepingResult(mode=mode, status="skipped", reason=reason)
        _record(timing, result)
        return result
    try:
        if timing is None:
            result = _for_collection(mode, now)
        else:
            with timing.stage("cache_housekeeping"):
                result = _for_collection(mode, now)
    except Exception as exc:  # noqa: BLE001 - housekeeping must never fail a run
        result = HousekeepingResult(mode=mode, status="failed", reason=_reason(exc))
    _record(timing, result)
    return result


def _for_collection(mode: str, now: datetime | None) -> HousekeepingResult:
    path = hosted_cache.hosted_analysis_cache_path()
    if path is None:
        return HousekeepingResult(mode=mode, status="skipped", reason="cache_path_unset")
    return run_cache_housekeeping(
        path,
        current_version=hosted_cache.hosted_static_cache_version(),
        mode=mode,
        now=now or datetime.now(timezone.utc),
    )


def _record(timing: HostedTiming | None, result: HousekeepingResult) -> None:
    if timing is None:
        return
    try:
        timing.record_cache_housekeeping(result)
    except Exception:  # noqa: BLE001 - telemetry must never raise
        pass
