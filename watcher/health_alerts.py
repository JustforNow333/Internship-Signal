"""Independent source-health alert policy, persistence, rendering, and SMTP."""

from __future__ import annotations

import json
import logging
import os
import smtplib
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from watcher.source_comparison import SourceComparisonReport
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_UNCOVERED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_DEGRADED,
    STATUS_EMPTY,
    STATUS_FAILING,
    STATUS_HEALTHY,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_UNKNOWN,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceHealthState,
    iso_utc,
    sanitize_error,
    safe_error_kind,
    safe_run_id,
    utc_datetime,
)

LOGGER = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_TRANSITIONS_ONLY = "transitions_only"
MODE_FAILURE_ONLY = "failure_only"
MODE_DAILY_SUMMARY = "daily_summary"
HEALTH_EMAIL_MODES = frozenset(
    {
        MODE_OFF,
        MODE_TRANSITIONS_ONLY,
        MODE_FAILURE_ONLY,
        MODE_DAILY_SUMMARY,
    }
)
DEFAULT_MODE = MODE_TRANSITIONS_ONLY
DEFAULT_HOUR_UTC = 12
DEFAULT_COOLDOWN_HOURS = 24
DEFAULT_FEED_STALE_HOURS = 48

ALERT_NEW_FAILURE = "new_failure"
ALERT_CONTINUED_FAILURE = "continued_failure"
ALERT_DIRECT_SOURCE_DEGRADED = "direct_source_degraded"
ALERT_UNKNOWN_DIAGNOSTICS = "unknown_diagnostics"
ALERT_FEED_STALE = "feed_stale"
ALERT_RECOVERY = "recovery"
ALERT_MINOR_DEGRADATION = "minor_degradation"
ALERT_MINOR_RECOVERY = "minor_recovery"
ALERT_BOTH_TIERS_UNAVAILABLE = "both_tiers_unavailable"
ALERT_COVERAGE_REGRESSION = "coverage_regression"

# Severity routes delivery. HIGH is the only severity that may send an
# immediate email; MEDIUM and INFO are deferred to one daily digest. There is
# no CRITICAL severity: conditions that once used it are HIGH.
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_INFO = "info"
DIGEST_SEVERITIES = (SEVERITY_MEDIUM, SEVERITY_INFO)
# ``critical`` is retained here only so alert payloads persisted before
# severity-based routing still sort predictably when they are replayed.
SEVERITY_ORDER = {"critical": 0, SEVERITY_HIGH: 1, SEVERITY_MEDIUM: 2, SEVERITY_INFO: 3}

# Reason codes that describe trustworthy, substantially complete collection.
# Every other code the adapters emit - pagination loss, truncation, repeated
# pages, enrichment failure, duplicate skipping - and every code this policy
# does not recognize is actionable, so it stays MEDIUM rather than dropping to
# informational. Both severities report in the daily digest.
MINOR_DEGRADATION_REASONS = frozenset(
    {
        "schema_invalid_records_skipped",
        "malformed_records_skipped",
        "request_retry_recovered",
    }
)
# Degradation alert types whose open incident a recovery closes.
DEGRADATION_ALERT_TYPES = (
    ALERT_NEW_FAILURE,
    ALERT_CONTINUED_FAILURE,
    "direct_source_silence",
    ALERT_DIRECT_SOURCE_DEGRADED,
    ALERT_UNKNOWN_DIAGNOSTICS,
    ALERT_FEED_STALE,
    ALERT_MINOR_DEGRADATION,
)

# Record loss stays minor only while it is both absolutely small and tiny
# relative to what the same attempt retained.
MAX_MINOR_SKIPPED_ROWS = 5
MIN_RETAINED_ROWS_PER_SKIPPED_ROW = 20
DIGEST_WINDOW_HOURS = 24
MAX_DIGEST_INCIDENTS = 25
# A digest that has not delivered for days still reports, but only back to this
# bound. Events stay queryable for 30 days; the clamp keeps one catch-up email
# finite and is reported as a warning when it engages.
MAX_DIGEST_CATCHUP_DAYS = 7
# Defensive bound on how many stored events one digest window may load.
MAX_DIGEST_EVENTS = 20_000

# How far back persisted company-level GitHub evidence stays trustworthy.
GITHUB_EVIDENCE_HORIZON_DAYS = 7
# Coverage snapshots are the evidence store, so retention must outlast that
# horizon. The scheduled watcher runs hourly, so seven days is 168 snapshots;
# 200 keeps roughly a day of margin for extra manual dispatches.
MAX_COVERAGE_SNAPSHOTS = 200

# A source that keeps failing and recovering on one error restates a mode that
# was already alerted. After this many prior occurrences of the *same* sanitized
# error kind inside the lookback window, one more isolated failure is a repeat
# rather than news, so it joins the daily digest instead of interrupting again.
# The window spans seven days of hourly runs, matching the alert-event history
# this reads; nothing new is persisted for it.
FLAP_LOOKBACK_HOURS = 168
FLAP_REPEAT_THRESHOLD = 3
# Defensive bound on how many stored failure events one lookback may load.
MAX_FLAP_HISTORY_EVENTS = 20_000

# A shared platform incident is obvious only when it is both broad and
# consistent: at least this many companies of one adapter family fail with the
# same sanitized error kind, and that kind accounts for at least this share of
# the family's failures on the run.
SYSTEMIC_GROUP_MIN_COMPANIES = 5
SYSTEMIC_GROUP_DOMINANCE_PERCENT = 60

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class HealthAlertPolicy:
    mode: str = DEFAULT_MODE
    hour_utc: int = DEFAULT_HOUR_UTC
    cooldown_hours: int = DEFAULT_COOLDOWN_HOURS
    feed_stale_hours: int = DEFAULT_FEED_STALE_HOURS


@dataclass(frozen=True)
class HealthAlertCandidate:
    fingerprint: str
    alert_type: str
    severity: str
    health_key: str
    source_kind: str
    company: str | None
    source_label: str
    previous_status: str | None
    current_status: str
    consecutive_failures: int
    consecutive_empty: int
    last_success_at: str | None
    rows_returned: int | None
    error_kind: str | None
    direct_fallback_available: bool | None
    github_fallback_available: bool | None
    recommended_action: str
    run_id: str
    diagnostic_summary: str = ""
    reason_codes: tuple[str, ...] = ()
    # Adapter family this source belongs to, used only to group obvious shared
    # platform incidents for presentation.
    adapter: str = ""
    # Whether a GitHub fallback was demonstrably usable *for this company* when
    # severity was assigned. This is deliberately distinct from
    # ``github_fallback_available``, which only reports that some GitHub feed
    # succeeded somewhere on the run. ``None`` means the question was not
    # evaluated for this alert type.
    github_fallback_usable: bool | None = None


@dataclass(frozen=True)
class DigestIncident:
    """One source's collapsed MEDIUM/INFO lifecycle in a reporting window."""

    health_key: str
    source_label: str
    severity: str
    alert_types: tuple[str, ...]
    occurrences: int
    first_detected_at: str
    last_detected_at: str
    retained_rows: int | None
    reason_codes: tuple[str, ...]
    diagnostic_summary: str
    recovered: str
    escalated: bool = False


@dataclass(frozen=True)
class SystemicIncidentGroup:
    """One run's shared same-family failure, for presentation only."""

    adapter_family: str
    error_kind: str
    companies: tuple[str, ...]
    run_id: str
    recommended_action: str

    @property
    def affected_companies(self) -> int:
        return len(self.companies)


