import os
from collections import Counter

import pytest

from backend.app.dedupe import norm_company
from watcher.config import (
    CompanyCfg,
    ConfigError,
    DEFAULT_WATCHLIST_PATH,
    SUPPORTED_ATS,
    WORKDAY_DETAIL_EARLY_CAREER,
    WORKDAY_DETAIL_INTERNSHIP,
    WORKDAY_DETAIL_NONE,
    WatcherConfig,
    _parse_env_assignment,
    _parse_watchlist_yaml,
    analysis_cache_enabled,
    load_dotenv,
    load_watchlist,
    resolve_analysis_cache_path,
)


RECENT_PRIORITY_COMPANIES = {
    "Analysis Group",
    "Cornerstone Research",
    "Charles River Associates",
    "FTI Delta",
    "Bain & Company",
    "Aon",
    "WTW",
    "Arup",
    "Thornton Tomasetti",
    "Jacobs",
    "Bechtel Corporation",
    "Air Products",
    "LevelTen Energy",
    "Convergent Energy and Power",
    "Orsted",
    "Fractal Energy Storage Consultants",
    "ClimaData Corporation",
    "Trail Ridge Power",
    "Vaisala",
    "Merck",
    "Pfizer",
    "Eli Lilly and Company",
    "Genentech",
    "Exxon Mobil",
    "Warner Bros. Discovery",
    "AT&T",
    "SGLang",
    "Strategic Analysis Incorporated",
    "Hospital for Special Surgery",
}


RECENT_DIRECT_ADAPTER_METADATA = {
    "Cornerstone Research": ("workday", "cornerstone", "wd501", "CornerstoneResearch_Careers"),
    "Charles River Associates": ("greenhouse", "charlesriverassociates", "", ""),
    "FTI Delta": ("workday", "fticonsulting", "wd108", "FTIConsultingCareers"),
    "WTW": ("smartrecruiters", "WTW", "", ""),
    "Thornton Tomasetti": ("workday", "tt", "wd503", "ThorntonTomasetti"),
    "Air Products": ("workday", "airproducts", "wd5", "AP0001"),
    "LevelTen Energy": ("greenhouse", "leveltenenergy", "", ""),
    "Convergent Energy and Power": ("workable", "convergent-careers", "", ""),
    "Halo Industries": ("workable", "halo-industries", "", ""),
    "Merck": ("workday", "msd", "wd5", "SearchJobs"),
    "Pfizer": ("workday", "pfizer", "wd1", "PfizerCareers"),
    "Eli Lilly and Company": ("workday", "lilly", "wd115", "LLY"),
    "Genentech": ("workday", "roche", "wd3", "ROG-A2O-GENE"),
    "Warner Bros. Discovery": ("workday", "warnerbros", "wd5", "global"),
    "AT&T": ("workday", "att", "wd1", "ATTGeneral"),
    "Hospital for Special Surgery": ("workday", "hss", "wd1", "HSS_Careers"),
}


CONFIRMED_DIRECT_SOURCE_ADDITIONS = {
    "BlackLine": {
        "ats": "workday",
        "token": "blackline",
        "workday_shard": "wd108",
        "workday_site": "BlackLineCareers",
        "source_url": "https://blackline.wd108.myworkdayjobs.com/BlackLineCareers",
    },
    "Federal Reserve Bank of New York": {
        "ats": "workday",
        "token": "rb",
        "workday_shard": "wd5",
        "workday_site": "FRS",
        "source_url": "https://rb.wd5.myworkdayjobs.com/FRS",
    },
    "Brookfield": {
        "ats": "workday",
        "token": "brookfield",
        "workday_shard": "wd5",
        "workday_site": "brookfield",
        "source_url": "https://brookfield.wd5.myworkdayjobs.com/brookfield",
    },
    "The Carlyle Group": {
        "ats": "workday",
        "token": "carlyle",
        "workday_shard": "wd1",
        "workday_site": "Carlyle",
        "source_url": "https://carlyle.wd1.myworkdayjobs.com/Carlyle",
    },
    "American Express": {
        "ats": "oracle_hcm",
        "oracle_hcm_host": "egug.fa.us2.oraclecloud.com",
        "oracle_hcm_site": "CX_1",
        "source_url": (
            "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/"
            "sites/CX_1/jobs"
        ),
    },
    "Goldman Sachs": {
        "ats": "oracle_hcm",
        "oracle_hcm_host": "hdpc.fa.us2.oraclecloud.com",
        "oracle_hcm_site": "CampusHiring",
        "source_url": (
            "https://hdpc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/"
            "sites/CampusHiring/jobs"
        ),
    },
    "Oracle": {
        "ats": "oracle_hcm",
        "oracle_hcm_host": "eeho.fa.us2.oraclecloud.com",
        "oracle_hcm_site": "CX_45001",
        "source_url": (
            "https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/"
            "sites/CX_45001/jobs"
        ),
    },
    "Uber": {
        "ats": "oracle_hcm",
        "oracle_hcm_host": "iaziqy.fa.ocs.oraclecloud.com",
        "oracle_hcm_site": "UberCareers",
        "source_url": (
            "https://iaziqy.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/"
            "sites/UberCareers/jobs"
        ),
    },
    "Compass": {
        "ats": "greenhouse",
        "token": "urbancompass",
        "source_url": "https://job-boards.greenhouse.io/urbancompass",
    },
    "Sixth Street": {
        "ats": "greenhouse",
        "token": "sixthstreet",
        "source_url": "https://job-boards.greenhouse.io/sixthstreet",
    },
}


