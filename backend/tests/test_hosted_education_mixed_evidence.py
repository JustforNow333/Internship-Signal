"""Mixed-degree evidence never excludes a level the posting itself names.

The shared classifier reports "Master's or PhD" and "MS/PhD" postings as
phd_only, and "BS, MS" or "Undergraduate, Master's" requirements as
graduate_only. Its verdict is reused unchanged; hosted matching only declines
to treat such a verdict as excluding a level that the stored evidence names.
"""

from __future__ import annotations

import pytest

from app.eligibility import assess_student_eligibility
from app.hosted.matching import MatchJob, MatchPreferences, evaluate_match


def _verdict(title: str, requirements: str = "") -> dict:
    return assess_student_eligibility(
        {"company": "Example", "title": title, "requirements": requirements}
    ).as_dict()


def _matches(verdict, level: str, standing: str = "not_specified") -> bool:
    job = MatchJob(
        company_id="example",
        role_id="software_engineering",
        career_level="internship",
        title="Intern",
        student_eligibility=verdict,
    )
    preferences = MatchPreferences(
        role_ids=frozenset({"software_engineering"}),
        career_levels=frozenset({"internship"}),
        education_level=level,
        class_standing=standing,
    )
    return evaluate_match(job, preferences, watching=True, watch_paused=False).matches


MASTERS_OR_PHD = [
    _verdict("2027 Summer Intern, MS/PhD, Software Engineer"),
    _verdict("Design Verification Engineering Intern - MS/PhD"),
    _verdict(
        "Hardware Intern",
        "Currently pursuing a Master's or PhD degree in Physics or a related field",
    ),
    _verdict(
        "Hardware Intern",
        "Currently enrolled in an MS or PhD program in Mechanical Engineering",
    ),
    _verdict("2027 PhD/MFE Summer Intern - Quantitative Portfolio Management"),
]
UNDERGRAD_OR_GRADUATE = [
    _verdict(
        "Bio Intern",
        "Minimum Qualifications: Currently pursuing a BS, MS, or equivalent degree in cell biology",
    ),
    _verdict(
        "Design Intern",
        "Currently pursuing an Undergraduate, Master's degree in HCI, AI, Computer Science",
    ),
]


def test_fixtures_reproduce_the_classifier_verdicts() -> None:
    assert [v["exclusion_reason"] for v in MASTERS_OR_PHD] == ["phd_only"] * 5
    assert [v["exclusion_reason"] for v in UNDERGRAD_OR_GRADUATE] == ["graduate_only"] * 2


@pytest.mark.parametrize("verdict", MASTERS_OR_PHD)
def test_masters_or_phd_postings_keep_masters_users(verdict) -> None:
    assert _matches(verdict, "masters")
    assert _matches(verdict, "doctoral")
    # Nothing in them names undergraduates, so undergraduates stay excluded.
    assert not _matches(verdict, "undergraduate")


@pytest.mark.parametrize("verdict", UNDERGRAD_OR_GRADUATE)
def test_undergraduate_or_graduate_requirements_keep_undergraduates(verdict) -> None:
    assert _matches(verdict, "undergraduate")
    assert _matches(verdict, "undergraduate", "senior")
    assert _matches(verdict, "masters")


@pytest.mark.parametrize(
    ("title", "requirements", "excluded"),
    [
        ("Research Intern", "Must be currently pursuing a PhD degree at an accredited university", ("undergraduate", "masters")),
        ("Applied Scientist Intern, PhD (Summer 2027)", "", ("undergraduate", "masters")),
        ("Software Engineering Intern - Masters", "", ("undergraduate",)),
        ("SWE Intern", "Currently pursuing a Master's degree in Computer Science", ("undergraduate",)),
    ],
)
def test_single_level_restrictions_still_exclude(title, requirements, excluded) -> None:
    verdict = _verdict(title, requirements)
    for level in ("undergraduate", "masters", "doctoral", "not_specified"):
        assert _matches(verdict, level) is (level not in excluded), level


def test_a_verdict_without_evidence_still_applies() -> None:
    verdict = {"exclusion_reason": "phd_only", "degree_level": "phd", "evidence_source": "title", "evidence": None}
    assert not _matches(verdict, "masters")
    assert not _matches(verdict, "undergraduate")
