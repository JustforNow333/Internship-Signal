import os
from collections import Counter

from backend.app.dedupe import norm_company
from watcher.config import (
    DEFAULT_WATCHLIST_PATH,
    SUPPORTED_ATS,
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

    assert config.terms == ("Summer 2026",)
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
        assert company.terms == ("Summer 2026",)

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
        assert companies_by_name[name].terms == ("Summer 2026",)


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