def _duplicates(values):
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)


def _default_watchlist_entries():
    raw = _parse_watchlist_yaml(DEFAULT_WATCHLIST_PATH.read_text(encoding="utf-8"))
    return raw["companies"]


def test_parse_env_assignment_accepts_standard_and_powershell_forms():
    assert _parse_env_assignment("SMTP_USER=youraddress@gmail.com") == (
        "SMTP_USER",
        "youraddress@gmail.com",
    )
    assert _parse_env_assignment('$env:SMTP_APP_PASSWORD = "abcdefghijklmnop"') == (
        "SMTP_APP_PASSWORD",
        "abcdefghijklmnop",
    )
    assert _parse_env_assignment("export WATCHER_SEND_EMAIL=1 # live send") == (
        "WATCHER_SEND_EMAIL",
        "1",
    )
    assert _parse_env_assignment("# comment only") is None


def test_parse_env_assignment_preserves_hashes_inside_values():
    assert _parse_env_assignment("SMTP_APP_PASSWORD=abc#def") == (
        "SMTP_APP_PASSWORD",
        "abc#def",
    )
    assert _parse_env_assignment(r'QUOTED_VALUE="abc\"#def" # comment') == (
        "QUOTED_VALUE",
        'abc"#def',
    )


def test_load_dotenv_sets_missing_values_without_overriding_existing(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "SMTP_USER=from-file@gmail.com",
                '$env:SMTP_APP_PASSWORD = "from-file-password"',
                "EMAIL_TO=to-file@gmail.com",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SMTP_USER", "already-set@gmail.com")
    for key in ("SMTP_APP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(key, raising=False)

    load_dotenv(env_path)

    assert os.environ["SMTP_USER"] == "already-set@gmail.com"
    assert os.environ["SMTP_APP_PASSWORD"] == "from-file-password"
    assert os.environ["EMAIL_TO"] == "to-file@gmail.com"


def test_watchlist_comments_preserve_hashes_inside_quotes(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer # 2027"] # trailing comment\n',
        '  - name: "Example #1"\n    ats: github_only # trailing comment\n',
    )

    config = load_watchlist(path)

    assert config.terms == ("Summer # 2027",)
    assert config.companies[0].name == "Example #1"


def test_default_watchlist_loads_and_preserves_core_invariants():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    entries = _default_watchlist_entries()
    names = [company.name for company in config.companies]
    normalized_names = [norm_company(name) for name in names]

    assert config.terms == ("Summer 2027",)
    assert config.github_listing_urls == ()
    assert [
        (source.name, source.format, source.default_term)
        for source in config.github_listing_sources
    ] == [
        ("simplify", "simplify_json", ""),
        ("sndsh404_summer_2027", "github_markdown_table", "Summer 2027"),
    ]
    assert config.target_roles == frozenset({"swe"})
    assert config.min_score is None
    assert _duplicates(names) == []
    assert _duplicates(normalized_names) == []

    entries_by_name = {entry["name"]: entry for entry in entries}
    assert set(entries_by_name) == set(names)

    for company in config.companies:
        entry = entries_by_name[company.name]
        assert company.name == company.name.strip()
        assert company.ats in SUPPORTED_ATS
        assert company.terms == ("Summer 2027",)

        if company.ats == "workday":
            assert company.token
            assert company.workday_shard.startswith("wd")
            assert company.workday_site
            assert company.workday_detail_policy in {
                WORKDAY_DETAIL_INTERNSHIP,
                WORKDAY_DETAIL_EARLY_CAREER,
            }
        elif company.ats in {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"}:
            assert company.token
        elif company.ats == "paylocity":
            assert company.paylocity_company_id
            assert company.paylocity_module_id
            assert company.paylocity_slug
        elif company.ats == "bespoke":
            assert company.module

        if company.ats != "github_only":
            assert entry.get("source_url")
        if company.ats in {"bespoke", "github_only"}:
            assert entry.get("note")


def test_default_watchlist_uses_canonical_procure_name_with_legacy_aliases():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    entry = next(
        item for item in _default_watchlist_entries()
        if item["name"] == "Procure Analytics"
    )
    procure = next(
        company
        for company in config.companies
        if company.name == "Procure Analytics"
    )

    assert procure.name == "Procure Analytics"
    assert procure.ats == "paylocity"
    assert procure.paylocity_company_id == "37f1fc46-3c9a-4802-995e-eebd78e096d7"
    assert procure.paylocity_module_id == "11566"
    assert procure.paylocity_slug == "Procurement-Advisors-LLC"
    assert procure.source_url == (
        "https://recruiting.paylocity.com/recruiting/jobs/All/"
        "37f1fc46-3c9a-4802-995e-eebd78e096d7/Procurement-Advisors-LLC"
    )
    assert procure.module == ""
    assert procure.platform_family == ""
    assert procure.coverage_status == ""
    assert not ({"module", "coverage_status", "platform_family", "note"} & set(entry))
    assert tuple(procure.aliases) == (
        "Procure",
        "Procutre Analytics",
        "Procutre",
    )
    assert tuple(procure.alumni_match) == (
        "procure analytics",
        "procure",
        "procutre analytics",
        "procutre",
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (
            '    paylocity_company_id: "bad"\n'
            '    paylocity_module_id: "11566"\n'
            '    paylocity_slug: "Example"\n'
            '    source_url: "https://recruiting.paylocity.com/recruiting/jobs/All/bad/Example"\n',
            "paylocity_company_id",
        ),
        (
            '    paylocity_company_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"\n'
            '    paylocity_module_id: "0"\n'
            '    paylocity_slug: "Example"\n'
            '    source_url: "https://recruiting.paylocity.com/recruiting/jobs/All/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/Example"\n',
            "paylocity_module_id",
        ),
        (
            '    paylocity_company_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"\n'
            '    paylocity_module_id: "1"\n'
            '    paylocity_slug: "../Example"\n'
            '    source_url: "https://recruiting.paylocity.com/recruiting/jobs/All/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/Example"\n',
            "paylocity_slug",
        ),
        (
            '    paylocity_company_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"\n'
            '    paylocity_module_id: "1"\n'
            '    paylocity_slug: "Example"\n'
            '    source_url: "https://evil.example/recruiting/jobs/All/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/Example"\n',
            "source_url",
        ),
    ],
)
def test_paylocity_configuration_requires_exact_safe_identity(
    tmp_path, fields, message
):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        '  - name: "Example"\n    ats: paylocity\n' + fields,
    )

    with pytest.raises(ConfigError, match=message):
        load_watchlist(path)


def test_default_watchlist_contains_recent_priority_companies():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    companies_by_name = {company.name: company for company in config.companies}

    missing = sorted(RECENT_PRIORITY_COMPANIES - set(companies_by_name))
    assert missing == []
    for name in RECENT_PRIORITY_COMPANIES:
        assert companies_by_name[name].terms == ("Summer 2027",)


def test_jpmorgan_watchlist_keeps_canonical_name_and_exact_match_variants():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    jpmorgan = next(
        company for company in config.companies if company.name == "JPMorgan Chase"
    )

    assert jpmorgan.name == "JPMorgan Chase"
    assert set(jpmorgan.match_names()) == {
        "JPMorgan Chase",
        "JPMorganChase",
        "J.P. Morgan Chase",
        "JP Morgan",
        "J.P. Morgan",
        "JPMC",
        "Chase",
    }
    assert jpmorgan.ats == "oracle_hcm"
    assert jpmorgan.oracle_hcm_host == "jpmc.fa.oraclecloud.com"
    assert jpmorgan.oracle_hcm_site == "CX_1001"
    assert jpmorgan.source_url == (
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
        "CX_1001/jobs"
    )


@pytest.mark.parametrize(
    ("oracle_lines", "message"),
    [
        (
            '    oracle_hcm_site: "CX_1001"\n'
            '    source_url: "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"\n',
            "oracle_hcm_host",
        ),
        (
            '    oracle_hcm_host: "jpmc.fa.oraclecloud.com"\n'
            '    source_url: "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"\n',
            "oracle_hcm_site",
        ),
        (
            '    oracle_hcm_host: "user@jpmc.fa.oraclecloud.com"\n'
            '    oracle_hcm_site: "CX_1001"\n'
            '    source_url: "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"\n',
            "hostname",
        ),
        (
            '    oracle_hcm_host: "jpmc.fa.oraclecloud.com"\n'
            '    oracle_hcm_site: "CX_1001"\n'
            '    source_url: "https://other.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/jobs"\n',
            "source_url",
        ),
    ],
)
def test_oracle_hcm_configuration_requires_explicit_safe_scope(
    tmp_path, oracle_lines, message
):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        '  - name: "Oracle Example"\n'
        '    ats: oracle_hcm\n'
        + oracle_lines,
    )

    with pytest.raises(ConfigError, match=message):
        load_watchlist(path)


