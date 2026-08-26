"""Public company catalog derived from watcher configuration."""

from __future__ import annotations

import pytest
from app.hosted.catalog import CompanyCatalog

from watcher.config import NON_DIRECT_ATS, CompanyCfg, WatcherConfig
from watcher.sources.registry import DIRECT_ATS


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
    """A registered direct adapter checks the employer's own source.

    Regression: the catalog carried its own hardcoded ATS list, so the eight
    adapters registered after it was written (bain, epic, ibm, icims,
    oracle_hcm, paylocity, successfactors, talentbrew) were reported to users
    as backstop-only even though the watcher collects them directly.
    """

    catalog = _catalog(CompanyCfg(name="Example Co", ats=ats))

    assert catalog.companies[0].coverage == "direct"
    assert catalog.companies[0].selectable is True


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
    """The catalog must not keep a second, drifting list of direct adapters."""

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
