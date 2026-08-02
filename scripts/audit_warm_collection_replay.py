#!/usr/bin/env python3
"""Profile the fully warm, network-free collection replay pipeline."""

from __future__ import annotations

import argparse
import cProfile
import functools
import gc
import hashlib
import json
import logging
import pstats
import sys
import tempfile
from collections import defaultdict
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Callable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app import ingest as backend_ingest  # noqa: E402
from watcher import analysis_cache as cache_module  # noqa: E402
from watcher import run as run_module  # noqa: E402
from watcher import source_comparison as comparison_module  # noqa: E402
from watcher.collection_snapshot import load_collection_snapshot  # noqa: E402
from watcher.config import DEFAULT_WATCHLIST_PATH, load_watchlist  # noqa: E402
from watcher.health_alerts import MODE_OFF, HealthAlertPolicy  # noqa: E402
from watcher.seen_store import SeenStore  # noqa: E402
from watcher.source_comparison import SourceComparisonStore  # noqa: E402

from scripts.benchmark_collection_replay import (  # noqa: E402
    _operational_state_fingerprint,
)


DEFAULT_SNAPSHOT = (
    REPO_ROOT
    / "watcher"
    / "collection-snapshots"
    / "collection-replay-benchmark-v2.json.gz"
)
DEFAULT_EFFECTIVE_DATE = date(2026, 7, 30)

STAGE_ORDER = (
    "snapshot_loading_decompression_validation",
    "deduplication",
    "static_analysis_fingerprint_generation",
    "batched_sqlite_cache_lookup",
    "cached_json_decoding_validation",
    "static_analysis_of_misses",
    "current_date_scoring_final_job_assembly",
    "final_job_sorting",
    "duplicate_report_enrichment",
    "categorical_eligibility_exclusion_audit",
    "filter_matches",
    "alumni_loading_attachment",
    "seen_store_partitioning",
    "source_comparison_audit_context",
    "source_comparison_lightweight_outcomes",
    "source_comparison_detail_selection",
    "source_comparison_rich_trace_construction",
    "source_comparison_trace_sanitization",
    "source_comparison_aggregation_sorting",
    "all_remaining_runtime",
)

FOCUS_FILES = (
    "backend/app/scoring.py",
    "backend/app/eligibility.py",
    "watcher/eligibility.py",
    "watcher/analysis_cache.py",
    "watcher/source_comparison.py",
    "watcher/audit_trace.py",
    "backend/app/ingest.py",
)

SPECIAL_PROFILE_TOKENS = (
    "re.pattern",
    "sre_",
    "regex",
    "json",
    "sqlite",
    "_hashlib",
    "sha256",
    "hexdigest",
    "hash.copy",
    "hash.update",
)

_ACTIVE_NETWORK_RECORDER: Recorder | None = None


