#!/usr/bin/env python3
"""Export a deterministic U.S.-focused role-fit benchmark."""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import os
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.ingest import analyze_rows  # noqa: E402
from watcher.config import DEFAULT_WATCHLIST_PATH, WatcherConfig, load_watchlist  # noqa: E402
from watcher.eligibility import OUTSIDE_US, assess_us_location, determine_watcher_eligibility  # noqa: E402
from watcher.run import collect_rows  # noqa: E402
from watcher.source_health import sanitize_error  # noqa: E402

from scripts.build_scoring_benchmark import (  # noqa: E402
    LABEL_FIELDS,
    baseline_prediction,
    candidate_pool as open_internship_pool,
    freeze_job,
    git_metadata,
    labels_row,
)
from scripts.evaluate_scoring_benchmark import evaluate_benchmark  # noqa: E402
from scripts.scoring_benchmark_common import (  # noqa: E402
    HUMAN_LABEL_COLUMNS,
    SCHEMA_VERSION,
    BenchmarkError,
    atomic_write_many,
    json_bytes,
    nonnegative_int,
    parse_date,
    render_csv_bytes,
    role_track,
    sha256_bytes,
    source_counts,
)

BENCHMARK_KIND = "us_rolefit"
GROUPS = ("random", "likely_match", "difficult_negative")
DEFAULT_RANDOM_COUNT = 160
DEFAULT_LIKELY_MATCH_COUNT = 80
DEFAULT_DIFFICULT_NEGATIVE_COUNT = 80

LIKELY_MATCH_TRACKS = (
    "general_swe",
    "backend",
    "frontend",
    "full_stack",
    "platform_infra",
    "cloud",
    "devops",
    "data_engineering",
    "ml_ai",
    "quant_dev",
    "embedded_software",
    "firmware",
    "sdet_qa_automation",
    "quality_test",
    "solutions_engineering",
)

DIFFICULT_TRACK_CATEGORIES = {
    "electrical_hardware": "electrical_hardware",
    "mechanical_manufacturing": "mechanical_manufacturing",
    "factory_automation": "manufacturing_automation",
    "civil_structural": "civil_structural",
    "quality_test": "nonsoftware_quality",
    "customer_experience": "customer_support",
    "product": "product",
    "non_technical": "nontechnical",
    "other_engineering": "other_engineering",
}

DIFFICULT_TITLE_PATTERNS = (
    ("naval_architecture", re.compile(r"\bnaval architect(?:ure)?\b", re.I)),
    ("product_design", re.compile(r"\bproduct design(?: engineering| engineer)?\b", re.I)),
    ("electrical_hardware", re.compile(
        r"\belectrical engineer(?:ing)?\b|\bhardware(?: design)? engineer(?:ing)?\b|"
        r"\bfpga\b|\bpcb\b|\b(?:rf|asic) engineer(?:ing)?\b",
        re.I,
    )),
    ("mechanical", re.compile(r"\bmechanical(?: design)? engineer(?:ing)?\b", re.I)),
    ("manufacturing", re.compile(
        r"\bmanufacturing engineer(?:ing)?\b|\bindustrial engineer(?:ing)?\b|"
        r"\bprocess engineer(?:ing)?\b",
        re.I,
    )),
    ("civil_structural", re.compile(r"\bcivil engineer(?:ing)?\b|\bstructural engineer(?:ing)?\b", re.I)),
    ("purchasing_operations", re.compile(
        r"\bpurchas(?:e|ing)\b|\bprocurement\b|\bbuyer\b|\bsupply chain\b|"
        r"\boperations? (?:intern|co[- ]?op)\b",
        re.I,
    )),
    ("marketing_nontechnical", re.compile(
        r"\bmarketing\b|\bhuman resources?\b|\brecruit(?:er|ing)\b|"
        r"\baccounting\b|\bcommunications?\b|\bpublic relations?\b",
        re.I,
    )),
    ("customer_support", re.compile(
        r"\bcustomer (?:support|success|experience)\b|\btechnical support\b|"
        r"\bhelp[- ]?desk\b|\bdesktop support\b",
        re.I,
    )),
)

QUALITY_TITLE_RE = re.compile(
    r"\bquality engineer(?:ing)?\b|\btest engineer(?:ing)?\b|"
    r"\bvalidation engineer(?:ing)?\b|\bverification engineer(?:ing)?\b",
    re.I,
)
SOFTWARE_QUALITY_RE = re.compile(
    r"\bsoftware\b|\bsdet\b|\bqa automation\b|\bautomated testing\b|"
    r"\btest automation\b|\bembedded\b|\bfirmware\b",
    re.I,
)


