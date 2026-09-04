from pathlib import Path

import pytest

from watcher.company_matching import (
    company_matching_key,
    company_matches,
    match_watchlist_company,
)
from watcher.config import (
    DEFAULT_WATCHLIST_PATH,
    CompanyCfg,
    ConfigError,
    load_watchlist,
)
from watcher.sources.github_listings import GitHubListingsSource
from watcher.sources.github_markdown_table import GitHubMarkdownTableSource


FEED_URL = "https://fixtures.example.test/internships"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("  ACME,   Inc. ", "acme"),
        ("Air Products & Chemicals", "Air Products and Chemicals"),
        ("Société Générale", "Societe Generale"),
        ("Ørsted", "Orsted"),
        ("JPMorganChase", "JP Morgan Chase"),
        ("BoschGroup", "Bosch Group"),
        ("Acme Incorporated LLC", "Acme"),
        ("Acme PLC GmbH AG", "Acme"),
        ("KPMG US LLP", "kpmg us"),
    ],
)
def test_company_matching_key_normalizes_safe_equivalents(left, right):
    assert company_matching_key(left) == company_matching_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Łódź Labs", "Lodz Labs"),
        ("Æther Systems", "Aether Systems"),
        ("Œuvre Software", "Oeuvre Software"),
        ("Ðelta Technologies", "Delta Technologies"),
        ("Þing Capital", "Thing Capital"),
    ],
)
def test_company_matching_key_has_explicit_non_decomposing_transliterations(left, right):
    assert company_matching_key(left) == company_matching_key(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Arm", "Armstrong"),
        ("Compass", "Compass Group"),
        ("Epic", "Epic Games"),
        ("American Express", "American Express Global Business Travel"),
        ("Amazon", "Amazon Web Services"),
        ("Oracle", "Oracle Health"),
        ("Acme Holdings", "Acme"),
        ("Acme Technologies", "Acme"),
        ("Acme Group AG", "Acme"),
        ("Acme A G", "Acme"),
        ("Convergent Energy + Power", "Convergent Energy and Power"),
    ],
)
def test_company_matching_key_preserves_meaningful_distinctions(left, right):
    assert company_matching_key(left) != company_matching_key(right)


def test_company_matches_uses_only_exact_canonical_and_alias_keys():
    company = CompanyCfg(
        name="Amazon",
        aliases=("Amazon Web Services", "AWS"),
        alumni_match=("Amazon Studios",),
    )

    assert company_matches("Amazon, Inc.", company)
    assert company_matches("Amazon Web Services", company)
    assert company_matches("AWS", company)
    assert not company_matches("Amazon Studios", company)
    assert not company_matches("Amazonian", company)
    assert not company_matches("Amazom", company)


@pytest.mark.parametrize(
    ("configured_name", "observed_name"),
    [
        ("AMD", "Advanced Micro Devices"),
        ("Analog Devices", "ADI"),
        ("Cadence", "Cadence Design Systems"),
        ("Marvell", "Marvell Technology"),
    ],
)
def test_verified_config_expansion_aliases_match_unambiguously(
    configured_name, observed_name
):
    companies = {
        company.name: company
        for company in load_watchlist(DEFAULT_WATCHLIST_PATH).companies
    }

    assert company_matches(observed_name, companies[configured_name])


def _write_watchlist(tmp_path: Path, companies: str) -> Path:
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n' + companies,
        encoding="utf-8",
    )
    return path


def test_config_rejects_cross_company_key_ambiguity(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  - name: "Acme Holdings"\n    ats: github_only\n'
        '  - name: "Other"\n    ats: github_only\n'
        '    aliases: ["Acme Holdings, Inc."]\n',
    )

    with pytest.raises(ConfigError, match="ambiguous"):
        load_watchlist(path)


