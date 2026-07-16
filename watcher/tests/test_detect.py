from watcher.detect import (
    BESPOKE,
    RESOLVED,
    UNRESOLVED,
    DetectionResult,
    ats_match_from_url,
    first_ats_match,
    first_bespoke_portal,
    first_unsupported_ats,
    write_report,
    write_watchlist,
)


def test_greenhouse_token_from_job_board_url():
    match = ats_match_from_url("https://job-boards.greenhouse.io/andurilindustries/jobs/123")

    assert match is not None
    assert match.ats == "greenhouse"
    assert match.token == "andurilindustries"


def test_greenhouse_token_from_embed_query():
    html = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=openai"></iframe>'

    match = first_ats_match(html, "https://openai.com/careers")

    assert match is not None
    assert match.ats == "greenhouse"
    assert match.token == "openai"


def test_greenhouse_regional_job_board_url():
    match = ats_match_from_url("https://job-boards.eu.greenhouse.io/teads1/jobs/4816378101")

    assert match is not None
    assert match.ats == "greenhouse"
    assert match.token == "teads1"


def test_lever_ashby_smartrecruiters_and_workable_urls():
    cases = [
        ("https://jobs.lever.co/ifm-us", "lever", "ifm-us"),
        ("https://jobs.ashbyhq.com/canarytechnologies", "ashby", "canarytechnologies"),
        ("https://jobs.smartrecruiters.com/BoschGroup/123", "smartrecruiters", "BoschGroup"),
        ("https://apply.workable.com/iceye/", "workable", "iceye"),
    ]

    for url, ats, token in cases:
        match = ats_match_from_url(url)
        assert match is not None
        assert match.ats == ats
        assert match.token == token


def test_workday_public_site_extracts_tenant_and_site():
    match = ats_match_from_url("https://capitalone.wd1.myworkdayjobs.com/Capital_One")

    assert match is not None
    assert match.ats == "workday"
    assert match.token == "capitalone"
    assert match.workday_shard == "wd1"
    assert match.workday_site == "Capital_One"


def test_workday_cxs_url_extracts_tenant_and_site():
    match = ats_match_from_url("https://workday.wd5.myworkdayjobs.com/wday/cxs/workday/Workday/jobs")

    assert match is not None
    assert match.ats == "workday"
    assert match.token == "workday"
    assert match.workday_shard == "wd5"
    assert match.workday_site == "Workday"


def test_workday_site_can_be_jobs():
    match = ats_match_from_url("https://paypal.wd1.myworkdayjobs.com/en-US/jobs/job/San-Jose/job-id")

    assert match is not None
    assert match.ats == "workday"
    assert match.token == "paypal"
    assert match.workday_shard == "wd1"
    assert match.workday_site == "jobs"


def test_workday_without_shard_does_not_resolve():
    assert ats_match_from_url("https://capitalone.myworkdayjobs.com/Capital_One") is None


def test_bespoke_portal_detection_from_known_url():
    match = first_bespoke_portal(
        '<a href="https://www.google.com/about/careers/applications/jobs/results/">Jobs</a>',
        "https://www.google.com/about/careers/",
        "Google",
    )

    assert match is not None
    assert match.source_url == "https://www.google.com/about/careers/"


def test_unsupported_ats_detection_does_not_resolve():
    match = first_unsupported_ats('<a href="https://company.icims.com/jobs/123">Apply</a>')

    assert match == ("icims", "https://company.icims.com/jobs/123")


def test_watchlist_writer_keeps_unresolved_as_github_only(tmp_path):
    path = tmp_path / "watchlist.yml"
    write_watchlist(
        [
            DetectionResult(company="Anduril Industries", status=RESOLVED, ats="greenhouse", token="andurilindustries"),
            DetectionResult(company="Google", status=BESPOKE, ats="bespoke", source_url="https://google.com/about/careers"),
            DetectionResult(company="Unknown Co", status=UNRESOLVED, reason="manual check needed"),
        ],
        path,
        terms=("Summer 2027",),
        github_listing_urls=("https://example.test/listings.json",),
    )

    output = path.read_text(encoding="utf-8")
    assert 'name: "Anduril Industries"' in output
    assert "ats: greenhouse" in output
    assert 'token: "andurilindustries"' in output
    assert "ats: bespoke" in output
    assert "ats: github_only" in output
    assert 'terms: ["Summer 2027"]' in output
    assert 'github_listing_urls: ["https://example.test/listings.json"]' in output


def test_report_writer_includes_workday_shard(tmp_path):
    path = tmp_path / "report.md"
    write_report(
        [
            DetectionResult(
                company="Capital One",
                status=RESOLVED,
                ats="workday",
                token="capitalone",
                workday_shard="wd12",
                workday_site="Capital_One",
                source_url="https://capitalone.wd12.myworkdayjobs.com/Capital_One",
            ),
        ],
        path,
    )

    assert "workday / capitalone/wd12/Capital_One" in path.read_text(encoding="utf-8")
