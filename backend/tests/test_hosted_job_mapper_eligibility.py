"""Stage 3 job mapper: bounded student-eligibility persistence (AC4) and the
hosted-layer neutralization of technology "MS <product>" phrases (AC5).

These tests are database-free. The PostgreSQL import regression lives in
``test_hosted_job_import.py`` beside the other import lifecycle tests.
"""

from __future__ import annotations

import copy

import pytest
from app.eligibility import analyze_student_eligibility
from app.hosted import job_mapper
from app.hosted.catalog import CompanyCatalog, PublicCompany

TECHNOLOGY_PRODUCTS = (
    "SQL",
    "Server",
    "Office",
    "Excel",
    "Azure",
    "Teams",
    "Dynamics",
    "Windows",
    "Access",
    "Exchange",
    "SharePoint",
    "Outlook",
    "365",
)
FIELDS = ("exclusion_reason", "degree_level", "evidence_source", "evidence")


@pytest.fixture
def catalog() -> CompanyCatalog:
    return CompanyCatalog(
        (
            PublicCompany(
                id="capital-one",
                name="Capital One",
                aliases=("Capital One Financial",),
                coverage="direct",
                selectable=True,
            ),
        )
    )


def raw_job(**overrides) -> dict:
    job = {
        "id": "watcher-job-1",
        "company": "Capital One Financial",
        "title": "Backend Software Engineer Intern",
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "description": "Build production APIs and distributed services.",
        "requirements": "Python and SQL",
        "source_url": "https://example.com/jobs/123",
        "date_posted": "2026-08-01",
        "deadline": "2026-09-01",
        "deadline_days_left": 30,
        "internship_type": "Summer 2027 Internship",
        "role_classification": {"role": "swe", "role_track": "backend"},
        "extra": {
            "source": "direct",
            "source_adapter": "workday",
            "source_requisition_id": "R-123",
        },
    }
    job.update(overrides)
    return job


def analyzed_job(**overrides) -> dict:
    """A final job carrying the real classifier verdict, as ingest produces."""

    job = raw_job(**overrides)
    decision = analyze_student_eligibility(job).decision.as_dict()
    # Mirror the ingest final-job shape, including the score outputs that the
    # classifier would treat as structured evidence if they were fed back.
    job["student_eligibility"] = decision
    job["degree_level"] = decision["degree_level"]
    return job


def mapped_eligibility(job: dict, catalog: CompanyCatalog):
    result = job_mapper.map_final_jobs([job], catalog)
    assert result.jobs_skipped == 0, result.skipped_reasons
    return result.jobs[0].student_eligibility


# --- AC4: bounded persistence -------------------------------------------------


def test_classifier_verdict_is_stored_as_the_bounded_four_field_dict(
    catalog: CompanyCatalog,
) -> None:
    job = analyzed_job(
        requirements="Must be currently pursuing a PhD in Computer Science."
    )
    stored = mapped_eligibility(job, catalog)
    assert tuple(stored) == FIELDS
    assert stored["exclusion_reason"] == "phd_only"
    assert stored["degree_level"] == "phd"
    assert stored["evidence_source"] == "requirements"
    assert "PhD" in stored["evidence"]
    # The classifier's explanation and diagnostic flags are never persisted.
    assert set(job["student_eligibility"]) - set(FIELDS)


def test_eligible_verdict_is_stored_with_null_reason(catalog: CompanyCatalog) -> None:
    stored = mapped_eligibility(analyzed_job(), catalog)
    assert stored == {
        "exclusion_reason": None,
        "degree_level": job_eligibility_degree(analyzed_job()),
        "evidence_source": None,
        "evidence": None,
    }


def job_eligibility_degree(job: dict) -> str:
    return job["student_eligibility"]["degree_level"]


@pytest.mark.parametrize(
    "value",
    [
        None,
        "graduate_only",
        ["graduate_only"],
        42,
        True,
        {},
        {"degree_level": "masters"},
        {"exclusion_reason": "senior_only"},
        {"exclusion_reason": "GRADUATE_ONLY"},
        {"exclusion_reason": 7},
        {"exclusion_reason": ["phd_only"]},
        {"exclusion_reason": "phd_only", "degree_level": 3},
        {"exclusion_reason": "phd_only", "evidence_source": {"a": 1}},
        {"exclusion_reason": "phd_only", "evidence": ["PhD"]},
        {"exclusion_reason": "phd_only", "eligible": True},
        {"exclusion_reason": None, "eligible": False},
        {"exclusion_reason": None, "eligible": "yes"},
    ],
)
def test_missing_malformed_or_unknown_classifications_are_null(
    catalog: CompanyCatalog, value: object
) -> None:
    assert job_mapper.normalized_student_eligibility(value) is None
    job = raw_job(student_eligibility=value)
    assert mapped_eligibility(job, catalog) is None


