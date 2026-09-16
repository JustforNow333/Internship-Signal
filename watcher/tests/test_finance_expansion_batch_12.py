"""Twelfth expansion batch: defence primes and large pharmaceutical employers."""

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
    "L3Harris",
    "General Dynamics",
    "Blue Origin",
    "Johnson & Johnson",
    "Moderna",
    "AbbVie",
    "Bristol Myers Squibb",
    "Amgen",
)

ALREADY_COVERED_BATCH_COMPANIES: tuple[str, ...] = ()

WORKDAY_BATCH_CONFIG = {
    "Amgen": (
        "amgen",
        "wd1",
        "Careers",
        "https://amgen.wd1.myworkdayjobs.com/Careers",
    ),
    "Bristol Myers Squibb": (
        "bristolmyerssquibb",
        "wd5",
        "BMS",
        "https://bristolmyerssquibb.wd5.myworkdayjobs.com/BMS",
    ),
    "Moderna": (
        "modernatx",
        "wd1",
        "M_tx",
        "https://modernatx.wd1.myworkdayjobs.com/M_tx",
    ),
}

TALENTBREW_BATCH_CONFIG = {
    "L3Harris": (
        "careers.l3harris.com",
        "4832",
        "62394",
        "Co-Op/Intern",
        "https://careers.l3harris.com/search-jobs",
    ),
}

FALLBACK_BATCH_COMPANIES = ("Blue Origin", "Johnson & Johnson", "AbbVie")
CURRENT_FEED_LABELS = (
    ("Blue Origin", "Blue Origin", "simplify"),
    ("Johnson & Johnson", "Johnson & Johnson", "simplify"),
    ("AbbVie", "AbbVie", "simplify"),
)
UNCOVERED_BATCH_COMPANIES = ("General Dynamics",)

# Separately incorporated employers whose names overlap a batch identity. Each
# must stay distinct: General Dynamics Electric Boat is already configured with
# its own iCIMS inventory, and ABB is a batch 11 backstop entry.
DISTINCT_NEIGHBOURS = (
    ("ABB", "AbbVie"),
    ("General Dynamics Electric Boat", "General Dynamics"),
    ("General Dynamics Electric Boat", "General Dynamics Information Technology"),
    ("General Dynamics Electric Boat", "General Dynamics Mission Systems"),
)


def direct_batch_names() -> set[str]:
    return set(WORKDAY_BATCH_CONFIG) | set(TALENTBREW_BATCH_CONFIG)


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
    assert covered.isdisjoint(ALREADY_COVERED_BATCH_COMPANIES)
    assert (
        covered
        | set(UNCOVERED_BATCH_COMPANIES)
        | set(ALREADY_COVERED_BATCH_COMPANIES)
    ) == set(AUDITED_BATCH_COMPANIES)
    assert set(WORKDAY_BATCH_CONFIG).isdisjoint(TALENTBREW_BATCH_CONFIG)
    assert direct_batch_names().isdisjoint(FALLBACK_BATCH_COMPANIES)


def test_batch_adds_each_company_exactly_once(watchlist):
    names = [cfg.name for cfg in watchlist.companies]

    assert len(names) == len(set(names))
    for name in configured_batch_names():
        assert names.count(name) == 1


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

    assert company_matching_key("General Dynamics Corporation") not in owners


def test_general_dynamics_identity_stays_separate_from_electric_boat(watchlist):
    """The configured subsidiary must not absorb the uncovered parent.

    General Dynamics Electric Boat is a separately recruited subsidiary with
    its own iCIMS inventory. Leaving the parent uncovered only stays honest
    while the subsidiary refuses to answer to the parent's identity.
    """

    gdeb = company(watchlist, "General Dynamics Electric Boat")

    assert gdeb.ats == "icims"
    assert not company_matches("General Dynamics", gdeb)
    assert company_matches("General Dynamics Electric Boat", gdeb)
    assert company_matching_key("General Dynamics") != company_matching_key(
        "General Dynamics Electric Boat"
    )


def test_abb_and_abbvie_are_never_the_same_identity(watchlist):
    abb = company(watchlist, "ABB")
    abbvie = company(watchlist, "AbbVie")

    assert company_matching_key("ABB") != company_matching_key("AbbVie")
    assert not company_matches("AbbVie", abb)
    assert not company_matches("ABB", abbvie)
    assert not company_matches("Abbott", abb)
    assert not company_matches("Abbott", abbvie)


@pytest.mark.parametrize(("configured", "neighbour"), DISTINCT_NEIGHBOURS)
def test_distinct_neighbouring_identities_are_not_absorbed(
    watchlist, configured, neighbour
):
    cfg = company(watchlist, configured)

    assert not company_matches(neighbour, cfg)


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
