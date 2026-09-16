"""Sixth finance-employer expansion batch: banks and asset managers."""

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
    "Bank of America",
    "Citi",
    "Wells Fargo",
    "Deutsche Bank",
    "BNP Paribas",
    "Fidelity Investments",
    "Vanguard",
    "State Street",
)

WORKDAY_BATCH_CONFIG = {
    "Fidelity Investments": (
        "fmr",
        "wd1",
        "FidelityCareers",
        "https://jobs.fidelity.com/en/jobs/",
    ),
    "Wells Fargo": (
        "wf",
        "wd1",
        "WellsFargoJobs",
        "https://www.wellsfargojobs.com/en/jobs/",
    ),
}

# Citi was a backstop entry until its real TalentBrew site id was read from
# its own job links; the board the adapter already supports enumerates it.
TALENTBREW_BATCH_CONFIG = {
    "Citi": (
        "jobs.citi.com",
        "287",
        "9378912",
        "Student and Grad Programs",
        "https://jobs.citi.com/search-jobs",
    ),
}

FALLBACK_BATCH_COMPANIES = (
    "Bank of America",
    "BNP Paribas",
    "Deutsche Bank",
    "Vanguard",
)

CURRENT_FEED_LABELS = (
    ("Bank of America", "Bank of America", "sndsh404_summer_2027"),
    ("BNP Paribas", "BNP Paribas", "sndsh404_summer_2027"),
    ("Deutsche Bank", "Deutsche Bank", "simplify"),
    ("Vanguard", "Vanguard", "simplify"),
)

UNCOVERED_BATCH_COMPANIES = ("State Street",)


@pytest.fixture(scope="module")
def watchlist():
    return load_watchlist()


def company(watchlist, name: str):
    return next(c for c in watchlist.companies if c.name == name)


def test_batch_is_disjoint_from_the_tech_universe_and_additive(watchlist):
    configured_names = {cfg.name for cfg in watchlist.companies}
    configured_batch = tuple(
        name for name in AUDITED_BATCH_COMPANIES if name not in UNCOVERED_BATCH_COMPANIES
    )

    assert set(AUDITED_BATCH_COMPANIES).isdisjoint(TECH_UNIVERSE_COMPANY_NAMES)
    assert (
        set(WORKDAY_BATCH_CONFIG)
        | set(TALENTBREW_BATCH_CONFIG)
        | set(FALLBACK_BATCH_COMPANIES)
    ) == set(configured_batch)
    assert_batch_is_additive(configured_batch, configured_names)
    assert set(UNCOVERED_BATCH_COMPANIES).isdisjoint(configured_names)


@pytest.mark.parametrize(
    ("name", "token", "shard", "site", "source_url"),
    [
        (name, *values)
        for name, values in sorted(WORKDAY_BATCH_CONFIG.items())
    ],
)
def test_workday_companies_use_first_party_published_site_tenants(
    watchlist, name, token, shard, site, source_url
):
    cfg = company(watchlist, name)

    assert cfg.ats == "workday"
    assert (cfg.token, cfg.workday_shard, cfg.workday_site) == (token, shard, site)
    assert cfg.workday_host_variant == "jobs"
    assert cfg.source_url == source_url
    assert "workday" in DIRECT_ATS
    assert build_direct_sources()["workday"] is not None
    assert WorkdaySource.endpoint(token, shard, site) == (
        f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/jobs"
    )


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

    assert feed_name in {"simplify", "sndsh404_summer_2027"}
    assert company_matches(feed_label, cfg)


def test_batch_names_and_aliases_do_not_collide_with_watchlist_identities(watchlist):
    owners: dict[str, set[str]] = {}
    for cfg in watchlist.companies:
        for label in (cfg.name, *cfg.aliases):
            owners.setdefault(company_matching_key(label), set()).add(cfg.name)

    assert {key: names for key, names in owners.items() if len(names) > 1} == {}

    configured_batch = set(WORKDAY_BATCH_CONFIG) | set(FALLBACK_BATCH_COMPANIES)
    for name in configured_batch:
        cfg = company(watchlist, name)
        for label in (cfg.name, *cfg.aliases):
            assert owners[company_matching_key(label)] == {name}

    for name in UNCOVERED_BATCH_COMPANIES:
        assert company_matching_key(name) not in owners
