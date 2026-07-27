import pytest

from watcher.eligibility import (
    LOCATION_AMBIGUOUS,
    LOCATION_US,
    OUTSIDE_US,
    assess_us_location,
    determine_watcher_eligibility,
)
from watcher.filters import filter_matches, is_internship, is_open


def job(**overrides):
    base = {
        "title": "Software Engineer Intern",
        "location": "",
        "remote_status": "",
        "description": "Build backend services with Python.",
        "internship_type": "",
        "deadline_days_left": None,
        "role_classification": {"role": "swe", "role_track": "general_swe"},
        "score": {
            "total": 60,
            "fit_score": 60,
            "watcher_eligible": True,
            "role_track": "general_swe",
        },
        "extra": {},
    }
    base.update(overrides)
    return base


def test_filters_keep_swe_internship_open_jobs():
    assert filter_matches([job()]) == [job()]


def test_normal_us_location_continues_through_existing_eligibility():
    posting = job(location="Boston, MA, United States")

    assert assess_us_location(posting).status == LOCATION_US
    assert determine_watcher_eligibility(posting)["watcher_eligible"] is True
    assert filter_matches([posting]) == [posting]


def test_normal_international_location_is_ineligible_with_stable_reason():
    posting = job(location="Berlin, Germany")

    decision = assess_us_location(posting)
    eligibility = determine_watcher_eligibility(posting)

    assert decision.status == OUTSIDE_US
    assert eligibility["watcher_eligible"] is False
    assert eligibility["ineligible_reason"] == OUTSIDE_US
    assert eligibility["location_explanation"]
    assert filter_matches([posting]) == []


def test_us_only_remote_role_continues():
    posting = job(remote_status="Remote — United States only")

    assert assess_us_location(posting).status == LOCATION_US
    assert filter_matches([posting]) == [posting]


def test_international_remote_role_is_ineligible():
    posting = job(remote_status="Remote within Canada only")

    assert assess_us_location(posting).status == OUTSIDE_US
    assert determine_watcher_eligibility(posting)["ineligible_reason"] == OUTSIDE_US
    assert filter_matches([posting]) == []


def test_multiple_locations_continue_when_one_is_in_the_us():
    posting = job(location="Toronto, Canada; Boston, MA, United States")

    assert assess_us_location(posting).status == LOCATION_US
    assert filter_matches([posting]) == [posting]


def test_multiple_locations_with_no_us_location_are_ineligible():
    posting = job(location="Berlin, Germany; Toronto, Canada")

    assert assess_us_location(posting).status == OUTSIDE_US
    assert filter_matches([posting]) == []


@pytest.mark.parametrize("location", ["", "8 Locations", "Boston, MA"])
def test_missing_or_ambiguous_location_is_not_automatically_rejected(location):
    posting = job(location=location)

    assert assess_us_location(posting).status == LOCATION_AMBIGUOUS
    assert filter_matches([posting]) == [posting]


def test_santiago_without_country_evidence_remains_ambiguous():
    posting = job(location="Santiago")

    assert assess_us_location(posting).status == LOCATION_AMBIGUOUS
    assert filter_matches([posting]) == [posting]


@pytest.mark.parametrize(
    "location",
    [
        "Madrid, MD, Spain",
        "Schiphol, NH, Netherlands",
        "Montpellier, France",
        "Boxmeer, Netherlands",
    ],
)
def test_foreign_country_text_wins_over_state_like_abbreviations(location):
    posting = job(location=location)

    assert assess_us_location(posting).status == OUTSIDE_US
    assert determine_watcher_eligibility(posting)["ineligible_reason"] == OUTSIDE_US
    assert filter_matches([posting]) == []


def test_structured_country_information_is_preferred_and_supports_multiple_locations():
    foreign = job(location={"city": "Madrid", "region": "MD", "country_code": "ES"})
    mixed = job(
        location=[
            {"city": "Madrid", "country": "Spain"},
            {"city": "Boston", "country_code": "US"},
        ]
    )

    assert assess_us_location(foreign).status == OUTSIDE_US
    assert assess_us_location(mixed).status == LOCATION_US


def test_workday_alpha3_country_prefixes_are_explicit_foreign_evidence():
    netherlands = job(location="NLD - North Brabant - Boxmeer")
    switzerland = job(location="CHE - Lucerne - Lucerne (Rösslimatt)")
    poland = job(location="POL - Mazowieckie Wojewodztwo - Warsaw")

    assert assess_us_location(netherlands).status == OUTSIDE_US
    assert assess_us_location(switzerland).status == OUTSIDE_US
    assert assess_us_location(poland).status == OUTSIDE_US


def test_nested_structured_country_metadata_is_used_consistently():
    netherlands = job(
        location="Utrecht",
        extra={"posting_metadata": {"location": {"country": "Netherlands"}}},
    )
    switzerland = job(
        location="Lucerne",
        extra={"normalized_location": {"country_code": "CH"}},
    )
    poland = job(
        location="Warsaw",
        extra={"source_details": {"raw_location": {"countryCode": "PL"}}},
    )

    assert assess_us_location(netherlands).status == OUTSIDE_US
    assert assess_us_location(switzerland).status == OUTSIDE_US
    assert assess_us_location(poland).status == OUTSIDE_US


def test_structured_country_overrides_misleading_text_in_the_same_location():
    posting = job(
        location={
            "display_name": "Boston, MA, United States",
            "country_code": "NL",
        }
    )

    assert assess_us_location(posting).status == OUTSIDE_US


