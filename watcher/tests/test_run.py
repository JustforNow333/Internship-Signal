"""Watcher pipeline behavior, end to end through `run_once`.

Collection mechanics live in `test_run_collection.py`, console output in
`test_run_reporting.py`, argument parsing in `test_run_cli.py`, the scheduled
workflow contract in `test_watcher_workflow.py`, and full synthetic digests in
`test_run_digest.py`.
"""

import sqlite3
from datetime import date, datetime, timezone

import pytest

from watcher import cli as watcher_cli
from watcher import collection as watcher_collection
from watcher import pipeline as watcher_pipeline
from watcher import reporting as watcher_reporting
from watcher import run as watcher_run
from watcher.config import (
    CompanyCfg,
    DEFAULT_WATCHLIST_PATH,
    GitHubListingSourceCfg,
    WatcherConfig,
    load_watchlist,
)
from watcher.filters import is_internship
from watcher.run import (
    RUN_MODE_DRY,
    RUN_MODE_LIVE,
    RUN_MODE_PRIME,
    run_once,
)
from watcher.seen_store import SeenStore
from watcher.source_health import (
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    SourceHealthStore,
)
from watcher.sources.base import (
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    make_row,
)
from watcher.sources.github_listings import GitHubListingsSource
from watcher.sources.workday import WorkdaySource
from watcher.tests.run_helpers import (
    CountingGithub,
    FakeDigestSender,
    FakeGithub,
    FakeSource,
    github_row,
    row,
)


def test_watcher_run_still_exports_its_public_entrypoints():
    """`watcher.run` stays the compatibility surface after the module split."""

    assert watcher_run.run_once is watcher_pipeline.run_once
    assert watcher_run.RunResult is watcher_pipeline.RunResult
    assert watcher_run.RUN_MODES is watcher_pipeline.RUN_MODES
    assert watcher_run.collect_rows is watcher_collection.collect_rows
    assert watcher_run.collect_batch is watcher_collection.collect_batch
    assert watcher_run.CollectionStats is watcher_collection.CollectionStats
    assert (
        watcher_run.WorkdayTransportSummary
        is watcher_collection.WorkdayTransportSummary
    )
    assert (
        watcher_run.summarize_workday_transport
        is watcher_collection.summarize_workday_transport
    )
    assert watcher_run.print_report is watcher_reporting.print_report
    assert watcher_run.print_heartbeat is watcher_reporting.print_heartbeat
    assert watcher_run.main is watcher_cli.main
    # Adapter tests import this seam from `watcher.run` and snapshot tests
    # patch it where it is defined; both must keep resolving to one function.
    assert (
        watcher_run._default_direct_sources
        is watcher_collection._default_direct_sources
    )


def test_run_once_filters_marks_seen_and_second_run_is_empty(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),
            CompanyCfg(name="GitHub", ats="github_only"),
        )
    )
    duplicate_url = "https://example.com/jobs/shared"
    direct_rows = [
        row("DirectCo", "Software Engineer Intern", source="direct", url=duplicate_url),
        row("DirectCo", "Marketing Intern", source="direct", description="Run campaigns."),
        row("DirectCo", "Software Engineer New Grad", source="direct", description="Build Python APIs."),
        row("DirectCo", "Software Engineer Intern Expired", source="direct", deadline="2026-01-01"),
    ]
    github_rows = [
        row("DirectCo", "Software Engineer Intern", source="github", url=duplicate_url, description=""),
        row("GitHub", "Software Engineering Intern", source="github", description=""),
    ]
    digest_sender = FakeDigestSender(sent=True)
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        first = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub(github_rows),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_LIVE,
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub(github_rows),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_LIVE,
        )

    assert [job["title"] for job in first.new_matches] == [
        "Software Engineer Intern",
        "Software Engineering Intern",
    ]
    assert first.new_matches[0]["extra"]["source"] == "direct"
    assert first.new_matches[1]["extra"]["source"] == "github"
    assert second.new_matches == []
    assert first.analysis_cache_stats.hits == 0
    assert first.analysis_cache_stats.misses == first.jobs_scored
    assert second.analysis_cache_stats.hits == second.jobs_scored
    assert second.analysis_cache_stats.misses == 0
    assert first.digest_sent is True
    assert first.seen_marked == 2
    assert [len(call) for call in digest_sender.calls] == [2, 0]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "select emailed_at from seen "
            "where emailed_at is not null order by job_id"
        ).fetchall()
    assert len(rows) == 2
    assert all(row[0] == "2026-06-09T00:00:00+00:00" for row in rows)