def test_recent_direct_watchlist_entries_keep_verified_adapter_metadata():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    companies_by_name = {company.name: company for company in config.companies}

    for name, expected in RECENT_DIRECT_ADAPTER_METADATA.items():
        ats, token, workday_shard, workday_site = expected
        company = companies_by_name[name]

        assert company.ats == ats
        assert company.token == token
        assert company.workday_shard == workday_shard
        assert company.workday_site == workday_site


def test_halo_uses_verified_workable_configuration_without_stale_metadata():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    company = next(item for item in config.companies if item.name == "Halo Industries")
    entry = next(
        item for item in _default_watchlist_entries() if item["name"] == company.name
    )

    assert company.ats == "workable"
    assert company.token == "halo-industries"
    assert company.source_url == "https://halo-industries.workable.com/"
    assert company.aliases == ("Halo",)
    assert company.alumni_match == ("halo industries", "halo")
    assert company.module == ""
    assert company.coverage_status == ""
    assert not ({"module", "coverage_status", "platform_family", "note"} & set(entry))


def test_confirmed_direct_source_additions_use_exact_supported_configurations():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    companies_by_name = {company.name: company for company in config.companies}
    entries_by_name = {entry["name"]: entry for entry in _default_watchlist_entries()}

    for name, expected in CONFIRMED_DIRECT_SOURCE_ADDITIONS.items():
        company = companies_by_name[name]
        entry = entries_by_name[name]

        for field, value in expected.items():
            assert getattr(company, field) == value
        assert company.module == ""
        assert company.coverage_status == ""
        assert company.platform_family == ""
        assert not ({"module", "coverage_status", "platform_family", "note"} & set(entry))


