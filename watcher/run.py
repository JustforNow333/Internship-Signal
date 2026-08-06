"""Runnable watcher collection, analysis, digest, and seen-store core."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, TextIO

from watcher.audit_trace import enrich_duplicate_entries
from watcher.alumni import AlumniIndex, attach_alumni, load_default_alumni, status_for_injected_index
from watcher.analysis_cache import (
    AnalysisCacheStats,
    analyze_rows_with_cache,
)
from watcher.collection_concurrency import (
    PROVIDER_GITHUB_FEED,
    CollectionConcurrencyMetrics,
    CollectionScheduler,
    CollectionTask,
    TaskResult,
    direct_origin_key,
    log_collection_concurrency,
    origin_key_for_url,
)
from watcher.config import (
    COLLECTION_MODE_SERIAL,
    DEFAULT_ANALYSIS_CACHE_FILENAME,
    DEFAULT_WATCHLIST_PATH,
    CollectionConcurrencyCfg,
    CompanyCfg,
    GitHubListingSourceCfg,
    WatcherConfig,
    load_watchlist,
    resolve_analysis_cache_path,
    workday_min_interval_seconds,
)
from watcher.collection_snapshot import (
    CollectionBatch,
    CollectionSnapshotError,
    collection_config_fingerprint,
    load_collection_snapshot,
    save_collection_snapshot,
)
from watcher.eligibility import determine_watcher_eligibility
from watcher.filters import filter_matches, is_internship, is_open
from watcher.health_alerts import (
    MODE_OFF as HEALTH_EMAIL_OFF,
    HealthAlertPolicy,
    HealthAlertResult,
    evaluate_and_send_health_alerts,
    load_health_alert_policy,
)
from watcher.notify import email_sending_enabled, send_digest
from watcher.season import (
    SEASON_ROLLOVER_DUE,
    SEASON_STALE,
    SEASON_UNKNOWN,
    company_season_warnings,
    season_status,
)
from watcher.seen_store import SeenStore
from watcher.source_comparison import (
    CATEGORY_BOTH,
    CATEGORY_DIRECT_ONLY,
    CATEGORY_GITHUB_ONLY,
    SourceComparisonReport,
    SourceComparisonStore,
    build_source_comparison,
)
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
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_UNKNOWN,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceAttempt,
    SourceHealthState,
    SourceHealthStore,
    calculate_next_state,
    calculate_company_coverage,
    direct_health_key,
    github_feed_health_key,
    new_run_id,
    sanitize_error,
    sanitize_feed_label,
    safe_error_kind,
    summarize_health,
    transition_for,
    utc_datetime,
    write_health_report,
)
from watcher.sources import (
    AshbySource,
    DirectSourceDiagnostics,
    GitHubListingsSource,
    GitHubMarkdownTableSource,
    GreenhouseSource,
    LeverSource,
    OracleHcmSource,
    SmartRecruitersSource,
    TalentBrewSource,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    WorkableSource,
    WorkdaySource,
)
from watcher.sources.workday import WorkdayPacer, WorkdayStartTelemetry

LOGGER = logging.getLogger(__name__)


@dataclass
class RunResult:
    rows_fetched: int
    jobs_scored: int
    matches: list[dict]
    new_matches: list[dict]
    previously_emailed: list[dict]
    explicitly_primed: list[dict]
    errors: list[str]
    notification_mode: str
    digest_sent: bool
    seen_marked: int
    dry_run_pending: int
    cross_source_duplicates_merged: int
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
    workday_transport: "WorkdayTransportSummary"
    analysis_cache_stats: AnalysisCacheStats
    alumni_status_message: str = ""
    eligibility_exclusions: tuple[dict[str, object], ...] = ()
    source_comparison: SourceComparisonReport | None = None
    health_alert_result: HealthAlertResult = field(
        default_factory=lambda: HealthAlertResult(
            mode=HEALTH_EMAIL_OFF,
            candidates=0,
            sent=False,
            suppressed_by_cooldown=0,
            recovery_alerts=0,
            subject="",
            error=None,
            daily_summary_sent=False,
        )
    )
    source_comparison_persisted: bool = False
    jobs: list[dict] = field(default_factory=list)
    duplicate_report: list[dict] = field(default_factory=list)
    collection_replayed: bool = False
    collection_mode: str = COLLECTION_MODE_SERIAL
    collection_concurrency: CollectionConcurrencyMetrics | None = None


@dataclass
class CollectionStats:
    github_feeds_configured: int = 0
    github_feeds_succeeded: int = 0
    source_attempts: list[SourceAttempt] = field(default_factory=list)
    workday_attempted: int = 0
    workday_succeeded: int = 0
    workday_failed: int = 0
    workday_request_attempts: int = 0
    workday_retry_attempts: int = 0
    workday_failure_codes: Counter[str] = field(default_factory=Counter)
    # Collection-mode diagnostics stay in memory: they are canary evidence, not
    # part of the persisted collection snapshot schema.
    collection_mode: str = COLLECTION_MODE_SERIAL
    collection_concurrency: CollectionConcurrencyMetrics | None = None
    workday_start_telemetry: WorkdayStartTelemetry | None = None
    http_status_counts: Counter[int] = field(default_factory=Counter)
    challenge_responses: int = 0
    unexpected_task_exceptions: int = 0


@dataclass(frozen=True)
class WorkdayTransportSummary:
    attempted_tenants: int = 0
    successful_tenants: int = 0
    failed_tenants: int = 0
    retry_attempts: int = 0
    dominant_error: str = "none"
    dominant_error_count: int = 0
    likely_shared_incident: bool = False


WORKDAY_TRANSPORT_ERROR_CODES = frozenset(
    {
        "compressed_decode_failure",
        "connection_reset",
        "dns_failure",
        "empty_response",
        "html_challenge",
        "html_response",
        "json_decode_failure",
        "network_failure",
        "rate_limited",
        "redirected_to_html",
        "timeout",
        "transient_http_error",
        "unsupported_content_type",
    }
)
RUN_MODE_LIVE = "live_send"
RUN_MODE_DRY = "dry_run"
RUN_MODE_PRIME = "explicit_prime"
RUN_MODES = frozenset({RUN_MODE_LIVE, RUN_MODE_DRY, RUN_MODE_PRIME})


@contextmanager
def _timed_stage(stage: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        LOGGER.info(
            "STAGE-TIMING stage=%s seconds=%.3f",
            _timing_log_value(stage),
            time.perf_counter() - started,
        )


def _log_source_timing(
    *,
    company: str,
    adapter: str,
    success: bool,
    elapsed_seconds: float,
    rows_returned: int,
    source: object,
    source_name: str | None = None,
    error: Exception | None = None,
    request_count: int | None = None,
    retry_count: int | None = None,
) -> None:
    fields = [
        "SOURCE-TIMING",
        f"company={_timing_log_value(company)}",
        f"adapter={_timing_log_value(adapter)}",
    ]
    if source_name is not None:
        fields.append(f"source={_timing_log_value(source_name)}")
    fields.extend(
        (
            f"success={'true' if success else 'false'}",
            f"seconds={elapsed_seconds:.3f}",
            f"rows={max(0, int(rows_returned))}",
        )
    )
    if request_count is None and retry_count is None:
        request_count, retry_count = _source_request_counts(source, error=error)
    if request_count is not None:
        fields.append(f"requests={request_count}")
    if retry_count is not None:
        fields.append(f"retries={retry_count}")
    LOGGER.info(" ".join(fields))


def _source_request_counts(
    source: object,
    *,
    error: Exception | None,
) -> tuple[int | None, int | None]:
    try:
        diagnostics = getattr(source, "last_diagnostics", None)
    except Exception:
        diagnostics = None
    request_count = _nonnegative_optional_int(
        _safe_attribute(diagnostics, "request_attempts")
    )
    retry_count = _nonnegative_optional_int(
        _safe_attribute(diagnostics, "retry_attempts")
    )
    if request_count is None:
        attempt_count = _nonnegative_optional_int(
            _safe_attribute(error, "attempt_count")
        )
        if attempt_count is not None:
            request_count = max(1, attempt_count)
            if retry_count is None:
                retry_count = max(0, request_count - 1)
    return request_count, retry_count


def _safe_attribute(value: object, name: str) -> object | None:
    if value is None:
        return None
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _nonnegative_optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except Exception:
        return None


def _timing_log_value(value: object) -> str:
    text = re.sub(r"[\x00-\x20\x7f]+", "_", str(value or "").strip())
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("_.-")
    text = re.sub(r"_+", "_", text)
    return text[:120] or "unknown"


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
    notification_mode: str | None = None,
    health_store: SourceHealthStore | None = None,
    run_id: str | None = None,
    health_observed_at: datetime | None = None,
    health_alert_policy: HealthAlertPolicy | None = None,
    health_alert_sender: Callable[[str, str], bool] | None = None,
    collection_batch: CollectionBatch | None = None,
    capture_collection_snapshot_path: str | Path | None = None,
    replay_collection_snapshot_path: str | Path | None = None,
    allow_collection_config_mismatch: bool = False,
) -> RunResult:
    replay_mode = collection_batch is not None
    if replay_mode and capture_collection_snapshot_path is not None:
        raise ValueError(
            "Collection snapshot capture and replay are mutually exclusive"
        )
    config_fingerprint = collection_config_fingerprint(config)
    config_matches_snapshot = True
    if replay_mode:
        assert collection_batch is not None
        config_matches_snapshot = (
            collection_batch.collection_config_fingerprint == config_fingerprint
        )
        if not config_matches_snapshot and not allow_collection_config_mismatch:
            raise CollectionSnapshotError(
                "Collection snapshot configuration does not match the current "
                "collection-affecting watchlist settings; use "
                "--allow-collection-config-mismatch only when this is intentional"
            )
        if mark_seen_without_send or notification_mode in {
            RUN_MODE_LIVE,
            RUN_MODE_PRIME,
        }:
            raise ValueError(
                "Collection replay is permanently dry-run and cannot send or "
                "mark notification state"
            )
        notification_mode = RUN_MODE_DRY

    if mark_seen_without_send:
        if notification_mode not in {None, RUN_MODE_PRIME}:
            raise ValueError(
                "mark_seen_without_send is only compatible with explicit-prime mode"
            )
        notification_mode = RUN_MODE_PRIME
    if notification_mode is None:
        notification_mode = (
            RUN_MODE_LIVE
            if digest_sender is not None or email_sending_enabled()
            else RUN_MODE_DRY
        )
    if notification_mode not in RUN_MODES:
        raise ValueError(f"Unknown notification mode: {notification_mode}")

    if replay_mode:
        assert collection_batch is not None
        observed_at = utc_datetime(collection_batch.captured_at)
        active_run_id = (
            collection_batch.source_attempts[0].run_id
            if collection_batch.source_attempts
            else f"replay-{observed_at:%Y%m%dT%H%M%SZ}-"
            f"{collection_batch.collection_config_fingerprint[:12]}"
        )
    else:
        observed_at = utc_datetime(health_observed_at or datetime.now(timezone.utc))
        active_run_id = run_id or new_run_id(observed_at)
    LOGGER.info("Watcher run ID: %s", active_run_id)
    current_date = today or (
        observed_at.date() if replay_mode else date.today()
    )
    active_season_status = season_status(config.terms, today=current_date)
    override_warnings = company_season_warnings(
        config.companies,
        config.terms,
        today=current_date,
    )
    _log_season_status(config.terms, active_season_status, override_warnings)
    if replay_mode:
        assert collection_batch is not None
        LOGGER.info(
            "COLLECTION-SNAPSHOT mode=replay path=%s rows=%d captured_at=%s "
            "config_match=%s",
            _timing_log_value(replay_collection_snapshot_path or "injected"),
            len(collection_batch.rows),
            collection_batch.captured_at.isoformat(),
            "true" if config_matches_snapshot else "false",
        )
    else:
        LOGGER.info("Collecting watcher rows...")
        live_collection_stats = CollectionStats()
        collection_batch = collect_batch(
            config,
            direct_sources=direct_sources,
            github_source=github_source,
            stats=live_collection_stats,
            run_id=active_run_id,
            observed_at=observed_at,
        )
        if capture_collection_snapshot_path is not None:
            save_collection_snapshot(
                collection_batch,
                capture_collection_snapshot_path,
            )
            LOGGER.info(
                "COLLECTION-SNAPSHOT mode=capture path=%s rows=%d captured_at=%s",
                _timing_log_value(capture_collection_snapshot_path),
                len(collection_batch.rows),
                collection_batch.captured_at.isoformat(),
            )
    assert collection_batch is not None
    collection_stats = _collection_stats_from_batch(collection_batch)
    # Snapshot replay has no live collection, so it reports no collection mode.
    collection_mode = "none" if replay_mode else live_collection_stats.collection_mode
    collection_concurrency = (
        None if replay_mode else live_collection_stats.collection_concurrency
    )
    rows = collection_batch.mutable_rows()
    errors = list(collection_batch.errors)
    workday_transport = summarize_workday_transport(collection_stats)
    _log_workday_transport(workday_transport)
    with _timed_stage("health_state_persistence"):
        if replay_mode:
            health_states, health_transitions = _ephemeral_health_state(
                collection_stats.source_attempts
            )
        else:
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
    analysis_cache_path = Path(config.analysis_cache_path)
    configured_default_cache_path = (
        config.seen_db_path.parent / DEFAULT_ANALYSIS_CACHE_FILENAME
    )
    if (
        analysis_cache_path == configured_default_cache_path
        and seen_store.path != config.seen_db_path
    ):
        # Reusable callers commonly inject a temporary SeenStore while relying
        # on default configuration. Keep the cache separate but colocated.
        analysis_cache_path = (
            seen_store.path.parent / DEFAULT_ANALYSIS_CACHE_FILENAME
        )
    with _timed_stage("analysis"):
        cached_analysis = analyze_rows_with_cache(
            rows,
            db_path=analysis_cache_path,
            enabled=config.analysis_cache_enabled,
            today=current_date,
            include_audit_diagnostics=True,
            accessed_at=(
                datetime.now(timezone.utc)
                if replay_mode
                else observed_at
            ),
        )
        jobs = cached_analysis.jobs
        duplicate_report = cached_analysis.duplicate_report
        duplicate_report = enrich_duplicate_entries(rows, duplicate_report)
        cross_source_duplicates_merged = sum(
            1
            for duplicate in duplicate_report
            if duplicate.get("cross_source") is True
        )
    LOGGER.info("Filtering %d scored job(s)...", len(jobs))
    with _timed_stage("filtering_eligibility"):
        eligibility_exclusions = _categorical_exclusion_audit(
            jobs,
            target_roles=config.target_roles,
        )
        if eligibility_exclusions:
            reason_counts = Counter(
                str(item["exclusion_reason"]) for item in eligibility_exclusions
            )
            LOGGER.info(
                "Categorical eligibility exclusions: total=%d reasons=%s",
                len(eligibility_exclusions),
                ",".join(
                    f"{reason}={count}"
                    for reason, count in sorted(reason_counts.items())
                ),
            )
        matches = filter_matches(
            jobs,
            target_roles=config.target_roles,
            min_score=config.min_score,
        )
    with _timed_stage("alumni_loading_matching"):
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
    with _timed_stage("seen_store_partitioning"):
        notification_selection = seen_store.partition(matches)
    new_matches = notification_selection.pending
    previously_emailed = notification_selection.emailed
    explicitly_primed = notification_selection.primed
    dry_run_pending = len(new_matches) if notification_mode == RUN_MODE_DRY else 0
    LOGGER.info(
        "Notification summary: eligible=%d new=%d emailed_suppressed=%d "
        "primed_suppressed=%d dry_run_pending=%d cross_source_duplicates_merged=%d",
        len(matches),
        len(new_matches),
        len(previously_emailed),
        len(explicitly_primed),
        dry_run_pending,
        cross_source_duplicates_merged,
    )
    _log_suppressed_postings("previously emailed", previously_emailed)
    _log_suppressed_postings("explicitly primed", explicitly_primed)

    with _timed_stage("digest_email_handling"):
        digest_sent = False
        if replay_mode:
            LOGGER.info(
                "Collection replay: email transport and notification writes were not invoked."
            )
        elif notification_mode == RUN_MODE_DRY:
            LOGGER.info(
                "Dry-run mode: email transport was not invoked."
            )
        elif notification_mode == RUN_MODE_PRIME:
            LOGGER.info("Explicit-prime mode: email transport was not invoked.")
        elif digest_sender is None:
            digest_sent = send_digest(
                new_matches,
                alumni_summary=alumni_status.as_dict(),
                active_terms=config.terms,
                season_status=active_season_status,
            )
        else:
            digest_sent = digest_sender(new_matches)

        if notification_mode == RUN_MODE_DRY:
            seen_marked = 0
            LOGGER.info(
                "Dry-run mode: left %d posting(s) pending; notification state unchanged.",
                len(new_matches),
            )
        elif notification_mode == RUN_MODE_PRIME:
            seen_marked = len(new_matches)
            if new_matches:
                timestamp = seen_at or datetime.now(timezone.utc)
                seen_store.mark_many_primed(new_matches, primed_at=timestamp)
            LOGGER.info(
                "Explicit-prime mode: marked %d posting(s) with primed_at.",
                seen_marked,
            )
        elif digest_sent:
            seen_marked = len(new_matches)
            timestamp = seen_at or datetime.now(timezone.utc)
            seen_store.mark_many_emailed(new_matches, emailed_at=timestamp)
            LOGGER.info(
                "Digest sent; marked %d posting(s) with emailed_at.",
                seen_marked,
            )
        else:
            seen_marked = 0
            LOGGER.info(
                "Live digest was not sent; left %d posting(s) pending.",
                len(new_matches),
            )
    with _timed_stage("source_comparison_generation_persistence"):
        source_comparison = build_source_comparison(
            config=config,
            jobs=jobs,
            seen_store=seen_store,
            run_id=active_run_id,
            observed_at=observed_at,
            duplicate_report=duplicate_report,
            coverage=company_coverage,
            source_attempts=collection_stats.source_attempts,
            source_health_states=health_states,
        )
        source_comparison_persisted = False
        if replay_mode:
            LOGGER.info(
                "Collection replay: source comparison built in memory and not persisted."
            )
        else:
            try:
                with SourceComparisonStore(seen_store.path) as comparison_store:
                    comparison_store.save(source_comparison)
                source_comparison_persisted = True
            except Exception as exc:  # observability must not alter notification semantics
                LOGGER.error(
                    "Source-comparison persistence failed: %s",
                    sanitize_error(exc),
                )
    active_health_alert_policy = health_alert_policy or HealthAlertPolicy(
        mode=HEALTH_EMAIL_OFF
    )
    with _timed_stage("health_alert_evaluation"):
        if replay_mode:
            health_alert_result = HealthAlertResult(
                mode=HEALTH_EMAIL_OFF,
                candidates=0,
                sent=False,
                suppressed_by_cooldown=0,
                recovery_alerts=0,
                subject="",
                error=None,
                daily_summary_sent=False,
            )
            LOGGER.info("Collection replay: source-health alerts were not evaluated.")
        else:
            try:
                health_alert_result = evaluate_and_send_health_alerts(
                    db_path=seen_store.path,
                    policy=active_health_alert_policy,
                    run_id=active_run_id,
                    observed_at=observed_at,
                    states=health_states,
                    transitions=health_transitions,
                    coverage=company_coverage,
                    summary=health_summary,
                    comparison=source_comparison,
                    sender=health_alert_sender,
                )
            except Exception as exc:  # alert diagnostics must not alter match delivery
                alert_error = sanitize_error(exc)
                LOGGER.error("Source-health alert evaluation failed: %s", alert_error)
                health_alert_result = HealthAlertResult(
                    mode=active_health_alert_policy.mode,
                    candidates=0,
                    sent=False,
                    suppressed_by_cooldown=0,
                    recovery_alerts=0,
                    subject="",
                    error=alert_error,
                    daily_summary_sent=False,
                )
    return RunResult(
        rows_fetched=len(rows),
        jobs_scored=len(jobs),
        matches=matches,
        new_matches=new_matches,
        previously_emailed=previously_emailed,
        explicitly_primed=explicitly_primed,
        errors=errors,
        notification_mode=notification_mode,
        digest_sent=digest_sent,
        seen_marked=seen_marked,
        dry_run_pending=dry_run_pending,
        cross_source_duplicates_merged=cross_source_duplicates_merged,
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
        workday_transport=workday_transport,
        analysis_cache_stats=cached_analysis.stats,
        alumni_status_message=alumni_status.message,
        eligibility_exclusions=eligibility_exclusions,
        source_comparison=source_comparison,
        health_alert_result=health_alert_result,
        source_comparison_persisted=source_comparison_persisted,
        jobs=jobs,
        duplicate_report=duplicate_report,
        collection_replayed=replay_mode,
        collection_mode=collection_mode,
        collection_concurrency=collection_concurrency,
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
    batch = collect_batch(
        config,
        direct_sources=direct_sources,
        github_source=github_source,
        stats=stats,
        run_id=run_id,
        observed_at=observed_at,
    )
    return batch.mutable_rows(), list(batch.errors)


def collect_batch(
    config: WatcherConfig,
    *,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    stats: CollectionStats | None = None,
    run_id: str | None = None,
    observed_at: datetime | None = None,
    captured_at: datetime | None = None,
) -> CollectionBatch:
    """Run normal live collection and return its complete replayable result."""

    active_stats = stats if stats is not None else CollectionStats()
    with _timed_stage("collection"):
        rows, errors = _collect_rows(
            config,
            direct_sources=direct_sources,
            github_source=github_source,
            stats=active_stats,
            run_id=run_id,
            observed_at=observed_at,
        )
    return CollectionBatch.create(
        captured_at=captured_at or datetime.now(timezone.utc),
        collection_config_fingerprint=collection_config_fingerprint(config),
        rows=rows,
        errors=errors,
        source_attempts=active_stats.source_attempts,
        github_feeds_configured=active_stats.github_feeds_configured,
        github_feeds_succeeded=active_stats.github_feeds_succeeded,
        workday_attempted=active_stats.workday_attempted,
        workday_succeeded=active_stats.workday_succeeded,
        workday_failed=active_stats.workday_failed,
        workday_request_attempts=active_stats.workday_request_attempts,
        workday_retry_attempts=active_stats.workday_retry_attempts,
        workday_failure_codes=active_stats.workday_failure_codes,
    )


def _collection_stats_from_batch(batch: CollectionBatch) -> CollectionStats:
    return CollectionStats(
        github_feeds_configured=batch.github_feeds_configured,
        github_feeds_succeeded=batch.github_feeds_succeeded,
        source_attempts=list(batch.source_attempts),
        workday_attempted=batch.workday_attempted,
        workday_succeeded=batch.workday_succeeded,
        workday_failed=batch.workday_failed,
        workday_request_attempts=batch.workday_request_attempts,
        workday_retry_attempts=batch.workday_retry_attempts,
        workday_failure_codes=Counter(dict(batch.workday_failure_codes)),
    )


def _ephemeral_health_state(
    attempts: list[SourceAttempt],
) -> tuple[dict[str, SourceHealthState], tuple[HealthTransition, ...]]:
    """Calculate replay diagnostics without reading or writing health history."""

    states: dict[str, SourceHealthState] = {}
    transitions: list[HealthTransition] = []
    for attempt in attempts:
        previous = states.get(attempt.health_key)
        current = calculate_next_state(previous, attempt)
        states[current.health_key] = current
        transition = transition_for(previous, current)
        if transition is not None:
            transitions.append(transition)
    return states, tuple(transitions)


@dataclass
class _DirectFetchOutcome:
    """Everything one direct fetch produced, captured inside its worker."""

    rows: list[dict] = field(default_factory=list)
    succeeded: bool = False
    error: Exception | None = None
    error_kind: str = ""
    workday_failure_code: str = ""
    request_count: int = 1
    retry_count: int = 0
    status_code: int | None = None
    challenge_response: bool = False
    diagnostics: DirectSourceDiagnostics | None = None


@dataclass
class _GithubFetchOutcome:
    rows: list[dict] = field(default_factory=list)
    succeeded: bool = False
    error: Exception | None = None
    error_kind: str = ""
    status_code: int | None = None
    challenge_response: bool = False


class _DirectSourceProvider:
    """Resolve direct adapters safely for serial and concurrent collection.

    Injected registries are reused as-is so existing callers keep their exact
    behavior. Production collection (no injection) builds one adapter set per
    worker thread, because adapters such as Workday keep per-fetch diagnostics
    on the instance. The Workday tenant pacer is deliberately shared across
    those per-thread adapters so concurrency cannot weaken tenant pacing.
    """

    def __init__(
        self,
        direct_sources: dict[str, object] | None,
        *,
        concurrent: bool,
    ) -> None:
        self._injected = direct_sources
        self._local = threading.local()
        self._shared: dict[str, object] | None = None
        self._workday_pacer: WorkdayPacer | None = None
        if direct_sources is None:
            if concurrent:
                self._workday_pacer = WorkdayPacer(workday_min_interval_seconds())
                self._supported = frozenset(_DEFAULT_DIRECT_ADAPTERS)
            else:
                self._shared = _default_direct_sources()
                self._supported = frozenset(self._shared)
        else:
            self._supported = frozenset(direct_sources)

    def supports(self, ats: str) -> bool:
        return ats in self._supported

    def get(self, ats: str) -> object | None:
        if self._injected is not None:
            return self._injected.get(ats)
        if self._shared is not None:
            return self._shared.get(ats)
        mapping = getattr(self._local, "mapping", None)
        if mapping is None:
            mapping = _default_direct_sources(workday_pacer=self._workday_pacer)
            self._local.mapping = mapping
        return mapping.get(ats)

    def workday_start_telemetry(self) -> WorkdayStartTelemetry | None:
        pacer = self._workday_pacer
        if pacer is None:
            registry = self._injected if self._injected is not None else self._shared
            source = registry.get("workday") if registry is not None else None
            candidate = getattr(source, "_pacer", None)
            pacer = candidate if isinstance(candidate, WorkdayPacer) else None
        return pacer.start_telemetry() if pacer is not None else None


def _collect_rows(
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
    concurrency = (
        getattr(config, "collection_concurrency", None) or CollectionConcurrencyCfg()
    )
    scheduler = CollectionScheduler(concurrency)
    stats.collection_mode = concurrency.mode
    source_provider = _DirectSourceProvider(
        direct_sources,
        concurrent=concurrency.concurrent,
    )
    configured_sources = config.effective_github_listing_sources()
    configured_count = len(configured_sources)
    if github_source is None:
        github_sources = [
            (source_config, _build_github_source(source_config))
            for source_config in configured_sources
        ]
    elif isinstance(github_source, (list, tuple)):
        github_sources = [
            (_config_for_injected_source(source, configured_sources), source)
            for source in github_source
        ]
    else:
        github_sources = [
            (_config_for_injected_source(github_source, configured_sources), github_source)
        ]
    github_sources.sort(key=lambda item: _github_runtime_source_sort_key(*item))
    stats.github_feeds_configured = configured_count
    direct_rows: list[dict] = []
    github_rows: list[dict] = []
    errors: list[str] = []

    with _timed_stage("direct_source_collection"):
        # Plan in configuration order, execute under the active mode, then apply
        # every outcome in that same order. Serial and concurrent collection
        # therefore produce identical rows, errors, attempts, and counters.
        planned: list[tuple[str, CompanyCfg, int | None]] = []
        direct_tasks: list[CollectionTask] = []
        for company in config.companies:
            if company.ats in {"bespoke", "github_only"}:
                planned.append(("unsupported", company, None))
                continue
            if not source_provider.supports(company.ats):
                planned.append(("missing_adapter", company, None))
                continue
            planned.append(("fetch", company, len(direct_tasks)))
            direct_tasks.append(_direct_collection_task(company, source_provider))
        direct_results = scheduler.run(direct_tasks)
        for kind, company, index in planned:
            if kind == "unsupported":
                LOGGER.info(
                    "Skipping direct fetch for %s (%s).", company.name, company.ats
                )
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
            if kind == "missing_adapter":
                _record_error(
                    errors,
                    f"{company.name}: no source registered for ats '{company.ats}'",
                )
                stats.source_attempts.append(
                    _failed_attempt(
                        health_key=direct_health_key(company.name, company.ats),
                        run_id=active_run_id,
                        observed_at=active_observed_at,
                        source_kind=SOURCE_KIND_DIRECT,
                        company=company.name,
                        adapter=company.ats,
                        error_kind=ERROR_MISSING_ADAPTER,
                        error=RuntimeError(
                            f"no source registered for ats '{company.ats}'"
                        ),
                    )
                )
                continue
            assert index is not None
            _apply_direct_outcome(
                company,
                _direct_outcome_from_result(company, direct_results[index], stats),
                stats=stats,
                errors=errors,
                direct_rows=direct_rows,
                run_id=active_run_id,
                observed_at=active_observed_at,
            )
        stats.workday_start_telemetry = source_provider.workday_start_telemetry()

    with _timed_stage("github_backstop_collection"):
        github_plan = [
            _github_feed_plan(source_config, source)
            for source_config, source in github_sources
        ]
        github_results = scheduler.run(
            [
                _github_collection_task(plan, config)
                for plan in github_plan
            ]
        )
        for plan, result in zip(github_plan, github_results):
            _apply_github_outcome(
                plan,
                _github_outcome_from_result(plan, result, stats),
                stats=stats,
                errors=errors,
                github_rows=github_rows,
                run_id=active_run_id,
                observed_at=active_observed_at,
            )

    stats.collection_concurrency = scheduler.metrics()
    log_collection_concurrency(stats.collection_concurrency)

    # Direct rows first: backend dedupe keeps the first row's extra metadata,
    # so this implements the direct-over-GitHub source-priority rule.
    return [*direct_rows, *github_rows], errors


@dataclass(frozen=True)
class _GithubFeedPlan:
    """Stable identity for one configured GitHub backstop feed."""

    source_config: GitHubListingSourceCfg | None
    source: object
    source_name: str
    adapter: str
    label: str
    health_key: str
    url: str


def _direct_collection_task(
    company: CompanyCfg,
    source_provider: "_DirectSourceProvider",
) -> CollectionTask:
    """Describe one direct fetch and the scopes that bound it."""

    def run() -> _DirectFetchOutcome:
        # Resolved inside the worker so concurrent mode uses that thread's
        # adapter instance rather than sharing per-fetch adapter state.
        source = source_provider.get(company.ats)
        return _fetch_direct_source(company, source)

    return CollectionTask(
        key=f"direct:{company.ats}:{company.name}",
        origin=direct_origin_key(
            company.ats,
            token=company.token,
            workday_shard=company.workday_shard,
            oracle_hcm_host=company.oracle_hcm_host,
            talentbrew_host=company.talentbrew_host,
        ),
        provider=company.ats,
        run=run,
        workday=company.ats == "workday",
    )


def _fetch_direct_source(
    company: CompanyCfg,
    source: object,
) -> _DirectFetchOutcome:
    """Fetch one direct source, classifying every failure exactly as before."""

    rows: list[dict] = []
    succeeded = False
    error: Exception | None = None
    error_kind = ""
    workday_failure_code = ""
    fetch_started = time.perf_counter()
    try:
        LOGGER.info("Fetching %s via %s...", company.name, company.ats)
        rows = list(source.fetch(company))
        succeeded = True
        LOGGER.info("Fetched %d direct row(s) for %s.", len(rows), company.name)
    except SourceSchemaError as exc:
        error, error_kind, workday_failure_code = exc, ERROR_SCHEMA, "schema_failure"
    except SourceFetchError as exc:
        error, error_kind, workday_failure_code = exc, _fetch_error_kind(exc), exc.error_code
    except SourceError as exc:
        error, error_kind, workday_failure_code = exc, ERROR_SOURCE, "source_failure"
    except Exception as exc:  # defensive run-loop boundary
        error, error_kind, workday_failure_code = exc, ERROR_UNEXPECTED, "unexpected_exception"
    finally:
        request_count, retry_count = _source_request_counts(source, error=error)
        _log_source_timing(
            company=company.name,
            adapter=company.ats,
            success=succeeded,
            elapsed_seconds=time.perf_counter() - fetch_started,
            rows_returned=len(rows) if succeeded else 0,
            source=source,
            error=error,
            request_count=request_count,
            retry_count=retry_count,
        )
    return _DirectFetchOutcome(
        rows=rows if succeeded else [],
        succeeded=succeeded,
        error=error,
        error_kind=error_kind,
        workday_failure_code=workday_failure_code,
        request_count=request_count if request_count is not None else 1,
        retry_count=retry_count or 0,
        status_code=_http_status_from_error(error),
        challenge_response=_challenge_response(error),
        diagnostics=_direct_diagnostics_from_source(
            source,
            rows=rows if succeeded else [],
            succeeded=succeeded,
            error_kind=error_kind,
        ),
    )


def _direct_outcome_from_result(
    company: CompanyCfg,
    result: TaskResult,
    stats: CollectionStats,
) -> _DirectFetchOutcome:
    """Convert an escaped worker error into an ordinary failed outcome."""

    if isinstance(result.value, _DirectFetchOutcome) and result.error is None:
        return result.value
    error = result.error or RuntimeError(
        f"collection task returned no outcome for {company.name}"
    )
    stats.unexpected_task_exceptions += 1
    LOGGER.error(
        "Collection task for %s failed unexpectedly: %s",
        company.name,
        sanitize_error(f"{type(error).__name__}: {error}"),
    )
    return _DirectFetchOutcome(
        succeeded=False,
        error=error if isinstance(error, Exception) else RuntimeError(str(error)),
        error_kind=ERROR_UNEXPECTED,
        workday_failure_code="unexpected_exception",
    )


def _apply_direct_outcome(
    company: CompanyCfg,
    outcome: _DirectFetchOutcome,
    *,
    stats: CollectionStats,
    errors: list[str],
    direct_rows: list[dict],
    run_id: str,
    observed_at: datetime,
) -> None:
    is_workday = company.ats == "workday"
    if is_workday:
        stats.workday_attempted += 1
    if outcome.status_code is not None:
        stats.http_status_counts[outcome.status_code] += 1
    if outcome.challenge_response:
        stats.challenge_responses += 1
    if outcome.succeeded:
        direct_rows.extend(outcome.rows)
        stats.source_attempts.append(
            _successful_attempt(
                health_key=direct_health_key(company.name, company.ats),
                run_id=run_id,
                observed_at=observed_at,
                source_kind=SOURCE_KIND_DIRECT,
                company=company.name,
                adapter=company.ats,
                rows_returned=len(outcome.rows),
                diagnostics=outcome.diagnostics,
            )
        )
        if is_workday:
            stats.workday_succeeded += 1
            stats.workday_request_attempts += outcome.request_count
            stats.workday_retry_attempts += outcome.retry_count
        return
    error = outcome.error or RuntimeError("unknown direct source failure")
    if is_workday:
        _record_workday_failure(
            stats,
            outcome.workday_failure_code,
            request_count=outcome.request_count,
            retry_count=outcome.retry_count,
        )
    if outcome.error_kind == ERROR_UNEXPECTED:
        _record_error(
            errors,
            f"{company.name}: unexpected {type(error).__name__}: {error}",
        )
    else:
        _record_error(errors, f"{company.name}: {error}")
    stats.source_attempts.append(
        _failed_direct_attempt(
            company,
            run_id,
            observed_at,
            outcome.error_kind or ERROR_SOURCE,
            error,
        )
    )


def _github_feed_plan(
    source_config: GitHubListingSourceCfg | None,
    source: object,
) -> _GithubFeedPlan:
    configured_url = source_config.url if source_config else _github_source_url(source)
    source_name = source_config.name if source_config else _github_source_name(source)
    adapter = (
        source_config.format if source_config else _github_source_adapter(source)
    )
    safe_url = sanitize_feed_label(configured_url or _github_source_label(source))
    label = (
        safe_url
        if source_config and source_config.name.startswith("legacy_simplify_")
        else sanitize_feed_label(f"{source_name} [{safe_url}]")
    )
    return _GithubFeedPlan(
        source_config=source_config,
        source=source,
        source_name=source_name,
        adapter=adapter,
        label=label,
        health_key=github_feed_health_key(configured_url or safe_url),
        url=configured_url or "",
    )


def _github_collection_task(
    plan: _GithubFeedPlan,
    config: WatcherConfig,
) -> CollectionTask:
    return CollectionTask(
        key=f"github:{plan.source_name}",
        origin=origin_key_for_url(plan.url),
        provider=PROVIDER_GITHUB_FEED,
        run=lambda: _fetch_github_source(plan, config),
    )


def _fetch_github_source(
    plan: _GithubFeedPlan,
    config: WatcherConfig,
) -> _GithubFetchOutcome:
    rows: list[dict] = []
    succeeded = False
    error: Exception | None = None
    error_kind = ""
    fetch_started = time.perf_counter()
    source = plan.source
    try:
        LOGGER.info("Fetching GitHub listings backstop source %s...", plan.label)
        if hasattr(source, "fetch_many"):
            rows.extend(source.fetch_many(config.companies))
        else:
            for company in config.companies:
                rows.extend(source.fetch(company))
        succeeded = True
        LOGGER.info(
            "Fetched %d GitHub backstop row(s) from %s.", len(rows), plan.label
        )
    except SourceSchemaError as exc:
        error, error_kind = exc, ERROR_SCHEMA
    except SourceFetchError as exc:
        error, error_kind = exc, ERROR_FETCH
    except SourceError as exc:
        error, error_kind = exc, ERROR_SOURCE
    except Exception as exc:  # defensive run-loop boundary
        error, error_kind = exc, ERROR_UNEXPECTED
    finally:
        _log_source_timing(
            company="all",
            adapter=plan.adapter,
            source_name=plan.source_name,
            success=succeeded,
            elapsed_seconds=time.perf_counter() - fetch_started,
            rows_returned=len(rows) if succeeded else 0,
            source=source,
            error=error,
        )
    return _GithubFetchOutcome(
        rows=rows if succeeded else [],
        succeeded=succeeded,
        error=error,
        error_kind=error_kind,
        status_code=_http_status_from_error(error),
        challenge_response=_challenge_response(error),
    )


def _github_outcome_from_result(
    plan: _GithubFeedPlan,
    result: TaskResult,
    stats: CollectionStats,
) -> _GithubFetchOutcome:
    if isinstance(result.value, _GithubFetchOutcome) and result.error is None:
        return result.value
    error = result.error or RuntimeError(
        f"collection task returned no outcome for {plan.source_name}"
    )
    stats.unexpected_task_exceptions += 1
    LOGGER.error(
        "GitHub backstop task %s failed unexpectedly: %s",
        plan.source_name,
        sanitize_error(f"{type(error).__name__}: {error}"),
    )
    return _GithubFetchOutcome(
        succeeded=False,
        error=error if isinstance(error, Exception) else RuntimeError(str(error)),
        error_kind=ERROR_UNEXPECTED,
    )


def _apply_github_outcome(
    plan: _GithubFeedPlan,
    outcome: _GithubFetchOutcome,
    *,
    stats: CollectionStats,
    errors: list[str],
    github_rows: list[dict],
    run_id: str,
    observed_at: datetime,
) -> None:
    if outcome.status_code is not None:
        stats.http_status_counts[outcome.status_code] += 1
    if outcome.challenge_response:
        stats.challenge_responses += 1
    if outcome.succeeded:
        github_rows.extend(outcome.rows)
        stats.github_feeds_succeeded += 1
        stats.source_attempts.append(
            _successful_attempt(
                health_key=plan.health_key,
                run_id=run_id,
                observed_at=observed_at,
                source_kind=SOURCE_KIND_GITHUB_FEED,
                company=None,
                adapter=plan.adapter,
                rows_returned=len(outcome.rows),
                feed_label=plan.label,
            )
        )
        return
    error = outcome.error or RuntimeError("unknown GitHub backstop failure")
    if outcome.error_kind == ERROR_UNEXPECTED:
        _record_error(
            errors,
            f"github listings ({plan.label}): unexpected "
            f"{type(error).__name__}: {_sanitize_error(error)}",
        )
    else:
        _record_error(
            errors,
            f"github listings ({plan.label}): {_sanitize_error(error)}",
        )
    stats.source_attempts.append(
        _failed_attempt(
            health_key=plan.health_key,
            run_id=run_id,
            observed_at=observed_at,
            source_kind=SOURCE_KIND_GITHUB_FEED,
            company=None,
            adapter=plan.adapter,
            error_kind=outcome.error_kind or ERROR_SOURCE,
            error=error,
            feed_label=plan.label,
        )
    )


def _http_status_from_error(error: Exception | None) -> int | None:
    """Return the observed HTTP status without reading raw response bodies."""

    if error is None:
        return None
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return status
    match = re.search(r"\bHTTP (\d{3})\b", str(error))
    if match:
        value = int(match.group(1))
        if 100 <= value <= 599:
            return value
    return None


def _challenge_response(error: Exception | None) -> bool:
    if error is None:
        return False
    if str(getattr(error, "error_code", "")) == "html_challenge":
        return True
    metadata = getattr(error, "response_metadata", None)
    if isinstance(metadata, dict):
        return str(metadata.get("body_kind") or "") == "html_challenge"
    return False


def _categorical_exclusion_audit(
    jobs: list[dict],
    *,
    target_roles: set[str] | frozenset[str],
) -> tuple[dict[str, object], ...]:
    exclusions: list[dict[str, object]] = []
    for job in jobs:
        if not is_internship(job) or not is_open(job):
            continue
        eligibility = determine_watcher_eligibility(job, target_roles)
        reason = eligibility.get("eligibility_exclusion_reason")
        if not reason:
            continue
        role = job.get("role_classification") or {}
        score = job.get("score") or {}
        exclusions.append(
            {
                "company": job.get("company", ""),
                "title": job.get("title", ""),
                "source_url": job.get("source_url", ""),
                "role": role.get("role", "unknown"),
                "role_track": score.get("role_track")
                or role.get("role_track")
                or "unknown",
                "exclusion_reason": reason,
                "evidence_source": eligibility.get("eligibility_evidence_source"),
                "evidence": eligibility.get("eligibility_evidence"),
                "mandatory_language_detected": eligibility.get(
                    "eligibility_mandatory_language_detected"
                ),
                "negation_detected": eligibility.get(
                    "eligibility_negation_detected"
                ),
                "mixed_eligibility_detected": eligibility.get(
                    "eligibility_mixed_eligibility_detected"
                ),
            }
        )
    return tuple(exclusions)


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
    _print_collection_mode(result, output=output)
    _print_workday_transport(getattr(result, "workday_transport", WorkdayTransportSummary()), output)
    _print_source_health(result, output=output)
    comparison = getattr(result, "source_comparison", None)
    if comparison is not None:
        print("Source comparison:", file=output)
        print(
            f"  GitHub-only eligible: {comparison.counts.get(CATEGORY_GITHUB_ONLY, 0)}",
            file=output,
        )
        print(
            f"  Direct-only eligible: {comparison.counts.get(CATEGORY_DIRECT_ONLY, 0)}",
            file=output,
        )
        print(
            f"  Both sources merged: {comparison.counts.get(CATEGORY_BOTH, 0)}",
            file=output,
        )
    alert_result = getattr(result, "health_alert_result", None)
    if alert_result is not None:
        print("Source-health email:", file=output)
        print(f"  Mode: {alert_result.mode}", file=output)
        print(f"  Alert candidates: {alert_result.candidates}", file=output)
        print(f"  Sent: {'yes' if alert_result.sent else 'no'}", file=output)
        print(
            f"  Suppressed by cooldown: {alert_result.suppressed_by_cooldown}",
            file=output,
        )
        if alert_result.error:
            print(f"  Error: {alert_result.error}", file=output)
    if result.errors:
        print(f"Source errors: {len(result.errors)}", file=output)
        for error in result.errors:
            print(f"  - {error}", file=output)

    previously_emailed = list(getattr(result, "previously_emailed", ()) or ())
    explicitly_primed = list(getattr(result, "explicitly_primed", ()) or ())
    print("Notification summary:", file=output)
    print(f"  Mode: {getattr(result, 'notification_mode', 'unknown')}", file=output)
    print(f"  Total eligible matches: {len(getattr(result, 'matches', ()) or ())}", file=output)
    print(f"  New postings eligible for email: {len(result.new_matches)}", file=output)
    print(f"  Previously emailed postings suppressed: {len(previously_emailed)}", file=output)
    print(f"  Explicitly primed postings suppressed: {len(explicitly_primed)}", file=output)
    print(
        f"  Current dry-run postings left pending: {getattr(result, 'dry_run_pending', 0)}",
        file=output,
    )
    print(
        "  Genuine cross-source duplicates merged: "
        f"{getattr(result, 'cross_source_duplicates_merged', 0)}",
        file=output,
    )
    eligibility_exclusions = tuple(
        getattr(result, "eligibility_exclusions", ()) or ()
    )
    print(
        f"  Categorical eligibility exclusions: {len(eligibility_exclusions)}",
        file=output,
    )
    for item in eligibility_exclusions:
        print(
            f"  - {item.get('company', '')} - {item.get('title', '')}: "
            f"{item.get('exclusion_reason', 'unknown')} "
            f"[{item.get('evidence_source') or 'unknown evidence'}] "
            f"{item.get('evidence') or ''}",
            file=output,
        )
    _print_suppressed_postings("Previously emailed", previously_emailed, output=output)
    _print_suppressed_postings("Explicitly primed", explicitly_primed, output=output)

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
    health_alert = getattr(result, "health_alert_result", None)
    comparison = getattr(result, "source_comparison", None)
    print(
        "HEARTBEAT: ran, "
        f"rows_fetched={result.rows_fetched}, "
        f"jobs_scored={result.jobs_scored}, "
        f"matches={len(result.matches)}, "
        f"new={len(result.new_matches)}, "
        f"emailed_suppressed={len(getattr(result, 'previously_emailed', ()) or ())}, "
        f"primed_suppressed={len(getattr(result, 'explicitly_primed', ()) or ())}, "
        f"dry_run_pending={getattr(result, 'dry_run_pending', 0)}, "
        f"cross_source_duplicates_merged={getattr(result, 'cross_source_duplicates_merged', 0)}, "
        f"errors={len(result.errors)}, "
        f"notification_mode={getattr(result, 'notification_mode', 'unknown')}, "
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
        f"direct_healthy_with_listings={_health_value(health, 'direct_healthy_with_listings')}, "
        f"direct_healthy_empty={_health_value(health, 'direct_healthy_empty')}, "
        f"direct_failed={_health_value(health, 'direct_failed')}, "
        f"direct_not_configured={_health_value(health, 'direct_not_configured')}, "
        f"direct_unknown={_health_value(health, 'direct_unknown')}, "
        f"github_feeds_healthy={_health_value(health, 'github_feeds_healthy')}, "
        f"backstop_only_companies={_health_value(health, 'backstop_only_companies')}, "
        f"uncovered_companies={_health_value(health, 'uncovered_companies')}, "
        f"health_transitions={_health_value(health, 'health_transitions')}, "
        f"health_recoveries={_health_value(health, 'health_recoveries')}, "
        f"health_email_mode={getattr(health_alert, 'mode', HEALTH_EMAIL_OFF)}, "
        f"health_alert_candidates={getattr(health_alert, 'candidates', 0)}, "
        f"health_alert_sent={'yes' if getattr(health_alert, 'sent', False) else 'no'}, "
        f"health_alert_suppressed_by_cooldown={getattr(health_alert, 'suppressed_by_cooldown', 0)}, "
        f"health_recovery_alerts={getattr(health_alert, 'recovery_alerts', 0)}, "
        f"health_alert_error={'yes' if getattr(health_alert, 'error', None) else 'no'}, "
        f"source_comparison_github_only={_comparison_value(comparison, CATEGORY_GITHUB_ONLY)}, "
        f"source_comparison_direct_only={_comparison_value(comparison, CATEGORY_DIRECT_ONLY)}, "
        f"source_comparison_both={_comparison_value(comparison, CATEGORY_BOTH)}, "
        f"source_comparison_persisted={'yes' if getattr(result, 'source_comparison_persisted', False) else 'no'}, "
        f"workday_attempted={getattr(getattr(result, 'workday_transport', None), 'attempted_tenants', 0)}, "
        f"workday_succeeded={getattr(getattr(result, 'workday_transport', None), 'successful_tenants', 0)}, "
        f"workday_failed={getattr(getattr(result, 'workday_transport', None), 'failed_tenants', 0)}, "
        f"workday_retry_attempts={getattr(getattr(result, 'workday_transport', None), 'retry_attempts', 0)}, "
        f"workday_shared_incident={int(bool(getattr(getattr(result, 'workday_transport', None), 'likely_shared_incident', False)))}, "
        f"collection_mode={getattr(result, 'collection_mode', COLLECTION_MODE_SERIAL)}, "
        f"collection_max_workers={_concurrency_value(result, 'max_workers')}, "
        f"collection_max_observed_concurrency={_concurrency_value(result, 'max_observed_global')}, "
        f"collection_max_observed_origin_concurrency={_concurrency_value(result, 'max_observed_per_origin')}, "
        f"collection_max_observed_workday_concurrency={_concurrency_value(result, 'max_observed_workday')}, "
        f"collection_unexpected_task_exceptions={_concurrency_value(result, 'unexpected_exceptions')}, "
        f"alumni_csv_status={getattr(result, 'alumni_csv_status', 'unknown')}, "
        f"alumni_records_loaded={getattr(result, 'alumni_records_loaded', 0)}, "
        f"alumni_employers_indexed={getattr(result, 'alumni_employers_indexed', 0)}, "
        f"sent={sent}, "
        f"seen_marked={result.seen_marked}",
        file=output,
    )


def _parse_today(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--today must use YYYY-MM-DD"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    with _timed_stage("watcher_runtime"):
        with _timed_stage("configuration_startup"):
            parser = argparse.ArgumentParser(
                description="Run the internship watcher once and print new matches."
            )
            parser.add_argument(
                "--watchlist",
                default=str(DEFAULT_WATCHLIST_PATH),
                help="Path to watchlist.yml",
            )
            parser.add_argument("--seen-db", help="Path to SQLite seen-store")
            parser.add_argument(
                "--health-report",
                help="Write the sanitized machine-readable source-health JSON report to this path.",
            )
            parser.add_argument(
                "--prime-seen",
                "--mark-seen-without-send",
                dest="prime_seen",
                action="store_true",
                help="Explicitly prime/suppress current matches without sending email.",
            )
            snapshot_group = parser.add_mutually_exclusive_group()
            snapshot_group.add_argument(
                "--capture-collection-snapshot",
                metavar="PATH",
                help=(
                    "Save live collection as an atomic .json.gz snapshot, then "
                    "continue through the normal watcher pipeline."
                ),
            )
            snapshot_group.add_argument(
                "--replay-collection-snapshot",
                metavar="PATH",
                help=(
                    "Skip all network collection and process a saved .json.gz "
                    "collection snapshot in side-effect-free dry-run mode."
                ),
            )
            parser.add_argument(
                "--allow-collection-config-mismatch",
                action="store_true",
                help=(
                    "Intentionally replay a snapshot captured with different "
                    "collection-affecting configuration."
                ),
            )
            parser.add_argument(
                "--today",
                type=_parse_today,
                help=(
                    "Override the effective analysis date as YYYY-MM-DD; replay "
                    "otherwise uses the snapshot capture date."
                ),
            )
            args = parser.parse_args(argv)

            logging.basicConfig(
                level=logging.INFO,
                format="%(levelname)s %(name)s: %(message)s",
            )
            config = load_watchlist(args.watchlist)
            if args.seen_db:
                seen_db_path = Path(args.seen_db)
                config = replace(
                    config,
                    seen_db_path=seen_db_path,
                    analysis_cache_path=resolve_analysis_cache_path(
                        seen_db_path
                    ),
                )
            if (
                args.allow_collection_config_mismatch
                and not args.replay_collection_snapshot
            ):
                parser.error(
                    "--allow-collection-config-mismatch requires "
                    "--replay-collection-snapshot"
                )
            if args.replay_collection_snapshot and args.prime_seen:
                parser.error(
                    "--replay-collection-snapshot cannot be combined with --prime-seen"
                )
            replay_batch = None
            if args.replay_collection_snapshot:
                try:
                    replay_batch = load_collection_snapshot(
                        args.replay_collection_snapshot
                    )
                except CollectionSnapshotError as exc:
                    parser.error(str(exc))
            send_enabled = email_sending_enabled()
            if send_enabled and args.prime_seen:
                parser.error(
                    "WATCHER_SEND_EMAIL and --prime-seen cannot both be enabled"
                )
            notification_mode = (
                RUN_MODE_DRY
                if replay_batch is not None
                else
                RUN_MODE_PRIME
                if args.prime_seen
                else RUN_MODE_LIVE
                if send_enabled
                else RUN_MODE_DRY
            )

        try:
            with SeenStore(
                config.seen_db_path,
                read_only=replay_batch is not None,
            ) as seen_store:
                result = run_once(
                    config,
                    seen_store=seen_store,
                    notification_mode=notification_mode,
                    health_alert_policy=(
                        HealthAlertPolicy(mode=HEALTH_EMAIL_OFF)
                        if replay_batch is not None
                        else load_health_alert_policy()
                    ),
                    today=args.today,
                    collection_batch=replay_batch,
                    capture_collection_snapshot_path=(
                        args.capture_collection_snapshot
                    ),
                    replay_collection_snapshot_path=(
                        args.replay_collection_snapshot
                    ),
                    allow_collection_config_mismatch=(
                        args.allow_collection_config_mismatch
                    ),
                )
        except CollectionSnapshotError as exc:
            parser.error(str(exc))
        health_report_path = (
            args.health_report
            or os.getenv("WATCHER_HEALTH_REPORT_PATH", "").strip()
        )
        if health_report_path and replay_batch is not None:
            LOGGER.info(
                "Collection replay: source-health report was not written."
            )
        elif health_report_path:
            _write_result_health_report(result, health_report_path)
            LOGGER.info("Wrote source-health JSON report: %s", health_report_path)
        print_report(result)
        print_heartbeat(result)
        return 0


_DEFAULT_DIRECT_ADAPTERS = frozenset(
    {
        "ashby",
        "greenhouse",
        "lever",
        "oracle_hcm",
        "smartrecruiters",
        "talentbrew",
        "workable",
        "workday",
    }
)


def _default_direct_sources(
    *,
    workday_pacer: WorkdayPacer | None = None,
) -> dict[str, object]:
    return {
        "ashby": AshbySource(),
        "greenhouse": GreenhouseSource(),
        "lever": LeverSource(),
        "oracle_hcm": OracleHcmSource(),
        "smartrecruiters": SmartRecruitersSource(),
        "talentbrew": TalentBrewSource(),
        "workable": WorkableSource(),
        "workday": WorkdaySource(pacer=workday_pacer),
    }


def _build_github_source(config: GitHubListingSourceCfg) -> object:
    if config.format == "simplify_json":
        return GitHubListingsSource(config.url, source_name=config.name)
    if config.format == "github_markdown_table":
        return GitHubMarkdownTableSource(
            config.url,
            source_name=config.name,
            default_term=config.default_term,
        )
    raise ValueError(f"Unsupported GitHub listing source format: {config.format}")


def _config_for_injected_source(
    source: object,
    configured_sources: tuple[GitHubListingSourceCfg, ...],
) -> GitHubListingSourceCfg | None:
    source_url = sanitize_feed_label(_github_source_url(source))
    for configured in configured_sources:
        if sanitize_feed_label(configured.url) == source_url:
            return configured
    return None


def _github_runtime_source_sort_key(
    config: GitHubListingSourceCfg | None,
    source: object,
) -> tuple[int, str, str]:
    if config is not None:
        return config.priority, config.name.casefold(), config.url
    try:
        priority = int(getattr(source, "priority", 50))
    except (TypeError, ValueError):
        priority = 50
    return priority, _github_source_name(source).casefold(), _github_source_url(source)


def _record_error(errors: list[str], message: str) -> None:
    safe_message = sanitize_error(message)
    LOGGER.warning(safe_message)
    errors.append(safe_message)


def _log_suppressed_postings(label: str, jobs: list[dict], *, limit: int = 10) -> None:
    for job in jobs[:limit]:
        LOGGER.info(
            "Suppressed (%s): %s - %s",
            label,
            str(job.get("company") or "")[:120],
            str(job.get("title") or "")[:160],
        )
    if len(jobs) > limit:
        LOGGER.info(
            "Suppressed (%s): %d additional posting(s) omitted from diagnostics.",
            label,
            len(jobs) - limit,
        )


def _print_suppressed_postings(
    label: str,
    jobs: list[dict],
    *,
    output: TextIO,
    limit: int = 10,
) -> None:
    if not jobs:
        return
    print(f"{label} (bounded list):", file=output)
    for job in jobs[:limit]:
        print(
            f"  - {str(job.get('company') or '')[:120]} - "
            f"{str(job.get('title') or '')[:160]}",
            file=output,
        )
    if len(jobs) > limit:
        print(f"  - ... {len(jobs) - limit} more", file=output)


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
    diagnostics: DirectSourceDiagnostics | None = None,
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
        malformed_row_count=(
            diagnostics.malformed_row_count if diagnostics is not None else None
        ),
        schema_error_row_count=(
            diagnostics.schema_error_row_count if diagnostics is not None else None
        ),
        duplicate_row_count=(
            diagnostics.duplicate_row_count if diagnostics is not None else None
        ),
        failed_request_count=(
            diagnostics.failed_request_count if diagnostics is not None else None
        ),
        incomplete=diagnostics.incomplete if diagnostics is not None else None,
        truncated=diagnostics.truncated if diagnostics is not None else None,
        reason_codes=diagnostics.reason_codes if diagnostics is not None else (),
        degraded=diagnostics.degraded if diagnostics is not None else None,
        complete=diagnostics.complete if diagnostics is not None else None,
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
    direct = source_kind == SOURCE_KIND_DIRECT
    reason_code = safe_error_kind(error_kind) or ERROR_SOURCE
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
        malformed_row_count=0 if direct else None,
        schema_error_row_count=0 if direct else None,
        duplicate_row_count=0 if direct else None,
        failed_request_count=1 if direct else None,
        incomplete=True if direct else None,
        truncated=False if direct else None,
        reason_codes=(reason_code,) if direct else (),
        degraded=False if direct else None,
        complete=False if direct else None,
    )


def _direct_diagnostics_from_source(
    source: object,
    *,
    rows: list[dict],
    succeeded: bool,
    error_kind: str,
) -> DirectSourceDiagnostics | None:
    """Map adapter-owned bounded counters into the shared attempt contract."""

    if not succeeded:
        return DirectSourceDiagnostics(
            succeeded=False,
            failed_request_count=1,
            incomplete=True,
            reason_codes=(safe_error_kind(error_kind) or ERROR_SOURCE,),
            complete=False,
        )
    shared = getattr(source, "last_health_diagnostics", None)
    if isinstance(shared, DirectSourceDiagnostics):
        return shared

    diagnostics = getattr(source, "last_diagnostics", None)
    adapter = str(getattr(source, "name", "") or "").casefold()
    if diagnostics is None:
        return None
    if adapter == "workday":
        skip_reasons = dict(getattr(diagnostics, "skip_reasons", ()) or ())
        malformed = int(skip_reasons.get("posting_not_object", 0) or 0)
        skipped = int(getattr(diagnostics, "malformed_postings_skipped", 0) or 0)
        schema_errors = max(0, skipped - malformed)
        recovered_retries = int(getattr(diagnostics, "retry_attempts", 0) or 0)
        detail_failures = int(getattr(diagnostics, "detail_failures", 0) or 0)
        failed_requests = recovered_retries
        failed_requests += detail_failures
        failed_requests += int(getattr(diagnostics, "disappeared_postings", 0) or 0)
        listing_incomplete = bool(
            getattr(diagnostics, "listing_incomplete", False)
        )
        degraded = bool(getattr(diagnostics, "detail_enrichment_degraded", False))
        degraded = degraded or listing_incomplete
        degraded = degraded or skipped > 0
        degraded = degraded or recovered_retries > 0
        incomplete = bool(
            listing_incomplete
            or skipped > 0
            or getattr(diagnostics, "detail_enrichment_degraded", False)
        )
        reasons: list[str] = []
        if malformed:
            reasons.append("malformed_records_skipped")
        if schema_errors:
            reasons.append("schema_invalid_records_skipped")
        if getattr(diagnostics, "detail_enrichment_degraded", False):
            reasons.append("material_enrichment_failed")
        elif detail_failures:
            reasons.append("optional_enrichment_failed")
        if recovered_retries:
            reasons.append("request_retry_recovered")
        reasons.extend(
            str(code)
            for code, _count in getattr(diagnostics, "detail_failure_reasons", ()) or ()
        )
        reasons.extend(
            str(code)
            for code in getattr(diagnostics, "listing_incomplete_reasons", ()) or ()
        )
        return DirectSourceDiagnostics(
            succeeded=True,
            retained_row_count=len(rows),
            malformed_row_count=malformed,
            schema_error_row_count=schema_errors,
            failed_request_count=failed_requests,
            incomplete=incomplete,
            reason_codes=tuple(dict.fromkeys(reasons))[:12],
            degraded=degraded,
            complete=not incomplete,
        )
    if adapter == "oracle_hcm":
        malformed = int(
            getattr(diagnostics, "malformed_postings_skipped", 0) or 0
        )
        schema_errors = int(
            getattr(diagnostics, "schema_error_postings_skipped", 0) or 0
        )
        reasons = []
        if malformed:
            reasons.append("malformed_records_skipped")
        if schema_errors:
            reasons.append("schema_invalid_records_skipped")
        recovered_retries = int(
            getattr(diagnostics, "retry_attempts", 0) or 0
        )
        if recovered_retries:
            reasons.append("request_retry_recovered")
        incomplete = bool(malformed or schema_errors)
        degraded = bool(incomplete or recovered_retries)
        return DirectSourceDiagnostics(
            succeeded=True,
            retained_row_count=len(rows),
            malformed_row_count=malformed,
            schema_error_row_count=schema_errors,
            duplicate_row_count=int(
                getattr(diagnostics, "duplicate_postings_skipped", 0) or 0
            ),
            failed_request_count=recovered_retries,
            incomplete=incomplete,
            reason_codes=tuple(reasons),
            degraded=degraded,
            complete=not incomplete,
        )
    if adapter == "talentbrew":
        recovered_retries = int(
            getattr(diagnostics, "retry_attempts", 0) or 0
        )
        return DirectSourceDiagnostics(
            succeeded=True,
            retained_row_count=len(rows),
            duplicate_row_count=int(
                getattr(diagnostics, "duplicate_postings_skipped", 0) or 0
            ),
            failed_request_count=recovered_retries,
            incomplete=False,
            reason_codes=("request_retry_recovered",) if recovered_retries else (),
            degraded=bool(recovered_retries),
            complete=True,
        )
    return None


def _fetch_error_kind(error: SourceFetchError) -> str:
    code = re.sub(r"[^a-z0-9_.-]+", "_", str(error.error_code or "").casefold()).strip("_")
    return ERROR_FETCH if not code or code == ERROR_FETCH else f"{ERROR_FETCH}/{code}"


def _record_workday_failure(
    stats: CollectionStats,
    code: str,
    *,
    request_count: int,
    retry_count: int,
) -> None:
    stable_code = re.sub(r"[^a-z0-9_.-]+", "_", str(code or "unknown").casefold()).strip("_")
    stats.workday_failed += 1
    stats.workday_request_attempts += max(1, int(request_count))
    stats.workday_retry_attempts += max(0, int(retry_count))
    stats.workday_failure_codes[stable_code or "unknown"] += 1


def summarize_workday_transport(stats: CollectionStats) -> WorkdayTransportSummary:
    dominant_error = "none"
    dominant_count = 0
    if stats.workday_failure_codes:
        dominant_error, dominant_count = sorted(
            stats.workday_failure_codes.items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
    shared = (
        stats.workday_failed >= 5
        and dominant_error in WORKDAY_TRANSPORT_ERROR_CODES
        and dominant_count * 100 >= stats.workday_failed * 60
    )
    return WorkdayTransportSummary(
        attempted_tenants=stats.workday_attempted,
        successful_tenants=stats.workday_succeeded,
        failed_tenants=stats.workday_failed,
        retry_attempts=stats.workday_retry_attempts,
        dominant_error=dominant_error,
        dominant_error_count=dominant_count,
        likely_shared_incident=shared,
    )


def _log_workday_transport(summary: WorkdayTransportSummary) -> None:
    LOGGER.warning(
        "WORKDAY TRANSPORT SUMMARY: attempted_tenants=%d successful_tenants=%d "
        "failed_tenants=%d retry_attempts=%d dominant_error=%s dominant_error_count=%d "
        "likely_shared_incident=%s",
        summary.attempted_tenants,
        summary.successful_tenants,
        summary.failed_tenants,
        summary.retry_attempts,
        summary.dominant_error,
        summary.dominant_error_count,
        "yes" if summary.likely_shared_incident else "no",
    )


def _print_collection_mode(result: RunResult, *, output: TextIO) -> None:
    metrics = getattr(result, "collection_concurrency", None)
    print("Collection:", file=output)
    print(
        f"  Mode: {getattr(result, 'collection_mode', COLLECTION_MODE_SERIAL)}",
        file=output,
    )
    if metrics is None:
        return
    print(
        "  Configured limits: "
        f"workers={metrics.max_workers} "
        f"per_origin={metrics.per_origin_limit} "
        f"workday={metrics.workday_limit}",
        file=output,
    )
    print(
        "  Maximum observed concurrency: "
        f"global={metrics.max_observed_global} "
        f"per_origin={metrics.max_observed_per_origin} "
        f"provider={metrics.max_observed_provider} "
        f"workday={metrics.max_observed_workday}",
        file=output,
    )
    print(
        f"  Unexpected task exceptions: {metrics.unexpected_exceptions}",
        file=output,
    )
    print(
        "  Executor shutdown clean: "
        f"{'yes' if metrics.executor_shutdown_clean else 'no'}",
        file=output,
    )


def _print_workday_transport(summary: WorkdayTransportSummary, output: TextIO) -> None:
    print("Workday transport:", file=output)
    print(f"  Attempted tenants: {summary.attempted_tenants}", file=output)
    print(f"  Successful tenants: {summary.successful_tenants}", file=output)
    print(f"  Failed tenants: {summary.failed_tenants}", file=output)
    print(f"  Retry attempts: {summary.retry_attempts}", file=output)
    print(
        f"  Dominant error: {summary.dominant_error} ({summary.dominant_error_count})",
        file=output,
    )
    print(
        f"  Likely shared incident: {'yes' if summary.likely_shared_incident else 'no'}",
        file=output,
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
    print(
        f"  Direct healthy with listings: {summary.direct_healthy_with_listings}",
        file=output,
    )
    print(f"  Direct healthy empty: {summary.direct_healthy_empty}", file=output)
    print(f"  Direct degraded: {summary.direct_degraded}", file=output)
    print(f"  Direct failed: {summary.direct_failed}", file=output)
    print(f"  Direct not configured: {summary.direct_not_configured}", file=output)
    print(f"  Direct unknown: {summary.direct_unknown}", file=output)
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
    direct_states = sorted(
        (state for state in states if state.source_kind == SOURCE_KIND_DIRECT),
        key=lambda item: (item.company or "").casefold(),
    )
    if direct_states:
        print("Direct company health:", file=output)
        for state in direct_states:
            print(
                f"  - {state.company or state.health_key} [{state.adapter}]: "
                f"{state.status}, listings={state.last_rows_returned if state.last_rows_returned is not None else 'unknown'}, "
                f"malformed={state.last_malformed_row_count if state.last_malformed_row_count is not None else 'unknown'}, "
                f"schema_errors={state.last_schema_error_row_count if state.last_schema_error_row_count is not None else 'unknown'}, "
                f"duplicates={state.last_duplicate_row_count if state.last_duplicate_row_count is not None else 'unknown'}, "
                f"failed_requests={state.last_failed_request_count if state.last_failed_request_count is not None else 'unknown'}, "
                f"complete={state.last_complete if state.last_complete is not None else 'unknown'}",
                file=output,
            )
    actionable = [
        state
        for state in states
        if state.status
        in {
            STATUS_DEGRADED,
            STATUS_FAILING,
            DIRECT_STATUS_DEGRADED,
            DIRECT_STATUS_FAILED,
            DIRECT_STATUS_UNKNOWN,
        }
    ]
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
        "Source health run_id=%s companies=%d direct_healthy_with_listings=%d "
        "direct_healthy_empty=%d direct_degraded=%d direct_failed=%d "
        "direct_not_configured=%d direct_unknown=%d "
        "github_healthy=%d/%d uncovered=%d transitions=%d recoveries=%d",
        run_id,
        summary.companies_configured,
        summary.direct_healthy_with_listings,
        summary.direct_healthy_empty,
        summary.direct_degraded,
        summary.direct_failed,
        summary.direct_not_configured,
        summary.direct_unknown,
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
        if state.status in {
            STATUS_DEGRADED,
            STATUS_FAILING,
            DIRECT_STATUS_DEGRADED,
            DIRECT_STATUS_FAILED,
            DIRECT_STATUS_UNKNOWN,
        }:
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
            "health_email_mode": result.health_alert_result.mode,
            "health_alert_candidates": result.health_alert_result.candidates,
            "health_alert_sent": result.health_alert_result.sent,
            "health_alert_suppressed_by_cooldown": (
                result.health_alert_result.suppressed_by_cooldown
            ),
            "health_recovery_alerts": result.health_alert_result.recovery_alerts,
            "health_alert_error": bool(result.health_alert_result.error),
            "source_comparison_github_only": _comparison_value(
                result.source_comparison,
                CATEGORY_GITHUB_ONLY,
            ),
            "source_comparison_direct_only": _comparison_value(
                result.source_comparison,
                CATEGORY_DIRECT_ONLY,
            ),
            "source_comparison_both": _comparison_value(
                result.source_comparison,
                CATEGORY_BOTH,
            ),
            "source_comparison_persisted": result.source_comparison_persisted,
            "workday_transport": {
                "attempted_tenants": result.workday_transport.attempted_tenants,
                "successful_tenants": result.workday_transport.successful_tenants,
                "failed_tenants": result.workday_transport.failed_tenants,
                "retry_attempts": result.workday_transport.retry_attempts,
                "dominant_error": result.workday_transport.dominant_error,
                "dominant_error_count": result.workday_transport.dominant_error_count,
                "likely_shared_incident": result.workday_transport.likely_shared_incident,
            },
        },
    )


def _concurrency_value(result: RunResult, field_name: str) -> int:
    metrics = getattr(result, "collection_concurrency", None)
    return int(getattr(metrics, field_name, 0) or 0)


def _health_value(summary: HealthSummary | None, field_name: str) -> int:
    return int(getattr(summary, field_name, 0) or 0)


def _comparison_value(
    report: SourceComparisonReport | None,
    category: str,
) -> int:
    return int(report.counts.get(category, 0)) if report else 0


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


def _github_source_url(source: object) -> str:
    return str(getattr(source, "url", "") or getattr(source, "feed_label", "") or "")


def _github_source_name(source: object) -> str:
    return str(
        getattr(source, "source_name", "")
        or getattr(source, "name", "")
        or "injected"
    )


def _github_source_adapter(source: object) -> str:
    return str(
        getattr(source, "format", "")
        or getattr(source, "name", "")
        or "github_listings"
    )


def _sanitize_error(error: Exception) -> str:
    return sanitize_error(error)


if __name__ == "__main__":
    raise SystemExit(main())
