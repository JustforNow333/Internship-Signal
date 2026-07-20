#!/usr/bin/env python3
"""Evaluate human labels against frozen and current watcher predictions."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.ingest import analyze_rows  # noqa: E402

from scripts.scoring_benchmark_common import (  # noqa: E402
    HUMAN_LABEL_COLUMNS,
    BenchmarkError,
    atomic_write_many,
    json_bytes,
    ordered_groups,
    prediction_from_job,
    ranking_key,
    sha256_bytes,
)

ALLOWED_ELIGIBILITY = frozenset({"yes", "no", "uncertain"})
ALLOWED_PRIORITIES = frozenset({"0", "1", "2", "3", "4"})
ALLOWED_ACTIONS = frozenset({"apply_now", "apply_later", "research_more", "skip"})
PRIORITY_SCALE = {0: 0, 1: 25, 2: 50, 3: 75, 4: 100}
REQUIRED_PREDICTION_FIELDS = frozenset(
    {
        "job_id",
        "sample_groups",
        "watcher_eligible",
        "fit_score",
        "watcher_action",
        "watcher_action_label",
        "role_track",
        "degree_level",
        "degree_eligible",
        "watcher_ineligible_reason",
        "fit_explanation",
        "total_score",
        "bucket",
        "action",
    }
)


@dataclass(frozen=True)
class HumanLabel:
    job_id: str
    sample_groups: tuple[str, ...]
    human_eligible: str
    human_priority: int | None
    human_action: str
    human_role_track: str
    error_category: str
    label_notes: str

    @property
    def evaluated(self) -> bool:
        if self.human_eligible not in {"yes", "no"} or not self.human_action:
            return False
        return self.human_eligible != "yes" or self.human_priority is not None


def read_json(path: str | Path) -> object:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkError(f"file not found: {path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"invalid UTF-8 JSON file {path}: {exc}") from exc


def load_manifest(path: str | Path) -> dict[str, object]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise BenchmarkError("manifest must be a JSON object")
    raw_date = value.get("as_of_date")
    try:
        date.fromisoformat(str(raw_date))
    except ValueError as exc:
        raise BenchmarkError("manifest as_of_date must use YYYY-MM-DD") from exc
    return value


def load_frozen_rows(path: str | Path) -> list[dict]:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise BenchmarkError(f"file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise BenchmarkError(f"frozen rows are not valid UTF-8: {path}") from exc
    rows: list[dict] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BenchmarkError(f"invalid JSONL at {path}:{line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise BenchmarkError(f"frozen row at {path}:{line_number} must be an object")
        rows.append(value)
    if not rows:
        raise BenchmarkError("frozen rows file is empty")
    return rows


def load_predictions(path: str | Path) -> dict[str, dict]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise BenchmarkError("baseline predictions must be a JSON object keyed by job_id")
    predictions: dict[str, dict] = {}
    for key, prediction in value.items():
        if not isinstance(prediction, dict):
            raise BenchmarkError(f"baseline prediction for {key!r} must be an object")
        job_id = str(prediction.get("job_id") or key)
        if job_id != str(key):
            raise BenchmarkError(f"baseline prediction key/job_id mismatch: {key!r}")
        if job_id in predictions:
            raise BenchmarkError(f"duplicate baseline job id: {job_id}")
        missing = sorted(REQUIRED_PREDICTION_FIELDS - set(prediction))
        if missing:
            raise BenchmarkError(
                f"baseline prediction for {job_id!r} is missing fields: {', '.join(missing)}"
            )
        predictions[job_id] = prediction
    if not predictions:
        raise BenchmarkError("baseline predictions file is empty")
    return predictions


def rescore_rows(rows: Sequence[dict], as_of: date) -> tuple[dict[str, dict], dict[str, dict]]:
    jobs = analyze_rows(list(rows), today=as_of)
    by_id: dict[str, dict] = {}
    for job in jobs:
        job_id = str(job.get("id") or "")
        if not job_id:
            raise BenchmarkError("current scorer produced a job without an id")
        if job_id in by_id:
            raise BenchmarkError(f"current scorer produced duplicate job id: {job_id}")
        by_id[job_id] = job
    return by_id, {
        job_id: prediction_from_job(job, ())
        for job_id, job in by_id.items()
    }


def load_labels(
    path: str | Path,
    *,
    benchmark_ids: set[str],
    expected_groups: Mapping[str, Sequence[str]],
    allow_partial: bool,
) -> tuple[dict[str, HumanLabel], dict[str, object]]:
    path = Path(path)
    try:
        handle = path.open("r", encoding="utf-8-sig", newline="")
    except FileNotFoundError as exc:
        raise BenchmarkError(f"file not found: {path}") from exc
    except UnicodeDecodeError as exc:
        raise BenchmarkError(f"labels file is not valid UTF-8: {path}") from exc

    with handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or ())
        if len(headers) != len(set(headers)):
            raise BenchmarkError("labels CSV contains duplicate header names")
        required = {"job_id", "sample_groups", *HUMAN_LABEL_COLUMNS}
        missing_headers = sorted(required - set(headers))
        if missing_headers:
            raise BenchmarkError(f"labels CSV missing required columns: {', '.join(missing_headers)}")

        labels: dict[str, HumanLabel] = {}
        incomplete: list[str] = []
        for line_number, row in enumerate(reader, start=2):
            job_id = str(row.get("job_id") or "").strip()
            if not job_id:
                raise BenchmarkError(f"labels CSV row {line_number} has a blank job_id")
            if job_id in labels:
                raise BenchmarkError(f"duplicate label job_id: {job_id}")
            if job_id not in benchmark_ids:
                raise BenchmarkError(f"label job_id is not in frozen benchmark: {job_id}")

            eligible = str(row.get("human_eligible") or "").strip().casefold()
            priority_text = str(row.get("human_priority") or "").strip()
            action = str(row.get("human_action") or "").strip().casefold()
            if eligible and eligible not in ALLOWED_ELIGIBILITY:
                raise BenchmarkError(f"{job_id}: invalid human_eligible {eligible!r}")
            if priority_text and priority_text not in ALLOWED_PRIORITIES:
                raise BenchmarkError(f"{job_id}: invalid human_priority {priority_text!r}")
            if action and action not in ALLOWED_ACTIONS:
                raise BenchmarkError(f"{job_id}: invalid human_action {action!r}")

            groups = tuple(ordered_groups(row.get("sample_groups", "")))
            expected = tuple(ordered_groups(expected_groups.get(job_id, ())))
            if groups != expected:
                raise BenchmarkError(f"{job_id}: sample_groups do not match the frozen baseline")
            label = HumanLabel(
                job_id=job_id,
                sample_groups=groups,
                human_eligible=eligible,
                human_priority=int(priority_text) if priority_text else None,
                human_action=action,
                human_role_track=str(row.get("human_role_track") or "").strip(),
                error_category=str(row.get("error_category") or "").strip(),
                label_notes=str(row.get("label_notes") or "").strip(),
            )
            labels[job_id] = label
            reasons = _incomplete_reasons(label)
            if reasons:
                incomplete.append(f"{job_id} ({', '.join(reasons)})")

    missing_rows = sorted(benchmark_ids - set(labels))
    incomplete.extend(f"{job_id} (missing CSV row)" for job_id in missing_rows)
    if incomplete and not allow_partial:
        preview = "; ".join(incomplete[:10])
        suffix = f"; plus {len(incomplete) - 10} more" if len(incomplete) > 10 else ""
        raise BenchmarkError(f"incomplete labels: {preview}{suffix}")
    evaluated = sum(label.evaluated for label in labels.values())
    uncertain = sum(label.human_eligible == "uncertain" for label in labels.values())
    return labels, {
        "benchmark_rows": len(benchmark_ids),
        "label_rows_present": len(labels),
        "evaluated_rows": evaluated,
        "uncertain_rows": uncertain,
        "incomplete_rows": len(incomplete),
        "coverage": evaluated / len(benchmark_ids) if benchmark_ids else None,
    }


def _incomplete_reasons(label: HumanLabel) -> list[str]:
    reasons: list[str] = []
    if not label.human_eligible:
        reasons.append("human_eligible blank")
        return reasons
    if label.human_eligible == "uncertain":
        return reasons
    if label.human_eligible == "yes" and label.human_priority is None:
        reasons.append("priority required for yes")
    if not label.human_action:
        reasons.append("human_action blank")
    return reasons


def eligibility_metrics(
    predictions: Mapping[str, Mapping[str, object]],
    labels: Mapping[str, HumanLabel],
    job_ids: Sequence[str],
) -> dict[str, object]:
    tp = fp = fn = tn = 0
    for job_id in job_ids:
        label = labels[job_id]
        predicted = bool(predictions[job_id].get("watcher_eligible"))
        actual = label.human_eligible == "yes"
        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1
    return {
        "evaluated_count": len(job_ids),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "specificity": _ratio(tn, tn + fp),
        "accuracy": _ratio(tp + tn, tp + fp + fn + tn),
        "f1": _ratio(2 * tp, 2 * tp + fp + fn),
    }


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    return numerator / denominator if denominator else None


def ranking_metrics(
    predictions: Mapping[str, Mapping[str, object]],
    contexts: Mapping[str, Mapping[str, object]],
    labels: Mapping[str, HumanLabel],
    job_ids: Sequence[str],
) -> dict[str, object]:
    ranked = sorted(
        job_ids,
        key=lambda job_id: ranking_key(predictions[job_id], contexts[job_id]),
    )
    output: dict[str, object] = {"evaluated_count": len(ranked)}
    for requested in (10, 20):
        used = min(requested, len(ranked))
        top = ranked[:used]
        good = sum(
            labels[job_id].human_eligible == "yes"
            and (labels[job_id].human_priority or 0) >= 3
            for job_id in top
        )
        priorities = [_priority_for_metrics(labels[job_id]) for job_id in top]
        output[f"at_{requested}"] = {
            "requested_k": requested,
            "used_k": used,
            "label": f"Precision@{used}",
            "precision": _ratio(good, used),
            "average_human_priority": _ratio(sum(priorities), len(priorities)),
            "job_ids": top,
        }
    return output


def _priority_for_metrics(label: HumanLabel) -> int:
    return label.human_priority if label.human_priority is not None else 0


def score_band_diagnostics(
    predictions: Mapping[str, Mapping[str, object]],
    labels: Mapping[str, HumanLabel],
    all_ids: Sequence[str],
) -> list[dict[str, object]]:
    bands = (
        ("85-100", lambda eligible, score: eligible and 85 <= score <= 100),
        ("70-84", lambda eligible, score: eligible and 70 <= score <= 84),
        ("50-69", lambda eligible, score: eligible and 50 <= score <= 69),
        ("25-49", lambda eligible, score: eligible and 25 <= score <= 49),
        ("1-24", lambda eligible, score: eligible and 1 <= score <= 24),
        ("0/ineligible", lambda eligible, score: not eligible or score <= 0),
    )
    output: list[dict[str, object]] = []
    for name, includes in bands:
        ids = [
            job_id for job_id in all_ids
            if includes(
                bool(predictions[job_id].get("watcher_eligible")),
                _int_value(predictions[job_id].get("fit_score")),
            )
        ]
        labeled = [job_id for job_id in ids if job_id in labels and labels[job_id].evaluated]
        eligible_count = sum(labels[job_id].human_eligible == "yes" for job_id in labeled)
        priorities = [_priority_for_metrics(labels[job_id]) for job_id in labeled]
        false_positives = sum(
            bool(predictions[job_id].get("watcher_eligible"))
            and labels[job_id].human_eligible == "no"
            for job_id in labeled
        )
        false_negatives = sum(
            not bool(predictions[job_id].get("watcher_eligible"))
            and labels[job_id].human_eligible == "yes"
            for job_id in labeled
        )
        output.append(
            {
                "band": name,
                "row_count": len(ids),
                "labeled_count": len(labeled),
                "human_eligible_rate": _ratio(eligible_count, len(labeled)),
                "average_human_priority": _ratio(sum(priorities), len(priorities)),
                "false_positives": false_positives,
                "false_negatives": false_negatives,
            }
        )
    return output


def _int_value(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def baseline_current_changes(
    baseline: Mapping[str, Mapping[str, object]],
    current: Mapping[str, Mapping[str, object]],
    all_ids: Sequence[str],
) -> dict[str, int]:
    return {
        "eligibility_decisions_changed": sum(
            bool(baseline[job_id].get("watcher_eligible"))
            != bool(current[job_id].get("watcher_eligible"))
            for job_id in all_ids
        ),
        "fit_scores_changed": sum(
            _int_value(baseline[job_id].get("fit_score"))
            != _int_value(current[job_id].get("fit_score"))
            for job_id in all_ids
        ),
        "role_tracks_changed": sum(
            str(baseline[job_id].get("role_track") or "")
            != str(current[job_id].get("role_track") or "")
            for job_id in all_ids
        ),
        "actions_changed": sum(
            (
                str(baseline[job_id].get("watcher_action") or ""),
                str(baseline[job_id].get("action") or ""),
            )
            != (
                str(current[job_id].get("watcher_action") or ""),
                str(current[job_id].get("action") or ""),
            )
            for job_id in all_ids
        ),
        "degree_eligibility_changed": sum(
            bool(baseline[job_id].get("degree_eligible"))
            != bool(current[job_id].get("degree_eligible"))
            for job_id in all_ids
        ),
    }


def error_diagnostics(
    predictions: Mapping[str, Mapping[str, object]],
    contexts: Mapping[str, Mapping[str, object]],
    baseline: Mapping[str, Mapping[str, object]],
    labels: Mapping[str, HumanLabel],
    label_ids: Sequence[str],
) -> dict[str, object]:
    false_positive_tracks: Counter[str] = Counter()
    false_negative_tracks: Counter[str] = Counter()
    error_categories: Counter[str] = Counter()
    role_confusion: Counter[str] = Counter()
    disagreements: list[dict[str, object]] = []
    for job_id in label_ids:
        prediction = predictions[job_id]
        label = labels[job_id]
        track = str(prediction.get("role_track") or "unknown")
        if label.error_category:
            error_categories[label.error_category] += 1
        if label.human_role_track:
            role_confusion[f"{track} -> {label.human_role_track}"] += 1
        if not label.evaluated:
            continue
        predicted_positive = bool(prediction.get("watcher_eligible"))
        actual_positive = label.human_eligible == "yes"
        if predicted_positive and not actual_positive:
            false_positive_tracks[track] += 1
        if not predicted_positive and actual_positive:
            false_negative_tracks[track] += 1

        priority_score = PRIORITY_SCALE[_priority_for_metrics(label)]
        fit_score = _int_value(prediction.get("fit_score"))
        context = contexts[job_id]
        disagreements.append(
            {
                "job_id": job_id,
                "company": str(context.get("company") or ""),
                "title": str(context.get("title") or ""),
                "source_url": str(context.get("source_url") or ""),
                "sample_groups": list(label.sample_groups),
                "baseline_watcher_eligible": bool(baseline[job_id].get("watcher_eligible")),
                "baseline_fit_score": _int_value(baseline[job_id].get("fit_score")),
                "current_watcher_eligible": predicted_positive,
                "current_fit_score": fit_score,
                "human_eligible": label.human_eligible,
                "human_priority": label.human_priority,
                "human_action": label.human_action,
                "predicted_role_track": track,
                "human_role_track": label.human_role_track,
                "error_category": label.error_category,
                "label_notes": label.label_notes,
                "eligibility_mismatch": predicted_positive != actual_positive,
                "preference_gap": abs(fit_score - priority_score),
            }
        )
    disagreements.sort(
        key=lambda item: (
            -int(bool(item["eligibility_mismatch"])),
            -int(item["preference_gap"]),
            str(item["company"]).casefold(),
            str(item["title"]).casefold(),
            str(item["job_id"]),
        )
    )
    return {
        "false_positives_by_predicted_role_track": dict(sorted(false_positive_tracks.items())),
        "false_negatives_by_predicted_role_track": dict(sorted(false_negative_tracks.items())),
        "error_categories": dict(sorted(error_categories.items())),
        "predicted_human_role_confusion": dict(sorted(role_confusion.items())),
        "largest_disagreements": disagreements[:25],
    }


def evaluate_benchmark(
    *,
    labels_path: str | Path,
    rows_path: str | Path,
    manifest_path: str | Path,
    baseline_predictions_path: str | Path,
    report_path: str | Path,
    metrics_path: str | Path,
    allow_partial_labels: bool = False,
) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    _verify_optional_hash(manifest, rows_path, "frozen_rows_sha256")
    _verify_optional_hash(manifest, baseline_predictions_path, "baseline_predictions_sha256")
    frozen_rows = load_frozen_rows(rows_path)
    baseline = load_predictions(baseline_predictions_path)
    as_of = date.fromisoformat(str(manifest["as_of_date"]))
    contexts, current = rescore_rows(frozen_rows, as_of)

    baseline_ids = set(baseline)
    current_ids = set(current)
    selected_count = manifest.get("selected_count")
    if selected_count is not None and _int_value(selected_count) != len(baseline_ids):
        raise BenchmarkError(
            "manifest selected_count does not match baseline predictions: "
            f"manifest={selected_count}, predictions={len(baseline_ids)}"
        )
    if current_ids != baseline_ids or len(frozen_rows) != len(current_ids):
        missing = sorted(baseline_ids - current_ids)
        unexpected = sorted(current_ids - baseline_ids)
        raise BenchmarkError(
            "frozen/current job IDs do not join exactly: "
            f"missing={missing or 'none'}, unexpected={unexpected or 'none'}, "
            f"frozen_rows={len(frozen_rows)}, current_jobs={len(current_ids)}"
        )
    expected_groups = {
        job_id: ordered_groups(prediction.get("sample_groups", ()))
        for job_id, prediction in baseline.items()
    }
    for job_id in current:
        current[job_id]["sample_groups"] = expected_groups[job_id]

    labels, coverage = load_labels(
        labels_path,
        benchmark_ids=baseline_ids,
        expected_groups=expected_groups,
        allow_partial=allow_partial_labels,
    )
    all_ids = sorted(baseline_ids)
    evaluated_ids = sorted(job_id for job_id, label in labels.items() if label.evaluated)
    random_ids = [job_id for job_id in evaluated_ids if "random" in expected_groups[job_id]]

    headline_current = eligibility_metrics(current, labels, random_ids)
    headline_baseline = eligibility_metrics(baseline, labels, random_ids)
    ranking_current = ranking_metrics(current, contexts, labels, evaluated_ids)
    ranking_baseline = ranking_metrics(baseline, contexts, labels, evaluated_ids)
    metrics: dict[str, object] = {
        "schema_version": 1,
        "as_of_date": as_of.isoformat(),
        "coverage": coverage,
        "headline_random_sample": {
            "current": headline_current,
            "baseline": headline_baseline,
        },
        "ranking": {
            "current": ranking_current,
            "baseline": ranking_baseline,
        },
        "score_bands": score_band_diagnostics(current, labels, all_ids),
        "baseline_vs_current": baseline_current_changes(baseline, current, all_ids),
        "diagnostics": error_diagnostics(
            current,
            contexts,
            baseline,
            labels,
            sorted(labels),
        ),
    }
    report = render_report(metrics)
    atomic_write_many(
        {
            Path(report_path): report.encode("utf-8"),
            Path(metrics_path): json_bytes(metrics),
        }
    )
    return metrics


def _verify_optional_hash(manifest: Mapping[str, object], path: str | Path, key: str) -> None:
    hashes = manifest.get("hashes")
    if not isinstance(hashes, Mapping) or not hashes.get(key):
        return
    path = Path(path)
    try:
        actual = sha256_bytes(path.read_bytes())
    except FileNotFoundError as exc:
        raise BenchmarkError(f"file not found: {path}") from exc
    expected = str(hashes[key])
    if actual != expected:
        raise BenchmarkError(f"{key} mismatch for {path}")


def render_report(metrics: Mapping[str, object]) -> str:
    coverage = metrics["coverage"]
    headline = metrics["headline_random_sample"]
    ranking = metrics["ranking"]
    changes = metrics["baseline_vs_current"]
    diagnostics = metrics["diagnostics"]
    lines = [
        "# Scoring benchmark evaluation",
        "",
        f"Frozen as-of date: `{metrics['as_of_date']}`",
        "",
        "## Label coverage",
        "",
        f"- Benchmark rows: {coverage['benchmark_rows']}",
        f"- Label rows present: {coverage['label_rows_present']}",
        f"- Fully evaluated rows: {coverage['evaluated_rows']}",
        f"- Uncertain rows: {coverage['uncertain_rows']}",
        f"- Incomplete rows: {coverage['incomplete_rows']}",
        f"- Evaluation coverage: {_percent(coverage['coverage'])}",
        "",
        "## Headline eligibility metrics (random cohort only)",
        "",
        "| Version | N | TP | FP | FN | TN | Precision | Recall | Specificity | Accuracy | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name in ("baseline", "current"):
        value = headline[name]
        lines.append(
            f"| {name.title()} | {value['evaluated_count']} | {value['true_positives']} | "
            f"{value['false_positives']} | {value['false_negatives']} | {value['true_negatives']} | "
            f"{_percent(value['precision'])} | {_percent(value['recall'])} | "
            f"{_percent(value['specificity'])} | {_percent(value['accuracy'])} | {_percent(value['f1'])} |"
        )
    lines.extend(
        [
            "",
            "## Ranking metrics (all fully labeled, non-uncertain rows)",
            "",
            "| Version | Cutoff | Precision | Average human priority |",
            "|---|---|---:|---:|",
        ]
    )
    for name in ("baseline", "current"):
        for requested in (10, 20):
            value = ranking[name][f"at_{requested}"]
            lines.append(
                f"| {name.title()} | {value['label']} | {_percent(value['precision'])} | "
                f"{_number(value['average_human_priority'])} |"
            )
    lines.extend(
        [
            "",
            "## Current score-band diagnostics",
            "",
            "| Band | Rows | Labeled | Human eligible | Avg priority | FP | FN |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for band in metrics["score_bands"]:
        lines.append(
            f"| {band['band']} | {band['row_count']} | {band['labeled_count']} | "
            f"{_percent(band['human_eligible_rate'])} | {_number(band['average_human_priority'])} | "
            f"{band['false_positives']} | {band['false_negatives']} |"
        )
    lines.extend(["", "## Baseline versus current", ""])
    for key in sorted(changes):
        lines.append(f"- {key.replace('_', ' ').title()}: {changes[key]}")

    lines.extend(["", "## Error diagnostics", ""])
    for title, key in (
        ("False positives by predicted role track", "false_positives_by_predicted_role_track"),
        ("False negatives by predicted role track", "false_negatives_by_predicted_role_track"),
        ("Human error categories", "error_categories"),
        ("Predicted → human role confusion", "predicted_human_role_confusion"),
    ):
        lines.append(f"### {title}")
        lines.append("")
        values = diagnostics[key]
        if values:
            lines.extend(f"- `{name}`: {count}" for name, count in values.items())
        else:
            lines.append("- None")
        lines.append("")

    lines.extend(
        [
            "## Largest disagreements",
            "",
            "Eligibility mismatches sort first, followed by the absolute gap between fit score and "
            "human priority mapped as 0/25/50/75/100. Ties use company, title, then job ID.",
            "",
            "| Job | Groups | Baseline | Current | Human | Tracks | Category / notes |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for item in diagnostics["largest_disagreements"]:
        job = f"{_md(item['company'])} — {_md(item['title'])}<br>`{_md(item['job_id'])}`<br>{_md(item['source_url'])}"
        groups = ", ".join(item["sample_groups"])
        baseline_text = f"eligible={item['baseline_watcher_eligible']}, fit={item['baseline_fit_score']}"
        current_text = f"eligible={item['current_watcher_eligible']}, fit={item['current_fit_score']}"
        human = f"{item['human_eligible']}, priority={item['human_priority']}, {item['human_action']}"
        tracks = f"{item['predicted_role_track']} → {item['human_role_track'] or '(blank)'}"
        notes = f"{item['error_category'] or '(none)'}; {item['label_notes'] or '(none)'}"
        lines.append(
            f"| {job} | {_md(groups)} | {_md(baseline_text)} | {_md(current_text)} | "
            f"{_md(human)} | {_md(tracks)} | {_md(notes)} |"
        )
    return "\n".join(lines) + "\n"


def _percent(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.1%}"


def _number(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.2f}"


def _md(value: object) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a labeled watcher scoring benchmark offline.")
    parser.add_argument("--labels", required=True)
    parser.add_argument("--rows", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--baseline-predictions", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--metrics-json", required=True)
    parser.add_argument("--allow-partial-labels", action="store_true")
    args = parser.parse_args(argv)
    try:
        metrics = evaluate_benchmark(
            labels_path=args.labels,
            rows_path=args.rows,
            manifest_path=args.manifest,
            baseline_predictions_path=args.baseline_predictions,
            report_path=args.report,
            metrics_path=args.metrics_json,
            allow_partial_labels=args.allow_partial_labels,
        )
    except (BenchmarkError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    current = metrics["headline_random_sample"]["current"]
    print(
        "Benchmark evaluated: "
        f"coverage={_percent(metrics['coverage']['coverage'])}, "
        f"random_n={current['evaluated_count']}, precision={_percent(current['precision'])}, "
        f"recall={_percent(current['recall'])}, F1={_percent(current['f1'])}."
    )
    print(f"Report: {args.report}")
    print(f"Metrics: {args.metrics_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
