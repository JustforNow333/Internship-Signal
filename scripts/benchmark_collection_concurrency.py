#!/usr/bin/env python3
"""Stage 1 deterministic offline benchmark for bounded collection concurrency.

This stage never touches the network, production state, alumni data, email, the
seen store, or `watcher-data`. It drives controlled fake delayed sources through
the real collection layer and verifies, in one run:

* exact serial/concurrent collection batch equivalence,
* exact downstream fixture equivalence through the unchanged pipeline,
* ordering invariants (source priority, attempt order, error order),
* every configured concurrency limit,
* failure isolation for source failures and worker programming errors,
* clean executor shutdown.

Usage:

    PYTHONPATH=.:backend python3 scripts/benchmark_collection_concurrency.py \
        --companies 40 --delay 0.05 --output evaluation/private/stage1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(1, str(REPO_ROOT / "backend"))

from watcher.collection_snapshot import save_collection_snapshot  # noqa: E402
from watcher.config import (  # noqa: E402
    COLLECTION_MODE_CONCURRENT,
    COLLECTION_MODE_SERIAL,
    CollectionConcurrencyCfg,
    CompanyCfg,
    GitHubListingSourceCfg,
    WatcherConfig,
)
from watcher.run import (  # noqa: E402
    RUN_MODE_DRY,
    CollectionStats,
    collect_batch,
    run_once,
)
from watcher.seen_store import SeenStore  # noqa: E402
from watcher.sources.base import SourceError, SourceFetchError, make_row  # noqa: E402
from watcher.sources.workday import WorkdayPacer  # noqa: E402

FIXED_DATE = date(2026, 7, 31)
FIXED_OBSERVED_AT = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
ADAPTERS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "workday")
SHARDS = ("wd1", "wd5", "wd103")


class ScopeProbe:
    """Adapter-side peak concurrency, measured independently of the scheduler."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, int] = {}
        self.peak: dict[str, int] = {}

    def enter(self, *scopes: str) -> None:
        with self._lock:
            for scope in scopes:
                value = self._current.get(scope, 0) + 1
                self._current[scope] = value
                self.peak[scope] = max(self.peak.get(scope, 0), value)

    def exit(self, *scopes: str) -> None:
        with self._lock:
            for scope in scopes:
                self._current[scope] = max(0, self._current.get(scope, 0) - 1)


class FakeDelayedSource:
    """Controlled adapter: fixed delay, deterministic rows, injected failures."""

    def __init__(
        self,
        adapter: str,
        delay: float,
        probe: ScopeProbe,
        *,
        pacer: WorkdayPacer | None = None,
    ) -> None:
        self.adapter = adapter
        self.delay = delay
        self.probe = probe
        self._pacer = pacer

    def fetch(self, company: CompanyCfg) -> list[dict]:
        if self._pacer is not None:
            self._pacer.wait_for_tenant(company.name)
        # Distinct scope labels: a shared ATS host is one origin, while each
        # Workday tenant is its own host under the shared Workday provider.
        origin = (
            f"origin:{company.token}.{company.workday_shard}.myworkdayjobs.com"
            if self.adapter == "workday"
            else f"origin:{self.adapter}"
        )
        self.probe.enter("any", f"provider:{self.adapter}", origin)
        try:
            time.sleep(self.delay)
            if company.name.endswith("-source-failure"):
                raise SourceError("controlled source failure")
            if company.name.endswith("-fetch-failure"):
                raise SourceFetchError(
                    f"{self.adapter} fetch failed with HTTP 429: "
                    f"https://{self.adapter}.test/{company.token}",
                    error_code="rate_limited",
                    status_code=429,
                    retryable=True,
                )
            if company.name.endswith("-worker-bug"):
                raise TypeError("controlled worker programming error")
            return [
                make_row(
                    source="direct",
                    source_adapter=self.adapter,
                    company=company.name,
                    title=f"{title} Intern",
                    location="New York, NY",
                    description="Build Python APIs and React interfaces.",
                    requirements="Python, SQL, REST APIs, Git",
                    source_url=(
                        f"https://{self.adapter}.test/{company.token}/"
                        f"{title.lower().replace(' ', '-')}"
                    ),
                    internship_type="Summer",
                )
                for title in ("Software Engineer", "Backend Software")
            ]
        finally:
            self.probe.exit("any", f"provider:{self.adapter}", origin)


