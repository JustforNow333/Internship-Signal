"""Ninth expansion batch: the final two original Tier 2 companies."""

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
    "Chewy",
    "The New York Times",
)

WORKDAY_BATCH_CONFIG = {
    "Chewy": (
        "chewy",
        "wd5",
        "External",
        "site",
        "https://wd5.myworkdaysite.com/recruiting/chewy/External",
    ),
}

GREENHOUSE_BATCH_CONFIG = {
    "The New York Times": (
        "thenewyorktimes",
        "https://www.nytco.com/careers/job-listings/",
    ),
}

FALLBACK_BATCH_COMPANIES: tuple[str, ...] = ()
UNCOVERED_BATCH_COMPANIES: tuple[str, ...] = ()


def configured_batch_names() -> set[str]:
    return (
        set(WORKDAY_BATCH_CONFIG)
        | set(GREENHOUSE_BATCH_CONFIG)
        | set(FALLBACK_BATCH_COMPANIES)
    )


@pytest.fixture(scope="module")
def watchlist():
    return load_watchlist()


def company(watchlist, name: str):
    return next(c for c in watchlist.companies if c.name == name)


def test_batch_is_disjoint_from_the_tech_universe_and_additive(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}

    assert set(AUDITED_BATCH_COMPANIES).isdisjoint(TECH_UNIVERSE_COMPANY_NAMES)
    assert_batch_is_additive(tuple(configured_batch_names()), configured_names)
    assert set(UNCOVERED_BATCH_COMPANIES).isdisjoint(configured_names)


def test_every_audited_company_has_exactly_one_outcome():
    covered = configured_batch_names()

    assert covered.isdisjoint(UNCOVERED_BATCH_COMPANIES)
    assert covered | set(UNCOVERED_BATCH_COMPANIES) == set(AUDITED_BATCH_COMPANIES)
    assert set(WORKDAY_BATCH_CONFIG).isdisjoint(GREENHOUSE_BATCH_CONFIG)


@pytest.mark.parametrize(
    ("name", "token", "shard", "site", "host_variant", "source_url"),
    [(name, *values) for name, values in sorted(WORKDAY_BATCH_CONFIG.items())],
)
def test_workday_companies_use_first_party_published_tenants(
    watchlist, name, token, shard, site, host_variant, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "workday"
    assert (cfg.token, cfg.workday_shard, cfg.workday_site) == (token, shard, site)
    assert cfg.workday_host_variant == host_variant
    assert cfg.workday_detail_policy == "internship_candidates"
    assert cfg.source_url == source_url
    assert WorkdaySource.api_host(token, shard, host_variant) == (
        f"{shard}.myworkdaysite.com"
    )
    assert WorkdaySource.endpoint(token, shard, site, host_variant) == (
        f"https://{shard}.myworkdaysite.com/wday/cxs/{token}/{site}/jobs"
    )
    assert cfg.ats in DIRECT_ATS


@pytest.mark.parametrize(
    ("name", "token", "source_url"),
    [(name, *values) for name, values in sorted(GREENHOUSE_BATCH_CONFIG.items())],
)
def test_greenhouse_companies_use_first_party_published_boards(
    watchlist, name, token, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "greenhouse"
    assert cfg.token == token
    assert cfg.source_url == source_url
    assert GreenhouseSource.endpoint(token) == (
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    )
    assert cfg.ats in DIRECT_ATS


@pytest.mark.parametrize("name", sorted(configured_batch_names()))
def test_direct_batch_companies_build_from_the_registry(watchlist, name):
    cfg = company(watchlist, name)

    assert build_direct_sources()[cfg.ats] is not None


def test_batch_names_and_aliases_do_not_collide_with_watchlist_identities(watchlist):
    owners: dict[str, set[str]] = {}
    for cfg in watchlist.companies:
        for label in (cfg.name, *cfg.aliases):
            owners.setdefault(company_matching_key(label), set()).add(cfg.name)

    assert {key: names for key, names in owners.items() if len(names) > 1} == {}

    for name in configured_batch_names():
        cfg = company(watchlist, name)
        for label in (cfg.name, *cfg.aliases):
            assert owners[company_matching_key(label)] == {name}


def test_new_york_times_owns_its_common_short_identities(watchlist):
    cfg = company(watchlist, "The New York Times")

    for label in ("New York Times", "NYT", "The New York Times Company"):
        assert label in cfg.aliases


def test_batch_does_not_pin_a_global_watchlist_total(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}

    assert configured_batch_names() <= configured_names
    assert len(configured_names) >= len(
        TECH_UNIVERSE_COMPANY_NAMES | configured_batch_names()
    )
