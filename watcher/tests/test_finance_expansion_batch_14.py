"""Fourteenth expansion batch: energy majors and professional services.

This batch closes the originally planned expansion universe.
"""

from __future__ import annotations

import pytest

from watcher.company_matching import company_matching_key, company_matches
from watcher.config import load_watchlist
from watcher.sources.registry import DIRECT_ATS, build_direct_sources
from watcher.sources.workday import WorkdaySource
from watcher.tests.tech_universe import (
    TECH_UNIVERSE_COMPANY_NAMES,
    assert_batch_is_additive,
)


AUDITED_BATCH_COMPANIES = (
    "Shell",
    "BP",
    "ConocoPhillips",
    "Deloitte",
    "Accenture",
    "PwC",
    "Oliver Wyman",
)

WORKDAY_BATCH_CONFIG = {
    "BP": (
        "bpinternational",
        "wd3",
        "bpCareers",
        "https://bpinternational.wd3.myworkdayjobs.com/bpCareers",
    ),
    "ConocoPhillips": (
        "conocophillips",
        "wd1",
        "External",
        "https://conocophillips.wd1.myworkdayjobs.com/External",
    ),
    "Shell": (
        "shell",
        "wd3",
        "ShellCareers",
        "https://shell.wd3.myworkdayjobs.com/ShellCareers",
    ),
}

FALLBACK_BATCH_COMPANIES = ("Deloitte", "PwC")
CURRENT_FEED_LABELS = (
    ("Deloitte", "Deloitte", "simplify"),
    ("PricewaterhouseCoopers (PwC)", "PwC", "simplify"),
)

# Accenture's Workday board reports 2000 while its own partitions count
# ~44,043, so the clamp safeguard proves it capped. Oliver Wyman has no
# inventory of its own: its careers site applies through the shared Marsh
# McLennan Workday tenant, which cannot be claimed under an Oliver Wyman
# identity without absorbing every other MMC operating company.
UNCOVERED_BATCH_COMPANIES = ("Accenture", "Oliver Wyman")

# Professional-services and parent naming that must never be collapsed into a
# configured batch identity.
DISTINCT_NEIGHBOURS = (
    ("Deloitte", "Deloitte Consulting"),
    ("Deloitte", "Deloitte Touche Tohmatsu"),
    ("PwC", "PwC Australia"),
    ("Shell", "Shell Energy Retail"),
)


def configured_batch_names() -> set[str]:
    return set(WORKDAY_BATCH_CONFIG) | set(FALLBACK_BATCH_COMPANIES)


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
    assert set(WORKDAY_BATCH_CONFIG).isdisjoint(FALLBACK_BATCH_COMPANIES)
    assert len(AUDITED_BATCH_COMPANIES) == 7


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


@pytest.mark.parametrize("name", sorted(WORKDAY_BATCH_CONFIG))
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
    """PwC only matches its backstop through the feed's own spelling."""

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

    # Oliver Wyman stays uncovered, so no Marsh McLennan identity may appear.
    for label in ("Marsh McLennan", "Marsh & McLennan", "MMC", "Mercer"):
        assert company_matching_key(label) not in owners


def test_marshall_wace_is_not_a_marsh_mclennan_identity(watchlist):
    """A pre-existing lookalike must not be mistaken for the MMC family."""

    marshall = company(watchlist, "Marshall Wace")

    assert not company_matches("Marsh McLennan", marshall)
    assert not company_matches("Marsh", marshall)


@pytest.mark.parametrize(("configured", "neighbour"), DISTINCT_NEIGHBOURS)
def test_professional_services_neighbours_are_not_absorbed(
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