class FakeDelayedFeed:
    def __init__(self, url: str, delay: float, probe: ScopeProbe, rows: list[dict]) -> None:
        self.url = url
        self.feed_label = url
        self.delay = delay
        self.probe = probe
        self.rows = rows

    def fetch_many(self, _companies) -> list[dict]:
        self.probe.enter("any", "provider:github_feed", "origin:raw.githubusercontent.test")
        try:
            time.sleep(self.delay)
            return list(self.rows)
        finally:
            self.probe.exit("any", "provider:github_feed", "origin:raw.githubusercontent.test")


def build_config(count: int, concurrency: CollectionConcurrencyCfg) -> WatcherConfig:
    companies: list[CompanyCfg] = []
    for index in range(count):
        adapter = ADAPTERS[index % len(ADAPTERS)]
        suffix = ""
        if index % 11 == 5:
            suffix = "-source-failure"
        elif index % 13 == 7:
            suffix = "-fetch-failure"
        elif index % 17 == 9:
            suffix = "-worker-bug"
        name = f"Company{index:03d}{suffix}"
        if adapter == "workday":
            companies.append(
                CompanyCfg(
                    name=name,
                    ats="workday",
                    token=f"tenant{index:03d}",
                    workday_shard=SHARDS[index % len(SHARDS)],
                    workday_site="Careers",
                )
            )
        else:
            companies.append(
                CompanyCfg(name=name, ats=adapter, token=f"token{index:03d}")
            )
    companies.append(CompanyCfg(name="BespokeCo", ats="bespoke"))
    companies.append(CompanyCfg(name="BackstopOnlyCo", ats="github_only"))
    return WatcherConfig(
        companies=tuple(companies),
        terms=("Summer 2027",),
        github_listing_sources=(
            GitHubListingSourceCfg(
                name="fixture_simplify",
                format="simplify_json",
                url="https://raw.githubusercontent.test/owner/repo/listings.json",
            ),
            GitHubListingSourceCfg(
                name="fixture_markdown",
                format="github_markdown_table",
                url="https://raw.githubusercontent.test/owner/repo/README.md",
                default_term="Summer 2027",
            ),
        ),
        analysis_cache_enabled=False,
        collection_concurrency=concurrency,
    )


def build_sources(delay: float, probe: ScopeProbe) -> dict[str, object]:
    workday_pacer = WorkdayPacer(min(0.01, max(0.0, delay)))
    return {
        adapter: FakeDelayedSource(
            adapter,
            delay,
            probe,
            pacer=workday_pacer if adapter == "workday" else None,
        )
        for adapter in ADAPTERS
    }


def build_feeds(delay: float, probe: ScopeProbe) -> list[FakeDelayedFeed]:
    backstop_row = make_row(
        source="github",
        source_adapter="github_listings",
        company="BackstopOnlyCo",
        title="Software Engineering Intern",
        location="Remote",
        description="Build Python services.",
        requirements="Python, Git",
        source_url="https://example.test/backstop/swe-intern",
        internship_type="Summer",
    )
    return [
        FakeDelayedFeed(
            "https://raw.githubusercontent.test/owner/repo/listings.json",
            delay,
            probe,
            [backstop_row],
        ),
        FakeDelayedFeed(
            "https://raw.githubusercontent.test/owner/repo/README.md",
            delay,
            probe,
            [],
        ),
    ]


def digest(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def batch_fixture(batch) -> dict[str, object]:
    return {
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
            }
            for attempt in batch.source_attempts
        ],
        "github_feeds": [batch.github_feeds_configured, batch.github_feeds_succeeded],
        "workday": [
            batch.workday_attempted,
            batch.workday_succeeded,
            batch.workday_failed,
            batch.workday_request_attempts,
            batch.workday_retry_attempts,
            list(batch.workday_failure_codes),
        ],
    }


def run_collection(config: WatcherConfig, delay: float) -> dict[str, object]:
    probe = ScopeProbe()
    stats = CollectionStats()
    started = time.perf_counter()
    batch = collect_batch(
        config,
        direct_sources=build_sources(delay, probe),
        github_source=build_feeds(delay, probe),
        stats=stats,
        run_id="stage1-fixed-run",
        observed_at=FIXED_OBSERVED_AT,
        captured_at=FIXED_OBSERVED_AT,
    )
    elapsed = time.perf_counter() - started
    return {
        "batch": batch,
        "stats": stats,
        "probe": probe,
        "elapsed_seconds": round(elapsed, 3),
    }


