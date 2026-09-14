"""Fifth finance-employer expansion batch: reuse-first source contracts."""

from __future__ import annotations

import pytest

from watcher.company_matching import company_matching_key
from watcher.config import load_watchlist
from watcher.sources.ashby import AshbySource
from watcher.sources.greenhouse import GreenhouseSource
from watcher.sources.registry import DIRECT_ATS, build_direct_sources
from watcher.tests.tech_universe import (
    TECH_UNIVERSE_COMPANY_NAMES,
    assert_batch_is_additive,
)


AUDITED_BATCH_COMPANIES = (
    "Virtu Financial",
    "Bridgewater Associates",
    "Schonfeld Strategic Advisors",
    "WorldQuant",
    "Aquatic Capital Management",
    "The Voleon Group",
)

GREENHOUSE_BATCH_CONFIG = {
    "Virtu Financial": (
        "virtu",
        "https://www.virtu.com/careers/",
    ),
    "Bridgewater Associates": (
        "bridgewater89",
        "https://www.bridgewater.com/working-at-bridgewater/job-openings",
    ),
    "Schonfeld Strategic Advisors": (
        "schonfeld",
        "https://www.schonfeld.com/careers/opportunities/",
    ),
    "WorldQuant": (
        "worldquant",
        "https://www.worldquant.com/career-listing/",
    ),
    "Aquatic Capital Management": (
        "aquaticcapitalmanagement",
        "https://aquatic.com/",
    ),
}

ASHBY_BATCH_CONFIG = {
    "The Voleon Group": (
        "voleon",
        "https://voleon.com/jobs/",
    ),
}


@pytest.fixture(scope="module")
def watchlist():
    return load_watchlist()


def company(watchlist, name: str):
    return next(c for c in watchlist.companies if c.name == name)


def test_batch_is_disjoint_from_the_tech_universe_and_additive(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}

    assert not set(AUDITED_BATCH_COMPANIES) & TECH_UNIVERSE_COMPANY_NAMES
    assert set(GREENHOUSE_BATCH_CONFIG) | set(ASHBY_BATCH_CONFIG) == set(
        AUDITED_BATCH_COMPANIES
    )
    assert_batch_is_additive(AUDITED_BATCH_COMPANIES, configured_names)


@pytest.mark.parametrize(
    ("name", "token", "source_url"),
    [
        (name, *values)
        for name, values in sorted(GREENHOUSE_BATCH_CONFIG.items())
    ],
)
def test_greenhouse_companies_use_the_published_board(
    watchlist, name, token, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "greenhouse"
    assert cfg.token == token
    assert cfg.source_url == source_url
    assert "greenhouse" in DIRECT_ATS
    assert build_direct_sources()["greenhouse"] is not None
    assert GreenhouseSource.endpoint(token) == (
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    )


@pytest.mark.parametrize(
    ("name", "token", "source_url"),
    [
        (name, *values)
        for name, values in sorted(ASHBY_BATCH_CONFIG.items())
    ],
)
def test_ashby_companies_use_the_published_board(
    watchlist, name, token, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "ashby"
    assert cfg.token == token
    assert cfg.source_url == source_url
    assert "ashby" in DIRECT_ATS
    assert build_direct_sources()["ashby"] is not None
    assert AshbySource.endpoint(token) == (
        f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    )


def test_batch_names_and_aliases_do_not_collide_with_the_watchlist(watchlist):
    owners: dict[str, set[str]] = {}
    for cfg in watchlist.companies:
        for label in (cfg.name, *cfg.aliases):
            owners.setdefault(company_matching_key(label), set()).add(cfg.name)

    assert {key: names for key, names in owners.items() if len(names) > 1} == {}

    for name in AUDITED_BATCH_COMPANIES:
        cfg = company(watchlist, name)
        for label in (cfg.name, *cfg.aliases):
            assert owners[company_matching_key(label)] == {name}
