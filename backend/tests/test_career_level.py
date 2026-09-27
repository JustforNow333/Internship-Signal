"""Title-only hosted career-level classification."""

from __future__ import annotations

import pytest

from app.hosted.career_level import (
    CAREER_LEVELS,
    INTERNSHIP,
    MID_LEVEL,
    NEW_GRAD_JUNIOR,
    PERSISTED_CAREER_LEVELS,
    SELECTABLE_CAREER_LEVELS,
    SENIOR_PLUS,
    UNKNOWN,
    classify_career_level,
)


def level(title: str, **fields: object) -> str:
    return classify_career_level({"title": title, **fields})


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Existing internship / co-op semantics.
        ("Software Engineer Intern", INTERNSHIP),
        ("Software Engineering Internship", INTERNSHIP),
        ("Software Engineering Co-op", INTERNSHIP),
        ("Software Engineering Co–op", INTERNSHIP),
        ("Summer 2027 Software Engineer", INTERNSHIP),
        ("Graduate Intern", INTERNSHIP),
        ("Entry Level Software Engineering Intern", INTERNSHIP),
        ("Junior Year Software Intern", INTERNSHIP),
        ("Product Manager Intern", INTERNSHIP),
        ("Chief of Staff Intern", INTERNSHIP),
        ("Analytics Engineer Intern JR-0101608", INTERNSHIP),
        # New grad / junior.
        ("Software Engineer, New Grad", NEW_GRAD_JUNIOR),
        ("Software Engineer, New Grad 2026", NEW_GRAD_JUNIOR),
        ("New College Grad Software Engineer", NEW_GRAD_JUNIOR),
        ("New Graduate Software Engineer", NEW_GRAD_JUNIOR),
        ("Recent Graduate Software Engineer", NEW_GRAD_JUNIOR),
        ("University Graduate Software Engineer", NEW_GRAD_JUNIOR),
        ("College Graduate Data Scientist", NEW_GRAD_JUNIOR),
        ("Early Career Software Engineer", NEW_GRAD_JUNIOR),
        ("Entry Level Software Engineer", NEW_GRAD_JUNIOR),
        ("Entry-Level Software Engineer", NEW_GRAD_JUNIOR),
        ("Junior Software Engineer", NEW_GRAD_JUNIOR),
        ("Jr. Software Engineer", NEW_GRAD_JUNIOR),
        ("Jr Software Engineer", NEW_GRAD_JUNIOR),
        ("Graduate Engineer", NEW_GRAD_JUNIOR),
        ("Software Engineer - Campus Hire", NEW_GRAD_JUNIOR),
        ("University Hire - Software Engineer", NEW_GRAD_JUNIOR),
        ("Associate Software Engineer", NEW_GRAD_JUNIOR),
        ("Associate Machine Learning Engineer", NEW_GRAD_JUNIOR),
        ("Software Engineer I", NEW_GRAD_JUNIOR),
        ("Data Scientist I", NEW_GRAD_JUNIOR),
        # Mid level, including associate-ladder traps.
        ("Software Engineer II", MID_LEVEL),
        ("Senior Associate", MID_LEVEL),
        ("Senior Associate, Software Engineer", MID_LEVEL),
        ("Principal Associate", MID_LEVEL),
        ("Principal Associate, Data Science", MID_LEVEL),
        ("Sr. Associate Software Engineer", MID_LEVEL),
        # Senior+.
        ("Senior Software Engineer", SENIOR_PLUS),
        ("Sr. Software Engineer", SENIOR_PLUS),
        ("Sr Software Engineer", SENIOR_PLUS),
        ("Software Engineer, Senior", SENIOR_PLUS),
        ("Staff Software Engineer", SENIOR_PLUS),
        ("Senior Staff Software Engineer", SENIOR_PLUS),
        ("Principal Software Engineer", SENIOR_PLUS),
        ("Senior Principal Engineer", SENIOR_PLUS),
        ("Distinguished Engineer", SENIOR_PLUS),
        ("Engineering Fellow", SENIOR_PLUS),
        ("Technical Fellow", SENIOR_PLUS),
        ("Lead Software Engineer", SENIOR_PLUS),
        ("Lead Machine Learning Engineer", SENIOR_PLUS),
        ("Technical Lead", SENIOR_PLUS),
        ("Tech Lead, Payments", SENIOR_PLUS),
        # Generic and ambiguous titles stay unknown.
        ("Software Engineer", UNKNOWN),
        ("Machine Learning Engineer", UNKNOWN),
        ("Software Architect", UNKNOWN),
        ("Member of Technical Staff", UNKNOWN),
        ("Software Engineer III", UNKNOWN),
        ("Software Engineer IV", UNKNOWN),
        ("Software Engineer V", UNKNOWN),
        ("Software Engineer I/II", UNKNOWN),
        ("L4 Software Engineer", UNKNOWN),
        ("Software Engineer (JR-12345)", UNKNOWN),
        ("Lead Generation Specialist", UNKNOWN),
        ("Engagement Lead", UNKNOWN),
        ("Research Fellowship Program", UNKNOWN),
        ("Associate", UNKNOWN),
        ("Associate Director, Engineering", UNKNOWN),
        ("Graduate Admissions Coordinator", UNKNOWN),
        ("", UNKNOWN),
        # Management is never senior_plus.
        ("Engineering Manager", UNKNOWN),
        ("Senior Manager, Software Engineering", UNKNOWN),
        ("Director of Engineering", UNKNOWN),
        ("Head of Machine Learning", UNKNOWN),
        ("VP, Engineering", UNKNOWN),
        ("Vice President, Software Engineering", UNKNOWN),
        ("Chief of Staff", UNKNOWN),
        ("Chief Technology Officer", UNKNOWN),
        ("President", UNKNOWN),
        # Contradictory career-stage signals.
        ("2026 New Grad Software Engineer Intern", UNKNOWN),
        ("New Grad Software Engineer - Internship", UNKNOWN),
        ("Senior Software Engineer Intern", UNKNOWN),
        ("Senior Junior Software Engineer", UNKNOWN),
        ("Associate Principal Engineer", UNKNOWN),
        ("Staff Software Engineer II", UNKNOWN),
        ("Senior Software Engineer III", UNKNOWN),
    ],
)
def test_title_classification(title: str, expected: str) -> None:
    assert level(title) == expected


