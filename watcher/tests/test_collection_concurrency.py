"""Offline regression coverage for opt-in bounded collection concurrency."""

import hashlib
import json
import threading
import time
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import pytest

from watcher.collection_concurrency import (
    DIRECT_ORIGIN_HOSTS,
    CollectionScheduler,
    CollectionTask,
    ConcurrencyObserver,
    TaskResult,
    direct_origin_key,
    dispatch_order,
    origin_key_for_url,
    run_collection_tasks,
)
from watcher.collection_snapshot import save_collection_snapshot
from watcher.config import (
    COLLECTION_MODE_CONCURRENT,
    COLLECTION_MODE_SERIAL,
    CollectionConcurrencyCfg,
    CompanyCfg,
    ConfigError,
    GitHubListingSourceCfg,
    WatcherConfig,
    load_collection_concurrency,
)
from watcher.run import (
    CollectionStats,
    _DirectFetchOutcome,
    _direct_outcome_from_result,
    collect_batch,
    run_once,
)
from watcher.seen_store import SeenStore
from watcher.source_health import (
    ERROR_UNEXPECTED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
)
from watcher.sources.ashby import AshbySource
from watcher.sources.base import (
    DirectSourceDiagnostics,
    SourceError,
    SourceFetchError,
    make_row,
)
from watcher.sources.bain import BainSource
from watcher.sources.epic import EpicSource
from watcher.sources.greenhouse import GreenhouseSource
from watcher.sources.ibm import IbmSource
from watcher.sources.lever import LeverSource
from watcher.sources.paylocity import PaylocitySource
from watcher.sources.smartrecruiters import SmartRecruitersSource
from watcher.sources.workable import WorkableSource
from watcher.sources.workday import (
    WorkdayPacer,
    WorkdaySource,
    WorkdayStartRecord,
    summarize_workday_starts,
)

CONCURRENT = CollectionConcurrencyCfg(
    mode=COLLECTION_MODE_CONCURRENT,
    max_workers=4,
    workday_max_concurrency=1,
    per_origin_max_concurrency=2,
)
SERIAL = CollectionConcurrencyCfg()


def row(company, title, *, source="direct"):
    return make_row(
        source=source,
        source_adapter="fake",
        company=company,
        title=title,
        location="New York, NY",
        description="Build Python APIs with React.",
        requirements="Python, SQL, REST APIs, Git",
        source_url=f"https://example.test/{company}/{title}".replace(" ", "-"),
        internship_type="Summer",
    )