def run_pipeline(
    config: WatcherConfig,
    batch,
    workdir: Path,
    label: str,
) -> dict[str, object]:
    with SeenStore(workdir / f"{label}-seen.sqlite") as store:
        state_before = _operational_state_rows(store.path)
        result = run_once(
            config,
            seen_store=store,
            collection_batch=batch,
            alumni_index={},
            digest_sender=lambda _matches: False,
            notification_mode=RUN_MODE_DRY,
            today=FIXED_DATE,
            run_id="stage1-fixed-run",
            health_observed_at=FIXED_OBSERVED_AT,
        )
        state_after = _operational_state_rows(store.path)
    fixture = {
        "jobs": result.jobs,
        "duplicate_report": result.duplicate_report,
        "matches": [job.get("id") for job in result.matches],
        "new_matches": [job.get("id") for job in result.new_matches],
        "errors": result.errors,
        "rows_fetched": result.rows_fetched,
        "jobs_scored": result.jobs_scored,
        "comparison_counts": dict(result.source_comparison.counts)
        if result.source_comparison
        else {},
    }
    return {
        "result": result,
        "fixture_digest": digest(fixture),
        "seen_rows": _seen_rows(workdir / f"{label}-seen.sqlite"),
        "operational_state_unchanged": state_before == state_after,
        "operational_state_before": state_before,
        "operational_state_after": state_after,
    }


def _seen_rows(path: Path) -> int:
    import sqlite3

    if not path.exists():
        return 0
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("select count(*) from seen").fetchone()[0])
    finally:
        connection.close()


