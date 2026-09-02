import copy
import json
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.config import (
    DEFAULT_WATCHLIST_PATH,
    CompanyCfg,
    ConfigError,
    WatcherConfig,
    load_watchlist,
)
from watcher.collection_concurrency import direct_origin_key
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.run import CollectionStats, _default_direct_sources, collect_rows
from watcher.sources import SourceError, SourceFetchError, SourceSchemaError
from watcher.sources.icims import IcimsSource


FIXTURES = Path(__file__).parent / "fixtures"
INVALID_DNS_HOSTS = (
    "-bad.example.test",
    "bad-.example.test",
    f"{'a' * 64}.example.test",
)


def json_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def text_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def jibe_company(host: str = "jobs.example.test") -> CompanyCfg:
    return CompanyCfg(
        name="Jibe Example",
        ats="icims",
        icims_variant="jibe_json",
        icims_host=host,
        source_url=f"https://{host}/jobs",
    )


def classic_company(
    host: str = "classic.example.test",
    portals: tuple[str, ...] = (),
) -> CompanyCfg:
    return CompanyCfg(
        name="Classic Example",
        ats="icims",
        icims_variant="classic",
        icims_host=host,
        icims_portals=portals,
        source_url=f"https://{host}/jobs/search",
    )


def test_jibe_multi_page_maps_canonical_fields_and_namespaced_identity():
    pages = iter((json_fixture("icims_jibe_page_1.json"), json_fixture("icims_jibe_page_2.json")))
    urls = []

    def request_json(url, source_name):
        urls.append(url)
        assert source_name == "icims"
        return next(pages)

    source = IcimsSource(request_json=request_json, jibe_page_size=2)
    rows = source.fetch(jibe_company())

    assert len(rows) == 3
    assert [parse_qs(urlsplit(url).query) for url in urls] == [
        {"limit": ["2"], "page": ["1"]},
        {"limit": ["2"], "page": ["2"]},
    ]
    first = rows[0]
    assert first["company"] == "Jibe Example"
    assert first["title"] == "Software Engineer Intern"
    assert first["location"] == (
        "New York, New York, United States; Boston, Massachusetts, United States"
    )
    assert first["description"] == (
        "Build reliable software. Design and test services."
    )
    assert first["requirements"] == "Currently enrolled in a degree program."
    assert first["internship_type"] == "Intern"
    assert first["date_posted"] == "2026-08-01"
    assert first["source_url"] == "https://jobs.example.test/jobs/101"
    assert first["extra"]["application_url"] == (
        "https://careers-example.icims.com/jobs/101/login"
    )
    assert rows[1]["extra"]["application_url"] == (
        "https://jobs.example.test/prelogin/102"
    )
    assert first["extra"]["source_id"] == "jobs.example.test:101"
    assert first["extra"]["source_requisition_id"] == "jobs.example.test:101"
    assert first["extra"]["icims_native_id"] == "101"
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.degraded is False


def test_jibe_exact_empty_requires_empty_jobs_and_zero_total():
    source = IcimsSource(request_json=lambda *_: json_fixture("icims_jibe_zero.json"))

    assert source.fetch(jibe_company()) == []
    assert source.last_health_diagnostics.complete is True

    for payload in (
        {"jobs": [], "totalCount": 1},
        {"jobs": [{"data": {}}], "totalCount": 0},
        {"jobs": []},
        {"jobs": [], "totalCount": "0"},
    ):
        with pytest.raises(SourceSchemaError):
            IcimsSource(request_json=lambda *_args, p=payload: p).fetch(jibe_company())


def test_jibe_mixed_malformed_records_degrade_but_all_malformed_fails():
    payload = json_fixture("icims_jibe_page_1.json")
    payload["jobs"] = [payload["jobs"][0], {"data": {"req_id": "bad"}}, "broken"]
    payload["totalCount"] = 3
    source = IcimsSource(request_json=lambda *_: payload)

    rows = source.fetch(jibe_company())

    assert len(rows) == 1
    assert source.last_health_diagnostics.malformed_row_count == 1
    assert source.last_health_diagnostics.schema_error_row_count == 1
    assert source.last_health_diagnostics.reason_codes == (
        "malformed_records_skipped",
        "schema_invalid_records_skipped",
    )
    assert source.last_health_diagnostics.degraded is True
    assert source.last_health_diagnostics.complete is False

    payload["jobs"] = payload["jobs"][1:]
    payload["totalCount"] = 2
    with pytest.raises(SourceSchemaError, match="none were valid"):
        IcimsSource(request_json=lambda *_: payload).fetch(jibe_company())


