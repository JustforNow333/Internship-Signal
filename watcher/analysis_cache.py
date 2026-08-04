"""Persistent watcher cache for date-independent backend job analysis."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.app import config as backend_config
from backend.app.dedupe import analyzed_job_ids
from backend.app.ingest import (
    analyze_static_row,
    assemble_scored_job,
    deduplicate_rows,
    sort_scored_jobs,
    static_analysis_artifact_is_valid,
)
from backend.app.eligibility import student_eligibility_fingerprint_inputs
from backend.app.profile import load_profile
from backend.app.signals import build_profile_skill_matcher
from watcher.source_health import iso_utc, utc_datetime

LOGGER = logging.getLogger(__name__)

# Increment this whenever static classification, compensation parsing, signal
# detection, profile matching, technology detection, student eligibility,
# date-independent score categories, fingerprint inputs, or the artifact
# schema changes. Deadline and final-decision logic are intentionally dynamic.
STATIC_ANALYSIS_CACHE_VERSION = 5
ANALYSIS_CACHE_RETENTION_DAYS = 30
_SQLITE_BATCH_SIZE = 500
_STATIC_ROW_FIELDS = (
    "company",
    "title",
    "description",
    "requirements",
    "compensation",
    "location",
    "remote_status",
)


@dataclass(frozen=True)
class CacheLookupResult:
    artifacts: dict[str, dict]
    invalid: int = 0
    failed: bool = False


@dataclass(frozen=True)
class AnalysisCacheStats:
    enabled: bool
    rows: int
    hits: int
    misses: int
    invalid: int
    writes: int
    lookup_seconds: float
    static_analysis_seconds: float
    scoring_seconds: float

    @property
    def hit_rate(self) -> float:
        return self.hits / self.rows if self.rows else 0.0


@dataclass(frozen=True)
class CachedAnalysisResult:
    jobs: list[dict]
    duplicate_report: list
    stats: AnalysisCacheStats


class StaticAnalysisFingerprintBuilder:
    """Build SHA-256 keys from deterministic static-analysis input JSON."""

    def __init__(
        self,
        *,
        profile: Mapping[str, object],
        known: Mapping[str, object],
        cache_version: int = STATIC_ANALYSIS_CACHE_VERSION,
    ) -> None:
        profile_json = _stable_json_bytes(profile)
        known_json = _stable_json_bytes(known)
        version_json = _stable_json_bytes(cache_version)
        prefix = (
            b'{"cache_version":'
            + version_json
            + b',"known_companies":'
            + known_json
            + b',"profile":'
            + profile_json
            + b',"row":'
        )
        self._base_hash = hashlib.sha256(prefix)

    def fingerprint(self, row: Mapping[str, object]) -> str:
        row_payload = {
            field: row.get(field)
            for field in _STATIC_ROW_FIELDS
        }
        row_payload["structured_eligibility"] = (
            student_eligibility_fingerprint_inputs(row)
        )
        digest = self._base_hash.copy()
        digest.update(_stable_json_bytes(row_payload))
        digest.update(b"}")
        return digest.hexdigest()


def static_analysis_fingerprint(
    row: Mapping[str, object],
    *,
    profile: Mapping[str, object],
    known: Mapping[str, object],
    cache_version: int = STATIC_ANALYSIS_CACHE_VERSION,
) -> str:
    """Return the stable SHA-256 fingerprint for one deduplicated row."""

    return StaticAnalysisFingerprintBuilder(
        profile=profile,
        known=known,
        cache_version=cache_version,
    ).fingerprint(row)


class AnalysisCache:
    """One-connection batch interface for the watcher-owned cache table."""

    def __init__(
        self,
        path: str | Path,
        *,
        cache_version: int = STATIC_ANALYSIS_CACHE_VERSION,
    ) -> None:
        self.path = Path(path)
        self.cache_version = int(cache_version)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def __enter__(self) -> "AnalysisCache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def lookup_many(
        self,
        fingerprints: Sequence[str],
        *,
        accessed_at: datetime | None = None,
    ) -> CacheLookupResult:
        keys = tuple(dict.fromkeys(str(value) for value in fingerprints))
        if not keys:
            return CacheLookupResult({})
        timestamp = iso_utc(accessed_at or datetime.now(timezone.utc))
        artifacts: dict[str, dict] = {}
        invalid = 0
        try:
            for batch in _batches(keys, _SQLITE_BATCH_SIZE):
                placeholders = ",".join("?" for _ in batch)
                rows = self._conn.execute(
                    f"""
                    select fingerprint, artifact_json
                    from analysis_cache
                    where cache_version = ?
                      and fingerprint in ({placeholders})
                    """,
                    (self.cache_version, *batch),
                ).fetchall()
                for row in rows:
                    try:
                        artifact = json.loads(row["artifact_json"])
                    except (json.JSONDecodeError, TypeError, ValueError):
                        invalid += 1
                        continue
                    if not static_analysis_artifact_is_valid(artifact):
                        invalid += 1
                        continue
                    artifacts[row["fingerprint"]] = artifact
            if artifacts:
                with self._conn:
                    self._conn.executemany(
                        """
                        update analysis_cache
                        set last_accessed_at = ?
                        where fingerprint = ? and cache_version = ?
                        """,
                        (
                            (timestamp, fingerprint, self.cache_version)
                            for fingerprint in artifacts
                        ),
                    )
        except sqlite3.Error as exc:
            _warn_cache_failure("lookup", exc)
            return CacheLookupResult({}, failed=True)
        if invalid:
            LOGGER.warning(
                "Analysis cache ignored %d invalid artifact(s); "
                "fresh analysis will replace them.",
                invalid,
            )
        return CacheLookupResult(artifacts, invalid=invalid)

    def store_many(
        self,
        artifacts: Mapping[str, Mapping[str, object]],
        *,
        accessed_at: datetime | None = None,
    ) -> int:
        if not artifacts:
            return 0
        timestamp = iso_utc(accessed_at or datetime.now(timezone.utc))
        records = []
        invalid = 0
        for fingerprint, artifact in artifacts.items():
            if not static_analysis_artifact_is_valid(artifact):
                invalid += 1
                continue
            try:
                artifact_json = json.dumps(
                    artifact,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError):
                invalid += 1
                continue
            records.append(
                (
                    fingerprint,
                    self.cache_version,
                    artifact_json,
                    timestamp,
                    timestamp,
                )
            )
        if invalid:
            LOGGER.warning(
                "Analysis cache skipped %d invalid artifact write(s).",
                invalid,
            )
        if not records:
            return 0
        try:
            with self._conn:
                self._conn.executemany(
                    """
                    insert into analysis_cache(
                      fingerprint, cache_version, artifact_json,
                      created_at, last_accessed_at
                    )
                    values (?, ?, ?, ?, ?)
                    on conflict(fingerprint) do update set
                      cache_version = excluded.cache_version,
                      artifact_json = excluded.artifact_json,
                      last_accessed_at = excluded.last_accessed_at
                    """,
                    records,
                )
        except sqlite3.Error as exc:
            _warn_cache_failure("write", exc)
            return 0
        return len(records)

    def cleanup_expired(
        self,
        *,
        accessed_at: datetime | None = None,
        retention_days: int = ANALYSIS_CACHE_RETENTION_DAYS,
    ) -> int:
        now = utc_datetime(accessed_at or datetime.now(timezone.utc))
        cutoff = iso_utc(now - timedelta(days=retention_days))
        try:
            with self._conn:
                cursor = self._conn.execute(
                    """
                    delete from analysis_cache
                    where last_accessed_at < ?
                    """,
                    (cutoff,),
                )
            return max(0, int(cursor.rowcount))
        except sqlite3.Error as exc:
            _warn_cache_failure("retention cleanup", exc)
            return 0

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                create table if not exists analysis_cache(
                  fingerprint text primary key,
                  cache_version integer not null,
                  artifact_json text not null,
                  created_at text not null,
                  last_accessed_at text not null
                )
                """
            )
            self._conn.execute(
                """
                create index if not exists analysis_cache_last_accessed_idx
                on analysis_cache(last_accessed_at)
                """
            )


