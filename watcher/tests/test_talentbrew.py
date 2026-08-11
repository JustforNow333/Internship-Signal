import json
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.app.dedupe import dedupe, stable_requisition_key
from watcher.collection_concurrency import direct_origin_key
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, WatcherConfig, load_watchlist
from watcher.run import CollectionStats, _default_direct_sources, collect_rows
from watcher.sources import SourceFetchError, SourceSchemaError, TalentBrewSource
from watcher.sources.base import get_text_response, make_row


FIXTURES = Path(__file__).parent / "fixtures"


def json_fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def text_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def company():
    return CompanyCfg(
        name="Barclays",
        ats="talentbrew",
        talentbrew_host="search.jobs.barclays",
        talentbrew_site_id="13015",
        talentbrew_category_id="8736272",
        talentbrew_category_name="Early Careers",
        source_url="https://search.jobs.barclays/search-jobs",
        terms=("intern", "internship", "early careers"),
    )


def arm_company():
    return CompanyCfg(
        name="Arm",
        ats="talentbrew",
        talentbrew_host="careers.arm.com",
        talentbrew_site_id="33099",
        talentbrew_category_id="8097056",
        talentbrew_category_name="Graduate",
        source_url="https://careers.arm.com/search-jobs",
        terms=("intern", "internship", "graduate"),
    )


def premise_company():
    return CompanyCfg(
        name="Premise Health",
        ats="talentbrew",
        talentbrew_host="jobs.premisehealth.com",
        talentbrew_site_id="1388",
        talentbrew_category_id="8343072",
        talentbrew_category_name="Information Technology Jobs",
        source_url="https://jobs.premisehealth.com/search-jobs",
        terms=("intern", "internship", "student"),
    )


DETAILS = {
    "97463249760": "talentbrew_detail_reference.html",
    "97500000001": "talentbrew_detail_fallback.html",
    "97500000002": "talentbrew_detail_minimal.html",
}


def detail_request(url, _source_name):
    posting_id = urlsplit(url).path.rstrip("/").split("/")[-1]
    return text_fixture(DETAILS[posting_id])


def variant_detail_request(url, _source_name):
    parsed = urlsplit(url)
    posting_id = parsed.path.rstrip("/").split("/")[-1]
    if parsed.hostname == "careers.arm.com":
        title = {
            "98111111111": "Graduate Software Engineer",
            "98111111112": "Graduate Hardware Engineer",
        }[posting_id]
        location = ""
    else:
        title = {
            "98222222221": "Software Engineer Intern",
            "98222222222": "Data Analyst Intern",
            "98222222223": "Security Intern",
        }[posting_id]
        location = (
            ', "jobLocation": {"@type": "Place", "address": '
            '{"@type": "PostalAddress", "addressLocality": "Brentwood", '
            '"addressRegion": "Tennessee", "addressCountry": "United States"}}'
        )
    return (
        '<script type="application/ld+json">'
        f'{{"@context": "https://schema.org", "@type": "JobPosting", '
        f'"title": {json.dumps(title)}, "url": {json.dumps(url)}{location}}}'
        "</script>"
    )


def test_semantic_job_anchor_accepts_shared_radancy_variants_and_ignores_content():
    arm_source = TalentBrewSource(request_text=variant_detail_request)
    arm_rows = arm_source.parse(
        json_fixture("talentbrew_search_job_card.json"), arm_company()
    )
    premise_source = TalentBrewSource(request_text=variant_detail_request)
    premise_rows = premise_source.parse(
        json_fixture("talentbrew_search_heading_card.json"), premise_company()
    )

    assert [row["title"] for row in arm_rows] == [
        "Graduate Software Engineer",
        "Graduate Hardware Engineer",
    ]
    assert [row["location"] for row in arm_rows] == [
        "Austin, Texas",
        "San Jose, California",
    ]
    assert [row["title"] for row in premise_rows] == [
        "Software Engineer Intern",
        "Data Analyst Intern",
    ]
    assert all(
        row["location"] == "Brentwood, Tennessee, United States"
        for row in premise_rows
    )
    assert [row["extra"]["source_id"] for row in arm_rows] == [
        "98111111111",
        "98111111112",
    ]
    assert [row["extra"]["source_requisition_id"] for row in premise_rows] == [
        "98222222221",
        "98222222222",
    ]
    assert all(
        row["source_url"].endswith(
            ("/33099/98111111111", "/33099/98111111112")
        )
        for row in arm_rows
    )
    assert all(
        row["source_url"].endswith(
            ("/1388/98222222221", "/1388/98222222222")
        )
        for row in premise_rows
    )
    assert arm_source.last_diagnostics.raw_postings_seen == 2


