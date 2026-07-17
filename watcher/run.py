"""Runnable watcher collection, analysis, digest, and seen-store core."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, TextIO

from backend.app.ingest import analyze_rows
from watcher.alumni import AlumniIndex, attach_alumni, load_default_alumni, status_for_injected_index
from watcher.config import DEFAULT_WATCHLIST_PATH, WatcherConfig, load_watchlist
from watcher.filters import filter_matches
from watcher.notify import send_digest
from watcher.season import (
    SEASON_ROLLOVER_DUE,
    SEASON_STALE,
    SEASON_UNKNOWN,
    company_season_warnings,
    season_status,
)
from watcher.seen_store import SeenStore
from watcher.source_health import (
    COVERAGE_UNCOVERED,
    ERROR_FETCH,
    ERROR_MISSING_ADAPTER,
    ERROR_SCHEMA,
    ERROR_SOURCE,
    ERROR_UNEXPECTED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_DEGRADED,
    STATUS_FAILING,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceAttempt,
    SourceHealthState,
    SourceHealthStore,
    calculate_company_coverage,
    direct_health_key,
    github_feed_health_key,
    new_run_id,
    sanitize_error,
    sanitize_feed_label,
    summarize_health,
    utc_datetime,
    write_health_report,
)
from watcher.sources import (
    AshbySource,
    GitHubListingsSource,
    GreenhouseSource,
    LeverSource,
    SmartRecruitersSource,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    WorkableSource,
    WorkdaySource,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class RunResult:
    rows_fetched: int
    jobs_scored: int
    matches: list[dict]
    new_matches: list[dict]
    errors: list[str]
    digest_sent: bool
    seen_marked: int
    alumni_csv_status: str
    alumni_records_loaded: int
    alumni_employers_indexed: int
    configured_terms: tuple[str, ...]
    season_status: str
    github_feeds_configured: int
    github_feeds_succeeded: int
    company_season_warnings: tuple[str, ...]
    run_id: str
    health_observed_at: datetime
    source_attempts: tuple[SourceAttempt, ...]
    source_health_states: dict[str, SourceHealthState]
    health_transitions: tuple[HealthTransition, ...]
    company_coverage: tuple[CompanyCoverage, ...]
    health_summary: HealthSummary
    alumni_status_message: str = ""


@dataclass
class CollectionStats:
    github_feeds_configured: int = 0
    github_feeds_succeeded: int = 0
    source_attempts: list[SourceAttempt] = field(default_factory=list)


def run_once(
    config: WatcherConfig,
    *,
    seen_store: SeenStore,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    alumni_index: AlumniIndex | None = None,
    digest_sender: Callable[[list[dict]], bool] | None = None,
    today: date | None = None,
    seen_at: datetime | None = None,
    mark_seen_without_send: bool = False,
    health_store: SourceHealthStore | None = None,
    run_id: str | None = None,
    health_observed_at: datetime | None = None,
) -> RunResult:
    observed_at = utc_datetime(health_observed_at or datetime.now(timezone.utc))
    active_run_id = run_id or new_run_id(observed_at)
    LOGGER.info("Watcher run ID: %s", active_run_id)
    current_date = today or date.today()
    active_season_status = season_status(config.terms, today=current_date)
    override_warnings = company_season_warnings(
        config.companies,
        config.terms,
        today=current_date,
    )
    _log_season_status(config.terms, active_season_status, override_warnings)
    LOGGER.info("Collecting watcher rows...")
    collection_stats = CollectionStats()
    rows, errors = collect_rows(
        config,
        direct_sources=direct_sources,
        github_source=github_source,
        stats=collection_stats,
        run_id=active_run_id,
        observed_at=observed_at,
    )
    owned_health_store = health_store is None
    active_health_store = health_store or SourceHealthStore(seen_store.path)
    try:
        health_states, health_transitions = active_health_store.record_attempts(
            collection_stats.source_attempts
        )
    finally:
        if owned_health_store:
            active_health_store.close()
    company_coverage = calculate_company_coverage(
        config.companies,
        collection_stats.source_attempts,
        health_states,
    )
    health_summary = summarize_health(
        config.companies,
        collection_stats.source_attempts,
        health_states,
        health_transitions,
        company_coverage,
    )
    _log_source_health(active_run_id, health_summary, health_states, health_transitions, company_coverage)
    LOGGER.info(
        "GitHub backstop feeds: %d configured, %d succeeded",
        collection_stats.github_feeds_configured,
        collection_stats.github_feeds_succeeded,
    )
    LOGGER.info("Analyzing %d fetched row(s)...", len(rows))
    jobs = analyze_rows(rows, today=today)
    LOGGER.info("Filtering %d scored job(s)...", len(jobs))
    matches = filter_matches(jobs, target_roles=config.target_roles, min_score=config.min_score)
    if alumni_index is None:
        alumni_index, alumni_status = load_default_alumni()
    else:
        alumni_status = status_for_injected_index(alumni_index)
    LOGGER.info(
        "Alumni CSV status: alumni_csv_status=%s alumni_records_loaded=%d alumni_employers_indexed=%d",
        alumni_status.status,
        alumni_status.records_loaded,
        alumni_status.employers_indexed,
    )
    matches = attach_alumni(
        matches,
        alumni_index,
        companies=config.companies,
    )
    new_matches = seen_store.unseen(matches)
    LOGGER.info("%d match(es), %d new.", len(matches), len(new_matches))
    LOGGER.info("Sending digest if needed...")
    if digest_sender is None:
        digest_sent = send_digest(
            new_matches,
            alumni_summary=alumni_status.as_dict(),
            active_terms=config.terms,
            season_status=active_season_status,
        )
    else:
        digest_sent = digest_sender(new_matches)
    should_mark_seen = digest_sent or (mark_seen_without_send and bool(new_matches))
    seen_marked = len(new_matches) if should_mark_seen else 0
    if should_mark_seen:
        timestamp = seen_at or datetime.now(timezone.utc)
        seen_store.mark_many_seen(
            new_matches,
            seen_at=timestamp,
            emailed_at=timestamp if digest_sent else None,
        )
        if digest_sent:
            LOGGER.info("Digest sent; marked %d job(s) seen.", seen_marked)
        else:
            LOGGER.info("Digest not sent; priming mode marked %d job(s) seen.", seen_marked)
    else:
        LOGGER.info("Digest not sent; seen-store unchanged.")
    return RunResult(
        rows_fetched=len(rows),
        jobs_scored=len(jobs),
        matches=matches,
        new_matches=new_matches,
        errors=errors,
        digest_sent=digest_sent,
        seen_marked=seen_marked,
        alumni_csv_status=alumni_status.status,
        alumni_records_loaded=alumni_status.records_loaded,
        alumni_employers_indexed=alumni_status.employers_indexed,
        configured_terms=config.terms,
        season_status=active_season_status,
        github_feeds_configured=collection_stats.github_feeds_configured,
        github_feeds_succeeded=collection_stats.github_feeds_succeeded,
        company_season_warnings=override_warnings,
        run_id=active_run_id,
        health_observed_at=observed_at,
        source_attempts=tuple(collection_stats.source_attempts),
        source_health_states=health_states,
        health_transitions=health_transitions,
        company_coverage=company_coverage,
        health_summary=health_summary,
        alumni_status_message=alumni_status.message,
    )


def collect_rows(
    config: WatcherConfig,
    *,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    stats: CollectionStats | None = None,
    run_id: str | None = None,
    observed_at: datetime | None = None,
) -> tuple[list[dict], list[str]]:
    active_run_id = run_id or new_run_id(observed_at)
    active_observed_at = utc_datetime(observed_at or datetime.now(timezone.utc))
    if stats is None:
        stats = CollectionStats()
    if direct_sources is None:
        direct_sources = _default_direct_sources()
    configured_count = len(config.github_listing_urls)
    if github_source is None:
        github_sources = [GitHubListingsSource(url) for url in config.github_listing_urls]
    elif isinstance(github_source, (list, tuple)):
        github_sources = list(github_source)
    else:
        github_sources = [github_source]
    stats.github_feeds_configured = configured_count
    direct_rows: list[dict] = []
    github_rows: list[dict] = []
    errors: list[str] = []

    for company in config.companies:
        if company.ats in {"bespoke", "github_only"}:
            LOGGER.info("Skipping direct fetch for %s (%s).", company.name, company.ats)
            stats.source_attempts.append(
                SourceAttempt(
                    health_key=direct_health_key(company.name, company.ats),
                    run_id=active_run_id,
                    observed_at=active_observed_at,
                    source_kind=SOURCE_KIND_DIRECT,
                    company=company.name,
                    adapter=company.ats,
                    attempted=False,
                    succeeded=None,
                    rows_returned=None,
                    unsupported_reason=company.ats,
                )
            )
            continue
        source = direct_sources.get(company.ats)
        if source is None:
            _record_error(errors, f"{company.name}: no source registered for ats '{company.ats}'")
            stats.source_attempts.append(
                _failed_attempt(
                    health_key=direct_health_key(company.name, company.ats),
                    run_id=active_run_id,
                    observed_at=active_observed_at,
                    source_kind=SOURCE_KIND_DIRECT,
                    company=company.name,
                    adapter=company.ats,
                    error_kind=ERROR_MISSING_ADAPTER,
                    error=RuntimeError(f"no source registered for ats '{company.ats}'"),
                )
            )
            continue
        try:
            LOGGER.info("Fetching %s via %s...", company.name, company.ats)
            rows = source.fetch(company)
            direct_rows.extend(rows)
            LOGGER.info("Fetched %d direct row(s) for %s.", len(rows), company.name)
            stats.source_attempts.append(
                _successful_attempt(
                    health_key=direct_health_key(company.name, company.ats),
                    run_id=active_run_id,
                    observed_at=active_observed_at,
                    source_kind=SOURCE_KIND_DIRECT,
                    company=company.name,
                    adapter=company.ats,
                    rows_returned=len(rows),
                )
            )
        except SourceSchemaError as exc:
            _record_error(errors, f"{company.name}: {exc}")
            stats.source_attempts.append(
                _failed_direct_attempt(company, active_run_id, active_observed_at, ERROR_SCHEMA, exc)
            )
        except SourceFetchError as exc:
            _record_error(errors, f"{company.name}: {exc}")
            stats.source_attempts.append(
                _failed_direct_attempt(company, active_run_id, active_observed_at, ERROR_FETCH, exc)
            )
        except SourceError as exc:
            _record_error(errors, f"{company.name}: {exc}")
            stats.source_attempts.append(
                _failed_direct_attempt(company, active_run_id, active_observed_at, ERROR_SOURCE, exc)
            )
        except Exception as exc:  # defensive run-loop boundary
            _record_error(errors, f"{company.name}: unexpected {type(exc).__name__}: {exc}")
            stats.source_attempts.append(
                _failed_direct_attempt(company, active_run_id, active_observed_at, ERROR_UNEXPECTED, exc)
            )

    for index, source in enumerate(github_sources):
        configured_url = config.github_listing_urls[index] if index < len(config.github_listing_urls) else ""
        label = sanitize_feed_label(configured_url or _github_source_label(source))
        health_key = github_feed_health_key(configured_url or label)
        before = len(github_rows)
        try:
            LOGGER.info("Fetching GitHub listings backstop feed %s...", label)
            if hasattr(source, "fetch_many"):
                github_rows.extend(source.fetch_many(config.companies))
            else:
                for company in config.companies:
                    github_rows.extend(source.fetch(company))
            stats.github_feeds_succeeded += 1
            LOGGER.info(
                "Fetched %d GitHub backstop row(s) from %s.",
                len(github_rows) - before,
                label,
            )
            stats.source_attempts.append(
                _successful_attempt(
                    health_key=health_key,
                    run_id=active_run_id,
                    observed_at=active_observed_at,
                    source_kind=SOURCE_KIND_GITHUB_FEED,
                    company=None,
                    adapter="github_listings",
                    rows_returned=len(github_rows) - before,
                    feed_label=label,
                )
            )
        except SourceSchemaError as exc:
            _record_error(errors, f"github listings ({label}): {_sanitize_error(exc)}")
            stats.source_attempts.append(
                _failed_attempt(
                    health_key=health_key,
                    run_id=active_run_id,
                    observed_at=active_observed_at,
                    source_kind=SOURCE_KIND_GITHUB_FEED,
                    company=None,
                    adapter="github_listings",
                    error_kind=ERROR_SCHEMA,
                    error=exc,
                    feed_label=label,
                )
            )
        except SourceFetchError as exc:
            _record_error(errors, f"github listings ({label}): {_sanitize_error(exc)}")
            stats.source_attempts.append(
                _failed_attempt(
                    health_key=health_key,
                    run_id=active_run_id,
                    observed_at=active_observed_at,
                    source_kind=SOURCE_KIND_GITHUB_FEED,
                    company=None,
                    adapter="github_listings",
                    error_kind=ERROR_FETCH,
                    error=exc,
                    feed_label=label,
                )
            )
        except SourceError as exc:
            _record_error(errors, f"github listings ({label}): {_sanitize_error(exc)}")
            stats.source_attempts.append(
                _failed_attempt(
                    health_key=health_key,
                    run_id=active_run_id,
                    observed_at=active_observed_at,
                    source_kind=SOURCE_KIND_GITHUB_FEED,
                    company=None,
                    adapter="github_listings",
                    error_kind=ERROR_SOURCE,
                    error=exc,
                    feed_label=label,
                )
            )
        except Exception as exc:  # defensive run-loop boundary
            _record_error(
                errors,
                f"github listings ({label}): unexpected {type(exc).__name__}: {_sanitize_error(exc)}",
            )
            stats.source_attempts.append(
                _failed_attempt(
                    health_key=health_key,
                    run_id=active_run_id,
                    observed_at=active_observed_at,
                    source_kind=SOURCE_KIND_GITHUB_FEED,
                    company=None,
                    adapter="github_listings",
                    error_kind=ERROR_UNEXPECTED,
                    error=exc,
                    feed_label=label,
                )
            )

    # Direct rows first: backend dedupe keeps the first row's extra metadata,
    # so this implements the direct-over-GitHub source-priority rule.
    return [*direct_rows, *github_rows], errors


def print_report(result: RunResult, *, output: TextIO | None = None) -> None:
    output = output or sys.stdout
    if getattr(result, "run_id", None):
        print(f"Watcher run ID: {result.run_id}", file=output)
    configured_terms = tuple(getattr(result, "configured_terms", ()) or ())
    print(
        f"Configured internship terms: {', '.join(configured_terms) if configured_terms else '(none)'}",
        file=output,
    )
    print(f"Season status: {getattr(result, 'season_status', 'unknown')}", file=output)
    print(
        "GitHub backstop feeds: "
        f"{getattr(result, 'github_feeds_configured', 0)} configured, "
        f"{getattr(result, 'github_feeds_succeeded', 0)} succeeded",
        file=output,
    )
    _print_source_health(result, output=output)
    if result.errors:
        print(f"Source errors: {len(result.errors)}", file=output)
        for error in result.errors:
            print(f"  - {error}", file=output)

    if not result.new_matches:
        print("No new matches.", file=output)
        return

    print(f"New matches: {len(result.new_matches)}", file=output)
    for job in result.new_matches:
        source = job.get("extra", {}).get("source", "unknown")
        score = job.get("score", {})
        reasons = score.get("reasons") or []
        red_flags = job.get("red_flags") or []
        print(f"[{source}] {job.get('company', '')} - {job.get('title', '')}", file=output)
        print(f"  location: {job.get('location', '') or '(not listed)'}", file=output)
        print(f"  role track: {score.get('role_track') or job.get('role_classification', {}).get('role_track', 'unknown')}", file=output)
        print(
            f"  score: {score.get('total', 0)}, fit: {score.get('fit_score', score.get('total', 0))} "
            f"({score.get('watcher_action_label') or score.get('action_label') or score.get('action', 'unknown')})",
            file=output,
        )
        if score.get("fit_explanation"):
            print(f"  fit reason: {score['fit_explanation']}", file=output)
        print(f"  top reason: {reasons[0] if reasons else '(none)'}", file=output)
        if red_flags:
            labels = ", ".join(flag.get("label", str(flag)) for flag in red_flags)
            print(f"  red flags: {labels}", file=output)
        else:
            print("  red flags: none", file=output)
        print(f"  url: {job.get('source_url', '')}", file=output)


def print_heartbeat(result: RunResult, *, output: TextIO | None = None) -> None:
    output = output or sys.stdout
    sent = "yes" if result.digest_sent else "no"
    health = getattr(result, "health_summary", None)
    print(
        "HEARTBEAT: ran, "
        f"rows_fetched={result.rows_fetched}, "
        f"jobs_scored={result.jobs_scored}, "
        f"matches={len(result.matches)}, "
        f"new={len(result.new_matches)}, "
        f"errors={len(result.errors)}, "
        f"season_status={getattr(result, 'season_status', 'unknown')}, "
        f"configured_terms={_heartbeat_terms(getattr(result, 'configured_terms', ()))}, "
        f"github_feeds_configured={getattr(result, 'github_feeds_configured', 0)}, "
        f"github_feeds_succeeded={getattr(result, 'github_feeds_succeeded', 0)}, "
        f"companies_configured={_health_value(health, 'companies_configured')}, "
        f"direct_healthy={_health_value(health, 'direct_healthy')}, "
        f"direct_empty={_health_value(health, 'direct_empty')}, "
        f"direct_degraded={_health_value(health, 'direct_degraded')}, "
        f"direct_failing={_health_value(health, 'direct_failing')}, "
        f"direct_unsupported={_health_value(health, 'direct_unsupported')}, "
        f"github_feeds_healthy={_health_value(health, 'github_feeds_healthy')}, "
        f"backstop_only_companies={_health_value(health, 'backstop_only_companies')}, "
        f"uncovered_companies={_health_value(health, 'uncovered_companies')}, "
        f"health_transitions={_health_value(health, 'health_transitions')}, "
        f"health_recoveries={_health_value(health, 'health_recoveries')}, "
        f"alumni_csv_status={getattr(result, 'alumni_csv_status', 'unknown')}, "
        f"alumni_records_loaded={getattr(result, 'alumni_records_loaded', 0)}, "
        f"alumni_employers_indexed={getattr(result, 'alumni_employers_indexed', 0)}, "
        f"sent={sent}, "
        f"seen_marked={result.seen_marked}",
        file=output,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the internship watcher once and print new matches.")
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH), help="Path to watchlist.yml")
    parser.add_argument("--seen-db", help="Path to SQLite seen-store")
    parser.add_argument(
        "--health-report",
        help="Write the sanitized machine-readable source-health JSON report to this path.",
    )
    parser.add_argument(
        "--mark-seen-without-send",
        action="store_true",
        help="Mark new matches seen even when the digest dry-runs; intended for CI priming.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    config = load_watchlist(args.watchlist)
    if args.seen_db:
        config = replace(config, seen_db_path=Path(args.seen_db))

    with SeenStore(config.seen_db_path) as seen_store:
        result = run_once(config, seen_store=seen_store, mark_seen_without_send=args.mark_seen_without_send)
    health_report_path = args.health_report or os.getenv("WATCHER_HEALTH_REPORT_PATH", "").strip()
    if health_report_path:
        _write_result_health_report(result, health_report_path)
        LOGGER.info("Wrote source-health JSON report: %s", health_report_path)
    print_report(result)
    print_heartbeat(result)
    return 0


def _default_direct_sources() -> dict[str, object]:
    return {
        "ashby": AshbySource(),
        "greenhouse": GreenhouseSource(),
        "lever": LeverSource(),
        "smartrecruiters": SmartRecruitersSource(),
        "workable": WorkableSource(),
        "workday": WorkdaySource(),
    }


def _record_error(errors: list[str], message: str) -> None:
    safe_message = sanitize_error(message)
    LOGGER.warning(safe_message)
    errors.append(safe_message)


def _successful_attempt(
    *,
    health_key: str,
    run_id: str,
    observed_at: datetime,
    source_kind: str,
    company: str | None,
    adapter: str,
    rows_returned: int,
    feed_label: str | None = None,
) -> SourceAttempt:
    return SourceAttempt(
        health_key=health_key,
        run_id=run_id,
        observed_at=observed_at,
        source_kind=source_kind,
        company=company,
        adapter=adapter,
        attempted=True,
        succeeded=True,
        rows_returned=rows_returned,
        feed_label=feed_label,
    )


def _failed_attempt(
    *,
    health_key: str,
    run_id: str,
    observed_at: datetime,
    source_kind: str,
    company: str | None,
    adapter: str,
    error_kind: str,
    error: Exception,
    feed_label: str | None = None,
) -> SourceAttempt:
    return SourceAttempt(
        health_key=health_key,
        run_id=run_id,
        observed_at=observed_at,
        source_kind=source_kind,
        company=company,
        adapter=adapter,
        attempted=True,
        succeeded=False,
        rows_returned=None,
        error_kind=error_kind,
        error_message=sanitize_error(f"{type(error).__name__}: {error}"),
        feed_label=feed_label,
    )


def _failed_direct_attempt(
    company,
    run_id: str,
    observed_at: datetime,
    error_kind: str,
    error: Exception,
) -> SourceAttempt:
    return _failed_attempt(
        health_key=direct_health_key(company.name, company.ats),
        run_id=run_id,
        observed_at=observed_at,
        source_kind=SOURCE_KIND_DIRECT,
        company=company.name,
        adapter=company.ats,
        error_kind=error_kind,
        error=error,
    )


def _print_source_health(result: RunResult, *, output: TextIO) -> None:
    summary = getattr(result, "health_summary", None)
    if summary is None:
        return
    print("Source health:", file=output)
    print(f"  Companies configured: {summary.companies_configured}", file=output)
    print(f"  Direct healthy: {summary.direct_healthy}", file=output)
    print(f"  Direct empty: {summary.direct_empty}", file=output)
    print(f"  Direct degraded: {summary.direct_degraded}", file=output)
    print(f"  Direct failing: {summary.direct_failing}", file=output)
    print(f"  Direct unsupported: {summary.direct_unsupported}", file=output)
    print(
        f"  Backstop feeds healthy: {summary.github_feeds_healthy}/{summary.github_feeds_configured}",
        file=output,
    )
    print(f"  Backstop-only companies: {summary.backstop_only_companies}", file=output)
    print(f"  Uncovered this run: {summary.uncovered_companies}", file=output)
    print(f"  Health transitions: {summary.health_transitions}", file=output)

    transitions = tuple(getattr(result, "health_transitions", ()) or ())
    if transitions:
        print("Health transitions:", file=output)
        for transition in transitions:
            label = transition.company or transition.feed_label or transition.health_key
            recovery = " (recovery)" if transition.recovery else ""
            print(
                f"  - {label} [{transition.adapter}]: "
                f"{transition.from_status} -> {transition.to_status}{recovery}",
                file=output,
            )

    states = tuple(getattr(result, "source_health_states", {}).values())
    actionable = [state for state in states if state.status in {STATUS_DEGRADED, STATUS_FAILING}]
    if actionable:
        print("Current degraded/failing sources:", file=output)
        for state in sorted(actionable, key=lambda item: (item.status, item.company or item.feed_label or "")):
            label = state.company or state.feed_label or state.health_key
            print(
                f"  - {label} [{state.adapter}]: {state.status}, "
                f"consecutive_failures={state.consecutive_failures}",
                file=output,
            )
            if state.last_error_message:
                print(f"    Last error: {state.last_error_message}", file=output)

    uncovered = [
        item
        for item in getattr(result, "company_coverage", ())
        if item.state == COVERAGE_UNCOVERED
    ]
    if uncovered:
        print("Uncovered companies this run:", file=output)
        for item in uncovered:
            print(f"  - {item.company} [{item.adapter}]", file=output)


def _log_source_health(
    run_id: str,
    summary: HealthSummary,
    states: dict[str, SourceHealthState],
    transitions: tuple[HealthTransition, ...],
    coverage: tuple[CompanyCoverage, ...],
) -> None:
    LOGGER.info(
        "Source health run_id=%s companies=%d direct_healthy=%d direct_empty=%d "
        "direct_degraded=%d direct_failing=%d direct_unsupported=%d "
        "github_healthy=%d/%d uncovered=%d transitions=%d recoveries=%d",
        run_id,
        summary.companies_configured,
        summary.direct_healthy,
        summary.direct_empty,
        summary.direct_degraded,
        summary.direct_failing,
        summary.direct_unsupported,
        summary.github_feeds_healthy,
        summary.github_feeds_configured,
        summary.uncovered_companies,
        summary.health_transitions,
        summary.health_recoveries,
    )
    for transition in transitions:
        label = transition.company or transition.feed_label or transition.health_key
        level = LOGGER.info if transition.recovery else LOGGER.warning
        level(
            "SOURCE HEALTH TRANSITION: %s [%s]: %s -> %s%s",
            label,
            transition.adapter,
            transition.from_status,
            transition.to_status,
            " recovery" if transition.recovery else "",
        )
    for state in states.values():
        if state.status in {STATUS_DEGRADED, STATUS_FAILING}:
            label = state.company or state.feed_label or state.health_key
            LOGGER.warning(
                "SOURCE HEALTH CURRENT: %s [%s] status=%s consecutive_failures=%d error=%s",
                label,
                state.adapter,
                state.status,
                state.consecutive_failures,
                state.last_error_message or "none",
            )
    for item in coverage:
        if item.state == COVERAGE_UNCOVERED:
            LOGGER.error("SOURCE COVERAGE: %s [%s] uncovered_for_run", item.company, item.adapter)


def _write_result_health_report(result: RunResult, path: str | Path) -> None:
    write_health_report(
        path,
        run_id=result.run_id,
        observed_at=result.health_observed_at,
        attempts=result.source_attempts,
        states=result.source_health_states,
        transitions=result.health_transitions,
        coverage=result.company_coverage,
        summary=result.health_summary,
        run_metadata={
            "configured_terms": ", ".join(result.configured_terms) or "(none)",
            "season_status": result.season_status,
            "rows_fetched": result.rows_fetched,
            "jobs_scored": result.jobs_scored,
            "matches": len(result.matches),
            "new_matches": len(result.new_matches),
            "errors": len(result.errors),
            "github_feeds_configured": result.github_feeds_configured,
            "github_feeds_succeeded": result.github_feeds_succeeded,
            "digest_sent": result.digest_sent,
            "seen_marked": result.seen_marked,
        },
    )


def _health_value(summary: HealthSummary | None, field_name: str) -> int:
    return int(getattr(summary, field_name, 0) or 0)


def _log_season_status(
    terms: tuple[str, ...],
    status: str,
    override_warnings: tuple[str, ...],
) -> None:
    LOGGER.info("Configured internship terms: %s", ", ".join(terms) if terms else "(none)")
    LOGGER.info("Season status: %s", status)
    if status == SEASON_ROLLOVER_DUE:
        LOGGER.warning(
            "SEASON WARNING: rollover_due; July or later has arrived without a future-year term."
        )
    elif status == SEASON_STALE:
        LOGGER.error(
            "SEASON WARNING: stale; every recognized configured term year is before the current year."
        )
    elif status == SEASON_UNKNOWN:
        LOGGER.warning(
            "SEASON WARNING: unknown; no four-digit year was found, so automatic season verification was impossible."
        )
    for warning in override_warnings:
        LOGGER.warning("SEASON WARNING: %s", warning)


def _heartbeat_terms(terms: object) -> str:
    values = []
    for term in terms or ():
        value = re.sub(r"\s+", "_", str(term).strip())
        value = value.replace(",", "-").replace("|", "/")
        if value:
            values.append(value)
    return "|".join(values) if values else "none"


def _github_source_label(source: object) -> str:
    return str(
        getattr(source, "feed_label", "")
        or getattr(source, "name", "")
        or "injected"
    )


def _sanitize_error(error: Exception) -> str:
    return sanitize_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