@dataclass
class Recorder:
    seconds: dict[str, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    counts: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    internal: dict[str, float] = field(
        default_factory=lambda: defaultdict(float)
    )
    cache_lookup_depth: int = 0
    network_calls: int = 0

    def finalize(self, total_seconds: float, result) -> None:
        cached_decode = (
            self.internal["cache_json_decode"]
            + self.internal["cache_artifact_validation"]
        )
        self.seconds["cached_json_decoding_validation"] = cached_decode
        self.seconds["batched_sqlite_cache_lookup"] = max(
            0.0,
            self.internal["cache_lookup_total"] - cached_decode,
        )
        self.counts["cached_json_decoding_validation"] = self.internal_count(
            "cache_json_decode_calls"
        )
        self.counts["batched_sqlite_cache_lookup"] = self.internal_count(
            "cache_lookup_keys"
        )

        source_components = (
            self.seconds["source_comparison_audit_context"]
            + self.seconds["source_comparison_lightweight_outcomes"]
            + self.seconds["source_comparison_detail_selection"]
            + self.seconds["source_comparison_rich_trace_construction"]
            + self.seconds["source_comparison_trace_sanitization"]
        )
        self.seconds["source_comparison_aggregation_sorting"] = max(
            0.0,
            self.internal["source_comparison_total"] - source_components,
        )
        comparison = result.source_comparison
        self.counts["source_comparison_aggregation_sorting"] = (
            len(comparison.entries) if comparison is not None else 0
        )

        explained = sum(self.seconds[stage] for stage in STAGE_ORDER[:-1])
        self.seconds["all_remaining_runtime"] = max(
            0.0,
            total_seconds - explained,
        )

    def internal_count(self, key: str) -> int:
        return int(self.internal.get(key, 0.0))


@dataclass(frozen=True)
class ReplayMeasurement:
    total_seconds: float
    stage_seconds: dict[str, float]
    stage_rows: dict[str, int]
    rows_fetched: int
    jobs_scored: int
    matches: int
    cache_hits: int
    cache_misses: int
    cache_hit_rate: float
    output_hash: str
    network_calls: int
    operational_state_unchanged: bool
    digest_sent: bool
    seen_marked: int
    source_comparison_persisted: bool
    health_alert_sent: bool

    @property
    def safe(self) -> bool:
        return all(
            (
                self.network_calls == 0,
                self.operational_state_unchanged,
                not self.digest_sent,
                self.seen_marked == 0,
                not self.source_comparison_persisted,
                not self.health_alert_sent,
            )
        )


class _JsonProxy:
    """Time cache JSON decoding without mutating the global json module."""

    def __init__(self, recorder: Recorder, wrapped) -> None:
        self._recorder = recorder
        self._wrapped = wrapped

    def loads(self, *args, **kwargs):
        if self._recorder.cache_lookup_depth <= 0:
            return self._wrapped.loads(*args, **kwargs)
        started = perf_counter()
        try:
            return self._wrapped.loads(*args, **kwargs)
        finally:
            self._recorder.internal["cache_json_decode"] += (
                perf_counter() - started
            )
            self._recorder.internal["cache_json_decode_calls"] += 1

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


class ReplayInstrumentation(AbstractContextManager):
    """Install reversible benchmark-only timing wrappers."""

    def __init__(
        self,
        recorder: Recorder,
        *,
        omitted_comparison=None,
    ) -> None:
        self.recorder = recorder
        self.omitted_comparison = omitted_comparison
        self._originals: list[tuple[object, str, object]] = []

    def __enter__(self) -> "ReplayInstrumentation":
        global _ACTIVE_NETWORK_RECORDER
        if _ACTIVE_NETWORK_RECORDER is not None:
            raise RuntimeError("nested replay instrumentation is not supported")
        _ACTIVE_NETWORK_RECORDER = self.recorder
        self._patch(run_module, "collect_batch", self._forbidden_collection)
        self._patch(
            cache_module,
            "deduplicate_rows",
            self._deduplicate_wrapper(cache_module.deduplicate_rows),
        )
        self._patch(
            cache_module.StaticAnalysisFingerprintBuilder,
            "fingerprint",
            self._timed_method(
                "static_analysis_fingerprint_generation",
                cache_module.StaticAnalysisFingerprintBuilder.fingerprint,
            ),
        )
        self._patch(
            cache_module.AnalysisCache,
            "lookup_many",
            self._cache_lookup_wrapper(
                cache_module.AnalysisCache.lookup_many
            ),
        )
        self._patch(
            cache_module,
            "json",
            _JsonProxy(self.recorder, cache_module.json),
        )
        self._patch(
            cache_module,
            "static_analysis_artifact_is_valid",
            self._cache_validation_wrapper(
                cache_module.static_analysis_artifact_is_valid
            ),
        )
        self._patch(
            cache_module,
            "analyze_static_row",
            self._timed_function(
                "static_analysis_of_misses",
                cache_module.analyze_static_row,
                count_calls=True,
            ),
        )
        self._patch(
            cache_module,
            "assemble_scored_job",
            self._timed_function(
                "current_date_scoring_final_job_assembly",
                cache_module.assemble_scored_job,
                count_calls=True,
            ),
        )
        self._patch(
            cache_module,
            "sort_scored_jobs",
            self._timed_function(
                "final_job_sorting",
                cache_module.sort_scored_jobs,
                count_first_arg=True,
            ),
        )
        self._patch(
            run_module,
            "enrich_duplicate_entries",
            self._timed_function(
                "duplicate_report_enrichment",
                run_module.enrich_duplicate_entries,
                count_first_arg=True,
            ),
        )
        self._patch(
            run_module,
            "_categorical_exclusion_audit",
            self._timed_function(
                "categorical_eligibility_exclusion_audit",
                run_module._categorical_exclusion_audit,
                count_first_arg=True,
            ),
        )
        self._patch(
            run_module,
            "filter_matches",
            self._timed_function(
                "filter_matches",
                run_module.filter_matches,
                count_first_arg=True,
            ),
        )
        self._patch(
            run_module,
            "load_default_alumni",
            self._timed_function(
                "alumni_loading_attachment",
                run_module.load_default_alumni,
            ),
        )
        self._patch(
            run_module,
            "attach_alumni",
            self._timed_function(
                "alumni_loading_attachment",
                run_module.attach_alumni,
                count_first_arg=True,
            ),
        )
        self._patch(
            SeenStore,
            "partition",
            self._timed_method(
                "seen_store_partitioning",
                SeenStore.partition,
                count_arg_index=1,
            ),
        )
        self._patch(
            comparison_module,
            "build_posting_audit_context",
            self._timed_function(
                "source_comparison_audit_context",
                comparison_module.build_posting_audit_context,
                count_first_arg=True,
            ),
        )
        self._patch(
            comparison_module,
            "evaluate_posting_outcome",
            self._timed_function(
                "source_comparison_lightweight_outcomes",
                comparison_module.evaluate_posting_outcome,
                count_calls=True,
            ),
        )
        self._patch(
            comparison_module,
            "build_posting_comparison_summary",
            self._timed_function(
                "source_comparison_lightweight_outcomes",
                comparison_module.build_posting_comparison_summary,
            ),
        )
        self._patch(
            comparison_module,
            "select_comparison_details",
            self._timed_function(
                "source_comparison_detail_selection",
                comparison_module.select_comparison_details,
                count_first_arg=True,
            ),
        )
        self._patch(
            comparison_module,
            "build_posting_trace",
            self._timed_function(
                "source_comparison_rich_trace_construction",
                comparison_module.build_posting_trace,
                count_calls=True,
            ),
        )
        self._patch(
            comparison_module,
            "not_collected_trace",
            self._timed_function(
                "source_comparison_rich_trace_construction",
                comparison_module.not_collected_trace,
                count_calls=True,
            ),
        )
        self._patch(
            comparison_module,
            "_sanitize_trace",
            self._timed_function(
                "source_comparison_trace_sanitization",
                comparison_module._sanitize_trace,
                count_calls=True,
            ),
        )
        comparison_function = (
            self._omitted_comparison
            if self.omitted_comparison is not None
            else self._source_comparison_wrapper(
                run_module.build_source_comparison
            )
        )
        self._patch(
            run_module,
            "build_source_comparison",
            comparison_function,
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        global _ACTIVE_NETWORK_RECORDER
        for target, name, original in reversed(self._originals):
            setattr(target, name, original)
        _ACTIVE_NETWORK_RECORDER = None

    def _patch(self, target: object, name: str, replacement: object) -> None:
        self._originals.append((target, name, getattr(target, name)))
        setattr(target, name, replacement)

    def _forbidden_collection(self, *_args, **_kwargs):
        self.recorder.network_calls += 1
        raise AssertionError("warm replay attempted live collection")

    def _omitted_comparison(self, *_args, **_kwargs):
        return self.omitted_comparison

    def _deduplicate_wrapper(self, function: Callable):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            rows = args[0] if args else kwargs["rows"]
            self.recorder.counts["deduplication"] = len(rows)
            started = perf_counter()
            try:
                result = function(*args, **kwargs)
            finally:
                self.recorder.seconds["deduplication"] += (
                    perf_counter() - started
                )
            self.recorder.internal["unique_rows"] = len(result[0])
            return result

        return wrapper

    def _cache_lookup_wrapper(self, function: Callable):
        @functools.wraps(function)
        def wrapper(cache, fingerprints, *args, **kwargs):
            self.recorder.internal["cache_lookup_keys"] = len(fingerprints)
            self.recorder.cache_lookup_depth += 1
            started = perf_counter()
            try:
                return function(cache, fingerprints, *args, **kwargs)
            finally:
                self.recorder.internal["cache_lookup_total"] += (
                    perf_counter() - started
                )
                self.recorder.cache_lookup_depth -= 1

        return wrapper

    def _cache_validation_wrapper(self, function: Callable):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if self.recorder.cache_lookup_depth <= 0:
                return function(*args, **kwargs)
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                self.recorder.internal["cache_artifact_validation"] += (
                    perf_counter() - started
                )

        return wrapper

    def _timed_function(
        self,
        stage: str,
        function: Callable,
        *,
        count_first_arg: bool = False,
        count_calls: bool = False,
    ):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if count_first_arg and args:
                self.recorder.counts[stage] += len(args[0])
            if count_calls:
                self.recorder.counts[stage] += 1
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                self.recorder.seconds[stage] += perf_counter() - started

        return wrapper

    def _timed_method(
        self,
        stage: str,
        function: Callable,
        *,
        count_arg_index: int | None = None,
    ):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if count_arg_index is None:
                self.recorder.counts[stage] += 1
            elif len(args) > count_arg_index:
                self.recorder.counts[stage] += len(args[count_arg_index])
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                self.recorder.seconds[stage] += perf_counter() - started

        return wrapper

    def _source_comparison_wrapper(self, function: Callable):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                self.recorder.internal["source_comparison_total"] += (
                    perf_counter() - started
                )

        return wrapper


def _run_core(
    *,
    config,
    snapshot_path: Path,
    db_path: Path,
    effective_date: date,
    omitted_comparison=None,
) -> tuple[ReplayMeasurement, object]:
    recorder = Recorder()
    operational_db_path = _operational_db_path(db_path)
    effective_config = replace(
        config,
        analysis_cache_path=db_path,
    )
    with ReplayInstrumentation(
        recorder,
        omitted_comparison=omitted_comparison,
    ):
        total_started = perf_counter()
        snapshot_started = perf_counter()
        batch = load_collection_snapshot(snapshot_path)
        recorder.seconds["snapshot_loading_decompression_validation"] = (
            perf_counter() - snapshot_started
        )
        recorder.counts["snapshot_loading_decompression_validation"] = len(
            batch.rows
        )
        with SeenStore(operational_db_path, read_only=True) as seen_store:
            result = run_module.run_once(
                effective_config,
                seen_store=seen_store,
                alumni_index=None,
                notification_mode=run_module.RUN_MODE_DRY,
                today=effective_date,
                health_alert_policy=HealthAlertPolicy(mode=MODE_OFF),
                collection_batch=batch,
                replay_collection_snapshot_path=snapshot_path,
            )
        total_seconds = perf_counter() - total_started
    recorder.finalize(total_seconds, result)
    stats = result.analysis_cache_stats
    measurement = ReplayMeasurement(
        total_seconds=total_seconds,
        stage_seconds={
            stage: recorder.seconds.get(stage, 0.0)
            for stage in STAGE_ORDER
        },
        stage_rows={
            stage: recorder.counts.get(stage, 0)
            for stage in STAGE_ORDER
        },
        rows_fetched=result.rows_fetched,
        jobs_scored=result.jobs_scored,
        matches=len(result.matches),
        cache_hits=stats.hits,
        cache_misses=stats.misses,
        cache_hit_rate=stats.hit_rate,
        output_hash="",
        network_calls=recorder.network_calls,
        operational_state_unchanged=False,
        digest_sent=result.digest_sent,
        seen_marked=result.seen_marked,
        source_comparison_persisted=result.source_comparison_persisted,
        health_alert_sent=result.health_alert_result.sent,
    )
    return measurement, result


def _measure_replay(**kwargs) -> tuple[ReplayMeasurement, object]:
    db_path = kwargs["db_path"]
    operational_db_path = _operational_db_path(db_path)
    state_before = _operational_state_fingerprint(operational_db_path)
    measurement, result = _run_core(**kwargs)
    state_after = _operational_state_fingerprint(operational_db_path)
    measurement = replace(
        measurement,
        output_hash=_deterministic_output_hash(result),
        operational_state_unchanged=state_before == state_after,
    )
    return measurement, result


def _operational_db_path(cache_db_path: Path) -> Path:
    return cache_db_path.with_name(
        f".{cache_db_path.name}.operational.sqlite"
    )


def _deterministic_output_hash(result) -> str:
    payload = {
        "jobs": result.jobs,
        "duplicate_report": result.duplicate_report,
        "matches": result.matches,
        "source_comparison": (
            result.source_comparison.as_dict()
            if result.source_comparison is not None
            else None
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _prepare_cached_artifacts(
    *,
    snapshot_path: Path,
    db_path: Path,
) -> tuple[list[dict], list[dict], Mapping[str, object]]:
    batch = load_collection_snapshot(snapshot_path)
    profile = cache_module.load_profile()
    known = cache_module.backend_config.load_known_companies()
    rows, _duplicate_report = cache_module.deduplicate_rows(
        batch.mutable_rows(),
        include_audit_diagnostics=True,
    )
    builder = cache_module.StaticAnalysisFingerprintBuilder(
        profile=profile,
        known=known,
    )
    fingerprints = [builder.fingerprint(row) for row in rows]
    with cache_module.AnalysisCache(db_path) as cache:
        lookup = cache.lookup_many(fingerprints)
    artifacts = [lookup.artifacts[fingerprint] for fingerprint in fingerprints]
    if len(artifacts) != len(rows):
        raise RuntimeError("isolated scoring input was not fully cached")
    return rows, artifacts, profile


def _measure_isolated_assembly(
    *,
    rows: list[dict],
    artifacts: list[dict],
    profile: Mapping[str, object],
    effective_date: date,
    runs: int,
) -> dict[str, object]:
    times = []
    for _ in range(runs):
        started = perf_counter()
        jobs = [
            backend_ingest.assemble_scored_job(
                row,
                artifact,
                profile=profile,
                today=effective_date,
            )
            for row, artifact in zip(rows, artifacts)
        ]
        times.append(perf_counter() - started)
        if len(jobs) != len(rows):
            raise AssertionError("isolated assembly dropped rows")
        del jobs
    return {
        "runs_seconds": times,
        "median_seconds": median(times),
        "rows": len(rows),
        "milliseconds_per_row": median(times) * 1000 / len(rows),
    }


def _measure_comparison_persistence(report, *, runs: int) -> dict[str, object]:
    times = []
    with tempfile.TemporaryDirectory(
        prefix="internship-signal-comparison-persistence-"
    ) as directory:
        path = Path(directory) / "state.sqlite"
        with SourceComparisonStore(path) as store:
            for index in range(runs):
                measured_report = replace(
                    report,
                    run_id=f"persistence-{index}",
                )
                started = perf_counter()
                store.save(measured_report)
                times.append(perf_counter() - started)
        database_bytes = path.stat().st_size
    return {
        "runs_seconds": times,
        "median_seconds": median(times),
        "entries": len(report.entries),
        "database_bytes": database_bytes,
    }


def _median_stage_report(
    measurements: list[ReplayMeasurement],
) -> tuple[dict[str, float], dict[str, dict[str, float | int | None]]]:
    median_seconds = {
        stage: median(
            measurement.stage_seconds[stage]
            for measurement in measurements
        )
        for stage in STAGE_ORDER
    }
    per_row = {}
    for stage in STAGE_ORDER:
        counts = [measurement.stage_rows[stage] for measurement in measurements]
        row_count = int(median(counts))
        per_row[stage] = {
            "rows": row_count,
            "milliseconds_per_row": (
                median_seconds[stage] * 1000 / row_count
                if row_count
                else None
            ),
        }
    return median_seconds, per_row


def _profile_rows(profile: cProfile.Profile) -> dict[str, list[dict[str, object]]]:
    stats = pstats.Stats(profile)
    focused = []
    special = []
    for (filename, line, function), values in stats.stats.items():
        primitive_calls, total_calls, self_time, cumulative_time, _callers = values
        normalized = filename.replace("\\", "/")
        label = f"{normalized}:{line}({function})"
        row = {
            "function": _display_function(normalized, line, function),
            "primitive_calls": primitive_calls,
            "call_count": total_calls,
            "self_seconds": self_time,
            "cumulative_seconds": cumulative_time,
        }
        if any(marker in normalized for marker in FOCUS_FILES):
            focused.append(row)
        lowered = label.casefold()
        if any(token in lowered for token in SPECIAL_PROFILE_TOKENS):
            special.append(row)

    return {
        "top_30_focused_by_cumulative": sorted(
            focused,
            key=lambda row: (
                -float(row["cumulative_seconds"]),
                str(row["function"]),
            ),
        )[:30],
        "top_30_focused_by_self": sorted(
            focused,
            key=lambda row: (
                -float(row["self_seconds"]),
                str(row["function"]),
            ),
        )[:30],
        "top_30_focused_by_call_count": sorted(
            focused,
            key=lambda row: (
                -int(row["call_count"]),
                str(row["function"]),
            ),
        )[:30],
        "regex_json_sqlite_hashing_operations": sorted(
            special,
            key=lambda row: (
                -float(row["cumulative_seconds"]),
                str(row["function"]),
            ),
        )[:30],
    }


def _display_function(filename: str, line: int, function: str) -> str:
    for marker in FOCUS_FILES:
        if marker in filename:
            return f"{marker}:{line}({function})"
    return f"{filename}:{line}({function})"


def _network_audit_hook(event: str, _args: tuple[object, ...]) -> None:
    if event not in {"socket.connect", "socket.getaddrinfo"}:
        return
    recorder = _ACTIVE_NETWORK_RECORDER
    if recorder is None:
        return
    recorder.network_calls += 1
    raise AssertionError(f"warm replay attempted network operation: {event}")


def _measurement_dict(measurement: ReplayMeasurement) -> dict[str, object]:
    return {
        "total_seconds": measurement.total_seconds,
        "stage_seconds": measurement.stage_seconds,
        "stage_rows": measurement.stage_rows,
        "rows_fetched": measurement.rows_fetched,
        "jobs_scored": measurement.jobs_scored,
        "matches": measurement.matches,
        "cache_hits": measurement.cache_hits,
        "cache_misses": measurement.cache_misses,
        "cache_hit_rate": measurement.cache_hit_rate,
        "output_hash": measurement.output_hash,
        "network_calls": measurement.network_calls,
        "operational_state_unchanged": (
            measurement.operational_state_unchanged
        ),
        "digest_sent": measurement.digest_sent,
        "seen_marked": measurement.seen_marked,
        "source_comparison_persisted": (
            measurement.source_comparison_persisted
        ),
        "health_alert_sent": measurement.health_alert_sent,
        "safe": measurement.safe,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=DEFAULT_SNAPSHOT,
    )
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=DEFAULT_EFFECTIVE_DATE,
    )
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--db",
        type=Path,
        required=True,
        help="Disposable SQLite path used only for the warmed analysis cache.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional UTF-8 JSON report path.",
    )
    parser.add_argument(
        "--reuse-warm-cache",
        action="store_true",
        help="Require and reuse an existing fully warmed disposable cache.",
    )
    args = parser.parse_args(argv)
    if args.runs < 3:
        parser.error("--runs must be at least 3")
    if args.db.exists() and not args.reuse_warm_cache:
        parser.error("--db must not already exist")
    if args.reuse_warm_cache and not args.db.is_file():
        parser.error("--reuse-warm-cache requires an existing --db file")

    config = replace(
        load_watchlist(args.watchlist),
        analysis_cache_enabled=True,
    )
    args.db.parent.mkdir(parents=True, exist_ok=True)

    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    sys.addaudithook(_network_audit_hook)
    try:
        warmup, warmup_result = _measure_replay(
            config=config,
            snapshot_path=args.snapshot,
            db_path=args.db,
            effective_date=args.today,
        )
        if args.reuse_warm_cache and (
            warmup.cache_misses != 0
            or warmup.cache_hits != warmup.jobs_scored
            or warmup.cache_hit_rate != 1.0
        ):
            raise RuntimeError(
                "unmeasured warm-up did not find a fully warmed cache"
            )
        prebuilt_comparison = warmup_result.source_comparison
        del warmup_result
        gc.collect()

        measured: list[ReplayMeasurement] = []
        for _ in range(args.runs):
            measurement, result = _measure_replay(
                config=config,
                snapshot_path=args.snapshot,
                db_path=args.db,
                effective_date=args.today,
            )
            measured.append(measurement)
            del result
            gc.collect()

        omitted: list[ReplayMeasurement] = []
        for _ in range(args.runs):
            measurement, result = _measure_replay(
                config=config,
                snapshot_path=args.snapshot,
                db_path=args.db,
                effective_date=args.today,
                omitted_comparison=prebuilt_comparison,
            )
            omitted.append(measurement)
            del result
            gc.collect()

        cached_rows, cached_artifacts, profile_data = _prepare_cached_artifacts(
            snapshot_path=args.snapshot,
            db_path=args.db,
        )
        isolated_assembly = _measure_isolated_assembly(
            rows=cached_rows,
            artifacts=cached_artifacts,
            profile=profile_data,
            effective_date=args.today,
            runs=args.runs,
        )
        comparison_persistence = _measure_comparison_persistence(
            prebuilt_comparison,
            runs=args.runs,
        )

        profiler = cProfile.Profile()
        profiled_measurement, profiled_result = profiler.runcall(
            _run_core,
            config=config,
            snapshot_path=args.snapshot,
            db_path=args.db,
            effective_date=args.today,
        )
        profiled_hash = _deterministic_output_hash(profiled_result)
        del profiled_result
        gc.collect()
    finally:
        logging.disable(previous_disable)

    median_stages, per_row = _median_stage_report(measured)
    normal_total = median(item.total_seconds for item in measured)
    omitted_total = median(item.total_seconds for item in omitted)
    source_comparison_total = median(
        sum(
            item.stage_seconds[stage]
            for stage in (
                "source_comparison_audit_context",
                "source_comparison_lightweight_outcomes",
                "source_comparison_detail_selection",
                "source_comparison_rich_trace_construction",
                "source_comparison_trace_sanitization",
                "source_comparison_aggregation_sorting",
            )
        )
        for item in measured
    )
    output_hashes = [item.output_hash for item in measured]
    all_measured_valid = all(
        item.safe
        and item.cache_misses == 0
        and item.cache_hits == item.jobs_scored
        and item.cache_hit_rate == 1.0
        for item in measured
    )
    report = {
        "snapshot": {
            "path": str(args.snapshot),
            "compressed_bytes": args.snapshot.stat().st_size,
            "effective_date": args.today.isoformat(),
        },
        "procedure": {
            "warmup_runs": 1,
            "warmup_started_with_preexisting_cache": args.reuse_warm_cache,
            "measured_runs": args.runs,
            "comparison_omitted_runs": args.runs,
            "isolated_assembly_runs": args.runs,
            "profile_runs": 1,
        },
        "warmup": _measurement_dict(warmup),
        "measured_runs": [_measurement_dict(item) for item in measured],
        "median": {
            "total_seconds": normal_total,
            "stage_seconds": median_stages,
            "stage_milliseconds": {
                stage: seconds * 1000
                for stage, seconds in median_stages.items()
            },
            "per_row": per_row,
        },
        "confirmations": {
            "all_measured_runs_valid": all_measured_valid,
            "zero_network_calls": all(
                item.network_calls == 0 for item in measured
            ),
            "all_cache_hit_rates": [
                item.cache_hit_rate for item in measured
            ],
            "identical_output_hashes": len(set(output_hashes)) == 1,
            "output_hashes": output_hashes,
            "profiled_output_hash_matches": profiled_hash == output_hashes[0],
            "operational_state_unchanged": all(
                item.operational_state_unchanged for item in measured
            ),
        },
        "isolation": {
            "comparison_omitted_runs_seconds": [
                item.total_seconds for item in omitted
            ],
            "comparison_omitted_median_seconds": omitted_total,
            "source_comparison_delta_seconds": normal_total - omitted_total,
            "direct_source_comparison_median_seconds": (
                source_comparison_total
            ),
            "isolated_current_scoring_assembly": isolated_assembly,
            "source_comparison_persistence": comparison_persistence,
        },
        "improvement_vs_approximate_baseline": {
            "baseline_source_comparison_seconds": 20.0,
            "baseline_total_warm_replay_seconds": 30.0,
            "source_comparison_absolute_seconds": (
                20.0 - source_comparison_total
            ),
            "source_comparison_percentage": (
                (20.0 - source_comparison_total) / 20.0 * 100
            ),
            "total_absolute_seconds": 30.0 - normal_total,
            "total_percentage": (30.0 - normal_total) / 30.0 * 100,
        },
        "source_comparison": {
            "summaries_evaluated": (
                prebuilt_comparison.postings_evaluated
                if prebuilt_comparison is not None
                else 0
            ),
            "details_retained": (
                prebuilt_comparison.detail_entries_retained
                if prebuilt_comparison is not None
                else 0
            ),
            "retained_by_category": (
                {
                    category: sum(
                        entry.category == category
                        for entry in prebuilt_comparison.entries
                    )
                    for category in comparison_module.CATEGORIES
                }
                if prebuilt_comparison is not None
                else {}
            ),
            "retained_by_reason": (
                {
                    reason: sum(
                        entry.final_reason == reason
                        for entry in prebuilt_comparison.entries
                    )
                    for reason in sorted(
                        {
                            entry.final_reason
                            for entry in prebuilt_comparison.entries
                        }
                    )
                }
                if prebuilt_comparison is not None
                else {}
            ),
        },
        "profile": {
            "profiled_total_seconds": profiled_measurement.total_seconds,
            **_profile_rows(profiler),
        },
    }
    report_json = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report_json + "\n", encoding="utf-8")
        print(
            f"WARM-REPLAY-AUDIT output={args.output} "
            f"rows={warmup.rows_fetched} "
            f"measured_runs={args.runs} "
            f"valid={str(all_measured_valid).lower()}"
        )
    else:
        print(report_json)
    return 0 if all_measured_valid and len(set(output_hashes)) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