def test_sparse_capital_one_and_jpmorgan_github_rows_match_end_to_end(tmp_path):
    production_config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    selected = {
        company.name: company
        for company in production_config.companies
        if company.name in {"Capital One", "JPMorgan Chase"}
    }
    config = WatcherConfig(
        companies=(selected["Capital One"], selected["JPMorgan Chase"]),
        terms=production_config.terms,
        target_roles=production_config.target_roles,
    )
    payload = [
        {
            "company_name": "Capital One",
            "title": "Technology Intern",
            "locations": ["United States"],
            "url": "https://example.test/jobs/capital-one-technology-intern",
            "date_posted": "2026-08-04",
            "active": True,
            "terms": ["Summer 2027"],
        },
        {
            "company_name": "JP Morgan Chase",
            "title": "Software Engineer Intern - Software Engineer Program",
            "locations": ["United States"],
            "url": "https://example.test/jobs/jpmorgan-software-engineer-program",
            "date_posted": "2026-08-04",
            "active": True,
            "terms": ["Summer 2027"],
        },
    ]
    github_source = GitHubListingsSource(
        "https://fixtures.example.test/summer-2027/listings.json"
    )
    github_source.fetch_payload = lambda: payload

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"workday": FakeSource()},
            github_source=github_source,
            alumni_index={},
            today=date(2026, 8, 4),
            notification_mode=RUN_MODE_DRY,
        )

    jobs_by_company = {job["company"]: job for job in result.jobs}
    capital_one = jobs_by_company["Capital One"]
    assert capital_one["description"] == ""
    assert capital_one["requirements"] == ""
    assert capital_one["role_classification"]["role"] == "swe"
    assert capital_one["role_classification"]["role_track"] == "general_swe"
    assert capital_one["score"]["fit_score"] > 0
    assert {job["company"] for job in result.matches} == {
        "Capital One",
        "JP Morgan Chase",
    }


def test_unicode_dash_coop_reaches_final_watcher_matches(tmp_path):
    company = CompanyCfg(name="Example", ats="greenhouse", token="example")
    posting = row(
        "Example",
        "Software Engineering Co\u2011op",
        url="https://example.test/jobs/software-engineering-coop",
    )
    posting["location"] = "Boston, MA, United States"
    posting["internship_type"] = ""

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            WatcherConfig(companies=(company,)),
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Example": [posting]})},
            github_source=FakeGithub([]),
            alumni_index={},
            today=date(2026, 8, 4),
            notification_mode=RUN_MODE_DRY,
        )

    assert is_internship(result.jobs[0])
    assert len(result.matches) == 1
    assert result.matches[0]["id"] == result.jobs[0]["id"]


def test_explicit_internship_evidence_with_soft_full_time_wording_reaches_matches(
    tmp_path,
):
    company = CompanyCfg(name="Example", ats="greenhouse", token="example")
    examples = (
        ("Full-Time Software Engineering Intern", ""),
        ("Software Engineering Internship - Full Time", ""),
        ("Entry-Level Software Internship", ""),
        ("Software Engineer - Full Time", "Intern"),
        ("Technology Program", "Summer 2027 Internship"),
    )
    postings = []
    for index, (title, internship_type) in enumerate(examples):
        posting = row(
            "Example",
            title,
            url=f"https://example.test/jobs/precedence-{index}",
            description=(
                "Design and build software applications, APIs, and backend services "
                "with Python, Java, and React."
            ),
        )
        posting["location"] = "Boston, MA, United States"
        posting["internship_type"] = internship_type
        postings.append(posting)

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            WatcherConfig(companies=(company,)),
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Example": postings})},
            github_source=FakeGithub([]),
            alumni_index={},
            today=date(2026, 8, 4),
            notification_mode=RUN_MODE_DRY,
        )

    assert {match["title"] for match in result.matches} == {
        title for title, _ in examples
    }


def test_real_run_false_positives_do_not_reach_final_matches(tmp_path):
    companies = (
        CompanyCfg(name="DoorDash", ats="github_only"),
        CompanyCfg(name="Capital One", ats="github_only"),
        CompanyCfg(name="Example", ats="github_only"),
    )
    postings = [
        row(
            "DoorDash",
            "Software Engineer I, Entry-Level (Graduation Date: Fall 2025-Summer 2026)",
            source="github",
            description="Build production software and APIs in Python.",
        ),
        row(
            "Capital One",
            "Intern, Strategy Analyst - Summer 2027",
            source="github",
            description=(
                "Use Python, SQL, analytics, APIs, and software tools to support "
                "business strategy and strategic planning."
            ),
            requirements="Python, SQL, APIs, and analytics.",
        ),
        row(
            "Example",
            "Software Engineer Intern - Summer 2027",
            source="github",
        ),
    ]

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            WatcherConfig(companies=companies),
            seen_store=store,
            direct_sources={},
            github_source=FakeGithub(postings),
            alumni_index={},
            today=date(2026, 8, 5),
            notification_mode=RUN_MODE_DRY,
        )

    jobs = {job["title"]: job for job in result.jobs}
    strategy = jobs["Intern, Strategy Analyst - Summer 2027"]
    assert strategy["role_classification"]["role_track"] == "non_technical"
    assert strategy["score"]["fit_score"] == 0
    assert [match["title"] for match in result.matches] == [
        "Software Engineer Intern - Summer 2027"
    ]


def test_sparse_exact_data_and_ai_title_reaches_final_watcher_matches(tmp_path):
    company = CompanyCfg(name="Example", ats="github_only")
    posting = make_row(
        source="github",
        source_adapter="github_listings",
        company="Example",
        title="Data & AI Intern - Analyst",
        location="United States",
        source_url="https://example.test/jobs/data-ai-intern-analyst",
        internship_type="",
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            WatcherConfig(companies=(company,)),
            seen_store=store,
            direct_sources={},
            github_source=FakeGithub([posting]),
            alumni_index={},
            today=date(2026, 8, 4),
            notification_mode=RUN_MODE_DRY,
        )

    analyzed = result.jobs[0]
    assert is_internship(analyzed)
    assert analyzed["description"] == ""
    assert analyzed["requirements"] == ""
    assert analyzed["role_classification"]["role"] == "ml_ai"
    assert analyzed["role_classification"]["role_track"] == "ml_ai"
    assert analyzed["score"]["fit_score"] > 0
    assert len(result.matches) == 1
    assert result.matches[0]["id"] == analyzed["id"]


