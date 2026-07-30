#!/usr/bin/env python3
"""Build an independent, blind U.S. role-fit holdout benchmark.

This exporter is measurement-only. It uses normal collection and analysis but
never invokes digest filtering, alumni matching, notification, or seen state.
It refuses to collect unless the tracked tree is clean at the expected commit.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import os
import random
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.dedupe import canonical_key, job_id, norm_url  # noqa: E402
from backend.app.ingest import analyze_rows  # noqa: E402
from watcher.config import DEFAULT_WATCHLIST_PATH, WatcherConfig, load_watchlist  # noqa: E402
from watcher.eligibility import (  # noqa: E402
    LOCATION_AMBIGUOUS,
    OUTSIDE_US,
    assess_us_location,
    determine_watcher_eligibility,
)
from watcher.run import collect_rows  # noqa: E402
from watcher.source_health import sanitize_error, sanitize_feed_label  # noqa: E402

from scripts.build_scoring_benchmark import (  # noqa: E402
    LABEL_FIELDS,
    baseline_prediction,
    candidate_pool as open_internship_pool,
    freeze_job,
    git_metadata,
    labels_row,
)
from scripts.evaluate_scoring_benchmark import (  # noqa: E402
    evaluate_benchmark,
    load_frozen_rows,
    load_manifest,
    load_predictions,
)
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
    score_value,
    sha256_bytes,
    source_counts,
)

BENCHMARK_KIND = "us_holdout"
CONSTRUCTION_VERSION = 1
GROUPS = ("random", "likely_match", "difficult_negative")
DEFAULT_RANDOM_COUNT = 180
DEFAULT_LIKELY_MATCH_COUNT = 50
DEFAULT_DIFFICULT_NEGATIVE_COUNT = 80
EVALUATION_PRIVATE = REPO_ROOT / "evaluation" / "private"
DEFAULT_PRIOR_PREFIXES = (
    EVALUATION_PRIVATE / "scoring_20260724",
    EVALUATION_PRIVATE / "scoring_us_rolefit_20260726",
)

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
    "technical_product",
    "embedded_software",
    "firmware",
    "sdet_qa_automation",
    "it_support",
    "solutions_engineering",
)

DIFFICULT_TITLE_PATTERNS = (
    ("naval_architecture", re.compile(r"\bnaval architect(?:ure)?\b", re.I)),
    (
        "physical_product_design",
        re.compile(
            r"\bproduct design(?: engineering| engineer)?\b|"
            r"\bindustrial design(?:er| engineering)?\b",
            re.I,
        ),
    ),
    (
        "electrical_hardware",
        re.compile(
            r"\belectrical engineer(?:ing)?\b|\belectronics? engineer(?:ing)?\b|"
            r"\bhardware(?: design)? engineer(?:ing)?\b|\bfpga\b|\bpcb\b|"
            r"\b(?:rf|asic) engineer(?:ing)?\b",
            re.I,
        ),
    ),
    (
        "mechanical_manufacturing",
        re.compile(
            r"\bmechanical(?: design)? engineer(?:ing)?\b|"
            r"\bmanufacturing engineer(?:ing)?\b|\bindustrial engineer(?:ing)?\b|"
            r"\bprocess engineer(?:ing)?\b|\bfactory automation\b",
            re.I,
        ),
    ),
    (
        "quality_engineering",
        re.compile(
            r"\bquality engineer(?:ing)?\b|\btest engineer(?:ing)?\b|"
            r"\bvalidation engineer(?:ing)?\b|\bverification engineer(?:ing)?\b",
            re.I,
        ),
    ),
    (
        "consumer_market_research",
        re.compile(
            r"\bconsumer insights?\b|\bmarket research\b|\bsurvey research\b|"
            r"\bcustomer insights?\b",
            re.I,
        ),
    ),
    (
        "product_roles",
        re.compile(
            r"\bproduct (?:management|manager|development|design)\b|"
            r"\bassociate product manager\b|\bapm intern",
            re.I,
        ),
    ),
    (
        "digital_operations",
        re.compile(
            r"\bdigital solutions?\b|\bworkflow automation\b|"
            r"\boperations? (?:intern|co[- ]?op)\b|\bbusiness operations?\b",
            re.I,
        ),
    ),
    (
        "quant_finance_research",
        re.compile(
            r"\bquant(?:itative)?\b|\bsystematic\b|\btrading\b|"
            r"\bfinancial (?:analysis|modeling|research)\b",
            re.I,
        ),
    ),
    (
        "nontechnical",
        re.compile(
            r"\bmarketing\b|\bhuman resources?\b|\brecruit(?:er|ing)\b|"
            r"\baccounting\b|\bcommunications?\b|\bpublic relations?\b|"
            r"\bsales\b|\bprocurement\b|\bsupply chain\b",
            re.I,
        ),
    ),
)

_SAFE_EXTRA_SCALARS = frozenset(
    {
        "source",
        "source_adapter",
        "primary_source",
        "source_name",
        "source_format",
        "source_added_date",
    }
)
_SAFE_EXTRA_BOOLEANS = frozenset(
    {
        "active",
        "closed",
        "no_sponsorship",
        "us_citizenship_required",
    }
)
_SAFE_EXTRA_STRUCTURES = frozenset(
    {
        "sources",
        "source_details",
        "location",
        "locations",
        "country",
        "country_code",
        "normalized_location",
        "normalized_locations",
        "raw_location",
        "raw_locations",
        "posting_metadata",
        "source_metadata",
    }
)
_FORBIDDEN_METADATA_KEY = re.compile(
    r"alumni|contact|cookie|credential|email|linkedin|password|recipient|"
    r"secret|seen|smtp|authorization|api[_-]?key|access[_-]?token|"
    r"(?:^|[_-])token(?:$|[_-])",
    re.I,
)


@dataclass(frozen=True)
class PriorBenchmarkIndex:
    """Leakage keys and validated input metadata from prior frozen benchmarks."""

    job_ids: frozenset[str]
    normalized_urls: frozenset[str]
    fallback_keys: frozenset[str]
    inputs: tuple[Mapping[str, object], ...]


def require_clean_expected_commit(
    expected_commit: str,
    *,
    git_state: Callable[[], tuple[str, bool | str]] | None = None,
) -> str:
    """Require an exact known commit and no tracked modifications."""

    commit, dirty = (git_state or (lambda: git_metadata(REPO_ROOT)))()
    if commit == "unknown" or dirty == "unknown":
        raise BenchmarkError("cannot verify Git commit and tracked working-tree state")
    if commit != expected_commit:
        raise BenchmarkError(
            f"HEAD mismatch: expected {expected_commit}, found {commit}"
        )
    if dirty is not False:
        raise BenchmarkError(
            "tracked working tree is dirty; refusing holdout collection and freeze"
        )
    return commit


def prior_paths(prefix: str | Path) -> dict[str, Path]:
    prefix = _repo_path(prefix)
    return {
        "rows": Path(f"{prefix}_rows.jsonl"),
        "predictions": Path(f"{prefix}_predictions.json"),
        "manifest": Path(f"{prefix}_manifest.json"),
    }


def load_prior_benchmark_index(
    prefixes: Sequence[str | Path],
    *,
    private_root: str | Path = EVALUATION_PRIVATE,
) -> PriorBenchmarkIndex:
    """Load only prior rows/predictions/manifests and validate their hashes."""

    if not prefixes:
        raise BenchmarkError("at least one prior benchmark prefix is required")
    all_ids: set[str] = set()
    all_urls: set[str] = set()
    all_fallbacks: set[str] = set()
    inputs: list[Mapping[str, object]] = []

    for raw_prefix in prefixes:
        prefix = Path(raw_prefix)
        _require_private_path(prefix, private_root)
        paths = prior_paths(prefix)
        for path in paths.values():
            _require_private_path(path, private_root)

        manifest = load_manifest(paths["manifest"])
        rows = load_frozen_rows(paths["rows"])
        predictions = load_predictions(paths["predictions"])
        _verify_declared_hash(manifest, paths["rows"], "frozen_rows_sha256")
        _verify_declared_hash(
            manifest,
            paths["predictions"],
            "baseline_predictions_sha256",
        )

        row_ids = [job_id(row) for row in rows]
        prediction_ids = set(predictions)
        if len(row_ids) != len(set(row_ids)):
            raise BenchmarkError(f"prior benchmark contains duplicate stable IDs: {prefix}")
        if set(row_ids) != prediction_ids:
            raise BenchmarkError(
                f"prior rows/predictions do not join by stable ID: {prefix}"
            )
        selected_count = manifest.get("selected_count")
        if selected_count is not None and int(selected_count) != len(row_ids):
            raise BenchmarkError(
                f"prior manifest selected_count does not match rows: {prefix}"
            )
        _verify_selected_ids_hash(manifest, row_ids, prefix)

        for row, stable_id in zip(rows, row_ids):
            all_ids.add(stable_id)
            normalized = norm_url(str(row.get("source_url") or ""))
            if normalized:
                all_urls.add(normalized)
            fallback = canonical_key(row)
            if fallback.strip("|"):
                all_fallbacks.add(fallback)

        inputs.append(
            {
                "prefix": _display_path(prefix),
                "selected_count": len(row_ids),
                "manifest_sha256": sha256_bytes(paths["manifest"].read_bytes()),
                "rows_sha256": sha256_bytes(paths["rows"].read_bytes()),
                "predictions_sha256": sha256_bytes(
                    paths["predictions"].read_bytes()
                ),
                "manifest_path": _display_path(paths["manifest"]),
                "rows_path": _display_path(paths["rows"]),
                "predictions_path": _display_path(paths["predictions"]),
            }
        )

    return PriorBenchmarkIndex(
        job_ids=frozenset(all_ids),
        normalized_urls=frozenset(all_urls),
        fallback_keys=frozenset(all_fallbacks),
        inputs=tuple(inputs),
    )


def qualifying_candidate_pool(jobs: Sequence[dict]) -> tuple[list[dict], int]:
    """Return open U.S./ambiguous internships and the outside-U.S. count."""

    open_candidates = open_internship_pool(jobs)
    qualifying = [
        job
        for job in open_candidates
        if assess_us_location(job).status != OUTSIDE_US
    ]
    return qualifying, len(open_candidates) - len(qualifying)


def exclude_prior_overlaps(
    candidates: Sequence[dict],
    prior: PriorBenchmarkIndex,
) -> tuple[list[dict], dict[str, int]]:
    """Exclude prior rows by all three leakage keys and report raw matches."""

    remaining: list[dict] = []
    matched_job_id = 0
    matched_url = 0
    matched_fallback = 0
    excluded_unique = 0
    for job in candidates:
        stable_id = str(job.get("id") or "")
        if not stable_id:
            raise BenchmarkError("analyzed candidate is missing a stable job id")
        normalized = norm_url(str(job.get("source_url") or ""))
        fallback = canonical_key(job)
        id_hit = stable_id in prior.job_ids
        url_hit = bool(normalized and normalized in prior.normalized_urls)
        fallback_hit = bool(
            fallback.strip("|") and fallback in prior.fallback_keys
        )
        matched_job_id += int(id_hit)
        matched_url += int(url_hit)
        matched_fallback += int(fallback_hit)
        if id_hit or url_hit or fallback_hit:
            excluded_unique += 1
        else:
            remaining.append(job)
    return remaining, {
        "job_id": matched_job_id,
        "normalized_url": matched_url,
        "fallback_company_title_location": matched_fallback,
        "unique_candidates_excluded": excluded_unique,
        "candidates_remaining": len(remaining),
    }


def sample_jobs(
    candidates: Sequence[dict],
    *,
    seed: int,
    random_count: int = DEFAULT_RANDOM_COUNT,
    likely_match_count: int = DEFAULT_LIKELY_MATCH_COUNT,
    difficult_negative_count: int = DEFAULT_DIFFICULT_NEGATIVE_COUNT,
) -> tuple[list[dict], dict[str, list[str]], dict[str, int]]:
    """Select independent cohorts and return their stable deduplicated union."""

    if min(random_count, likely_match_count, difficult_negative_count) < 0:
        raise BenchmarkError("sample counts must be nonnegative")
    stable_by_id: dict[str, dict] = {}
    for job in candidates:
        stable_id = str(job.get("id") or "")
        if not stable_id:
            raise BenchmarkError("analyzed candidate is missing a stable job id")
        stable_by_id.setdefault(stable_id, job)
    stable = sorted(stable_by_id.values(), key=lambda job: str(job["id"]))

    random_selected = random.Random(seed).sample(
        stable,
        min(random_count, len(stable)),
    )
    likely_pool = [job for job in stable if _is_likely_match(job)]
    likely_selected = _stratified_sample(
        likely_pool,
        count=min(likely_match_count, len(likely_pool)),
        seed=seed ^ 0x484F4C44,
        category=lambda job: role_track(job),
        category_order=LIKELY_MATCH_TRACKS,
    )
    difficult_pool = [
        job for job in stable if difficult_negative_category(job) is not None
    ]
    difficult_selected = _stratified_sample(
        difficult_pool,
        count=min(difficult_negative_count, len(difficult_pool)),
        seed=seed ^ 0x4E454741,
        category=lambda job: difficult_negative_category(job) or "other",
    )

    memberships_by_id: dict[str, set[str]] = defaultdict(set)
    selected: list[dict] = []
    emitted: set[str] = set()
    for group, group_jobs in (
        ("random", random_selected),
        ("likely_match", likely_selected),
        ("difficult_negative", difficult_selected),
    ):
        for job in group_jobs:
            stable_id = str(job["id"])
            memberships_by_id[stable_id].add(group)
            if stable_id not in emitted:
                selected.append(job)
                emitted.add(stable_id)

    memberships = {
        str(job["id"]): [
            group
            for group in GROUPS
            if group in memberships_by_id[str(job["id"])]
        ]
        for job in selected
    }
    available = {
        "random": len(stable),
        "likely_match": len(likely_pool),
        "difficult_negative": len(difficult_pool),
    }
    return selected, memberships, available


def difficult_negative_category(job: Mapping[str, object]) -> str | None:
    if job.get("degree_eligible") is False:
        return "graduate_only"
    if assess_us_location(job).status == LOCATION_AMBIGUOUS:
        return "location_ambiguous"
    title = str(job.get("title") or "")
    for category, pattern in DIFFICULT_TITLE_PATTERNS:
        if pattern.search(title):
            return category
    track = role_track(job)
    if track in {
        "electrical_hardware",
        "mechanical_manufacturing",
        "factory_automation",
        "quality_test",
        "product",
        "technical_product",
        "non_technical",
        "other_engineering",
        "firmware",
        "embedded_software",
        "sdet_qa_automation",
        "quant_dev",
    }:
        return track
    if not determine_watcher_eligibility(dict(job))["watcher_eligible"]:
        return "current_ineligible_other"
    return None


def output_paths(
    prefix: str | Path,
    *,
    private_root: str | Path = EVALUATION_PRIVATE,
) -> dict[str, Path]:
    raw_prefix = Path(prefix)
    if not raw_prefix.name.startswith("scoring_us_holdout_"):
        raise BenchmarkError(
            "holdout output prefix must start with scoring_us_holdout_"
        )
    prefix = _repo_path(raw_prefix)
    _require_private_path(prefix, private_root)
    paths = {
        "labels": Path(f"{prefix}_labels.csv"),
        "rows": Path(f"{prefix}_rows.jsonl"),
        "predictions": Path(f"{prefix}_predictions.json"),
        "manifest": Path(f"{prefix}_manifest.json"),
        "report": Path(f"{prefix}_report.md"),
        "metrics": Path(f"{prefix}_metrics.json"),
    }
    for path in paths.values():
        _require_private_path(path, private_root)
    return paths


def export_benchmark(
    *,
    watchlist_path: str | Path,
    prior_prefixes: Sequence[str | Path],
    as_of: date,
    seed: int,
    expected_commit: str,
    output_prefix: str | Path,
    random_count: int = DEFAULT_RANDOM_COUNT,
    likely_match_count: int = DEFAULT_LIKELY_MATCH_COUNT,
    difficult_negative_count: int = DEFAULT_DIFFICULT_NEGATIVE_COUNT,
    collector: Callable[[WatcherConfig], tuple[list[dict], list[str]]] | None = None,
    analyzer: Callable[..., list[dict]] | None = None,
    git_state: Callable[[], tuple[str, bool | str]] | None = None,
    created_at: datetime | None = None,
    private_root: str | Path = EVALUATION_PRIVATE,
) -> dict[str, object]:
    """Collect and freeze a clean-commit holdout with zero prior overlap."""

    paths = output_paths(output_prefix, private_root=private_root)
    if seed != int(as_of.strftime("%Y%m%d")):
        raise BenchmarkError("holdout seed must equal the as-of date as YYYYMMDD")
    if not Path(output_prefix).name.endswith(as_of.strftime("%Y%m%d")):
        raise BenchmarkError("holdout output prefix date must match --as-of")
    frozen_commit = require_clean_expected_commit(
        expected_commit,
        git_state=git_state,
    )
    resolved_watchlist = _repo_path(watchlist_path)
    config = load_watchlist(resolved_watchlist)
    prior = load_prior_benchmark_index(
        prior_prefixes,
        private_root=private_root,
    )

    rows, source_errors = (collector or collect_rows)(config)
    if not rows:
        raise BenchmarkError("source collection produced no rows; no holdout was written")
    jobs = (analyzer or analyze_rows)(rows, today=as_of)
    location_candidates, outside_us_excluded = qualifying_candidate_pool(jobs)
    candidates, prior_exclusions = exclude_prior_overlaps(
        location_candidates,
        prior,
    )
    if not candidates:
        raise BenchmarkError(
            "no unseen U.S. or location-ambiguous internship candidates remained"
        )
    selected, memberships, available = sample_jobs(
        candidates,
        seed=seed,
        random_count=random_count,
        likely_match_count=likely_match_count,
        difficult_negative_count=difficult_negative_count,
    )
    if not selected:
        raise BenchmarkError("all requested cohort sizes are zero; no holdout was written")

    require_clean_expected_commit(expected_commit, git_state=git_state)
    validate_prior_inputs(prior)
    selected_overlap = overlap_counts(selected, prior)
    if any(selected_overlap.values()):
        raise BenchmarkError(
            f"selected holdout overlaps a prior benchmark: {selected_overlap}"
        )

    labels = [labels_row(job, memberships[str(job["id"])]) for job in selected]
    frozen_rows = [freeze_holdout_job(job) for job in selected]
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
    requested = {
        "random": random_count,
        "likely_match": likely_match_count,
        "difficult_negative": difficult_negative_count,
    }
    actual = {
        group: sum(group in memberships[str(job["id"])] for job in selected)
        for group in GROUPS
    }
    sanitized_errors = [sanitize_error(error) for error in source_errors]
    location_counts = Counter(assess_us_location(job).status for job in selected)
    candidate_location_counts = Counter(
        assess_us_location(job).status for job in candidates
    )
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_kind": BENCHMARK_KIND,
        "construction_version": CONSTRUCTION_VERSION,
        "benchmark_prefix": _display_path(Path(output_prefix)),
        "created_at_utc": observed.isoformat().replace("+00:00", "Z"),
        "as_of_date": as_of.isoformat(),
        "seed": seed,
        "frozen_git_commit": frozen_commit,
        "git_commit": frozen_commit,
        "git_dirty": False,
        "watchlist_path": _display_path(resolved_watchlist),
        "configured_terms": list(config.terms),
        "rows_collected": len(rows),
        "jobs_scored": len(jobs),
        "open_internship_pool_size": len(open_internship_pool(jobs)),
        "outside_us_excluded_count": outside_us_excluded,
        "candidate_pool_before_prior_exclusion": len(location_candidates),
        "candidate_pool_size": len(candidates),
        "candidate_pool_count": len(candidates),
        "selected_count": len(selected),
        "selected_row_count": len(selected),
        "requested_group_counts": requested,
        "available_group_counts": available,
        "actual_group_counts": actual,
        "cohort_overlap_counts": cohort_overlap_counts(memberships),
        "target_shortfalls": {
            group: {
                "requested": requested[group],
                "available": available[group],
                "selected": actual[group],
            }
            for group in GROUPS
            if actual[group] < requested[group]
        },
        "expected_model_positive_count": sum(
            model_positive(job) for job in selected
        ),
        "expected_model_positive_by_cohort": {
            group: sum(
                group in memberships[str(job["id"])] and model_positive(job)
                for job in selected
            )
            for group in GROUPS
        },
        "company_counts": dict(
            sorted(
                Counter(
                    str(job.get("company") or "unknown") for job in selected
                ).items()
            )
        ),
        "source_counts": source_counts(selected),
        "role_track_counts": dict(
            sorted(Counter(role_track(job) for job in selected).items())
        ),
        "location_status_counts": dict(sorted(location_counts.items())),
        "candidate_location_status_counts": dict(
            sorted(candidate_location_counts.items())
        ),
        "prior_benchmark_inputs": list(prior.inputs),
        "prior_overlap_exclusions": prior_exclusions,
        "selected_prior_overlap_counts": selected_overlap,
        "collection_health": {
            "sanitized": True,
            "source_failure_count": len(sanitized_errors),
            "source_failures": sanitized_errors,
        },
        "source_errors": sanitized_errors,
        "runtime_isolation": {
            "email_disabled": True,
            "alumni_loaded": False,
            "seen_state_used": False,
            "production_seen_state_modified": False,
        },
        "output_files": {
            name: _display_path(path) for name, path in paths.items()
        },
        "hashes": {
            "blank_labels_sha256": sha256_bytes(labels_payload),
            "selected_job_ids_sha256": sha256_bytes(selected_ids_payload),
            "frozen_rows_sha256": sha256_bytes(rows_payload),
            "baseline_predictions_sha256": sha256_bytes(predictions_payload),
            "watchlist_sha256": sha256_bytes(
                resolved_watchlist.read_bytes()
            ),
            "construction_script_sha256": sha256_bytes(
                Path(__file__).read_bytes()
            ),
        },
    }

    atomic_write_many(
        {
            paths["labels"]: labels_payload,
            paths["rows"]: rows_payload,
            paths["predictions"]: predictions_payload,
            paths["manifest"]: json_bytes(manifest),
        }
    )
    evaluate_benchmark(
        labels_path=paths["labels"],
        rows_path=paths["rows"],
        manifest_path=paths["manifest"],
        baseline_predictions_path=paths["predictions"],
        report_path=paths["report"],
        metrics_path=paths["metrics"],
        allow_partial_labels=True,
    )
    manifest["hashes"]["initial_report_sha256"] = sha256_bytes(
        paths["report"].read_bytes()
    )
    manifest["hashes"]["initial_metrics_sha256"] = sha256_bytes(
        paths["metrics"].read_bytes()
    )
    atomic_write_many({paths["manifest"]: json_bytes(manifest)})
    validate_frozen_artifacts(
        manifest,
        paths,
        prior=prior,
        private_root=private_root,
    )
    return manifest


def freeze_holdout_job(job: Mapping[str, object]) -> dict[str, object]:
    """Freeze canonical fields plus sanitized source/location provenance."""

    frozen = freeze_job(job)
    frozen["extra"] = safe_holdout_extra(job.get("extra"))
    return frozen


def safe_holdout_extra(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    output: dict[str, object] = {}
    for key in _SAFE_EXTRA_SCALARS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            output[key] = item.strip()
    feed_url = value.get("feed_url")
    if feed_url:
        output["feed_url"] = sanitize_feed_label(feed_url)
    for key in _SAFE_EXTRA_BOOLEANS:
        if isinstance(value.get(key), bool):
            output[key] = value[key]
    priority = value.get("source_priority")
    if isinstance(priority, int):
        output["source_priority"] = priority
    for key in _SAFE_EXTRA_STRUCTURES:
        if key not in value:
            continue
        sanitized = _sanitize_public_metadata(value[key])
        if sanitized not in (None, "", [], {}):
            output[key] = sanitized
    return output


def _sanitize_public_metadata(value: object, *, depth: int = 0) -> object:
    if depth > 8:
        return None
    if isinstance(value, Mapping):
        output = {}
        for raw_key, nested in value.items():
            key = str(raw_key)
            if _FORBIDDEN_METADATA_KEY.search(key):
                continue
            if re.sub(r"[^a-z0-9]+", "", key.casefold()) == "feedurl":
                sanitized = sanitize_feed_label(nested)
            else:
                sanitized = _sanitize_public_metadata(
                    nested,
                    depth=depth + 1,
                )
            if sanitized not in (None, "", [], {}):
                output[key] = sanitized
        return output
    if isinstance(value, (list, tuple, set, frozenset)):
        return [
            sanitized
            for item in value
            if (sanitized := _sanitize_public_metadata(item, depth=depth + 1))
            not in (None, "", [], {})
        ]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value)


def validate_frozen_artifacts(
    manifest: Mapping[str, object],
    paths: Mapping[str, Path],
    *,
    prior: PriorBenchmarkIndex,
    private_root: str | Path = EVALUATION_PRIVATE,
) -> None:
    hashes = manifest.get("hashes")
    if not isinstance(hashes, Mapping):
        raise BenchmarkError("manifest hashes must be an object")
    expected = {
        "blank_labels_sha256": paths["labels"],
        "frozen_rows_sha256": paths["rows"],
        "baseline_predictions_sha256": paths["predictions"],
        "initial_report_sha256": paths["report"],
        "initial_metrics_sha256": paths["metrics"],
    }
    for key, path in expected.items():
        _require_private_path(path, private_root)
        actual = sha256_bytes(path.read_bytes())
        if actual != str(hashes.get(key) or ""):
            raise BenchmarkError(f"{key} mismatch for {path}")

    with paths["labels"].open("r", encoding="utf-8-sig", newline="") as handle:
        label_rows = list(csv.DictReader(handle))
    if not label_rows:
        raise BenchmarkError("holdout labels file is empty")
    if any(
        str(row.get(field) or "").strip()
        for row in label_rows
        for field in HUMAN_LABEL_COLUMNS
    ):
        raise BenchmarkError("holdout human-label fields must all be blank")
    selected_ids = [str(row.get("job_id") or "") for row in label_rows]
    selected_payload = ("\n".join(selected_ids) + "\n").encode("utf-8")
    if sha256_bytes(selected_payload) != str(
        hashes.get("selected_job_ids_sha256") or ""
    ):
        raise BenchmarkError("selected_job_ids_sha256 mismatch")

    frozen_rows = load_frozen_rows(paths["rows"])
    overlaps = overlap_counts(frozen_rows, prior)
    if any(overlaps.values()):
        raise BenchmarkError(f"frozen holdout overlaps prior benchmarks: {overlaps}")
    if any(assess_us_location(row).status == OUTSIDE_US for row in frozen_rows):
        raise BenchmarkError("frozen holdout contains an outside_us row")

    metrics = load_manifest(paths["metrics"])
    coverage = metrics.get("coverage")
    if not isinstance(coverage, Mapping):
        raise BenchmarkError("initial metrics coverage must be an object")
    if (
        coverage.get("labeled_rows") != 0
        or coverage.get("evaluated_rows") != 0
        or coverage.get("labeled_coverage") != 0
    ):
        raise BenchmarkError("initial holdout evaluation must have zero label coverage")
    validate_prior_inputs(prior)

    watchlist = _repo_path(str(manifest.get("watchlist_path") or ""))
    if sha256_bytes(watchlist.read_bytes()) != str(
        hashes.get("watchlist_sha256") or ""
    ):
        raise BenchmarkError("watchlist_sha256 mismatch")
    if sha256_bytes(Path(__file__).read_bytes()) != str(
        hashes.get("construction_script_sha256") or ""
    ):
        raise BenchmarkError("construction_script_sha256 mismatch")


def validate_prior_inputs(prior: PriorBenchmarkIndex) -> None:
    for item in prior.inputs:
        for label in ("manifest", "rows", "predictions"):
            path = _repo_path(str(item[f"{label}_path"]))
            actual = sha256_bytes(path.read_bytes())
            if actual != str(item[f"{label}_sha256"]):
                raise BenchmarkError(
                    f"prior benchmark {label} hash changed: {path}"
                )


def overlap_counts(
    jobs: Sequence[Mapping[str, object]],
    prior: PriorBenchmarkIndex,
) -> dict[str, int]:
    counts = {
        "job_id": 0,
        "normalized_url": 0,
        "fallback_company_title_location": 0,
    }
    for job in jobs:
        stable_id = str(job.get("id") or job_id(job))
        normalized = norm_url(str(job.get("source_url") or ""))
        fallback = canonical_key(dict(job))
        counts["job_id"] += int(stable_id in prior.job_ids)
        counts["normalized_url"] += int(
            bool(normalized and normalized in prior.normalized_urls)
        )
        counts["fallback_company_title_location"] += int(
            bool(fallback.strip("|") and fallback in prior.fallback_keys)
        )
    return counts


def cohort_overlap_counts(
    memberships: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    combinations = Counter("+".join(groups) for groups in memberships.values())
    return {
        "rows_in_multiple_cohorts": sum(
            len(groups) > 1 for groups in memberships.values()
        ),
        "membership_combinations": dict(sorted(combinations.items())),
        "pairwise": {
            f"{left}+{right}": sum(
                left in groups and right in groups
                for groups in memberships.values()
            )
            for left, right in itertools.combinations(GROUPS, 2)
        },
    }


def model_positive(job: Mapping[str, object]) -> bool:
    return bool(determine_watcher_eligibility(dict(job))["watcher_eligible"])


def _is_likely_match(job: Mapping[str, object]) -> bool:
    return model_positive(job) or score_value(job, "fit_score") > 0


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
            if position >= len(buckets[name]):
                continue
            selected.append(buckets[name][position])
            positions[name] += 1
            progressed = True
            if len(selected) >= count:
                break
        if not progressed:
            break
    return selected


def _verify_declared_hash(
    manifest: Mapping[str, object],
    path: Path,
    key: str,
) -> None:
    hashes = manifest.get("hashes")
    if not isinstance(hashes, Mapping) or not hashes.get(key):
        raise BenchmarkError(f"prior manifest is missing required hash: {key}")
    actual = sha256_bytes(path.read_bytes())
    if actual != str(hashes[key]):
        raise BenchmarkError(f"{key} mismatch for prior input {path}")


def _verify_selected_ids_hash(
    manifest: Mapping[str, object],
    stable_ids: Sequence[str],
    prefix: Path,
) -> None:
    hashes = manifest.get("hashes")
    if not isinstance(hashes, Mapping) or not hashes.get(
        "selected_job_ids_sha256"
    ):
        raise BenchmarkError(
            f"prior manifest is missing selected_job_ids_sha256: {prefix}"
        )
    payload = ("\n".join(stable_ids) + "\n").encode("utf-8")
    if sha256_bytes(payload) != str(hashes["selected_job_ids_sha256"]):
        raise BenchmarkError(
            f"selected_job_ids_sha256 mismatch for prior input {prefix}"
        )


def _require_private_path(path: str | Path, private_root: str | Path) -> None:
    resolved = _repo_path(path).resolve()
    root = _repo_path(private_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise BenchmarkError(
            f"benchmark artifact path must remain under {root}: {resolved}"
        ) from exc


def _repo_path(path: str | Path) -> Path:
    value = Path(path)
    return value if value.is_absolute() else REPO_ROOT / value


def _display_path(path: Path) -> str:
    resolved = _repo_path(path).resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT.resolve()).as_posix())
    except ValueError:
        return str(resolved.as_posix())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export a clean-commit independent U.S. holdout benchmark."
    )
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH))
    parser.add_argument("--prior-prefix", action="append", default=None)
    parser.add_argument("--as-of", required=True, type=parse_date)
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument(
        "--random-count",
        type=nonnegative_int,
        default=DEFAULT_RANDOM_COUNT,
    )
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
    prior_prefixes = args.prior_prefix or [
        str(prefix) for prefix in DEFAULT_PRIOR_PREFIXES
    ]
    print(
        "HOLDOUT BENCHMARK-ONLY MODE: email, alumni loading, seen state, "
        "and watcher-data are disabled."
    )
    try:
        manifest = export_benchmark(
            watchlist_path=args.watchlist,
            prior_prefixes=prior_prefixes,
            as_of=args.as_of,
            seed=args.seed,
            expected_commit=args.expected_commit,
            output_prefix=args.output_prefix,
            random_count=args.random_count,
            likely_match_count=args.likely_match_count,
            difficult_negative_count=args.difficult_negative_count,
        )
    except (BenchmarkError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(
        "U.S. holdout benchmark exported: "
        f"rows={manifest['rows_collected']}, "
        f"candidates={manifest['candidate_pool_count']}, "
        f"selected={manifest['selected_count']}, "
        f"source_errors={len(manifest['source_errors'])}."
    )
    if manifest["target_shortfalls"]:
        print(f"Sampling limitations: {manifest['target_shortfalls']}")
    for path in manifest["output_files"].values():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