@pytest.mark.parametrize(
    "aliases",
    [
        '["KPMG US", "KPMG US LLP"]',
        '["Valid", "  "]',
    ],
)
def test_config_rejects_duplicate_normalized_or_blank_aliases(tmp_path, aliases):
    path = _write_watchlist(
        tmp_path,
        f'  - name: "Example"\n    ats: github_only\n    aliases: {aliases}\n',
    )

    with pytest.raises(ConfigError, match="alias"):
        load_watchlist(path)


def test_alumni_match_does_not_claim_a_collection_label_or_create_ambiguity(tmp_path):
    path = _write_watchlist(
        tmp_path,
        '  - name: "First"\n    ats: github_only\n'
        '  - name: "Second"\n    ats: github_only\n'
        '    alumni_match: ["First"]\n',
    )

    config = load_watchlist(path)
    second = next(company for company in config.companies if company.name == "Second")
    assert not company_matches("First", second)


def _listing(label: str, index: int) -> dict:
    return {
        "company_name": label,
        "title": "Software Engineer Intern",
        "locations": ["United States"],
        "url": f"https://example.test/jobs/{index}",
        "date_posted": "2026-08-04",
        "active": True,
        "terms": ["Summer 2027"],
    }


def _representative_companies() -> tuple[CompanyCfg, ...]:
    terms = ("Summer 2027",)
    return (
        CompanyCfg(name="JPMorgan Chase", terms=terms),
        CompanyCfg(name="KPMG", aliases=("KPMG US",), terms=terms),
        CompanyCfg(name="EY", aliases=("Ernst & Young",), terms=terms),
        CompanyCfg(name="UBS", aliases=("UBS Group",), terms=terms),
        CompanyCfg(name="Orsted", aliases=("Oersted",), terms=terms),
        CompanyCfg(
            name="Amazon",
            aliases=("Amazon Web Services", "AWS"),
            terms=terms,
        ),
    )


VARIANTS = (
    "JPMorgan Chase & Co.",
    "KPMG LLP",
    "Ernst & Young LLP",
    "UBS Group AG",
    "Ørsted",
    "Amazon Web Services",
)


def test_simplify_matches_representative_variants_and_rejects_similar_names():
    payload = [
        *(_listing(label, index) for index, label in enumerate(VARIANTS)),
        _listing("Armstrong", 100),
        _listing("Amazon Studios", 101),
        _listing("Oracle Health", 102),
    ]
    source = GitHubListingsSource(FEED_URL)

    rows = [
        row
        for company in _representative_companies()
        for row in source.parse(payload, company)
    ]

    assert [row["company"] for row in rows] == list(VARIANTS)


def test_markdown_matches_the_same_representative_variants_and_rejects_similar_names():
    labels = (*VARIANTS, "Armstrong", "Amazon Studios", "Oracle Health")
    body = "\n".join(
        f"| {label} | Software Engineer Intern | United States | "
        f"[Apply](https://example.test/jobs/{index}) | 2026-08-04 |"
        for index, label in enumerate(labels)
    )
    markdown = (
        "| Company | Role | Location | Apply | Added |\n"
        "| --- | --- | --- | --- | --- |\n"
        f"{body}\n"
    )
    source = GitHubMarkdownTableSource(
        FEED_URL,
        source_name="fixture",
        default_term="Summer 2027",
    )

    rows = source.parse(markdown, _representative_companies())

    assert [row["company"] for row in rows] == list(VARIANTS)


