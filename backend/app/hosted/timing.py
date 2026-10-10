"""Hosted-safe stage timing for the scheduled collection/import command.

``collect_and_import`` prints a small, bounded set of ``HOSTED-TIMING`` lines
so production stage costs are measurable from the hosted process logs without
enabling the watcher's verbose INFO logging, whose per-source records name
companies. Every line carries only fixed stage names, low-cardinality ATS
family names, counts, and seconds - never company names, URLs, titles, or user
data.

Telemetry is strictly best effort: recording or emitting it can never change
collection, snapshot, import, matching, or notification behaviour, and can
never fail or alter the exit status of the command.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from typing import TextIO

PREFIX = "HOSTED-TIMING"

# Canonical emission order. Only recorded stages are emitted.
STAGE_ORDER = (
    "direct_collection",
    "github_collection",
    "collection",
    "snapshot_save",
    "snapshot_verify_load",
    "analysis",
    "job_mapping",
    "job_upsert",
    "match_reconciliation",
    "notification_enqueue",
    "database_import",
)
# Bounds the family lines one run can print, whatever the configuration.
MAX_FAMILY_LINES = 64

_SAFE_TOKEN_RE = re.compile(r"[^a-z0-9_]+")


def safe_token(value: object, *, limit: int = 40) -> str:
    """Return a bounded lowercase ``[a-z0-9_]`` token, or ``unknown``."""

    try:
        text = _SAFE_TOKEN_RE.sub("_", str(value or "").strip().casefold())
    except Exception:  # noqa: BLE001 - telemetry must never raise
        return "unknown"
    return text.strip("_")[:limit] or "unknown"


def _seconds(value: object) -> str:
    try:
        return f"{max(0.0, float(value)):.3f}"
    except Exception:  # noqa: BLE001
        return "0.000"


def _count(value: object) -> int:
    try:
        return max(0, int(value))
    except Exception:  # noqa: BLE001
        return 0


class HostedTiming:
    """Monotonic stage timings plus collection aggregates for one run."""

    def __init__(self, clock=time.perf_counter) -> None:
        self._clock = clock
        self._started = self._now()
        self._stages: dict[str, float] = {}
        self._concurrency: dict[str, object] | None = None
        self._families: list[str] = []
        self._analysis_cache: dict[str, object] | None = None

    def _now(self) -> float | None:
        try:
            return float(self._clock())
        except Exception:  # noqa: BLE001
            return None

    def record(self, stage: str, seconds: object) -> None:
        try:
            name = safe_token(stage)
            self._stages[name] = self._stages.get(name, 0.0) + max(
                0.0, float(seconds)
            )
        except Exception:  # noqa: BLE001
            pass

    @contextmanager
    def stage(self, stage: str) -> Iterator[None]:
        """Time the block, recording it even when the block raises."""

        started = self._now()
        try:
            yield
        finally:
            ended = self._now()
            if started is not None and ended is not None:
                self.record(stage, ended - started)

    def record_collection(self, config: object, stats: object | None) -> None:
        """Capture concurrency settings and in-memory collection aggregates."""

        try:
            self._concurrency = _concurrency_fields(config, stats)
        except Exception:  # noqa: BLE001
            self._concurrency = None
        if stats is None:
            return
        try:
            for stage, seconds in dict(getattr(stats, "stage_seconds", {})).items():
                self.record(stage, seconds)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._families = _family_lines(getattr(stats, "family_timing", {}))
        except Exception:  # noqa: BLE001
            self._families = []

    def record_analysis_cache(self, stats: object | None, *, reason: str = "") -> None:
        """Capture hosted analysis-cache counters, or why the cache was skipped."""

        try:
            self._analysis_cache = _analysis_cache_fields(stats, reason=reason)
        except Exception:  # noqa: BLE001
            self._analysis_cache = None

    def lines(self, *, exit_code: int) -> list[str]:
        lines: list[str] = []
        try:
            if self._concurrency is not None:
                lines.append(
                    f"{PREFIX} kind=concurrency "
                    + " ".join(f"{key}={value}" for key, value in self._concurrency.items())
                )
            for stage in STAGE_ORDER:
                if stage in self._stages:
                    lines.append(
                        f"{PREFIX} kind=stage stage={stage} "
                        f"seconds={_seconds(self._stages[stage])}"
                    )
            lines.extend(self._families)
            if self._analysis_cache is not None:
                lines.append(
                    f"{PREFIX} kind=cache cache=analysis "
                    + " ".join(
                        f"{key}={value}" for key, value in self._analysis_cache.items()
                    )
                )
            ended = self._now()
            total = (
                ended - self._started
                if ended is not None and self._started is not None
                else 0.0
            )
            lines.append(
                f"{PREFIX} kind=stage stage=total seconds={_seconds(total)} "
                f"exit_code={_count(exit_code)}"
            )
        except Exception:  # noqa: BLE001
            pass
        return lines

    def emit(self, *, exit_code: int, stream: TextIO | None = None) -> None:
        try:
            target = stream if stream is not None else sys.stdout
            for line in self.lines(exit_code=exit_code):
                print(line, file=target)
            target.flush()
        except Exception:  # noqa: BLE001
            pass


def stage(timing: HostedTiming | None, name: str):
    """Return a timing context, or a no-op context when timing is absent."""

    return timing.stage(name) if timing is not None else nullcontext()


def _concurrency_fields(config: object, stats: object | None) -> dict[str, object]:
    settings = getattr(config, "collection_concurrency", None)
    mode = safe_token(getattr(settings, "mode", "serial"))
    concurrent = bool(getattr(settings, "concurrent", False))
    # Serial collection ignores the configured limits, so the effective
    # values are one, matching ``CollectionConcurrencyMetrics``.
    fields: dict[str, object] = {
        "mode": mode,
        "max_workers": _count(getattr(settings, "max_workers", 1)) if concurrent else 1,
        "per_origin_limit": (
            _count(getattr(settings, "per_origin_max_concurrency", 1))
            if concurrent
            else 1
        ),
        "workday_limit": (
            _count(getattr(settings, "workday_max_concurrency", 1)) if concurrent else 1
        ),
    }
    metrics = getattr(stats, "collection_concurrency", None) if stats else None
    if metrics is not None:
        fields["observed_global"] = _count(getattr(metrics, "max_observed_global", 0))
        fields["observed_per_origin"] = _count(
            getattr(metrics, "max_observed_per_origin", 0)
        )
        fields["observed_workday"] = _count(getattr(metrics, "max_observed_workday", 0))
    return fields


def _analysis_cache_fields(stats: object | None, *, reason: str) -> dict[str, object]:
    # Only counts, a ratio, seconds, and a fixed reason token: never paths,
    # fingerprints, or job content.
    if stats is None:
        return {"enabled": "false", "reason": safe_token(reason)}
    rows = _count(getattr(stats, "rows", 0))
    hits = _count(getattr(stats, "hits", 0))
    return {
        "enabled": "true",
        "rows": rows,
        "hits": hits,
        "misses": _count(getattr(stats, "misses", 0)),
        "invalid": _count(getattr(stats, "invalid", 0)),
        "writes": _count(getattr(stats, "writes", 0)),
        "hit_rate": f"{min(1.0, hits / rows) if rows else 0.0:.3f}",
        "lookup_seconds": _seconds(getattr(stats, "lookup_seconds", 0.0)),
        "static_seconds": _seconds(getattr(stats, "static_analysis_seconds", 0.0)),
        "scoring_seconds": _seconds(getattr(stats, "scoring_seconds", 0.0)),
    }


def _family_lines(families: Mapping[object, object]) -> list[str]:
    merged: dict[tuple[str, str], list[object]] = {}
    for key, family in families.items():
        if isinstance(key, tuple) and len(key) == 2:
            source_kind, name = key
        else:
            source_kind, name = "direct", key
        merged.setdefault((safe_token(source_kind), safe_token(name)), []).append(family)

    lines: list[str] = []
    for (source_kind, name), entries in sorted(merged.items())[:MAX_FAMILY_LINES]:
        tasks = sum(_count(getattr(entry, "tasks", 0)) for entry in entries)
        total = sum(float(getattr(entry, "seconds_total", 0.0) or 0.0) for entry in entries)
        longest = max(float(getattr(entry, "seconds_max", 0.0) or 0.0) for entry in entries)
        fields = [
            f"{PREFIX} kind=family",
            f"source_kind={source_kind}",
            f"family={name}",
            f"tasks={tasks}",
            f"seconds_total={_seconds(total)}",
            f"seconds_max={_seconds(longest)}",
            f"failed={sum(_count(getattr(e, 'failed', 0)) for e in entries)}",
            f"degraded={sum(_count(getattr(e, 'degraded', 0)) for e in entries)}",
            f"rows={sum(_count(getattr(e, 'rows', 0)) for e in entries)}",
        ]
        optional = (
            ("requests", "requests_reported_tasks", "requests"),
            ("retries", "retries_reported_tasks", "retries"),
            ("detail_requests", "detail_reported_tasks", "detail_requests"),
        )
        for attribute, reported_attribute, label in optional:
            reported = sum(_count(getattr(e, reported_attribute, 0)) for e in entries)
            if reported:
                fields.append(
                    f"{label}={sum(_count(getattr(e, attribute, 0)) for e in entries)}"
                )
        budget_skips = sum(_count(getattr(e, "detail_budget_skips", 0)) for e in entries)
        if budget_skips:
            fields.append(f"detail_budget_skips={budget_skips}")
        lines.append(" ".join(fields))
    return lines
