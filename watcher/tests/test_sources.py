import json
import logging
from pathlib import Path

import pytest

from backend.app.normalize import CANONICAL_COLUMNS
from watcher.config import CompanyCfg
from watcher.sources import (
    AshbySource,
    GitHubListingsSource,
    GreenhouseSource,
    LeverSource,
    SourceError,
    SourceFetchError,
    SmartRecruitersSource,
    SourceSchemaError,
    WorkableSource,
    WorkdaySource,
)

FIXTURES = Path(__file__).parent / "fixtures"
TEST_GITHUB_FEED_URL = "https://fixtures.example.test/internships/listings.json"


def workday_company(name="Merck"):
    return CompanyCfg(
        name=name,
        ats="workday",
        token="merck",
        workday_shard="wd5",
        workday_site="Search_Jobs",
    )


def workday_posting(title="Software Engineer Intern", external_path="/job/Test/Software-Engineer_R1"):
    return {
        "title": title,
        "externalPath": external_path,
        "locationsText": "Rahway, NJ",
        "postedOn": "Posted Today",
        "jobDescription": "Build software.",
        "bulletFields": ["R1"],
    }


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def assert_canonical_row(row: dict) -> None:
    assert set(row) == set(CANONICAL_COLUMNS) | {"extra"}
    assert row["source_url"]
    assert "source" in row["extra"]
    assert "source_adapter" in row["extra"]


