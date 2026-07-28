import json
from datetime import date
from pathlib import Path

import pytest

from backend.app.ingest import analyze_rows
from scripts.scoring_benchmark_common import prediction_from_job
from watcher.eligibility import determine_watcher_eligibility
from watcher.filters import filter_matches
from watcher.notify import render_digest
from watcher.sources.base import make_row


FIXTURES = Path(__file__).parent / "fixtures"
TECHNICAL_DESCRIPTION = "Build Python REST APIs and backend services."
TECHNICAL_REQUIREMENTS = "Python, SQL, and Git."


def scored(
    *,
    company="Example",
    title="Software Engineer Intern",
    location="United States",
    description=TECHNICAL_DESCRIPTION,
    requirements=TECHNICAL_REQUIREMENTS,
    source_url=None,
    extra=None,
):
    row = make_row(
        source="direct",
        source_adapter="fixture",
        company=company,
        title=title,
        location=location,
        description=description,
        requirements=requirements,
        source_url=source_url
        or f"https://example.test/{company}/{title}".replace(" ", "-"),
    )
    if extra:
        row["extra"].update(extra)
    return analyze_rows([row], today=date(2026, 7, 28))[0]


def assert_excluded(posting, reason):
    result = determine_watcher_eligibility(posting)

    assert result["watcher_eligible"] is False
    assert result["fit_score"] == 0
    assert result["ineligible_reason"] == reason
    assert posting["student_eligibility"]["exclusion_reason"] == reason
    assert posting["student_eligibility"]["evidence_source"]
    assert posting["student_eligibility"]["evidence"]
    assert posting["role_classification"]["role"] == "swe"
    assert posting["score"]["fit_score"] == 0
    assert posting["score"]["watcher_action"] == "skip"
    assert filter_matches([posting]) == []
    assert render_digest([posting]) == ("", "")


def assert_retained(posting):
    result = determine_watcher_eligibility(posting)

    assert result["watcher_eligible"] is True
    assert result["ineligible_reason"] is None
    assert result["fit_score"] > 0
    assert posting["student_eligibility"]["eligible"] is True
    assert posting["student_eligibility"]["exclusion_reason"] is None
    assert filter_matches([posting]) == [posting]


@pytest.mark.parametrize(
    ("posting", "reason"),
    [
        (
            lambda: scored(title="Machine Learning PhD Intern"),
            "phd_only",
        ),
        (
            lambda: scored(requirements="Must be enrolled in a doctoral program. Python required."),
            "phd_only",
        ),
        (
            lambda: scored(requirements="Graduate students only. Python required."),
            "graduate_only",
        ),
        (
            lambda: scored(requirements="Must be enrolled in a master's program. Python required."),
            "graduate_only",
        ),
        (
            lambda: scored(title="Software Engineer MBA Intern"),
            "graduate_only",
        ),
        (
            lambda: scored(requirements="Freshmen only. Python required."),
            "freshman_only",
        ),
        (
            lambda: scored(requirements="Open exclusively to first-year students."),
            "freshman_only",
        ),
        (
            lambda: scored(
                requirements=(
                    "Eligible applicants are rising sophomores only after completing "
                    "their freshman year."
                )
            ),
            "freshman_only",
        ),
        (
            lambda: scored(title="Returning Intern Software Engineer"),
            "returning_intern_only",
        ),
        (
            lambda: scored(
                company="Example",
                requirements=(
                    "Applicants must have previously completed an internship with Example."
                ),
            ),
            "returning_intern_only",
        ),
        (
            lambda: scored(
                description=(
                    "This is an invitation-only return internship for former interns."
                )
            ),
            "returning_intern_only",
        ),
        (
            lambda: scored(
                description=(
                    "Eligible only for prior internship program participants."
                )
            ),
            "returning_intern_only",
        ),
    ],
)
def test_clear_categorical_restrictions_are_excluded(posting, reason):
    assert_excluded(posting(), reason)


@pytest.mark.parametrize(
    "posting",
    [
        lambda: scored(requirements="Minimum: bachelor's degree. PhD preferred."),
        lambda: scored(
            requirements="Bachelor's, Master's, or PhD candidates are eligible."
        ),
        lambda: scored(
            description="You will collaborate with PhD researchers on advanced research."
        ),
        lambda: scored(
            extra={
                "minimum_qualifications": (
                    "Experience collaborating with PhD researchers."
                )
            }
        ),
        lambda: scored(
            requirements="Undergraduate or graduate students are eligible to apply."
        ),
        lambda: scored(requirements="Bachelor's or Master's students are accepted."),
        lambda: scored(requirements="Must graduate in 2028."),
        lambda: scored(requirements="Current students and recent graduates are accepted."),
        lambda: scored(requirements="Freshmen and sophomores are accepted."),
        lambda: scored(description="Freshmen are encouraged to apply."),
        lambda: scored(title="Software Engineering Early Insight Internship"),
        lambda: scored(requirements="Previous internship experience preferred."),
        lambda: scored(requirements="Prior participation preferred."),
        lambda: scored(
            description="Eligible only for prior leadership program participants."
        ),
        lambda: scored(requirements="Must return to school after the internship."),
        lambda: scored(
            description="You must be returning to university following the internship."
        ),
    ],
)
def test_mixed_preferred_incidental_and_ambiguous_evidence_is_retained(posting):
    assert_retained(posting())


def test_preferred_qualifications_section_never_excludes_by_itself():
    posting = scored(
        requirements=(
            "Minimum qualifications:\n"
            "Currently pursuing a bachelor's degree.\n"
            "Preferred qualifications:\n"
            "Currently pursuing a PhD in computer science."
        )
    )

    assert_retained(posting)


def test_explicit_structured_eligibility_has_priority_over_title():
    posting = scored(
        title="Software Engineer Intern",
        extra={"eligibility": {"academic_level": "doctoral program only"}},
    )

    assert_excluded(posting, "phd_only")
    assert posting["student_eligibility"]["evidence_source"].startswith(
        "extra.eligibility"
    )


def test_structured_mixed_degree_eligibility_overrides_lower_priority_title():
    posting = scored(
        title="Software Engineer PhD Intern",
        extra={
            "eligibility": {
                "eligible_degrees": ["Bachelor's", "Master's", "PhD"],
            }
        },
    )

    assert_retained(posting)


def test_northrop_returning_intern_fixture_is_excluded_with_title_evidence():
    fixture = json.loads(
        (FIXTURES / "northrop_returning_intern.json").read_text(encoding="utf-8")
    )
    posting = scored(**fixture)

    assert_excluded(posting, "returning_intern_only")
    assert posting["student_eligibility"]["evidence_source"] == "title"
    assert posting["title"] == "2027 Returning Intern Software Engineer"


def test_benchmark_prediction_exposes_stable_reason_and_evidence():
    posting = scored(requirements="PhD students only. Python required.")

    prediction = prediction_from_job(posting, ["difficult_negative"])

    assert prediction["watcher_eligible"] is False
    assert prediction["fit_score"] == 0
    assert prediction["eligibility_exclusion_reason"] == "phd_only"
    assert prediction["eligibility_evidence_source"] == "requirements"
    assert prediction["eligibility_evidence"] == "PhD students only"
    assert "phd_only" in prediction["eligibility_explanation"]