@dataclass(frozen=True)
class HealthAlertResult:
    mode: str
    candidates: int
    sent: bool
    suppressed_by_cooldown: int
    recovery_alerts: int
    subject: str
    error: str | None
    daily_summary_sent: bool
    daily_digest_sent: bool = False
    digest_incidents_reported: int = 0
    digest_catchup_clamped: bool = False


def load_health_alert_policy(
    environ: Mapping[str, str] | None = None,
) -> HealthAlertPolicy:
    env = os.environ if environ is None else environ
    mode = str(env.get("WATCHER_HEALTH_EMAIL_MODE", DEFAULT_MODE)).strip().casefold()
    if mode not in HEALTH_EMAIL_MODES:
        raise ValueError(
            "WATCHER_HEALTH_EMAIL_MODE must be one of "
            + ", ".join(sorted(HEALTH_EMAIL_MODES))
        )
    return HealthAlertPolicy(
        mode=mode,
        hour_utc=_bounded_int(
            env.get("WATCHER_HEALTH_EMAIL_HOUR_UTC"),
            default=DEFAULT_HOUR_UTC,
            minimum=0,
            maximum=23,
            name="WATCHER_HEALTH_EMAIL_HOUR_UTC",
        ),
        cooldown_hours=_bounded_int(
            env.get("WATCHER_HEALTH_ALERT_COOLDOWN_HOURS"),
            default=DEFAULT_COOLDOWN_HOURS,
            minimum=1,
            maximum=24 * 30,
            name="WATCHER_HEALTH_ALERT_COOLDOWN_HOURS",
        ),
        feed_stale_hours=_bounded_int(
            env.get("WATCHER_FEED_STALE_HOURS"),
            default=DEFAULT_FEED_STALE_HOURS,
            minimum=1,
            maximum=24 * 90,
            name="WATCHER_FEED_STALE_HOURS",
        ),
    )


def is_minor_degradation(state: SourceHealthState) -> bool:
    """Report whether a degraded direct source is only minor record-level noise.

    Classification reads the diagnostics the adapters already publish. Reason
    codes are the reliable signal: ``incomplete``/``complete`` are set *by* the
    skipped records themselves on every adapter, so gating skip cases on them
    would reject the very case this policy exists for. Each cause that makes
    collection genuinely partial publishes its own code (``pagination_*``,
    ``*_enrichment_failed``, ``duplicate_postings_skipped``), so an
    unrecognized or mixed set is always actionable.
    """

    if state.source_kind == SOURCE_KIND_GITHUB_FEED:
        return False
    if state.status != DIRECT_STATUS_DEGRADED:
        return False
    codes = tuple(state.last_reason_codes or ())
    if not codes or not all(code in MINOR_DEGRADATION_REASONS for code in codes):
        return False
    if state.last_truncated:
        return False
    skipped = max(0, int(state.last_malformed_row_count or 0)) + max(
        0, int(state.last_schema_error_row_count or 0)
    )
    skip_claimed = any(code.endswith("_records_skipped") for code in codes)
    if skip_claimed or skipped:
        if skipped <= 0:
            # The codes claim dropped records the counters cannot confirm.
            return False
        if skipped > MAX_MINOR_SKIPPED_ROWS:
            return False
        retained = max(0, int(state.last_rows_returned or 0))
        return retained >= skipped * MIN_RETAINED_ROWS_PER_SKIPPED_ROW
    # A recovered retry is minor only when the final collection is whole.
    return not state.last_incomplete and state.last_complete is True


def github_feed_fallback_usable(
    states: Mapping[str, SourceHealthState],
    *,
    observed_at: datetime,
    feed_stale_hours: int,
) -> bool:
    """Report whether *every* GitHub feed attempted this run is trustworthy.

    This is the run-wide half of the usable-fallback test: a feed counts only
    when its most recent attempt succeeded and it published postings inside the
    existing staleness window. A feed that has never returned a posting is
    unproven rather than fresh, so it never qualifies.

    Company-level evidence is stored as an aggregate row count that does not
    record which feed supplied it, so a company that only ever appeared in one
    feed is indistinguishable from one carried by all of them. One healthy feed
    therefore proves nothing while another is failing or stale: that ambiguity
    must fail closed, so every attempted feed has to qualify. With no feed
    attempted at all there is no fallback to prove.
    """

    stale_after = timedelta(hours=feed_stale_hours)
    feeds = [
        state
        for state in states.values()
        if state.source_kind == SOURCE_KIND_GITHUB_FEED
    ]
    if not feeds:
        return False
    for state in feeds:
        if state.status != STATUS_HEALTHY:
            return False
        if state.last_nonzero_at is None:
            return False
        if observed_at - state.last_nonzero_at >= stale_after:
            return False
    return True


def usable_github_fallback(
    coverage: CompanyCoverage | None,
    *,
    feed_usable: bool,
    historical_evidence: frozenset[str] = frozenset(),
) -> bool:
    """Report whether GitHub demonstrably protects one specific company.

    All four conditions must hold: a GitHub feed succeeded on this run
    (``github_fallback_configured``), that feed is not stale (``feed_usable``),
    GitHub is a fallback rather than this company's primary source (also
    ``github_fallback_configured``), and there is positive company-level row
    evidence either on this run or inside the persisted evidence horizon.

    Zero rows are never evidence, because a covered company may simply have no
    current postings, and an absent count is unknown rather than zero. Both
    cases leave the fallback unproven, which keeps a first failure HIGH.
    """

    if coverage is None or not coverage.github_fallback_configured:
        return False
    if not feed_usable:
        return False
    rows = coverage.github_rows_returned
    if rows is not None and rows > 0:
        return True
    return sanitize_error(coverage.company) in historical_evidence


def repeat_flap_deferrable(
    *,
    consecutive_failures: int,
    error_kind: str | None,
    fallback_available: bool | None,
    fallback_usable: bool | None,
    occurrences: Sequence[HealthAlertCandidate],
) -> bool:
    """Report whether one failure only repeats an already-alerted mode.

    ``occurrences`` are the prior failure events for this same health key and
    the same sanitized error kind inside the lookback window, oldest first.
    Keying on the error kind rather than the fingerprint is deliberate: the
    fingerprint omits ``error_kind``, so a source that flaps on one error would
    otherwise mask a genuinely different failure.

    Deferral is refused whenever the incident could be worse than the ones
    already reported: a second consecutive failure means the source is down
    now rather than flapping, and weaker fallback posture means a repeat that
    used to be covered no longer is.
    """

    if consecutive_failures != 1:
        return False
    if not error_kind:
        return False
    if len(occurrences) < FLAP_REPEAT_THRESHOLD:
        return False
    latest = occurrences[-1]
    if _fallback_regressed(latest.github_fallback_available, fallback_available):
        return False
    return not _fallback_regressed(latest.github_fallback_usable, fallback_usable)


def _failure_action(
    *,
    covered_first_failure: bool,
    repeating_flap: bool,
) -> str:
    """Describe why one failure waits for the digest, or why it did not."""

    if covered_first_failure:
        return (
            "A usable GitHub fallback currently covers this company; "
            "review the daily digest and escalate if it fails again."
        )
    if repeating_flap:
        return (
            "This source keeps failing and recovering on the same error; "
            "review the daily digest and escalate if the failure changes."
        )
    return "Inspect the sanitized source-health report and adapter endpoint."


