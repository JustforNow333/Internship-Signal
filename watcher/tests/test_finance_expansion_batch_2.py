"""Second finance-employer expansion batch: config contracts and feed matching.

Like the first batch, this one adds no adapter and no abstraction. Every direct
entry reuses an adapter that is already registered, and the rest fall back to
the configured GitHub feeds with a verified, non-inert match.
"""

from __future__ import annotations

import pytest

from watcher.collection_concurrency import direct_origin_key
from watcher.company_matching import company_matching_key, company_matches
from watcher.config import load_watchlist
from watcher.sources.greenhouse import GreenhouseSource
from watcher.sources.icims import IcimsSource
from watcher.sources.registry import DIRECT_ATS, build_direct_sources
from watcher.tests.tech_universe import (
    TECH_UNIVERSE_COMPANY_COUNT,
    assert_batch_is_additive,
)


BATCH_COMPANIES = (
    "Akuna Capital",
    "DRW",
    "Five Rings",
    "IMC Trading",
    "Jump Trading",
    "Optiver",
    "Susquehanna International Group",
    "Tower Research Capital",
)

GREENHOUSE_BATCH_TOKENS = {
    "Akuna Capital": "akunacapital",
    "Five Rings": "fiveringsllc",
    "Jump Trading": "jumptrading",
    "Tower Research Capital": "towerresearchcapital",
}

FALLBACK_BATCH_COMPANIES = ("DRW", "IMC Trading", "Optiver")


def watchlist():
    return load_watchlist()


def company(name: str):
    return next(c for c in watchlist().companies if c.name == name)


@pytest.mark.parametrize("name", BATCH_COMPANIES)
def test_batch_company_is_configured(name):
    assert company(name).name == name


def test_batch_is_purely_additive_to_the_tech_universe():
    """Checked by membership, so a later batch never makes this stale."""

    names = [c.name for c in watchlist().companies]

    assert len(names) == len(set(names))
    assert_batch_is_additive(BATCH_COMPANIES, set(names))
    assert len(names) >= TECH_UNIVERSE_COMPANY_COUNT + len(BATCH_COMPANIES)


@pytest.mark.parametrize(
    ("name", "token"), sorted(GREENHOUSE_BATCH_TOKENS.items())
)
def test_greenhouse_batch_companies_reuse_the_registered_adapter(name, token):
    cfg = company(name)

    assert cfg.ats == "greenhouse"
    assert "greenhouse" in DIRECT_ATS
    assert cfg.token == token
    assert cfg.source_url
    assert GreenhouseSource.endpoint(token) == (
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    )
    assert company_matches(name, cfg)


def test_susquehanna_uses_the_registered_icims_jibe_portal():
    cfg = company("Susquehanna International Group")

    assert cfg.ats == "icims"
    assert "icims" in DIRECT_ATS
    assert cfg.icims_variant == "jibe_json"
    assert cfg.icims_host == "careers.sig.com"
    assert cfg.source_url == "https://careers.sig.com/jobs"
    assert IcimsSource.jibe_endpoint(cfg.icims_host, limit=100, page=1) == (
        "https://careers.sig.com/api/jobs?limit=100&page=1"
    )
    assert direct_origin_key("icims", icims_host=cfg.icims_host) == (
        "https://careers.sig.com"
    )
    # Both label forms the feeds publish must resolve to this company.
    assert company_matches("Susquehanna International Group", cfg)
    assert company_matches("Susquehanna International Group (SIG)", cfg)
    assert company_matches("SIG", cfg)


@pytest.mark.parametrize("name", sorted(set(GREENHOUSE_BATCH_TOKENS) | {"Susquehanna International Group"}))
def test_direct_batch_companies_build_from_the_registry(name):
    cfg = company(name)

    assert build_direct_sources()[cfg.ats] is not None


@pytest.mark.parametrize("name", FALLBACK_BATCH_COMPANIES)
def test_fallback_batch_companies_are_backstop_only(name):
    cfg = company(name)

    assert cfg.ats == "github_only"
    assert cfg.ats not in DIRECT_ATS
    assert not cfg.token
    assert not cfg.source_url
    assert not cfg.icims_host
    assert not cfg.eightfold_host


@pytest.mark.parametrize(
    ("feed_label", "name"),
    [
        ("Akuna Capital", "Akuna Capital"),
        ("Akuna Capital University", "Akuna Capital"),
        ("DRW", "DRW"),
        ("Five Rings Capital", "Five Rings"),
        ("IMC Trading", "IMC Trading"),
        ("Jump Trading", "Jump Trading"),
        ("Optiver", "Optiver"),
        ("Susquehanna International Group (SIG)", "Susquehanna International Group"),
        ("Tower Research Capital", "Tower Research Capital"),
    ],
)
def test_configured_names_match_the_labels_the_feeds_publish(feed_label, name):
    assert company_matches(feed_label, company(name))


@pytest.mark.parametrize(
    ("feed_label", "name"),
    [
        # Lookalike feed labels that must not be absorbed by this batch.
        ("PIMCO", "IMC Trading"),
        ("Sigma Computing", "Susquehanna International Group"),
        ("Two Sigma", "Susquehanna International Group"),
        ("Signify", "Susquehanna International Group"),
    ],
)
def test_lookalike_feed_labels_are_not_absorbed(feed_label, name):
    assert not company_matches(feed_label, company(name))


def test_batch_labels_do_not_collide_with_any_other_watchlist_company():
    owners: dict[str, set[str]] = {}
    for cfg in watchlist().companies:
        for label in (cfg.name, *cfg.aliases):
            owners.setdefault(company_matching_key(label), set()).add(cfg.name)

    assert {key: names for key, names in owners.items() if len(names) > 1} == {}

    for name in BATCH_COMPANIES:
        cfg = company(name)
        for label in (cfg.name, *cfg.aliases):
            assert owners[company_matching_key(label)] == {name}
