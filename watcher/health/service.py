"""Alert evaluation, delivery orchestration, and the SMTP boundary."""

from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import replace
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Mapping, Sequence

from watcher.health.models import (
    ALERT_MINOR_DEGRADATION,
    ALERT_MINOR_RECOVERY,
    ALERT_NEW_FAILURE,
    ALERT_RECOVERY,
    FLAP_LOOKBACK_HOURS,
    GITHUB_EVIDENCE_HORIZON_DAYS,
    MAX_DIGEST_CATCHUP_DAYS,
    MODE_DAILY_SUMMARY,
    MODE_OFF,
    CompanyCoverage,
    HealthAlertCandidate,
    HealthAlertPolicy,
    HealthAlertResult,
    HealthSummary,
    HealthTransition,
    SourceHealthState,
)
from watcher.health.policy import (
    _allowed_by_mode,
    build_alert_candidates,
    build_digest_incidents,
    resolve_digest_window,
)
from watcher.health.rendering import (
    render_alert_email,
    render_daily_summary,
    render_source_health_digest,
)
from watcher.health.sanitize import sanitize_error, utc_datetime
from watcher.health.store import HealthAlertStore
from watcher.source_comparison import SourceComparisonReport

# The logger name is pinned rather than derived from ``__name__`` because the
# console format prints it and callers filter records by it.
LOGGER = logging.getLogger("watcher.health_alerts")


SMTP_HOST = "smtp.gmail.com"


SMTP_PORT = 465


SMTP_TIMEOUT_SECONDS = 30


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
    """Apply daily-summary or severity-routed immediate-alert policy."""

    candidates = tuple(candidates)
    if policy.mode == MODE_DAILY_SUMMARY:
        due = (
            now.hour >= policy.hour_utc
            and not store.daily_summary_sent(now.date().isoformat())
        )
        sendable: list[HealthAlertCandidate] = []
        suppressed = 0
        if not due:
            return _empty_alert_result(policy.mode, candidates)
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
        policy_candidates = [
            candidate
            for candidate in candidates
            if _allowed_by_mode(candidate, policy.mode)
        ]
        sendable = []
        suppressed = 0
        cooldown = timedelta(hours=policy.cooldown_hours)
        for candidate in policy_candidates:
            if store.should_send(candidate, now=now, cooldown=cooldown):
                sendable.append(candidate)
            else:
                suppressed += 1
        if not sendable:
            return _empty_alert_result(
                policy.mode,
                candidates,
                suppressed=suppressed,
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
                item.alert_type == ALERT_RECOVERY for item in candidates
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
            item.alert_type == ALERT_RECOVERY for item in candidates
        ),
        subject=subject,
        error=None if sent else "health_sender_returned_false",
        daily_summary_sent=bool(sent and policy.mode == MODE_DAILY_SUMMARY),
    )


def _empty_alert_result(
    mode: str,
    candidates: Sequence[HealthAlertCandidate],
    *,
    suppressed: int = 0,
) -> HealthAlertResult:
    return HealthAlertResult(
        mode=mode,
        candidates=len(candidates),
        sent=False,
        suppressed_by_cooldown=suppressed,
        recovery_alerts=sum(
            item.alert_type == ALERT_RECOVERY for item in candidates
        ),
        subject="",
        error=None,
        daily_summary_sent=False,
    )


def _maybe_send_daily_digest(
    *,
    store: "HealthAlertStore",
    policy: HealthAlertPolicy,
    run_id: str,
    now: datetime,
    states: Mapping[str, SourceHealthState],
    sender: Callable[[str, str], bool],
) -> tuple[bool, int, bool]:
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