def test_jibe_rejects_repeated_pages_changing_totals_and_incomplete_enumeration():
    first = json_fixture("icims_jibe_page_1.json")
    repeated = copy.deepcopy(first)
    iter_pages = iter((first, repeated))
    with pytest.raises(SourceSchemaError, match="repeated"):
        IcimsSource(request_json=lambda *_: next(iter_pages), jibe_page_size=2).fetch(jibe_company())

    changed = json_fixture("icims_jibe_page_2.json")
    changed["totalCount"] = 4
    iter_pages = iter((first, changed))
    with pytest.raises(SourceSchemaError, match="totalCount"):
        IcimsSource(request_json=lambda *_: next(iter_pages), jibe_page_size=2).fetch(jibe_company())

    premature = {"jobs": [], "totalCount": 3}
    iter_pages = iter((first, premature))
    with pytest.raises(SourceSchemaError, match="before"):
        IcimsSource(request_json=lambda *_: next(iter_pages), jibe_page_size=2).fetch(jibe_company())


def test_jibe_rejects_cross_host_or_generic_urls_and_conflicting_req_ids():
    payload = json_fixture("icims_jibe_page_1.json")
    payload["jobs"] = payload["jobs"][:1]
    payload["totalCount"] = 1

    for url in (
        "https://other.example.test/jobs/101",
        "https://jobs.example.test/jobs",
        "https://jobs.example.test/signin",
    ):
        invalid = copy.deepcopy(payload)
        invalid["jobs"][0]["data"]["meta_data"]["canonical_url"] = url
        with pytest.raises(SourceSchemaError, match="none were valid"):
            IcimsSource(request_json=lambda *_args, p=invalid: p).fetch(jibe_company())

    invalid_application = copy.deepcopy(payload)
    invalid_application["jobs"][0]["data"]["apply_url"] = (
        "https://unrelated.test/jobs/101/login"
    )
    with pytest.raises(SourceSchemaError, match="none were valid"):
        IcimsSource(request_json=lambda *_: invalid_application).fetch(jibe_company())

    conflicting = copy.deepcopy(payload)
    second = copy.deepcopy(conflicting["jobs"][0])
    second["data"]["title"] = "Conflicting title"
    conflicting["jobs"].append(second)
    conflicting["totalCount"] = 2
    with pytest.raises(SourceSchemaError, match="conflicting"):
        IcimsSource(request_json=lambda *_: conflicting).fetch(jibe_company())


def test_jibe_exact_duplicate_is_removed_without_degrading_health():
    payload = json_fixture("icims_jibe_page_1.json")
    payload["jobs"] = [payload["jobs"][0], copy.deepcopy(payload["jobs"][0])]
    payload["totalCount"] = 2
    source = IcimsSource(request_json=lambda *_: payload)

    rows = source.fetch(jibe_company())

    assert len(rows) == 1
    assert source.last_health_diagnostics.duplicate_row_count == 1
    assert source.last_health_diagnostics.degraded is False


def test_classic_multi_page_iframe_listing_maps_rows_without_detail_enrichment():
    pages = iter((text_fixture("icims_classic_page_0.html"), text_fixture("icims_classic_page_1.html")))
    urls = []

    def request_text(url, source_name):
        urls.append(url)
        assert source_name == "icims"
        return next(pages)

    source = IcimsSource(request_text=request_text)
    rows = source.fetch(classic_company())

    assert len(rows) == 3
    assert [parse_qs(urlsplit(url).query) for url in urls] == [
        {"ss": ["1"], "in_iframe": ["1"], "pr": ["0"]},
        {"ss": ["1"], "in_iframe": ["1"], "pr": ["1"]},
    ]
    assert rows[0]["title"] == "Software Engineer Intern"
    assert rows[0]["location"] == "US-CT-Groton"
    assert rows[0]["description"] == "Build production services."
    assert rows[0]["internship_type"] == ""
    assert rows[0]["extra"]["category"] == "Engineering"
    assert rows[0]["source_url"] == (
        "https://classic.example.test/jobs/201/software-engineer-intern/job"
    )
    assert rows[0]["extra"]["source_id"] == "classic.example.test:201"
    assert rows[0]["extra"]["icims_native_id"] == "201"
    assert rows[1]["location"] == "US-RI-Quonset Point"
    assert source.last_health_diagnostics.complete is True


def test_classic_explicit_empty_is_success_but_outer_shell_and_ambiguous_empty_fail():
    source = IcimsSource(request_text=lambda *_: text_fixture("icims_classic_zero.html"))
    assert source.fetch(classic_company()) == []
    assert source.last_health_diagnostics.complete is True

    with pytest.raises(SourceSchemaError, match="outer"):
        IcimsSource(request_text=lambda *_: text_fixture("icims_classic_outer_shell.html")).fetch(
            classic_company()
        )
    with pytest.raises(SourceSchemaError, match="listing contract"):
        IcimsSource(request_text=lambda *_: "<html><body>Open positions</body></html>").fetch(
            classic_company()
        )