def candidate_pool(jobs: Sequence[dict]) -> list[dict]:
    """Keep open internships that the production location helper allows."""

    return [
        job
        for job in open_internship_pool(jobs)
        if assess_us_location(job).status != OUTSIDE_US
    ]


def sample_jobs(
    candidates: Sequence[dict],
    *,
    seed: int,
    random_count: int = DEFAULT_RANDOM_COUNT,
    likely_match_count: int = DEFAULT_LIKELY_MATCH_COUNT,
    difficult_negative_count: int = DEFAULT_DIFFICULT_NEGATIVE_COUNT,
) -> tuple[list[dict], dict[str, list[str]], dict[str, int]]:
    """Select independent U.S. role-fit cohorts and return their stable union."""

    if min(random_count, likely_match_count, difficult_negative_count) < 0:
        raise BenchmarkError("sample counts must be nonnegative")
    stable = sorted(_deduplicate_candidates(candidates).values(), key=lambda job: str(job["id"]))

    random_rng = random.Random(seed)
    random_selected = random_rng.sample(stable, min(random_count, len(stable)))

    likely_pool = [job for job in stable if _is_likely_match(job)]
    likely_selected = _stratified_sample(
        likely_pool,
        count=min(likely_match_count, len(likely_pool)),
        seed=seed ^ 0x11A11E,
        category=lambda job: role_track(job),
        category_order=LIKELY_MATCH_TRACKS,
    )

    difficult_pool = [
        job for job in stable if difficult_negative_category(job) is not None
    ]
    difficult_selected = _stratified_sample(
        difficult_pool,
        count=min(difficult_negative_count, len(difficult_pool)),
        seed=seed ^ 0xD1FF1C,
        category=lambda job: difficult_negative_category(job) or "other",
    )

    group_sets: dict[str, set[str]] = defaultdict(set)
    selected: list[dict] = []
    emitted: set[str] = set()
    for group, jobs in (
        ("random", random_selected),
        ("likely_match", likely_selected),
        ("difficult_negative", difficult_selected),
    ):
        for job in jobs:
            job_id = str(job["id"])
            group_sets[job_id].add(group)
            if job_id not in emitted:
                selected.append(job)
                emitted.add(job_id)

    memberships = {
        str(job["id"]): [group for group in GROUPS if group in group_sets[str(job["id"])]]
        for job in selected
    }
    available = {
        "random": len(stable),
        "likely_match": len(likely_pool),
        "difficult_negative": len(difficult_pool),
    }
    return selected, memberships, available


def difficult_negative_category(job: Mapping[str, object]) -> str | None:
    """Return a benchmark-only difficult-negative stratum, if applicable."""

    if job.get("degree_eligible") is False:
        return "graduate_only"
    title = str(job.get("title") or "")
    if QUALITY_TITLE_RE.search(title) and not SOFTWARE_QUALITY_RE.search(title):
        return "nonsoftware_quality"
    for category, pattern in DIFFICULT_TITLE_PATTERNS:
        if pattern.search(title):
            return category
    track = role_track(job)
    if track in DIFFICULT_TRACK_CATEGORIES:
        return DIFFICULT_TRACK_CATEGORIES[track]
    if not determine_watcher_eligibility(dict(job))["watcher_eligible"]:
        return "current_ineligible_other"
    return None


def _is_likely_match(job: Mapping[str, object]) -> bool:
    return (
        role_track(job) in LIKELY_MATCH_TRACKS
        and bool(determine_watcher_eligibility(dict(job))["watcher_eligible"])
    )


def _deduplicate_candidates(candidates: Sequence[dict]) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for job in candidates:
        job_id = str(job.get("id") or "")
        if not job_id:
            raise BenchmarkError("analyzed candidate is missing a stable job id")
        by_id.setdefault(job_id, job)
    return by_id


def _stratified_sample(
    candidates: Sequence[dict],
    *,
    count: int,
    seed: int,
    category: Callable[[dict], str],
    category_order: Sequence[str] = (),
) -> list[dict]:
    if count <= 0:
        return []
    buckets: dict[str, list[dict]] = defaultdict(list)
    for job in candidates:
        buckets[category(job)].append(job)
    ordered_categories = [
        *[name for name in category_order if name in buckets],
        *sorted(set(buckets) - set(category_order)),
    ]
    rng = random.Random(seed)
    for name in ordered_categories:
        buckets[name].sort(key=lambda job: str(job["id"]))
        rng.shuffle(buckets[name])

    selected: list[dict] = []
    positions = {name: 0 for name in ordered_categories}
    while len(selected) < count:
        progressed = False
        for name in ordered_categories:
            position = positions[name]
            bucket = buckets[name]
            if position >= len(bucket):
                continue
            selected.append(bucket[position])
            positions[name] += 1
            progressed = True
            if len(selected) >= count:
                break
        if not progressed:
            break
    return selected


