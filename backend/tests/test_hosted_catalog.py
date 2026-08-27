"""Public company catalog coverage derived from watcher configuration."""

from app.hosted.catalog import CompanyCatalog


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
    "Aon",
    "Analysis Group",
    "General Dynamics Electric Boat",
    "JHU Applied Physics Laboratory",
    "ZS",
}

SUCCESSFACTORS_DIRECT_COMPANIES = {
    "EY",
    "Exxon Mobil",
    "MIT Lincoln Laboratory",
    "Nomura",
    "Vaisala",
}

BAIN_IBM_EPIC_DIRECT_COMPANIES = {
    "Bain & Company",
    "Epic",
    "IBM",
}

PAYLOCITY_DIRECT_COMPANIES = {"Procure Analytics"}


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