def test_semantic_job_anchor_skips_malformed_neighbor_but_all_malformed_still_fails():
    payload = json_fixture("talentbrew_search_heading_card.json")
    payload["results"] = payload["results"].replace(
        '<a href="/en/job/nashville/data-analyst-intern/1388/98222222222" '
        'data-job-id="98222222222">',
        '<a href="/search-jobs" data-job-id="98222222222">',
    ).replace(
        'data-total-results="2" data-total-job-results="2"',
        'data-total-results="1" data-total-job-results="1"',
    )
    rows = TalentBrewSource(request_text=variant_detail_request).parse(
        payload, premise_company()
    )
    assert [row["extra"]["source_id"] for row in rows] == ["98222222221"]

    payload["results"] = payload["results"].replace(
        '/en/job/brentwood/software-engineer-intern/1388/98222222221',
        '/search-jobs',
    )
    with pytest.raises(SourceSchemaError, match="without valid listing records"):
        TalentBrewSource(request_text=variant_detail_request).parse(
            payload, premise_company()
        )


def test_live_zero_page_one_variant_is_a_successful_empty_board():
    source = TalentBrewSource(
        request_json=lambda *_: json_fixture("talentbrew_search_live_zero.json")
    )
    assert source.fetch(arm_company()) == []
    assert source.last_diagnostics.detail_pages_requested == 0


def test_heading_card_variant_paginates_to_the_declared_job_total():
    first = json_fixture("talentbrew_search_heading_card.json")
    first["results"] = first["results"].replace(
        'data-total-results="2" data-total-job-results="2" data-total-pages="1"',
        'data-total-results="3" data-total-job-results="3" data-total-pages="2"',
    ).replace('data-records-per-page="16"', 'data-records-per-page="2"')
    payloads = iter(
        (first, json_fixture("talentbrew_search_heading_page_2.json"))
    )
    source = TalentBrewSource(
        request_json=lambda *_: next(payloads),
        request_text=variant_detail_request,
        page_size=2,
    )

    rows = source.fetch(premise_company())

    assert [row["extra"]["source_id"] for row in rows] == [
        "98222222221",
        "98222222222",
        "98222222223",
    ]
    assert source.last_diagnostics.listing_pages_requested == 2
    assert source.last_diagnostics.detail_pages_requested == 3


def test_single_page_maps_official_fields_reference_and_scope():
    source = TalentBrewSource(request_text=detail_request)
    rows = source.parse(json_fixture("talentbrew_search_single.json"), company())

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "2027 Technology Developer Summer Internship Programme Singapore"
    assert row["location"] == "Singapore, Central Singapore, Singapore; Whippany, New Jersey, United States"
    assert row["description"] == "Build resilient banking technology. Who we are looking for Currently studying computer science. Strong programming skills."
    assert row["requirements"] == "Currently studying computer science. Strong programming skills."
    assert row["date_posted"] == "2026-07-07"
    assert row["deadline"] == "2026-10-15"
    assert row["compensation"] == "USD 35–45 per HOUR"
    assert row["remote_status"] == "Hybrid"
    assert row["internship_type"] == "Intern; Full time; Internship programmes; Technology Internship Programme"
    assert row["source_url"].endswith("/13015/97463249760")
    assert row["extra"] == {
        "source": "direct",
        "source_adapter": "talentbrew",
        "source_id": "97463249760",
        "source_requisition_id": "JR-0000121763",
        "source_system": "talentbrew",
        "source_scope": "search.jobs.barclays:13015",
        "talentbrew_host": "search.jobs.barclays",
        "talentbrew_site_id": "13015",
        "talentbrew_posting_id": "97463249760",
        "active": True,
        "official_category": "Early Careers",
        "official_programme": "Internship programmes",
        "official_contract": "Intern",
        "official_reference_code": "JR-0000121763",
        "official_work_pattern": "Hybrid",
    }