def _write_watchlist(tmp_path, defaults: str, companies: str | None = None):
    path = tmp_path / "watchlist.yml"
    path.write_text(
        "defaults:\n"
        f"{defaults}"
        "companies:\n"
        + (companies or '  - name: "Example"\n    ats: github_only\n'),
        encoding="utf-8",
    )
    return path


def test_default_watchlist_uses_early_career_detail_policy_only_for_verified_sites():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    workday_companies = {
        company.name: company
        for company in config.companies
        if company.ats == "workday"
    }

    assert {
        name
        for name, company in workday_companies.items()
        if company.workday_detail_policy == WORKDAY_DETAIL_EARLY_CAREER
    } == {"Workday", "Salesforce"}
    assert all(
        company.workday_detail_policy == WORKDAY_DETAIL_INTERNSHIP
        for name, company in workday_companies.items()
        if name not in {"Workday", "Salesforce"}
    )


@pytest.mark.parametrize(
    "policy",
    [WORKDAY_DETAIL_NONE, WORKDAY_DETAIL_INTERNSHIP, WORKDAY_DETAIL_EARLY_CAREER],
)
def test_workday_detail_policy_accepts_supported_values(tmp_path, policy):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        "  - name: Example\n"
        "    ats: workday\n"
        "    token: example\n"
        "    workday_shard: wd5\n"
        "    workday_site: Site\n"
        f"    workday_detail_policy: {policy}\n",
    )

    assert load_watchlist(path).companies[0].workday_detail_policy == policy


