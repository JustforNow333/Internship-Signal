"""Fourth finance-employer expansion batch: reuse-first source contracts."""

from __future__ import annotations

import pytest

from watcher.company_matching import company_matching_key
from watcher.config import load_watchlist
from watcher.sources.greenhouse import GreenhouseSource
from watcher.sources.registry import DIRECT_ATS, build_direct_sources
from watcher.sources.workday import WorkdaySource
from watcher.tests.tech_universe import (
    TECH_UNIVERSE_COMPANY_NAMES,
    assert_batch_is_additive,
)


AUDITED_BATCH_COMPANIES = (
    "G-Research",
    "Man Group",
    "Qube Research & Technologies",
    "Squarepoint Capital",
    "Maven Securities",
    "Flow Traders",
    "Mako Trading",
    "Marshall Wace",
)

GREENHOUSE_BATCH_CONFIG = {
    "Man Group": (
        "mangroup",
        "https://www.man.com/careers",
    ),
    "Qube Research & Technologies": (
        "quberesearchandtechnologies",
        "https://www.qube-rt.com/careers/",
    ),
    "Squarepoint Capital": (
        "squarepointcapital",
        "https://www.squarepoint-capital.com/open-opportunities",
    ),
    "Maven Securities": (
        "mavensecuritiesholdingltd",
        "https://www.mavensecurities.com/jobs/",
    ),
    "Flow Traders": (
        "flowtraders",
        "https://www.flowtraders.com/careers/job-search/",
    ),
    "Mako Trading": (
        "mako",
        "https://www.mako.com/opportunities",
    ),
    "Marshall Wace": (
        "marshallwace",
        "https://www.mwam.com/join-us/",
    ),
}

WORKDAY_BATCH_CONFIG = {
    "G-Research": (
        "gresearch",
        "wd103",
        "G-Research",
        "https://www.gresearch.com/vacancies/",
    ),
}

MARSHALL_WACE_GREENHOUSE_TOKENS = (
    "marshallwace",
    "mw-tech-grad",
    "mwinternshipprogram",
    "mw-early-career-juniors",
)


@pytest.fixture(scope="module")
def watchlist():
    return load_watchlist()


def company(watchlist, name: str):
    return next(c for c in watchlist.companies if c.name == name)


def test_batch_is_disjoint_from_the_tech_universe_and_additive(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}

    assert not set(AUDITED_BATCH_COMPANIES) & TECH_UNIVERSE_COMPANY_NAMES
    assert (
        set(GREENHOUSE_BATCH_CONFIG)
        | set(WORKDAY_BATCH_CONFIG)
    ) == set(
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
def test_direct_companies_use_the_published_greenhouse_board(
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


def test_marshall_wace_composes_the_published_employment_boards(watchlist):
    cfg = company(watchlist, "Marshall Wace")

    assert tuple(cfg.greenhouse_tokens) == MARSHALL_WACE_GREENHOUSE_TOKENS
    assert [GreenhouseSource.endpoint(token) for token in cfg.greenhouse_tokens] == [
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
        for token in MARSHALL_WACE_GREENHOUSE_TOKENS
    ]


@pytest.mark.parametrize(
    ("name", "token", "shard", "site", "source_url"),
    [
        (name, *values)
        for name, values in sorted(WORKDAY_BATCH_CONFIG.items())
    ],
)
def test_workday_companies_use_the_published_board(
    watchlist, name, token, shard, site, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "workday"
    assert cfg.token == token
    assert cfg.workday_shard == shard
    assert cfg.workday_site == site
    assert cfg.source_url == source_url
    assert "workday" in DIRECT_ATS
    assert build_direct_sources()["workday"] is not None
    assert WorkdaySource.endpoint(token, shard, site) == (
        f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/"
        f"{token}/{site}/jobs"
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
