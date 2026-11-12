"""Opt-in persistent static-analysis cache for hosted snapshot replay.

Hosted replay reuses the watcher's existing cache
(``watcher.analysis_cache.analyze_rows_with_cache``) through the analyzer
injection point of ``replay_snapshot_jobs``. The cache is off unless
``HOSTED_ANALYSIS_CACHE_PATH`` names a SQLite file, which in production must
live on a persistent volume or every run starts cold.

Hosted entries are keyed by a version derived automatically from the
static-analysis source code, so deploying changed analysis code can never
reuse stale artifacts even if ``STATIC_ANALYSIS_CACHE_VERSION`` was not bumped.

The cache is strictly fail-open: an unset path, an untrustworthy code
fingerprint, or any cache failure runs the normal uncached ``analyze_rows`` and
the analyzed jobs are identical either way. The cache never touches the
snapshot, its fingerprint, or the hosted database.
"""

from __future__ import annotations

import copy
import hashlib
import importlib
import logging
import os
from collections.abc import Callable
from datetime import date
from pathlib import Path

from backend.app.ingest import STATIC_ANALYSIS_ARTIFACT_SCHEMA_VERSION, analyze_rows
from watcher.analysis_cache import STATIC_ANALYSIS_CACHE_VERSION, analyze_rows_with_cache

from .timing import HostedTiming

LOGGER = logging.getLogger(__name__)

CACHE_PATH_ENV = "HOSTED_ANALYSIS_CACHE_PATH"
# Every module whose source can change a cached static artifact or its key:
# the static analyzers and the configuration/profile they load, the domain
# eligibility and canonical-column definitions they import, and the watcher
# cache module that owns the fingerprinted row fields. Final date-relative
# scoring is recomputed on every run and needs no entry here, but it shares
# these modules, so a change to it merely costs one cold run.
STATIC_ANALYSIS_MODULES = (
    "backend.app.ingest",
    "backend.app.classify",
    "backend.app.signals",
    "backend.app.salary",
    "backend.app.eligibility",
    "backend.app.scoring",
    "backend.app.normalize",
    "backend.app.profile",
    "backend.app.config",
    "backend.app.dedupe",
    "internship_signal.domain.eligibility",
    "internship_signal.domain.jobs",
    "watcher.analysis_cache",
)
_VERSION_DOMAIN = b"hosted-static-analysis-cache\x00"
# Seven bytes keep the version a positive integer inside SQLite's signed
# 64-bit range.
_VERSION_BYTES = 7

Analyzer = Callable[..., list[dict]]
RowsFactory = Callable[[], list[dict]]


def hosted_analysis_cache_path() -> Path | None:
    """Return the configured cache file, or ``None`` when caching is off."""

    value = os.getenv(CACHE_PATH_ENV, "").strip()
    return Path(value) if value else None


def hosted_static_cache_version() -> int | None:
    """Return a cache version fingerprinting the static-analysis code.

    The version covers both manual version constants and the exact source
    bytes of every module in ``STATIC_ANALYSIS_MODULES``. ``None`` means the
    fingerprint cannot be trusted, and the caller must not use the cache.
    """

    try:
        digest = hashlib.sha256(_VERSION_DOMAIN)
        for value in (
            STATIC_ANALYSIS_CACHE_VERSION,
            STATIC_ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        ):
            digest.update(f"{int(value)}\x00".encode("ascii"))
        for name in STATIC_ANALYSIS_MODULES:
            source = _module_source(name)
            digest.update(f"{name}\x00{len(source)}\x00".encode("ascii"))
            digest.update(source)
        return int.from_bytes(digest.digest()[:_VERSION_BYTES], "big")
    except Exception:  # noqa: BLE001 - an unknown fingerprint disables caching
        return None


def _module_source(name: str) -> bytes:
    filename = getattr(importlib.import_module(name), "__file__", None)
    if not filename or not str(filename).endswith(".py"):
        raise ValueError("static analysis module source unavailable")
    return Path(filename).read_bytes()


def build_hosted_analyzer(timing: HostedTiming | None = None) -> Analyzer:
    """Return a ``replay_snapshot_jobs`` analyzer honoring the hosted cache.

    The cached path mutates its input (dedupe), so a part-way cache failure
    must re-analyze pristine rows. ``replay_snapshot_jobs`` passes
    ``fresh_rows``, which rebuilds them from the immutable replayed snapshot
    only when that fallback runs, instead of holding a deep copy of every row
    for the whole analysis. Callers without it keep the pristine-copy fallback.
    """

    def analyze(
        rows: list[dict],
        *,
        today: date | None = None,
        fresh_rows: RowsFactory | None = None,
    ) -> list[dict]:
        path = hosted_analysis_cache_path()
        if path is None:
            return analyze_rows(rows, today=today)
        version = hosted_static_cache_version()
        if version is None:
            return _uncached(
                rows,
                today=today,
                timing=timing,
                reason="fingerprint_unavailable",
            )
        if fresh_rows is None:
            try:
                # Dedupe mutates its input, so keep a pristine copy for the
                # uncached fallback should the cached path fail part-way.
                pristine = copy.deepcopy(rows)
            except Exception:  # noqa: BLE001
                return _uncached(rows, today=today, timing=timing, reason="copy_failed")
            fresh_rows = _constant_rows(pristine)
        try:
            result = analyze_rows_with_cache(
                rows,
                db_path=path,
                today=today,
                cache_version=version,
            )
        except Exception as exc:  # noqa: BLE001 - caching must never fail the import
            error_type = type(exc).__name__
        else:
            _record(timing, stats=result.stats)
            return result.jobs
        # Release the partly mutated rows (the exception and its traceback are
        # already gone) before the fallback analyzes freshly rebuilt rows.
        del rows
        return _uncached(
            fresh_rows(),
            today=today,
            timing=timing,
            reason="cache_failed",
            error_type=error_type,
        )

    # Tells ``replay_snapshot_jobs`` this analyzer accepts ``fresh_rows``.
    analyze.accepts_fresh_rows = True
    return analyze


def _constant_rows(rows: list[dict]) -> RowsFactory:
    def factory() -> list[dict]:
        return rows

    return factory


def _uncached(
    rows: list[dict],
    *,
    today: date | None,
    timing: HostedTiming | None,
    reason: str,
    error_type: str | None = None,
) -> list[dict]:
    LOGGER.warning(
        "Hosted analysis cache unavailable (%s%s); using uncached analysis.",
        reason,
        f": {error_type}" if error_type is not None else "",
    )
    _record(timing, reason=reason)
    return analyze_rows(rows, today=today)


def _record(
    timing: HostedTiming | None,
    *,
    stats: object | None = None,
    reason: str = "",
) -> None:
    if timing is None:
        return
    try:
        timing.record_analysis_cache(stats, reason=reason)
    except Exception:  # noqa: BLE001 - telemetry must never raise
        pass