def test_capital_one_workday_title_wins_simplify_merge_and_reaches_matches(
    tmp_path,
):
    company = CompanyCfg(
        name="Capital One",
        ats="workday",
        token="capitalone",
        workday_shard="wd12",
        workday_site="Capital_One",
        aliases=("Capital One Financial",),
        terms=("Summer 2027",),
    )
    config = WatcherConfig(
        companies=(company,),
        terms=("Summer 2027",),
    )
    application_url = (
        "https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/"
        "McLean-VA/Technology-Internship-Program---Summer-2027_R244387-1"
    )
    direct_rows = WorkdaySource().parse(
        {
            "total": 1,
            "jobPostings": [
                {
                    "title": "Technology Internship Program - Summer 2027",
                    "externalPath": (
                        "/job/McLean-VA/Technology-Internship-Program---"
                        "Summer-2027_R244387-1"
                    ),
                    "timeType": "Full time",
                    "locationsText": "McLean, VA, United States",
                    "postedOn": "Posted Today",
                    "bulletFields": ["R244387"],
                }
            ],
        },
        company,
    )
    simplify_rows = GitHubListingsSource(
        "https://fixtures.example.test/summer-2027/listings.json"
    ).parse(
        [
            {
                "company_name": "Capital One",
                "title": "Technology Intern",
                "locations": ["McLean, VA, United States"],
                "url": application_url,
                "date_posted": "2026-08-04",
                "active": True,
                "terms": ["Summer 2027"],
            }
        ],
        company,
    )

    assert direct_rows[0]["extra"]["source_requisition_id"] == "R244387"
    assert direct_rows[0]["source_url"] == application_url
    assert simplify_rows[0]["source_url"] == application_url

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"workday": FakeSource({"Capital One": direct_rows})},
            github_source=FakeGithub(simplify_rows),
            alumni_index={},
            today=date(2026, 8, 4),
            notification_mode=RUN_MODE_DRY,
        )

    assert result.rows_fetched == 2
    assert result.jobs_scored == 1
    assert result.cross_source_duplicates_merged == 1
    assert len(result.duplicate_report) == 1
    assert result.duplicate_report[0]["matched_on"] == "requisition_id"
    assert len(result.jobs) == 1
    merged = result.jobs[0]
    assert merged["extra"]["primary_source"] == "direct_ats"
    assert merged["extra"]["sources"] == ["direct_ats", "simplify"]
    assert merged["title"] == "Technology Internship Program - Summer 2027"
    assert merged["role_classification"]["role"] == "swe"
    assert merged["role_classification"]["role_track"] == "general_swe"
    assert merged["score"]["fit_score"] > 0
    assert len(result.matches) == 1
    assert result.matches[0]["id"] == merged["id"]
    assert result.matches[0]["title"] == merged["title"]


def _enriching_workday_source(search_postings, details_by_requisition):
    def detail(url, source_name):
        requisition = next(
            requisition
            for requisition in details_by_requisition
            if f"_{requisition}" in url
        )
        detail_value = details_by_requisition[requisition]
        if isinstance(detail_value, Exception):
            raise detail_value
        return detail_value

    return WorkdaySource(
        min_interval_seconds=0,
        request_json=lambda url, payload, source_name: {
            "jobPostings": search_postings,
            "total": len(search_postings),
        },
        request_detail_json=detail,
        sleeper=lambda delay: None,
    )


def _workday_search_posting(title, requisition):
    return {
        "title": title,
        "externalPath": f"/job/New-York/{title.replace(' ', '-')}_{requisition}-1",
        "locationsText": "New York, NY, United States",
        "postedOn": "Posted Today",
        "bulletFields": [requisition],
    }


def _workday_detail(requisition, description, *, worker_subtype="Intern"):
    return {
        "jobPostingInfo": {
            "jobReqId": requisition,
            "jobDescription": f"<p>{description}</p>",
            "location": "New York, NY, United States",
            "startDate": "2026-08-05",
            "timeType": "Full time",
            "workerSubType": worker_subtype,
            "canApply": True,
        }
    }


def test_workday_detail_enrichment_changes_only_technical_candidates_to_matches(
    tmp_path,
):
    company = CompanyCfg(
        name="Example",
        ats="workday",
        token="tenant",
        workday_shard="wd5",
        workday_site="Early_Careers",
        workday_detail_policy="early_career_candidates",
    )
    postings = [
        _workday_search_posting("Technology Intern", "R1"),
        _workday_search_posting("Technology Programme", "R2"),
        _workday_search_posting("Business Operations Intern", "R3"),
    ]
    source = _enriching_workday_source(
        postings,
        {
            "R1": _workday_detail(
                "R1",
                "Design and build Python services, APIs, and production software.",
            ),
            "R2": _workday_detail(
                "R2",
                "Develop Java applications, backend APIs, and cloud software systems.",
            ),
            "R3": _workday_detail(
                "R3",
                "Support business operations, financial reporting, and client scheduling.",
            ),
        },
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            WatcherConfig(companies=(company,)),
            seen_store=store,
            direct_sources={"workday": source},
            github_source=FakeGithub([]),
            alumni_index={},
            today=date(2026, 8, 5),
            notification_mode=RUN_MODE_DRY,
        )

    jobs = {job["title"]: job for job in result.jobs}
    assert jobs["Technology Intern"]["role_classification"]["role"] == "swe"
    assert jobs["Technology Programme"]["role_classification"]["role"] == "swe"
    assert (
        jobs["Business Operations Intern"]["role_classification"]["role"]
        == "non_technical"
    )
    assert {job["title"] for job in result.matches} == {
        "Technology Intern",
        "Technology Programme",
    }


