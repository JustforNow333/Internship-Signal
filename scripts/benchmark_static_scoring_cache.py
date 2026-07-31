#!/usr/bin/env python3
"""Benchmark disabled, cold, and warm replay after static-scoring caching."""

from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import tempfile
from dataclasses import replace
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.audit_warm_collection_replay import (  # noqa: E402
    DEFAULT_SNAPSHOT,
    _measure_replay,
    _network_audit_hook,
)
from watcher.config import DEFAULT_WATCHLIST_PATH, load_watchlist  # noqa: E402


DEFAULT_EFFECTIVE_DATE = date(2026, 7, 30)
PREVIOUS_CACHE_DATABASE_BYTES = 89_657_344
SOURCE_COMPARISON_STAGES = (
    "source_comparison_audit_context",
    "source_comparison_per_job_trace",
    "source_comparison_trace_sanitization",
    "source_comparison_aggregation_sorting",
)


def _case_dict(measurement) -> dict[str, object]:
    stages = measurement.stage_seconds
    source_comparison = sum(stages[name] for name in SOURCE_COMPARISON_STAGES)
    return {
        "total_seconds": measurement.total_seconds,
        "rows": measurement.rows_fetched,
        "jobs": measurement.jobs_scored,
        "matches": measurement.matches,
        "cache_hits": measurement.cache_hits,
        "cache_misses": measurement.cache_misses,
        "cache_hit_rate": measurement.cache_hit_rate,
        "fingerprint_seconds": stages[
            "static_analysis_fingerprint_generation"
        ],
        "sqlite_lookup_seconds": stages["batched_sqlite_cache_lookup"],
        "json_decode_validation_seconds": stages[
            "cached_json_decoding_validation"
        ],
        "lookup_total_seconds": (
            stages["batched_sqlite_cache_lookup"]
            + stages["cached_json_decoding_validation"]
        ),
        "static_analysis_seconds": stages["static_analysis_of_misses"],
        "dynamic_scoring_assembly_seconds": stages[
            "current_date_scoring_final_job_assembly"
        ],
        "source_comparison_seconds": source_comparison,
        "milliseconds_per_job": (
            measurement.total_seconds * 1000 / measurement.jobs_scored
        ),
        "dynamic_scoring_milliseconds_per_job": (
            stages["current_date_scoring_final_job_assembly"]
            * 1000
            / measurement.jobs_scored
        ),
        "output_hash": measurement.output_hash,
        "network_calls": measurement.network_calls,
        "operational_state_unchanged": (
            measurement.operational_state_unchanged
        ),
        "safe": measurement.safe,
    }


def _improvement(before: float, after: float) -> dict[str, float]:
    absolute = before - after
    return {
        "absolute_seconds": absolute,
        "percentage": absolute / before * 100 if before else 0.0,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST_PATH)
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=DEFAULT_EFFECTIVE_DATE,
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    base_config = load_watchlist(args.watchlist)
    disabled_config = replace(
        base_config,
        analysis_cache_enabled=False,
    )
    enabled_config = replace(
        base_config,
        analysis_cache_enabled=True,
    )
    sys.addaudithook(_network_audit_hook)

    previous_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with tempfile.TemporaryDirectory(
            prefix="internship-signal-static-scoring-"
        ) as directory:
            temp_dir = Path(directory)
            disabled, disabled_result = _measure_replay(
                config=disabled_config,
                snapshot_path=args.snapshot,
                db_path=temp_dir / "disabled.sqlite",
                effective_date=args.today,
            )
            del disabled_result
            gc.collect()

            cache_db = temp_dir / "cache.sqlite"
            cold, cold_result = _measure_replay(
                config=enabled_config,
                snapshot_path=args.snapshot,
                db_path=cache_db,
                effective_date=args.today,
            )
            cold_database_bytes = cache_db.stat().st_size
            del cold_result
            gc.collect()

            warm, warm_result = _measure_replay(
                config=enabled_config,
                snapshot_path=args.snapshot,
                db_path=cache_db,
                effective_date=args.today,
            )
            warm_database_bytes = cache_db.stat().st_size
            del warm_result
            gc.collect()
    finally:
        logging.disable(previous_disable)

    disabled_data = _case_dict(disabled)
    cold_data = _case_dict(cold)
    warm_data = _case_dict(warm)
    hashes = {
        disabled.output_hash,
        cold.output_hash,
        warm.output_hash,
    }
    all_safe = all(case.safe for case in (disabled, cold, warm))
    report = {
        "snapshot": {
            "path": str(args.snapshot),
            "compressed_bytes": args.snapshot.stat().st_size,
            "effective_date": args.today.isoformat(),
        },
        "cases": {
            "cache_disabled": disabled_data,
            "cold_cache": cold_data,
            "warm_cache": warm_data,
        },
        "improvement_vs_warm_baseline": {
            "baseline": {
                "dynamic_scoring_assembly_seconds": 42.883,
                "source_comparison_seconds": 16.297,
                "total_seconds": 63.464,
            },
            "dynamic_scoring_assembly": _improvement(
                42.883,
                float(warm_data["dynamic_scoring_assembly_seconds"]),
            ),
            "source_comparison": _improvement(
                16.297,
                float(warm_data["source_comparison_seconds"]),
            ),
            "total": _improvement(
                63.464,
                float(warm_data["total_seconds"]),
            ),
        },
        "database": {
            "cold_bytes": cold_database_bytes,
            "warm_bytes": warm_database_bytes,
            "warm_access_increase_bytes": (
                warm_database_bytes - cold_database_bytes
            ),
            "previous_artifact_database_bytes": (
                PREVIOUS_CACHE_DATABASE_BYTES
            ),
            "artifact_database_size_change_bytes": (
                cold_database_bytes - PREVIOUS_CACHE_DATABASE_BYTES
            ),
        },
        "confirmation": {
            "deterministic_outputs_identical": len(hashes) == 1,
            "output_hashes": sorted(hashes),
            "zero_network_calls": all(
                case.network_calls == 0
                for case in (disabled, cold, warm)
            ),
            "zero_operational_side_effects": all(
                case.operational_state_unchanged
                for case in (disabled, cold, warm)
            ),
            "all_cases_safe": all_safe,
        },
    }
    report_json = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    if args.output is None:
        print(report_json)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report_json + "\n", encoding="utf-8")
        print(
            f"STATIC-SCORING-CACHE-BENCHMARK output={args.output} "
            f"rows={warm.rows_fetched} "
            f"warm_hits={warm.cache_hits} warm_misses={warm.cache_misses} "
            f"valid={str(all_safe and len(hashes) == 1).lower()}"
        )
    return 0 if all_safe and len(hashes) == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
