import copy
import json
from datetime import date
from pathlib import Path

from app import config
from app.classify import TECHNICAL_ROLES, classify_company, classify_role
from app.ingest import (
    _analyze_rows_with_report,
    _read_csv,
    analyze_static_row,
    assemble_scored_job,
    deduplicate_rows,
    sort_scored_jobs,
    static_analysis_artifact_is_valid,
)
from app.normalize import build_row, infer_fields, map_headers
from app.profile import load_profile
from app.salary import parse_compensation
from app.scoring import score_job
from app.signals import (
    PostingAnalysisContext,
    build_profile_skill_matcher,
    count_tech_tools,
    detect_positive_signals,
    detect_red_flags,
    profile_match,
)


AS_OF = date(2026, 7, 30)
SAMPLE_CSV = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "sample_postings.csv"
)


def _row(**overrides):
    row = {
        "company": "",
        "title": "",
        "location": "",
        "compensation": "",
        "description": "",
        "requirements": "",
        "source_url": "",
        "date_posted": "",
        "deadline": "",
        "remote_status": "",
        "internship_type": "",
        "extra": {},
    }
    row.update(overrides)
    return row


def _representative_rows():
    return [
        _row(
            company="Stripe",
            title="Backend Engineering Intern",
            location="New York, NY",
            compensation="$45/hr",
            description=(
                "Own a project end-to-end with a dedicated mentor and weekly "
                "1:1s. Ship production code through code review."
            ),
            requirements="Python, Flask, SQL, REST APIs, Git, Docker",
            source_url="https://jobs.example.test/stripe/backend-intern",
            date_posted="2026-07-20",
            deadline="2026-08-30",
            internship_type="internship",
            extra={"source": "direct", "source_adapter": "greenhouse"},
        ),
        _row(
            company="HustleHub",
            title="Founding Engineer Intern",
            location="Remote",
            compensation="Equity only",
            description=(
                "Join the ground floor, wear many hats, and build our MVP from "
                "scratch. Do whatever it takes, including nights and weekends."
            ),
            requirements=(
                "3+ years with Python, Java, C++, Go, Rust, JavaScript, "
                "TypeScript, React, Node, SQL, Postgres, Redis, AWS, and Docker"
            ),
            source_url="https://jobs.example.test/hustle/founding-intern",
            internship_type="internship",
            extra={"source": "github", "source_adapter": "github_listings"},
        ),
        _row(
            company="QuickStart Careers",
            title="Remote Data Intern",
            location="Remote",
            compensation="$45/hr",
            description=(
                "No interview needed; start today. A $99 onboarding fee covers "
                "training. Message us on WhatsApp."
            ),
            requirements="Python and pandas",
            source_url="https://jobs.example.test/quickstart/data-intern",
            internship_type="internship",
        ),
        _row(
            company="ExampleCo",
            title="Operations Intern",
            location="Ithaca, NY",
            compensation="Paid stipend",
            description="Enter supplier invoices and perform repetitive filing.",
            requirements="Maintain the master data and master schedule.",
            source_url="https://jobs.example.test/example/operations-intern",
            internship_type="internship",
        ),
    ]


