#!/usr/bin/env python3
"""Export a deterministic, blind-label scoring benchmark from watcher rows."""

from __future__ import annotations

import argparse
import os
import random
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.ingest import analyze_rows  # noqa: E402
from backend.app.normalize import CANONICAL_COLUMNS  # noqa: E402
from watcher.config import DEFAULT_WATCHLIST_PATH, WatcherConfig, load_watchlist  # noqa: E402
from watcher.filters import is_internship, is_open  # noqa: E402
from watcher.run import collect_rows  # noqa: E402
from watcher.source_health import sanitize_error, sanitize_feed_label  # noqa: E402

from scripts.scoring_benchmark_common import (  # noqa: E402
    GROUP_ORDER,
    HUMAN_LABEL_COLUMNS,
    SCHEMA_VERSION,
    BenchmarkError,
    atomic_write_many,
    json_bytes,
    ordered_groups,
    prediction_from_job,
    ranking_key,
    render_csv_bytes,
    role_track,
    score_value,
    sha256_bytes,
)

DEFAULT_RANDOM_COUNT = 100
DEFAULT_TOP_COUNT = 30
DEFAULT_DIFFICULT_COUNT = 50

DIFFICULT_ROLE_TRACKS = (
    "unknown",
    "general_swe",
    "data_engineering",
    "ml_ai",
    "cloud",
    "devops",
    "embedded_software",
    "firmware",
    "sdet_qa_automation",
    "quality_test",
    "it_support",
    "solutions_engineering",
    "electrical_hardware",
    "mechanical_manufacturing",
    "factory_automation",
    "civil_structural",
    "customer_experience",
    "non_technical",
)

DIFFICULT_SCORE_BANDS = (
    ("1-24", 1, 24),
    ("25-44", 25, 44),
    ("45-69", 45, 69),
    ("70-84", 70, 84),
    ("85-100", 85, 100),
)

LABEL_FIELDS = [
    "job_id",
    "sample_groups",
    "company",
    "title",
    "location",
    "remote_status",
    "internship_type",
    "compensation_display",
    "description",
    "requirements",
    "source_url",
    "date_posted",
    "deadline",
    "source",
    "source_adapter",
    *HUMAN_LABEL_COLUMNS,
]

SAFE_EXTRA_FIELDS = frozenset({"source", "source_adapter", "feed_url", "active"})


def candidate_pool(jobs: Sequence[dict]) -> list[dict]:
    """Return open internships without applying watcher eligibility."""

    return [job for job in jobs if is_internship(job) and is_open(job)]


