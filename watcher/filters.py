"""Post-score filtering for watcher matches."""

from __future__ import annotations

import re
from typing import Iterable

from watcher.eligibility import DEFAULT_TARGET_ROLES, determine_watcher_eligibility

TARGET_ROLES = DEFAULT_TARGET_ROLES
MIN_SCORE: int | None = None

INTERNSHIP_RE = re.compile(r"\b(intern|internship|co[- ]?op|summer 20\d\d)\b", re.I)
NEW_GRAD_RE = re.compile(r"\bnew[- ]?grad(?:uate)?\b", re.I)
FULL_TIME_RE = re.compile(r"\b(full[- ]?time|fulltime|entry[- ]?level)\b", re.I)


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
    if not is_internship(job):
        return False
    if not is_open(job):
        return False
    eligibility = determine_watcher_eligibility(job, target_roles)
    if not eligibility["watcher_eligible"] or eligibility["fit_score"] <= 0:
        return False
    if min_score is not None and eligibility["fit_score"] < min_score:
        return False
    return True


def is_target_role(job: dict, *, target_roles: set[str] | frozenset[str] = TARGET_ROLES) -> bool:
    return determine_watcher_eligibility(job, target_roles)["watcher_eligible"]


def is_internship(job: dict) -> bool:
    title = str(job.get("title") or "")
    internship_type = str(job.get("internship_type") or "")
    evidence = f"{title}\n{internship_type}"

    # New-graduate labels identify a different recruiting track and remain a
    # hard exclusion. Full-time and entry-level labels are only soft negative
    # evidence because internship programs commonly use both descriptions.
    if NEW_GRAD_RE.search(evidence):
        return False
    positive = bool(INTERNSHIP_RE.search(evidence))
    if positive:
        return True
    if FULL_TIME_RE.search(evidence):
        return False
    return False


def is_open(job: dict) -> bool:
    extra = job.get("extra", {})
    if extra.get("active") is False:
        return False
    days_left = job.get("deadline_days_left")
    return days_left is None or days_left >= 0