def test_classic_malformed_neighbors_skip_and_invalid_urls_or_conflicts_fail():
    valid = text_fixture("icims_classic_page_1.html").replace("Page 2 of 2", "Page 1 of 1")
    malformed = valid.replace(
        "</ul>",
        '<li class="iCIMS_JobCardItem"><div class="title"><h3>Missing URL</h3></div></li></ul>',
    )
    source = IcimsSource(request_text=lambda *_: malformed)
    rows = source.fetch(classic_company())
    assert len(rows) == 1
    assert source.last_health_diagnostics.schema_error_row_count == 1
    assert source.last_health_diagnostics.degraded is True

    all_bad = re.sub(
        r'<li class="iCIMS_JobCardItem">.*?</li>',
        '<li class="iCIMS_JobCardItem"><div class="title"><h3>Missing URL</h3></div></li>',
        valid,
        flags=re.S,
    )
    with pytest.raises(SourceSchemaError, match="none were valid"):
        IcimsSource(request_text=lambda *_: all_bad).fetch(classic_company())

    cross_host = valid.replace("https://classic.example.test/jobs/203", "https://other.test/jobs/203")
    with pytest.raises(SourceSchemaError, match="none were valid"):
        IcimsSource(request_text=lambda *_: cross_host).fetch(classic_company())

    generic_url = valid.replace(
        "/jobs/203/systems-engineer/job",
        "/jobs/search",
    )
    with pytest.raises(SourceSchemaError, match="none were valid"):
        IcimsSource(request_text=lambda *_: generic_url).fetch(classic_company())

    conflicting_card = re.search(r'<li class="iCIMS_JobCardItem">.*?</li>', valid, re.S).group(0)
    conflicting_card = conflicting_card.replace("Systems Engineer", "Conflicting Title")
    conflict = valid.replace("</ul>", conflicting_card + "</ul>")
    with pytest.raises(SourceSchemaError, match="conflicting"):
        IcimsSource(request_text=lambda *_: conflict).fetch(classic_company())


def test_classic_repeated_or_inconsistent_pagination_fails():
    first = text_fixture("icims_classic_page_0.html")
    pages = iter((first, first))
    with pytest.raises(SourceSchemaError, match="repeated"):
        IcimsSource(request_text=lambda *_: next(pages)).fetch(classic_company())

    changed = text_fixture("icims_classic_page_1.html").replace("Page 2 of 2", "Page 2 of 3")
    pages = iter((first, changed))
    with pytest.raises(SourceSchemaError, match="page count"):
        IcimsSource(request_text=lambda *_: next(pages)).fetch(classic_company())


def test_multi_portal_combines_namespaced_collisions_and_allows_empty_sibling():
    portals = ("one.example.test", "two.example.test")
    populated = text_fixture("icims_classic_page_1.html").replace(
        "classic.example.test", "one.example.test"
    ).replace("Page 2 of 2", "Page 1 of 1")
    second = populated.replace("one.example.test", "two.example.test")

    def both(url, _source_name):
        return second if urlsplit(url).hostname == "two.example.test" else populated

    rows = IcimsSource(request_text=both).fetch(classic_company("one.example.test", portals))
    assert len(rows) == 2
    assert [row["extra"]["source_id"] for row in rows] == [
        "one.example.test:203",
        "two.example.test:203",
    ]

    rows = IcimsSource(
        request_text=lambda url, _: (
            text_fixture("icims_classic_zero.html")
            if urlsplit(url).hostname == "two.example.test"
            else populated
        )
    ).fetch(classic_company("one.example.test", portals))
    assert len(rows) == 1


def test_multi_portal_failure_makes_entire_company_incomplete():
    portals = ("one.example.test", "two.example.test")
    populated = text_fixture("icims_classic_page_1.html").replace(
        "classic.example.test", "one.example.test"
    ).replace("Page 2 of 2", "Page 1 of 1")

    def request(url, _source_name):
        if urlsplit(url).hostname == "two.example.test":
            raise SourceFetchError("unavailable", error_code="transient_http_error")
        return populated

    with pytest.raises(SourceFetchError):
        IcimsSource(request_text=request).fetch(classic_company("one.example.test", portals))

    stats = CollectionStats()
    rows, errors = collect_rows(
        WatcherConfig(companies=(classic_company("one.example.test", portals),)),
        direct_sources={"icims": IcimsSource(request_text=request)},
        stats=stats,
    )
    assert rows == []
    assert len(errors) == 1
    assert stats.source_attempts[0].succeeded is False
    assert stats.source_attempts[0].incomplete is True


