"""Seventh finance-employer expansion batch: markets and asset managers."""

from __future__ import annotations

import pytest

from watcher.company_matching import company_matching_key, company_matches
from watcher.config import load_watchlist
from watcher.sources.greenhouse import GreenhouseSource
from watcher.sources.icims import IcimsSource
from watcher.sources.registry import DIRECT_ATS, build_direct_sources
from watcher.sources.talentbrew import TalentBrewSource
from watcher.sources.workday import WorkdaySource
from watcher.tests.tech_universe import (
    TECH_UNIVERSE_COMPANY_NAMES,
    assert_batch_is_additive,
)


AUDITED_BATCH_COMPANIES = (
    "KKR",
    "PIMCO",
    "S&P Global",
    "Moody's",
    "Nasdaq",
    "Cboe Global Markets",
    "Intercontinental Exchange (ICE)",
    "CME Group",
)

GREENHOUSE_BATCH_CONFIG = {
    "KKR": (
        "stage",
        "https://www.kkr.com/careers/career-opportunities",
    ),
}

WORKDAY_BATCH_CONFIG = {
    "CME Group": (
        "cmegroup",
        "wd1",
        "cme_careers",
        "https://www.cmegroup.com/careers.html",
    ),
    "Cboe Global Markets": (
        "cboe",
        "wd1",
        "External_Career_CBOE",
        "https://careers.cboe.com/us/en/search-results",
    ),
    "Nasdaq": (
        "nasdaq",
        "wd1",
        "Global_External_Site",
        "https://www.nasdaq.com/about/careers",
    ),
    "PIMCO": (
        "pimco",
        "wd1",
        "pimco-careers",
        "https://www.pimco.com/us/en/about-us/careers",
    ),
}

ICIMS_BATCH_CONFIG = {
    "Intercontinental Exchange (ICE)": (
        "jibe_derived_url",
        "careers.ice.com",
        "https://careers.ice.com/jobs",
    ),
}

TALENTBREW_BATCH_CONFIG = {
    "Moody's": (
        "careers.moodys.com",
        "49841",
        "9383648",
        "Students & Early Careers",
        "https://careers.moodys.com/en/search-jobs",
    ),
}

FALLBACK_BATCH_COMPANIES = ("S&P Global",)
CURRENT_FEED_LABELS = (("S&P Global", "S&P Global", "simplify"),)
UNCOVERED_BATCH_COMPANIES: tuple[str, ...] = ()


@pytest.fixture(scope="module")
def watchlist():
    return load_watchlist()


def company(watchlist, name: str):
    return next(c for c in watchlist.companies if c.name == name)


def test_batch_is_disjoint_from_the_tech_universe_and_additive(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}
    configured_batch = (
        set(GREENHOUSE_BATCH_CONFIG)
        | set(WORKDAY_BATCH_CONFIG)
        | set(ICIMS_BATCH_CONFIG)
        | set(TALENTBREW_BATCH_CONFIG)
        | set(FALLBACK_BATCH_COMPANIES)
    )

    assert set(AUDITED_BATCH_COMPANIES).isdisjoint(TECH_UNIVERSE_COMPANY_NAMES)
    assert_batch_is_additive(configured_batch, configured_names)
    assert set(UNCOVERED_BATCH_COMPANIES).isdisjoint(configured_names)


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