def test_multi_page_final_partial_page_and_cross_page_duplicate_are_complete():
    payloads = iter((json_fixture("talentbrew_search_page_1.json"), json_fixture("talentbrew_search_page_2.json")))
    urls = []

    def request_json(url, _source_name):
        urls.append(url)
        return next(payloads)

    source = TalentBrewSource(request_json=request_json, request_text=detail_request, page_size=2)
    rows = source.fetch(company())

    assert [row["extra"]["talentbrew_posting_id"] for row in rows] == [
        "97463249760", "97500000001", "97500000002"
    ]
    assert [parse_qs(urlsplit(url).query)["CurrentPage"] for url in urls] == [["1"], ["2"]]
    assert source.last_diagnostics.listing_pages_requested == 2
    assert source.last_diagnostics.detail_pages_requested == 3
    assert source.last_diagnostics.duplicate_postings_skipped == 1


def test_reference_falls_back_to_stable_platform_posting_id_and_optional_fields_can_be_missing():
    payload = json_fixture("talentbrew_search_page_1.json")
    payload["results"] = payload["results"].replace('data-total-results="3" data-total-pages="2"', 'data-total-results="2" data-total-pages="1"')
    rows = TalentBrewSource(request_text=detail_request).parse(payload, company())

    assert rows[1]["extra"]["source_requisition_id"] == "97500000001"
    assert rows[1]["date_posted"] == ""
    assert rows[1]["deadline"] == ""
    assert rows[1]["source_url"].endswith("/13015/97500000001")


def test_valid_zero_results_are_a_successful_empty_board():
    source = TalentBrewSource(request_json=lambda *_: json_fixture("talentbrew_search_zero.json"))
    stats = CollectionStats()
    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"talentbrew": source},
        stats=stats,
    )
    assert rows == []
    assert errors == []
    assert stats.source_attempts[0].succeeded is True
    assert stats.source_attempts[0].rows_returned == 0
    assert source.last_diagnostics.detail_pages_requested == 0


def test_source_failure_is_reported_differently_from_an_empty_board():
    failure = SourceFetchError("blocked", error_code="html_challenge", retryable=False)
    source = TalentBrewSource(request_json=lambda *_: (_ for _ in ()).throw(failure))
    stats = CollectionStats()
    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"talentbrew": source},
        stats=stats,
    )
    assert rows == []
    assert len(errors) == 1
    assert stats.source_attempts[0].succeeded is False
    assert stats.source_attempts[0].error_kind == "fetch_failure/html_challenge"


@pytest.mark.parametrize("payload", [None, [], {}, {"filters": "", "results": "", "hasJobs": True, "hasContent": True}])
def test_malformed_or_materially_changed_search_schema_fails(payload):
    with pytest.raises(SourceSchemaError):
        TalentBrewSource(request_text=detail_request).parse(payload, company())


def test_repeated_page_is_rejected():
    first = json_fixture("talentbrew_search_page_1.json")
    pages = iter((first, first))
    source = TalentBrewSource(request_json=lambda *_: next(pages), request_text=detail_request, page_size=2)
    with pytest.raises(SourceSchemaError, match="repeated|current page"):
        source.fetch(company())


def test_non_advancing_current_page_is_rejected():
    first = json_fixture("talentbrew_search_page_1.json")
    second = json_fixture("talentbrew_search_page_2.json")
    second["results"] = second["results"].replace('data-current-page="2"', 'data-current-page="1"')
    pages = iter((first, second))
    source = TalentBrewSource(request_json=lambda *_: next(pages), request_text=detail_request, page_size=2)
    with pytest.raises(SourceSchemaError, match="current page|advance"):
        source.fetch(company())


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_search_and_detail_failures_use_bounded_retries(status):
    calls = 0
    delays = []

    def request_json(*_):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise SourceFetchError("transient", status_code=status, retryable=True, response_metadata={"retry_after_seconds": 0})
        return json_fixture("talentbrew_search_zero.json")

    source = TalentBrewSource(request_json=request_json, sleeper=delays.append, jitter=lambda *_: 0)
    assert source.fetch(company()) == []
    assert calls == 3
    assert delays == [1.0, 3.0]
    assert source.last_diagnostics.retry_attempts == 2


def test_permanent_4xx_fails_without_retry():
    calls = 0
    def request_json(*_):
        nonlocal calls
        calls += 1
        raise SourceFetchError("not found", status_code=404, retryable=False)
    with pytest.raises(SourceFetchError):
        TalentBrewSource(request_json=request_json, sleeper=lambda _: None).fetch(company())
    assert calls == 1


def test_detail_failure_identifies_posting_and_does_not_emit_incomplete_row():
    def fail_detail(*_):
        raise SourceFetchError("challenge", error_code="html_challenge", retryable=False)
    with pytest.raises(SourceFetchError, match="97463249760"):
        TalentBrewSource(request_text=fail_detail).parse(json_fixture("talentbrew_search_single.json"), company())