def test_mixed_record_loss_flows_into_existing_source_health_contract():
    payload = json_fixture("icims_jibe_page_1.json")
    payload["jobs"] = [payload["jobs"][0], {"data": {"req_id": "bad"}}]
    payload["totalCount"] = 2
    source = IcimsSource(request_json=lambda *_: payload)
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(jibe_company(),)),
        direct_sources={"icims": source},
        stats=stats,
    )

    assert len(rows) == 1
    assert errors == []
    attempt = stats.source_attempts[0]
    assert attempt.succeeded is True
    assert attempt.degraded is True
    assert attempt.complete is False
    assert attempt.schema_error_row_count == 1
    assert attempt.reason_codes == ("schema_invalid_records_skipped",)


def test_default_registry_includes_icims_without_replacing_existing_adapters():
    sources = _default_direct_sources()

    assert isinstance(sources["icims"], IcimsSource)
    assert {"greenhouse", "lever", "ashby", "oracle_hcm", "talentbrew", "workday"} <= set(sources)


def test_icims_scope_affects_origin_and_collection_snapshot_fingerprint():
    company = classic_company(
        "one.example.test",
        ("one.example.test", "two.example.test"),
    )
    config = WatcherConfig(companies=(company,))

    assert direct_origin_key("icims", icims_host=company.icims_host) == (
        "https://one.example.test"
    )
    for changed in (
        replace(company, icims_variant="jibe_json"),
        replace(company, icims_host="three.example.test"),
        replace(
            company,
            icims_portals=("one.example.test", "three.example.test"),
        ),
    ):
        assert collection_config_fingerprint(
            WatcherConfig(companies=(changed,))
        ) != collection_config_fingerprint(config)


def test_default_watchlist_uses_verified_icims_configuration_for_eight_companies():
    companies = {
        company.name: company
        for company in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
    }
    expected = {
        "AMD": ("jibe_json", "careers.amd.com", ()),
        "Aon": ("jibe_json", "jobs.aon.com", ()),
        "Docusign": ("jibe_json", "careers.docusign.com", ()),
        "GitHub": ("jibe_json", "githubinc.jibeapply.com", ()),
        "JHU Applied Physics Laboratory": ("jibe_json", "careers.jhuapl.edu", ()),
        "ZS": ("jibe_json", "jobs.zs.com", ()),
        "General Dynamics Electric Boat": ("classic", "careers-gdeb.icims.com", ()),
        "Analysis Group": (
            "classic",
            "professionalcareers-analysisgroup.icims.com",
            (
                "professionalcareers-analysisgroup.icims.com",
                "datasciencecareers-analysisgroup.icims.com",
            ),
        ),
    }

    for name, (variant, host, portals) in expected.items():
        company = companies[name]
        assert company.ats == "icims"
        assert company.icims_variant == variant
        assert company.icims_host == host
        assert tuple(company.icims_portals) == portals
        assert company.module == ""


@pytest.mark.parametrize(
    ("config_lines", "message"),
    [
        ('    icims_host: "jobs.example.test"\n', "icims_variant"),
        ('    icims_variant: jibe_json\n', "icims_host"),
        (
            '    icims_variant: unsupported\n    icims_host: "jobs.example.test"\n',
            "icims_variant",
        ),
        (
            '    icims_variant: jibe_json\n    icims_host: "https://jobs.example.test"\n',
            "hostname",
        ),
        (
            '    icims_variant: classic\n    icims_host: "one.example.test"\n'
            '    icims_portals: ["two.example.test"]\n',
            "icims_portals",
        ),
        (
            '    icims_variant: classic\n    icims_host: "one.example.test"\n'
            '    icims_portals: ["one.example.test", "one.example.test"]\n',
            "unique",
        ),
    ],
)
def test_icims_configuration_requires_an_explicit_safe_scope(tmp_path, config_lines, message):
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
        '  - name: "iCIMS Example"\n    ats: icims\n'
        + config_lines
        + '    source_url: "https://one.example.test/jobs"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=message):
        load_watchlist(path)


@pytest.mark.parametrize("host", INVALID_DNS_HOSTS)
def test_icims_configuration_rejects_invalid_dns_hostname_labels(tmp_path, host):
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
        '  - name: "iCIMS Example"\n    ats: icims\n'
        '    icims_variant: jibe_json\n'
        f'    icims_host: "{host}"\n'
        f'    source_url: "https://{host}/jobs"\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="icims_host must be a hostname"):
        load_watchlist(path)


@pytest.mark.parametrize("host", INVALID_DNS_HOSTS)
def test_icims_runtime_rejects_invalid_dns_hostname_without_request(host):
    requested = []
    source = IcimsSource(request_json=lambda url, _source_name: requested.append(url))

    with pytest.raises(SourceError, match="valid icims_host"):
        source.fetch(jibe_company(host))

    assert requested == []