def test_fixture_json_round_trips_utf8_non_ascii(tmp_path):
    expected = {
        "company_name": "Caf\u00e9 Robotics",
        "title": "Students\u2019 CXL\u00ae Internship",
        "locations": ["Montr\u00e9al, QC"],
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")

    loaded = json.loads(path.read_text(encoding="utf-8"))
    output = tmp_path / "fixture_roundtrip.json"
    output.write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")

    assert json.loads(output.read_text(encoding="utf-8")) == expected
    assert b"\xe2\x80\x99" in output.read_bytes()
    assert b"\xc2\xae" in output.read_bytes()


def test_greenhouse_fixture_to_canonical_rows():
    fixture_path = FIXTURES / "greenhouse_asteraearlycareer2026.json"
    fixture_path.read_bytes().decode("ascii")
    payload = load_fixture(fixture_path.name)
    company = CompanyCfg(name="Astera Labs", ats="greenhouse", token="asteraearlycareer2026")

    rows = GreenhouseSource().parse(payload, company)

    assert len(rows) == 5
    first = rows[0]
    assert_canonical_row(first)
    assert first["company"] == "Astera Labs"
    assert first["title"] == "Design Verification Engineer (Intern 2026)"
    assert first["location"] == "Toronto, Ontario, Canada"
    assert first["source_url"] == "https://job-boards.greenhouse.io/asteraearlycareer2026/jobs/4611422005"
    assert first["date_posted"] == "2025-09-22"
    assert first["internship_type"] == "Internship"
    assert first["extra"]["source"] == "direct"
    assert first["extra"]["source_adapter"] == "greenhouse"
    assert first["extra"]["greenhouse_company_name"] == "Astera Labs Early Career"
    assert "<" not in first["description"]
    assert "\u2019" in first["description"]
    assert "\u00ae" in first["description"]
    assert "\u2122" in first["description"]
    for mojibake in (
        "\u00e2\u20ac\u2122",
        "\u00c2\u00ae",
        "\u00e2\u201e\u00a2",
        "\\u00e2\\u20ac\\u2122",
        "\\u00c2\\u00ae",
        "\\u00e2\\u201e\\u00a2",
        "\ufffd",
    ):
        assert mojibake not in first["description"]


def test_greenhouse_unexpected_shape_raises():
    with pytest.raises(SourceSchemaError, match="jobs"):
        GreenhouseSource().parse({"openings": []}, CompanyCfg(name="Astera Labs", token="asteraearlycareer2026"))


def test_lever_fixture_to_canonical_rows():
    payload = load_fixture("lever_ifm_us.json")
    company = CompanyCfg(name="Institute of Foundation Models", ats="lever", token="ifm-us")

    rows = LeverSource().parse(payload, company)

    assert len(rows) == 43
    first = rows[0]
    assert_canonical_row(first)
    assert first["company"] == "Institute of Foundation Models"
    assert first["title"] == "AI Research Internship - LLM"
    assert first["location"] == "Sunnyvale, CA"
    assert first["source_url"] == "https://jobs.lever.co/ifm-us/5342e333-61b9-406d-bfea-61a687a94d1f/apply"
    assert first["date_posted"] == "2025-07-24"
    assert first["remote_status"] == "On-site"
    assert first["compensation"] == "$100,000 - $140,000 per year"
    assert first["extra"]["source"] == "direct"
    assert first["extra"]["source_adapter"] == "lever"
    assert first["extra"]["posting_url"] == "https://jobs.lever.co/ifm-us/5342e333-61b9-406d-bfea-61a687a94d1f"
    assert "Institute of Foundation Models" in first["description"]


def test_lever_unexpected_shape_raises():
    with pytest.raises(SourceSchemaError, match="list"):
        LeverSource().parse({"postings": []}, CompanyCfg(name="Institute of Foundation Models", token="ifm-us"))


def test_workday_fixture_to_canonical_rows():
    payload = load_fixture("workday_capitalone_intern.json")
    company = CompanyCfg(
        name="Capital One",
        ats="workday",
        token="capitalone",
        workday_shard="wd12",
        workday_site="Capital_One",
    )

    rows = WorkdaySource().parse(payload, company)

    assert len(rows) == 5
    first = rows[0]
    assert_canonical_row(first)
    assert first["company"] == "Capital One"
    assert first["title"] == "Lead Data Engineer"
    assert first["location"] == "3 Locations"
    assert first["source_url"] == "https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/Richmond-VA/Lead-Data-Engineer_R241466-1"
    assert first["date_posted"] == "Posted Yesterday"
    assert first["internship_type"] == ""
    assert first["extra"]["source"] == "direct"
    assert first["extra"]["source_adapter"] == "workday"
    assert first["extra"]["workday_tenant"] == "capitalone"
    assert first["extra"]["workday_shard"] == "wd12"
    assert first["extra"]["workday_site"] == "Capital_One"
    assert first["extra"]["time_type"] == "Full time"


def test_workday_unexpected_shape_raises():
    company = CompanyCfg(
        name="Capital One",
        ats="workday",
        token="capitalone",
        workday_shard="wd12",
        workday_site="Capital_One",
    )
    with pytest.raises(SourceSchemaError, match="jobPostings"):
        WorkdaySource().parse({"jobs": []}, company)


def test_workday_missing_shard_fails_loudly():
    payload = load_fixture("workday_capitalone_intern.json")
    company = CompanyCfg(name="Capital One", ats="workday", token="capitalone", workday_site="Capital_One")

    with pytest.raises(SourceError, match="workday_shard"):
        WorkdaySource().parse(payload, company)


def test_workday_malformed_posting_followed_by_valid_posting_is_skipped():
    source = WorkdaySource()
    rows = source.parse(
        {"jobPostings": [{"title": ""}, workday_posting()], "total": 2},
        workday_company(),
    )

    assert [item["title"] for item in rows] == ["Software Engineer Intern"]
    assert source.last_diagnostics.raw_postings_seen == 2
    assert source.last_diagnostics.valid_rows_retained == 1
    assert source.last_diagnostics.malformed_postings_skipped == 1


def test_workday_valid_posting_followed_by_malformed_posting_is_retained():
    source = WorkdaySource()
    rows = source.parse(
        {"jobPostings": [workday_posting(), {"externalPath": "/job/Test/Missing-Title_R2"}], "total": 2},
        workday_company(),
    )

    assert [item["title"] for item in rows] == ["Software Engineer Intern"]
    assert source.last_diagnostics.skip_reasons == (("missing_title", 1),)


@pytest.mark.parametrize(
    ("posting", "reason"),
    [
        ({"externalPath": "/job/Test/Missing-Title_R2"}, "missing_title"),
        ({"title": "Missing URL Intern"}, "missing_external_path"),
        ({"title": "", "externalPath": ""}, "missing_title_and_external_path"),
        ("not-an-object", "posting_not_object"),
    ],
)
def test_workday_malformed_record_reason_categories(posting, reason):
    source = WorkdaySource()
    rows = source.parse(
        {"jobPostings": [workday_posting(), posting], "total": 2},
        workday_company(),
    )

    assert len(rows) == 1
    assert source.last_diagnostics.skip_reasons == ((reason, 1),)


def test_workday_mixed_parse_logs_one_bounded_aggregate_warning_without_payload(caplog):
    source = WorkdaySource()
    payload = {
        "jobPostings": [
            workday_posting(),
            {"title": "", "externalPath": "/job/private?token=DO_NOT_LOG", "secret_marker": "DO_NOT_LOG"},
            {"title": "No URL", "secret_marker": "DO_NOT_LOG"},
        ],
        "total": 3,
    }

    with caplog.at_level(logging.WARNING, logger="watcher.sources.workday"):
        rows = source.parse(payload, workday_company())

    warnings = [record.getMessage() for record in caplog.records if "Skipped" in record.getMessage()]
    assert len(rows) == 1
    assert len(warnings) == 1
    assert "Merck" in warnings[0]
    assert "2 malformed" in warnings[0]
    assert "1 valid posting retained" in warnings[0]
    assert "missing_title=1" in warnings[0]
    assert "missing_external_path=1" in warnings[0]
    assert "DO_NOT_LOG" not in warnings[0]
    assert len(warnings[0]) < 500


def test_workday_other_record_schema_error_is_skipped_with_stable_reason(monkeypatch):
    source = WorkdaySource()
    original = source._parse_posting

    def parse_posting(posting, company, token, shard, site):
        if posting["title"] == "Record Schema Problem":
            raise SourceSchemaError("record-only schema problem")
        return original(posting, company, token, shard, site)

    monkeypatch.setattr(source, "_parse_posting", parse_posting)
    rows = source.parse(
        {
            "jobPostings": [
                workday_posting(),
                workday_posting("Record Schema Problem", "/job/Test/Problem_R2"),
            ],
            "total": 2,
        },
        workday_company(),
    )

    assert len(rows) == 1
    assert source.last_diagnostics.skip_reasons == (("posting_schema_error", 1),)


def test_workday_fetch_skips_malformed_records_across_pages_and_uses_raw_offsets(monkeypatch):
    payloads = [
        {
            "jobPostings": [workday_posting("First Intern", "/job/Test/First_R1"), {"title": "Bad"}],
            "total": 4,
        },
        {
            "jobPostings": [{"externalPath": "/job/Test/Bad_R2"}, workday_posting("Later Intern", "/job/Test/Later_R3")],
            "total": 4,
        },
    ]
    offsets = []

    def fake_post_json(url, data, source_name):
        offsets.append(data["offset"])
        return payloads.pop(0)

    monkeypatch.setattr("watcher.sources.workday.post_json", fake_post_json)
    source = WorkdaySource()
    rows = source.fetch(workday_company())

    assert offsets == [0, 2]
    assert [item["title"] for item in rows] == ["First Intern", "Later Intern"]
    assert source.last_diagnostics.raw_postings_seen == 4
    assert source.last_diagnostics.valid_rows_retained == 2
    assert source.last_diagnostics.malformed_postings_skipped == 2


def test_workday_parse_nonempty_all_malformed_raises_schema_error():
    with pytest.raises(SourceSchemaError, match="none were valid"):
        WorkdaySource().parse(
            {"jobPostings": [{"title": ""}, "bad"], "total": 2},
            workday_company(),
        )


def test_workday_complete_paginated_fetch_with_no_valid_rows_raises(monkeypatch):
    payloads = [
        {"jobPostings": [{"title": "Bad"}], "total": 2},
        {"jobPostings": [{"externalPath": "/job/Test/Bad_R2"}], "total": 2},
    ]

    def fake_post_json(url, data, source_name):
        return payloads.pop(0)

    monkeypatch.setattr("watcher.sources.workday.post_json", fake_post_json)
    with pytest.raises(SourceSchemaError, match="2 posting record.*none were valid"):
        WorkdaySource().fetch(workday_company())


def test_workday_genuinely_empty_board_is_successful():
    source = WorkdaySource()

    assert source.parse({"jobPostings": [], "total": 0}, workday_company()) == []
    assert source.last_diagnostics.raw_postings_seen == 0
    assert source.last_diagnostics.malformed_postings_skipped == 0


def test_workday_invalid_total_still_raises():
    with pytest.raises(SourceSchemaError, match="total to be an integer"):
        WorkdaySource().parse({"jobPostings": [], "total": "zero"}, workday_company())


@pytest.mark.parametrize(
    ("source", "company", "payload"),
    [
        (
            GreenhouseSource(),
            CompanyCfg(name="Acme", ats="greenhouse", token="acme"),
            {"jobs": [{"title": "Intern", "absolute_url": "https://example.test/1"}, {"title": "broken"}]},
        ),
        (
            LeverSource(),
            CompanyCfg(name="Acme", ats="lever", token="acme"),
            [{"text": "Intern", "applyUrl": "https://example.test/1"}, {"text": "broken"}],
        ),
        (
            AshbySource(),
            CompanyCfg(name="Acme", ats="ashby", token="acme"),
            {"jobs": [{"title": "Intern", "applyUrl": "https://example.test/1"}, {"title": "broken"}]},
        ),
        (
            SmartRecruitersSource(),
            CompanyCfg(name="Acme", ats="smartrecruiters", token="acme"),
            {
                "content": [
                    {"name": "Intern", "id": "1", "postingUrl": "https://example.test/1"},
                    {"name": "broken"},
                ],
                "totalFound": 2,
            },
        ),
        (
            WorkableSource(),
            CompanyCfg(name="Acme", ats="workable", token="acme"),
            {
                "results": [
                    {"title": "Intern", "shortcode": "ONE", "url": "https://example.test/1"},
                    {"title": "broken"},
                ],
                "total": 2,
            },
        ),
    ],
)
def test_direct_adapters_retain_valid_rows_when_one_record_is_malformed(source, company, payload, caplog):
    with caplog.at_level(logging.WARNING, logger="watcher.sources.base"):
        rows = source.parse(payload, company)

    assert [row["title"] for row in rows] == ["Intern"]
    assert "Skipped 1 malformed" in caplog.text
    assert "Acme" in caplog.text
    assert "broken" not in caplog.text


@pytest.mark.parametrize(
    ("source", "company", "payload"),
    [
        (GreenhouseSource(), CompanyCfg(name="Acme", ats="greenhouse", token="acme"), {"jobs": [{"title": "broken"}]}),
        (LeverSource(), CompanyCfg(name="Acme", ats="lever", token="acme"), [{"text": "broken"}]),
        (AshbySource(), CompanyCfg(name="Acme", ats="ashby", token="acme"), {"jobs": [{"title": "broken"}]}),
        (
            SmartRecruitersSource(),
            CompanyCfg(name="Acme", ats="smartrecruiters", token="acme"),
            {"content": [{"name": "broken"}], "totalFound": 1},
        ),
        (
            WorkableSource(),
            CompanyCfg(name="Acme", ats="workable", token="acme"),
            {"results": [{"title": "broken"}], "total": 1},
        ),
    ],
)
def test_direct_adapters_still_fail_nonempty_all_malformed_payloads(source, company, payload):
    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.parse(payload, company)


def test_ashby_all_unlisted_jobs_are_a_successful_empty_result():
    payload = {
        "jobs": [
            {"title": "Private", "applyUrl": "https://example.test/private", "isListed": False}
        ]
    }

    assert AshbySource().parse(payload, CompanyCfg(name="Acme", ats="ashby", token="acme")) == []


def test_smartrecruiters_repeated_page_fails_instead_of_looping(monkeypatch):
    offsets = []
    payload = {
        "content": [{"name": "Intern", "id": "1", "postingUrl": "https://example.test/1"}],
        "totalFound": 3,
    }

    def fake_fetch_json(url, source_name):
        offsets.append(int(url.rsplit("offset=", 1)[1]))
        return payload

    monkeypatch.setattr("watcher.sources.smartrecruiters.fetch_json", fake_fetch_json)
    with pytest.raises(SourceSchemaError, match="repeated pagination page"):
        SmartRecruitersSource().fetch(
            CompanyCfg(name="Acme", ats="smartrecruiters", token="acme")
        )

    assert offsets == [0, 1]


def test_workday_repeated_page_fails_and_diagnostics_do_not_leak(monkeypatch):
    source = WorkdaySource()
    source.parse(
        {"jobPostings": [workday_posting(), {"title": "broken"}], "total": 2},
        workday_company(),
    )
    assert source.last_diagnostics.malformed_postings_skipped == 1
    offsets = []
    payload = {"jobPostings": [workday_posting()], "total": 3}

    def fake_post_json(url, data, source_name):
        offsets.append(data["offset"])
        return payload

    monkeypatch.setattr("watcher.sources.workday.post_json", fake_post_json)
    with pytest.raises(SourceSchemaError, match="repeated pagination page"):
        source.fetch(workday_company())

    assert offsets == [0, 1]
    assert source.last_diagnostics.malformed_postings_skipped == 0
    assert source.last_diagnostics.raw_postings_seen == 0


def test_ashby_fixture_to_canonical_rows():
    payload = load_fixture("ashby_chainalysis_careers.json")
    company = CompanyCfg(name="Chainalysis", ats="ashby", token="chainalysis-careers")

    rows = AshbySource().parse(payload, company)

    assert len(rows) == 47
    first = rows[0]
    assert_canonical_row(first)
    assert first["company"] == "Chainalysis"
    assert first["title"] == "Senior Manager, Engineering-Clustering & Exposure"
    assert first["location"] == "Aarhus Office, Denmark Copenhagen"
    assert first["source_url"] == "https://jobs.ashbyhq.com/chainalysis-careers/02989a06-0562-4074-8f64-963716ae9801/application"
    assert first["date_posted"] == "2026-06-24"
    assert first["remote_status"] == "Hybrid"
    assert first["internship_type"] == "FullTime"
    assert first["extra"]["source_adapter"] == "ashby"
    assert "Chainalysis" in first["description"]


def test_ashby_unexpected_shape_raises():
    with pytest.raises(SourceSchemaError, match="jobs"):
        AshbySource().parse({"postings": []}, CompanyCfg(name="OpenAI", ats="ashby", token="openai"))


def test_smartrecruiters_fixture_to_canonical_rows():
    payload = load_fixture("smartrecruiters_boschgroup_page.json")
    company = CompanyCfg(name="Bosch", ats="smartrecruiters", token="BoschGroup")

    rows = SmartRecruitersSource().parse(payload, company)

    assert len(rows) == 5
    first = rows[0]
    assert_canonical_row(first)
    assert first["company"] == "Bosch"
    assert first["title"] == "ADAS车载通讯专家_XC"
    assert first["location"] == "Suzhou, Jiangsu, China"
    assert first["source_url"] == "https://jobs.smartrecruiters.com/BoschGroup/744000134596109-adas-xc"
    assert first["date_posted"] == "2026-06-27"
    assert first["extra"]["source_adapter"] == "smartrecruiters"
    assert first["extra"]["smartrecruiters_company"] == "Bosch Group"


def test_smartrecruiters_unexpected_shape_raises():
    with pytest.raises(SourceSchemaError, match="content"):
        SmartRecruitersSource().parse({"postings": []}, CompanyCfg(name="Bosch", ats="smartrecruiters", token="BoschGroup"))


def test_workable_fixture_to_canonical_rows():
    payload = load_fixture("workable_huggingface_jobs.json")
    company = CompanyCfg(name="Hugging Face", ats="workable", token="huggingface")

    rows = WorkableSource().parse(payload, company)

    assert len(rows) == 7
    first = rows[0]
    assert_canonical_row(first)
    assert first["company"] == "Hugging Face"
    assert first["title"] == "Senior Python Software Engineer/Open-Source Contributor - US Remote"
    assert first["location"] == "United States"
    assert first["source_url"] == "https://apply.workable.com/huggingface/j/F8427A442D/"
    assert first["date_posted"] == "2026-06-02"
    assert first["remote_status"] == "Remote"
    assert first["internship_type"] == "full"
    assert first["extra"]["source_adapter"] == "workable"
    assert first["extra"]["shortcode"] == "F8427A442D"


def test_workable_watchlist_company_with_no_openings_parses_empty_real_response():
    payload = load_fixture("workable_iceye_empty.json")
    rows = WorkableSource().parse(payload, CompanyCfg(name="ICEYE", ats="workable", token="iceye"))

    assert rows == []


def test_workable_unexpected_shape_raises():
    with pytest.raises(SourceSchemaError, match="results"):
        WorkableSource().parse({"jobs": []}, CompanyCfg(name="ICEYE", ats="workable", token="iceye"))


def test_github_listings_fixture_filters_active_company_and_terms():
    payload = load_fixture("github_listings_subset.json")
    company = CompanyCfg(name="GitHub", terms=("Summer 2026",))

    source = GitHubListingsSource(TEST_GITHUB_FEED_URL)
    rows = source.parse(payload, company)

    assert len(rows) == 1
    row = rows[0]
    assert_canonical_row(row)
    assert row["company"] == "GitHub"
    assert row["title"] == "Software Engineering Intern"
    assert row["location"] == "Remote in USA"
    assert row["source_url"] == "https://githubinc.jibeapply.com/jobs/4640"
    assert row["date_posted"] == "2025-10-30"
    assert row["internship_type"] == "Summer 2026"
    assert row["extra"]["source"] == "github"
    assert row["extra"]["source_adapter"] == "github_listings"
    assert row["extra"]["feed_url"] == TEST_GITHUB_FEED_URL
    assert source.url == TEST_GITHUB_FEED_URL


def test_github_listings_matches_aliases_and_filters_inactive_or_wrong_term():
    payload = load_fixture("github_listings_subset.json")
    source = GitHubListingsSource(TEST_GITHUB_FEED_URL)

    alias_rows = source.parse(
        payload,
        CompanyCfg(name="IFM", aliases=("Institute of Foundation Models",), terms=("Summer 2026",)),
    )
    inactive_rows = source.parse(
        payload,
        CompanyCfg(name="Thermo Fisher Scientific", terms=("Summer 2026",)),
    )
    wrong_term_rows = source.parse(
        payload,
        CompanyCfg(name="Tesla", terms=("Summer 2026",)),
    )

    assert [row["company"] for row in alias_rows] == ["Institute of Foundation Models"]
    assert inactive_rows == []
    assert wrong_term_rows == []


def test_github_schema_change_logs_and_raises(caplog):
    with pytest.raises(SourceSchemaError, match="missing keys"):
        GitHubListingsSource(TEST_GITHUB_FEED_URL).parse(
            [{"company_name": "GitHub"}],
            CompanyCfg(name="GitHub"),
        )

    assert "GitHub listings schema problem" in caplog.text


def test_github_term_matching_is_case_insensitive_whitespace_tolerant_and_exact():
    payload = load_fixture("github_listings_subset.json")
    source = GitHubListingsSource(TEST_GITHUB_FEED_URL)

    matching = source.parse(payload, CompanyCfg(name="GitHub", terms=("  summer   2026  ",)))
    substring_only = source.parse(payload, CompanyCfg(name="GitHub", terms=("Summer",)))

    assert len(matching) == 1
    assert substring_only == []


def test_github_empty_payload_is_not_a_silent_success():
    with pytest.raises(SourceSchemaError, match="contained no entries"):
        GitHubListingsSource(TEST_GITHUB_FEED_URL).parse(
            [],
            CompanyCfg(name="GitHub", terms=("Summer 2027",)),
        )


def test_github_fetch_errors_do_not_log_or_raise_query_parameters(monkeypatch):
    source = GitHubListingsSource(f"{TEST_GITHUB_FEED_URL}?temporary_token=secret")

    def fail(url, source_name):
        raise SourceFetchError(f"{source_name} fetch failed: {url}")

    monkeypatch.setattr("watcher.sources.github_listings.fetch_json", fail)

    with pytest.raises(SourceFetchError) as exc_info:
        source.fetch_payload()

    assert str(exc_info.value) == f"github_listings fetch failed: {TEST_GITHUB_FEED_URL}"
    assert "secret" not in str(exc_info.value)