def sample_jobs(
    candidates: Sequence[dict],
    *,
    seed: int,
    random_count: int = DEFAULT_RANDOM_COUNT,
    top_count: int = DEFAULT_TOP_COUNT,
    difficult_count: int = DEFAULT_DIFFICULT_COUNT,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Select independent cohorts and return their stable ordered union.

    Independent selection preserves the random cohort's population meaning.
    Cohort overlap is retained as multiple memberships while each job is
    emitted once, in random/top/difficult first-selection order.
    """

    if min(random_count, top_count, difficult_count) < 0:
        raise BenchmarkError("sample counts must be nonnegative")
    by_id = _unique_candidates(candidates)
    stable = sorted(by_id.values(), key=lambda job: str(job["id"]))

    random_rng = random.Random(seed)
    random_selected = random_rng.sample(stable, min(random_count, len(stable)))
    top_selected = sorted(stable, key=ranking_key)[: min(top_count, len(stable))]
    difficult_selected = _difficult_sample(
        stable,
        count=min(difficult_count, len(stable)),
        seed=seed,
    )

    groups: dict[str, set[str]] = defaultdict(set)
    selected: list[dict] = []
    emitted: set[str] = set()
    for group, jobs in (
        ("random", random_selected),
        ("top", top_selected),
        ("difficult", difficult_selected),
    ):
        for job in jobs:
            job_id = str(job["id"])
            groups[job_id].add(group)
            if job_id not in emitted:
                selected.append(job)
                emitted.add(job_id)

    memberships = {
        str(job["id"]): ordered_groups(groups[str(job["id"])])
        for job in selected
    }
    return selected, memberships


def _unique_candidates(candidates: Sequence[dict]) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for job in candidates:
        job_id = str(job.get("id") or "")
        if not job_id:
            raise BenchmarkError("analyzed candidate is missing a stable job id")
        if job_id in by_id:
            raise BenchmarkError(f"analyzed candidate pool contains duplicate job id: {job_id}")
        by_id[job_id] = job
    return by_id


def _difficult_sample(candidates: Sequence[dict], *, count: int, seed: int) -> list[dict]:
    if count <= 0:
        return []
    rng = random.Random(seed ^ 0x5C0A1E)
    strata: list[list[dict]] = []

    for track in DIFFICULT_ROLE_TRACKS:
        bucket = [job for job in candidates if role_track(job) == track]
        bucket.sort(key=lambda job: str(job["id"]))
        rng.shuffle(bucket)
        if bucket:
            strata.append(bucket)
    for _label, lower, upper in DIFFICULT_SCORE_BANDS:
        bucket = [
            job for job in candidates
            if lower <= score_value(job, "fit_score") <= upper
        ]
        bucket.sort(key=lambda job: str(job["id"]))
        rng.shuffle(bucket)
        if bucket:
            strata.append(bucket)

    selected: list[dict] = []
    seen: set[str] = set()
    positions = [0] * len(strata)
    while len(selected) < count:
        progressed = False
        for index, bucket in enumerate(strata):
            while positions[index] < len(bucket):
                job = bucket[positions[index]]
                positions[index] += 1
                job_id = str(job["id"])
                if job_id in seen:
                    continue
                selected.append(job)
                seen.add(job_id)
                progressed = True
                break
            if len(selected) >= count:
                break
        if not progressed:
            break
    return selected


def freeze_job(job: Mapping[str, object]) -> dict[str, object]:
    compensation = job.get("compensation")
    raw_compensation = compensation.get("raw", "") if isinstance(compensation, Mapping) else compensation
    frozen = {
        field: str(job.get(field) or "")
        for field in CANONICAL_COLUMNS
    }
    frozen["compensation"] = str(raw_compensation or "")
    frozen["extra"] = _safe_extra(job.get("extra"))
    return frozen


def _safe_extra(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    extra: dict[str, object] = {}
    for key in SAFE_EXTRA_FIELDS:
        item = value.get(key)
        if key == "active" and isinstance(item, bool):
            extra[key] = item
        elif key == "feed_url" and item:
            extra[key] = sanitize_feed_label(item)
        elif key != "active" and isinstance(item, str) and item.strip():
            extra[key] = item.strip()
    return extra


def baseline_prediction(job: Mapping[str, object], groups: Sequence[str]) -> dict[str, object]:
    return prediction_from_job(job, groups)


def labels_row(job: Mapping[str, object], groups: Sequence[str]) -> dict[str, object]:
    extra = job.get("extra") if isinstance(job.get("extra"), Mapping) else {}
    row = {
        "job_id": str(job.get("id") or ""),
        "sample_groups": "|".join(groups),
        "company": job.get("company", ""),
        "title": job.get("title", ""),
        "location": job.get("location", ""),
        "remote_status": job.get("remote_status", ""),
        "internship_type": job.get("internship_type", ""),
        "compensation_display": compensation_display(job.get("compensation")),
        "description": job.get("description", ""),
        "requirements": job.get("requirements", ""),
        "source_url": job.get("source_url", ""),
        "date_posted": job.get("date_posted", ""),
        "deadline": job.get("deadline", ""),
        "source": extra.get("source", ""),
        "source_adapter": extra.get("source_adapter", ""),
    }
    row.update({field: "" for field in HUMAN_LABEL_COLUMNS})
    return row


def compensation_display(value: object) -> str:
    if not isinstance(value, Mapping):
        return str(value or "")
    raw = str(value.get("raw") or "").strip()
    if raw:
        return raw
    minimum = value.get("usd_hourly_min")
    maximum = value.get("usd_hourly_max")
    if minimum is not None:
        return f"${float(minimum):g}-${float(maximum if maximum is not None else minimum):g}/hr USD"
    kind = str(value.get("kind") or "unknown").replace("_", " ")
    return "" if kind == "unknown" else kind


def output_paths(prefix: str | Path) -> dict[str, Path]:
    prefix = Path(prefix)
    if not prefix.name:
        raise BenchmarkError("output prefix must include a filename prefix")
    return {
        "labels": Path(f"{prefix}_labels.csv"),
        "rows": Path(f"{prefix}_rows.jsonl"),
        "predictions": Path(f"{prefix}_predictions.json"),
        "manifest": Path(f"{prefix}_manifest.json"),
    }


def export_benchmark(
    *,
    watchlist_path: str | Path,
    as_of: date,
    seed: int,
    output_prefix: str | Path,
    random_count: int = DEFAULT_RANDOM_COUNT,
    top_count: int = DEFAULT_TOP_COUNT,
    difficult_count: int = DEFAULT_DIFFICULT_COUNT,
    collector: Callable[[WatcherConfig], tuple[list[dict], list[str]]] | None = None,
    analyzer: Callable[..., list[dict]] | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    config = load_watchlist(watchlist_path)
    active_collector = collector or collect_rows
    active_analyzer = analyzer or analyze_rows
    rows, source_errors = active_collector(config)
    if not rows:
        raise BenchmarkError("source collection produced no rows; no benchmark was written")
    jobs = active_analyzer(rows, today=as_of)
    candidates = candidate_pool(jobs)
    if not candidates:
        raise BenchmarkError("no open internship candidates remained after analysis")
    selected, memberships = sample_jobs(
        candidates,
        seed=seed,
        random_count=random_count,
        top_count=top_count,
        difficult_count=difficult_count,
    )

    paths = output_paths(output_prefix)
    labels = [labels_row(job, memberships[str(job["id"])]) for job in selected]
    frozen_rows = [freeze_job(job) for job in selected]
    predictions = {
        str(job["id"]): baseline_prediction(job, memberships[str(job["id"])])
        for job in selected
    }
    labels_payload = render_csv_bytes(LABEL_FIELDS, labels)
    rows_payload = b"".join(json_bytes(row, indent=None) for row in frozen_rows)
    predictions_payload = json_bytes(predictions)
    selected_ids_payload = ("\n".join(str(job["id"]) for job in selected) + "\n").encode("utf-8")
    git_commit, git_dirty = git_metadata(REPO_ROOT)
    observed = created_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)

    requested = {
        "random": random_count,
        "top": top_count,
        "difficult": difficult_count,
    }
    actual = {
        group: sum(group in memberships[str(job["id"])] for job in selected)
        for group in GROUP_ORDER
    }
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": observed.isoformat().replace("+00:00", "Z"),
        "as_of_date": as_of.isoformat(),
        "seed": seed,
        "watchlist_path": str(Path(watchlist_path).as_posix()),
        "configured_terms": list(config.terms),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "rows_collected": len(rows),
        "jobs_scored": len(jobs),
        "candidate_pool_size": len(candidates),
        "selected_count": len(selected),
        "requested_group_counts": requested,
        "actual_group_counts": actual,
        "source_errors": [sanitize_error(error) for error in source_errors],
        "output_files": {name: str(path.as_posix()) for name, path in paths.items()},
        "hashes": {
            "selected_job_ids_sha256": sha256_bytes(selected_ids_payload),
            "frozen_rows_sha256": sha256_bytes(rows_payload),
            "baseline_predictions_sha256": sha256_bytes(predictions_payload),
        },
    }
    manifest_payload = json_bytes(manifest)
    atomic_write_many(
        {
            paths["labels"]: labels_payload,
            paths["rows"]: rows_payload,
            paths["predictions"]: predictions_payload,
            paths["manifest"]: manifest_payload,
        }
    )
    return manifest


def git_metadata(repo_root: Path) -> tuple[str, bool | str]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() or "unknown"
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return commit, bool(status.strip())
    except (OSError, subprocess.SubprocessError):
        return "unknown", "unknown"


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sample count must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("sample count must be nonnegative")
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a deterministic watcher scoring benchmark.")
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH))
    parser.add_argument("--as-of", required=True, type=parse_date)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--random-count", type=nonnegative_int, default=DEFAULT_RANDOM_COUNT)
    parser.add_argument("--top-count", type=nonnegative_int, default=DEFAULT_TOP_COUNT)
    parser.add_argument("--difficult-count", type=nonnegative_int, default=DEFAULT_DIFFICULT_COUNT)
    args = parser.parse_args(argv)

    os.environ["WATCHER_SEND_EMAIL"] = "0"
    print("BENCHMARK-ONLY MODE: email, alumni loading, seen state, and watcher-data are disabled.")
    try:
        manifest = export_benchmark(
            watchlist_path=args.watchlist,
            as_of=args.as_of,
            seed=args.seed,
            output_prefix=args.output_prefix,
            random_count=args.random_count,
            top_count=args.top_count,
            difficult_count=args.difficult_count,
        )
    except (BenchmarkError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        "Benchmark exported: "
        f"rows={manifest['rows_collected']}, candidates={manifest['candidate_pool_size']}, "
        f"selected={manifest['selected_count']}, source_errors={len(manifest['source_errors'])}."
    )
    for path in manifest["output_files"].values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
