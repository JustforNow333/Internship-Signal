"""Stage 3 pure education matching rules and model adapters (AC6, AC7).

Database-free. New symbols are reached through the ``matching`` module inside
each test, so these tests collect on the base and fail during execution.

The undergraduate ``freshman_only`` rule follows the owner-approved
correction to AC7: sophomore, junior and senior are excluded, while
first_year and not_specified standing stay eligible.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.eligibility import analyze_student_eligibility
from app.hosted import job_mapper, matching, models
from app.hosted.career_level import CAREER_LEVELS, INTERNSHIP

EDUCATION_LEVELS = ("not_specified", "undergraduate", "masters", "doctoral")
CLASS_STANDINGS = ("not_specified", "first_year", "sophomore", "junior", "senior")
REASONS = ("phd_only", "graduate_only", "freshman_only", "returning_intern_only")
NON_INTERNSHIP_LEVELS = tuple(level for level in CAREER_LEVELS if level != INTERNSHIP)
EXPECTED_REASON_CODES = frozenset(
    {
        "company_watched",
        "role_selected",
        "career_level_selected",
        "location_any",
        "location_preferred",
        "location_united_states",
        "remote_included",
        "season_any",
        "season_match",
        "season_unspecified",
    }
)


def verdict(reason: str | None, **overrides) -> dict:
    value = {
        "exclusion_reason": reason,
        "degree_level": None,
        "evidence_source": "title" if reason else None,
        "evidence": "PhD Research Intern" if reason else None,
    }
    value.update(overrides)
    return value


def excluded(level: str, standing: str, reason: str | None) -> bool:
    """The AC7 rule table, with the owner-approved sophomore correction."""

    if level == "undergraduate":
        if reason in {"phd_only", "graduate_only"}:
            return True
        if reason == "freshman_only":
            return standing in {"sophomore", "junior", "senior"}
        return False
    if level == "masters":
        return reason == "phd_only"
    return False


def valid_pairs() -> list[tuple[str, str]]:
    pairs = [(level, "not_specified") for level in EDUCATION_LEVELS]
    pairs += [("undergraduate", standing) for standing in CLASS_STANDINGS[1:]]
    return pairs


def make_job(career_level: str = INTERNSHIP, eligibility=None):
    return matching.MatchJob(
        company_id="capital-one",
        role_id="software_engineering",
        career_level=career_level,
        title="Software Engineer Intern",
        location="New York, NY",
        student_eligibility=eligibility,
    )


def make_preferences(level: str = "not_specified", standing: str = "not_specified"):
    return matching.MatchPreferences(
        role_ids=frozenset({"software_engineering"}),
        career_levels=frozenset(CAREER_LEVELS),
        education_level=level,
        class_standing=standing,
    )


def decide(job, prefs):
    return matching.evaluate_match(job, prefs, watching=True, watch_paused=False)


def test_education_constants_mirror_the_stored_allowlists() -> None:
    assert models.EDUCATION_LEVELS == EDUCATION_LEVELS
    assert models.CLASS_STANDINGS == CLASS_STANDINGS


def test_dataclass_defaults_are_permissive() -> None:
    job = matching.MatchJob(company_id="c", role_id="r")
    prefs = matching.MatchPreferences()
    assert job.student_eligibility is None
    assert prefs.education_level == "not_specified"
    assert prefs.class_standing == "not_specified"


@pytest.mark.parametrize("level,standing", valid_pairs())
@pytest.mark.parametrize("reason", (None, *REASONS))
def test_complete_rule_table_for_internships(level, standing, reason) -> None:
    decision = decide(make_job(eligibility=verdict(reason)), make_preferences(level, standing))
    assert decision.matches is not excluded(level, standing, reason)
    if not decision.matches:
        assert decision.reasons == ()


@pytest.mark.parametrize(
    "standing,expected",
    (
        ("not_specified", True),
        ("first_year", True),
        ("sophomore", False),
        ("junior", False),
        ("senior", False),
    ),
)
def test_freshman_only_sophomore_correction(standing, expected) -> None:
    decision = decide(
        make_job(eligibility=verdict("freshman_only")),
        make_preferences("undergraduate", standing),
    )
    assert decision.matches is expected


@pytest.mark.parametrize(
    "level,reason,expected",
    (
        ("undergraduate", "phd_only", False),
        ("undergraduate", "graduate_only", False),
        ("masters", "phd_only", False),
        ("masters", "graduate_only", True),
        ("masters", "freshman_only", True),
        ("doctoral", "phd_only", True),
        ("doctoral", "graduate_only", True),
        ("doctoral", "freshman_only", True),
        ("not_specified", "phd_only", True),
        ("not_specified", "graduate_only", True),
        ("not_specified", "freshman_only", True),
    ),
)
def test_named_rule_rows(level, reason, expected) -> None:
    decision = decide(make_job(eligibility=verdict(reason)), make_preferences(level))
    assert decision.matches is expected


@pytest.mark.parametrize("level,standing", valid_pairs())
def test_returning_intern_only_never_excludes(level, standing) -> None:
    decision = decide(
        make_job(eligibility=verdict("returning_intern_only")),
        make_preferences(level, standing),
    )
    assert decision.matches is True


@pytest.mark.parametrize("career_level", NON_INTERNSHIP_LEVELS)
@pytest.mark.parametrize("level,standing", valid_pairs())
@pytest.mark.parametrize("reason", REASONS)
def test_non_internship_career_levels_are_never_filtered_by_education(
    career_level, level, standing, reason
) -> None:
    decision = decide(
        make_job(career_level, verdict(reason)), make_preferences(level, standing)
    )
    assert decision.matches is True


MALFORMED_VERDICTS = (
    None,
    "phd_only",
    ["phd_only"],
    ("exclusion_reason", "phd_only"),
    42,
    {},
    {"degree_level": "phd"},
    {"exclusion_reason": "PHD_ONLY"},
    {"exclusion_reason": " phd_only"},
    {"exclusion_reason": "unknown_reason"},
    {"exclusion_reason": 1},
    {"exclusion_reason": ["phd_only"]},
    verdict("phd_only", degree_level=7),
    verdict("phd_only", evidence={"text": "PhD"}),
    verdict("phd_only", evidence_source=["title"]),
    verdict("phd_only", evidence="x" * 301),
    verdict("phd_only", degree_level="d" * 65),
    verdict("phd_only", eligible=True),
    verdict("phd_only", eligible="false"),
    verdict("graduate_only", eligible=1),
)


@pytest.mark.parametrize("value", MALFORMED_VERDICTS)
@pytest.mark.parametrize("level,standing", valid_pairs())
def test_missing_malformed_and_unknown_verdicts_are_permissive(
    value, level, standing
) -> None:
    decision = decide(make_job(eligibility=value), make_preferences(level, standing))
    assert decision.matches is True


def test_well_formed_verdict_with_consistent_eligible_flag_still_applies() -> None:
    value = verdict("phd_only", eligible=False, evidence="x" * 300)
    assert decide(make_job(eligibility=value), make_preferences("masters")).matches is False


@pytest.mark.parametrize(
    "level,standing",
    (("bachelors", "not_specified"), ("", "not_specified"), ("UNDERGRADUATE", "senior")),
)
def test_unknown_preference_ids_exclude_nothing(level, standing) -> None:
    for reason in REASONS:
        decision = decide(make_job(eligibility=verdict(reason)), make_preferences(level, standing))
        assert decision.matches is True


def test_unknown_class_standing_keeps_undergraduate_degree_rules_only() -> None:
    prefs = make_preferences("undergraduate", "fifth_year")
    assert decide(make_job(eligibility=verdict("freshman_only")), prefs).matches is True
    assert decide(make_job(eligibility=verdict("phd_only")), prefs).matches is False


CLASSIFIER_ELIGIBLE_ROWS = {
    "preferred": {"title": "Software Engineer Intern", "requirements": "PhD preferred"},
    "mixed": {
        "title": "Software Engineer Intern",
        "requirements": "Open to undergraduate, Master's, or PhD students",
    },
    "negated": {
        "title": "Software Engineer Intern",
        "requirements": "A PhD is not required",
    },
    "ambiguous": {
        "title": "Software Engineer Intern",
        "description": "Our PhD researchers mentor interns.",
    },
}


@pytest.mark.parametrize("kind", sorted(CLASSIFIER_ELIGIBLE_ROWS))
@pytest.mark.parametrize("level,standing", valid_pairs())
def test_eligible_classifier_evidence_stays_eligible(kind, level, standing) -> None:
    raw = analyze_student_eligibility(CLASSIFIER_ELIGIBLE_ROWS[kind]).decision.as_dict()
    stored = job_mapper.normalized_student_eligibility(raw)
    assert stored is not None and stored["exclusion_reason"] is None
    for value in (stored, raw):
        decision = decide(make_job(eligibility=value), make_preferences(level, standing))
        assert decision.matches is True


def test_excluding_classifier_verdict_flows_through_the_stored_shape() -> None:
    raw = analyze_student_eligibility({"title": "PhD Research Intern"}).decision.as_dict()
    stored = job_mapper.normalized_student_eligibility(raw)
    assert stored["exclusion_reason"] == "phd_only"
    assert decide(make_job(eligibility=stored), make_preferences("undergraduate")).matches is False
    assert decide(make_job(eligibility=stored), make_preferences("doctoral")).matches is True


@pytest.mark.parametrize("level,standing", valid_pairs())
def test_education_adds_no_reason_codes(level, standing) -> None:
    baseline = decide(make_job(), make_preferences())
    decision = decide(
        make_job(eligibility=verdict("returning_intern_only")),
        make_preferences(level, standing),
    )
    assert decision.reasons == baseline.reasons
    assert matching.REASON_CODES == EXPECTED_REASON_CODES
    assert {entry["code"] for entry in decision.reasons} <= EXPECTED_REASON_CODES


def test_other_hard_constraints_still_win_over_eligible_education() -> None:
    prefs = make_preferences("doctoral")
    job = make_job(eligibility=verdict(None))
    assert matching.evaluate_match(job, prefs, watching=False, watch_paused=False).matches is False
    assert matching.evaluate_match(job, prefs, watching=True, watch_paused=True).matches is False


def test_preferences_from_model_populates_education_fields() -> None:
    row = SimpleNamespace(
        role_ids=["software_engineering"],
        career_levels=["internship"],
        preferred_locations=[],
        include_remote=True,
        internship_season="Any season",
        education_level="undergraduate",
        class_standing="sophomore",
    )
    prefs = matching.preferences_from_model(row)
    assert prefs.education_level == "undergraduate"
    assert prefs.class_standing == "sophomore"


@pytest.mark.parametrize("missing", (None, "", 5, ["undergraduate"]))
def test_preferences_from_model_defaults_education_to_not_specified(missing) -> None:
    legacy = SimpleNamespace(role_ids=["software_engineering"])
    prefs = matching.preferences_from_model(legacy)
    assert prefs.education_level == "not_specified"
    assert prefs.class_standing == "not_specified"
    odd = SimpleNamespace(
        role_ids=["software_engineering"], education_level=missing, class_standing=missing
    )
    prefs = matching.preferences_from_model(odd)
    assert prefs.education_level == "not_specified"
    assert prefs.class_standing == "not_specified"


def test_job_from_model_populates_student_eligibility() -> None:
    stored = verdict("graduate_only")
    row = SimpleNamespace(
        company_id="capital-one",
        role_id="software_engineering",
        career_level="internship",
        title="Software Engineer Intern",
        location="",
        remote_status="",
        description="",
        is_open=True,
        student_eligibility=stored,
    )
    job = matching.job_from_model(row)
    assert job.student_eligibility == stored
    assert matching.job_from_model(row, include_description=False).student_eligibility == stored


@pytest.mark.parametrize("value", (None, "graduate_only", ["graduate_only"], 3))
def test_job_from_model_treats_missing_or_non_mapping_eligibility_as_unknown(
    value,
) -> None:
    legacy = SimpleNamespace(company_id="c", role_id="r", is_open=True)
    assert matching.job_from_model(legacy).student_eligibility is None
    row = SimpleNamespace(
        company_id="c", role_id="r", is_open=True, student_eligibility=value
    )
    assert matching.job_from_model(row).student_eligibility is None


def test_default_accounts_keep_existing_match_results() -> None:
    legacy = matching.preferences_from_model(
        SimpleNamespace(role_ids=["software_engineering"], career_levels=["internship"])
    )
    for reason in (None, *REASONS):
        job = make_job(eligibility=verdict(reason))
        assert decide(job, legacy).matches is True
        assert decide(job, legacy).reasons == decide(make_job(), legacy).reasons