def test_workday_detail_policy_defaults_to_internship_candidates(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        "  - name: Example\n"
        "    ats: workday\n"
        "    token: example\n"
        "    workday_shard: wd5\n"
        "    workday_site: Site\n",
    )

    assert (
        load_watchlist(path).companies[0].workday_detail_policy
        == WORKDAY_DETAIL_INTERNSHIP
    )


def test_invalid_workday_detail_policy_is_rejected(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        "  - name: Example\n"
        "    ats: workday\n"
        "    token: example\n"
        "    workday_shard: wd5\n"
        "    workday_site: Site\n"
        "    workday_detail_policy: everything\n",
    )

    with pytest.raises(ConfigError, match="workday_detail_policy"):
        load_watchlist(path)


def test_load_watchlist_parses_explicit_terms_multiple_feeds_and_inheritance(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Fall 2026", "Summer 2027"]\n'
        '  github_listing_urls: ["https://example.com/one.json", "http://example.org/two.json"]\n',
    )

    config = load_watchlist(path)

    assert config.terms == ("Fall 2026", "Summer 2027")
    assert config.github_listing_urls == (
        "https://example.com/one.json",
        "http://example.org/two.json",
    )
    assert config.companies[0].terms == config.terms
    assert [source.format for source in config.effective_github_listing_sources()] == [
        "simplify_json",
        "simplify_json",
    ]


def test_load_watchlist_parses_typed_github_sources_in_fixed_priority_order(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n'
        "  github_listing_sources:\n"
        "    - name: markdown\n"
        "      format: github_markdown_table\n"
        "      url: https://example.test/README.md\n"
        '      default_term: "Summer 2027"\n'
        "    - name: simplify\n"
        "      format: simplify_json\n"
        "      url: https://example.test/listings.json\n",
    )

    config = load_watchlist(path)

    assert [source.name for source in config.github_listing_sources] == ["markdown", "simplify"]
    assert [source.name for source in config.effective_github_listing_sources()] == [
        "simplify",
        "markdown",
    ]


def test_legacy_github_listing_urls_remain_backward_compatible(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n'
        '  github_listing_urls: ["https://example.test/listings.json"]\n',
    )

    config = load_watchlist(path)
    sources = config.effective_github_listing_sources()

    assert config.github_listing_sources == ()
    assert config.github_listing_urls == ("https://example.test/listings.json",)
    assert len(sources) == 1
    assert sources[0].format == "simplify_json"
    assert sources[0].url == config.github_listing_urls[0]


@pytest.mark.parametrize(
    ("source_lines", "message"),
    [
        (
            "    - name: markdown\n"
            "      format: github_markdown_table\n"
            "      url: https://example.test/README.md\n",
            "default_term",
        ),
        (
            "    - name: unknown\n"
            "      format: csv\n"
            "      url: https://example.test/jobs.csv\n",
            "format",
        ),
        (
            "    - name: bad name\n"
            "      format: simplify_json\n"
            "      url: https://example.test/listings.json\n",
            "name",
        ),
    ],
)
def test_invalid_typed_github_sources_are_rejected(tmp_path, source_lines, message):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n'
        "  github_listing_sources:\n"
        + source_lines,
    )

    with pytest.raises(ConfigError, match=message):
        load_watchlist(path)


def test_company_specific_terms_override_defaults(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        '  - name: "Example"\n    ats: github_only\n    terms: ["Fall 2027"]\n',
    )

    config = load_watchlist(path)

    assert config.companies[0].terms == ("Fall 2027",)


@pytest.mark.parametrize("second_name", ["Acme", "ACME, Inc."])
def test_duplicate_normalized_company_names_are_rejected(tmp_path, second_name):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        '  - name: "Acme"\n    ats: greenhouse\n    token: one\n'
        f'  - name: "{second_name}"\n    ats: greenhouse\n    token: two\n',
    )

    with pytest.raises(ConfigError, match="ambiguous"):
        load_watchlist(path)


def test_alumni_match_shared_with_another_company_alias_is_allowed(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        '  - name: "First Co"\n    ats: greenhouse\n    token: first\n    aliases: ["Shared"]\n'
        '  - name: "Second Co"\n    ats: greenhouse\n    token: second\n    alumni_match: ["shared"]\n',
    )

    config = load_watchlist(path)

    assert [company.name for company in config.companies] == ["First Co", "Second Co"]


