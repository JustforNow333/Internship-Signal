"""Post-score filtering for watcher matches."""

from __future__ import annotations

import re
from typing import Iterable

from watcher.eligibility import DEFAULT_TARGET_ROLES, determine_watcher_eligibility

TARGET_ROLES = DEFAULT_TARGET_ROLES
MIN_SCORE: int | None = None

COOP_SEPARATOR_PATTERN = r"[-\u2010\u2011\u2012\u2013\u2014 ]?"
STRONG_INTERNSHIP_RE = re.compile(
    rf"\b(?:intern|internship|co{COOP_SEPARATOR_PATTERN}op)\b",
    re.I,
)
SEASON_YEAR_RE = re.compile(
    r"(?:\b(?:spring|summer|fall|autumn|winter)"
    r"[\s\-\u2010\u2011\u2012\u2013\u2014]+20\d{2}\b|"
    r"\b20\d{2}[\s\-\u2010\u2011\u2012\u2013\u2014]+"
    r"(?:spring|summer|fall|autumn|winter)\b)",
    re.I,
)
NEW_GRAD_RE = re.compile(r"\bnew[- ]?grad(?:uate)?\b", re.I)
PROFESSIONAL_SEASON_CONTEXT_RE = re.compile(
    r"\b(?:full[- ]?time|fulltime|entry[- ]?level|graduation date|"
    r"expected graduation|graduating|graduate between|class of|"
    r"degree completion)\b",
    re.I,
)


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
    # hard exclusion even when explicit internship wording is also present.
    if NEW_GRAD_RE.search(evidence):
        return False

    # Explicit internship/co-op wording is strong enough to override ordinary
    # full-time or entry-level wording used by some real student programs.
    if STRONG_INTERNSHIP_RE.search(evidence):
        return True

    # A bare season/year is useful for program titles, but graduation windows
    # in professional postings are eligibility criteria, not internship labels.
    if not SEASON_YEAR_RE.search(evidence):
        return False
    return not PROFESSIONAL_SEASON_CONTEXT_RE.search(evidence)


def is_open(job: dict) -> bool:
    extra = job.get("extra", {})
    if extra.get("active") is False:
        return False
    days_left = job.get("deadline_days_left")
    return days_left is None or days_left >= 0