def test_workday_detail_failure_preserves_sparse_posting_previous_behavior(tmp_path):
    company = CompanyCfg(
        name="Example",
        ats="workday",
        token="tenant",
        workday_shard="wd5",
        workday_site="Site",
    )
    source = _enriching_workday_source(
        [_workday_search_posting("Technology Intern", "R1")],
        {
            "R1": SourceFetchError(
                "permanent detail failure",
                error_code="permanent_http_error",
                status_code=400,
            )
        },
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            WatcherConfig(companies=(company,)),
            seen_store=store,
            direct_sources={"workday": source},
            github_source=FakeGithub([]),
            alumni_index={},
            today=date(2026, 8, 5),
            notification_mode=RUN_MODE_DRY,
        )

    assert len(result.jobs) == 1
    assert result.jobs[0]["title"] == "Technology Intern"
    assert result.jobs[0]["description"] == ""
    assert result.jobs[0]["role_classification"]["role"] == "unknown"
    assert result.matches == []


def test_enriched_workday_row_merges_with_github_and_remains_primary(tmp_path):
    company = CompanyCfg(
        name="Example",
        ats="workday",
        token="tenant",
        workday_shard="wd5",
        workday_site="Site",
    )
    posting = _workday_search_posting("Technology Intern", "R1")
    source = _enriching_workday_source(
        [posting],
        {
            "R1": _workday_detail(
                "R1",
                "Design and build Python services, APIs, and production software.",
            )
        },
    )
    application_url = WorkdaySource.posting_url(
        "tenant",
        "wd5",
        "Site",
        posting["externalPath"],
    )
    github = github_row(
        "Example",
        "Technology Intern",
        source_name="simplify",
        source_format="simplify_json",
        priority=10,
        url=application_url,
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            WatcherConfig(companies=(company,)),
            seen_store=store,
            direct_sources={"workday": source},
            github_source=FakeGithub([github]),
            alumni_index={},
            today=date(2026, 8, 5),
            notification_mode=RUN_MODE_DRY,
        )

    assert result.rows_fetched == 2
    assert result.jobs_scored == 1
    assert result.cross_source_duplicates_merged == 1
    assert result.jobs[0]["extra"]["primary_source"] == "direct_ats"
    assert result.jobs[0]["extra"]["sources"] == ["direct_ats", "simplify"]
    assert result.jobs[0]["description"].startswith("Design and build")
    assert len(result.matches) == 1
    assert result.matches[0]["id"] == result.jobs[0]["id"]


def test_categorical_exclusion_is_audited_but_never_emailed_or_marked_seen(
    tmp_path,
):
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="Northrop Grumman", ats="greenhouse", token="northrop"),
        )
    )
    direct_rows = [
        row(
            "Northrop Grumman",
            "2027 Returning Intern Software Engineer",
            url="https://example.test/jobs/returning-101",
        ),
        row(
            "Northrop Grumman",
            "2027 Software Engineer Intern",
            url="https://example.test/jobs/open-202",
        ),
    ]
    digest_sender = FakeDigestSender(sent=True)
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        dry = run_once(
            config,
            seen_store=store,
            direct_sources={
                "greenhouse": FakeSource({"Northrop Grumman": direct_rows})
            },
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=digest_sender,
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_DRY,
        )
        with sqlite3.connect(db_path) as conn:
            assert conn.execute(
                "select count(*) from seen "
                "where emailed_at is not null or primed_at is not null"
            ).fetchone()[0] == 0

        live = run_once(
            config,
            seen_store=store,
            direct_sources={
                "greenhouse": FakeSource({"Northrop Grumman": direct_rows})
            },
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=digest_sender,
            today=date(2026, 7, 28),
            seen_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_LIVE,
        )

    for result in (dry, live):
        assert [job["title"] for job in result.new_matches] == [
            "2027 Software Engineer Intern"
        ]
        assert len(result.eligibility_exclusions) == 1
        exclusion = result.eligibility_exclusions[0]
        assert exclusion["title"] == "2027 Returning Intern Software Engineer"
        assert exclusion["exclusion_reason"] == "returning_intern_only"
        assert exclusion["evidence_source"] == "title"
        assert exclusion["role"] == "swe"
        assert exclusion["role_track"] == "general_swe"

    assert [len(call) for call in digest_sender.calls] == [1]
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            "select title, url from seen where emailed_at is not null"
        ).fetchall()
    assert stored == [
        ("2027 Software Engineer Intern", "https://example.test/jobs/open-202")
    ]


def test_run_once_does_not_mark_seen_when_digest_not_sent(tmp_path):
    config = WatcherConfig(companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),))
    direct_rows = [row("DirectCo", "Software Engineer Intern")]
    digest_sender = FakeDigestSender(sent=False)

    with SeenStore(tmp_path / "seen.sqlite") as store:
        first = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            notification_mode=RUN_MODE_LIVE,
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            notification_mode=RUN_MODE_LIVE,
        )

    assert [job["title"] for job in first.new_matches] == ["Software Engineer Intern"]
    assert [job["title"] for job in second.new_matches] == ["Software Engineer Intern"]
    assert first.digest_sent is False
    assert first.seen_marked == 0
    assert [len(call) for call in digest_sender.calls] == [1, 1]
    with sqlite3.connect(tmp_path / "seen.sqlite") as conn:
        assert conn.execute(
            "select count(*) from seen "
            "where emailed_at is not null or primed_at is not null"
        ).fetchone()[0] == 0


