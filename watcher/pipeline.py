"""Watcher pipeline orchestration: one run from collection to :class:`RunResult`.

:func:`run_once` sequences the stages a watcher run performs -- run/replay
initialization, live collection or snapshot replay, source-health state,
analysis and caching, dedupe enrichment, eligibility and match selection,
alumni attachment, seen/notification state, source comparison, and
source-health alerts -- and returns everything the reporting layer prints.
Collection mechanics live in :mod:`watcher.collection`; console and heartbeat
rendering lives in :mod:`watcher.reporting`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from watcher.alumni import (
    AlumniIndex,
    attach_alumni,
    load_default_alumni,
    status_for_injected_index,
)
from watcher.analysis_cache import (
    AnalysisCacheStats,
    analyze_rows_with_cache,
)
from watcher.audit_trace import enrich_duplicate_entries
from watcher.collection import (
    CollectionStats,
    WorkdayTransportSummary,
    _collection_stats_from_batch,
    _log_workday_transport,
    collect_batch,
    summarize_workday_transport,
)
from watcher.collection_concurrency import CollectionConcurrencyMetrics
from watcher.collection_snapshot import (
    CollectionBatch,
    CollectionSnapshotError,
    collection_config_fingerprint,
    save_collection_snapshot,
)
from watcher.config import (
    COLLECTION_MODE_SERIAL,
    DEFAULT_ANALYSIS_CACHE_FILENAME,
    WatcherConfig,
)
from watcher.eligibility import determine_watcher_eligibility
from watcher.filters import filter_matches, is_internship, is_open
from watcher.generation import ShadowGenerationCandidate
from watcher.health_alerts import (
    MODE_OFF as HEALTH_EMAIL_OFF,
    HealthAlertPolicy,
    HealthAlertResult,
    evaluate_and_send_health_alerts,
)
from watcher.notify import email_sending_enabled, send_digest
from watcher.run_logging import LOGGER, _timed_stage, _timing_log_value
from watcher.season import (
    SEASON_ROLLOVER_DUE,
    SEASON_STALE,
    SEASON_UNKNOWN,
    company_season_warnings,
    season_status,
)
from watcher.seen_store import SeenStore
from watcher.source_comparison import (
    SourceComparisonReport,
    SourceComparisonStore,
    build_source_comparison,
)
from watcher.source_health import (
    COVERAGE_UNCOVERED,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_UNKNOWN,
    STATUS_DEGRADED,
    STATUS_FAILING,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceAttempt,
    SourceHealthState,
    SourceHealthStore,
    calculate_company_coverage,
    calculate_next_state,
    count_github_rows_by_company,
    new_run_id,
    sanitize_error,
    summarize_health,
    transition_for,
    utc_datetime,
)


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
    shadow_generation_candidates: tuple[ShadowGenerationCandidate, ...] = ()
    shadow_observations: int = 0
    jobs: list[dict] = field(default_factory=list)
    duplicate_report: list[dict] = field(default_factory=list)
    collection_replayed: bool = False
    collection_mode: str = COLLECTION_MODE_SERIAL
    collection_concurrency: CollectionConcurrencyMetrics | None = None


SHADOW_GENERATION_REPORT_LIMIT = 20


RUN_MODE_LIVE = "live_send"


RUN_MODE_DRY = "dry_run"


RUN_MODE_PRIME = "explicit_prime"


RUN_MODES = frozenset({RUN_MODE_LIVE, RUN_MODE_DRY, RUN_MODE_PRIME})


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
        # Per-company GitHub row counts come from the rows this run already
        # parsed, so alert severity can tell "GitHub covers this company" from
        # "some GitHub feed succeeded somewhere".
        count_github_rows_by_company(rows, config.companies),
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
    with _timed_stage("shadow_generation_observation"):
        # Shadow only: this records that postings were collected and reports
        # what *would* qualify as a new generation. It writes no notification
        # state, and `partition()` below is unchanged by it. Replay is
        # side-effect-free, so it never observes.
        shadow_candidates: tuple[ShadowGenerationCandidate, ...] = ()
        shadow_observations = 0
        if replay_mode:
            LOGGER.info(
                "Collection replay: shadow-generation observation was not recorded."
            )
        else:
            try:
                observation = seen_store.observe(
                    jobs,
                    observed_at=observed_at,
                    collection_health=_generation_absence_health(company_coverage),
                    # Every collected posting that already has a row is refreshed;
                    # only notification-eligible postings may add one, so the
                    # durable store keeps tracking the suppression surface rather
                    # than every posting these companies have ever published.
                    create_rows_for=matches,
                )
                shadow_candidates = observation.shadow_candidates
                shadow_observations = observation.observed
            except Exception as exc:  # shadow diagnostics must never block delivery
                LOGGER.error(
                    "Shadow-generation observation failed: %s",
                    sanitize_error(exc),
                )
        _log_shadow_generation(shadow_candidates, shadow_observations)
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
        shadow_generation_candidates=shadow_candidates,
        shadow_observations=shadow_observations,
        jobs=jobs,
        duplicate_report=duplicate_report,
        collection_replayed=replay_mode,
        collection_mode=collection_mode,
        collection_concurrency=collection_concurrency,
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


# Only a direct source that completed cleanly proves a specific company was
# represented this run. Any other status -- including `not_configured`, which is
# what backstop-only companies report -- yields no absence evidence.
_ABSENCE_HEALTHY_DIRECT_STATUSES = frozenset(
    {DIRECT_STATUS_HEALTHY_WITH_LISTINGS, DIRECT_STATUS_HEALTHY_EMPTY}
)


def _generation_absence_health(
    coverage: tuple[CompanyCoverage, ...],
) -> dict[str, bool]:
    """Map each company to whether it produced trustworthy *absence* evidence.

    This is deliberately narrower than company coverage. GitHub backstop health
    is global: one healthy feed says nothing about whether a particular
    unsupported company was represented continuously, so `backstop_only`
    coverage must never let a posting look like it disappeared. Deriving the
    answer from `direct_status` rather than the coverage label also means a new
    coverage label cannot silently start granting absence credit.

    Only `healthy_with_listings` and `healthy_empty` count. `degraded`,
    `failed`, `unknown`, and `not_configured` all restart the absence streak.
    A single failed direct attempt yields `failed` immediately, so an outage
    breaks credit in the same run it happens.
    """

    return {
        item.company: item.direct_status in _ABSENCE_HEALTHY_DIRECT_STATUSES
        for item in coverage
        if item.company
    }


def _log_shadow_generation(
    candidates: tuple[ShadowGenerationCandidate, ...],
    observations: int,
) -> None:
    reasons = Counter(candidate.trigger for candidate in candidates)
    LOGGER.info(
        "SHADOW-GENERATION observed=%d candidates=%d triggers=%s (shadow only; "
        "notification selection unchanged)",
        observations,
        len(candidates),
        ",".join(f"{trigger}={count}" for trigger, count in sorted(reasons.items()))
        or "none",
    )
    for candidate in candidates[:SHADOW_GENERATION_REPORT_LIMIT]:
        LOGGER.info("SHADOW-GENERATION CANDIDATE: %s", candidate.console_line())


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
