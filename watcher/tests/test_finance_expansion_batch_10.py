"""Tenth expansion batch: automotive and heavy-equipment manufacturers."""

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
    "General Motors",
    "Ford",
    "Rivian",
    "Lucid Motors",
    "Toyota",
    "BMW",
    "Mercedes-Benz",
    "John Deere",
)

WORKDAY_BATCH_CONFIG = {
    "Toyota": (
        "toyota",
        "wd503",
        "TMNA",
        "https://toyota.wd503.myworkdayjobs.com/TMNA",
    ),
}

GREENHOUSE_BATCH_CONFIG = {
    "Lucid Motors": (
        "lucidmotors",
        "https://lucidmotors.com/careers/search",
    ),
}

TALENTBREW_BATCH_CONFIG = {
    "Ford": (
        "www.careers.ford.com",
        "48560",
        "9255664",
        "Enterprise Technology",
        "https://www.careers.ford.com/en/search-jobs",
    ),
}

ICIMS_BATCH_CONFIG = {
    "Rivian": (
        "jibe_json",
        "careers.rivian.com",
        "https://careers.rivian.com/jobs",
    ),
}

SUCCESSFACTORS_BATCH_CONFIG = {
    "John Deere": (
        "jobs.deere.com",
        "https://jobs.deere.com/search/",
    ),
}

FALLBACK_BATCH_COMPANIES = ("General Motors",)
CURRENT_FEED_LABELS = (("General Motors", "General Motors", "simplify"),)
UNCOVERED_BATCH_COMPANIES = ("BMW", "Mercedes-Benz")


def direct_batch_names() -> set[str]:
    return (
        set(WORKDAY_BATCH_CONFIG)
        | set(GREENHOUSE_BATCH_CONFIG)
        | set(TALENTBREW_BATCH_CONFIG)
        | set(ICIMS_BATCH_CONFIG)
        | set(SUCCESSFACTORS_BATCH_CONFIG)
    )


def configured_batch_names() -> set[str]:
    return direct_batch_names() | set(FALLBACK_BATCH_COMPANIES)


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
    assert direct_batch_names().isdisjoint(FALLBACK_BATCH_COMPANIES)
    # Each direct company is claimed by exactly one provider table.
    tables = (
        WORKDAY_BATCH_CONFIG,
        GREENHOUSE_BATCH_CONFIG,
        TALENTBREW_BATCH_CONFIG,
        ICIMS_BATCH_CONFIG,
        SUCCESSFACTORS_BATCH_CONFIG,
    )
    claimed = [name for table in tables for name in table]
    assert sorted(claimed) == sorted(set(claimed))


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
    assert cfg.workday_host_variant == "jobs"
    assert cfg.workday_detail_policy == "internship_candidates"
    assert cfg.source_url == source_url
    assert WorkdaySource.endpoint(token, shard, site) == (
        f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/jobs"
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
    ("name", "variant", "host", "source_url"),
    [(name, *values) for name, values in sorted(ICIMS_BATCH_CONFIG.items())],
)
def test_icims_companies_use_their_published_jibe_inventory(
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
    ("name", "host", "source_url"),
    [(name, *values) for name, values in sorted(SUCCESSFACTORS_BATCH_CONFIG.items())],
)
def test_successfactors_companies_use_their_published_career_site(
    watchlist, name, host, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "successfactors"
    assert cfg.successfactors_host == host
    assert cfg.source_url == source_url
    assert cfg.ats in DIRECT_ATS


@pytest.mark.parametrize("name", sorted(direct_batch_names()))
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

    for label in ("BMW Group", "Mercedes-Benz Group", "Daimler"):
        assert company_matching_key(label) not in owners


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


def test_batch_does_not_claim_neighbouring_feed_identities(watchlist):
    """Distinct employers with similar names must not be absorbed."""

    for name, neighbour in (
        ("Rivian", "Rivian and Volkswagen Group Technologies"),
        ("Lucid Motors", "Lucid Bots"),
        ("Toyota", "Toyota Research Institute"),
    ):
        cfg = company(watchlist, name)
        assert not company_matches(neighbour, cfg)


def test_batch_does_not_pin_a_global_watchlist_total(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}

    assert configured_batch_names() <= configured_names
    assert len(configured_names) >= len(
        TECH_UNIVERSE_COMPANY_NAMES | configured_batch_names()
    )