def test_feed_urls_differing_only_by_query_are_rejected(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n'
        '  github_listing_urls: ["https://example.test/listings.json?region=us", "https://example.test/listings.json?region=eu"]\n',
    )

    with pytest.raises(ConfigError, match="duplicate feed identities"):
        load_watchlist(path)


@pytest.mark.parametrize("defaults", ["", "  target_roles: [\"swe\"]\n"])
def test_missing_defaults_terms_is_rejected(tmp_path, defaults):
    path = _write_watchlist(tmp_path, defaults)

    with pytest.raises(ConfigError, match=r"defaults\.terms.*explicitly"):
        load_watchlist(path)


@pytest.mark.parametrize("value", ["[]", '["  "]', ""])
def test_empty_defaults_terms_is_rejected(tmp_path, value):
    path = _write_watchlist(tmp_path, f"  terms: {value}\n")

    with pytest.raises(ConfigError, match=r"defaults\.terms.*nonblank"):
        load_watchlist(path)


@pytest.mark.parametrize(
    "url_value",
    [
        '["ftp://example.com/listings.json"]',
        '["not-a-url"]',
        '[""]',
        '["https://user:secret@example.com/listings.json"]',
        '["https://example.com:invalid/listings.json"]',
        '["https://[broken/listings.json"]',
        "[123]",
    ],
)
def test_invalid_github_listing_urls_are_rejected(tmp_path, url_value):
    path = _write_watchlist(
        tmp_path,
        f'  terms: ["Summer 2027"]\n  github_listing_urls: {url_value}\n',
    )

    with pytest.raises(ConfigError, match="github_listing_urls"):
        load_watchlist(path)


@pytest.mark.parametrize("value", ["[]", '["  "]', ""])
def test_explicitly_empty_company_terms_are_rejected(tmp_path, value):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        f'  - name: "Example"\n    ats: github_only\n    terms: {value}\n',
    )

    with pytest.raises(ConfigError, match=r"Example\.terms.*nonblank"):
        load_watchlist(path)


def test_dataclass_defaults_do_not_insert_a_season_or_feed():
    company = CompanyCfg(name="Manual")
    config = WatcherConfig(companies=(company,))

    assert tuple(company.terms) == ()
    assert config.terms == ()
    assert config.github_listing_sources == ()
    assert config.github_listing_urls == ()
    assert config.analysis_cache_enabled is True
    assert config.analysis_cache_path == config.seen_db_path.parent / "analysis-cache.sqlite"


def test_analysis_cache_path_defaults_to_seen_database_directory(tmp_path):
    seen_db_path = tmp_path / "durable" / "seen.sqlite"

    config = WatcherConfig(
        companies=(CompanyCfg(name="Manual"),),
        seen_db_path=seen_db_path,
    )

    assert config.analysis_cache_path == (
        seen_db_path.parent / "analysis-cache.sqlite"
    )
    assert resolve_analysis_cache_path(seen_db_path, "") == (
        seen_db_path.parent / "analysis-cache.sqlite"
    )


def test_load_watchlist_reads_explicit_analysis_cache_path(
    tmp_path,
    monkeypatch,
):
    cache_path = tmp_path / "rebuildable" / "custom-cache.sqlite"
    monkeypatch.setenv("WATCHER_ANALYSIS_CACHE_PATH", str(cache_path))

    config = load_watchlist(DEFAULT_WATCHLIST_PATH)

    assert config.analysis_cache_path == cache_path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("no", False),
        ("off", False),
        ("", True),
        ("  ", True),
    ],
)
def test_analysis_cache_enabled_parses_recognized_values(value, expected):
    assert analysis_cache_enabled(value) is expected


def test_analysis_cache_enabled_rejects_invalid_value():
    with pytest.raises(ConfigError, match="WATCHER_ANALYSIS_CACHE_ENABLED"):
        analysis_cache_enabled("sometimes")


def test_load_watchlist_reads_analysis_cache_switch_from_environment(
    monkeypatch,
):
    monkeypatch.setenv("WATCHER_ANALYSIS_CACHE_ENABLED", "false")

    config = load_watchlist(DEFAULT_WATCHLIST_PATH)

    assert config.analysis_cache_enabled is False