def _fallback_regressed(previous: bool | None, current: bool | None) -> bool:
    """Report whether fallback posture is weaker than it was.

    Tri-state, so unknown never counts as an improvement: losing a proven
    ``True`` to either ``False`` or unknown is a regression, while posture that
    was never proven has nothing to lose.
    """

    return previous is True and current is not True


def evaluate_and_send_health_alerts(
    *,
    db_path: str | Path,
    policy: HealthAlertPolicy,
    run_id: str,
    observed_at: datetime,
    states: Mapping[str, SourceHealthState],
    transitions: Sequence[HealthTransition],
    coverage: Sequence[CompanyCoverage],
    summary: HealthSummary,
    comparison: SourceComparisonReport | None,
    sender: Callable[[str, str], bool] | None = None,
) -> HealthAlertResult:
    """Evaluate and optionally send health mail without touching posting state."""

    now = utc_datetime(observed_at)
    if policy.mode == MODE_OFF:
        # Initialize only the dedicated alert tables so workflow persistence
        # remains schema-stable even while alert delivery is disabled.
        with HealthAlertStore(db_path):
            pass
        return HealthAlertResult(
            mode=policy.mode,
            candidates=0,
            sent=False,
            suppressed_by_cooldown=0,
            recovery_alerts=0,
            subject="",
            error=None,
            daily_summary_sent=False,
        )

    with HealthAlertStore(db_path) as store:
        previous_coverage = store.latest_coverage_snapshot()
        # Read open incidents before recording, because recording a recovery
        # resolves the incident it ends. The GitHub evidence read must also
        # precede this run's snapshot write so it stays strictly historical;
        # current-run evidence arrives through `coverage` instead.
        minor_incident_keys = store.open_minor_incident_keys()
        github_evidence = store.companies_with_recent_github_rows(
            since=now - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS),
        )
        # Repeat history must also stay strictly historical, so it is read
        # before this run's own events are recorded below.
        failure_history = store.recent_failure_occurrences(
            since=now - timedelta(hours=FLAP_LOOKBACK_HOURS),
        )
        candidates = build_alert_candidates(
            policy=policy,
            run_id=run_id,
            observed_at=now,
            states=states,
            transitions=transitions,
            coverage=coverage,
            previous_coverage=previous_coverage,
            minor_incident_keys=minor_incident_keys,
            github_evidence_companies=github_evidence,
            failure_history=failure_history,
        )
        store.record_coverage_snapshot(
            run_id=run_id,
            observed_at=now,
            coverage=coverage,
        )
        for candidate in candidates:
            store.record_detected(candidate, detected_at=now)
            if candidate.alert_type in {ALERT_RECOVERY, ALERT_MINOR_RECOVERY}:
                store.resolve_source_failures(
                    candidate.health_key,
                    resolved_at=now,
                )
            elif candidate.alert_type in {
                ALERT_NEW_FAILURE,
                ALERT_MINOR_DEGRADATION,
            }:
                store.resolve_source_recoveries(
                    candidate.health_key,
                    resolved_at=now,
                )

        active_sender = sender or send_health_email
        immediate = _evaluate_immediate_alerts(
            store=store,
            policy=policy,
            run_id=run_id,
            now=now,
            candidates=candidates,
            coverage=coverage,
            summary=summary,
            comparison=comparison,
            sender=active_sender,
        )
        # The digest runs on every immediate outcome, including cooldown
        # suppression and delivery failure, and can never delay an alert.
        digest_sent, digest_incidents, clamped = _maybe_send_daily_digest(
            store=store,
            policy=policy,
            run_id=run_id,
            now=now,
            states=states,
            sender=active_sender,
        )
        return replace(
            immediate,
            daily_digest_sent=digest_sent,
            digest_incidents_reported=digest_incidents,
            digest_catchup_clamped=clamped,
        )


def _evaluate_immediate_alerts(
    *,
    store: "HealthAlertStore",
    policy: HealthAlertPolicy,
    run_id: str,
    now: datetime,
    candidates: Sequence[HealthAlertCandidate],
    coverage: Sequence[CompanyCoverage],
    summary: HealthSummary,
    comparison: SourceComparisonReport | None,
    sender: Callable[[str, str], bool],
) -> HealthAlertResult:
    """Apply the immediate-alert and daily-summary policy.

    Cooldown, fingerprinting, and daily-summary behavior are unchanged; only
    which candidates arrive here changed, and severity decides that.
    """

    candidates = tuple(candidates)
    if policy.mode == MODE_DAILY_SUMMARY:
        due = (
            now.hour >= policy.hour_utc
            and not store.daily_summary_sent(now.date().isoformat())
        )
        sendable: list[HealthAlertCandidate] = []
        suppressed = 0
        if not due:
            return HealthAlertResult(
                mode=policy.mode,
                candidates=len(candidates),
                sent=False,
                suppressed_by_cooldown=0,
                recovery_alerts=sum(
                    item.alert_type == "recovery" for item in candidates
                ),
                subject="",
                error=None,
                daily_summary_sent=False,
            )
        subject, body = render_daily_summary(
            run_id=run_id,
            observed_at=now,
            policy=policy,
            coverage=coverage,
            summary=summary,
            comparison=comparison,
            candidates=candidates,
            recent_candidates=store.recent_candidates(
                since=now - timedelta(days=1)
            ),
        )
    else:
        # Only HIGH candidates reach immediate delivery. MEDIUM and INFO are
        # recorded above and reported by the daily digest instead, so there is
        # no unsent-recovery retry path here any more.
        policy_candidates = [
            candidate
            for candidate in candidates
            if _allowed_by_mode(candidate, policy.mode)
        ]
        sendable = []
        suppressed = 0
        cooldown = timedelta(hours=policy.cooldown_hours)
        for candidate in policy_candidates:
            if store.should_send(
                candidate,
                now=now,
                cooldown=cooldown,
            ):
                sendable.append(candidate)
            else:
                suppressed += 1
        if not sendable:
            return HealthAlertResult(
                mode=policy.mode,
                candidates=len(candidates),
                sent=False,
                suppressed_by_cooldown=suppressed,
                recovery_alerts=sum(
                    item.alert_type == "recovery" for item in candidates
                ),
                subject="",
                error=None,
                daily_summary_sent=False,
            )
        subject, body = render_alert_email(sendable)

    try:
        sent = bool(sender(subject, body))
    except Exception as exc:  # SMTP must not affect internship state or run success
        error = sanitize_error(exc)
        LOGGER.error("Source-health email failed: %s", error)
        return HealthAlertResult(
            mode=policy.mode,
            candidates=len(candidates),
            sent=False,
            suppressed_by_cooldown=suppressed,
            recovery_alerts=sum(
                item.alert_type == "recovery" for item in candidates
            ),
            subject=subject,
            error=error,
            daily_summary_sent=False,
        )

    if sent:
        if policy.mode == MODE_DAILY_SUMMARY:
            store.mark_daily_summary_sent(
                summary_date=now.date().isoformat(),
                sent_at=now,
                run_id=run_id,
            )
        else:
            for candidate in sendable:
                store.record_sent(candidate, sent_at=now)
    return HealthAlertResult(
        mode=policy.mode,
        candidates=len(candidates),
        sent=sent,
        suppressed_by_cooldown=suppressed,
        recovery_alerts=sum(
            item.alert_type == "recovery" for item in candidates
        ),
        subject=subject,
        error=None if sent else "health_sender_returned_false",
        daily_summary_sent=bool(sent and policy.mode == MODE_DAILY_SUMMARY),
    )


