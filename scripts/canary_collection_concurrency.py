#!/usr/bin/env python3
"""Staged live canary for opt-in bounded collection concurrency.

Stage 2 (`--stage limited`) runs a small representative allowlist across the
supported adapter types, including at most one or two Workday tenants. Stage 3
(`--stage full`) runs the complete configured source set once the limited canary
has passed.

Every canary is collection-only and operationally isolated:

* temporary seen database and temporary analysis-cache database (never opened),
* internship email disabled, priming disabled, seen marking never invoked,
* health-alert delivery disabled, durable health persistence not performed,
* source-comparison persistence not performed,
* production SQLite state fingerprinted before and after and reported.

A source that answers 401, 403, 429, a challenge response, or repeated transport
failures is recorded and removed from the rest of the canary; it is never
retried immediately. Adapter pacing, timeouts, retries, and backoff are
unchanged, and no proxy, cookie, header-rotation, or challenge-bypass behavior
exists anywhere in this path.

Baseline rule: do not run a fresh full serial collection immediately before or
after a full concurrent canary. Compare against a recent normal serial
production run, existing serial timing logs, or a serial run performed in a
separate normal collection window.

Usage:

    PYTHONPATH=.:backend python3 scripts/canary_collection_concurrency.py \
        --stage limited --output evaluation/private/canary-limited.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "backend") not in sys.path:
    sys.path.insert(1, str(REPO_ROOT / "backend"))

_CANARY_TEMP_DIR = Path(tempfile.mkdtemp(prefix="watcher-collection-canary-"))
# Set before importing watcher configuration so no default resolves to
# production state, and so no code path can enable email or priming.
os.environ["WATCHER_SEND_EMAIL"] = "0"
os.environ["WATCHER_HEALTH_EMAIL_MODE"] = "off"
os.environ["WATCHER_PRIME_SEEN"] = "0"
os.environ["WATCHER_SEEN_DB"] = str(_CANARY_TEMP_DIR / "canary-seen.sqlite")
os.environ["WATCHER_ANALYSIS_CACHE_PATH"] = str(
    _CANARY_TEMP_DIR / "canary-analysis-cache.sqlite"
)

from watcher.collection_snapshot import save_collection_snapshot  # noqa: E402
from watcher.config import (  # noqa: E402
    COLLECTION_MODE_CONCURRENT,
    COLLECTION_MODE_SERIAL,
    DEFAULT_WATCHLIST_PATH,
    CollectionConcurrencyCfg,
    CompanyCfg,
    WatcherConfig,
    load_watchlist,
    workday_min_interval_seconds,
)
from watcher.notify import email_sending_enabled  # noqa: E402
from watcher.run import CollectionStats, collect_batch  # noqa: E402
from watcher.source_health import SOURCE_KIND_DIRECT  # noqa: E402
from watcher.sources.workday import summarize_workday_starts  # noqa: E402

LOGGER = logging.getLogger("watcher.canary")
DEFAULT_STATE_FILE = REPO_ROOT / "evaluation" / "private" / "collection-canary-state.json"
PRODUCTION_STATE_PATHS = (
    REPO_ROOT / "watcher" / "seen.sqlite",
    REPO_ROOT / "watcher" / "analysis-cache.sqlite",
    REPO_ROOT / ".watcher-state" / "seen.sqlite",
)
BLOCKED_STATUSES = (401, 403, 429)
BLOCKED_ERROR_CODES = frozenset(
    {"rate_limited", "html_challenge", "redirected_to_html"}
)
TRANSPORT_ERROR_CODES = frozenset(
    {"network_failure", "timeout", "dns_failure", "connection_reset", "empty_response"}
)
FULL_RUN_MIN_INTERVAL_MINUTES = 60


def fingerprint_production_state() -> dict[str, str]:
    """Hash durable operational databases so any change is provable."""

    fingerprints: dict[str, str] = {}
    for path in PRODUCTION_STATE_PATHS:
        if not path.exists():
            fingerprints[str(path)] = "absent"
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        fingerprints[str(path)] = f"{path.stat().st_size}:{digest}"
    return fingerprints


def select_companies(
    config: WatcherConfig,
    *,
    stage: str,
    allowlist: tuple[str, ...],
    max_workday: int,
    max_sources: int | None,
    blocked: set[str],
) -> tuple[CompanyCfg, ...]:
    fetchable = [
        company
        for company in config.companies
        if company.ats not in {"bespoke", "github_only"}
        and company.name not in blocked
    ]
    if allowlist:
        wanted = {name.casefold() for name in allowlist}
        selected = [
            company for company in fetchable if company.name.casefold() in wanted
        ]
        missing = wanted - {company.name.casefold() for company in selected}
        if missing:
            raise SystemExit(
                "Allowlist entries are not fetchable configured companies: "
                + ", ".join(sorted(missing))
            )
    elif stage == "limited":
        selected = _representative_sample(fetchable, max_workday=max_workday)
    else:
        selected = list(fetchable)

    if stage == "limited":
        workday = [company for company in selected if company.ats == "workday"]
        if len(workday) > max_workday:
            keep = {company.name for company in workday[:max_workday]}
            selected = [
                company
                for company in selected
                if company.ats != "workday" or company.name in keep
            ]
        if max_sources is not None and len(selected) > max_sources:
            selected = selected[:max_sources]
    return tuple(selected)


def _representative_sample(
    companies: list[CompanyCfg],
    *,
    max_workday: int,
) -> list[CompanyCfg]:
    """One company per adapter type, in configuration order."""

    seen_adapters: set[str] = set()
    selected: list[CompanyCfg] = []
    for company in companies:
        if company.ats in seen_adapters:
            continue
        if company.ats == "workday" and max_workday <= 0:
            continue
        seen_adapters.add(company.ats)
        selected.append(company)
    return selected


def classify_attempt(attempt) -> dict[str, object]:
    """Classify one recorded attempt without inspecting raw response bodies."""

    error_kind = str(attempt.error_kind or "")
    message = str(attempt.error_message or "")
    status = None
    for candidate in BLOCKED_STATUSES:
        if f"HTTP {candidate}" in message or f"status={candidate}" in message:
            status = candidate
            break
    code = error_kind.split("/", 1)[1] if "/" in error_kind else ""
    challenge = code in {"html_challenge", "redirected_to_html"} or "challenge" in message
    blocked_reason = ""
    if status in BLOCKED_STATUSES:
        blocked_reason = f"http_{status}"
    elif code in BLOCKED_ERROR_CODES or challenge:
        blocked_reason = code or "challenge"
    transport = code in TRANSPORT_ERROR_CODES
    if attempt.succeeded:
        outcome = "success" if (attempt.rows_returned or 0) > 0 else "empty"
    elif attempt.attempted:
        outcome = "failure"
    else:
        outcome = "unsupported"
    return {
        "label": attempt.company or attempt.feed_label or attempt.health_key,
        "adapter": attempt.adapter,
        "kind": attempt.source_kind,
        "outcome": outcome,
        "rows": int(attempt.rows_returned or 0),
        "error_kind": error_kind,
        "status": status,
        "challenge": bool(challenge),
        "transport_failure": bool(transport),
        "blocked_reason": blocked_reason,
        "unexpected_exception": error_kind == "unexpected_exception",
    }


def summarize_run(batch, stats: CollectionStats, elapsed: float) -> dict[str, object]:
    classified = [classify_attempt(attempt) for attempt in batch.source_attempts]
    attempted = [item for item in classified if item["outcome"] != "unsupported"]
    metrics = stats.collection_concurrency
    status_counts = Counter(
        item["status"] for item in attempted if item["status"] is not None
    )
    start_telemetry = stats.workday_start_telemetry or summarize_workday_starts(
        workday_min_interval_seconds(), ()
    )
    attempt_unexpected_exceptions = sum(
        1 for item in attempted if item["unexpected_exception"]
    )
    return {
        "elapsed_seconds": round(elapsed, 3),
        "rows_collected": len(batch.rows),
        "sources_attempted": len(attempted),
        "sources_successful": sum(1 for item in attempted if item["outcome"] == "success"),
        "sources_empty": sum(1 for item in attempted if item["outcome"] == "empty"),
        "sources_failed": sum(1 for item in attempted if item["outcome"] == "failure"),
        "http_401": int(status_counts.get(401, 0)),
        "http_403": int(status_counts.get(403, 0)),
        "http_429": int(status_counts.get(429, 0)),
        "challenge_responses": sum(1 for item in attempted if item["challenge"]),
        "transport_failures": sum(1 for item in attempted if item["transport_failure"]),
        # Escaped worker exceptions are also reduced to failed source attempts;
        # use the larger count so the same failure is not counted twice.
        "unexpected_exceptions": max(
            attempt_unexpected_exceptions,
            stats.unexpected_task_exceptions,
        ),
        "escaped_worker_exceptions": stats.unexpected_task_exceptions,
        "workday_tenants_attempted": batch.workday_attempted,
        "workday_tenants_succeeded": batch.workday_succeeded,
        "workday_tenants_failed": batch.workday_failed,
        "workday_request_attempts": batch.workday_request_attempts,
        "workday_retry_attempts": batch.workday_retry_attempts,
        "workday_failure_codes": {code: count for code, count in batch.workday_failure_codes},
        "workday_start_telemetry": start_telemetry.as_dict(),
        "github_feeds_configured": batch.github_feeds_configured,
        "github_feeds_succeeded": batch.github_feeds_succeeded,
        "rows_by_source": {
            str(item["label"]): item["rows"]
            for item in attempted
            if item["kind"] == SOURCE_KIND_DIRECT or item["rows"]
        },
        "observed_concurrency": {
            "max_global": metrics.max_observed_global if metrics else 0,
            "max_per_origin": metrics.max_observed_per_origin if metrics else 0,
            "max_provider": metrics.max_observed_provider if metrics else 0,
            "max_workday": metrics.max_observed_workday if metrics else 0,
            "busiest_origin": metrics.busiest_origin if metrics else "",
        },
        "limits_within_bounds": bool(metrics and metrics.limits_within_bounds()),
        "executor_shutdown_clean": bool(metrics and metrics.executor_shutdown_clean),
        "errors": list(batch.errors),
        "sources": attempted,
        "blocked_sources": [
            {"label": item["label"], "reason": item["blocked_reason"]}
            for item in attempted
            if item["blocked_reason"]
        ],
        # Transport failures are ordinary network variability, never evidence of
        # blocking, but the source is still not retried inside this canary.
        "paused_sources": [
            {"label": item["label"], "reason": item["error_kind"] or "transport_failure"}
            for item in attempted
            if item["transport_failure"] and not item["blocked_reason"]
        ],
    }


def load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("limited", "full"), default="limited")
    parser.add_argument(
        "--mode",
        choices=(COLLECTION_MODE_CONCURRENT, COLLECTION_MODE_SERIAL),
        default=COLLECTION_MODE_CONCURRENT,
    )
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH))
    parser.add_argument("--allowlist", default="", help="Comma-separated company names.")
    parser.add_argument("--allowlist-file", help="One company name per line.")
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--workday-concurrency", type=int, default=1)
    parser.add_argument("--per-origin-concurrency", type=int, default=2)
    parser.add_argument("--max-workday", type=int, default=1, help="Limited stage only.")
    parser.add_argument("--max-sources", type=int, default=6, help="Limited stage only.")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--include-feeds",
        action="store_true",
        help="Also fetch the configured GitHub backstop feeds (default for --stage full).",
    )
    parser.add_argument("--capture-snapshot", help="Optional .json.gz snapshot path.")
    parser.add_argument("--output", help="Write the JSON canary report to this path.")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    parser.add_argument(
        "--allow-serial-full-run",
        action="store_true",
        help=(
            "Acknowledge that a full serial run is being performed in its own "
            "normal collection window, not as a back-to-back comparison."
        ),
    )
    parser.add_argument(
        "--override-interval-guard",
        action="store_true",
        help="Bypass the one-collection-interval guard between full live runs.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if email_sending_enabled():
        raise SystemExit("Refusing to run: internship email sending is enabled.")
    if args.stage == "full" and args.mode == COLLECTION_MODE_SERIAL and not args.allow_serial_full_run:
        raise SystemExit(
            "Full serial collections must not be run back-to-back with a concurrent "
            "canary. Use a recent normal serial production run, existing serial "
            "timing logs, or pass --allow-serial-full-run for a separate window."
        )

    state_path = Path(args.state_file)
    state = load_state(state_path)
    now = datetime.now(timezone.utc)
    if args.stage == "full" and not args.override_interval_guard:
        previous = str(state.get("last_full_run_at") or "")
        if previous:
            try:
                last = datetime.fromisoformat(previous)
            except ValueError:
                last = None
            if last is not None and now - last < timedelta(
                minutes=FULL_RUN_MIN_INTERVAL_MINUTES
            ):
                raise SystemExit(
                    "A full live canary ran at "
                    f"{previous}; leave at least one normal collection interval "
                    f"({FULL_RUN_MIN_INTERVAL_MINUTES} minutes) between full live runs."
                )

    concurrency = CollectionConcurrencyCfg(
        mode=args.mode,
        max_workers=args.max_workers,
        workday_max_concurrency=args.workday_concurrency,
        per_origin_max_concurrency=args.per_origin_concurrency,
    )
    base_config = load_watchlist(args.watchlist)
    allowlist: list[str] = [
        name.strip() for name in args.allowlist.split(",") if name.strip()
    ]
    if args.allowlist_file:
        allowlist.extend(
            line.strip()
            for line in Path(args.allowlist_file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        )

    include_feeds = args.include_feeds or args.stage == "full"
    blocked: set[str] = set(state.get("blocked_sources", {}) or {})
    before_state = fingerprint_production_state()
    runs: list[dict[str, object]] = []
    blocked_this_canary: dict[str, str] = {}
    paused_this_canary: dict[str, str] = {}

    for run_index in range(1, max(1, args.runs) + 1):
        companies = select_companies(
            base_config,
            stage=args.stage,
            allowlist=tuple(allowlist),
            max_workday=args.max_workday if args.stage == "limited" else len(base_config.companies),
            max_sources=args.max_sources if args.stage == "limited" else None,
            blocked=blocked,
        )
        if not companies:
            LOGGER.warning("No eligible sources remain for run %d; stopping.", run_index)
            break
        config = replace(
            base_config,
            companies=companies,
            collection_concurrency=concurrency,
            github_listing_sources=(
                base_config.github_listing_sources if include_feeds else ()
            ),
            github_listing_urls=(
                base_config.github_listing_urls if include_feeds else ()
            ),
        )
        LOGGER.info(
            "Canary run %d/%d: stage=%s mode=%s sources=%d feeds=%s",
            run_index,
            args.runs,
            args.stage,
            concurrency.mode,
            len(companies),
            "yes" if include_feeds else "no",
        )
        stats = CollectionStats()
        started = time.perf_counter()
        batch = collect_batch(config, stats=stats)
        elapsed = time.perf_counter() - started
        summary = summarize_run(batch, stats, elapsed)
        summary["run"] = run_index
        summary["sources_configured"] = [company.name for company in companies]
        runs.append(summary)
        for entry in summary["blocked_sources"]:
            label = str(entry["label"])
            blocked.add(label)
            blocked_this_canary[label] = str(entry["reason"])
            LOGGER.warning(
                "Recorded blocked source %s (%s); it will not be tested again in "
                "this canary.",
                label,
                entry["reason"],
            )
        for entry in summary["paused_sources"]:
            label = str(entry["label"])
            blocked.add(label)
            paused_this_canary[label] = str(entry["reason"])
            LOGGER.warning(
                "Paused source %s after repeated transport failures (%s); it will "
                "not be retried in this canary.",
                label,
                entry["reason"],
            )
        if args.capture_snapshot and run_index == 1:
            save_collection_snapshot(batch, args.capture_snapshot)

    after_state = fingerprint_production_state()
    production_state_changed = before_state != after_state
    failure_conditions = {
        "blocked_or_challenged_source": bool(blocked_this_canary),
        "http_401_or_403_present": any(
            run["http_401"] or run["http_403"] for run in runs
        ),
        "http_429_present": any(run["http_429"] for run in runs),
        "unexpected_exception": any(run["unexpected_exceptions"] for run in runs),
        "limits_exceeded": any(not run["limits_within_bounds"] for run in runs),
        "executor_shutdown_unclean": any(
            not run["executor_shutdown_clean"] for run in runs
        ),
        "workday_pacing_violation": any(
            int(run["workday_start_telemetry"]["pacing_violation_count"]) > 0
            for run in runs
        ),
        "production_state_modified": production_state_changed,
        "no_runs_completed": not runs,
    }
    report = {
        "stage": f"{'2_limited' if args.stage == 'limited' else '3_full'}_live_canary",
        "generated_at": now.isoformat(),
        "mode": concurrency.mode,
        "configured_limits": concurrency.as_dict(),
        "include_github_feeds": include_feeds,
        "isolation": {
            "seen_database": os.environ["WATCHER_SEEN_DB"],
            "analysis_cache_database": os.environ["WATCHER_ANALYSIS_CACHE_PATH"],
            "internship_email": "disabled",
            "priming": "disabled",
            "seen_marking": "not invoked (collection only)",
            "health_alert_delivery": "disabled",
            "durable_health_persistence": "not performed (collection only)",
            "source_comparison_persistence": "not performed (collection only)",
        },
        "production_state_before": before_state,
        "production_state_after": after_state,
        "production_state_changed": production_state_changed,
        "runs": runs,
        "blocked_sources_recorded": blocked_this_canary,
        "paused_sources_recorded": paused_this_canary,
        "failure_conditions": failure_conditions,
        "passed": not any(failure_conditions.values()),
        "baseline_note": (
            "Compare against a recent normal serial production run, existing serial "
            "timing logs, or a serial run from a separate normal collection window. "
            "Do not run a fresh full serial collection back-to-back with this canary."
        ),
        "production_default": "Production default remains serial; concurrent mode is available for controlled canaries.",
    }

    if args.stage == "full":
        state["last_full_run_at"] = now.isoformat()
    state["blocked_sources"] = {
        **(state.get("blocked_sources", {}) or {}),
        **blocked_this_canary,
    }
    state.setdefault("history", []).append(
        {
            "at": now.isoformat(),
            "stage": args.stage,
            "mode": concurrency.mode,
            "passed": report["passed"],
        }
    )
    save_state(state_path, state)

    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    print(
        f"CANARY {'PASSED' if report['passed'] else 'FAILED'} stage={args.stage} "
        f"mode={concurrency.mode}",
        file=sys.stderr,
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
