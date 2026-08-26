"""Alert policy: severity, fallback evidence, flapping, and grouping.

Everything here is pure and decides *whether* something is worth reporting and
how loudly. Delivery lives in :mod:`watcher.health.service`, wording in
:mod:`watcher.health.rendering`, and persistence in :mod:`watcher.health.store`.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Mapping, Sequence

from watcher.health.models import (
    ALERT_BOTH_TIERS_UNAVAILABLE,
    ALERT_CONTINUED_FAILURE,
    ALERT_COVERAGE_REGRESSION,
    ALERT_DIRECT_SOURCE_DEGRADED,
    ALERT_FEED_STALE,
    ALERT_MINOR_DEGRADATION,
    ALERT_MINOR_RECOVERY,
    ALERT_NEW_FAILURE,
    ALERT_RECOVERY,
    ALERT_UNKNOWN_DIAGNOSTICS,
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_UNCOVERED,
    DEFAULT_COOLDOWN_HOURS,
    DEFAULT_FEED_STALE_HOURS,
    DEFAULT_HOUR_UTC,
    DEFAULT_MODE,
    DIGEST_SEVERITIES,
    DIGEST_WINDOW_HOURS,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_UNKNOWN,
    FAILURE_ALERT_TYPES,
    FLAP_REPEAT_THRESHOLD,
    HEALTH_EMAIL_MODES,
    MAX_DIGEST_CATCHUP_DAYS,
    MAX_MINOR_SKIPPED_ROWS,
    MIN_RETAINED_ROWS_PER_SKIPPED_ROW,
    MINOR_DEGRADATION_REASONS,
    MODE_FAILURE_ONLY,
    MODE_TRANSITIONS_ONLY,
    RECOVERY_ALERT_TYPES,
    SEVERITY_HIGH,
    SEVERITY_INFO,
    SEVERITY_MEDIUM,
    SEVERITY_ORDER,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_EMPTY,
    STATUS_FAILING,
    STATUS_HEALTHY,
    SYSTEMIC_GROUP_DOMINANCE_PERCENT,
    SYSTEMIC_GROUP_MIN_COMPANIES,
    CompanyCoverage,
    DigestIncident,
    HealthAlertCandidate,
    HealthAlertPolicy,
    HealthTransition,
    SourceHealthState,
    SystemicIncidentGroup,
)
from watcher.health.sanitize import (
    iso_utc,
    safe_error_kind,
    safe_run_id,
    sanitize_error,
)


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
    # A recovered request or whole-crawl retry is minor only when the final
    # collection is whole.
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


def _fingerprint_token(value: object) -> str:
    return "_".join(
        token
        for token in "".join(
            char.casefold() if char.isalnum() else " "
            for char in str(value or "")
        ).split()
    )[:160] or "unknown"


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


def _grouping_error_kind(candidate: HealthAlertCandidate) -> str:
    return safe_error_kind(candidate.error_kind) or "unknown"


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