def build_alert_candidates(
    *,
    policy: HealthAlertPolicy,
    run_id: str,
    observed_at: datetime,
    states: Mapping[str, SourceHealthState],
    transitions: Sequence[HealthTransition],
    coverage: Sequence[CompanyCoverage],
    previous_coverage: Mapping[str, str] | None,
    minor_incident_keys: frozenset[str] = frozenset(),
    github_evidence_companies: frozenset[str] = frozenset(),
    failure_history: Mapping[
        tuple[str, str], Sequence[HealthAlertCandidate]
    ] = MappingProxyType({}),
) -> tuple[HealthAlertCandidate, ...]:
    transition_by_key = {transition.health_key: transition for transition in transitions}
    coverage_by_company = {item.company: item for item in coverage}
    feed_usable = github_feed_fallback_usable(
        states,
        observed_at=observed_at,
        feed_stale_hours=policy.feed_stale_hours,
    )
    candidates: list[HealthAlertCandidate] = []
    for state in states.values():
        transition = transition_by_key.get(state.health_key)
        company_coverage = coverage_by_company.get(state.company or "")
        if transition and transition.recovery:
            # A source whose only open incident was unreported minor noise
            # recovers just as quietly; the digest still records that it came
            # back.
            minor = state.health_key in minor_incident_keys
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type=(
                        ALERT_MINOR_RECOVERY if minor else ALERT_RECOVERY
                    ),
                    severity=SEVERITY_INFO,
                    run_id=run_id,
                    coverage=company_coverage,
                    action=(
                        "No action required; the earlier minor anomaly cleared."
                        if minor
                        else "No action required; verify the next scheduled run remains healthy."
                    ),
                )
            )
            continue
        if state.status in {STATUS_FAILING, DIRECT_STATUS_FAILED}:
            # A single direct-source failure is only deferrable while GitHub
            # demonstrably covers that company. A second consecutive failure is
            # HIGH regardless, so proven fallback can delay one alert but never
            # suppress escalation.
            first_failure = (
                state.source_kind != SOURCE_KIND_GITHUB_FEED
                and state.consecutive_failures == 1
            )
            fallback_usable = usable_github_fallback(
                company_coverage,
                feed_usable=feed_usable,
                historical_evidence=github_evidence_companies,
            )
            covered_first_failure = first_failure and fallback_usable
            # An isolated failure that only repeats a mode already alerted
            # several times is the other way to reach the digest. Both routes
            # require an isolated failure, so neither can mute an escalation.
            error_kind = (
                safe_error_kind(state.last_error_kind)
                if state.last_error_kind
                else None
            )
            repeating_flap = repeat_flap_deferrable(
                consecutive_failures=state.consecutive_failures,
                error_kind=error_kind,
                fallback_available=(
                    company_coverage.github_backstop_available
                    if company_coverage
                    else None
                ),
                fallback_usable=fallback_usable,
                occurrences=failure_history.get(
                    (state.health_key, error_kind or ""), ()
                ),
            )
            deferrable = covered_first_failure or repeating_flap
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type=(
                        ALERT_NEW_FAILURE
                        if transition
                        and transition.to_status in {STATUS_FAILING, DIRECT_STATUS_FAILED}
                        else ALERT_CONTINUED_FAILURE
                    ),
                    severity=SEVERITY_MEDIUM if deferrable else SEVERITY_HIGH,
                    run_id=run_id,
                    coverage=company_coverage,
                    action=_failure_action(
                        covered_first_failure=covered_first_failure,
                        repeating_flap=repeating_flap,
                    ),
                    fallback_usable=fallback_usable,
                )
            )
        elif (
            state.source_kind != SOURCE_KIND_GITHUB_FEED
            and state.status == DIRECT_STATUS_DEGRADED
        ):
            # Degradation that left a trustworthy, substantially complete
            # result stays informational. Everything else is medium. Both are
            # reported by the daily digest rather than an immediate email.
            minor = is_minor_degradation(state)
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type=(
                        ALERT_MINOR_DEGRADATION
                        if minor
                        else ALERT_DIRECT_SOURCE_DEGRADED
                    ),
                    severity=SEVERITY_INFO if minor else SEVERITY_MEDIUM,
                    run_id=run_id,
                    coverage=company_coverage,
                    action=(
                        "No immediate action; review the daily source-health digest."
                        if minor
                        else "Inspect the bounded adapter diagnostics for incomplete collection."
                    ),
                )
            )
        elif (
            state.source_kind != SOURCE_KIND_GITHUB_FEED
            and state.status == DIRECT_STATUS_UNKNOWN
        ):
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type=ALERT_UNKNOWN_DIAGNOSTICS,
                    severity=SEVERITY_MEDIUM,
                    run_id=run_id,
                    coverage=company_coverage,
                    action="Add or restore the adapter's shared collection diagnostics.",
                )
            )

        if (
            state.source_kind == SOURCE_KIND_GITHUB_FEED
            and state.status == STATUS_HEALTHY
            and state.last_nonzero_at is not None
            and observed_at - state.last_nonzero_at
            >= timedelta(hours=policy.feed_stale_hours)
        ):
            candidates.append(
                _candidate(
                    state,
                    transition=transition,
                    alert_type=ALERT_FEED_STALE,
                    severity=SEVERITY_MEDIUM,
                    run_id=run_id,
                    coverage=None,
                    action="Verify the feed is still publishing postings for the configured season.",
                )
            )

    for item in coverage:
        if item.state != COVERAGE_UNCOVERED:
            continue
        candidates.append(
            HealthAlertCandidate(
                fingerprint=f"both_tiers_unavailable|{_fingerprint_token(item.company)}",
                alert_type=ALERT_BOTH_TIERS_UNAVAILABLE,
                # Losing both tiers is the most urgent condition there is, but
                # it is HIGH: this policy has no CRITICAL severity.
                severity=SEVERITY_HIGH,
                health_key=f"coverage:{_fingerprint_token(item.company)}",
                source_kind="company_coverage",
                company=sanitize_error(item.company),
                source_label=sanitize_error(item.company),
                previous_status=(
                    previous_coverage.get(item.company)
                    if previous_coverage
                    else None
                ),
                current_status=item.state,
                consecutive_failures=0,
                consecutive_empty=0,
                last_success_at=None,
                rows_returned=item.direct_rows_returned,
                error_kind=None,
                direct_fallback_available=False,
                github_fallback_available=False,
                recommended_action="Restore either the direct source or a healthy GitHub backstop.",
                run_id=safe_run_id(run_id),
            )
        )

    if previous_coverage:
        became_backstop = sorted(
            item.company
            for item in coverage
            if item.state == COVERAGE_BACKSTOP_ONLY
            and previous_coverage.get(item.company) != COVERAGE_BACKSTOP_ONLY
        )
        previous_direct = sum(
            state not in {COVERAGE_BACKSTOP_ONLY, COVERAGE_UNCOVERED}
            for state in previous_coverage.values()
        )
        current_direct = sum(
            item.state not in {COVERAGE_BACKSTOP_ONLY, COVERAGE_UNCOVERED}
            for item in coverage
        )
        if current_direct < previous_direct or became_backstop:
            label = ", ".join(became_backstop[:10]) or "direct coverage"
            candidates.append(
                HealthAlertCandidate(
                    fingerprint=(
                        "coverage_regression|"
                        + _fingerprint_token(",".join(became_backstop))
                        + f"|{previous_direct}|{current_direct}"
                    ),
                    alert_type=ALERT_COVERAGE_REGRESSION,
                    severity=SEVERITY_HIGH,
                    health_key="coverage:aggregate",
                    source_kind="company_coverage",
                    company=None,
                    source_label=sanitize_error(label),
                    previous_status=f"direct_covered={previous_direct}",
                    current_status=f"direct_covered={current_direct}",
                    consecutive_failures=0,
                    consecutive_empty=0,
                    last_success_at=None,
                    rows_returned=None,
                    error_kind=None,
                    direct_fallback_available=None,
                    github_fallback_available=True,
                    recommended_action="Review companies that became backstop-only and restore direct coverage.",
                    run_id=safe_run_id(run_id),
                )
            )
    return _merge_candidates(candidates)


