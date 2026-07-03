"""Watcher-specific eligibility derived from scored role tracks."""

from __future__ import annotations

SWE_TARGET_TRACKS = {
    "backend",
    "full_stack",
    "frontend",
    "general_swe",
    "platform_infra",
    "data_engineering",
    "ml_ai",
    "quant_dev",
    "cloud",
    "devops",
    "embedded_software",
    "firmware",
    "sdet_qa_automation",
    # Deliberate low-priority exceptions: visible, but fit-scored around 20.
    "it_support",
    "quality_test",
    "solutions_engineering",
}


def determine_watcher_eligibility(job: dict, target_roles: set[str] | frozenset[str]) -> dict:
    """Return the watcher gate result for a scored job.

    The backend scorer owns the hard role-track decision. This wrapper applies
    the active watcher's target role set so a broad `swe` target can include
    strong software-adjacent tracks such as data engineering, ML, and quant dev.
    """

    score = job.get("score") or {}
    role_cls = job.get("role_classification") or {}
    role = role_cls.get("role")
    role_track = score.get("role_track") or role_cls.get("role_track") or role or "unknown"
    fit_score = _int_score(score.get("fit_score", score.get("total", 0)))
    degree_eligible = job.get("degree_eligible", score.get("degree_eligible", True))
    if degree_eligible is False:
        reason = (
            job.get("degree_ineligible_reason")
            or score.get("degree_ineligible_reason")
            or "Graduate/PhD-level internship outside undergraduate target."
        )
        return {
            "watcher_eligible": False,
            "fit_score": 0,
            "eligible_reason": None,
            "ineligible_reason": reason,
        }
    scorer_eligible = bool(score.get("watcher_eligible", fit_score > 0))

    target_match = role in target_roles
    if "swe" in target_roles and role_track in SWE_TARGET_TRACKS:
        target_match = True

    watcher_eligible = bool(scorer_eligible and target_match and fit_score > 0)
    if watcher_eligible:
        return {
            "watcher_eligible": True,
            "fit_score": fit_score,
            "eligible_reason": score.get("fit_explanation") or f"{role_track} matches watcher target roles.",
            "ineligible_reason": None,
        }

    reason = score.get("watcher_ineligible_reason")
    if not reason and not target_match:
        reason = f"{role_track} does not match watcher target roles."
    if not reason:
        reason = "Role is outside the watcher target profile."
    return {
        "watcher_eligible": False,
        "fit_score": 0,
        "eligible_reason": None,
        "ineligible_reason": reason,
    }


def _int_score(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