def test_absent_classification_key_is_null(catalog: CompanyCatalog) -> None:
    job = raw_job()
    assert "student_eligibility" not in job
    assert mapped_eligibility(job, catalog) is None


def test_every_known_reason_is_accepted() -> None:
    for reason in ("phd_only", "graduate_only", "freshman_only", "returning_intern_only"):
        stored = job_mapper.normalized_student_eligibility(
            {"exclusion_reason": reason, "eligible": False}
        )
        assert stored == {
            "exclusion_reason": reason,
            "degree_level": None,
            "evidence_source": None,
            "evidence": None,
        }


def test_evidence_is_bounded_and_control_free() -> None:
    stored = job_mapper.normalized_student_eligibility(
        {
            "exclusion_reason": "phd_only",
            "degree_level": "phd",
            "evidence_source": "requirements",
            "evidence": "PhD\x00 required\n" + "x" * 1_000,
        }
    )
    assert len(stored["evidence"]) == job_mapper.MAX_ELIGIBILITY_EVIDENCE_LENGTH == 300
    assert stored["evidence"].startswith("PhD  required ")
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in stored["evidence"])


def test_overlong_or_blank_identifier_strings_become_null() -> None:
    stored = job_mapper.normalized_student_eligibility(
        {
            "exclusion_reason": "phd_only",
            "degree_level": "p" * 65,
            "evidence_source": "   ",
            "evidence": "",
        }
    )
    assert stored == {
        "exclusion_reason": "phd_only",
        "degree_level": None,
        "evidence_source": None,
        "evidence": None,
    }


def test_eligibility_is_part_of_business_values_for_change_detection(
    catalog: CompanyCatalog,
) -> None:
    eligible = job_mapper.map_final_jobs([analyzed_job()], catalog).jobs[0]
    restricted = job_mapper.map_final_jobs(
        [analyzed_job(requirements="Must be currently pursuing a PhD.")], catalog
    ).jobs[0]
    unknown = job_mapper.map_final_jobs([raw_job()], catalog).jobs[0]
    assert "student_eligibility" in eligible.business_values()
    values = [
        job.business_values()["student_eligibility"]
        for job in (eligible, restricted, unknown)
    ]
    assert values[0] != values[1] != values[2] and values[2] is None
    # Identical input maps to an identical value, so a repeat is unchanged.
    again = job_mapper.map_final_jobs([analyzed_job()], catalog).jobs[0]
    assert again.business_values() == eligible.business_values()


def test_eligibility_does_not_change_identity_or_other_fields(
    catalog: CompanyCatalog,
) -> None:
    with_verdict = job_mapper.map_final_jobs(
        [analyzed_job(requirements="Must be currently pursuing a PhD.")], catalog
    ).jobs[0]
    without = job_mapper.map_final_jobs(
        [raw_job(requirements="Must be currently pursuing a PhD.")], catalog
    ).jobs[0]
    assert with_verdict.watcher_job_id == without.watcher_job_id
    a = with_verdict.business_values()
    b = without.business_values()
    a.pop("student_eligibility")
    b.pop("student_eligibility")
    assert a == b


def test_mapping_never_mutates_the_incoming_row(catalog: CompanyCatalog) -> None:
    job = analyzed_job(
        title="Data Analyst Intern",
        requirements="Must hold MS SQL Server certification.",
        description="Daily work in MS Excel and MS Teams is required.",
    )
    assert job["student_eligibility"]["exclusion_reason"] == "graduate_only"
    before = copy.deepcopy(job)
    mapped_eligibility(job, catalog)
    assert job == before


# --- AC5: technology "MS <product>" phrases ----------------------------------


@pytest.mark.parametrize("product", TECHNOLOGY_PRODUCTS)
@pytest.mark.parametrize("spelling", ["MS", "ms", "Ms"])
@pytest.mark.parametrize(
    "field,template",
    [
        ("title", "{ms} {product} Data Intern"),
        ("requirements", "Must hold {ms} {product} certification."),
        ("description", "Proficiency with {ms} {product} required."),
    ],
)
def test_technology_ms_phrase_never_stores_graduate_only(
    catalog: CompanyCatalog,
    product: str,
    spelling: str,
    field: str,
    template: str,
) -> None:
    text = template.format(ms=spelling, product=product)
    overrides = {field: text}
    if field != "title":
        overrides["title"] = "Data Analyst Intern"
    job = analyzed_job(**overrides)
    # Precondition: the unchanged classifier reads this "MS" as a Master's.
    assert job["student_eligibility"]["exclusion_reason"] == "graduate_only"
    stored = mapped_eligibility(job, catalog)
    assert stored is not None
    assert stored["exclusion_reason"] != "graduate_only"


