"""First finance-employer expansion batch: config contracts and feed matching.

These companies are an expansion universe layered on top of the completed
251-company recognizable-tech coverage milestone. The batch adds no adapter and
no abstraction: the direct entries reuse already-registered adapters, and the
rest fall back to the configured GitHub feeds.
"""

from __future__ import annotations

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.company_matching import company_matching_key, company_matches
from watcher.config import load_watchlist
from watcher.sources.eightfold import EightfoldSource
from watcher.sources.greenhouse import GreenhouseSource
from watcher.sources.registry import DIRECT_ATS, build_direct_sources


# The tech universe that was complete before this batch.
TECH_UNIVERSE_COMPANY_COUNT = 251

BATCH_COMPANIES = (
    "Citadel",
    "Citadel Securities",
    "D. E. Shaw",
    "Hudson River Trading",
    "Jane Street",
    "Millennium Management",
    "Point72",
    "Two Sigma",
)

DIRECT_BATCH_COMPANIES = {
    "Hudson River Trading": "greenhouse",
    "Point72": "greenhouse",
    "Millennium Management": "eightfold",
}

FALLBACK_BATCH_COMPANIES = (
    "Citadel",
    "Citadel Securities",
    "D. E. Shaw",
    "Jane Street",
    "Two Sigma",
)


def watchlist():
    return load_watchlist()


def company(name: str):
    return next(c for c in watchlist().companies if c.name == name)


@pytest.mark.parametrize("name", BATCH_COMPANIES)
def test_batch_company_is_configured(name):
    assert company(name).name == name


def test_batch_is_purely_additive_to_the_tech_universe():
    companies = watchlist().companies
    names = [c.name for c in companies]

    assert len(names) == len(set(names))
    assert set(BATCH_COMPANIES) <= set(names)
    assert len(companies) == TECH_UNIVERSE_COMPANY_COUNT + len(BATCH_COMPANIES)
    # Removing the batch leaves the earlier milestone untouched.
    assert len([n for n in names if n not in set(BATCH_COMPANIES)]) == (
        TECH_UNIVERSE_COMPANY_COUNT
    )


@pytest.mark.parametrize(("name", "ats"), sorted(DIRECT_BATCH_COMPANIES.items()))
def test_direct_batch_companies_reuse_registered_adapters(name, ats):
    cfg = company(name)

    assert cfg.ats == ats
    assert ats in DIRECT_ATS
    assert build_direct_sources()[ats] is not None


def test_hudson_river_trading_uses_its_published_greenhouse_board():
    cfg = company("Hudson River Trading")

    assert cfg.ats == "greenhouse"
    assert cfg.token == "wehrtyou"
    assert cfg.source_url == "https://www.hudsonrivertrading.com/careers/"
    assert GreenhouseSource.endpoint(cfg.token) == (
        "https://boards-api.greenhouse.io/v1/boards/wehrtyou/jobs?content=true"
    )
    assert company_matches("Hudson River Trading", cfg)
    assert company_matches("HRT", cfg)


def test_point72_uses_its_published_greenhouse_board():
    cfg = company("Point72")

    assert cfg.ats == "greenhouse"
    assert cfg.token == "point72"
    assert cfg.source_url == "https://careers.point72.com/"
    assert GreenhouseSource.endpoint(cfg.token) == (
        "https://boards-api.greenhouse.io/v1/boards/point72/jobs?content=true"
    )
    assert company_matches("Point72", cfg)


def test_millennium_uses_the_legacy_eightfold_board_linked_from_mlp_com():
    cfg = company("Millennium Management")

    assert cfg.ats == "eightfold"
    assert cfg.eightfold_host == "mlp.eightfold.ai"
    assert cfg.eightfold_domain == "mlp.com"
    assert cfg.eightfold_variant == "legacy"
    assert cfg.source_url == "https://mlp.eightfold.ai/careers"
    assert EightfoldSource.endpoint(cfg.eightfold_host, cfg.eightfold_domain, 0) == (
        "https://mlp.eightfold.ai/api/apply/v2/jobs"
        "?domain=mlp.com&start=0&num=10"
    )
    assert direct_origin_key("eightfold", eightfold_host=cfg.eightfold_host) == (
        "https://mlp.eightfold.ai"
    )
    assert company_matches("Millennium", cfg)


@pytest.mark.parametrize("name", FALLBACK_BATCH_COMPANIES)
def test_fallback_batch_companies_are_backstop_only(name):
    cfg = company(name)

    assert cfg.ats == "github_only"
    assert cfg.ats not in DIRECT_ATS
    assert not cfg.token
    assert not cfg.source_url
    # A backstop entry carries no direct-adapter configuration at all.
    assert not cfg.eightfold_host
    assert not cfg.eightfold_domain
    assert not cfg.workday_shard
    assert not cfg.icims_host


def test_citadel_entities_stay_distinct_under_exact_label_matching():
    """The two Citadel entities must never absorb each other's feed rows."""

    citadel = company("Citadel")
    securities = company("Citadel Securities")

    assert company_matches("Citadel", citadel)
    assert not company_matches("Citadel Securities", citadel)
    assert company_matches("Citadel Securities", securities)
    assert not company_matches("Citadel", securities)


@pytest.mark.parametrize(
    ("feed_label", "name"),
    [
        ("Citadel", "Citadel"),
        ("Citadel Securities", "Citadel Securities"),
        ("D. E. Shaw", "D. E. Shaw"),
        ("Hudson River Trading", "Hudson River Trading"),
        ("Jane Street", "Jane Street"),
        ("Point72", "Point72"),
        ("Two Sigma", "Two Sigma"),
    ],
)
def test_configured_names_match_the_labels_the_feeds_publish(feed_label, name):
    assert company_matches(feed_label, company(name))


def test_batch_labels_do_not_collide_with_any_other_watchlist_company():
    companies = watchlist().companies
    owners: dict[str, set[str]] = {}
    for cfg in companies:
        for label in (cfg.name, *cfg.aliases):
            owners.setdefault(company_matching_key(label), set()).add(cfg.name)

    ambiguous = {key: names for key, names in owners.items() if len(names) > 1}
    assert ambiguous == {}

    for name in BATCH_COMPANIES:
        cfg = company(name)
        for label in (cfg.name, *cfg.aliases):
            assert owners[company_matching_key(label)] == {name}
