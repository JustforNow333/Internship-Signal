"""Post-score filtering for watcher matches."""

from __future__ import annotations

import re
from typing import Iterable

TARGET_ROLES = frozenset({"swe"})
MIN_SCORE: int | None = None

INTERNSHIP_RE = re.compile(r"\b(intern|internship|co[- ]?op|summer 20\d\d)\b", re.I)
FULL_TIME_RE = re.compile(r"\b(new[- ]?grad|new graduate|full[- ]?time|fulltime|entry[- ]?level)\b", re.I)


def filter_matches(
    jobs: Iterable[dict],
    *,
    target_roles: set[str] | frozenset[str] = TARGET_ROLES,
    min_score: int | None = MIN_SCORE,
) -> list[dict]:
    return [job for job in jobs if is_match(job, target_roles=target_roles, min_score=min_score)]


def is_match(
    job: dict,
    *,
    target_roles: set[str] | frozenset[str] = TARGET_ROLES,
    min_score: int | None = MIN_SCORE,
) -> bool:
    if not is_target_role(job, target_roles=target_roles):
        return False
    if not is_internship(job):
        return False
    if not is_open(job):
        return False
    if min_score is not None and job.get("score", {}).get("total", 0) < min_score:
        return False
    return True


def is_target_role(job: dict, *, target_roles: set[str] | frozenset[str] = TARGET_ROLES) -> bool:
    return job.get("role_classification", {}).get("role") in target_roles


def is_internship(job: dict) -> bool:
    title = job.get("title", "")
    blob = " ".join([
        title,
        job.get("internship_type", ""),
        job.get("description", ""),
    ])
    if FULL_TIME_RE.search(title):
        return False
    return bool(job.get("internship_type") or INTERNSHIP_RE.search(blob))


def is_open(job: dict) -> bool:
    extra = job.get("extra", {})
    if extra.get("active") is False:
        return False
    days_left = job.get("deadline_days_left")
    return days_left is None or days_left >= 0