def group_systemic_incidents(
    candidates: Sequence[HealthAlertCandidate],
) -> tuple[tuple["SystemicIncidentGroup", ...], tuple[HealthAlertCandidate, ...]]:
    """Fold obvious same-family shared failures into one presentation group.

    This is strictly a presentation and delivery operation performed after every
    per-company incident has already been calculated and persisted. It reads
    candidates, writes nothing, and invents no synthetic health record: each
    affected company keeps its own state, counters, diagnostics, coverage, and
    cooldown entry. Groups plus leftovers always cover exactly the input.

    Only direct-source failures group, only within one adapter family, and only
    on one dominant sanitized error kind. Anything short of that reports
    independently, because two honest family alerts beat one invented shared
    diagnosis.
    """

    groupable = [
        candidate
        for candidate in candidates
        if candidate.severity == SEVERITY_HIGH
        and candidate.alert_type in FAILURE_ALERT_TYPES
        and candidate.source_kind == SOURCE_KIND_DIRECT
        and candidate.adapter
        and candidate.company
    ]
    by_family: dict[str, list[HealthAlertCandidate]] = {}
    for candidate in groupable:
        by_family.setdefault(candidate.adapter, []).append(candidate)

    groups: list[SystemicIncidentGroup] = []
    grouped: set[str] = set()
    for family, members in sorted(by_family.items()):
        if len(members) < SYSTEMIC_GROUP_MIN_COMPANIES:
            continue
        counts = Counter(_grouping_error_kind(item) for item in members)
        error_kind, count = min(
            counts.most_common(),
            key=lambda item: (-item[1], item[0]),
        )
        if count < SYSTEMIC_GROUP_MIN_COMPANIES:
            continue
        if count * 100 < len(members) * SYSTEMIC_GROUP_DOMINANCE_PERCENT:
            continue
        affected = sorted(
            (
                item
                for item in members
                if _grouping_error_kind(item) == error_kind
            ),
            key=lambda item: (item.source_label.casefold(), item.fingerprint),
        )
        groups.append(
            SystemicIncidentGroup(
                adapter_family=family,
                error_kind=error_kind,
                companies=tuple(item.source_label for item in affected),
                run_id=affected[0].run_id,
                recommended_action=(
                    f"Investigate the shared {family} platform or transport "
                    "before treating these as independent company failures."
                ),
            )
        )
        grouped.update(item.fingerprint for item in affected)
    remaining = tuple(
        candidate
        for candidate in candidates
        if candidate.fingerprint not in grouped
    )
    return tuple(groups), remaining


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


RECOVERY_ALERT_TYPES = frozenset({ALERT_RECOVERY, ALERT_MINOR_RECOVERY})
FAILURE_ALERT_TYPES = frozenset({ALERT_NEW_FAILURE, ALERT_CONTINUED_FAILURE})


def build_digest_incidents(
    events: Sequence[tuple[datetime, HealthAlertCandidate]],
    states: Mapping[str, SourceHealthState] | None = None,
) -> tuple[DigestIncident, ...]:
    """Collapse one source's MEDIUM/INFO lifecycle into a single entry.

    Every transition recorded for a health key inside the window folds into one
    incident: repeated degradations become an occurrence count, and a later
    recovery becomes the incident's outcome rather than a second entry.

    Escalation is resolved by position. Events up to and including the last
    HIGH for a key were already emailed immediately, so they are dropped; only
    what happened *after* that HIGH can still be news. A key whose window ends
    on the HIGH therefore leaves the digest entirely instead of reappearing as
    an unresolved MEDIUM, while a recovery that followed the HIGH is reported.
    """

    by_key: dict[str, list[tuple[datetime, HealthAlertCandidate]]] = {}
    for detected_at, candidate in events:
        by_key.setdefault(candidate.health_key, []).append((detected_at, candidate))

    incidents = []
    for health_key, entries in by_key.items():
        entries.sort(key=lambda item: item[0])
        escalated_at = max(
            (
                detected_at
                for detected_at, candidate in entries
                if candidate.severity == SEVERITY_HIGH
            ),
            default=None,
        )
        reportable = [
            (detected_at, candidate)
            for detected_at, candidate in entries
            if candidate.severity in DIGEST_SEVERITIES
            and (escalated_at is None or detected_at > escalated_at)
        ]
        if not reportable:
            continue
        degradations = [
            item
            for item in reportable
            if item[1].alert_type not in RECOVERY_ALERT_TYPES
        ]
        recovered_at = max(
            (
                detected_at
                for detected_at, candidate in reportable
                if candidate.alert_type in RECOVERY_ALERT_TYPES
            ),
            default=None,
        )
        # A recovery-only window still describes the degradation it ended, so
        # detail falls back to the newest event when nothing degraded here.
        detail_source = degradations or reportable
        first_at = reportable[0][0]
        last_at, _ = detail_source[-1]
        latest = detail_source[-1][1]
        severity = (
            SEVERITY_MEDIUM
            if any(
                candidate.severity == SEVERITY_MEDIUM
                for _, candidate in reportable
            )
            else SEVERITY_INFO
        )
        incidents.append(
            DigestIncident(
                health_key=health_key,
                source_label=latest.source_label,
                severity=severity,
                alert_types=tuple(
                    sorted({candidate.alert_type for _, candidate in reportable})
                ),
                occurrences=len(degradations),
                first_detected_at=iso_utc(first_at),
                last_detected_at=iso_utc(reportable[-1][0]),
                retained_rows=latest.rows_returned,
                reason_codes=tuple(
                    sorted(
                        {
                            code
                            for _, candidate in detail_source
                            for code in candidate.reason_codes
                        }
                    )
                ),
                diagnostic_summary=latest.diagnostic_summary,
                recovered=_recovery_state(
                    health_key,
                    last_degraded_at=last_at,
                    recovered_at=recovered_at,
                    states=states,
                ),
                escalated=escalated_at is not None,
            )
        )
    return tuple(
        sorted(
            incidents,
            key=lambda item: (
                SEVERITY_ORDER.get(item.severity, 9),
                item.source_label.casefold(),
                item.health_key,
            ),
        )
    )


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


