"""Alert, daily-summary, and digest wording.

Field order and phrasing here are the operator-facing contract; tests assert on
them. Rendering reads already-decided candidates and incidents and never
re-decides severity.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from watcher.health.models import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_UNCOVERED,
    FAILURE_ALERT_TYPES,
    MAX_DIGEST_CATCHUP_DAYS,
    MAX_DIGEST_INCIDENTS,
    SEVERITY_MEDIUM,
    CompanyCoverage,
    DigestIncident,
    HealthAlertCandidate,
    HealthAlertPolicy,
    HealthSummary,
    SystemicIncidentGroup,
)
from watcher.health.policy import group_systemic_incidents
from watcher.health.sanitize import iso_utc, safe_run_id, sanitize_error
from watcher.source_comparison import SourceComparisonReport


def render_alert_email(
    candidates: Sequence[HealthAlertCandidate],
) -> tuple[str, str]:
    groups, remaining = group_systemic_incidents(candidates)
    first = candidates[0]
    if groups:
        subject = (
            f"Internship Watcher Source Alert: {len(groups)} shared source "
            f"incident(s), {len(remaining)} other"
        )
    elif len(candidates) == 1 and first.alert_type == "recovery":
        subject = f"Internship Watcher Source Recovery: {first.source_label}"
    elif len(candidates) == 1:
        subject = (
            f"Internship Watcher Source Alert: {first.source_label} "
            f"{first.current_status}"
        )
    else:
        subject = f"Internship Watcher Source Alert: {len(candidates)} source incidents"
    lines = [subject, ""]
    for group in groups:
        lines.extend(_group_lines(group))
    for candidate in remaining:
        lines.extend(_candidate_lines(candidate))
    return subject, "\n".join(lines).rstrip() + "\n"


def _group_lines(group: SystemicIncidentGroup) -> list[str]:
    return [
        f"HIGH: likely shared {group.adapter_family} incident "
        f"({group.affected_companies} companies)",
        f"  source family: {group.adapter_family}",
        f"  safe failure category: {group.error_kind}",
        f"  affected companies: {group.affected_companies}",
        *[f"    - {sanitize_error(company)}" for company in group.companies],
        f"  recommended action: {group.recommended_action}",
        f"  run ID: {group.run_id}",
        "",
    ]


def _candidate_lines(candidate: HealthAlertCandidate) -> list[str]:
    return [
        f"{candidate.severity.upper()}: {candidate.source_label}",
        f"  type: {candidate.alert_type}",
        f"  previous status: {candidate.previous_status or 'unknown'}",
        f"  current status: {candidate.current_status}",
        f"  consecutive failures: {candidate.consecutive_failures}",
        f"  consecutive empty successes: {candidate.consecutive_empty}",
        f"  last successful time: {candidate.last_success_at or 'unknown'}",
        f"  rows returned: {candidate.rows_returned if candidate.rows_returned is not None else 'unknown'}",
        f"  safe error category: {candidate.error_kind or 'none'}",
        f"  bounded diagnostics: {candidate.diagnostic_summary or 'none'}",
        f"  direct fallback available: {_yes_no_unknown(candidate.direct_fallback_available)}",
        # Two different questions, deliberately reported separately: a feed
        # succeeding somewhere says nothing about this company.
        f"  some GitHub feed succeeded on this run: {_yes_no_unknown(candidate.github_fallback_available)}",
        f"  usable GitHub fallback for this company: {_yes_no_unknown(candidate.github_fallback_usable)}",
        f"  recommended action: {candidate.recommended_action}",
        f"  run ID: {candidate.run_id}",
        "",
    ]


def _yes_no_unknown(value: bool | None) -> str:
    return "unknown" if value is None else "yes" if value else "no"


def render_daily_summary(
    *,
    run_id: str,
    observed_at: datetime,
    policy: HealthAlertPolicy,
    coverage: Sequence[CompanyCoverage],
    summary: HealthSummary,
    comparison: SourceComparisonReport | None,
    candidates: Sequence[HealthAlertCandidate],
    recent_candidates: Sequence[HealthAlertCandidate] = (),
) -> tuple[str, str]:
    subject = "Internship Watcher Daily Source Health"
    backstop = sorted(
        item.company for item in coverage if item.state == COVERAGE_BACKSTOP_ONLY
    )
    uncovered = sorted(
        item.company for item in coverage if item.state == COVERAGE_UNCOVERED
    )
    stale = sorted(
        candidate.source_label
        for candidate in candidates
        if candidate.alert_type == "feed_stale"
    )
    recoveries = sorted(
        candidate.source_label
        for candidate in recent_candidates
        if candidate.alert_type == "recovery"
    )
    failures = sorted(
        candidate.source_label
        for candidate in recent_candidates
        if candidate.alert_type == "new_failure"
    )
    comparison_counts = comparison.counts if comparison else {}
    lines = [
        subject,
        "",
        f"Run ID: {safe_run_id(run_id)}",
        f"Observed at: {iso_utc(observed_at)}",
        f"Feed stale threshold: {policy.feed_stale_hours} hours",
        "",
        "Current source status",
        f"  healthy direct sources with listings: {summary.direct_healthy_with_listings}",
        f"  healthy empty direct sources: {summary.direct_healthy_empty}",
        f"  degraded direct sources: {summary.direct_degraded}",
        f"  failed direct sources: {summary.direct_failed}",
        f"  not-configured direct sources: {summary.direct_not_configured}",
        f"  unknown direct sources: {summary.direct_unknown}",
        f"  healthy GitHub feeds: {summary.github_feeds_healthy}",
        f"  failing GitHub feeds: {summary.github_feeds_failing}",
        f"  backstop-only companies: {len(backstop)}",
        f"  uncovered companies: {len(uncovered)}",
        "",
        "Source comparison",
        f"  GitHub-only eligible: {comparison_counts.get('github_only', 0)}",
        f"  direct-only eligible: {comparison_counts.get('direct_only', 0)}",
        f"  both found and merged: {comparison_counts.get('both', 0)}",
        f"  companies with no postings: {comparison_counts.get('no_postings', 0)}",
    ]
    _append_bounded(lines, "New failures", failures)
    _append_bounded(lines, "Recoveries", recoveries)
    _append_bounded(lines, "Stale configured-season feeds", stale)
    _append_bounded(lines, "Backstop-only companies", backstop)
    _append_bounded(lines, "Uncovered companies", uncovered)
    return subject, "\n".join(lines).rstrip() + "\n"


def _append_bounded(
    lines: list[str],
    label: str,
    values: Sequence[str],
    *,
    limit: int = 20,
) -> None:
    lines.extend(["", label])
    if not values:
        lines.append("  (none)")
        return
    lines.extend(f"  - {sanitize_error(value)}" for value in values[:limit])
    if len(values) > limit:
        lines.append(f"  - ... and {len(values) - limit} more")


def summarize_digest_incident(incident: DigestIncident) -> str:
    """Render one incident's collapsed lifecycle as a single sentence."""

    label = sanitize_error(incident.source_label)
    noun = (
        "direct-source failure"
        if any(item in FAILURE_ALERT_TYPES for item in incident.alert_types)
        else "direct-source degradation"
    )
    if incident.occurrences == 0:
        # Only a recovery landed in this window, so the degradation itself was
        # reported earlier - immediately when it escalated, or in a prior digest.
        return f"{label} — recovered; currently healthy."
    runs = (
        "run"
        if incident.occurrences == 1
        else "runs"
    )
    counted = (
        f"{incident.occurrences} failed {runs}"
        if noun.endswith("failure")
        else f"{incident.occurrences} degraded {runs}"
    )
    if incident.recovered == "yes":
        return f"{label} — transient {noun} recovered; {counted}, currently healthy."
    if incident.recovered == "no":
        return f"{label} — {noun} ongoing; {counted}, not yet recovered."
    return f"{label} — {noun}; {counted}, current state unknown."