def test_run_once_failed_live_send_exception_does_not_mark_emailed(tmp_path):
    config = WatcherConfig(
        companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),)
    )
    db_path = tmp_path / "seen.sqlite"

    def failed_sender(_matches):
        raise RuntimeError("simulated SMTP failure")

    with SeenStore(db_path) as store:
        with pytest.raises(RuntimeError, match="simulated SMTP failure"):
            run_once(
                config,
                seen_store=store,
                direct_sources={
                    "greenhouse": FakeSource(
                        {"DirectCo": [row("DirectCo", "Software Engineer Intern")]}
                    )
                },
                github_source=FakeGithub([]),
                alumni_index={},
                digest_sender=failed_sender,
                today=date(2026, 6, 9),
                notification_mode=RUN_MODE_LIVE,
            )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "select count(*) from seen "
            "where emailed_at is not null or primed_at is not null"
        ).fetchone()[0] == 0


def test_run_once_ordinary_dry_run_does_not_alter_notification_state(tmp_path):
    config = WatcherConfig(companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),))
    direct_rows = [row("DirectCo", "Software Engineer Intern")]
    digest_sender = FakeDigestSender(sent=True)
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            notification_mode=RUN_MODE_DRY,
        )

    assert len(result.new_matches) == 1
    assert result.dry_run_pending == 1
    assert result.digest_sent is False
    assert result.seen_marked == 0
    assert digest_sender.calls == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "select count(*) from seen "
            "where emailed_at is not null or primed_at is not null"
        ).fetchone()[0] == 0


def test_run_once_can_prime_seen_store_without_sending(tmp_path):
    config = WatcherConfig(companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),))
    direct_rows = [row("DirectCo", "Software Engineer Intern")]
    digest_sender = FakeDigestSender(sent=False)
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        first = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_PRIME,
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            notification_mode=RUN_MODE_PRIME,
        )

    assert [job["title"] for job in first.new_matches] == ["Software Engineer Intern"]
    assert second.new_matches == []
    assert first.digest_sent is False
    assert first.seen_marked == 1
    assert digest_sender.calls == []
    with sqlite3.connect(db_path) as conn:
        seen_row = conn.execute(
            "select first_seen, emailed_at, primed_at from seen"
        ).fetchone()
    # `first_seen` now records the first observation, which the shadow pass
    # writes before priming. The notification timestamps are what matter.
    assert seen_row[0]
    assert seen_row[1] is None
    assert seen_row[2] == "2026-06-09T00:00:00+00:00"


def test_six_distinct_requisitions_at_one_company_produce_six_matches(tmp_path):
    config = WatcherConfig(
        companies=(CompanyCfg(name="Google", ats="greenhouse", token="google"),)
    )
    google_rows = []
    for index in range(1, 7):
        posting = row(
            "Google",
            "Software Engineering Intern",
            url="https://careers.google.com/internships",
        )
        posting["extra"].update(
            {
                "source_adapter": "greenhouse",
                "source_system": "greenhouse",
                "source_requisition_id": f"GOOG-{index}",
            }
        )
        google_rows.append(posting)

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Google": google_rows})},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_DRY,
        )

    assert len(result.matches) == 6
    assert len(result.new_matches) == 6
    assert result.dry_run_pending == 6


