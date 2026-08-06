import logging

import pytest

from watcher.config import (
    WORKDAY_DETAIL_EARLY_CAREER,
    WORKDAY_DETAIL_INTERNSHIP,
    WORKDAY_DETAIL_NONE,
    CompanyCfg,
)
from watcher.sources.base import SourceFetchError, SourceSchemaError
from watcher.sources.workday import WorkdaySource, workday_detail_candidate_reason
from watcher.tests.test_sources import load_fixture


def company(*, policy=WORKDAY_DETAIL_INTERNSHIP):
    return CompanyCfg(
        name="Example",
        ats="workday",
        token="tenant",
        workday_shard="wd5",
        workday_site="Site",
        workday_detail_policy=policy,
    )


def posting(
    title="Technology Intern",
    path="/job/New-York/Technology-Intern_R123-1",
    requisition="R123",
    **fields,
):
    return {
        "title": title,
        "externalPath": path,
        "locationsText": fields.pop("locationsText", "3 Locations"),
        "postedOn": fields.pop("postedOn", "Posted Yesterday"),
        "bulletFields": [requisition],
        **fields,
    }


def source_for(search_payload, details, **kwargs):
    detail_calls = []

    def request_detail(url, source_name):
        detail_calls.append(url)
        value = details[url] if url in details else details.get("default")
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value

    source = WorkdaySource(
        min_interval_seconds=0,
        request_json=lambda url, payload, source_name: search_payload,
        request_detail_json=request_detail,
        sleeper=lambda delay: None,
        jitter=lambda low, high: low,
        **kwargs,
    )
    return source, detail_calls


@pytest.mark.parametrize(
    ("title", "metadata", "reason"),
    [
        ("Software Engineering Intern", {}, "internship"),
        ("Data Science Co\u2011op", {}, "co_op"),
        ("Summer 2027 Technology Analyst", {}, "season_year"),
        ("Student Developer", {}, "student"),
        ("Campus Technology Analyst", {}, "campus"),
        ("Engineering Apprenticeship", {}, "apprenticeship"),
        ("Technology Analyst", {"jobFamily": "University Program"}, "university"),
    ],
)
def test_general_policy_selects_only_clear_student_program_signals(
    title,
    metadata,
    reason,
):
    assert (
        workday_detail_candidate_reason(title, metadata, WORKDAY_DETAIL_INTERNSHIP)
        == reason
    )


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Financial Analyst",
        "Technology Associate",
        "Application Developer",
        "Leadership Program",
        "Technology Program",
    ],
)
def test_general_policy_does_not_select_professional_or_generic_vague_titles(title):
    assert workday_detail_candidate_reason(title, {}, WORKDAY_DETAIL_INTERNSHIP) is None


@pytest.mark.parametrize(
    "title",
    [
        "Technology Programme",
        "Engineering Program",
        "Software Development Programme",
        "Summer Technology Analyst Program",
        "Technical Program",
    ],
)
def test_approved_programme_titles_require_verified_early_career_policy(title):
    assert workday_detail_candidate_reason(title, {}, WORKDAY_DETAIL_INTERNSHIP) is None
    assert (
        workday_detail_candidate_reason(title, {}, WORKDAY_DETAIL_EARLY_CAREER)
        == "early_career_program"
    )


def test_none_policy_selects_no_details():
    assert workday_detail_candidate_reason(
        "Software Intern",
        {"studentProgram": "Internship"},
        WORKDAY_DETAIL_NONE,
    ) is None


def test_candidate_limit_fails_before_any_detail_request():
    payload = {
        "jobPostings": [
            posting("Software Intern", f"/job/Test/Intern-{index}_R{index}", f"R{index}")
            for index in range(3)
        ],
        "total": 3,
    }
    source, calls = source_for(
        payload,
        {"default": load_fixture("workday_detail_enriched.json")},
        max_detail_candidates=2,
    )

    with pytest.raises(SourceSchemaError, match="detail candidate limit"):
        source.fetch(company())

    assert calls == []
    assert source.last_diagnostics.detail_candidates == 3
    assert source.last_diagnostics.detail_requests == 0


def test_duplicate_listing_paths_trigger_one_detail_request():
    shared = posting()
    source, calls = source_for(
        {"jobPostings": [shared, dict(shared)], "total": 2},
        {"default": load_fixture("workday_detail_enriched.json")},
    )

    rows = source.fetch(company())

    assert len(rows) == 2
    assert len(calls) == 1
    assert source.last_diagnostics.detail_candidates == 1
    assert source.last_diagnostics.detail_requests == 1
    assert source.last_diagnostics.rows_enriched == 2


