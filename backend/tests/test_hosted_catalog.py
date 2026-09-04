"""Public company catalog coverage derived from watcher configuration."""

from __future__ import annotations

import pytest
from app.hosted.catalog import CompanyCatalog

from watcher.config import NON_DIRECT_ATS, CompanyCfg, WatcherConfig
from watcher.sources.registry import DIRECT_ATS


WAVE_ONE_DIRECT_COMPANIES = {
    "American Express",
    "BlackLine",
    "Brookfield",
    "Compass",
    "Federal Reserve Bank of New York",
    "Goldman Sachs",
    "Halo Industries",
    "Oracle",
    "Sixth Street",
    "The Carlyle Group",
    "Uber",
    "Whatnot",
}

ICIMS_DIRECT_COMPANIES = {
    "AMD",
    "Aon",
    "Analysis Group",
    "Docusign",
    "General Dynamics Electric Boat",
    "GitHub",
    "JHU Applied Physics Laboratory",
    "ZS",
}

VERIFIED_CONFIG_EXPANSION_COMPANIES = {
    "AMD",
    "Analog Devices",
    "Applied Materials",
    "Autodesk",
    "Box",
    "Broadcom",
    "Cadence",
    "CrowdStrike",
    "DigitalOcean",
    "Docusign",
    "Fortinet",
    "GitHub",
    "GlobalFoundries",
    "Marvell",
    "Nvidia",
    "Palo Alto Networks",
    "TSMC",
    "Texas Instruments",
    "Unity",
}

SUCCESSFACTORS_DIRECT_COMPANIES = {
    "EY",
    "Exxon Mobil",
    "MIT Lincoln Laboratory",
    "Nomura",
    "TSMC",
    "Vaisala",
}

BAIN_IBM_EPIC_DIRECT_COMPANIES = {
    "Bain & Company",
    "Bloomberg",
    "Epic",
    "IBM",
}

PAYLOCITY_DIRECT_COMPANIES = {"Procure Analytics"}


def _catalog(*companies: CompanyCfg, backstop: bool = True) -> CompanyCatalog:
    return CompanyCatalog.from_watcher_config(
        WatcherConfig(
            companies=tuple(companies),
            github_listing_urls=(
                ("https://example.test/listings.json",) if backstop else ()
            ),
        )
    )


@pytest.mark.parametrize("ats", sorted(DIRECT_ATS))
def test_every_registered_direct_adapter_reports_direct_coverage(ats: str) -> None:
    catalog = _catalog(CompanyCfg(name="Example Co", ats=ats))

    assert catalog.companies[0].coverage == "direct"
    assert catalog.companies[0].selectable is True


@pytest.mark.parametrize("name", ["Barclays", "Synopsys"])
def test_talentbrew_reports_direct_coverage(name: str) -> None:
    catalog = CompanyCatalog.from_watcher_config()
    talentbrew = next(
        company
        for company in catalog.companies
        if company.name == name
    )

    assert talentbrew.coverage == "direct"
    assert talentbrew.selectable is True


@pytest.mark.parametrize("ats", sorted(NON_DIRECT_ATS))
def test_configuration_only_modes_report_backstop_coverage(ats: str) -> None:
    catalog = _catalog(CompanyCfg(name="Example Co", ats=ats))

    assert catalog.companies[0].coverage == "backstop"
    assert catalog.companies[0].selectable is True


@pytest.mark.parametrize("ats", sorted(NON_DIRECT_ATS))
def test_backstop_only_companies_are_unselectable_without_a_feed(ats: str) -> None:
    catalog = _catalog(CompanyCfg(name="Example Co", ats=ats), backstop=False)

    assert catalog.companies[0].selectable is False


def test_direct_coverage_tracks_the_canonical_source_registry() -> None:
    companies = tuple(
        CompanyCfg(name=f"Company {ats}", ats=ats)
        for ats in sorted(DIRECT_ATS | NON_DIRECT_ATS)
    )
    catalog = _catalog(*companies)
    direct = {
        company.name.removeprefix("Company ")
        for company in catalog.companies
        if company.coverage == "direct"
    }

    assert direct == set(DIRECT_ATS)


def test_wave_one_sources_are_exposed_as_direct_hosted_catalog_coverage():
    catalog = CompanyCatalog.from_watcher_config()
    companies_by_name = {company.name: company for company in catalog.companies}

    for name in WAVE_ONE_DIRECT_COMPANIES:
        company = companies_by_name[name]
        assert company.coverage == "direct"
        assert company.selectable is True


def test_icims_sources_are_exposed_as_direct_hosted_catalog_coverage():
    catalog = CompanyCatalog.from_watcher_config()
    companies_by_name = {company.name: company for company in catalog.companies}

    for name in ICIMS_DIRECT_COMPANIES:
        company = companies_by_name[name]
        assert company.coverage == "direct"
        assert company.selectable is True


def test_verified_config_expansion_is_exposed_as_direct_hosted_catalog_coverage():
    catalog = CompanyCatalog.from_watcher_config()
    companies_by_name = {company.name: company for company in catalog.companies}

    for name in VERIFIED_CONFIG_EXPANSION_COMPANIES:
        company = companies_by_name[name]
        assert company.coverage == "direct"
        assert company.selectable is True


def test_successfactors_sources_are_exposed_as_direct_hosted_catalog_coverage():
    catalog = CompanyCatalog.from_watcher_config()
    companies_by_name = {company.name: company for company in catalog.companies}

    for name in SUCCESSFACTORS_DIRECT_COMPANIES:
        company = companies_by_name[name]
        assert company.coverage == "direct"
        assert company.selectable is True


def test_bain_ibm_epic_sources_are_exposed_as_direct_hosted_catalog_coverage():
    catalog = CompanyCatalog.from_watcher_config()
    companies_by_name = {company.name: company for company in catalog.companies}

    for name in BAIN_IBM_EPIC_DIRECT_COMPANIES:
        company = companies_by_name[name]
        assert company.coverage == "direct"
        assert company.selectable is True


def test_paylocity_sources_are_exposed_as_direct_hosted_catalog_coverage():
    catalog = CompanyCatalog.from_watcher_config()
    companies_by_name = {company.name: company for company in catalog.companies}

    for name in PAYLOCITY_DIRECT_COMPANIES:
        company = companies_by_name[name]
        assert company.coverage == "direct"
        assert company.selectable is True

    assert catalog.resolve("procutre-analytics") == companies_by_name["Procure Analytics"]