def render_source_health_digest(
    *,
    run_id: str,
    observed_at: datetime,
    window_start: datetime,
    incidents: Sequence[DigestIncident],
    catchup_clamped: bool = False,
) -> tuple[str, str]:
    """Render the once-daily summary of deferred MEDIUM and INFO incidents."""

    medium = sum(item.severity == SEVERITY_MEDIUM for item in incidents)
    info = len(incidents) - medium
    subject = (
        f"Internship Watcher Daily Source Health: {medium} medium, {info} info"
    )
    lines = [
        subject,
        "",
        f"Run ID: {safe_run_id(run_id)}",
        f"Observed at: {iso_utc(observed_at)}",
        f"Reporting window: {iso_utc(window_start)} to {iso_utc(observed_at)}",
        "",
        "These incidents did not warrant an immediate alert. High-severity",
        "incidents are emailed as they happen and are not listed here.",
        "",
    ]
    if catchup_clamped:
        lines.extend(
            [
                "WARNING: no digest was delivered for more than "
                f"{MAX_DIGEST_CATCHUP_DAYS} days, so this report starts at that",
                "bound and older retained events are omitted.",
                "",
            ]
        )
    for incident in incidents[:MAX_DIGEST_INCIDENTS]:
        retained = (
            incident.retained_rows
            if incident.retained_rows is not None
            else "unknown"
        )
        lines.append(
            f"{incident.severity.upper()}: {summarize_digest_incident(incident)}"
        )
        lines.extend(
            [
                f"  types: {', '.join(incident.alert_types) or 'none'}",
                f"  occurrences: {incident.occurrences}",
                f"  reason codes: {', '.join(incident.reason_codes) or 'none'}",
                f"  retained rows: {retained}",
                f"  diagnostics: {incident.diagnostic_summary or 'none'}",
                f"  first seen: {incident.first_detected_at}",
                f"  last seen: {incident.last_detected_at}",
                f"  recovered later: {incident.recovered}",
            ]
        )
        if incident.escalated:
            lines.append(
                "  note: escalated earlier in this window and alerted immediately"
            )
        lines.append("")
    if len(incidents) > MAX_DIGEST_INCIDENTS:
        lines.append(
            f"... and {len(incidents) - MAX_DIGEST_INCIDENTS} more source(s)"
        )
    return subject, "\n".join(lines).rstrip() + "\n"