def test_access_challenge_html_200_detail_is_a_failure():
    challenge = "<!doctype html><html><body>Security check: verify you are human</body></html>"
    with pytest.raises(SourceFetchError) as raised:
        TalentBrewSource(request_text=lambda *_: challenge).parse(json_fixture("talentbrew_search_single.json"), company())
    assert raised.value.error_code == "html_challenge"


def test_normal_job_copy_using_the_word_challenge_is_not_an_access_interstitial():
    detail = text_fixture("talentbrew_detail_reference.html").replace(
        "Build resilient banking technology.",
        "Challenge yourself while building resilient banking technology.",
    )
    class Response:
        status = 200
        code = 200
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        def read(self, size=-1):
            body = detail.encode("utf-8")
            return body if size < 0 else body[:size]
        def geturl(self):
            return "https://search.jobs.barclays/job/example/13015/97463249760"
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    rows = TalentBrewSource(
        request_text=lambda url, name: get_text_response(
            url, name, opener=lambda *_args, **_kwargs: Response()
        )
    ).parse(
        json_fixture("talentbrew_search_single.json"), company()
    )
    assert len(rows) == 1
    assert rows[0]["extra"]["source_requisition_id"] == "JR-0000121763"


def test_direct_and_github_rows_merge_by_official_url_with_direct_primary():
    direct = TalentBrewSource(request_text=detail_request).parse(json_fixture("talentbrew_search_single.json"), company())[0]
    github = make_row(
        source="github", source_adapter="github_markdown_table", company="Barclays",
        title=direct["title"], location=direct["location"], source_url=direct["source_url"],
        extra={"source_format": "github_markdown_table"},
    )
    merged, report = dedupe([github, direct])
    assert len(merged) == 1
    assert merged[0]["extra"]["source_adapter"] == "talentbrew"
    assert report[0]["cross_source"] is True


def test_distinct_reference_codes_remain_distinct():
    first = TalentBrewSource(request_text=detail_request).parse(json_fixture("talentbrew_search_single.json"), company())[0]
    second = dict(first)
    second["extra"] = dict(first["extra"], source_requisition_id="JR-0000999999", official_reference_code="JR-0000999999")
    second["source_url"] = first["source_url"].replace("97463249760", "99999999999")
    assert stable_requisition_key(first) != stable_requisition_key(second)
    assert len(dedupe([first, second])[0]) == 2


def test_dispatch_origin_and_snapshot_fingerprint_include_talentbrew_config():
    sources = _default_direct_sources()
    assert isinstance(sources["talentbrew"], TalentBrewSource)
    assert direct_origin_key("talentbrew", talentbrew_host="search.jobs.barclays") == "https://search.jobs.barclays"
    config = WatcherConfig(companies=(company(),))
    changed = WatcherConfig(companies=(CompanyCfg(**{**company().__dict__, "talentbrew_category_id": "different"}),))
    assert collection_config_fingerprint(config) != collection_config_fingerprint(changed)


def test_watchlist_registers_barclays_official_early_careers_scope():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    barclays = next(item for item in config.companies if item.name == "Barclays")
    assert barclays.ats == "talentbrew"
    assert barclays.talentbrew_host == "search.jobs.barclays"
    assert barclays.talentbrew_site_id == "13015"
    assert barclays.talentbrew_category_id == "8736272"
    assert barclays.talentbrew_category_name == "Early Careers"
    assert barclays.source_url == "https://search.jobs.barclays/search-jobs"


def test_watchlist_registers_arm_and_premise_health_talentbrew_scopes():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    companies = {item.name: item for item in config.companies}

    arm = companies["Arm"]
    assert arm.ats == "talentbrew"
    assert arm.talentbrew_host == "careers.arm.com"
    assert arm.talentbrew_site_id == "33099"
    assert arm.talentbrew_category_id == "8097056"
    assert arm.talentbrew_category_name == "Graduate"
    assert arm.source_url == "https://careers.arm.com/search-jobs"

    premise = companies["Premise Health"]
    assert premise.ats == "talentbrew"
    assert premise.talentbrew_host == "jobs.premisehealth.com"
    assert premise.talentbrew_site_id == "1388"
    assert premise.talentbrew_category_id == "8343072"
    assert premise.talentbrew_category_name == "Information Technology Jobs"
    assert premise.source_url == "https://jobs.premisehealth.com/search-jobs"
