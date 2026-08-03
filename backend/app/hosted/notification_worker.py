"""One-shot durable delivery worker for hosted notification batches."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from .database import HostedDatabase
from .models import (
    HostedJob,
    HostedNotificationAttempt,
    HostedNotificationBatch,
    HostedNotificationItem,
    User,
    UserJobMatch,
    UserPreference,
)
from .notification_mail import (
    DeliveryResult,
    DigestJob,
    NotificationEmail,
    NotificationTransport,
    build_digest_email,
)
from .services import utc_now

logger = logging.getLogger("hosted.notification_delivery")

LEASE_DURATION = timedelta(minutes=10)
RETRY_DELAYS = {
    1: timedelta(minutes=1),
    2: timedelta(minutes=5),
    3: timedelta(minutes=15),
    4: timedelta(hours=1),
}
MAX_ATTEMPTS = 5
MIN_LIMIT = 1
MAX_LIMIT = 100

BATCH_CANCELLATION_CODES = frozenset(
    {
        "user_inactive",
        "email_unverified",
        "globally_paused",
        "frequency_paused",
        "frequency_changed",
        "no_valid_items",
    }
)
ITEM_CANCELLATION_CODES = frozenset(
    {"match_dismissed", "match_inactive", "job_closed", "match_missing", "job_missing"}
)
ERROR_CODES = frozenset(
    {
        "smtp_not_configured",
        "smtp_authentication_failed",
        "sender_rejected",
        "recipient_rejected",
        "smtp_data_4xx",
        "smtp_data_5xx",
        "smtp_data_unknown",
        "smtp_4xx",
        "smtp_5xx",
        "smtp_response_unknown",
        "connection_lost_after_submission",
        "connection_failed_before_submission",
        "smtp_unexpected_after_submission",
        "smtp_setup_failed",
        "unexpected_after_submission",
        "unexpected_before_submission",
        "unexpected_transport_failure",
        "unexpected_transport_result",
        "retry_exhausted",
        "lease_expired_after_send_started",
    }
)


@dataclass(frozen=True)
class WorkerSummary:
    recovered_pending: int = 0
    recovered_uncertain: int = 0
    claimed: int = 0
    sent: int = 0
    retryable_failures: int = 0
    permanent_failures: int = 0
    uncertain: int = 0
    cancelled: int = 0


@dataclass(frozen=True)
class _PreparedDelivery:
    batch_id: uuid.UUID
    token: uuid.UUID
    attempt_number: int
    item_ids: tuple[uuid.UUID, ...]
    message: NotificationEmail


class NotificationDeliveryWorker:
    def __init__(
        self,
        database: HostedDatabase,
        transport: NotificationTransport,
        public_frontend_url: str,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database = database
        self.transport = transport
        self.public_frontend_url = public_frontend_url
        self.clock = clock

    def run(self, *, limit: int = 25) -> WorkerSummary:
        validated_limit = validate_limit(limit)
        recovered_pending, recovered_uncertain = self.recover_expired_leases()
        claims = self.claim_due_batches(limit=validated_limit)
        counts = {
            "sent": 0,
            "retryable_failures": 0,
            "permanent_failures": 0,
            "uncertain": recovered_uncertain,
            "cancelled": 0,
        }
        for batch_id, token in claims:
            started = time.perf_counter()
            prepared = self._prepare_delivery(batch_id, token)
            if prepared is None:
                counts["cancelled"] += 1
                logger.info(
                    "HOSTED-NOTIFICATION batch=%s outcome=cancelled elapsed_ms=%.3f",
                    str(batch_id)[:12],
                    (time.perf_counter() - started) * 1000,
                )
                continue
            try:
                result = self.transport.send(prepared.message)
            except Exception:  # noqa: BLE001 - submission may already have begun
                result = DeliveryResult("uncertain", "unexpected_transport_failure")
            outcome = self._apply_result(prepared, result)
            if outcome == "sent":
                counts["sent"] += 1
            elif outcome == "retryable_failure":
                counts["retryable_failures"] += 1
            elif outcome == "permanent_failure":
                counts["permanent_failures"] += 1
            elif outcome == "uncertain":
                counts["uncertain"] += 1
            logger.info(
                "HOSTED-NOTIFICATION batch=%s items=%d outcome=%s code=%s elapsed_ms=%.3f",
                str(batch_id)[:12],
                len(prepared.item_ids),
                outcome,
                _error_code(result.error_code, "none"),
                (time.perf_counter() - started) * 1000,
            )
        return WorkerSummary(
            recovered_pending=recovered_pending,
            recovered_uncertain=recovered_uncertain,
            claimed=len(claims),
            **counts,
        )

    def recover_expired_leases(self) -> tuple[int, int]:
        now = self.clock()
        pending = 0
        uncertain = 0
        with self.database.session_factory.begin() as db:
            batches = list(
                db.scalars(
                    select(HostedNotificationBatch)
                    .where(
                        HostedNotificationBatch.status == "processing",
                        HostedNotificationBatch.lease_expires_at <= now,
                    )
                    .order_by(HostedNotificationBatch.lease_expires_at)
                    .with_for_update(skip_locked=True)
                )
            )
            for batch in batches:
                attempt = None
                if batch.send_started_at is not None:
                    attempt = db.scalar(
                        select(HostedNotificationAttempt)
                        .where(
                            HostedNotificationAttempt.batch_id == batch.id,
                            HostedNotificationAttempt.attempt_number
                            == batch.attempt_count,
                        )
                        .with_for_update()
                    )
                if batch.send_started_at is None:
                    batch.status = "pending"
                    batch.next_attempt_at = now
                    batch.last_error_code = None
                    pending += 1
                else:
                    batch.status = "uncertain"
                    batch.last_error_code = "lease_expired_after_send_started"
                    if attempt is not None and attempt.completed_at is None:
                        attempt.completed_at = now
                        attempt.outcome = "uncertain"
                        attempt.error_code = "lease_expired_after_send_started"
                    uncertain += 1
                _clear_processing(batch)
                batch.updated_at = now
        return pending, uncertain

    def claim_due_batches(self, *, limit: int) -> list[tuple[uuid.UUID, uuid.UUID]]:
        now = self.clock()
        claims: list[tuple[uuid.UUID, uuid.UUID]] = []
        with self.database.session_factory.begin() as db:
            batches = list(
                db.scalars(
                    select(HostedNotificationBatch)
                    .where(
                        HostedNotificationBatch.status == "pending",
                        HostedNotificationBatch.due_at <= now,
                        HostedNotificationBatch.next_attempt_at <= now,
                    )
                    .order_by(
                        HostedNotificationBatch.due_at,
                        HostedNotificationBatch.created_at,
                        HostedNotificationBatch.id,
                    )
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for batch in batches:
                token = uuid.uuid4()
                batch.status = "processing"
                batch.processing_token = token
                batch.processing_started_at = now
                batch.lease_expires_at = now + LEASE_DURATION
                batch.updated_at = now
                claims.append((batch.id, token))
        return claims

    def _prepare_delivery(
        self, batch_id: uuid.UUID, token: uuid.UUID
    ) -> _PreparedDelivery | None:
        now = self.clock()
        with self.database.session_factory.begin() as db:
            batch = db.scalar(
                select(HostedNotificationBatch)
                .where(
                    HostedNotificationBatch.id == batch_id,
                    HostedNotificationBatch.status == "processing",
                    HostedNotificationBatch.processing_token == token,
                )
                .with_for_update()
            )
            if batch is None:
                return None
            user = db.get(User, batch.user_id)
            preferences = db.get(UserPreference, batch.user_id)
            cancellation = _user_cancellation(batch, user, preferences)
            if cancellation is not None:
                _cancel_batch(batch, cancellation, now)
                for item in batch.items:
                    if item.status == "pending":
                        _cancel_item(item, cancellation, now)
                return None

            rows = db.execute(
                select(HostedNotificationItem, UserJobMatch, HostedJob)
                .outerjoin(
                    UserJobMatch,
                    UserJobMatch.id == HostedNotificationItem.user_job_match_id,
                )
                .outerjoin(HostedJob, HostedJob.id == UserJobMatch.job_id)
                .where(
                    HostedNotificationItem.batch_id == batch.id,
                    HostedNotificationItem.status == "pending",
                )
                .order_by(
                    UserJobMatch.matched_at,
                    HostedNotificationItem.created_at,
                    HostedNotificationItem.id,
                )
                .with_for_update(of=HostedNotificationItem)
            ).all()
            valid: list[tuple[HostedNotificationItem, UserJobMatch, HostedJob]] = []
            for item, match, job in rows:
                reason = _item_cancellation(match, job)
                if reason is not None:
                    _cancel_item(item, reason, now)
                else:
                    valid.append((item, match, job))
            if not valid:
                _cancel_batch(batch, "no_valid_items", now)
                return None

            jobs = [
                DigestJob(
                    company_name=job.company_name,
                    title=job.title,
                    location=job.location,
                    remote_status=job.remote_status,
                    posting_date=job.posting_date,
                    deadline=job.deadline,
                    application_url=job.application_url,
                    match_reasons=match.match_reasons,
                )
                for _item, match, job in valid
            ]
            assert user is not None
            message = build_digest_email(
                recipient=user.email,
                frequency=batch.frequency,
                jobs=jobs,
                message_id=batch.email_message_id,
                public_frontend_url=self.public_frontend_url,
            )
            attempt_number = batch.attempt_count + 1
            batch.attempt_count = attempt_number
            batch.send_started_at = now
            batch.updated_at = now
            db.add(
                HostedNotificationAttempt(
                    batch_id=batch.id,
                    attempt_number=attempt_number,
                    started_at=now,
                )
            )
            return _PreparedDelivery(
                batch_id=batch.id,
                token=token,
                attempt_number=attempt_number,
                item_ids=tuple(item.id for item, _match, _job in valid),
                message=message,
            )

    def _apply_result(
        self, prepared: _PreparedDelivery, result: DeliveryResult
    ) -> str:
        now = self.clock()
        allowed_outcomes = {
            "sent", "retryable_failure", "permanent_failure", "uncertain"
        }
        outcome = (
            result.outcome if result.outcome in allowed_outcomes else "uncertain"
        )
        code = _error_code(result.error_code, "unexpected_transport_result")
        with self.database.session_factory.begin() as db:
            batch = db.scalar(
                select(HostedNotificationBatch)
                .where(
                    HostedNotificationBatch.id == prepared.batch_id,
                    HostedNotificationBatch.status == "processing",
                    HostedNotificationBatch.processing_token == prepared.token,
                )
                .with_for_update()
            )
            if batch is None:
                return "uncertain"
            attempt = db.scalar(
                select(HostedNotificationAttempt)
                .where(
                    HostedNotificationAttempt.batch_id == prepared.batch_id,
                    HostedNotificationAttempt.attempt_number
                    == prepared.attempt_number,
                )
                .with_for_update()
            )
            if attempt is None or attempt.completed_at is not None:
                return "uncertain"
            attempt.completed_at = now
            attempt.outcome = outcome
            attempt.error_code = None if outcome == "sent" else code

            if outcome == "sent":
                items = list(
                    db.scalars(
                        select(HostedNotificationItem).where(
                            HostedNotificationItem.id.in_(prepared.item_ids),
                            HostedNotificationItem.status == "pending",
                        )
                    )
                )
                batch.status = "sent"
                batch.sent_at = now
                batch.last_error_code = None
                for item in items:
                    item.status = "sent"
                    item.sent_at = now
                    item.updated_at = now
            elif outcome == "retryable_failure" and batch.attempt_count < MAX_ATTEMPTS:
                batch.status = "pending"
                batch.next_attempt_at = now + RETRY_DELAYS[batch.attempt_count]
                batch.send_started_at = None
                batch.last_error_code = code
            elif outcome == "retryable_failure":
                batch.status = "permanent_failed"
                batch.last_error_code = "retry_exhausted"
            elif outcome == "permanent_failure":
                batch.status = "permanent_failed"
                batch.last_error_code = code
            else:
                batch.status = "uncertain"
                batch.last_error_code = code
            _clear_processing(batch)
            batch.updated_at = now
        return outcome


def validate_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_LIMIT <= value <= MAX_LIMIT:
        raise ValueError(f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}")
    return value


def _user_cancellation(
    batch: HostedNotificationBatch,
    user: User | None,
    preferences: UserPreference | None,
) -> str | None:
    if user is None or not user.is_active:
        return "user_inactive"
    if user.email_verified_at is None:
        return "email_unverified"
    if preferences is None or preferences.globally_paused:
        return "globally_paused"
    if preferences.alert_frequency == "paused":
        return "frequency_paused"
    if preferences.alert_frequency != batch.frequency:
        return "frequency_changed"
    return None


def _item_cancellation(
    match: UserJobMatch | None, job: HostedJob | None
) -> str | None:
    if match is None:
        return "match_missing"
    if job is None:
        return "job_missing"
    if match.dismissed_at is not None:
        return "match_dismissed"
    if match.no_longer_matches_at is not None:
        return "match_inactive"
    if not job.is_open:
        return "job_closed"
    return None


def _cancel_batch(batch: HostedNotificationBatch, code: str, now: datetime) -> None:
    assert code in BATCH_CANCELLATION_CODES
    batch.status = "cancelled"
    batch.cancelled_at = now
    batch.last_error_code = code
    _clear_processing(batch)
    batch.updated_at = now


def _cancel_item(item: HostedNotificationItem, code: str, now: datetime) -> None:
    # Batch-wide cancellation codes are also an explicit safe allowlist.
    assert code in ITEM_CANCELLATION_CODES or code in BATCH_CANCELLATION_CODES
    item.status = "cancelled"
    item.cancellation_reason = code
    item.cancelled_at = now
    item.updated_at = now


def _clear_processing(batch: HostedNotificationBatch) -> None:
    batch.processing_token = None
    batch.processing_started_at = None
    batch.lease_expires_at = None


def _error_code(value: str | None, fallback: str) -> str:
    return value if value in ERROR_CODES else fallback