def resolve_digest_window(
    *,
    now: datetime,
    last_sent_at: datetime | None,
) -> tuple[datetime, bool, bool]:
    """Resolve the reporting window as ``(start, inclusive, clamped)``.

    Catch-up resumes at the last successful digest so a delivery outage longer
    than a day cannot silently drop still-retained events, and is clamped so a
    long outage still produces one finite report. A window that starts at the
    previous digest is exclusive, because the events recorded by that run share
    its exact timestamp and were already reported.
    """

    default_start = now - timedelta(hours=DIGEST_WINDOW_HOURS)
    if last_sent_at is None:
        return default_start, True, False
    clamp_start = now - timedelta(days=MAX_DIGEST_CATCHUP_DAYS)
    if last_sent_at < clamp_start:
        return clamp_start, True, True
    return last_sent_at, False, False


def _maybe_send_daily_digest(
    *,
    store: "HealthAlertStore",
    policy: HealthAlertPolicy,
    run_id: str,
    now: datetime,
    states: Mapping[str, SourceHealthState],
    sender: Callable[[str, str], bool],
) -> tuple[bool, int, bool]:
    """Send at most one MEDIUM/INFO source-health digest per UTC day."""

    if now.hour < policy.hour_utc:
        return False, 0, False
    digest_date = now.date().isoformat()
    if store.digest_sent(digest_date):
        return False, 0, False
    window_start, inclusive, clamped = resolve_digest_window(
        now=now,
        last_sent_at=store.last_digest_sent_at(),
    )
    if clamped:
        LOGGER.warning(
            "Source-health digest catch-up clamped to %d days; older retained "
            "events are omitted from this report.",
            MAX_DIGEST_CATCHUP_DAYS,
        )
    incidents = build_digest_incidents(
        store.recent_digest_events(since=window_start, inclusive=inclusive),
        states,
    )
    if not incidents:
        # Never send an empty digest, and never mark the day used, so a later
        # run today can still report a new incident.
        return False, 0, clamped
    subject, body = render_source_health_digest(
        run_id=run_id,
        observed_at=now,
        window_start=window_start,
        incidents=incidents,
        catchup_clamped=clamped,
    )
    try:
        sent = bool(sender(subject, body))
    except Exception as exc:  # digest delivery must not affect the run
        LOGGER.error(
            "Daily source-health digest email failed: %s",
            sanitize_error(exc),
        )
        return False, len(incidents), clamped
    if sent:
        store.mark_digest_sent(
            digest_date=digest_date,
            sent_at=now,
            run_id=run_id,
            incident_count=len(incidents),
        )
    return sent, len(incidents), clamped


def _recovery_state(
    health_key: str,
    *,
    last_degraded_at: datetime,
    recovered_at: datetime | None,
    states: Mapping[str, SourceHealthState] | None,
) -> str:
    if recovered_at is not None and recovered_at >= last_degraded_at:
        return "yes"
    state = (states or {}).get(health_key)
    if state is None:
        return "unknown"
    if state.status in {
        STATUS_HEALTHY,
        STATUS_EMPTY,
        DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
        DIRECT_STATUS_HEALTHY_EMPTY,
    }:
        return "yes"
    return "no"


def _coverage_snapshot_value(coverage: CompanyCoverage) -> object:
    """Serialize one company's snapshot entry.

    A run that supplied no GitHub row count writes the legacy bare-string shape,
    so an entry never claims evidence it does not have.
    """

    if coverage.github_rows_returned is None:
        return coverage.state
    return {
        "state": coverage.state,
        "github_rows": max(0, int(coverage.github_rows_returned)),
    }


def _coverage_snapshot_entries(payload: object) -> dict[str, tuple[str, int | None]]:
    """Read a coverage snapshot in either the legacy or current shape.

    Legacy snapshots map a company straight to its coverage state string and
    carry no company-level GitHub evidence; current snapshots map it to an
    object. Both are accepted, so an existing production database keeps working
    and simply reports unknown evidence until new snapshots accumulate.
    """

    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    entries: dict[str, tuple[str, int | None]] = {}
    for company, value in data.items():
        if isinstance(value, Mapping):
            state = str(value.get("state") or "")
            raw_rows = value.get("github_rows")
            github_rows = (
                max(0, int(raw_rows))
                if isinstance(raw_rows, int) and not isinstance(raw_rows, bool)
                else None
            )
        else:
            state, github_rows = str(value), None
        entries[str(company)] = (state, github_rows)
    return entries


def _candidate_from_payload(payload: str) -> HealthAlertCandidate:
    """Rebuild a stored candidate, tolerating payloads from older versions."""

    data = json.loads(payload)
    known = {field.name for field in fields(HealthAlertCandidate)}
    values = {key: value for key, value in data.items() if key in known}
    values["reason_codes"] = tuple(values.get("reason_codes") or ())
    return HealthAlertCandidate(**values)


def send_health_email(subject: str, body: str) -> bool:
    """Send a source-health email using a separate renderer and call path."""

    env = _health_email_env()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = env["EMAIL_FROM"]
    message["To"] = env["EMAIL_TO"]
    message.set_content(body)
    LOGGER.info("Sending source-health email via %s:%s...", SMTP_HOST, SMTP_PORT)
    with smtplib.SMTP_SSL(
        SMTP_HOST,
        SMTP_PORT,
        timeout=SMTP_TIMEOUT_SECONDS,
    ) as smtp:
        smtp.login(env["SMTP_USER"], env["SMTP_APP_PASSWORD"])
        smtp.send_message(message)
    LOGGER.info("Source-health email sent.")
    return True


