"""Transactional notification creation for newly inserted import matches,
plus the re-homing that keeps pending work alive across frequency changes."""

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

# The bounded reason recorded on a batch that re-homing emptied. The moved
# items stay pending; only the retired batch carries this code.
REHOMED_BATCH_CODE = "frequency_changed"


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


def rehome_pending_items(
    db: Session,
    batch: HostedNotificationBatch,
    *,
    frequency: str,
    now: datetime,
) -> int:
    """Move ``batch``'s pending items onto delivery work for ``frequency``.

    Changing between active delivery frequencies must never discard a valid
    alert. ``HostedNotificationItem`` is unique on ``user_job_match_id`` for the
    row's whole lifetime, so a cancelled item can never be replaced; the item is
    therefore re-pointed at a batch for the user's current frequency instead.
    Provenance (``user_job_match_id`` and ``source_import_run_id``) is untouched.

    Returns the number of items moved. An item whose target cannot be resolved
    safely is left in place rather than dropped, so the caller must re-check for
    remaining pending items before retiring ``batch``.
    """

    if frequency not in DELIVERY_FREQUENCIES or frequency == batch.frequency:
        return 0

    items = list(
        db.scalars(
            select(HostedNotificationItem)
            .where(
                HostedNotificationItem.batch_id == batch.id,
                HostedNotificationItem.status == "pending",
            )
            .order_by(
                HostedNotificationItem.created_at, HostedNotificationItem.id
            )
            .with_for_update()
        )
    )
    if not items:
        return 0

    moved = 0
    targets: dict[uuid.UUID | None, HostedNotificationBatch] = {}
    for item in items:
        # Rolling frequencies share one target per user; as-detected keeps one
        # batch per source import run, which is what its unique key and its
        # source_import_frequency check constraint both require.
        key = item.source_import_run_id if frequency == "as_detected" else None
        target = targets.get(key)
        if target is None:
            target = _rehome_target(
                db,
                user_id=batch.user_id,
                frequency=frequency,
                import_run_id=item.source_import_run_id,
                now=now,
            )
            if target is None:
                continue
            targets[key] = target
        item.batch_id = target.id
        item.updated_at = now
        moved += 1
    if moved:
        db.flush()
    return moved


def rehome_user_pending_items(
    db: Session,
    *,
    user_id: uuid.UUID,
    frequency: str,
    now: datetime,
) -> int:
    """Re-home every pending batch a user holds under a different frequency.

    Batches a delivery worker has already claimed are skipped: those are
    ``processing`` rather than ``pending``, and the worker re-homes them itself
    inside the transaction that holds their lease.
    """

    if frequency not in DELIVERY_FREQUENCIES:
        return 0
    batches = list(
        db.scalars(
            select(HostedNotificationBatch)
            .where(
                HostedNotificationBatch.user_id == user_id,
                HostedNotificationBatch.status == "pending",
                HostedNotificationBatch.frequency != frequency,
            )
            .order_by(
                HostedNotificationBatch.created_at, HostedNotificationBatch.id
            )
            .with_for_update(skip_locked=True)
        )
    )
    moved = 0
    for batch in batches:
        moved += rehome_pending_items(db, batch, frequency=frequency, now=now)
        if not pending_item_count(db, batch):
            retire_rehomed_batch(batch, now)
    return moved


def pending_item_count(db: Session, batch: HostedNotificationBatch) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(HostedNotificationItem)
            .where(
                HostedNotificationItem.batch_id == batch.id,
                HostedNotificationItem.status == "pending",
            )
        )
        or 0
    )


def retire_rehomed_batch(
    batch: HostedNotificationBatch, now: datetime
) -> None:
    """Cancel a batch that re-homing emptied.

    Only the batch is cancelled. Its items have already moved and stay pending,
    so none of them is given a ``cancellation_reason``.
    """

    batch.status = "cancelled"
    batch.cancelled_at = now
    batch.last_error_code = REHOMED_BATCH_CODE
    batch.processing_token = None
    batch.processing_started_at = None
    batch.lease_expires_at = None
    batch.updated_at = now


def _rehome_target(
    db: Session,
    *,
    user_id: uuid.UUID,
    frequency: str,
    import_run_id: uuid.UUID,
    now: datetime,
) -> HostedNotificationBatch | None:
    """Resolve the batch a re-homed item should join, or None when unsafe.

    The new due window is measured from the moment the change is processed, so
    a move behaves exactly like a fresh enqueue under the new frequency.
    """

    if frequency != "as_detected":
        return _batch_for_item(
            db,
            user_id=user_id,
            frequency=frequency,
            import_run_id=None,
            now=now,
        )

    existing = db.scalar(
        select(HostedNotificationBatch)
        .where(
            HostedNotificationBatch.user_id == user_id,
            HostedNotificationBatch.source_import_run_id == import_run_id,
        )
        .with_for_update()
    )
    if existing is None:
        return _new_batch(
            db,
            user_id=user_id,
            frequency="as_detected",
            import_run_id=import_run_id,
            due_at=now,
            now=now,
        )
    if existing.status == "pending":
        return existing
    if (
        existing.status == "cancelled"
        and existing.last_error_code == REHOMED_BATCH_CODE
        and existing.send_started_at is None
    ):
        # This is the slot an earlier re-home of these same never-submitted
        # items retired. (user_id, source_import_run_id) is unique, so the slot
        # is reclaimed rather than duplicated. Nothing was ever handed to a mail
        # provider from it, so reviving it cannot resend anything.
        existing.status = "pending"
        existing.cancelled_at = None
        existing.last_error_code = None
        existing.due_at = now
        existing.next_attempt_at = now
        existing.processing_token = None
        existing.processing_started_at = None
        existing.lease_expires_at = None
        existing.updated_at = now
        return existing
    # A batch that has reached a terminal or in-flight delivery state keeps its
    # slot. The item stays where it is and is still delivered.
    return None


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
