"""Transactional notification creation for newly inserted import matches."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from .models import (
    HostedJob,
    HostedNotificationBatch,
    HostedNotificationItem,
    User,
    UserJobMatch,
    UserPreference,
)

DELIVERY_FREQUENCIES = frozenset({"as_detected", "three_hour", "daily"})
ROLLING_DELAYS = {
    "three_hour": timedelta(hours=3),
    "daily": timedelta(hours=24),
}


def enqueue_import_notifications(
    db: Session,
    match_ids: Iterable[uuid.UUID],
    *,
    import_run_id: uuid.UUID,
    now: datetime,
) -> int:
    """Create notification items for this import's genuinely new match rows.

    The caller owns the surrounding job-import transaction. Any failure here
    therefore rolls back jobs, matches, import success, batches, and items
    together. Preference/watchlist reconciliation never calls this function.
    """

    unique_ids = list(dict.fromkeys(match_ids))
    if not unique_ids:
        return 0

    rows = db.execute(
        select(UserJobMatch, HostedJob, User, UserPreference)
        .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
        .join(User, User.id == UserJobMatch.user_id)
        .join(UserPreference, UserPreference.user_id == UserJobMatch.user_id)
        .where(UserJobMatch.id.in_(unique_ids))
        .order_by(UserJobMatch.user_id, UserJobMatch.id)
    ).all()
    eligible = [
        (match, job, preference.alert_frequency)
        for match, job, user, preference in rows
        if user.is_active
        and user.email_verified_at is not None
        and not preference.globally_paused
        and preference.alert_frequency in DELIVERY_FREQUENCIES
        and match.no_longer_matches_at is None
        and match.dismissed_at is None
        and job.is_open
    ]

    created = 0
    batch_cache: dict[tuple[uuid.UUID, str], HostedNotificationBatch] = {}
    for match, _job, frequency in eligible:
        cache_key = (match.user_id, frequency)
        batch = batch_cache.get(cache_key)
        if batch is None:
            batch = _batch_for_item(
                db,
                user_id=match.user_id,
                frequency=frequency,
                import_run_id=import_run_id,
                now=now,
            )
            batch_cache[cache_key] = batch

        inserted = db.scalar(
            postgresql_insert(HostedNotificationItem)
            .values(
                id=uuid.uuid4(),
                batch_id=batch.id,
                user_job_match_id=match.id,
                source_import_run_id=import_run_id,
                status="pending",
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["user_job_match_id"])
            .returning(HostedNotificationItem.id)
        )
        if inserted is not None:
            created += 1
    return created


def _batch_for_item(
    db: Session,
    *,
    user_id: uuid.UUID,
    frequency: str,
    import_run_id: uuid.UUID,
    now: datetime,
) -> HostedNotificationBatch:
    if frequency == "as_detected":
        existing = db.scalar(
            select(HostedNotificationBatch).where(
                HostedNotificationBatch.user_id == user_id,
                HostedNotificationBatch.source_import_run_id == import_run_id,
            )
        )
        if existing is not None:
            return existing
        return _new_batch(
            db,
            user_id=user_id,
            frequency=frequency,
            import_run_id=import_run_id,
            due_at=now,
            now=now,
        )

    # A transaction-scoped PostgreSQL advisory lock serializes rolling-batch
    # selection per user/frequency without blocking unrelated users. This is a
    # database-enforced concurrency guard; item and as-detected uniqueness are
    # additionally protected by constraints and conflict-safe inserts.
    lock_key = f"hosted-notification:{user_id}:{frequency}"
    db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
    )
    existing = db.scalar(
        select(HostedNotificationBatch)
        .where(
            HostedNotificationBatch.user_id == user_id,
            HostedNotificationBatch.frequency == frequency,
            HostedNotificationBatch.status == "pending",
            HostedNotificationBatch.attempt_count == 0,
        )
        .order_by(HostedNotificationBatch.created_at, HostedNotificationBatch.id)
        .limit(1)
        .with_for_update()
    )
    if existing is not None:
        return existing
    return _new_batch(
        db,
        user_id=user_id,
        frequency=frequency,
        import_run_id=None,
        due_at=now + ROLLING_DELAYS[frequency],
        now=now,
    )


def _new_batch(
    db: Session,
    *,
    user_id: uuid.UUID,
    frequency: str,
    import_run_id: uuid.UUID | None,
    due_at: datetime,
    now: datetime,
) -> HostedNotificationBatch:
    batch_id = uuid.uuid4()
    batch = HostedNotificationBatch(
        id=batch_id,
        user_id=user_id,
        frequency=frequency,
        status="pending",
        due_at=due_at,
        next_attempt_at=due_at,
        attempt_count=0,
        email_message_id=f"<notification-{batch_id}@internship-signal.invalid>",
        source_import_run_id=import_run_id,
        created_at=now,
        updated_at=now,
    )
    db.add(batch)
    db.flush()
    return batch