def test_detail_fixture_merges_every_supported_canonical_and_structured_field():
    source, _calls = source_for(
        {"jobPostings": [posting()], "total": 1},
        {"default": load_fixture("workday_detail_enriched.json")},
    )

    row = source.fetch(company())[0]

    assert "Design and build Python services" in row["description"]
    assert row["requirements"] == (
        "Experience with Python and SQL. Pursuing a bachelor's degree in Computer Science."
    )
    assert row["location"] == "New York, NY; Boston, MA; Remote - US"
    assert row["date_posted"] == "2026-08-01"
    assert row["deadline"] == "2026-09-01"
    assert row["internship_type"] == "Intern; Full time"
    assert row["remote_status"] == "Hybrid"
    assert row["compensation"] == "$30 - $40 per hour"
    assert row["extra"]["student_program"] == "University Internship"
    assert row["extra"]["degree_requirements"] == [
        "Bachelor's degree",
        "Computer Science",
    ]
    assert row["extra"]["country"]["descriptor"] == "United States"
    assert row["extra"]["workday_detail_status"] == "enriched"
    assert row["extra"]["source_requisition_id"] == "R123"
    assert source.last_diagnostics.detail_successes == 1
    assert source.last_diagnostics.descriptions_filled == 1
    assert source.last_diagnostics.locations_expanded == 1
    assert source.last_diagnostics.canonical_fields_filled >= 8


def test_blank_detail_values_never_erase_nonblank_listing_fields():
    original = posting(
        jobDescription="Listing description",
        locationsText="Boston, MA",
        postedOn="2026-07-31",
        timeType="Intern",
    )
    detail = {
        "jobPostingInfo": {
            "jobReqId": "R123",
            "jobDescription": "",
            "location": "",
            "startDate": "",
            "timeType": "",
            "remoteType": "",
        }
    }
    source, _calls = source_for(
        {"jobPostings": [original], "total": 1},
        {"default": detail},
    )

    row = source.fetch(company())[0]

    assert row["description"] == "Listing description"
    assert row["location"] == "Boston, MA"
    assert row["date_posted"] == "2026-07-31"
    assert row["extra"]["time_type"] == "Intern"


def test_conflicting_detail_requisition_is_rejected_without_changing_identity():
    source, _calls = source_for(
        {"jobPostings": [posting()], "total": 1},
        {"default": load_fixture("workday_detail_conflicting_requisition.json")},
    )

    row = source.fetch(company())[0]

    assert row["extra"]["source_requisition_id"] == "R123"
    assert row["description"] == ""
    assert row["extra"]["workday_detail_status"] == "failed"
    assert row["extra"]["workday_detail_error"] == "requisition_id_conflict"
    assert source.last_diagnostics.detail_failures == 1


def test_transient_detail_failure_retries_then_enriches():
    attempts = 0

    def detail():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise SourceFetchError("temporary", error_code="timeout", retryable=True)
        return load_fixture("workday_detail_enriched.json")

    source, _calls = source_for(
        {"jobPostings": [posting()], "total": 1},
        {"default": detail},
    )

    row = source.fetch(company())[0]

    assert row["extra"]["workday_detail_status"] == "enriched"
    assert attempts == 3
    assert source.last_diagnostics.detail_requests == 3
    assert source.last_diagnostics.detail_retries == 2
    assert source.last_diagnostics.request_attempts == 4
    assert source.last_diagnostics.retry_attempts == 2


def test_permanent_detail_failure_is_not_retried_and_retains_listing():
    error = SourceFetchError(
        "permanent",
        error_code="permanent_http_error",
        status_code=400,
        retryable=False,
    )
    source, calls = source_for(
        {"jobPostings": [posting()], "total": 1},
        {"default": error},
    )

    row = source.fetch(company())[0]

    assert len(calls) == 1
    assert row["title"] == "Technology Intern"
    assert row["extra"]["source_requisition_id"] == "R123"
    assert row["extra"]["workday_detail_status"] == "failed"
    assert row["extra"]["workday_detail_error"] == "permanent_http_error"
    assert source.last_diagnostics.detail_failures == 1


def test_malformed_detail_retains_listing_and_does_not_discard_other_jobs():
    first_path = posting()["externalPath"]
    second = posting("Software Intern", "/job/Boston/Software-Intern_R124-1", "R124")
    second_path = second["externalPath"]
    good = load_fixture("workday_detail_enriched.json")
    good["jobPostingInfo"]["jobReqId"] = "R124"
    source, _calls = source_for(
        {"jobPostings": [posting(), second], "total": 2},
        {
            WorkdaySource.detail_endpoint("tenant", "wd5", "Site", first_path): {
                "unexpected": {}
            },
            WorkdaySource.detail_endpoint("tenant", "wd5", "Site", second_path): good,
        },
    )

    rows = source.fetch(company())

    assert len(rows) == 2
    assert rows[0]["extra"]["workday_detail_status"] == "failed"
    assert rows[0]["description"] == ""
    assert rows[1]["extra"]["workday_detail_status"] == "enriched"
    assert source.last_diagnostics.detail_failures == 1
    assert source.last_diagnostics.detail_successes == 1
    assert source.last_diagnostics.detail_enrichment_degraded is False