def output_paths(prefix: str | Path) -> dict[str, Path]:
    prefix = Path(prefix)
    if not prefix.name:
        raise BenchmarkError("output prefix must include a filename prefix")
    return {
        "labels": Path(f"{prefix}_labels.csv"),
        "rows": Path(f"{prefix}_rows.jsonl"),
        "predictions": Path(f"{prefix}_predictions.json"),
        "manifest": Path(f"{prefix}_manifest.json"),
        "report": Path(f"{prefix}_report.md"),
        "metrics": Path(f"{prefix}_metrics.json"),
    }


def export_benchmark(
    *,
    watchlist_path: str | Path,
    as_of: date,
    seed: int,
    output_prefix: str | Path,
    random_count: int = DEFAULT_RANDOM_COUNT,
    likely_match_count: int = DEFAULT_LIKELY_MATCH_COUNT,
    difficult_negative_count: int = DEFAULT_DIFFICULT_NEGATIVE_COUNT,
    collector: Callable[[WatcherConfig], tuple[list[dict], list[str]]] | None = None,
    analyzer: Callable[..., list[dict]] | None = None,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Collect, freeze, and write the U.S. role-fit benchmark plus empty evaluation."""

    config = load_watchlist(watchlist_path)
    rows, source_errors = (collector or collect_rows)(config)
    if not rows:
        raise BenchmarkError("source collection produced no rows; no benchmark was written")
    jobs = (analyzer or analyze_rows)(rows, today=as_of)
    open_candidates = open_internship_pool(jobs)
    candidates = candidate_pool(jobs)
    if not candidates:
        raise BenchmarkError("no U.S. or location-ambiguous internship candidates remained")
    selected, memberships, available = sample_jobs(
        candidates,
        seed=seed,
        random_count=random_count,
        likely_match_count=likely_match_count,
        difficult_negative_count=difficult_negative_count,
    )
    if not selected:
        raise BenchmarkError("all requested cohort sizes are zero; no benchmark was written")

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
    selected_ids_payload = (
        "\n".join(str(job["id"]) for job in selected) + "\n"
    ).encode("utf-8")

    observed = created_at or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    git_commit, git_dirty = git_metadata(REPO_ROOT)
    requested = {
        "random": random_count,
        "likely_match": likely_match_count,
        "difficult_negative": difficult_negative_count,
    }
    actual = {
        group: sum(group in memberships[str(job["id"])] for job in selected)
        for group in GROUPS
    }
    location_counts = Counter(assess_us_location(job).status for job in selected)
    candidate_location_counts = Counter(assess_us_location(job).status for job in candidates)
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_kind": BENCHMARK_KIND,
        "created_at_utc": observed.isoformat().replace("+00:00", "Z"),
        "as_of_date": as_of.isoformat(),
        "seed": seed,
        "frozen_git_commit": git_commit,
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "watchlist_path": str(Path(watchlist_path).as_posix()),
        "configured_terms": list(config.terms),
        "rows_collected": len(rows),
        "jobs_scored": len(jobs),
        "open_internship_pool_size": len(open_candidates),
        "outside_us_excluded_count": len(open_candidates) - len(candidates),
        "candidate_pool_count": len(candidates),
        "candidate_pool_size": len(candidates),
        "selected_row_count": len(selected),
        "selected_count": len(selected),
        "requested_group_counts": requested,
        "available_group_counts": available,
        "actual_group_counts": actual,
        "cohort_overlap_counts": _overlap_counts(memberships),
        "expected_model_positive_count": sum(_model_positive(job) for job in selected),
        "expected_model_positive_by_cohort": {
            group: sum(
                group in memberships[str(job["id"])] and _model_positive(job)
                for job in selected
            )
            for group in GROUPS
        },
        "source_counts": source_counts(selected),
        "company_counts": dict(sorted(Counter(str(job.get("company") or "unknown") for job in selected).items())),
        "role_track_counts": dict(sorted(Counter(role_track(job) for job in selected).items())),
        "location_status_counts": dict(sorted(location_counts.items())),
        "candidate_location_status_counts": dict(sorted(candidate_location_counts.items())),
        "location_gate": {
            "helper": "watcher.eligibility.assess_us_location",
            "allowed_statuses": ["us", "ambiguous"],
            "excluded_status": OUTSIDE_US,
            "all_selected_passed_or_ambiguous": all(
                assess_us_location(job).status != OUTSIDE_US for job in selected
            ),
        },
        "target_shortfalls": {
            group: {
                "requested": requested[group],
                "available": available[group],
                "selected": actual[group],
            }
            for group in GROUPS
            if actual[group] < requested[group]
        },
        "source_errors": [sanitize_error(error) for error in source_errors],
        "output_files": {name: str(path.as_posix()) for name, path in paths.items()},
        "hashes": {
            "blank_labels_sha256": sha256_bytes(labels_payload),
            "selected_job_ids_sha256": sha256_bytes(selected_ids_payload),
            "frozen_rows_sha256": sha256_bytes(rows_payload),
            "baseline_predictions_sha256": sha256_bytes(predictions_payload),
            "watchlist_sha256": sha256_bytes(Path(watchlist_path).read_bytes()),
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
    validate_frozen_artifact_hashes(manifest, paths)
    evaluate_benchmark(
        labels_path=paths["labels"],
        rows_path=paths["rows"],
        manifest_path=paths["manifest"],
        baseline_predictions_path=paths["predictions"],
        report_path=paths["report"],
        metrics_path=paths["metrics"],
        allow_partial_labels=True,
    )
    return manifest


def _model_positive(job: Mapping[str, object]) -> bool:
    return bool(determine_watcher_eligibility(dict(job))["watcher_eligible"])


def _overlap_counts(memberships: Mapping[str, Sequence[str]]) -> dict[str, object]:
    combinations = Counter("+".join(groups) for groups in memberships.values())
    pairwise = {
        f"{left}+{right}": sum(
            left in groups and right in groups for groups in memberships.values()
        )
        for left, right in itertools.combinations(GROUPS, 2)
    }
    return {
        "rows_in_multiple_cohorts": sum(len(groups) > 1 for groups in memberships.values()),
        "membership_combinations": dict(sorted(combinations.items())),
        "pairwise": pairwise,
    }


def validate_frozen_artifact_hashes(
    manifest: Mapping[str, object],
    paths: Mapping[str, Path],
) -> None:
    """Validate every immutable payload written by this exporter."""

    hashes = manifest.get("hashes")
    if not isinstance(hashes, Mapping):
        raise BenchmarkError("manifest hashes must be an object")
    expected = {
        "blank_labels_sha256": paths["labels"],
        "frozen_rows_sha256": paths["rows"],
        "baseline_predictions_sha256": paths["predictions"],
    }
    for key, path in expected.items():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != str(hashes.get(key) or ""):
            raise BenchmarkError(f"{key} mismatch for {path}")
    with paths["labels"].open("r", encoding="utf-8-sig", newline="") as handle:
        selected_ids = [str(row.get("job_id") or "") for row in csv.DictReader(handle)]
    selected_payload = ("\n".join(selected_ids) + "\n").encode("utf-8")
    if sha256_bytes(selected_payload) != str(hashes.get("selected_job_ids_sha256") or ""):
        raise BenchmarkError("selected_job_ids_sha256 mismatch")
    watchlist = Path(str(manifest.get("watchlist_path") or ""))
    if not watchlist.is_absolute():
        watchlist = REPO_ROOT / watchlist
    try:
        watchlist_payload = watchlist.read_bytes()
    except OSError as exc:
        raise BenchmarkError(f"cannot validate frozen watchlist: {watchlist}") from exc
    if sha256_bytes(watchlist_payload) != str(hashes.get("watchlist_sha256") or ""):
        raise BenchmarkError(f"watchlist_sha256 mismatch for {watchlist}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a deterministic U.S.-location role-fit benchmark."
    )
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH))
    parser.add_argument("--as-of", required=True, type=parse_date)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument("--random-count", type=nonnegative_int, default=DEFAULT_RANDOM_COUNT)
    parser.add_argument(
        "--likely-match-count",
        type=nonnegative_int,
        default=DEFAULT_LIKELY_MATCH_COUNT,
    )
    parser.add_argument(
        "--difficult-negative-count",
        type=nonnegative_int,
        default=DEFAULT_DIFFICULT_NEGATIVE_COUNT,
    )
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
            likely_match_count=args.likely_match_count,
            difficult_negative_count=args.difficult_negative_count,
        )
    except (BenchmarkError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        "U.S. role-fit benchmark exported: "
        f"rows={manifest['rows_collected']}, candidates={manifest['candidate_pool_count']}, "
        f"selected={manifest['selected_count']}, source_errors={len(manifest['source_errors'])}."
    )
    if manifest["target_shortfalls"]:
        print(f"Sampling limitations: {manifest['target_shortfalls']}")
    for path in manifest["output_files"].values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