def _operational_state_rows(path: Path) -> dict[str, int]:
    """Count every durable table except replay-permitted analysis cache rows."""

    import sqlite3

    connection = sqlite3.connect(path)
    try:
        table_names = [
            str(row[0])
            for row in connection.execute(
                "select name from sqlite_master "
                "where type = 'table' and name not like 'sqlite_%' "
                "order by name"
            )
            if str(row[0]) != "analysis_cache"
        ]
        return {
            table: int(connection.execute(f'select count(*) from "{table}"').fetchone()[0])
            for table in table_names
        }
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies", type=int, default=40)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--workday-concurrency", type=int, default=1)
    parser.add_argument("--per-origin-concurrency", type=int, default=2)
    parser.add_argument("--output", help="Write the JSON report to this path.")
    args = parser.parse_args(argv)

    serial_cfg = CollectionConcurrencyCfg(mode=COLLECTION_MODE_SERIAL)
    concurrent_cfg = CollectionConcurrencyCfg(
        mode=COLLECTION_MODE_CONCURRENT,
        max_workers=args.max_workers,
        workday_max_concurrency=args.workday_concurrency,
        per_origin_max_concurrency=args.per_origin_concurrency,
    )
    serial_config = build_config(args.companies, serial_cfg)
    concurrent_config = build_config(args.companies, concurrent_cfg)

    serial = run_collection(serial_config, args.delay)
    concurrent = run_collection(concurrent_config, args.delay)

    serial_fixture = batch_fixture(serial["batch"])
    concurrent_fixture = batch_fixture(concurrent["batch"])

    with TemporaryDirectory(
        prefix="stage1-collection-concurrency-",
        ignore_cleanup_errors=True,
    ) as raw_dir:
        workdir = Path(raw_dir)
        save_collection_snapshot(serial["batch"], workdir / "serial.json.gz")
        save_collection_snapshot(concurrent["batch"], workdir / "concurrent.json.gz")
        serial_bytes = (workdir / "serial.json.gz").read_bytes()
        concurrent_bytes = (workdir / "concurrent.json.gz").read_bytes()
        serial_pipeline = run_pipeline(
            serial_config,
            serial["batch"],
            workdir,
            "serial",
        )
        concurrent_pipeline = run_pipeline(
            concurrent_config,
            concurrent["batch"],
            workdir,
            "concurrent",
        )

    metrics = concurrent["stats"].collection_concurrency
    workday_start_telemetry = concurrent["stats"].workday_start_telemetry
    probe = concurrent["probe"]
    serial_probe = serial["probe"]
    direct_attempts = [
        attempt
        for attempt in concurrent["batch"].source_attempts
        if attempt.source_kind == "direct"
    ]
    checks = {
        "batch_equivalence": digest(concurrent_fixture) == digest(serial_fixture),
        "snapshot_byte_equivalence": concurrent_bytes == serial_bytes,
        "downstream_fixture_equivalence": (
            concurrent_pipeline["fixture_digest"] == serial_pipeline["fixture_digest"]
        ),
        "row_order_preserved": [dict(row) for row in concurrent["batch"].rows]
        == [dict(row) for row in serial["batch"].rows],
        "attempt_order_preserved": [
            (attempt.company, attempt.adapter)
            for attempt in concurrent["batch"].source_attempts
        ]
        == [
            (attempt.company, attempt.adapter)
            for attempt in serial["batch"].source_attempts
        ],
        "error_order_preserved": list(concurrent["batch"].errors)
        == list(serial["batch"].errors),
        "global_limit_respected": probe.peak.get("any", 0) <= args.max_workers,
        "per_origin_limit_respected": all(
            value <= args.per_origin_concurrency
            for scope, value in probe.peak.items()
            if scope.startswith("origin:")
        ),
        "provider_limit_respected": all(
            value <= args.per_origin_concurrency
            for scope, value in probe.peak.items()
            if scope.startswith("provider:")
        ),
        "workday_limit_respected": probe.peak.get("provider:workday", 0)
        <= args.workday_concurrency,
        "scheduler_limits_within_bounds": bool(metrics and metrics.limits_within_bounds()),
        "serial_mode_never_overlaps": serial_probe.peak.get("any", 0) == 1,
        "failure_isolation": (
            len(concurrent["batch"].errors) > 0
            and sum(1 for attempt in direct_attempts if attempt.succeeded) > 0
        ),
        "worker_programming_errors_isolated": (
            concurrent["stats"].unexpected_task_exceptions == 0
            and any(
                attempt.error_kind == "unexpected_exception"
                for attempt in direct_attempts
            )
        ),
        "executor_shutdown_clean": bool(metrics and metrics.executor_shutdown_clean),
        "zero_workday_pacing_violations": bool(
            workday_start_telemetry
            and workday_start_telemetry.pacing_violation_count == 0
        ),
        "no_seen_rows_written": (
            serial_pipeline["seen_rows"] == 0 and concurrent_pipeline["seen_rows"] == 0
        ),
        "zero_operational_state_writes": (
            serial_pipeline["operational_state_unchanged"]
            and concurrent_pipeline["operational_state_unchanged"]
        ),
        "no_email_sent": (
            serial_pipeline["result"].digest_sent is False
            and concurrent_pipeline["result"].digest_sent is False
        ),
    }

    report = {
        "stage": "1_deterministic_offline_benchmark",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "network_used": False,
        "companies_configured": len(serial_config.companies),
        "per_source_delay_seconds": args.delay,
        "configured_limits": concurrent_cfg.as_dict(),
        "serial": {
            "mode": COLLECTION_MODE_SERIAL,
            "elapsed_seconds": serial["elapsed_seconds"],
            "rows": len(serial["batch"].rows),
            "errors": len(serial["batch"].errors),
            "max_observed_adapter_concurrency": serial_probe.peak.get("any", 0),
        },
        "concurrent": {
            "mode": COLLECTION_MODE_CONCURRENT,
            "elapsed_seconds": concurrent["elapsed_seconds"],
            "rows": len(concurrent["batch"].rows),
            "errors": len(concurrent["batch"].errors),
            "max_observed_global": metrics.max_observed_global if metrics else 0,
            "max_observed_per_origin": metrics.max_observed_per_origin if metrics else 0,
            "max_observed_provider": metrics.max_observed_provider if metrics else 0,
            "max_observed_workday": metrics.max_observed_workday if metrics else 0,
            "adapter_observed_peaks": dict(sorted(probe.peak.items())),
            "unexpected_task_exceptions": concurrent["stats"].unexpected_task_exceptions,
            "http_status_counts": {
                str(status): count
                for status, count in sorted(concurrent["stats"].http_status_counts.items())
            },
            "challenge_responses": concurrent["stats"].challenge_responses,
            "executor_shutdown_clean": metrics.executor_shutdown_clean if metrics else False,
            "workday_start_telemetry": (
                workday_start_telemetry.as_dict()
                if workday_start_telemetry is not None
                else None
            ),
        },
        "speedup_ratio": round(
            (serial["elapsed_seconds"] / concurrent["elapsed_seconds"])
            if concurrent["elapsed_seconds"]
            else 0.0,
            2,
        ),
        "digests": {
            "serial_batch": digest(serial_fixture),
            "concurrent_batch": digest(concurrent_fixture),
            "serial_pipeline_fixture": serial_pipeline["fixture_digest"],
            "concurrent_pipeline_fixture": concurrent_pipeline["fixture_digest"],
        },
        "operational_state": {
            "serial_before": serial_pipeline["operational_state_before"],
            "serial_after": serial_pipeline["operational_state_after"],
            "concurrent_before": concurrent_pipeline["operational_state_before"],
            "concurrent_after": concurrent_pipeline["operational_state_after"],
        },
        "checks": checks,
        "passed": all(checks.values()),
        "production_default": "Production default remains serial; concurrent mode is available for controlled canaries.",
    }

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    for name, passed in sorted(checks.items()):
        print(f"{'PASS' if passed else 'FAIL'} {name}", file=sys.stderr)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
