import os
from collections import Counter

import pytest

from backend.app.dedupe import norm_company
from watcher.config import (
    CompanyCfg,
    ConfigError,
    DEFAULT_WATCHLIST_PATH,
    SUPPORTED_ATS,
    WatcherConfig,
    _parse_env_assignment,
    _parse_watchlist_yaml,
    load_dotenv,
    load_watchlist,
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
    "Merck": ("workday", "msd", "wd5", "SearchJobs"),
    "Pfizer": ("workday", "pfizer", "wd1", "PfizerCareers"),
    "Eli Lilly and Company": ("workday", "lilly", "wd115", "LLY"),
    "Genentech": ("workday", "roche", "wd3", "ROG-A2O-GENE"),
    "Warner Bros. Discovery": ("workday", "warnerbros", "wd5", "global"),
    "AT&T": ("workday", "att", "wd1", "ATTGeneral"),
    "Hospital for Special Surgery": ("workday", "hss", "wd1", "HSS_Careers"),
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


def test_default_watchlist_loads_and_preserves_core_invariants():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    entries = _default_watchlist_entries()
    names = [company.name for company in config.companies]
    normalized_names = [norm_company(name) for name in names]

    assert config.terms == ("Summer 2027",)
    assert config.github_listing_urls == (
        "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    )
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
        elif company.ats in {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"}:
            assert company.token
        elif company.ats == "bespoke":
            assert company.module

        if company.ats != "github_only":
            assert entry.get("source_url")
        if company.ats in {"bespoke", "github_only"}:
            assert entry.get("note")


def test_default_watchlist_contains_recent_priority_companies():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    companies_by_name = {company.name: company for company in config.companies}

    missing = sorted(RECENT_PRIORITY_COMPANIES - set(companies_by_name))
    assert missing == []
    for name in RECENT_PRIORITY_COMPANIES:
        assert companies_by_name[name].terms == ("Summer 2027",)


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


def test_alias_shared_by_two_companies_is_rejected(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  terms: ["Summer 2027"]\n',
        '  - name: "First Co"\n    ats: greenhouse\n    token: first\n    aliases: ["Shared"]\n'
        '  - name: "Second Co"\n    ats: greenhouse\n    token: second\n    alumni_match: ["shared"]\n',
    )

    with pytest.raises(ConfigError, match="ambiguous"):
        load_watchlist(path)


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
    assert config.github_listing_urls == ()