@pytest.mark.parametrize(
    ("label", "canonical"),
    [
        ("Amazon Web Services", "Amazon"),
        ("AWS", "Amazon"),
        ("Ørsted", "Orsted"),
        ("KPMG LLP", "KPMG"),
        ("KPMG US LLP", "KPMG"),
        ("Ernst & Young LLP", "EY"),
        ("Ernst and Young LLP", "EY"),
        ("Goldman Sachs Group", "Goldman Sachs"),
        ("Goldman Sachs Group Inc", "Goldman Sachs"),
        ("JPMorgan Chase & Co.", "JPMorgan Chase"),
        ("JPMorgan Chase and Co.", "JPMorgan Chase"),
        ("UBS Group", "UBS"),
        ("UBS Group AG", "UBS"),
        ("Nomura Holdings", "Nomura"),
        ("Nomura Holdings Inc", "Nomura"),
        ("Federal Reserve Bank of NY", "Federal Reserve Bank of New York"),
        ("Carlyle Group", "The Carlyle Group"),
        ("Moelis and Company", "Moelis & Company"),
        ("Bain and Company", "Bain & Company"),
        ("Air Products & Chemicals", "Air Products"),
        ("Uber Technologies", "Uber"),
        ("ASML Holding", "ASML"),
        ("ASML Holding NV", "ASML"),
        ("PayPal Holdings", "PayPal"),
        ("Hewlett Packard (HP)", "HP"),
    ],
)
def test_default_watchlist_covers_required_exact_variants(label, canonical):
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)

    matched = match_watchlist_company(label, config.companies)

    assert matched is not None
    assert matched.name == canonical


@pytest.mark.parametrize(
    "label",
    [
        "Armstrong",
        "Compass Group",
        "American Express Global Business Travel",
        "Amazon Studios",
        "Oracle Health",
    ],
)
def test_default_watchlist_does_not_claim_similar_unconfigured_companies(label):
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)

    assert match_watchlist_company(label, config.companies) is None


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # Epic Games is configured in its own right, so the watchlist must
        # resolve it and Epic to different companies rather than conflating them.
        ("Epic Games", "Epic Games"),
        ("Epic", "Epic"),
        ("Riot Games", "Riot Games"),
        ("Square", "Block"),
        # xAI shares Musk-entity recruiting branding with SpaceX but is a
        # separate Greenhouse board with a disjoint posting set.
        ("xAI", "xAI"),
        ("X Corp", "xAI"),
        ("SpaceX", "SpaceX"),
        ("Mistral", "Mistral AI"),
        ("Intel", "Intel"),
        ("Intel Corporation", "Intel"),
        ("NXP", "NXP"),
        ("NXP Semiconductors", "NXP"),
        ("KLA", "KLA"),
        ("KLA Corporation", "KLA"),
        ("Snap", "Snap"),
        ("Snapchat", "Snap"),
        ("Cisco", "Cisco"),
        ("Cisco Systems", "Cisco"),
        ("eBay", "eBay"),
        ("Snowflake", "Snowflake"),
        # Pure Storage, Inc. continued as Everpure; both names resolve to the
        # single configured company rather than creating a duplicate entry.
        ("Everpure", "Everpure"),
        ("Pure Storage", "Everpure"),
        ("Pure Storage, Inc.", "Everpure"),
        ("Akamai", "Akamai"),
        ("Akamai Technologies", "Akamai"),
        ("Shopify", "Shopify"),
        ("Shopify Inc.", "Shopify"),
        ("Zoom", "Zoom"),
        ("Zoom Video Communications", "Zoom"),
        ("HPE", "HPE"),
        ("Hewlett Packard Enterprise", "HPE"),
        ("Samsung Electronics", "Samsung Electronics"),
        ("Samsung", "Samsung Electronics"),
        ("Nvidia", "Nvidia"),
        ("NVIDIA Corporation", "Nvidia"),
        ("Broadcom Inc.", "Broadcom"),
        ("Unity Technologies", "Unity"),
        ("GlobalFoundries Inc.", "GlobalFoundries"),
        ("Applied Materials Inc.", "Applied Materials"),
        ("Synopsys, Inc.", "Synopsys"),
        ("Taiwan Semiconductor Manufacturing Company", "TSMC"),
        ("Box Inc.", "Box"),
        ("DigitalOcean Holdings", "DigitalOcean"),
        ("Baidu", "Baidu USA"),
    ],
)
def test_similarly_named_configured_companies_resolve_distinctly(label, expected):
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)

    matched = match_watchlist_company(label, config.companies)

    assert matched is not None
    assert matched.name == expected