def test_uppercase_product_and_hyphen_variants_are_neutralized(
    catalog: CompanyCatalog,
) -> None:
    for text in (
        "Must hold MS-SQL certification.",
        "MUST HOLD MS OFFICE CERTIFICATION.",
        "Must hold MS  sharepoint certification.",
    ):
        job = analyzed_job(title="Data Analyst Intern", requirements=text)
        assert job["student_eligibility"]["exclusion_reason"] == "graduate_only", text
        assert mapped_eligibility(job, catalog)["exclusion_reason"] is None, text


def test_genuine_masters_requirement_with_technology_terms_stays_graduate_only(
    catalog: CompanyCatalog,
) -> None:
    job = analyzed_job(
        title="MS Office Data Intern",
        requirements=(
            "Must be currently pursuing a Master's degree in Statistics. "
            "Must hold MS SQL Server and MS Excel certifications."
        ),
        description="You will automate MS Teams and MS 365 reports.",
    )
    stored = mapped_eligibility(job, catalog)
    assert stored["exclusion_reason"] == "graduate_only"
    assert stored["evidence_source"] == "requirements"
    assert "Master" in stored["evidence"]


def test_genuine_ms_degree_requirement_is_not_rewritten(
    catalog: CompanyCatalog,
) -> None:
    job = analyzed_job(
        title="Data Analyst Intern",
        requirements="Must be currently pursuing an MS degree. Must know MS Excel.",
    )
    stored = mapped_eligibility(job, catalog)
    assert stored["exclusion_reason"] == "graduate_only"
    assert "MS degree" in stored["evidence"]


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"requirements": "Must be currently pursuing a PhD. Uses MS Office."}, "phd_only"),
        (
            {"title": "Freshman Software Intern", "requirements": "MS Excel skills."},
            "freshman_only",
        ),
        ({"requirements": "Experience with MS Office is a plus."}, None),
    ],
)
def test_other_verdicts_are_preserved_without_rerunning_the_classifier(
    catalog: CompanyCatalog,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict,
    reason: str | None,
) -> None:
    job = analyzed_job(**overrides)
    assert job["student_eligibility"]["exclusion_reason"] == reason

    def fail(_row):
        raise AssertionError("classifier must not re-run for this verdict")

    monkeypatch.setattr(job_mapper, "analyze_student_eligibility", fail)
    stored = mapped_eligibility(job, catalog)
    assert stored == job_mapper.normalized_student_eligibility(job["student_eligibility"])


def test_graduate_only_without_technology_phrase_is_kept_without_rerun(
    catalog: CompanyCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = analyzed_job(requirements="Must be currently pursuing a Master's degree.")
    assert job["student_eligibility"]["exclusion_reason"] == "graduate_only"

    def fail(_row):
        raise AssertionError("no technology phrase, so no re-run")

    monkeypatch.setattr(job_mapper, "analyze_student_eligibility", fail)
    assert mapped_eligibility(job, catalog) == job_mapper.normalized_student_eligibility(
        job["student_eligibility"]
    )


def test_rerun_uses_a_rewritten_copy_of_the_row(
    catalog: CompanyCatalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = analyzed_job(
        title="Data Analyst Intern",
        requirements="Must hold MS SQL certification.",
        description="MS Excel required.",
    )
    seen: list[dict] = []
    real = job_mapper.analyze_student_eligibility

    def spy(row):
        seen.append(dict(row))
        return real(row)

    monkeypatch.setattr(job_mapper, "analyze_student_eligibility", spy)
    mapped_eligibility(job, catalog)
    assert len(seen) == 1
    rerun_row = seen[0]
    assert "Microsoft SQL" in rerun_row["requirements"]
    assert "Microsoft Excel" in rerun_row["description"]
    assert rerun_row["title"] == "Data Analyst Intern"
    assert rerun_row["extra"] == job["extra"]
    # Score outputs on the final job must not be read back as evidence.
    assert "student_eligibility" not in rerun_row
    assert "degree_level" not in rerun_row
    assert job["requirements"] == "Must hold MS SQL certification."


def test_stored_text_fields_keep_the_original_ms_wording(
    catalog: CompanyCatalog,
) -> None:
    job = analyzed_job(
        title="Data Analyst Intern",
        requirements="Must have MS SQL experience.",
    )
    mapped = job_mapper.map_final_jobs([job], catalog).jobs[0]
    assert mapped.requirements == "Must have MS SQL experience."