def test_same_requisition_from_direct_and_github_produces_one_match(tmp_path):
    config = WatcherConfig(
        companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),)
    )
    direct = row(
        "DirectCo",
        "Software Engineering Intern",
        source="direct",
        url="https://careers.example.test/internships",
    )
    direct["extra"].update(
        {
            "source_adapter": "greenhouse",
            "source_system": "greenhouse",
            "source_requisition_id": "REQ-42",
        }
    )
    github = row(
        "DirectCo",
        "SWE Intern display wording",
        source="github",
        url="https://careers.example.test/internships",
    )
    github["extra"].update(
        {
            "source_adapter": "github_listings",
            "source_system": "greenhouse",
            "source_requisition_id": "REQ-42",
        }
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": [direct]})},
            github_source=FakeGithub([github]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_DRY,
        )

    assert len(result.matches) == 1
    assert len(result.new_matches) == 1
    assert result.cross_source_duplicates_merged == 1
    assert result.matches[0]["extra"]["source"] == "direct"


def test_notification_identity_integration_six_requisitions_and_cross_source_duplicate(
    tmp_path,
):
    config = WatcherConfig(
        companies=(CompanyCfg(name="Google", ats="greenhouse", token="google"),)
    )
    db_path = tmp_path / "notification-integration.sqlite"

    def google_posting(requisition_id):
        posting = row(
            "Google",
            "Software Engineering Intern",
            url="https://careers.google.com/internships",
        )
        posting["extra"].update(
            {
                "source_adapter": "greenhouse",
                "source_system": "greenhouse",
                "source_requisition_id": requisition_id,
            }
        )
        return posting

    first_six = [google_posting(f"GOOG-{index}") for index in range(1, 7)]
    github_duplicate = google_posting("GOOG-1")
    github_duplicate["title"] = "SWE Intern - GitHub display wording"
    github_duplicate["location"] = "Remote"
    github_duplicate["extra"].update(
        {
            "source": "github",
            "source_adapter": "github_listings",
        }
    )

    with SeenStore(db_path) as store:
        dry = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Google": first_six})},
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_DRY,
        )
        seen_after_dry = store._conn.execute(
            "select count(*) from seen "
            "where emailed_at is not null or primed_at is not null"
        ).fetchone()[0]

        live_sender = FakeDigestSender(sent=True)
        live = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Google": first_six})},
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=live_sender,
            today=date(2026, 7, 28),
            seen_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_LIVE,
        )
        emailed_after_live = store._conn.execute(
            "select count(*) from seen where emailed_at is not null"
        ).fetchone()[0]

        rerun = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Google": first_six})},
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=True),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_LIVE,
        )

        seventh = google_posting("GOOG-7")
        primed = run_once(
            config,
            seen_store=store,
            direct_sources={
                "greenhouse": FakeSource({"Google": [*first_six, seventh]})
            },
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=True),
            today=date(2026, 7, 28),
            seen_at=datetime(2026, 7, 28, 1, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_PRIME,
        )
        primed_rows = store._conn.execute(
            "select count(*) from seen where primed_at is not null"
        ).fetchone()[0]

        after_prime = run_once(
            config,
            seen_store=store,
            direct_sources={
                "greenhouse": FakeSource({"Google": [*first_six, seventh]})
            },
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=True),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_LIVE,
        )

    assert len(dry.matches) == 6
    assert len(dry.new_matches) == 6
    assert dry.cross_source_duplicates_merged == 1
    assert seen_after_dry == 0
    assert len(live.new_matches) == 6
    assert [len(batch) for batch in live_sender.calls] == [6]
    assert emailed_after_live == 6
    assert rerun.new_matches == []
    assert len(primed.new_matches) == 1
    assert primed_rows == 1
    assert after_prime.new_matches == []
    assert len(after_prime.previously_emailed) == 6
    assert len(after_prime.explicitly_primed) == 1

    print(
        "INTEGRATION "
        f"eligible={len(dry.matches)} "
        f"dry_new={len(dry.new_matches)} "
        f"seen_after_dry={seen_after_dry} "
        f"live_emailed={emailed_after_live} "
        f"rerun_new={len(rerun.new_matches)} "
        f"primed_seventh={primed_rows} "
        f"after_prime_new={len(after_prime.new_matches)} "
        f"cross_source_merged={dry.cross_source_duplicates_merged}"
    )


def test_run_once_passes_watchlist_aliases_to_alumni_join(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="AliasCo Software",
                ats="greenhouse",
                token="aliasco",
                alumni_match=("ShortCo",),
            ),
        )
    )
    direct_rows = [row("AliasCo Software", "Software Engineer Intern")]
    alumni_index = {
        "shortco": [{
            "name": "Ada Alias",
            "occupation": "Software Engineer",
            "linkedin_url": "https://www.linkedin.com/in/fake-ada-alias",
            "employer": "ShortCo",
        }]
    }

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"AliasCo Software": direct_rows})},
            github_source=FakeGithub([]),
            alumni_index=alumni_index,
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 6, 9),
        )

    assert [record["name"] for record in result.matches[0]["alumni"]] == ["Ada Alias"]


def test_run_once_logs_every_pipeline_stage_without_changing_results(tmp_path, caplog):
    caplog.set_level("INFO", logger="watcher.run")
    config = WatcherConfig(
        companies=(CompanyCfg(name="StageCo", ats="greenhouse", token="stage"),)
    )
    expected = row("StageCo", "Software Engineer Intern")

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"StageCo": [expected]})},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 30),
            run_id="timing-stage-test",
        )

    assert result.rows_fetched == 1
    assert result.jobs_scored == 1
    assert [match["company"] for match in result.matches] == ["StageCo"]
    stage_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("STAGE-TIMING ")
    ]
    expected_stages = {
        "direct_source_collection",
        "github_backstop_collection",
        "collection",
        "health_state_persistence",
        "analysis",
        "filtering_eligibility",
        "alumni_loading_matching",
        "shadow_generation_observation",
        "seen_store_partitioning",
        "digest_email_handling",
        "source_comparison_generation_persistence",
        "health_alert_evaluation",
    }
    assert {
        line.split("stage=", 1)[1].split(" ", 1)[0]
        for line in stage_lines
    } == expected_stages
    assert all(
        len(line.rsplit("seconds=", 1)[1].split(".")[1]) == 3
        for line in stage_lines
    )


