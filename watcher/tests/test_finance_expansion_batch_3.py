"""Third finance-employer expansion batch: reuse-first source contracts.

Five companies reuse registered direct adapters. The three audited companies
without a completeness-safe direct configuration or a current feed match stay
out of the watchlist rather than becoming structurally inert ``github_only``
entries.
"""

from __future__ import annotations

import pytest

from watcher.company_matching import company_matching_key, company_matches
from watcher.config import load_watchlist
from watcher.sources.greenhouse import GreenhouseSource
from watcher.sources.lever import LeverSource
from watcher.sources.registry import DIRECT_ATS, build_direct_sources
from watcher.tests.tech_universe import (
    TECH_UNIVERSE_COMPANY_COUNT,
    TECH_UNIVERSE_COMPANY_NAMES,
    assert_batch_is_additive,
)


AUDITED_BATCH_COMPANIES = (
    "Wolverine Trading",
    "Old Mission Capital",
    "Belvedere Trading",
    "Radix Trading",
    "Headlands Technologies",
    "PDT Partners",
    "Quantlab",
    "XTX Markets",
)

DIRECT_BATCH_CONFIG = {
    "Old Mission Capital": (
        "greenhouse",
        "oldmissioncapital",
        "https://www.oldmissioncapital.com/careers/",
    ),
    "Belvedere Trading": (
        "lever",
        "belvederetrading",
        "https://www.belvederetrading.com/our-positions",
    ),
    "Headlands Technologies": (
        "greenhouse",
        "headlandstechnologiesllc",
        "https://www.headlandstech.com/careers/",
    ),
    "PDT Partners": (
        "greenhouse",
        "pdtpartners",
        "https://pdtpartners.com/careers",
    ),
    "XTX Markets": (
        "greenhouse",
        "xtxmarketstechnologies",
        "https://www.xtxmarkets.com/careers/",
    ),
}

UNCOVERED_BATCH_ALIASES = {
    "Wolverine Trading": ("Wolverine", "Wolverine Holdings"),
    "Radix Trading": ("Radix Trading, LLC",),
    "Quantlab": ("Quantlab Financial", "Quantlab Financial, LLC"),
}

# These labels are currently published by the configured feeds. Direct source
# precedence remains primary, but the aliases preserve useful backstop matches.
CURRENT_FEED_LABELS = (
    ("Old Mission", "Old Mission Capital"),
    ("Belvedere Trading", "Belvedere Trading"),
    ("PDT Partners", "PDT Partners"),
)


def watchlist():
    return load_watchlist()


def company(name: str):
    return next(c for c in watchlist().companies if c.name == name)


def test_batch_is_disjoint_from_the_tech_universe_and_additive():
    configured_names = {cfg.name for cfg in watchlist().companies}

    assert not set(AUDITED_BATCH_COMPANIES) & TECH_UNIVERSE_COMPANY_NAMES
    assert_batch_is_additive(tuple(DIRECT_BATCH_CONFIG), configured_names)
    assert len(configured_names) >= (
        TECH_UNIVERSE_COMPANY_COUNT + len(DIRECT_BATCH_CONFIG)
    )


@pytest.mark.parametrize(
    ("name", "ats", "token", "source_url"),
    [
        (name, *values)
        for name, values in sorted(DIRECT_BATCH_CONFIG.items())
    ],
)
def test_direct_batch_companies_reuse_registered_adapters(
    name, ats, token, source_url
):
    cfg = company(name)

    assert cfg.ats == ats
    assert cfg.token == token
    assert cfg.source_url == source_url
    assert ats in DIRECT_ATS
    assert build_direct_sources()[ats] is not None


@pytest.mark.parametrize(
    ("name", "token"),
    [
        (name, values[1])
        for name, values in sorted(DIRECT_BATCH_CONFIG.items())
        if values[0] == "greenhouse"
    ],
)
def test_greenhouse_config_builds_the_published_board_endpoint(name, token):
    assert GreenhouseSource.endpoint(token) == (
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    )
    assert company_matches(name, company(name))


def test_belvedere_config_builds_the_published_lever_endpoint():
    cfg = company("Belvedere Trading")

    assert LeverSource.endpoint(cfg.token) == (
        "https://api.lever.co/v0/postings/belvederetrading?mode=json"
    )
    assert company_matches("Belvedere Trading, LLC", cfg)


@pytest.mark.parametrize(("feed_label", "name"), CURRENT_FEED_LABELS)
def test_configured_names_match_current_feed_labels(feed_label, name):
    assert company_matches(feed_label, company(name))


def test_uncovered_companies_are_not_inert_github_only_entries():
    configured_names = {cfg.name for cfg in watchlist().companies}

    assert set(UNCOVERED_BATCH_ALIASES).isdisjoint(configured_names)


def test_batch_names_and_aliases_do_not_collide_with_existing_companies():
    configs = watchlist().companies
    owners: dict[str, set[str]] = {}
    for cfg in configs:
        for label in (cfg.name, *cfg.aliases):
            owners.setdefault(company_matching_key(label), set()).add(cfg.name)

    assert {key: names for key, names in owners.items() if len(names) > 1} == {}

    for name in DIRECT_BATCH_CONFIG:
        cfg = company(name)
        for label in (cfg.name, *cfg.aliases):
            assert owners[company_matching_key(label)] == {name}

    for name, aliases in UNCOVERED_BATCH_ALIASES.items():
        for label in (name, *aliases):
            assert company_matching_key(label) not in owners
