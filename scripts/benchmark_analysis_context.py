#!/usr/bin/env python3
"""Benchmark the offline posting-analysis path at fixed representative sizes."""

from __future__ import annotations

import argparse
import copy
import gc
import json
import sys
from datetime import date
from pathlib import Path
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.ingest import _analyze_rows_with_report, analyze_rows  # noqa: E402


BASELINE_SECONDS = {
    500: 7.400,
    1_000: 14.658,
    2_000: 29.307,
}
DEFAULT_INPUT = (
    REPO_ROOT
    / "evaluation"
    / "private"
    / "scoring_us_rolefit_20260726_rows.jsonl"
)
DEFAULT_AS_OF = date(2026, 7, 30)


def load_rows(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number} is not a JSON object")
            rows.append(value)
    if not rows:
        raise ValueError("benchmark input has no rows")
    return rows


def representative_rows(rows: list[dict], count: int) -> list[dict]:
    expanded = []
    for index in range(count):
        row = copy.deepcopy(rows[index % len(rows)])
        extra = row.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        else:
            extra = dict(extra)
        extra.update(
            {
                "source": "direct",
                "source_adapter": "greenhouse",
                "source_requisition_id": f"analysis-benchmark-{index}",
            }
        )
        row["extra"] = extra
        row["source_url"] = f"https://benchmark.invalid/postings/{index}"
        expanded.append(row)
    return expanded


def serialized(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def benchmark(
    rows: list[dict],
    *,
    as_of: date,
    counts: tuple[int, ...],
) -> list[dict]:
    results = []
    for count in counts:
        baseline = BASELINE_SECONDS[count]
        sample = representative_rows(rows, count)
        gc.collect()
        started = perf_counter()
        jobs = analyze_rows(sample, today=as_of)
        elapsed = perf_counter() - started
        improvement = (baseline - elapsed) / baseline * 100
        result = {
            "rows": count,
            "jobs": len(jobs),
            "baseline_seconds": baseline,
            "optimized_seconds": elapsed,
            "improvement_percent": improvement,
        }
        results.append(result)
        print(
            "ANALYSIS-BENCHMARK "
            f"rows={count} jobs={len(jobs)} "
            f"baseline_seconds={baseline:.3f} "
            f"optimized_seconds={elapsed:.3f} "
            f"improvement_percent={improvement:.1f}"
        )
    return results


def verify_equivalence(rows: list[dict], *, as_of: date) -> None:
    optimized = _analyze_rows_with_report(
        copy.deepcopy(rows),
        today=as_of,
        use_analysis_context=True,
    )
    reference = _analyze_rows_with_report(
        copy.deepcopy(rows),
        today=as_of,
        use_analysis_context=False,
    )
    identical = serialized(optimized) == serialized(reference)
    print(
        "ANALYSIS-EQUIVALENCE "
        f"rows={len(rows)} serialized_outputs_identical={str(identical).lower()}"
    )
    if not identical:
        raise RuntimeError("context and context-free analysis outputs differ")


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--as-of", type=parse_date, default=DEFAULT_AS_OF)
    parser.add_argument(
        "--counts",
        nargs="+",
        type=int,
        choices=tuple(BASELINE_SECONDS),
        default=tuple(BASELINE_SECONDS),
    )
    args = parser.parse_args(argv)

    try:
        rows = load_rows(args.input)
        verify_equivalence(rows, as_of=args.as_of)
        benchmark(rows, as_of=args.as_of, counts=tuple(args.counts))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