def analyze_rows_with_cache(
    rows: list[dict],
    *,
    db_path: str | Path,
    enabled: bool = True,
    today: date | None = None,
    include_audit_diagnostics: bool = False,
    profile: Mapping[str, object] | None = None,
    known: Mapping[str, object] | None = None,
    cache_version: int = STATIC_ANALYSIS_CACHE_VERSION,
    cache_factory: Callable[..., AnalysisCache] = AnalysisCache,
    accessed_at: datetime | None = None,
) -> CachedAnalysisResult:
    """Run dedupe → cache/static analysis → current scoring → sort."""

    effective_profile = profile if profile is not None else load_profile()
    effective_known = (
        known
        if known is not None
        else backend_config.load_known_companies()
    )
    effective_accessed_at = utc_datetime(
        accessed_at or datetime.now(timezone.utc)
    )
    unique_rows, duplicate_report = deduplicate_rows(
        rows,
        include_audit_diagnostics=include_audit_diagnostics,
    )

    fingerprint_failures = 0
    try:
        fingerprint_builder = StaticAnalysisFingerprintBuilder(
            profile=effective_profile,
            known=effective_known,
            cache_version=cache_version,
        )
    except (TypeError, ValueError):
        fingerprint_builder = None
        fingerprint_failures = len(unique_rows)
        LOGGER.warning(
            "Analysis cache fingerprint configuration was invalid; "
            "using fresh analysis for this run."
        )

    fingerprints: list[str | None] = []
    for row in unique_rows:
        if fingerprint_builder is None:
            fingerprints.append(None)
            continue
        try:
            fingerprints.append(fingerprint_builder.fingerprint(row))
        except (TypeError, ValueError):
            fingerprint_failures += 1
            fingerprints.append(None)
    if fingerprint_failures and fingerprint_builder is not None:
        LOGGER.warning(
            "Analysis cache could not fingerprint %d row(s); "
            "using fresh analysis for those rows.",
            fingerprint_failures,
        )

    cached_artifacts: dict[str, dict] = {}
    invalid = fingerprint_failures
    lookup_seconds = 0.0
    cache: AnalysisCache | None = None
    if enabled:
        lookup_started = time.perf_counter()
        try:
            cache = cache_factory(
                db_path,
                cache_version=cache_version,
            )
            lookup = cache.lookup_many(
                [
                    fingerprint
                    for fingerprint in fingerprints
                    if fingerprint is not None
                ],
                accessed_at=effective_accessed_at,
            )
            cached_artifacts = lookup.artifacts
            invalid += lookup.invalid
        except Exception as exc:
            _warn_cache_failure("initialization", exc)
            if cache is not None:
                try:
                    cache.close()
                except Exception:
                    pass
            cache = None
        finally:
            lookup_seconds = time.perf_counter() - lookup_started

    hits = sum(
        1
        for fingerprint in fingerprints
        if fingerprint is not None
        and fingerprint in cached_artifacts
    )
    misses = len(unique_rows) - hits

    generated_artifacts: dict[str, dict] = {}
    row_artifacts: list[dict] = []
    profile_skill_matcher = (
        build_profile_skill_matcher(effective_profile)
        if misses
        else None
    )
    static_started = time.perf_counter()
    for row, fingerprint in zip(unique_rows, fingerprints):
        artifact = (
            cached_artifacts.get(fingerprint)
            if fingerprint is not None
            else None
        )
        if artifact is None:
            artifact = analyze_static_row(
                row,
                profile=effective_profile,
                known=effective_known,
                profile_skill_matcher=profile_skill_matcher,
            )
            if fingerprint is not None:
                generated_artifacts[fingerprint] = artifact
        row_artifacts.append(artifact)
    static_analysis_seconds = time.perf_counter() - static_started

    writes = 0
    if cache is not None:
        try:
            writes = cache.store_many(
                generated_artifacts,
                accessed_at=effective_accessed_at,
            )
            cache.cleanup_expired(
                accessed_at=effective_accessed_at,
                retention_days=ANALYSIS_CACHE_RETENTION_DAYS,
            )
        except Exception as exc:
            _warn_cache_failure("maintenance", exc)
        finally:
            try:
                cache.close()
            except Exception as exc:
                _warn_cache_failure("close", exc)

    scoring_started = time.perf_counter()
    resolved_job_ids = analyzed_job_ids(unique_rows)
    jobs = [
        assemble_scored_job(
            row,
            artifact,
            profile=effective_profile,
            today=today,
            analyzed_job_id=resolved_job_id,
        )
        for row, artifact, resolved_job_id in zip(
            unique_rows,
            row_artifacts,
            resolved_job_ids,
        )
    ]
    sort_scored_jobs(jobs)
    scoring_seconds = time.perf_counter() - scoring_started

    stats = AnalysisCacheStats(
        enabled=bool(enabled),
        rows=len(unique_rows),
        hits=hits,
        misses=misses,
        invalid=invalid,
        writes=writes,
        lookup_seconds=lookup_seconds,
        static_analysis_seconds=static_analysis_seconds,
        scoring_seconds=scoring_seconds,
    )
    LOGGER.info(
        "ANALYSIS-CACHE enabled=%s rows=%d hits=%d misses=%d invalid=%d "
        "writes=%d hit_rate=%.3f lookup_seconds=%.3f "
        "static_analysis_seconds=%.3f scoring_seconds=%.3f",
        "true" if stats.enabled else "false",
        stats.rows,
        stats.hits,
        stats.misses,
        stats.invalid,
        stats.writes,
        stats.hit_rate,
        stats.lookup_seconds,
        stats.static_analysis_seconds,
        stats.scoring_seconds,
    )
    return CachedAnalysisResult(
        jobs=jobs,
        duplicate_report=duplicate_report,
        stats=stats,
    )


def _stable_json_bytes(value: object) -> bytes:
    return json.dumps(
        _stable_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _stable_json_value(value: object) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _stable_json_value(nested)
            for key, nested in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_stable_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_stable_json_value(item) for item in value]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
        )
    raise TypeError(
        f"Unsupported fingerprint value type: {type(value).__name__}"
    )


def _batches(values: Sequence[str], size: int):
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _warn_cache_failure(operation: str, exc: Exception) -> None:
    LOGGER.warning(
        "Analysis cache %s failed (%s); continuing with fresh analysis.",
        operation,
        type(exc).__name__,
    )