def test_reliable_description_country_context_resolves_city_only_locations():
    montpellier = job(
        location="Montpellier",
        description=(
            "Work in an international environment with offices located in "
            "Israel, Slovenia and France."
        ),
    )
    santiago = job(
        location="Santiago",
        description=(
            "Ability to work on-site at our office located in Ciudad Empresarial, "
            "Huechuraba, Santiago, Chile."
        ),
    )
    netherlands = job(
        location="Utrecht",
        description="Join our Sales Engineering team in the Netherlands.",
    )

    for posting, country in (
        (montpellier, "France"),
        (santiago, "Chile"),
        (netherlands, "Netherlands"),
    ):
        decision = assess_us_location(posting)
        assert decision.status == OUTSIDE_US
        assert "description location context" in decision.explanation
        assert country in decision.explanation


def test_explicit_us_location_wins_over_separate_foreign_evidence():
    posting = job(
        location=[
            {"city": "Amsterdam", "country": "Netherlands"},
            {"city": "Boston", "country_code": "US"},
        ],
        description="Other company offices are located in France.",
    )

    decision = assess_us_location(posting)

    assert decision.status == LOCATION_US
    assert "other non-U.S. evidence does not override it" in decision.explanation
    assert filter_matches([posting]) == [posting]


def test_filters_drop_non_swe_roles():
    assert filter_matches([job(
        role_classification={"role": "unknown", "role_track": "electrical_hardware"},
        score={"total": 90, "fit_score": 0, "watcher_eligible": False, "role_track": "electrical_hardware"},
    )]) == []


def test_filters_drop_new_grad_full_time_titles():
    assert not is_internship(job(title="Software Engineer New Grad"))
    assert filter_matches([job(title="Software Engineer New Grad")]) == []


def test_filters_drop_expired_or_inactive_jobs():
    assert not is_open(job(deadline_days_left=-1))
    assert not is_open(job(extra={"active": False}))
    assert filter_matches([job(deadline_days_left=-1), job(extra={"active": False})]) == []


def test_filters_optional_score_gate():
    assert filter_matches([job(score={"total": 95, "fit_score": 69, "watcher_eligible": True})], min_score=70) == []
    assert filter_matches([job(score={"total": 70, "fit_score": 70, "watcher_eligible": True})], min_score=70)


def test_full_time_title_with_intern_boilerplate_is_not_internship():
    # Full-time/senior title, but description mentions interns -> must NOT match.
    assert not is_internship(job(
        title="Staff Software Engineer",
        description="We also run a Summer 2026 internship program.",
    ))
    assert filter_matches([job(
        title="Staff Software Engineer",
        description="We also run a Summer 2026 internship program.",
    )]) == []


def test_title_based_internship_still_matches():
    assert is_internship(job(title="Software Engineer Intern - Summer 2026"))
    assert is_internship(job(title="Data Science Co-op"))


def test_truthy_non_intern_employment_type_is_not_internship():
    # Adapters store the ATS employment-type string in internship_type;
    # a plain truthiness check wrongly matched all of them.
    assert not is_internship(job(title="Security Reliability Engineer", internship_type="FullTime"))
    assert not is_internship(job(title="Web-App developer", internship_type="full"))
    assert not is_internship(job(title="Senior DevOps Engineer", internship_type="Contract"))
    assert filter_matches([job(title="Security Reliability Engineer", internship_type="FullTime")]) == []


def test_intern_employment_type_string_still_matches():
    assert is_internship(job(title="Software Engineer", internship_type="Intern"))
    assert is_internship(job(title="Backend Engineer", internship_type="internship"))


def test_filters_use_watcher_eligibility_not_generic_total_score():
    bad = job(
        title="Electrical Engineer Intern",
        role_classification={"role": "unknown", "role_track": "electrical_hardware"},
        score={
            "total": 99,
            "fit_score": 0,
            "watcher_eligible": False,
            "role_track": "electrical_hardware",
            "watcher_ineligible_reason": "Electrical role outside target SWE track.",
        },
    )
    good = job(
        title="Backend Engineer Intern",
        role_classification={"role": "swe", "role_track": "backend"},
        score={"total": 80, "fit_score": 100, "watcher_eligible": True, "role_track": "backend"},
    )

    assert filter_matches([bad, good]) == [good]


def test_low_priority_it_quality_and_solutions_pass_with_low_fit_score():
    matches = filter_matches([
        job(
            title="IT Support Intern",
            role_classification={"role": "it", "role_track": "it_support"},
            score={"total": 41, "fit_score": 20, "watcher_eligible": True, "role_track": "it_support"},
        ),
        job(
            title="Quality Engineer Intern",
            role_classification={"role": "unknown", "role_track": "quality_test"},
            score={"total": 41, "fit_score": 20, "watcher_eligible": True, "role_track": "quality_test"},
        ),
        job(
            title="Solutions Engineer Intern",
            role_classification={"role": "unknown", "role_track": "solutions_engineering"},
            score={"total": 41, "fit_score": 20, "watcher_eligible": True, "role_track": "solutions_engineering"},
        ),
    ])

    assert [match["title"] for match in matches] == [
        "IT Support Intern",
        "Quality Engineer Intern",
        "Solutions Engineer Intern",
    ]


def test_filters_drop_degree_ineligible_jobs_even_with_positive_fit_score():
    grad = job(
        title="Machine Learning Engineer PhD Intern",
        degree_level="phd",
        degree_eligible=False,
        degree_ineligible_reason="Graduate/PhD-level internship outside undergraduate target.",
        role_classification={"role": "swe", "role_track": "ml_ai"},
        score={
            "total": 94,
            "fit_score": 91,
            "watcher_eligible": True,
            "role_track": "ml_ai",
            "degree_eligible": False,
            "degree_ineligible_reason": "Graduate/PhD-level internship outside undergraduate target.",
        },
    )

    assert filter_matches([grad]) == []
