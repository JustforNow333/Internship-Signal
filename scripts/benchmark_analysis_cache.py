#!/usr/bin/env python3
"""Benchmark disabled, empty, and warm watcher analysis-cache runs offline."""

from __future__ import annotations

import argparse
import copy
import gc
import json
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.benchmark_analysis_context import (  # noqa: E402
    DEFAULT_INPUT,
    load_rows,
    representative_rows,
)
from watcher.analysis_cache import analyze_rows_with_cache  # noqa: E402
from watcher.seen_store import SeenStore  # noqa: E402


DEFAULT_ROWS = 2_000
DEFAULT_AS_OF = date(2026, 7, 30)
ACCESSED_AT = datetime(2026, 7, 30, 12, tzinfo=timezone.utc)


def serialized(result) -> str:
    return json.dumps(
        {
            "jobs": result.jobs,
            "duplicate_report": result.duplicate_report,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def run_case(
    label: str,
    rows: list[dict],
    *,
    db_path: Path,
    enabled: bool,
    as_of: date,
    initial_db_size: int,
):
    current_rows = copy.deepcopy(rows)
    gc.collect()
    started = perf_counter()
    result = analyze_rows_with_cache(
        current_rows,
        db_path=db_path,
        enabled=enabled,
        today=as_of,
        include_audit_diagnostics=True,
        accessed_at=ACCESSED_AT,
    )
    total_seconds = perf_counter() - started
    analysis_seconds = (
        result.stats.static_analysis_seconds
        + result.stats.scoring_seconds
    )
    db_size = db_path.stat().st_size
    print(
        "ANALYSIS-CACHE-BENCHMARK "
        f"mode={label} rows={result.stats.rows} "
        f"analysis_seconds={analysis_seconds:.3f} "
        f"total_seconds={total_seconds:.3f} "
        f"hits={result.stats.hits} misses={result.stats.misses} "
        f"hit_rate={result.stats.hit_rate:.3f} "
        f"database_size_bytes={db_size} "
        f"database_increase_bytes={db_size - initial_db_size}"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--rows", type=int, default=DEFAULT_ROWS)
    parser.add_argument("--as-of", type=date.fromisoformat, default=DEFAULT_AS_OF)
    args = parser.parse_args(argv)
    if args.rows <= 0:
        parser.error("--rows must be positive")

    try:
        source_rows = load_rows(args.input)
        benchmark_rows = representative_rows(source_rows, args.rows)
        for index, row in enumerate(benchmark_rows):
            row["description"] = (
                str(row.get("description") or "")
                + f"\nOffline benchmark corpus row {index}."
            )
        with tempfile.TemporaryDirectory(
            prefix="internship-signal-analysis-cache-"
        ) as temp_dir:
            db_path = Path(temp_dir) / "seen.sqlite"
            with SeenStore(db_path):
                pass
            initial_db_size = db_path.stat().st_size
            disabled = run_case(
                "disabled",
                benchmark_rows,
                db_path=db_path,
                enabled=False,
                as_of=args.as_of,
                initial_db_size=initial_db_size,
            )
            cold = run_case(
                "empty",
                benchmark_rows,
                db_path=db_path,
                enabled=True,
                as_of=args.as_of,
                initial_db_size=initial_db_size,
            )
            warm = run_case(
                "warm",
                benchmark_rows,
                db_path=db_path,
                enabled=True,
                as_of=args.as_of,
                initial_db_size=initial_db_size,
            )
            payloads = {
                serialized(disabled),
                serialized(cold),
                serialized(warm),
            }
            identical = len(payloads) == 1
            print(
                "ANALYSIS-CACHE-EQUIVALENCE "
                f"rows={args.rows} "
                f"serialized_outputs_identical={str(identical).lower()}"
            )
            if not identical:
                return 1
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