def _serialized(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_context_and_context_free_signal_and_scoring_paths_are_identical():
    row = _representative_rows()[0]
    profile = load_profile()
    known = config.load_known_companies()
    matcher = build_profile_skill_matcher(profile)
    context = PostingAnalysisContext.from_row(row, matcher)
    comp = parse_compensation(row["compensation"])
    role_cls = classify_role(row)
    company_cls = classify_company(
        row,
        known,
        role_is_technical=role_cls["role"] in TECHNICAL_ROLES,
    )

    reference_red = detect_red_flags(row, comp, role_cls, company_cls)
    reference_positive = detect_positive_signals(
        row, comp, role_cls, company_cls, profile, known
    )
    reference_profile = profile_match(row, role_cls, profile)
    reference_tools = count_tech_tools(
        " ".join([row["description"], row["requirements"]])
    )
    reference_score = score_job(
        row,
        comp,
        role_cls,
        company_cls,
        reference_red,
        reference_positive,
        reference_profile,
        profile,
        reference_tools,
        today=AS_OF,
    )

    optimized_red = detect_red_flags(
        row, comp, role_cls, company_cls, analysis_context=context
    )
    optimized_positive = detect_positive_signals(
        row,
        comp,
        role_cls,
        company_cls,
        profile,
        known,
        analysis_context=context,
    )
    optimized_profile = profile_match(
        row, role_cls, profile, analysis_context=context
    )
    optimized_score = score_job(
        row,
        comp,
        role_cls,
        company_cls,
        optimized_red,
        optimized_positive,
        optimized_profile,
        profile,
        list(context.full_technology_matches),
        today=AS_OF,
        analysis_context=context,
    )

    assert optimized_red == reference_red
    assert optimized_positive == reference_positive
    assert optimized_profile == reference_profile
    assert list(context.full_technology_matches) == reference_tools
    assert optimized_score == reference_score


def test_analysis_context_preserves_serialized_jobs_and_order():
    rows = _representative_rows()

    optimized = _analyze_rows_with_report(
        copy.deepcopy(rows),
        today=AS_OF,
        use_analysis_context=True,
    )
    reference = _analyze_rows_with_report(
        copy.deepcopy(rows),
        today=AS_OF,
        use_analysis_context=False,
    )

    assert optimized == reference
    assert _serialized(optimized) == _serialized(reference)


def test_analysis_context_matches_context_free_path_for_sample_fixture():
    headers, raw_rows = _read_csv(SAMPLE_CSV.read_text(encoding="utf-8"))
    mapping, _column_report = map_headers(headers)
    rows = []
    for row_number, raw in enumerate(raw_rows, start=1):
        row = build_row(raw, mapping)
        row["_row_number"] = row_number
        row["_inferred"] = infer_fields(row)
        rows.append(row)

    optimized = _analyze_rows_with_report(
        copy.deepcopy(rows),
        today=AS_OF,
        use_analysis_context=True,
    )
    reference = _analyze_rows_with_report(
        copy.deepcopy(rows),
        today=AS_OF,
        use_analysis_context=False,
    )

    assert _serialized(optimized) == _serialized(reference)


def test_profile_skill_patterns_are_reused_for_the_loaded_profile():
    profile = load_profile()

    first = build_profile_skill_matcher(profile)
    second = build_profile_skill_matcher(profile)

    assert first is second
    assert len(first.patterns) == len(profile["skills"])


def test_pure_static_artifact_pipeline_matches_full_analysis_after_json_round_trip():
    rows = _representative_rows()
    profile = load_profile()
    known = config.load_known_companies()
    unique_rows, duplicate_report = deduplicate_rows(copy.deepcopy(rows))

    artifacts = [
        analyze_static_row(row, profile=profile, known=known)
        for row in unique_rows
    ]
    json_artifacts = [
        json.loads(json.dumps(artifact, ensure_ascii=False))
        for artifact in artifacts
    ]
    jobs = [
        assemble_scored_job(
            row,
            artifact,
            profile=profile,
            today=AS_OF,
        )
        for row, artifact in zip(unique_rows, json_artifacts)
    ]
    sort_scored_jobs(jobs)
    reference_jobs, reference_duplicates = _analyze_rows_with_report(
        copy.deepcopy(rows),
        today=AS_OF,
    )

    assert all(
        static_analysis_artifact_is_valid(artifact)
        for artifact in json_artifacts
    )
    assert _serialized(jobs) == _serialized(reference_jobs)
    assert duplicate_report == reference_duplicates
    assert all("score" not in artifact for artifact in artifacts)
    assert all("deadline_days_left" not in artifact for artifact in artifacts)
    assert all("static_scoring" in artifact for artifact in artifacts)
    assert all("eligibility_context" in artifact for artifact in artifacts)


def test_static_artifact_caches_eligibility_and_non_deadline_categories_only():
    row = _row(
        company="ExampleCo",
        title="Software Engineering Intern",
        location="Remote",
        remote_status="remote",
        compensation="$35/hr",
        description=(
            "Build Python APIs. United States citizenship is required; "
            "sponsorship is not available."
        ),
        requirements=(
            "Minimum qualifications:\n"
            "Currently pursuing a bachelor's degree and graduating in 2028.\n"
            "Preferred qualifications:\n"
            "Currently pursuing a master's degree."
        ),
        deadline="2026-08-01",
    )
    profile = load_profile()
    artifact = analyze_static_row(
        row,
        profile=profile,
        known=config.load_known_companies(),
    )

    assert artifact["schema_version"] == 2
    assert static_analysis_artifact_is_valid(artifact)
    assert set(artifact["static_scoring"]["categories"]) == {
        "role_relevance",
        "compensation",
        "legitimacy",
        "learning_value",
        "technical_depth",
        "effort_vs_value",
        "location_convenience",
    }
    assert "deadline_urgency" not in artifact["static_scoring"]["categories"]
    assert artifact["static_scoring"]["student_eligibility"]["eligible"] is True
    assert artifact["eligibility_context"]["normalized_evidence"]
    segments = artifact["eligibility_context"]["qualification_segments"]
    assert any(
        item["preferred"]
        for item in segments["requirements"]
    )
    assert "deadline_days_left" not in artifact
    assert "id" not in artifact
    assert "extra" not in artifact


def test_one_static_artifact_recomputes_deadline_and_final_decisions():
    row = _representative_rows()[0]
    row["deadline"] = "2026-08-01"
    profile = load_profile()
    artifact = analyze_static_row(
        row,
        profile=profile,
        known=config.load_known_companies(),
    )

    open_job = assemble_scored_job(
        row,
        artifact,
        profile=profile,
        today=date(2026, 7, 30),
    )
    expired_job = assemble_scored_job(
        row,
        artifact,
        profile=profile,
        today=date(2026, 8, 2),
    )

    assert open_job["deadline_days_left"] == 2
    assert expired_job["deadline_days_left"] == -1
    assert (
        open_job["score"]["categories"]["deadline_urgency"]
        != expired_job["score"]["categories"]["deadline_urgency"]
    )
    assert open_job["score"]["action"] != expired_job["score"]["action"]
    assert open_job["score"]["categories"]["role_relevance"] == (
        expired_job["score"]["categories"]["role_relevance"]
    )


def test_static_artifact_validation_requires_eligibility_and_static_scores():
    artifact = analyze_static_row(
        _representative_rows()[0],
        profile=load_profile(),
        known=config.load_known_companies(),
    )

    without_eligibility = copy.deepcopy(artifact)
    without_eligibility.pop("eligibility_context")
    without_scoring = copy.deepcopy(artifact)
    without_scoring["static_scoring"]["categories"].pop("effort_vs_value")
    with_cached_deadline = copy.deepcopy(artifact)
    with_cached_deadline["static_scoring"]["categories"][
        "deadline_urgency"
    ] = {"score": 100, "explanation": "incorrectly cached"}

    assert not static_analysis_artifact_is_valid(without_eligibility)
    assert not static_analysis_artifact_is_valid(without_scoring)
    assert not static_analysis_artifact_is_valid(with_cached_deadline)