def test_description_and_requirements_never_change_the_level() -> None:
    noisy = {
        "description": (
            "Senior staff principal lead. New grad early career entry level "
            "junior. Open to college seniors and juniors."
        ),
        "requirements": "8+ years of experience. 0-1 years for graduates.",
        "role_classification": {"role": "swe", "role_track": "backend"},
        "extra": {"time_type": "Full time", "jobLevel": "Senior"},
    }
    for title in (
        "Software Engineer",
        "Senior Software Engineer",
        "Software Engineer, New Grad",
        "Software Engineer Intern",
    ):
        assert level(title, **noisy) == level(title)


def test_existing_internship_type_evidence_is_preserved() -> None:
    assert level("Software Engineer", internship_type="Internship") == INTERNSHIP
    assert level("Software Engineer", internship_type="Summer 2027") == INTERNSHIP
    assert level("Software Engineer", internship_type="Full-time") == UNKNOWN
    # A contradictory senior title is not forced into the internship catalog.
    assert (
        level("Senior Software Engineer", internship_type="Internship") == UNKNOWN
    )


def test_classification_is_case_and_whitespace_insensitive() -> None:
    assert level("  SENIOR   software ENGINEER ") == SENIOR_PLUS
    assert level("software engineer, NEW GRAD") == NEW_GRAD_JUNIOR


def test_contract_constants() -> None:
    assert CAREER_LEVELS == (
        INTERNSHIP,
        NEW_GRAD_JUNIOR,
        MID_LEVEL,
        SENIOR_PLUS,
        UNKNOWN,
    )
    assert SELECTABLE_CAREER_LEVELS == (INTERNSHIP, NEW_GRAD_JUNIOR, SENIOR_PLUS)
    assert PERSISTED_CAREER_LEVELS == frozenset(SELECTABLE_CAREER_LEVELS)
    assert MID_LEVEL not in SELECTABLE_CAREER_LEVELS
    assert UNKNOWN not in SELECTABLE_CAREER_LEVELS