class HealthAlertStore:
    """Alert cooldown and daily-summary state, separate from ``seen``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def record_detected(
        self,
        candidate: HealthAlertCandidate,
        *,
        detected_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_alert_state(
                  fingerprint, alert_type, health_key, current_status,
                  first_detected_at, last_detected_at, last_sent_at,
                  send_count, resolved_at
                ) values (?, ?, ?, ?, ?, ?, null, 0, null)
                on conflict(fingerprint) do update set
                  current_status=excluded.current_status,
                  last_detected_at=excluded.last_detected_at
                """,
                (
                    candidate.fingerprint,
                    candidate.alert_type,
                    candidate.health_key,
                    candidate.current_status,
                    iso_utc(detected_at),
                    iso_utc(detected_at),
                ),
            )
            self._conn.execute(
                """
                insert into source_health_alert_events(
                  run_id, fingerprint, detected_at, alert_type, payload_json
                ) values (?, ?, ?, ?, ?)
                on conflict(run_id, fingerprint) do nothing
                """,
                (
                    candidate.run_id,
                    candidate.fingerprint,
                    iso_utc(detected_at),
                    candidate.alert_type,
                    json.dumps(
                        asdict(candidate),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            self._conn.execute(
                """
                delete from source_health_alert_events
                where detected_at < ?
                """,
                (iso_utc(utc_datetime(detected_at) - timedelta(days=30)),),
            )

    def should_send(
        self,
        candidate: HealthAlertCandidate,
        *,
        now: datetime,
        cooldown: timedelta,
    ) -> bool:
        row = self._conn.execute(
            """
            select last_sent_at, resolved_at
            from source_health_alert_state
            where fingerprint = ?
            """,
            (candidate.fingerprint,),
        ).fetchone()
        if row is None or not row["last_sent_at"] or row["resolved_at"]:
            return True
        return now - _parse_datetime(row["last_sent_at"]) >= cooldown

    def record_sent(
        self,
        candidate: HealthAlertCandidate,
        *,
        sent_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set last_sent_at = ?,
                    send_count = send_count + 1,
                    resolved_at = null
                where fingerprint = ?
                """,
                (iso_utc(sent_at), candidate.fingerprint),
            )

    def resolve_source_failures(
        self,
        health_key: str,
        *,
        resolved_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set resolved_at = ?
                where health_key = ?
                  and alert_type in (
                    'new_failure', 'continued_failure',
                    'direct_source_silence', 'direct_source_degraded',
                    'unknown_diagnostics', 'feed_stale', 'minor_degradation'
                  )
                  and resolved_at is null
                """,
                (iso_utc(resolved_at), health_key),
            )

    def resolve_source_recoveries(
        self,
        health_key: str,
        *,
        resolved_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set resolved_at = ?
                where health_key = ?
                  and alert_type in (?, ?)
                  and resolved_at is null
                """,
                (
                    iso_utc(resolved_at),
                    health_key,
                    ALERT_RECOVERY,
                    ALERT_MINOR_RECOVERY,
                ),
            )

    def open_minor_incident_keys(self) -> frozenset[str]:
        """Return keys whose only unresolved degradation is minor.

        Fingerprints are reused across cycles, so an incident detected again
        after an earlier resolution is open even though ``resolved_at`` still
        holds that older timestamp.
        """

        placeholders = ", ".join("?" * len(DEGRADATION_ALERT_TYPES))
        rows = self._conn.execute(
            f"""
            select health_key,
                   sum(case when alert_type = ? then 0 else 1 end) as actionable
            from source_health_alert_state
            where (resolved_at is null or last_detected_at > resolved_at)
              and alert_type in ({placeholders})
            group by health_key
            having actionable = 0
            """,
            (ALERT_MINOR_DEGRADATION, *DEGRADATION_ALERT_TYPES),
        ).fetchall()
        return frozenset(str(row["health_key"]) for row in rows)

    def recent_digest_events(
        self,
        *,
        since: datetime,
        inclusive: bool = True,
    ) -> tuple[tuple[datetime, HealthAlertCandidate], ...]:
        """Return every detected event in a window, newest last.

        HIGH events are returned alongside the digest-eligible ones because
        collapsing needs them to tell an escalated incident from an open one.
        Severity lives in the stored payload, so filtering happens in the
        caller rather than in SQL; the window and a hard row bound keep the
        read small.
        """

        rows = self._conn.execute(
            f"""
            select detected_at, payload_json
            from source_health_alert_events
            where detected_at {'>=' if inclusive else '>'} ?
            order by detected_at, event_id
            limit ?
            """,
            (iso_utc(since), MAX_DIGEST_EVENTS),
        ).fetchall()
        return tuple(
            (
                _parse_datetime(row["detected_at"]),
                _candidate_from_payload(row["payload_json"]),
            )
            for row in rows
        )

    def digest_sent(self, digest_date: str) -> bool:
        return (
            self._conn.execute(
                """
                select 1
                from source_health_digest
                where digest_date = ?
                """,
                (digest_date,),
            ).fetchone()
            is not None
        )

    def last_digest_sent_at(self) -> datetime | None:
        row = self._conn.execute(
            """
            select sent_at
            from source_health_digest
            order by digest_date desc
            limit 1
            """
        ).fetchone()
        if row is None or not row["sent_at"]:
            return None
        return _parse_datetime(row["sent_at"])

    def mark_digest_sent(
        self,
        *,
        digest_date: str,
        sent_at: datetime,
        run_id: str,
        incident_count: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_digest(
                  digest_date, sent_at, run_id, incident_count
                ) values (?, ?, ?, ?)
                on conflict(digest_date) do nothing
                """,
                (
                    digest_date,
                    iso_utc(sent_at),
                    safe_run_id(run_id),
                    max(0, int(incident_count)),
                ),
            )

    def daily_summary_sent(self, summary_date: str) -> bool:
        return (
            self._conn.execute(
                """
                select 1
                from source_health_daily_summary
                where summary_date = ?
                """,
                (summary_date,),
            ).fetchone()
            is not None
        )

    def mark_daily_summary_sent(
        self,
        *,
        summary_date: str,
        sent_at: datetime,
        run_id: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_daily_summary(
                  summary_date, sent_at, run_id
                ) values (?, ?, ?)
                on conflict(summary_date) do nothing
                """,
                (summary_date, iso_utc(sent_at), safe_run_id(run_id)),
            )

    def latest_coverage_snapshot(self) -> dict[str, str] | None:
        row = self._conn.execute(
            """
            select coverage_json
            from source_health_coverage_snapshots
            order by observed_at desc, run_id desc
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            company: state
            for company, (state, _) in _coverage_snapshot_entries(
                row["coverage_json"]
            ).items()
        }

    def companies_with_recent_github_rows(
        self,
        *,
        since: datetime,
    ) -> frozenset[str]:
        """Companies with persisted evidence of a GitHub row inside a window.

        Only a positive count is evidence. Zero rows are ambiguous, because a
        covered company may simply have no current postings, and a legacy
        snapshot that recorded no count at all is unknown rather than zero.
        """

        rows = self._conn.execute(
            """
            select coverage_json
            from source_health_coverage_snapshots
            where observed_at >= ?
            """,
            (iso_utc(since),),
        ).fetchall()
        companies: set[str] = set()
        for row in rows:
            for company, (_, github_rows) in _coverage_snapshot_entries(
                row["coverage_json"]
            ).items():
                if github_rows is not None and github_rows > 0:
                    companies.add(company)
        return frozenset(companies)

    def recent_failure_occurrences(
        self,
        *,
        since: datetime,
    ) -> dict[tuple[str, str], tuple[HealthAlertCandidate, ...]]:
        """Group recent failure events by health key and sanitized error kind.

        Only the failure family is read, because a repeat is evidence about one
        recurring failure mode; recoveries and degradations never count toward
        it. Groups arrive oldest first, so the last entry of each is the most
        recent qualifying occurrence. Events without an error kind cannot be
        matched to a mode and are skipped rather than grouped together.
        """

        rows = self._conn.execute(
            """
            select payload_json
            from source_health_alert_events
            where detected_at >= ?
              and alert_type in (?, ?)
            order by detected_at, event_id
            limit ?
            """,
            (
                iso_utc(since),
                ALERT_NEW_FAILURE,
                ALERT_CONTINUED_FAILURE,
                MAX_FLAP_HISTORY_EVENTS,
            ),
        ).fetchall()
        grouped: dict[tuple[str, str], list[HealthAlertCandidate]] = {}
        for row in rows:
            candidate = _candidate_from_payload(row["payload_json"])
            if not candidate.error_kind:
                continue
            grouped.setdefault(
                (candidate.health_key, candidate.error_kind), []
            ).append(candidate)
        return {key: tuple(items) for key, items in grouped.items()}

    def recent_candidates(
        self,
        *,
        since: datetime,
    ) -> tuple[HealthAlertCandidate, ...]:
        rows = self._conn.execute(
            """
            select payload_json
            from source_health_alert_events
            where detected_at >= ?
            order by detected_at, run_id, fingerprint
            """,
            (iso_utc(since),),
        ).fetchall()
        return tuple(
            _candidate_from_payload(row["payload_json"])
            for row in rows
        )

    def record_coverage_snapshot(
        self,
        *,
        run_id: str,
        observed_at: datetime,
        coverage: Sequence[CompanyCoverage],
    ) -> None:
        # Each company records its coverage state plus, when the run supplied
        # one, its GitHub row count. Snapshots written before this field exist
        # remain readable, so no production database needs wiping.
        data = {
            sanitize_error(item.company): _coverage_snapshot_value(item)
            for item in coverage
        }
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_coverage_snapshots(
                  run_id, observed_at, coverage_json
                ) values (?, ?, ?)
                on conflict(run_id) do update set
                  observed_at=excluded.observed_at,
                  coverage_json=excluded.coverage_json
                """,
                (
                    safe_run_id(run_id),
                    iso_utc(observed_at),
                    json.dumps(data, sort_keys=True, separators=(",", ":")),
                ),
            )
            self._conn.execute(
                f"""
                delete from source_health_coverage_snapshots
                where run_id not in (
                  select run_id
                  from source_health_coverage_snapshots
                  order by observed_at desc, run_id desc
                  limit {MAX_COVERAGE_SNAPSHOTS}
                )
                """
            )

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            create table if not exists source_health_alert_state(
              fingerprint text primary key,
              alert_type text not null,
              health_key text not null,
              current_status text not null,
              first_detected_at text not null,
              last_detected_at text not null,
              last_sent_at text,
              send_count integer not null,
              resolved_at text
            );
            create index if not exists source_health_alert_health_key_idx
              on source_health_alert_state(health_key);
            create table if not exists source_health_daily_summary(
              summary_date text primary key,
              sent_at text not null,
              run_id text not null
            );
            create table if not exists source_health_alert_events(
              event_id integer primary key autoincrement,
              run_id text not null,
              fingerprint text not null,
              detected_at text not null,
              alert_type text not null,
              payload_json text not null,
              unique(run_id, fingerprint)
            );
            create index if not exists source_health_alert_events_detected_idx
              on source_health_alert_events(detected_at);
            create table if not exists source_health_coverage_snapshots(
              run_id text primary key,
              observed_at text not null,
              coverage_json text not null
            );
            create table if not exists source_health_minor_digest(
              digest_date text primary key,
              sent_at text not null,
              run_id text not null,
              incident_count integer not null
            );
            create table if not exists source_health_digest(
              digest_date text primary key,
              sent_at text not null,
              run_id text not null,
              incident_count integer not null
            );
            -- The minor-degradation digest this one generalizes kept the same
            -- shape, so carrying its ledger forward keeps "already sent today"
            -- true across the upgrade. Idempotent: digest_date is the key.
            insert or ignore into source_health_digest(
              digest_date, sent_at, run_id, incident_count
            )
            select digest_date, sent_at, run_id, incident_count
            from source_health_minor_digest;
            """
        )
        self._conn.commit()


def _candidate(
    state: SourceHealthState,
    *,
    transition: HealthTransition | None,
    alert_type: str,
    severity: str,
    run_id: str,
    coverage: CompanyCoverage | None,
    action: str,
    fallback_usable: bool | None = None,
) -> HealthAlertCandidate:
    label = state.company or state.feed_label or state.adapter or state.health_key
    failure_family = (
        "source_failure"
        if alert_type in {"new_failure", "continued_failure"}
        else alert_type
    )
    return HealthAlertCandidate(
        fingerprint=f"{failure_family}|{state.health_key}",
        alert_type=alert_type,
        severity=severity,
        health_key=state.health_key,
        source_kind=state.source_kind,
        company=sanitize_error(state.company) if state.company else None,
        source_label=sanitize_error(label),
        previous_status=(
            transition.from_status
            if transition
            else state.previous_status
        ),
        current_status=state.status,
        consecutive_failures=state.consecutive_failures,
        consecutive_empty=state.consecutive_zero_successes,
        last_success_at=(
            iso_utc(state.last_success_at)
            if state.last_success_at
            else None
        ),
        rows_returned=state.last_rows_returned,
        error_kind=(
            safe_error_kind(state.last_error_kind)
            if state.last_error_kind
            else None
        ),
        direct_fallback_available=(
            coverage.direct_attempt_succeeded
            if coverage
            else None
        ),
        github_fallback_available=(
            coverage.github_backstop_available
            if coverage
            else None
        ),
        recommended_action=action,
        run_id=safe_run_id(run_id),
        diagnostic_summary=_state_diagnostic_summary(state),
        reason_codes=tuple(
            safe_error_kind(code) for code in (state.last_reason_codes or ())
        )[:12],
        adapter=safe_error_kind(state.adapter) if state.adapter else "",
        github_fallback_usable=fallback_usable,
    )


def _allowed_by_mode(
    candidate: HealthAlertCandidate,
    mode: str,
) -> bool:
    """Report whether a candidate may be emailed immediately.

    Severity is the routing key: only HIGH sends immediately. MEDIUM and INFO
    are always deferred to the daily digest, so ``failure_only`` still never
    emails a recovery and no mode can make a MEDIUM incident interrupt.
    ``alert_type`` remains the semantic label and no longer gates delivery.
    """

    if candidate.severity != SEVERITY_HIGH:
        return False
    return mode in {MODE_TRANSITIONS_ONLY, MODE_FAILURE_ONLY}


def _merge_candidates(
    *groups: Sequence[HealthAlertCandidate],
) -> tuple[HealthAlertCandidate, ...]:
    by_fingerprint = {
        candidate.fingerprint: candidate
        for group in groups
        for candidate in group
    }
    return tuple(
        sorted(
            by_fingerprint.values(),
            key=lambda item: (
                SEVERITY_ORDER.get(item.severity, 9),
                item.source_label.casefold(),
                item.alert_type,
            ),
        )
    )


def _grouping_error_kind(candidate: HealthAlertCandidate) -> str:
    return safe_error_kind(candidate.error_kind) or "unknown"


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


def _state_diagnostic_summary(state: SourceHealthState) -> str:
    parts = []
    for label, value in (
        ("malformed", state.last_malformed_row_count),
        ("schema", state.last_schema_error_row_count),
        ("duplicates", state.last_duplicate_row_count),
        ("failed_requests", state.last_failed_request_count),
    ):
        if value is not None:
            parts.append(f"{label}={max(0, int(value))}")
    if state.last_reason_codes:
        parts.append(
            "reasons="
            + ",".join(safe_error_kind(code) for code in state.last_reason_codes[:12])
        )
    return sanitize_error(" ".join(parts))


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


def _health_email_env() -> dict[str, str]:
    values = {
        "SMTP_USER": os.getenv("SMTP_USER", "").strip(),
        "SMTP_APP_PASSWORD": os.getenv("SMTP_APP_PASSWORD", "").strip(),
        "EMAIL_TO": (
            os.getenv("WATCHER_HEALTH_EMAIL_TO", "").strip()
            or os.getenv("EMAIL_TO", "").strip()
        ),
    }
    values["EMAIL_FROM"] = (
        os.getenv("WATCHER_HEALTH_EMAIL_FROM", "").strip()
        or os.getenv("EMAIL_FROM", "").strip()
        or values["SMTP_USER"]
    )
    missing = [
        key
        for key in ("SMTP_USER", "SMTP_APP_PASSWORD", "EMAIL_TO")
        if not values[key]
    ]
    if missing:
        raise RuntimeError(
            "source-health email configuration missing: "
            + ", ".join(missing)
        )
    return values


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= number <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return number


def _fingerprint_token(value: object) -> str:
    return "_".join(
        token
        for token in "".join(
            char.casefold() if char.isalnum() else " "
            for char in str(value or "")
        ).split()
    )[:160] or "unknown"


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return utc_datetime(parsed)


def _yes_no_unknown(value: bool | None) -> str:
    return "unknown" if value is None else "yes" if value else "no"