def test_run_merges_direct_simplify_and_markdown_by_fixed_priority(tmp_path):
    shared_url = "https://example.test/jobs/shared"
    simplify_config = GitHubListingSourceCfg(
        name="simplify",
        format="simplify_json",
        url="https://example.test/simplify.json",
    )
    markdown_config = GitHubListingSourceCfg(
        name="sndsh404_summer_2027",
        format="github_markdown_table",
        url="https://example.test/README.md",
        default_term="Summer 2027",
    )
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="DirectCo",
                ats="greenhouse",
                token="direct",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(markdown_config, simplify_config),
    )
    direct = row(
        "DirectCo",
        "Software Engineer Intern",
        source="direct",
        url=shared_url,
        description="",
    )
    simplify = github_row(
        "Simplify Company Name",
        "SWE Intern",
        source_name="simplify",
        source_format="simplify_json",
        priority=10,
        url=f"{shared_url}?utm_source=simplify",
        active=True,
        description="Structured feed description",
    )
    markdown = github_row(
        "Markdown Company Name",
        "Engineering Intern",
        source_name="sndsh404_summer_2027",
        source_format="github_markdown_table",
        priority=20,
        url=f"{shared_url}?ref=readme",
        active=False,
    )
    markdown["extra"]["source_added_date"] = "2026-07-20"
    sources = [
        CountingGithub(markdown_config.url, [markdown]),
        CountingGithub(simplify_config.url, [simplify]),
    ]

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": [direct]})},
            github_source=sources,
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 24),
        )

    assert result.rows_fetched == 3
    assert result.jobs_scored == 1
    assert len(result.matches) == 1
    merged = result.matches[0]
    assert merged["company"] == "DirectCo"
    assert merged["title"] == "Software Engineer Intern"
    assert merged["description"] == "Structured feed description"
    assert merged["extra"]["primary_source"] == "direct_ats"
    assert merged["extra"]["sources"] == [
        "direct_ats",
        "simplify",
        "sndsh404_summer_2027",
    ]
    assert merged["extra"]["active"] is True
    assert merged["extra"]["closed"] is False
    assert (
        merged["extra"]["source_details"]["sndsh404_summer_2027"]["active"]
        is False
    )


def test_typed_source_order_does_not_change_deterministic_results(tmp_path):
    simplify_config = GitHubListingSourceCfg(
        name="simplify",
        format="simplify_json",
        url="https://example.test/simplify.json",
    )
    markdown_config = GitHubListingSourceCfg(
        name="sndsh404_summer_2027",
        format="github_markdown_table",
        url="https://example.test/README.md",
        default_term="Summer 2027",
    )
    shared_url = "https://example.test/jobs/shared"
    simplify = github_row(
        "GitHub",
        "Software Engineering Intern",
        source_name="simplify",
        source_format="simplify_json",
        priority=10,
        url=shared_url,
        active=True,
    )
    markdown = github_row(
        "Different Markdown Name",
        "SWE Intern",
        source_name="sndsh404_summer_2027",
        source_format="github_markdown_table",
        priority=20,
        url=shared_url,
        active=True,
    )

    outputs = []
    for index, configured in enumerate(
        (
            (simplify_config, markdown_config),
            (markdown_config, simplify_config),
        )
    ):
        config = WatcherConfig(
            companies=(CompanyCfg(name="GitHub", ats="github_only", terms=("Summer 2027",)),),
            terms=("Summer 2027",),
            github_listing_sources=configured,
        )
        injected = [
            CountingGithub(markdown_config.url, [markdown]),
            CountingGithub(simplify_config.url, [simplify]),
        ]
        with SeenStore(tmp_path / f"seen-{index}.sqlite") as store:
            result = run_once(
                config,
                seen_store=store,
                direct_sources={},
                github_source=injected,
                alumni_index={},
                digest_sender=FakeDigestSender(sent=False),
                today=date(2026, 7, 24),
            )
        outputs.append(
            [
                {
                    "id": job["id"],
                    "company": job["company"],
                    "title": job["title"],
                    "extra": job["extra"],
                }
                for job in result.matches
            ]
        )

    assert outputs[0] == outputs[1]
    assert outputs[0][0]["company"] == "GitHub"
    assert outputs[0][0]["extra"]["primary_source"] == "simplify"


def test_markdown_failure_is_independent_in_health_and_keeps_other_results(tmp_path):
    simplify_config = GitHubListingSourceCfg(
        name="simplify",
        format="simplify_json",
        url="https://example.test/simplify.json",
    )
    markdown_config = GitHubListingSourceCfg(
        name="sndsh404_summer_2027",
        format="github_markdown_table",
        url="https://example.test/README.md",
        default_term="Summer 2027",
    )
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="DirectCo",
                ats="greenhouse",
                token="direct",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(markdown_config, simplify_config),
    )
    direct = row("DirectCo", "Software Engineer Intern", source="direct")
    sources = [
        CountingGithub(markdown_config.url, error=SourceSchemaError("missing expected table")),
        CountingGithub(simplify_config.url, rows=[]),
    ]

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": [direct]})},
            github_source=sources,
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 24),
            run_id="typed-health-run",
        )

    github_attempts = [
        attempt
        for attempt in result.source_attempts
        if attempt.source_kind == SOURCE_KIND_GITHUB_FEED
    ]
    assert len(result.matches) == 1
    assert result.github_feeds_configured == 2
    assert result.github_feeds_succeeded == 1
    assert result.health_summary.github_feeds_healthy == 1
    assert result.health_summary.github_feeds_degraded == 1
    assert [(attempt.adapter, attempt.succeeded) for attempt in github_attempts] == [
        ("simplify_json", True),
        ("github_markdown_table", False),
    ]
    assert "simplify" in github_attempts[0].feed_label
    assert "sndsh404_summer_2027" in github_attempts[1].feed_label
    assert len(result.errors) == 1


