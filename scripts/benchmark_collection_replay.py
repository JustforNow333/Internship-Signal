#!/usr/bin/env python3
"""Benchmark live collection against cold and warm collection replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from watcher.collection_snapshot import (  # noqa: E402
    DEFAULT_COLLECTION_SNAPSHOT_DIR,
    load_collection_snapshot,
)
from watcher.config import DEFAULT_WATCHLIST_PATH, load_watchlist  # noqa: E402
from watcher.health_alerts import MODE_OFF, HealthAlertPolicy  # noqa: E402
from watcher.run import RUN_MODE_DRY, run_once  # noqa: E402
from watcher.seen_store import SeenStore  # noqa: E402

OPERATIONAL_STATE_TABLES = (
    "seen",
    "source_health_alert_events",
    "source_health_alert_state",
    "source_health_attempts",
    "source_health_coverage_snapshots",
    "source_health_current",
    "source_health_daily_summary",
    "source_comparison_postings",
    "source_comparison_runs",
)


class _StageTimingCapture(logging.Handler):
    """Capture unrounded watcher stage durations without changing its logs."""

    def __init__(self) -> None:
        super().__init__()
        self.seconds: dict[str, float] = {}

    def emit(self, record: logging.LogRecord) -> None:
        if (
            record.name == "watcher.run"
            and record.msg == "STAGE-TIMING stage=%s seconds=%.3f"
            and isinstance(record.args, tuple)
            and len(record.args) == 2
        ):
            stage, seconds = record.args
            self.seconds[str(stage)] = float(seconds)


def _run(
    config,
    *,
    db_path: Path,
    today,
    collection_batch=None,
    capture_path: Path | None = None,
):
    stage_capture = _StageTimingCapture()
    watcher_logger = logging.getLogger("watcher.run")
    previous_log_level = watcher_logger.level
    if not watcher_logger.isEnabledFor(logging.INFO):
        watcher_logger.setLevel(logging.INFO)
    watcher_logger.addHandler(stage_capture)
    state_before = (
        _operational_state_fingerprint(db_path)
        if collection_batch is not None
        else None
    )
    started = perf_counter()
    try:
        with SeenStore(
            db_path,
            read_only=collection_batch is not None,
        ) as seen_store:
            result = run_once(
                config,
                seen_store=seen_store,
                alumni_index={},
                digest_sender=lambda _matches: False,
                notification_mode=RUN_MODE_DRY,
                today=today,
                health_alert_policy=HealthAlertPolicy(mode=MODE_OFF),
                collection_batch=collection_batch,
                capture_collection_snapshot_path=(
                    capture_path if collection_batch is None else None
                ),
                replay_collection_snapshot_path=(
                    capture_path if collection_batch is not None else None
                ),
            )
    finally:
        total_seconds = perf_counter() - started
        watcher_logger.removeHandler(stage_capture)
        watcher_logger.setLevel(previous_log_level)
    state_after = (
        _operational_state_fingerprint(db_path)
        if collection_batch is not None
        else None
    )
    return (
        result,
        total_seconds,
        stage_capture.seconds,
        state_before == state_after,
    )


def _deterministic_payload(result) -> str:
    comparison = result.source_comparison.as_dict()
    comparison.pop("observed_at", None)
    return json.dumps(
        {
            "jobs": result.jobs,
            "duplicate_report": result.duplicate_report,
            "matches": result.matches,
            "source_comparison": comparison,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _print_case(
    label: str,
    result,
    total_seconds: float,
    stage_seconds: dict[str, float],
) -> None:
    stats = result.analysis_cache_stats
    analysis_seconds = stats.static_analysis_seconds + stats.scoring_seconds
    print(
        "COLLECTION-REPLAY-BENCHMARK "
        f"mode={label} rows={result.rows_fetched} "
        f"collection_seconds={stage_seconds.get('collection', 0.0):.3f} "
        f"analysis_seconds={analysis_seconds:.3f} "
        f"total_seconds={total_seconds:.3f} "
        f"hits={stats.hits} misses={stats.misses} "
        f"hit_rate={stats.hit_rate:.3f}"
    )


def _operational_state_fingerprint(db_path: Path) -> str:
    """Hash non-cache persistent state without exposing its contents."""

    payload: dict[str, list[list[object]]] = {}
    if db_path.is_file():
        with sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
        ) as connection:
            existing = {
                str(row[0])
                for row in connection.execute(
                    "select name from sqlite_master where type = 'table'"
                )
            }
            for table in OPERATIONAL_STATE_TABLES:
                if table not in existing:
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                rows = [
                    list(row)
                    for row in connection.execute(f"select * from {quoted}")
                ]
                payload[table] = sorted(rows, key=repr)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--watchlist",
        type=Path,
        default=DEFAULT_WATCHLIST_PATH,
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=(
            DEFAULT_COLLECTION_SNAPSHOT_DIR
            / "collection-replay-benchmark.json.gz"
        ),
    )
    args = parser.parse_args(argv)

    config = load_watchlist(args.watchlist)
    benchmark_date = datetime.now(timezone.utc).date()
    disabled_config = replace(config, analysis_cache_enabled=False)
    enabled_config = replace(config, analysis_cache_enabled=True)
    args.snapshot.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix="internship-signal-collection-replay-"
    ) as directory:
        temp_dir = Path(directory)
        live, live_total, live_stages, _live_state_unchanged = _run(
            disabled_config,
            db_path=temp_dir / "live.sqlite",
            today=benchmark_date,
            capture_path=args.snapshot,
        )
        batch = load_collection_snapshot(args.snapshot)
        (
            replay_disabled,
            replay_disabled_total,
            replay_disabled_stages,
            replay_disabled_state_unchanged,
        ) = _run(
            disabled_config,
            db_path=temp_dir / "replay-disabled.sqlite",
            today=benchmark_date,
            collection_batch=batch,
            capture_path=args.snapshot,
        )

        warm_db = temp_dir / "replay-warm.sqlite"
        _run(
            enabled_config,
            db_path=warm_db,
            today=benchmark_date,
            collection_batch=batch,
            capture_path=args.snapshot,
        )
        (
            replay_warm,
            replay_warm_total,
            replay_warm_stages,
            replay_warm_state_unchanged,
        ) = _run(
            enabled_config,
            db_path=warm_db,
            today=benchmark_date,
            collection_batch=batch,
            capture_path=args.snapshot,
        )

        _print_case("live_dry", live, live_total, live_stages)
        _print_case(
            "replay_cache_disabled",
            replay_disabled,
            replay_disabled_total,
            replay_disabled_stages,
        )
        _print_case(
            "replay_cache_warm",
            replay_warm,
            replay_warm_total,
            replay_warm_stages,
        )
        identical = len(
            {
                _deterministic_payload(live),
                _deterministic_payload(replay_disabled),
                _deterministic_payload(replay_warm),
            }
        ) == 1
        print(
            "COLLECTION-REPLAY-EQUIVALENCE "
            f"rows={len(batch.rows)} "
            f"snapshot_compressed_bytes={args.snapshot.stat().st_size} "
            f"deterministic_outputs_identical={str(identical).lower()}"
        )
        replay_safe = all(
            (
                replay_disabled_state_unchanged,
                replay_warm_state_unchanged,
                "collection" not in replay_disabled_stages,
                "collection" not in replay_warm_stages,
                not replay_disabled.digest_sent,
                not replay_warm.digest_sent,
                replay_disabled.seen_marked == 0,
                replay_warm.seen_marked == 0,
                not replay_disabled.source_comparison_persisted,
                not replay_warm.source_comparison_persisted,
                not replay_disabled.health_alert_result.sent,
                not replay_warm.health_alert_result.sent,
            )
        )
        print(
            "COLLECTION-REPLAY-SAFETY "
            "network_collection_skipped="
            f"{str('collection' not in replay_disabled_stages and 'collection' not in replay_warm_stages).lower()} "
            "operational_state_unchanged="
            f"{str(replay_disabled_state_unchanged and replay_warm_state_unchanged).lower()} "
            f"notifications_sent={str(replay_disabled.digest_sent or replay_warm.digest_sent).lower()} "
            f"seen_marked={replay_disabled.seen_marked + replay_warm.seen_marked} "
            "source_comparison_persisted="
            f"{str(replay_disabled.source_comparison_persisted or replay_warm.source_comparison_persisted).lower()} "
            "health_alert_sent="
            f"{str(replay_disabled.health_alert_result.sent or replay_warm.health_alert_result.sent).lower()}"
        )
        return 0 if identical and replay_safe else 1


if __name__ == "__main__":
    raise SystemExit(main())
