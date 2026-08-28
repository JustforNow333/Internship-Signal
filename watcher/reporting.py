"""Console report, application heartbeat, and the sanitized health report."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TextIO

from watcher.collection import WorkdayTransportSummary
from watcher.config import COLLECTION_MODE_SERIAL
from watcher.health_alerts import MODE_OFF as HEALTH_EMAIL_OFF
from watcher.pipeline import RunResult
from watcher.source_comparison import (
    CATEGORY_BOTH,
    CATEGORY_DIRECT_ONLY,
    CATEGORY_GITHUB_ONLY,
    SourceComparisonReport,
)
from watcher.source_health import (
    COVERAGE_UNCOVERED,
    SOURCE_KIND_DIRECT,
    STATUS_DEGRADED,
    STATUS_FAILING,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_UNKNOWN,
    HealthSummary,
    write_health_report,
)


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


def _heartbeat_terms(terms: object) -> str:
    values = []
    for term in terms or ():
        value = re.sub(r"\s+", "_", str(term).strip())
        value = value.replace(",", "-").replace("|", "/")
        if value:
            values.append(value)
    return "|".join(values) if values else "none"