@pytest.mark.parametrize(
    "detail",
    [
        {"jobPostingInfo": {}},
        {"jobPostingInfo": {"unexpectedField": "not a Workday detail record"}},
    ],
)
def test_materially_changed_detail_schema_is_not_counted_as_enriched(detail):
    source, _calls = source_for(
        {"jobPostings": [posting()], "total": 1},
        {"default": detail},
    )

    row = source.fetch(company())[0]

    assert row["title"] == "Technology Intern"
    assert row["extra"]["workday_detail_status"] == "failed"
    assert row["extra"]["workday_detail_error"] == "schema_error"
    assert source.last_diagnostics.detail_successes == 0
    assert source.last_diagnostics.detail_failures == 1
    assert source.last_diagnostics.detail_enrichment_degraded is True


@pytest.mark.parametrize(
    ("detail_info", "expected_row", "expected_extra", "expected_status"),
    [
        (
            {"jobDescription": "Build APIs."},
            {"description": "Build APIs."},
            {},
            "enriched",
        ),
        (
            {"requirements": "Python required."},
            {"requirements": "Python required."},
            {},
            "enriched",
        ),
        ({"requisitionId": "R123"}, {}, {"source_requisition_id": "R123"}, "success"),
        ({"location": "New York, NY"}, {"location": "New York, NY"}, {}, "enriched"),
        (
            {"additionalLocations": ["Boston, MA", "Remote - US"]},
            {"location": "Boston, MA; Remote - US"},
            {},
            "enriched",
        ),
        ({"startDate": "2026-08-01"}, {"date_posted": "2026-08-01"}, {}, "enriched"),
        (
            {"applicationDeadline": "2026-09-01"},
            {"deadline": "2026-09-01"},
            {},
            "enriched",
        ),
        (
            {"employmentType": "Internship"},
            {"internship_type": "Internship"},
            {"employment_type": "Internship"},
            "enriched",
        ),
        ({"workerType": "Employee"}, {}, {"worker_type": "Employee"}, "success"),
        ({"remoteStatus": "Hybrid"}, {"remote_status": "Hybrid"}, {}, "enriched"),
        (
            {"salaryRange": "$30-$40 per hour"},
            {"compensation": "$30-$40 per hour"},
            {},
            "enriched",
        ),
        (
            {"studentProgram": "University Internship"},
            {},
            {"student_program": "University Internship"},
            "success",
        ),
        (
            {"degreeRequirements": ["Bachelor's degree"]},
            {},
            {"degree_requirements": ["Bachelor's degree"]},
            "success",
        ),
        ({"classYear": "2027"}, {}, {"class_year": "2027"}, "success"),
        (
            {"eligibilityRequirements": "Currently enrolled"},
            {},
            {"eligibility_requirements": "Currently enrolled"},
            "success",
        ),
    ],
)
def test_sparse_supported_detail_categories_are_valid(
    detail_info,
    expected_row,
    expected_extra,
    expected_status,
):
    source, _calls = source_for(
        {"jobPostings": [posting()], "total": 1},
        {"default": {"jobPostingInfo": detail_info}},
    )

    row = source.fetch(company())[0]

    for field, expected in expected_row.items():
        assert row[field] == expected
    for field, expected in expected_extra.items():
        assert row["extra"][field] == expected
    assert row["extra"]["workday_detail_status"] == expected_status
    assert source.last_diagnostics.detail_successes == 1
    assert source.last_diagnostics.detail_failures == 0
    assert source.last_diagnostics.detail_enrichment_degraded is False


def test_recognized_unchanged_detail_is_a_valid_success():
    source, _calls = source_for(
        {"jobPostings": [posting()], "total": 1},
        {"default": {"jobPostingInfo": {"title": "Technology Intern"}}},
    )

    row = source.fetch(company())[0]

    assert row["title"] == "Technology Intern"
    assert row["extra"]["workday_detail_status"] == "success"
    assert source.last_diagnostics.detail_successes == 1
    assert source.last_diagnostics.detail_failures == 0
    assert source.last_diagnostics.detail_enrichment_degraded is False


@pytest.mark.parametrize("status", [404, 410])
def test_disappeared_detail_is_retained_inactive(status):
    error = SourceFetchError(
        "gone",
        error_code="permanent_http_error",
        status_code=status,
        retryable=False,
    )
    source, _calls = source_for(
        {"jobPostings": [posting()], "total": 1},
        {"default": error},
    )

    row = source.fetch(company())[0]

    assert row["extra"]["workday_detail_status"] == "disappeared"
    assert row["extra"]["active"] is False
    assert source.last_diagnostics.disappeared_postings == 1


def test_all_same_reason_detail_failures_surface_degraded_diagnostic(caplog):
    error = SourceFetchError(
        "private raw detail must not be logged",
        error_code="timeout",
        retryable=False,
    )
    source, _calls = source_for(
        {
            "jobPostings": [
                posting(),
                posting("Software Intern", "/job/Boston/Intern_R124-1", "R124"),
            ],
            "total": 2,
        },
        {"default": error},
    )

    with caplog.at_level(logging.WARNING, logger="watcher.sources.workday"):
        rows = source.fetch(company())

    assert len(rows) == 2
    assert source.last_diagnostics.detail_enrichment_degraded is True
    assert source.last_diagnostics.detail_degraded_reason == "timeout"
    assert "detail_candidates=2" in caplog.text
    assert "private raw detail" not in caplog.text
