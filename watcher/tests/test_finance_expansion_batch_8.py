"""Eighth expansion batch: large retail, consumer, and consulting employers."""

from __future__ import annotations

import pytest

from watcher.company_matching import company_matching_key, company_matches
from watcher.config import load_watchlist
from watcher.sources.registry import DIRECT_ATS, build_direct_sources
from watcher.sources.talentbrew import TalentBrewSource
from watcher.sources.workday import WorkdaySource
from watcher.tests.tech_universe import (
    TECH_UNIVERSE_COMPANY_NAMES,
    assert_batch_is_additive,
)


AUDITED_BATCH_COMPANIES = (
    "Walmart",
    "Disney",
    "Nike",
    "McKinsey & Company",
    "Boston Consulting Group (BCG)",
    "Target",
    "Home Depot",
    "Lowe's",
)

WORKDAY_BATCH_CONFIG = {
    "Home Depot": (
        "homedepot",
        "wd5",
        "CareerDepot",
        "https://careers.homedepot.com/",
    ),
    "Nike": (
        "nike",
        "wd1",
        "nke",
        "https://careers.nike.com/",
    ),
}

TALENTBREW_BATCH_CONFIG = {
    "Disney": (
        "www.disneycareers.com",
        "391",
        "26715",
        "Technology",
        "https://www.disneycareers.com/search-jobs",
    ),
}

FALLBACK_BATCH_COMPANIES = ("Walmart", "Target", "Lowe's")
CURRENT_FEED_LABELS = (
    ("Walmart", "Walmart", "simplify"),
    ("Target", "Target", "simplify"),
    ("Lowe's", "Lowe's", "simplify"),
)
UNCOVERED_BATCH_COMPANIES = (
    "Boston Consulting Group (BCG)",
    "McKinsey & Company",
)


def configured_batch_names() -> set[str]:
    return (
        set(WORKDAY_BATCH_CONFIG)
        | set(TALENTBREW_BATCH_CONFIG)
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
    assert set(WORKDAY_BATCH_CONFIG).isdisjoint(TALENTBREW_BATCH_CONFIG)
    assert set(FALLBACK_BATCH_COMPANIES).isdisjoint(
        set(WORKDAY_BATCH_CONFIG) | set(TALENTBREW_BATCH_CONFIG)
    )


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
        f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/jobs"
    )
    assert cfg.ats in DIRECT_ATS


@pytest.mark.parametrize(
    ("name", "host", "site_id", "category_id", "category_name", "source_url"),
    [(name, *values) for name, values in sorted(TALENTBREW_BATCH_CONFIG.items())],
)
def test_talentbrew_companies_pin_a_published_category_facet(
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
    endpoint = TalentBrewSource.search_endpoint(cfg, 1, 16)
    assert endpoint.startswith(f"https://{host}/search-jobs/results?")
    assert f"ID={category_id}" in endpoint
    assert cfg.ats in DIRECT_ATS


@pytest.mark.parametrize(
    "name", sorted(set(WORKDAY_BATCH_CONFIG) | set(TALENTBREW_BATCH_CONFIG))
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


def test_uncovered_companies_are_not_accidentally_claimed(watchlist):
    owners = {
        company_matching_key(label)
        for cfg in watchlist.companies
        for label in (cfg.name, *cfg.aliases)
    }

    for name in UNCOVERED_BATCH_COMPANIES:
        assert company_matching_key(name) not in owners

    assert company_matching_key("McKinsey") not in owners
    assert company_matching_key("Boston Consulting Group") not in owners
    assert company_matching_key("BCG") not in owners


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


def test_batch_does_not_pin_a_global_watchlist_total(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}

    assert configured_batch_names() <= configured_names
    assert len(configured_names) >= len(
        TECH_UNIVERSE_COMPANY_NAMES | configured_batch_names()
    )