class ScopeProbe:
    """Thread-safe peak-concurrency recorder shared by the fake adapters."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = {}
        self.peak = {}
        self.starts = []

    def enter(self, *scopes):
        with self._lock:
            self.starts.append(time.perf_counter())
            for scope in scopes:
                value = self.current.get(scope, 0) + 1
                self.current[scope] = value
                self.peak[scope] = max(self.peak.get(scope, 0), value)

    def exit(self, *scopes):
        with self._lock:
            for scope in scopes:
                self.current[scope] = max(0, self.current.get(scope, 0) - 1)


class DelayedSource:
    """Deterministic fake adapter with a fixed delay and recorded scopes."""

    def __init__(self, name, rows_by_company=None, *, probe=None, delay=0.02, errors=None):
        self.name = name
        self.rows_by_company = rows_by_company or {}
        self.errors = errors or {}
        self.probe = probe
        self.delay = delay
        self.calls = []
        self._lock = threading.Lock()

    def fetch(self, company):
        scopes = (self.name, f"{self.name}:{company.token or company.name}", "any")
        if self.probe:
            self.probe.enter(*scopes)
        try:
            with self._lock:
                self.calls.append(company.name)
            time.sleep(self.delay)
            error = self.errors.get(company.name)
            if error is not None:
                raise error
            rows = list(self.rows_by_company.get(company.name, []))
            self.last_health_diagnostics = DirectSourceDiagnostics(
                succeeded=True,
                retained_row_count=len(rows),
                complete=True,
            )
            return rows
        finally:
            if self.probe:
                self.probe.exit(*scopes)


class DelayedGithub:
    def __init__(self, url, rows=None, *, error=None, delay=0.01):
        self.feed_label = url
        self.url = url
        self.rows = rows or []
        self.error = error
        self.delay = delay

    def fetch_many(self, companies):
        time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return list(self.rows)


def batch_digest(batch):
    """Return a deterministic digest of everything one collection produced."""

    payload = {
        "rows": [dict(row) for row in batch.rows],
        "errors": list(batch.errors),
        "attempts": [
            {
                "health_key": attempt.health_key,
                "source_kind": attempt.source_kind,
                "company": attempt.company,
                "adapter": attempt.adapter,
                "attempted": attempt.attempted,
                "succeeded": attempt.succeeded,
                "rows_returned": attempt.rows_returned,
                "error_kind": attempt.error_kind,
                "error_message": attempt.error_message,
                "feed_label": attempt.feed_label,
                "unsupported_reason": attempt.unsupported_reason,
                "malformed_row_count": attempt.malformed_row_count,
                "schema_error_row_count": attempt.schema_error_row_count,
                "duplicate_row_count": attempt.duplicate_row_count,
                "failed_request_count": attempt.failed_request_count,
                "incomplete": attempt.incomplete,
                "truncated": attempt.truncated,
                "reason_codes": list(attempt.reason_codes),
                "degraded": attempt.degraded,
                "complete": attempt.complete,
            }
            for attempt in batch.source_attempts
        ],
        "github": [batch.github_feeds_configured, batch.github_feeds_succeeded],
        "workday": [
            batch.workday_attempted,
            batch.workday_succeeded,
            batch.workday_failed,
            batch.workday_request_attempts,
            batch.workday_retry_attempts,
            list(batch.workday_failure_codes),
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def mixed_config(concurrency):
    feed = GitHubListingSourceCfg(
        name="fixture_feed",
        format="simplify_json",
        url="https://raw.githubusercontent.test/owner/repo/listings.json",
    )
    return WatcherConfig(
        companies=(
            CompanyCfg(name="AlphaCo", ats="greenhouse", token="alpha"),
            CompanyCfg(name="BetaCo", ats="greenhouse", token="beta"),
            CompanyCfg(name="GammaCo", ats="lever", token="gamma"),
            CompanyCfg(
                name="DeltaCo",
                ats="workday",
                token="delta",
                workday_shard="wd5",
                workday_site="Delta",
            ),
            CompanyCfg(
                name="EpsilonCo",
                ats="workday",
                token="epsilon",
                workday_shard="wd1",
                workday_site="Epsilon",
            ),
            CompanyCfg(name="BrokenCo", ats="greenhouse", token="broken"),
            CompanyCfg(name="BespokeCo", ats="bespoke"),
            CompanyCfg(name="BackstopCo", ats="github_only"),
            CompanyCfg(name="NoAdapterCo", ats="ashby", token="none"),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(feed,),
        collection_concurrency=concurrency,
    )


def mixed_sources(probe=None, *, delay=0.02):
    greenhouse = DelayedSource(
        "greenhouse",
        {
            "AlphaCo": [row("AlphaCo", "Software Engineer Intern")],
            "BetaCo": [row("BetaCo", "Backend Intern")],
        },
        probe=probe,
        delay=delay,
        errors={"BrokenCo": SourceError("boom")},
    )
    lever = DelayedSource(
        "lever",
        {"GammaCo": [row("GammaCo", "Software Engineering Intern")]},
        probe=probe,
        delay=delay,
    )
    workday = DelayedSource(
        "workday",
        {
            "DeltaCo": [row("DeltaCo", "Software Engineer Intern")],
            "EpsilonCo": [row("EpsilonCo", "Data Engineering Intern")],
        },
        probe=probe,
        delay=delay,
    )
    return {"greenhouse": greenhouse, "lever": lever, "workday": workday}


def collect(config, *, stats=None, probe=None, delay=0.02, github_rows=None):
    return collect_batch(
        config,
        direct_sources=mixed_sources(probe, delay=delay),
        github_source=DelayedGithub(
            "https://raw.githubusercontent.test/owner/repo/listings.json",
            github_rows if github_rows is not None else [row("BackstopCo", "SWE Intern", source="github")],
        ),
        stats=stats,
        run_id="fixed-run",
        observed_at=datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
        captured_at=datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
    )


# --- configuration -------------------------------------------------------


def test_production_default_stays_serial(monkeypatch):
    for name in (
        "WATCHER_COLLECTION_MODE",
        "WATCHER_COLLECTION_MAX_WORKERS",
        "WATCHER_WORKDAY_MAX_CONCURRENCY",
        "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)

    concurrency = load_collection_concurrency()

    assert concurrency.mode == COLLECTION_MODE_SERIAL
    assert concurrency.concurrent is False
    assert concurrency.max_workers == 4
    assert concurrency.workday_max_concurrency == 1
    assert concurrency.per_origin_max_concurrency == 2
    assert WatcherConfig(companies=()).collection_concurrency.mode == COLLECTION_MODE_SERIAL


def test_recommended_canary_configuration_is_opt_in(monkeypatch):
    monkeypatch.setenv("WATCHER_COLLECTION_MODE", "Concurrent")
    monkeypatch.setenv("WATCHER_COLLECTION_MAX_WORKERS", "4")
    monkeypatch.setenv("WATCHER_WORKDAY_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY", "2")

    concurrency = load_collection_concurrency()

    assert concurrency.concurrent is True
    assert concurrency.as_dict() == {
        "mode": "concurrent",
        "max_workers": 4,
        "workday_max_concurrency": 1,
        "per_origin_max_concurrency": 2,
    }


def test_blank_environment_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("WATCHER_COLLECTION_MODE", "   ")
    monkeypatch.setenv("WATCHER_COLLECTION_MAX_WORKERS", "")

    concurrency = load_collection_concurrency()

    assert concurrency.mode == COLLECTION_MODE_SERIAL
    assert concurrency.max_workers == 4


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"mode": "parallel"}, "WATCHER_COLLECTION_MODE"),
        ({"max_workers": 0}, "WATCHER_COLLECTION_MAX_WORKERS"),
        ({"max_workers": 17}, "WATCHER_COLLECTION_MAX_WORKERS"),
        ({"max_workers": "four"}, "WATCHER_COLLECTION_MAX_WORKERS"),
        ({"workday_max_concurrency": 0}, "WATCHER_WORKDAY_MAX_CONCURRENCY"),
        ({"workday_max_concurrency": 6}, "WATCHER_WORKDAY_MAX_CONCURRENCY"),
        ({"per_origin_max_concurrency": 0}, "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY"),
        ({"per_origin_max_concurrency": 5}, "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY"),
    ],
)
def test_invalid_limits_fail_loudly(kwargs, message):
    with pytest.raises(ConfigError, match=message):
        CollectionConcurrencyCfg(**kwargs)


def test_scope_limits_cannot_exceed_the_global_worker_pool():
    with pytest.raises(ConfigError, match="WATCHER_WORKDAY_MAX_CONCURRENCY cannot exceed"):
        CollectionConcurrencyCfg(max_workers=1, workday_max_concurrency=2)
    with pytest.raises(
        ConfigError, match="WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY cannot exceed"
    ):
        CollectionConcurrencyCfg(max_workers=2, per_origin_max_concurrency=3)


# --- origin and provider keys -------------------------------------------


def test_direct_origin_hosts_match_the_adapter_endpoints():
    endpoints = {
        "bain": BainSource.endpoint(page=0, results=100),
        "epic": EpicSource.endpoint(),
        "ibm": IbmSource.endpoint(start=0, results=100, page=1),
        "greenhouse": GreenhouseSource.endpoint("token"),
        "lever": LeverSource.endpoint("token"),
        "ashby": AshbySource.endpoint("token"),
        "smartrecruiters": SmartRecruitersSource.endpoint("token"),
        "paylocity": PaylocitySource.endpoint(
            CompanyCfg(
                name="Example",
                ats="paylocity",
                paylocity_company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                paylocity_module_id="1",
                paylocity_slug="Example",
            )
        ),
        "workable": WorkableSource.endpoint("token"),
    }

    assert {
        adapter: urlsplit(url).hostname for adapter, url in endpoints.items()
    } == dict(DIRECT_ORIGIN_HOSTS)


def test_same_ats_host_shares_one_origin_key_across_companies():
    alpha = direct_origin_key("greenhouse", token="alpha")
    beta = direct_origin_key("greenhouse", token="beta")

    assert alpha == beta == "https://boards-api.greenhouse.io"


def test_paylocity_tenants_share_the_public_recruiting_origin():
    assert direct_origin_key("paylocity") == "https://recruiting.paylocity.com"


def test_workday_tenants_use_their_own_host_origin():
    delta = direct_origin_key("workday", token="delta", workday_shard="wd5")
    epsilon = direct_origin_key("workday", token="epsilon", workday_shard="wd5")

    assert delta != epsilon
    assert "myworkdayjobs.com" in delta
    assert urlsplit(WorkdaySource.endpoint("delta", "wd5", "Site")).hostname in delta


def test_oracle_hcm_tenants_use_their_configured_host_origin():
    jpmc = direct_origin_key(
        "oracle_hcm", oracle_hcm_host="jpmc.fa.oraclecloud.com"
    )
    example = direct_origin_key(
        "oracle_hcm", oracle_hcm_host="example.fa.oraclecloud.com"
    )

    assert jpmc == "https://jpmc.fa.oraclecloud.com"
    assert example == "https://example.fa.oraclecloud.com"
    assert jpmc != example


def test_origin_keys_exclude_credentials_paths_and_queries():
    key = origin_key_for_url(
        "https://user:secret@raw.githubusercontent.test:8443/owner/repo/listings.json?token=abc#frag"
    )

    assert key == "https://raw.githubusercontent.test:8443"
    assert "secret" not in key
    assert "token" not in key
    assert "listings" not in key


def test_origin_key_is_total_over_malformed_urls():
    assert origin_key_for_url("https://[oops") == "unknown"
    assert origin_key_for_url("") == "unknown"
    assert direct_origin_key("mystery_ats") == "adapter:mystery_ats"


# --- scheduler invariants ------------------------------------------------


def test_dispatch_order_round_robins_bounded_scopes():
    tasks = [
        CollectionTask(key=f"t{index}", origin="o1", provider="workday", run=lambda: None, workday=True)
        for index in range(3)
    ] + [
        CollectionTask(key="gh", origin="o2", provider="greenhouse", run=lambda: None)
    ]

    order = dispatch_order(tasks)

    assert sorted(order) == [0, 1, 2, 3]
    # The lone non-Workday task is dispatched before the queued Workday tenants.
    assert order[0] == 3


def test_worker_programming_errors_become_failed_task_results():
    def explode():
        raise RuntimeError("worker bug")

    tasks = [
        CollectionTask(key="ok", origin="o", provider="p", run=lambda: "value"),
        CollectionTask(key="bad", origin="o", provider="p", run=explode),
    ]

    results, metrics = run_collection_tasks(tasks, concurrency=CONCURRENT)

    assert [result.index for result in results] == [0, 1]
    assert results[0].value == "value"
    assert results[1].value is None
    assert isinstance(results[1].error, RuntimeError)
    assert results[1].error_label() == "runtimeerror"
    assert metrics.unexpected_exceptions == 1
    assert metrics.executor_shutdown_clean is True


def test_executor_shuts_down_cleanly_and_leaves_no_worker_threads():
    before = {thread.name for thread in threading.enumerate()}
    tasks = [
        CollectionTask(key=f"t{index}", origin=f"o{index}", provider="p", run=lambda: time.sleep(0.01))
        for index in range(6)
    ]

    _results, metrics = run_collection_tasks(tasks, concurrency=CONCURRENT)

    assert metrics.executor_shutdown_clean is True
    assert not [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith("watcher-collect-") and thread.name not in before
    ]


def test_unexpected_task_result_is_converted_into_a_failed_direct_outcome():
    stats = CollectionStats()
    company = CompanyCfg(name="AlphaCo", ats="greenhouse", token="alpha")
    task = CollectionTask(key="direct", origin="o", provider="greenhouse", run=lambda: None)

    outcome = _direct_outcome_from_result(
        company,
        TaskResult(index=0, task=task, value=None, error=TypeError("bad argument")),
        stats,
    )

    assert isinstance(outcome, _DirectFetchOutcome)
    assert outcome.succeeded is False
    assert outcome.error_kind == ERROR_UNEXPECTED
    assert isinstance(outcome.error, TypeError)
    assert stats.unexpected_task_exceptions == 1


# --- collection equivalence ---------------------------------------------


def test_concurrent_collection_produces_the_serial_batch_exactly(tmp_path):
    serial_stats = CollectionStats()
    concurrent_stats = CollectionStats()

    serial_batch = collect(mixed_config(SERIAL), stats=serial_stats)
    concurrent_batch = collect(mixed_config(CONCURRENT), stats=concurrent_stats)

    assert batch_digest(concurrent_batch) == batch_digest(serial_batch)
    assert [dict(item) for item in concurrent_batch.rows] == [
        dict(item) for item in serial_batch.rows
    ]
    assert list(concurrent_batch.errors) == list(serial_batch.errors)
    save_collection_snapshot(serial_batch, tmp_path / "serial.json.gz")
    save_collection_snapshot(concurrent_batch, tmp_path / "concurrent.json.gz")
    assert (tmp_path / "concurrent.json.gz").read_bytes() == (
        tmp_path / "serial.json.gz"
    ).read_bytes()
    assert serial_stats.collection_mode == COLLECTION_MODE_SERIAL
    assert concurrent_stats.collection_mode == COLLECTION_MODE_CONCURRENT
    assert not hasattr(serial_batch, "workday_start_telemetry")
    assert not hasattr(concurrent_batch, "workday_start_telemetry")


def test_concurrent_collection_preserves_source_priority_and_attempt_order():
    batch = collect(mixed_config(CONCURRENT), delay=0.0)

    companies = [
        attempt.company
        for attempt in batch.source_attempts
        if attempt.source_kind == SOURCE_KIND_DIRECT
    ]
    assert companies == [
        "AlphaCo",
        "BetaCo",
        "GammaCo",
        "DeltaCo",
        "EpsilonCo",
        "BrokenCo",
        "BespokeCo",
        "BackstopCo",
        "NoAdapterCo",
    ]
    assert [dict(item)["company"] for item in batch.rows] == [
        "AlphaCo",
        "BetaCo",
        "GammaCo",
        "DeltaCo",
        "EpsilonCo",
        "BackstopCo",
    ]
    assert [dict(item)["extra"]["source"] for item in batch.rows][-1] == "github"
    assert (
        len([a for a in batch.source_attempts if a.source_kind == SOURCE_KIND_GITHUB_FEED])
        == 1
    )


def test_slowest_source_completing_last_does_not_reorder_rows():
    config = mixed_config(CONCURRENT)
    sources = mixed_sources(delay=0.0)
    # AlphaCo/BetaCo share the greenhouse adapter and finish last.
    sources["greenhouse"].delay = 0.08

    batch = collect_batch(
        config,
        direct_sources=sources,
        github_source=DelayedGithub(
            "https://raw.githubusercontent.test/owner/repo/listings.json", []
        ),
        run_id="fixed-run",
        observed_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )

    assert [dict(item)["company"] for item in batch.rows] == [
        "AlphaCo",
        "BetaCo",
        "GammaCo",
        "DeltaCo",
        "EpsilonCo",
    ]


def test_one_failing_source_does_not_affect_the_others():
    batch = collect(mixed_config(CONCURRENT), delay=0.0)

    assert list(batch.errors) == [
        "BrokenCo: boom",
        "NoAdapterCo: no source registered for ats 'ashby'",
    ]
    succeeded = {
        attempt.company: attempt.succeeded
        for attempt in batch.source_attempts
        if attempt.source_kind == SOURCE_KIND_DIRECT and attempt.attempted
    }
    assert succeeded == {
        "AlphaCo": True,
        "BetaCo": True,
        "GammaCo": True,
        "DeltaCo": True,
        "EpsilonCo": True,
        "BrokenCo": False,
        "NoAdapterCo": False,
    }


# --- limit enforcement ---------------------------------------------------


def test_every_configured_limit_is_respected():
    probe = ScopeProbe()
    stats = CollectionStats()

    collect(mixed_config(CONCURRENT), stats=stats, probe=probe, delay=0.05)

    metrics = stats.collection_concurrency
    assert metrics is not None
    assert metrics.mode == COLLECTION_MODE_CONCURRENT
    assert metrics.max_observed_global <= CONCURRENT.max_workers
    assert metrics.max_observed_per_origin <= CONCURRENT.per_origin_max_concurrency
    assert metrics.max_observed_workday <= CONCURRENT.workday_max_concurrency
    assert metrics.limits_within_bounds() is True
    # Adapter-side observation, independent of the scheduler's own counters.
    assert probe.peak.get("greenhouse", 0) <= CONCURRENT.per_origin_max_concurrency
    assert probe.peak.get("workday", 0) <= CONCURRENT.workday_max_concurrency
    assert probe.peak.get("any", 0) <= CONCURRENT.max_workers


def test_per_origin_limit_binds_across_different_companies_on_one_host():
    probe = ScopeProbe()
    single_origin = WatcherConfig(
        companies=tuple(
            CompanyCfg(name=f"Shared{index}", ats="greenhouse", token=f"t{index}")
            for index in range(6)
        ),
        terms=("Summer 2027",),
        collection_concurrency=CollectionConcurrencyCfg(
            mode=COLLECTION_MODE_CONCURRENT,
            max_workers=6,
            workday_max_concurrency=1,
            per_origin_max_concurrency=2,
        ),
    )
    stats = CollectionStats()

    collect_batch(
        single_origin,
        direct_sources={"greenhouse": DelayedSource("greenhouse", probe=probe, delay=0.05)},
        github_source=DelayedGithub("https://example.test/feed.json", []),
        stats=stats,
        run_id="fixed-run",
    )

    assert probe.peak["greenhouse"] == 2
    assert stats.collection_concurrency.max_observed_per_origin == 2


def test_workday_limit_binds_across_distinct_tenants():
    probe = ScopeProbe()
    tenants = WatcherConfig(
        companies=tuple(
            CompanyCfg(
                name=f"Tenant{index}",
                ats="workday",
                token=f"tenant{index}",
                workday_shard="wd5",
                workday_site="Site",
            )
            for index in range(4)
        ),
        terms=("Summer 2027",),
        collection_concurrency=CollectionConcurrencyCfg(
            mode=COLLECTION_MODE_CONCURRENT,
            max_workers=4,
            workday_max_concurrency=1,
            per_origin_max_concurrency=2,
        ),
    )
    stats = CollectionStats()

    collect_batch(
        tenants,
        direct_sources={"workday": DelayedSource("workday", probe=probe, delay=0.05)},
        github_source=DelayedGithub("https://example.test/feed.json", []),
        stats=stats,
        run_id="fixed-run",
    )

    assert probe.peak["workday"] == 1
    assert stats.collection_concurrency.max_observed_workday == 1


def test_serial_mode_never_overlaps_fetches():
    probe = ScopeProbe()
    stats = CollectionStats()

    collect(mixed_config(SERIAL), stats=stats, probe=probe, delay=0.01)

    assert probe.peak.get("any", 0) == 1
    assert stats.collection_concurrency.mode == COLLECTION_MODE_SERIAL
    assert stats.collection_concurrency.max_observed_global == 1


# --- source safety rules -------------------------------------------------


def test_concurrent_default_adapters_share_one_workday_tenant_pacer():
    from watcher.run import _DirectSourceProvider

    provider = _DirectSourceProvider(None, concurrent=True)
    pacers = []
    barrier = threading.Barrier(3)

    def resolve():
        barrier.wait(timeout=5)
        pacers.append(provider.get("workday")._pacer)

    threads = [threading.Thread(target=resolve) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(pacers) == 3
    assert all(pacer is pacers[0] for pacer in pacers)
    assert isinstance(pacers[0], WorkdayPacer)
    assert provider.get("workday") is not None


def test_serial_collection_keeps_one_shared_adapter_set():
    from watcher.run import _DirectSourceProvider

    provider = _DirectSourceProvider(None, concurrent=False)

    assert provider.get("greenhouse") is provider.get("greenhouse")
    assert provider.supports("workday") is True
    assert provider.supports("bespoke") is False


def test_shared_workday_pacer_serializes_tenant_starts_across_threads():
    slept = []
    clock = {"now": 0.0}

    def sleeper(delay):
        slept.append(delay)
        clock["now"] += delay

    pacer = WorkdayPacer(
        min_interval_seconds=0.5,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    )
    results = []

    def start(index):
        results.append(pacer.wait_for_tenant(f"Tenant {index}"))

    threads = [threading.Thread(target=start, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 3
    assert sorted(results) == [0.0, 0.5, 0.5]
    assert sum(slept) == pytest.approx(1.0)
    telemetry = pacer.start_telemetry()
    assert telemetry.start_count == 3
    assert telemetry.minimum_spacing_seconds == pytest.approx(0.5)
    assert telemetry.median_spacing_seconds == pytest.approx(0.5)
    assert telemetry.maximum_spacing_seconds == pytest.approx(0.5)
    assert telemetry.pacing_violation_count == 0


def test_workday_start_is_recorded_after_pacing_and_before_request():
    clock = {"now": 0.0}
    request_start_counts = []

    def sleeper(delay):
        clock["now"] += delay

    pacer = WorkdayPacer(
        min_interval_seconds=0.5,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    )

    def request_json(_url, _payload, _adapter):
        telemetry = pacer.start_telemetry()
        request_start_counts.append((telemetry.start_count, clock["now"]))
        return {"jobPostings": [], "total": 0}

    source = WorkdaySource(pacer=pacer, request_json=request_json)
    first = CompanyCfg(
        name="First Tenant",
        ats="workday",
        token="first",
        workday_shard="wd5",
        workday_site="Careers",
    )
    second = CompanyCfg(
        name="Second Tenant",
        ats="workday",
        token="second",
        workday_shard="wd5",
        workday_site="Careers",
    )

    source.fetch(first)
    clock["now"] = 0.1
    source.fetch(second)

    assert request_start_counts == [(1, 0.0), (2, 0.5)]
    assert [item.company_identifier for item in pacer.start_records()] == [
        "First Tenant",
        "Second Tenant",
    ]


def test_workday_start_spacing_handles_zero_and_one_start():
    empty = summarize_workday_starts(0.5, ())
    one = summarize_workday_starts(
        0.5,
        (WorkdayStartRecord(company_identifier="Only Tenant", started_at=12.0),),
    )

    assert empty.start_count == 0
    assert empty.minimum_spacing_seconds is None
    assert empty.median_spacing_seconds is None
    assert empty.maximum_spacing_seconds is None
    assert empty.pacing_violation_count == 0
    assert empty.as_dict()["start_offsets"] == []
    assert one.start_count == 1
    assert one.minimum_spacing_seconds is None
    assert one.median_spacing_seconds is None
    assert one.maximum_spacing_seconds is None
    assert one.pacing_violation_count == 0
    assert one.as_dict()["start_offsets"] == [
        {
            "task_identifier": "workday-start-001",
            "company_identifier": "Only Tenant",
            "offset_seconds": 0.0,
        }
    ]


def test_workday_start_spacing_detects_violations_and_orders_deterministically():
    telemetry = summarize_workday_starts(
        0.5,
        (
            WorkdayStartRecord(company_identifier="Third Tenant", started_at=11.1),
            WorkdayStartRecord(company_identifier="https://user:secret@test/path", started_at=10.0),
            WorkdayStartRecord(company_identifier="Second Tenant", started_at=10.4),
            WorkdayStartRecord(company_identifier="Fourth Tenant", started_at=12.0),
        ),
    )

    assert telemetry.start_count == 4
    assert telemetry.minimum_spacing_seconds == pytest.approx(0.4)
    assert telemetry.median_spacing_seconds == pytest.approx(0.7)
    assert telemetry.maximum_spacing_seconds == pytest.approx(0.9)
    assert telemetry.pacing_violation_count == 1
    report = telemetry.as_dict()
    assert [item["task_identifier"] for item in report["start_offsets"]] == [
        "workday-start-001",
        "workday-start-002",
        "workday-start-003",
        "workday-start-004",
    ]
    assert [item["offset_seconds"] for item in report["start_offsets"]] == [
        0.0,
        0.4,
        1.1,
        2.0,
    ]
    assert report["start_offsets"][0]["company_identifier"].startswith("company-")
    assert "secret" not in json.dumps(report)
    assert "https" not in json.dumps(report)


def test_workday_pacer_does_not_hold_lock_while_sleeping():
    clock = {"now": 0.0}
    lock_was_available = []
    pacer = None

    def sleeper(delay):
        assert pacer is not None
        acquired = pacer._lock.acquire(blocking=False)
        lock_was_available.append(acquired)
        if acquired:
            pacer._lock.release()
        clock["now"] += delay

    pacer = WorkdayPacer(
        min_interval_seconds=0.5,
        sleeper=sleeper,
        clock=lambda: clock["now"],
    )
    pacer.wait_for_tenant("First Tenant")
    clock["now"] = 0.1

    pacer.wait_for_tenant("Second Tenant")

    assert lock_was_available == [True]


def test_blocked_and_rate_limited_responses_stay_ordinary_source_failures():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="RateLimited", ats="greenhouse", token="rl"),
            CompanyCfg(name="Forbidden", ats="lever", token="fb"),
        ),
        terms=("Summer 2027",),
        collection_concurrency=CONCURRENT,
    )
    stats = CollectionStats()

    batch = collect_batch(
        config,
        direct_sources={
            "greenhouse": DelayedSource(
                "greenhouse",
                delay=0.0,
                errors={
                    "RateLimited": SourceFetchError(
                        "greenhouse fetch failed with HTTP 429: https://boards-api.greenhouse.io/v1/boards/rl/jobs",
                        error_code="rate_limited",
                        status_code=429,
                        retryable=True,
                    )
                },
            ),
            "lever": DelayedSource(
                "lever",
                delay=0.0,
                errors={
                    "Forbidden": SourceFetchError(
                        "lever fetch failed with HTTP 403: https://api.lever.co/v0/postings/fb",
                        error_code="html_challenge",
                        status_code=403,
                        response_metadata={"body_kind": "html_challenge"},
                    )
                },
            ),
        },
        github_source=DelayedGithub("https://example.test/feed.json", []),
        stats=stats,
        run_id="fixed-run",
    )

    assert len(batch.errors) == 2
    assert stats.http_status_counts[429] == 1
    assert stats.http_status_counts[403] == 1
    assert stats.challenge_responses == 1
    assert stats.unexpected_task_exceptions == 0
    assert [attempt.succeeded for attempt in batch.source_attempts if attempt.attempted] == [
        False,
        False,
        True,
    ]


# --- downstream equivalence ---------------------------------------------


def _pipeline_fixture(tmp_path, concurrency, name):
    with SeenStore(tmp_path / f"{name}.sqlite") as store:
        result = run_once(
            mixed_config(concurrency),
            seen_store=store,
            direct_sources=mixed_sources(delay=0.0),
            github_source=DelayedGithub(
                "https://raw.githubusercontent.test/owner/repo/listings.json",
                [row("BackstopCo", "Software Engineer Intern", source="github")],
            ),
            alumni_index={},
            digest_sender=lambda matches: False,
            today=date(2026, 7, 31),
            run_id="fixed-run",
            health_observed_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        )
    fixture = {
        "jobs": result.jobs,
        "duplicates": result.duplicate_report,
        "matches": [job.get("id") for job in result.matches],
        "errors": result.errors,
        "rows_fetched": result.rows_fetched,
    }
    digest = hashlib.sha256(
        json.dumps(fixture, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return result, digest


def test_downstream_pipeline_outputs_are_identical_in_both_modes(tmp_path):
    serial_result, serial_digest = _pipeline_fixture(tmp_path, SERIAL, "serial")
    concurrent_result, concurrent_digest = _pipeline_fixture(tmp_path, CONCURRENT, "concurrent")

    assert concurrent_digest == serial_digest
    assert concurrent_result.collection_mode == COLLECTION_MODE_CONCURRENT
    assert serial_result.collection_mode == COLLECTION_MODE_SERIAL
    assert concurrent_result.health_summary == serial_result.health_summary
    assert concurrent_result.source_health_states == serial_result.source_health_states
    assert concurrent_result.collection_concurrency.executor_shutdown_clean is True
    assert concurrent_result.collection_concurrency.unexpected_exceptions == 0


def test_heartbeat_reports_collection_mode_and_observed_concurrency(tmp_path, capsys):
    from watcher.run import print_heartbeat

    result, _digest = _pipeline_fixture(tmp_path, CONCURRENT, "heartbeat")
    print_heartbeat(result)

    line = capsys.readouterr().out
    assert "collection_mode=concurrent" in line
    assert "collection_max_workers=4" in line
    assert "collection_max_observed_concurrency=" in line
    assert "collection_max_observed_origin_concurrency=" in line
    assert "collection_max_observed_workday_concurrency=" in line
    assert "collection_unexpected_task_exceptions=0" in line


def test_observer_reports_leaked_scopes_and_peaks():
    observer = ConcurrencyObserver()
    task = CollectionTask(key="t", origin="o", provider="p", run=lambda: None)

    observer.start(task)
    assert observer.peak("global") == 1
    assert observer.leaked_scopes()
    observer.finish(task)

    assert observer.leaked_scopes() == ()
    assert observer.peaks_by_prefix("origin:") == (("o", 1),)


def test_scheduler_metrics_accumulate_across_phases():
    scheduler = CollectionScheduler(CONCURRENT)
    scheduler.run(
        [CollectionTask(key="a", origin="o1", provider="p", run=lambda: 1)]
    )
    scheduler.run(
        [CollectionTask(key="b", origin="o2", provider="p", run=lambda: 2)]
    )

    metrics = scheduler.metrics()

    assert metrics.tasks_total == 2
    assert metrics.tasks_failed == 0
    assert metrics.executor_shutdown_clean is True
    assert metrics.as_dict()["mode"] == COLLECTION_MODE_CONCURRENT
