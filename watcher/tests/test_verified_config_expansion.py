"""Exact config-only coverage added after bounded live source verification."""

from watcher.config import DEFAULT_WATCHLIST_PATH, load_watchlist
from watcher.sources.icims import IcimsSource
from watcher.sources.oracle_hcm import OracleHcmSource
from watcher.sources.registry import build_direct_sources
from watcher.sources.workday import WorkdaySource


EXPECTED_CONFIG = {
    "Autodesk": ("workday", "autodesk", "wd1", "Ext"),
    "Palo Alto Networks": (
        "workday",
        "paloaltonetworks",
        "wd5",
        "panwexternalcareers",
    ),
    "CrowdStrike": ("workday", "crowdstrike", "wd5", "crowdstrikecareers"),
    "Analog Devices": ("workday", "analogdevices", "wd1", "External"),
    "Cadence": ("workday", "cadence", "wd1", "External_Careers"),
    "Marvell": ("workday", "marvell", "wd1", "MarvellCareers"),
    "Intel": ("workday", "intel", "wd1", "External"),
    "NXP": ("workday", "nxp", "wd3", "careers"),
    "KLA": ("workday", "kla", "wd1", "Search"),
    "Snap": ("workday", "snapchat", "wd1", "snap"),
    "Cisco": ("workday", "cisco", "wd5", "Cisco_Careers"),
    "eBay": ("workday", "ebay", "wd5", "apply"),
    "AMD": ("icims", "jibe_json", "careers.amd.com"),
    "GitHub": ("icims", "jibe_json", "githubinc.jibeapply.com"),
    "Docusign": ("icims", "jibe_json", "careers.docusign.com"),
    "Fortinet": ("oracle_hcm", "edel.fa.us2.oraclecloud.com", "CX_2001"),
    "Texas Instruments": ("oracle_hcm", "edbz.fa.us2.oraclecloud.com", "CX"),
}


def test_verified_expansion_loads_exact_configs_and_registered_sources():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    companies = {company.name: company for company in config.companies}
    sources = build_direct_sources()

    assert len(companies) == len(config.companies)
    assert isinstance(sources["workday"], WorkdaySource)
    assert isinstance(sources["icims"], IcimsSource)
    assert isinstance(sources["oracle_hcm"], OracleHcmSource)

    for name, expected in EXPECTED_CONFIG.items():
        company = companies[name]
        assert company.ats == expected[0]
        assert company.source_url.startswith("https://")
        if company.ats == "workday":
            assert (
                company.ats,
                company.token,
                company.workday_shard,
                company.workday_site,
            ) == expected
        elif company.ats == "icims":
            assert (
                company.ats,
                company.icims_variant,
                company.icims_host,
            ) == expected
        else:
            assert (
                company.ats,
                company.oracle_hcm_host,
                company.oracle_hcm_site,
            ) == expected