def test_closed_markdown_only_row_is_scored_but_not_notified(tmp_path):
    markdown_config = GitHubListingSourceCfg(
        name="sndsh404_summer_2027",
        format="github_markdown_table",
        url="https://example.test/README.md",
        default_term="Summer 2027",
    )
    config = WatcherConfig(
        companies=(CompanyCfg(name="GitHub", ats="github_only", terms=("Summer 2027",)),),
        terms=("Summer 2027",),
        github_listing_sources=(markdown_config,),
    )
    closed = github_row(
        "GitHub",
        "Software Engineering Intern",
        source_name="sndsh404_summer_2027",
        source_format="github_markdown_table",
        priority=20,
        url="https://example.test/jobs/closed",
        active=False,
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={},
            github_source=CountingGithub(markdown_config.url, [closed]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 24),
        )

    assert result.jobs_scored == 1
    assert result.matches == []
    assert result.new_matches == []
    assert result.health_summary.github_feeds_healthy == 1


def test_run_result_exposes_season_feed_counts_and_stale_company_warning(tmp_path, caplog):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Stale Override Co",
                ats="github_only",
                terms=("Summer 2026",),
            ),
        ),
        terms=("Summer 2027",),
        github_listing_urls=("https://example.test/listings.json",),
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2027, 7, 15),
        )

    assert result.configured_terms == ("Summer 2027",)
    assert result.season_status == "rollover_due"
    assert result.github_feeds_configured == 1
    assert result.github_feeds_succeeded == 1
    assert result.company_season_warnings == (
        "Stale Override Co: stale company terms override (Summer 2026)",
    )
    assert "SEASON WARNING: rollover_due" in caplog.text
    assert "Stale Override Co: stale company terms override" in caplog.text


def test_run_once_persists_health_without_matches_email_or_seen_marking(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="EmptyCo", ats="greenhouse", token="empty"),
            CompanyCfg(name="BackstopCo", ats="github_only"),
        ),
        github_listing_urls=("https://example.test/listings.json",),
    )
    digest_sender = FakeDigestSender(sent=False)
    observed = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)

    with SeenStore(db_path) as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"EmptyCo": []})},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=digest_sender,
            today=date(2026, 7, 16),
            run_id="fixed-run",
            health_observed_at=observed,
        )

    assert result.run_id == "fixed-run"
    assert result.health_summary.direct_empty == 1
    assert result.health_summary.direct_unsupported == 1
    assert result.health_summary.github_feeds_healthy == 1
    assert result.health_summary.backstop_only_companies == 1
    assert result.matches == []
    assert result.seen_marked == 0
    assert digest_sender.calls == [[]]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "select count(*) from seen "
            "where emailed_at is not null or primed_at is not null"
        ).fetchone()[0] == 0
        assert conn.execute(
            "select count(*) from source_health_attempts where run_id = ?", ("fixed-run",)
        ).fetchone()[0] == 3


def test_run_once_reuses_injected_health_store_and_detects_recovery(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    config = WatcherConfig(companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="direct"),))
    observed = datetime(2026, 7, 16, tzinfo=timezone.utc)

    with SeenStore(db_path) as seen_store, SourceHealthStore(db_path) as health_store:
        failed = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"greenhouse": FakeSource(error=SourceError("boom"))},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="run-failed",
            health_observed_at=observed,
        )
        recovered = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": [row("DirectCo", "Intern")]})},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="run-recovered",
            health_observed_at=observed.replace(hour=1),
        )

    assert failed.health_summary.direct_failed == 1
    assert recovered.health_summary.direct_healthy == 1
    assert recovered.health_summary.health_recoveries == 1
    assert recovered.health_transitions[0].recovery is True
    assert (
        recovered.source_health_states[recovered.source_attempts[0].health_key].status
        == "healthy_with_listings"
    )


def test_partial_workday_fetch_is_successful_but_reports_degradation(tmp_path, monkeypatch):
    db_path = tmp_path / "seen.sqlite"
    company = CompanyCfg(
        name="Merck",
        ats="workday",
        token="merck",
        workday_shard="wd5",
        workday_site="Search_Jobs",
    )
    config = WatcherConfig(companies=(company,))
    observed = datetime(2026, 7, 16, tzinfo=timezone.utc)
    payload = {
        "jobPostings": [
            {"title": "Malformed Workday Posting", "externalPath": ""},
            {
                "title": "Software Engineer Intern",
                "externalPath": "/job/Rahway-NJ/Software-Engineer-Intern_R123",
                "locationsText": "Rahway, NJ",
                "jobDescription": "Build Python backend APIs and SQL services.",
                "bulletFields": ["R123"],
            },
        ],
        "total": 2,
    }
    monkeypatch.setattr("watcher.sources.workday.post_json", lambda url, data, source_name: payload)

    with SeenStore(db_path) as seen_store, SourceHealthStore(db_path) as health_store:
        failed = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"workday": FakeSource(error=SourceSchemaError("malformed posting"))},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="merck-failed",
            health_observed_at=observed,
        )
        recovered = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"workday": WorkdaySource()},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="merck-recovered",
            health_observed_at=observed.replace(hour=1),
        )

    direct_attempt = next(
        item for item in recovered.source_attempts if item.source_kind == SOURCE_KIND_DIRECT
    )
    assert failed.health_summary.direct_failed == 1
    assert recovered.errors == []
    assert direct_attempt.succeeded is True
    assert direct_attempt.rows_returned == 1
    assert recovered.health_summary.direct_degraded == 1
    assert recovered.health_summary.health_recoveries == 0
    assert recovered.health_transitions[0].company == "Merck"
    assert recovered.health_transitions[0].recovery is False