@pytest.mark.parametrize(
    ("name", "token", "shard", "site", "source_url"),
    [(name, *values) for name, values in sorted(WORKDAY_BATCH_CONFIG.items())],
)
def test_workday_companies_use_first_party_published_tenants(
    watchlist, name, token, shard, site, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "workday"
    assert (cfg.token, cfg.workday_shard, cfg.workday_site) == (token, shard, site)
    assert cfg.workday_detail_policy == "internship_candidates"
    assert cfg.workday_host_variant == "jobs"
    assert cfg.source_url == source_url
    assert WorkdaySource.endpoint(token, shard, site) == (
        f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/"
        f"{token}/{site}/jobs"
    )
    assert cfg.ats in DIRECT_ATS


@pytest.mark.parametrize(
    ("name", "variant", "host", "source_url"),
    [(name, *values) for name, values in sorted(ICIMS_BATCH_CONFIG.items())],
)
def test_icims_companies_use_their_authoritative_jibe_inventory(
    watchlist, name, variant, host, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "icims"
    assert cfg.icims_variant == variant
    assert cfg.icims_host == host
    assert tuple(cfg.icims_portals) == ()
    assert cfg.source_url == source_url
    assert IcimsSource.jibe_endpoint(host, limit=100, page=1) == (
        f"https://{host}/api/jobs?limit=100&page=1"
    )
    assert cfg.ats in DIRECT_ATS


@pytest.mark.parametrize(
    ("name", "host", "site_id", "category_id", "category_name", "source_url"),
    [
        (name, *values)
        for name, values in sorted(TALENTBREW_BATCH_CONFIG.items())
    ],
)
def test_talentbrew_companies_pin_the_published_early_career_category(
    watchlist, name, host, site_id, category_id, category_name, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "talentbrew"
    assert (
        cfg.talentbrew_host,
        cfg.talentbrew_site_id,
        cfg.talentbrew_category_id,
        cfg.talentbrew_category_name,
    ) == (host, site_id, category_id, category_name)
    assert cfg.source_url == source_url
    assert host in TalentBrewSource.search_endpoint(cfg, 1, 16)
    assert f"ID={category_id}" in TalentBrewSource.search_endpoint(cfg, 1, 16)
    assert cfg.ats in DIRECT_ATS


@pytest.mark.parametrize(
    "name",
    sorted(
        set(GREENHOUSE_BATCH_CONFIG)
        | set(WORKDAY_BATCH_CONFIG)
        | set(ICIMS_BATCH_CONFIG)
        | set(TALENTBREW_BATCH_CONFIG)
    ),
)
def test_direct_batch_companies_build_from_the_registry(watchlist, name):
    cfg = company(watchlist, name)

    assert build_direct_sources()[cfg.ats] is not None


@pytest.mark.parametrize("name", FALLBACK_BATCH_COMPANIES)
def test_fallback_companies_are_backstop_only(watchlist, name):
    cfg = company(watchlist, name)

    assert cfg.ats == "github_only"
    assert cfg.ats not in DIRECT_ATS
    assert not cfg.token
    assert not cfg.source_url


@pytest.mark.parametrize(("feed_label", "name", "feed_name"), CURRENT_FEED_LABELS)
def test_fallback_companies_match_current_feed_labels(
    watchlist, feed_label, name, feed_name
):
    cfg = company(watchlist, name)

    assert feed_name == "simplify"
    assert company_matches(feed_label, cfg)


def test_no_batch_company_remains_uncovered(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}
    configured_batch = (
        set(GREENHOUSE_BATCH_CONFIG)
        | set(WORKDAY_BATCH_CONFIG)
        | set(ICIMS_BATCH_CONFIG)
        | set(TALENTBREW_BATCH_CONFIG)
        | set(FALLBACK_BATCH_COMPANIES)
    )

    assert UNCOVERED_BATCH_COMPANIES == ()
    assert configured_batch == set(AUDITED_BATCH_COMPANIES)
    assert configured_batch <= configured_names


def test_uncovered_company_is_not_accidentally_claimed(watchlist):
    owners = {
        company_matching_key(label)
        for cfg in watchlist.companies
        for label in (cfg.name, *cfg.aliases)
    }

    for name in UNCOVERED_BATCH_COMPANIES:
        assert company_matching_key(name) not in owners


def test_batch_names_and_aliases_do_not_collide_with_watchlist_identities(watchlist):
    owners: dict[str, set[str]] = {}
    for cfg in watchlist.companies:
        for label in (cfg.name, *cfg.aliases):
            owners.setdefault(company_matching_key(label), set()).add(cfg.name)

    assert {key: names for key, names in owners.items() if len(names) > 1} == {}

    configured_batch = (
        set(GREENHOUSE_BATCH_CONFIG)
        | set(WORKDAY_BATCH_CONFIG)
        | set(ICIMS_BATCH_CONFIG)
        | set(TALENTBREW_BATCH_CONFIG)
        | set(FALLBACK_BATCH_COMPANIES)
    )
    for name in configured_batch:
        cfg = company(watchlist, name)
        for label in (cfg.name, *cfg.aliases):
            assert owners[company_matching_key(label)] == {name}
