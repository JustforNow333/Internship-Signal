"""Explicit Workday public host layouts: jobs (default) and site.

Both layouts serve the same CXS contract; only URL routing differs. These tests
pin that the existing ``jobs`` URLs are byte-identical and that ``site`` routes
to the myworkdaysite host with the tenant carried in the posting path.
"""

import pytest

from watcher.config import (
    SUPPORTED_WORKDAY_HOST_VARIANTS,
    WORKDAY_HOST_JOBS,
    WORKDAY_HOST_SITE,
    CompanyCfg,
    ConfigError,
    DEFAULT_WATCHLIST_PATH,
    load_watchlist,
)
from watcher.collection_concurrency import direct_origin_key
from watcher.collection_snapshot import collection_config_fingerprint
from watcher.config.models import WatcherConfig
from watcher.sources.workday import WorkdaySource


JOBS_ARGS = ("nxp", "wd3", "careers")
SITE_ARGS = ("snapchat", "wd1", "snap")


def test_supported_variants_are_exactly_jobs_and_site():
    assert SUPPORTED_WORKDAY_HOST_VARIANTS == {WORKDAY_HOST_JOBS, WORKDAY_HOST_SITE}
    assert WORKDAY_HOST_JOBS == "jobs"
    assert WORKDAY_HOST_SITE == "site"


# --- the default layout must not move -------------------------------------


def test_jobs_urls_are_unchanged_by_default_and_when_named():
    token, shard, site = JOBS_ARGS
    listing = f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/jobs"
    posting = f"https://{token}.{shard}.myworkdayjobs.com/{site}/job/X"
    detail = f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/job/X"

    assert WorkdaySource.endpoint(token, shard, site) == listing
    assert WorkdaySource.posting_url(token, shard, site, "/job/X") == posting
    assert WorkdaySource.detail_endpoint(token, shard, site, "/job/X") == detail

    # Naming the default explicitly must produce identical URLs.
    assert WorkdaySource.endpoint(token, shard, site, WORKDAY_HOST_JOBS) == listing
    assert (
        WorkdaySource.posting_url(token, shard, site, "/job/X", WORKDAY_HOST_JOBS)
        == posting
    )
    assert (
        WorkdaySource.detail_endpoint(token, shard, site, "/job/X", WORKDAY_HOST_JOBS)
        == detail
    )


# --- the site layout ------------------------------------------------------


def test_site_listing_url_uses_the_shard_only_host():
    token, shard, site = SITE_ARGS
    assert WorkdaySource.endpoint(token, shard, site, WORKDAY_HOST_SITE) == (
        f"https://{shard}.myworkdaysite.com/wday/cxs/{token}/{site}/jobs"
    )


def test_site_detail_url_uses_the_shard_only_host():
    token, shard, site = SITE_ARGS
    assert WorkdaySource.detail_endpoint(
        token, shard, site, "/job/X", WORKDAY_HOST_SITE
    ) == f"https://{shard}.myworkdaysite.com/wday/cxs/{token}/{site}/job/X"


def test_site_posting_url_carries_the_tenant_in_the_path():
    token, shard, site = SITE_ARGS
    assert WorkdaySource.posting_url(
        token, shard, site, "/job/X", WORKDAY_HOST_SITE
    ) == f"https://{shard}.myworkdaysite.com/recruiting/{token}/{site}/job/X"


def test_detail_endpoint_normalizes_a_missing_leading_slash_in_both_layouts():
    for variant, host in (
        (WORKDAY_HOST_JOBS, "nxp.wd3.myworkdayjobs.com"),
        (WORKDAY_HOST_SITE, "wd1.myworkdaysite.com"),
    ):
        token, shard, site = JOBS_ARGS if variant == WORKDAY_HOST_JOBS else SITE_ARGS
        assert WorkdaySource.detail_endpoint(
            token, shard, site, "job/X", variant
        ) == f"https://{host}/wday/cxs/{token}/{site}/job/X"


# --- configuration --------------------------------------------------------


def _watchlist(tmp_path, variant_line=""):
    path = tmp_path / "watchlist.yml"
    path.write_text(
        'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
        '  - name: "Example"\n    ats: workday\n    token: "example"\n'
        '    workday_shard: "wd1"\n    workday_site: "Careers"\n' + variant_line,
        encoding="utf-8",
    )
    return path


def test_missing_variant_defaults_to_jobs(tmp_path):
    config = load_watchlist(_watchlist(tmp_path))

    assert config.companies[0].workday_host_variant == WORKDAY_HOST_JOBS


@pytest.mark.parametrize("variant", sorted(SUPPORTED_WORKDAY_HOST_VARIANTS))
def test_valid_variants_load(tmp_path, variant):
    config = load_watchlist(_watchlist(tmp_path, f"    workday_host_variant: {variant}\n"))

    assert config.companies[0].workday_host_variant == variant


@pytest.mark.parametrize("variant", ["sites", "SITE", "myworkdaysite", "0"])
def test_invalid_variants_are_rejected(tmp_path, variant):
    with pytest.raises(ConfigError, match="workday_host_variant must be one of"):
        load_watchlist(_watchlist(tmp_path, f'    workday_host_variant: "{variant}"\n'))


# --- cross-cutting integration --------------------------------------------


def test_site_tenants_share_one_shard_origin_and_jobs_grouping_is_unchanged():
    snap = direct_origin_key(
        "workday", token="snapchat", workday_shard="wd1", workday_host_variant="site"
    )
    other_site_tenant = direct_origin_key(
        "workday", token="othertenant", workday_shard="wd1", workday_host_variant="site"
    )

    # One myworkdaysite shard is a single host, so its tenants share the limit.
    assert snap == other_site_tenant == "https://wd1.myworkdaysite.com"

    # The default layout keeps its per-tenant grouping.
    assert (
        direct_origin_key("workday", token="nxp", workday_shard="wd3")
        == "https://nxp.wd3.myworkdayjobs.com"
    )
    assert direct_origin_key(
        "workday", token="nxp", workday_shard="wd3", workday_host_variant="jobs"
    ) == "https://nxp.wd3.myworkdayjobs.com"


def test_host_variant_changes_the_collection_fingerprint():
    company = CompanyCfg(
        name="Example",
        ats="workday",
        token="snapchat",
        workday_shard="wd1",
        workday_site="snap",
    )
    config = WatcherConfig(companies=(company,))
    from dataclasses import replace

    changed = replace(
        config,
        companies=(replace(company, workday_host_variant=WORKDAY_HOST_SITE),),
    )

    assert collection_config_fingerprint(changed) != collection_config_fingerprint(config)


def test_real_watchlist_builds_snap_on_the_site_layout():
    config = load_watchlist(DEFAULT_WATCHLIST_PATH)
    snap = next(c for c in config.companies if c.name == "Snap")

    assert snap.ats == "workday"
    assert (snap.token, snap.workday_shard, snap.workday_site) == SITE_ARGS
    assert snap.workday_host_variant == WORKDAY_HOST_SITE
    assert WorkdaySource.endpoint(
        snap.token, snap.workday_shard, snap.workday_site, snap.workday_host_variant
    ) == "https://wd1.myworkdaysite.com/wday/cxs/snapchat/snap/jobs"

    # Every other Workday company must stay on the default layout.
    others = [
        c.name
        for c in config.companies
        if c.ats == "workday" and c.workday_host_variant != WORKDAY_HOST_JOBS
    ]
    assert others == ["Snap"]
