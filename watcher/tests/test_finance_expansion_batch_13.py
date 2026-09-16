"""Thirteenth expansion batch: healthcare, medical devices, and energy."""

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


# The audit targets. Three are corporate families whose parent and subsidiary
# brands were checked against the live recruiting inventory rather than assumed.
AUDITED_BATCH_TARGETS = (
    "Gilead Sciences",
    "Regeneron",
    "UnitedHealth Group / Optum",
    "CVS Health / Aetna",
    "Cigna / Evernorth",
    "Stryker",
    "Medtronic",
    "Chevron",
)

# Each family resolved to ONE authoritative inventory, so each is configured as
# one company carrying the sibling brand as an alias. The subsidiary brands are
# deliberately not separate entries: adding them would double-count the same
# postings. UHG/Optum and Cigna/Evernorth are configured; CVS/Aetna is not,
# because its shared board cannot be enumerated within the page safeguard.
CORPORATE_FAMILIES = {
    "UnitedHealth Group": ("Optum", "UnitedHealth", "UHG"),
    "Cigna": ("The Cigna Group", "Cigna Group", "Evernorth"),
}
UNCONFIGURED_FAMILY_BRANDS = ("CVS Health", "Aetna", "CVS")

WORKDAY_BATCH_CONFIG = {
    "Cigna": (
        "cigna",
        "wd5",
        "cignacareers",
        "https://cigna.wd5.myworkdayjobs.com/cignacareers",
    ),
}

TALENTBREW_BATCH_CONFIG = {
    "Chevron": (
        "careers.chevron.com",
        "38138",
        "8271696",
        "Interns",
        "https://careers.chevron.com/search-jobs",
    ),
    "UnitedHealth Group": (
        "careers.unitedhealthgroup.com",
        "34088",
        "81405",
        "Technology",
        "https://careers.unitedhealthgroup.com/search-jobs",
    ),
}

FALLBACK_BATCH_COMPANIES = ("Stryker", "Medtronic")
CURRENT_FEED_LABELS = (
    ("Stryker", "Stryker", "simplify"),
    ("Medtronic", "Medtronic", "simplify"),
)

# Left uncovered. Gilead and CVS Health both have working, uncapped tenants but
# exceed a bounded-collection safeguard, which must not be raised to force
# coverage; Regeneron is behind an anti-bot challenge that must not be evaded.
UNCOVERED_BATCH_COMPANIES = ("Gilead Sciences", "Regeneron", "CVS Health")


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

    assert configured_batch_names().isdisjoint(TECH_UNIVERSE_COMPANY_NAMES)
    assert_batch_is_additive(tuple(configured_batch_names()), configured_names)
    assert set(UNCOVERED_BATCH_COMPANIES).isdisjoint(configured_names)


def test_every_audited_target_has_exactly_one_outcome():
    covered = configured_batch_names()

    assert covered.isdisjoint(UNCOVERED_BATCH_COMPANIES)
    assert len(AUDITED_BATCH_TARGETS) == 8
    assert len(covered) + len(UNCOVERED_BATCH_COMPANIES) == len(AUDITED_BATCH_TARGETS)
    assert set(WORKDAY_BATCH_CONFIG).isdisjoint(TALENTBREW_BATCH_CONFIG)
    assert direct_batch_names().isdisjoint(FALLBACK_BATCH_COMPANIES)


def test_batch_adds_each_company_exactly_once(watchlist):
    names = [cfg.name for cfg in watchlist.companies]

    assert len(names) == len(set(names))
    for name in configured_batch_names():
        assert names.count(name) == 1


@pytest.mark.parametrize(("parent", "brands"), sorted(CORPORATE_FAMILIES.items()))
def test_corporate_families_are_one_entry_with_sibling_aliases(
    watchlist, parent, brands
):
    """One shared inventory means one company, not one entry per brand."""

    cfg = company(watchlist, parent)
    configured = {c.name for c in watchlist.companies}

    for brand in brands:
        # The sibling brand resolves to the parent entry...
        assert company_matches(brand, cfg), brand
        # ...and is never configured as a duplicate company of its own.
        assert brand not in configured, brand


def test_cvs_family_is_not_configured_under_any_brand(watchlist):
    """CVS Health stays uncovered, so no CVS/Aetna brand may be claimed."""

    owners = {
        company_matching_key(label)
        for cfg in watchlist.companies
        for label in (cfg.name, *cfg.aliases)
    }

    for brand in UNCONFIGURED_FAMILY_BRANDS:
        assert company_matching_key(brand) not in owners, brand


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

    for label in ("Gilead", "Regeneron Pharmaceuticals"):
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


def test_batch_does_not_claim_neighbouring_identities(watchlist):
    """Similarly named but separately recruited employers stay distinct."""

    for name, neighbour in (
        ("Chevron", "Chevron Phillips Chemical"),
        ("Stryker", "Stryker Corporation Sustainability Solutions"),
        ("UnitedHealth Group", "United Airlines"),
    ):
        cfg = company(watchlist, name)
        assert not company_matches(neighbour, cfg)


def test_batch_does_not_pin_a_global_watchlist_total(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}

    assert configured_batch_names() <= configured_names
    assert len(configured_names) >= len(
        TECH_UNIVERSE_COMPANY_NAMES | configured_batch_names()
    )
