"""Small deterministic helpers shared by scoring benchmark scripts."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Mapping

SCHEMA_VERSION = 1
GROUP_ORDER = ("random", "top", "difficult")
HUMAN_LABEL_COLUMNS = (
    "human_eligible",
    "human_priority",
    "human_action",
    "human_role_track",
    "error_category",
    "label_notes",
)


class BenchmarkError(ValueError):
    """Raised for invalid benchmark input or an inconsistent benchmark set."""


def score_value(job_or_prediction: Mapping[str, object], field: str, default: int = 0) -> int:
    score = job_or_prediction.get("score")
    value = score.get(field) if isinstance(score, Mapping) else None
    if value is None:
        value = job_or_prediction.get("total_score" if field == "total" else field)
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def role_track(job_or_prediction: Mapping[str, object]) -> str:
    score = job_or_prediction.get("score")
    if isinstance(score, Mapping) and score.get("role_track"):
        return str(score["role_track"])
    if job_or_prediction.get("role_track"):
        return str(job_or_prediction["role_track"])
    role = job_or_prediction.get("role_classification")
    if isinstance(role, Mapping) and role.get("role_track"):
        return str(role["role_track"])
    return "unknown"


def ranking_key(
    prediction: Mapping[str, object],
    context: Mapping[str, object] | None = None,
) -> tuple[object, ...]:
    context = context or prediction
    job_id = str(prediction.get("job_id") or prediction.get("id") or context.get("id") or "")
    return (
        -score_value(prediction, "fit_score"),
        -score_value(prediction, "total"),
        str(context.get("company") or "").casefold(),
        str(context.get("title") or "").casefold(),
        job_id,
    )


def prediction_from_job(
    job: Mapping[str, object],
    groups: object,
) -> dict[str, object]:
    score = job.get("score") if isinstance(job.get("score"), Mapping) else {}
    return {
        "job_id": str(job.get("id") or ""),
        "sample_groups": ordered_groups(groups),
        "watcher_eligible": bool(score.get("watcher_eligible")),
        "fit_score": score_value(job, "fit_score"),
        "watcher_action": str(score.get("watcher_action") or ""),
        "watcher_action_label": str(score.get("watcher_action_label") or ""),
        "role_track": role_track(job),
        "degree_level": str(job.get("degree_level") or score.get("degree_level") or ""),
        "degree_eligible": bool(job.get("degree_eligible", score.get("degree_eligible"))),
        "watcher_ineligible_reason": score.get("watcher_ineligible_reason"),
        "fit_explanation": str(score.get("fit_explanation") or ""),
        "total_score": score_value(job, "total"),
        "bucket": str(score.get("bucket") or ""),
        "action": str(score.get("action") or ""),
    }


def ordered_groups(groups: object) -> list[str]:
    if isinstance(groups, str):
        values = [value.strip() for value in groups.replace(";", "|").split("|")]
    elif isinstance(groups, (list, tuple, set, frozenset)):
        values = [str(value).strip() for value in groups]
    else:
        values = []
    unique = {value for value in values if value}
    return [group for group in GROUP_ORDER if group in unique] + sorted(unique - set(GROUP_ORDER))


def csv_safe(value: object) -> str:
    """Neutralize spreadsheet formulas while preserving the original text."""

    text = "" if value is None else str(value)
    first = text.lstrip("\t\r\n ")[:1]
    return "'" + text if first in {"=", "+", "-", "@"} else text


def json_bytes(value: object, *, indent: int | None = 2) -> bytes:
    separators = (",", ":") if indent is None else None
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=indent,
            separators=separators,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def render_csv_bytes(fieldnames: list[str], rows: list[Mapping[str, object]]) -> bytes:
    import io

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({field: csv_safe(row.get(field, "")) for field in fieldnames})
    return output.getvalue().encode("utf-8")


def atomic_write_many(payloads: Mapping[Path, bytes]) -> None:
    """Prepare every file before replacing destinations in the same directory."""

    temporary: dict[Path, Path] = {}
    try:
        for destination, payload in payloads.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary[destination] = Path(handle.name)
        for destination, temporary_path in temporary.items():
            os.replace(temporary_path, destination)
    finally:
        for temporary_path in temporary.values():
            temporary_path.unlink(missing_ok=True)
