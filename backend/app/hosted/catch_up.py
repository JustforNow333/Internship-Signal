"""Durable recent-openings catch-up digests for newly watched companies.

Import alerts only ever cover genuinely new openings. A user who starts
watching companies with ``include_recent_openings`` on is additionally owed
one consolidated email of the recent matches that watch admitted. That claim
is a ``HostedCatchUpRequest`` per newly watched company, recorded in the
watchlist transaction, and resolved exactly once:

* immediately, when the account is verified and the company has stored jobs;
* on email verification, for an account that activated its watchlist first;
* on the first later import that stores jobs for the company, when the
  catalog had none yet, bounded by ``JOB_WAIT``.

Resolution re-checks the account (active, verified, not paused, catch-up
still enabled) and queues the eligible matches through the ordinary durable
notification batches, so delivery, retries, and leases are shared with
import alerts. Lifetime item uniqueness means no match is ever sent twice.

A catch-up presents postings as current, so it only includes listings
confirmed current (``listing_confirmed``): not proven gone by a later complete
collection of their own source, and seen within ``UNCONFIRMED_LISTING_MAX_AGE``.
The delivery worker re-checks the same rule before sending. Nothing here closes
a job; a posting left out still matches on the dashboard.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, not_, select
from sqlalchemy.orm import Session

from .match_service import CHUNK_SIZE, is_recent_opening
from .models import (
    CATCH_UP_OPEN_STATUSES,
    HostedCatchUpRequest,
    HostedJob,
    HostedNotificationItem,
    HostedSourceObservation,
    User,
    UserJobMatch,
    UserPreference,
)
from .notification_enqueue import DELIVERY_FREQUENCIES, enqueue_catch_up_items

# How long a request waits for a company with no stored jobs to be collected.
# After that the catch-up lapses rather than becoming an open-ended backlog.
JOB_WAIT = timedelta(days=7)
# How long a posting may go unseen before a catch-up stops presenting it as
# current. Healthy sources are collected every few hours, so this only binds
# when completeness cannot be proven: the source failed, was incomplete, or
# never reported completeness. A complete later collection of the posting's
# own source that no longer lists it excludes it sooner.
UNCONFIRMED_LISTING_MAX_AGE = timedelta(days=7)


def listing_confirmed(now: datetime):
    """SQL condition: the posting is still confirmed current at ``now``.

    Only a successful, complete observation of the source that owns the job,
    made after the job was last seen, proves the posting gone; failed,
    incomplete, and unknown-completeness collections never do.
    """

    proven_gone = exists().where(
        HostedSourceObservation.source_key == HostedJob.source_key,
        HostedSourceObservation.succeeded.is_(True),
        HostedSourceObservation.complete.is_(True),
        HostedSourceObservation.observed_at > HostedJob.last_seen_at,
    )
    return and_(
        HostedJob.last_seen_at >= now - UNCONFIRMED_LISTING_MAX_AGE,
        not_(proven_gone),
    )


def unconfirmed_job_ids(
    db: Session, job_ids: Iterable[uuid.UUID], *, now: datetime
) -> set[uuid.UUID]:
    """The given jobs that are no longer confirmed current at ``now``."""

    unique_ids = list(dict.fromkeys(job_ids))
    unconfirmed: set[uuid.UUID] = set()
    for start in range(0, len(unique_ids), CHUNK_SIZE):
        unconfirmed.update(
            db.scalars(
                select(HostedJob.id).where(
                    HostedJob.id.in_(unique_ids[start : start + CHUNK_SIZE]),
                    not_(listing_confirmed(now)),
                )
            )
        )
    return unconfirmed


def record_watchlist_catch_ups(
    db: Session,
    user_id: uuid.UUID,
    *,
    added_company_ids: Sequence[str],
    removed_company_ids: Sequence[str],
    now: datetime,
) -> None:
    """Record catch-ups for newly watched companies and resolve what can be.

    Called inside the watchlist transaction after match reconciliation, with
    only the companies that were genuinely added (unpaused) or removed, so an
    unchanged save, a pause, or a resume records nothing. Removing a company
    withdraws its unresolved request.
    """

    if removed_company_ids:
        for request in db.scalars(
            select(HostedCatchUpRequest)
            .where(
                HostedCatchUpRequest.user_id == user_id,
                HostedCatchUpRequest.company_id.in_(sorted(removed_company_ids)),
                HostedCatchUpRequest.status.in_(CATCH_UP_OPEN_STATUSES),
            )
            .with_for_update()
        ):
            _complete(request, "cancelled", now)

    preferences = db.get(UserPreference, user_id)
    if (
        not added_company_ids
        or preferences is None
        or not preferences.include_recent_openings
    ):
        db.flush()
        return
    for company_id in sorted(set(added_company_ids)):
        db.add(
            HostedCatchUpRequest(
                user_id=user_id,
                company_id=company_id,
                status="awaiting_verification",
                requested_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    db.flush()
    resolve_user_catch_ups(db, user_id, now=now)


def resolve_user_catch_ups(
    db: Session, user_id: uuid.UUID, *, now: datetime
) -> int:
    """Resolve every unresolved request a user holds. Returns items queued."""

    requests = list(
        db.scalars(
            select(HostedCatchUpRequest)
            .where(
                HostedCatchUpRequest.user_id == user_id,
                HostedCatchUpRequest.status.in_(CATCH_UP_OPEN_STATUSES),
            )
            .order_by(HostedCatchUpRequest.company_id, HostedCatchUpRequest.id)
            .with_for_update()
        )
    )
    return _resolve(db, user_id, requests, now=now) if requests else 0


def resolve_import_catch_ups(
    db: Session, job_ids: Iterable[uuid.UUID], *, now: datetime
) -> int:
    """Resolve requests waiting on the companies an import just stored.

    Runs inside the import transaction after reconciliation and after the
    import's own new-opening alerts, so a new opening keeps its import alert
    and is never repeated in the catch-up.
    """

    unique_ids = list(dict.fromkeys(job_ids))
    if not unique_ids or not db.scalar(
        select(HostedCatchUpRequest.id)
        .where(HostedCatchUpRequest.status == "awaiting_jobs")
        .limit(1)
    ):
        return 0
    company_ids: set[str] = set()
    for start in range(0, len(unique_ids), CHUNK_SIZE):
        company_ids.update(
            db.scalars(
                select(HostedJob.company_id)
                .where(HostedJob.id.in_(unique_ids[start : start + CHUNK_SIZE]))
                .distinct()
            )
        )
    if not company_ids:
        return 0
    waiting = list(
        db.scalars(
            select(HostedCatchUpRequest)
            .where(
                HostedCatchUpRequest.company_id.in_(sorted(company_ids)),
                HostedCatchUpRequest.status == "awaiting_jobs",
            )
            .order_by(
                HostedCatchUpRequest.user_id,
                HostedCatchUpRequest.company_id,
                HostedCatchUpRequest.id,
            )
            .with_for_update()
        )
    )
    by_user: dict[uuid.UUID, list[HostedCatchUpRequest]] = {}
    for request in waiting:
        by_user.setdefault(request.user_id, []).append(request)
    return sum(
        _resolve(db, user_id, requests, now=now)
        for user_id, requests in by_user.items()
    )


def _resolve(
    db: Session,
    user_id: uuid.UUID,
    requests: list[HostedCatchUpRequest],
    *,
    now: datetime,
) -> int:
    user = db.get(User, user_id)
    preferences = db.get(UserPreference, user_id)
    if user is None or not user.is_active:
        _complete_all(requests, "suppressed", now)
        return 0
    if user.email_verified_at is None:
        # Kept durably; verification resolves it.
        return 0
    if (
        preferences is None
        or preferences.globally_paused
        or preferences.alert_frequency not in DELIVERY_FREQUENCIES
        or not preferences.include_recent_openings
    ):
        _complete_all(requests, "suppressed", now)
        return 0

    live: list[HostedCatchUpRequest] = []
    for request in requests:
        if request.wait_expires_at is not None and request.wait_expires_at <= now:
            _complete(request, "expired", now)
        else:
            live.append(request)
    if not live:
        return 0

    candidates = _eligible_matches(db, live, now=now)
    contributed = enqueue_catch_up_items(
        db,
        user_id=user_id,
        frequency=preferences.alert_frequency,
        items=candidates,
        now=now,
    )

    stored_companies = set(
        db.scalars(
            select(HostedJob.company_id)
            .where(HostedJob.company_id.in_(sorted({r.company_id for r in live})))
            .distinct()
        )
    )
    for request in live:
        if request.id in contributed:
            _complete(request, "enqueued", now)
        elif request.company_id in stored_companies:
            # The company has been collected and simply has nothing to catch
            # up on, so there is no reason to keep waiting.
            _complete(request, "empty", now)
        else:
            if request.status != "awaiting_jobs":
                request.status = "awaiting_jobs"
                request.wait_expires_at = now + JOB_WAIT
                request.updated_at = now
    db.flush()
    return sum(contributed.values())


def _eligible_matches(
    db: Session, requests: list[HostedCatchUpRequest], *, now: datetime
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """Unnotified, active matches each request admitted, in a stable order."""

    request_by_company = {request.company_id: request for request in requests}
    rows = db.execute(
        select(
            UserJobMatch.id,
            UserJobMatch.matched_at,
            HostedJob.company_id,
            HostedJob.posting_date,
            HostedJob.first_seen_at,
        )
        .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
        .where(
            UserJobMatch.user_id == requests[0].user_id,
            HostedJob.company_id.in_(sorted(request_by_company)),
            UserJobMatch.no_longer_matches_at.is_(None),
            UserJobMatch.dismissed_at.is_(None),
            HostedJob.is_open.is_(True),
            listing_confirmed(now),
            ~select(HostedNotificationItem.id)
            .where(HostedNotificationItem.user_job_match_id == UserJobMatch.id)
            .exists(),
        )
        .order_by(UserJobMatch.id)
    ).all()
    return [
        (match_id, request_by_company[company_id].id)
        for match_id, matched_at, company_id, posting_date, first_seen_at in rows
        if matched_at >= request_by_company[company_id].requested_at
        and is_recent_opening(posting_date, first_seen_at, now=now)
    ]


def _complete_all(
    requests: list[HostedCatchUpRequest], status: str, now: datetime
) -> None:
    for request in requests:
        _complete(request, status, now)


def _complete(request: HostedCatchUpRequest, status: str, now: datetime) -> None:
    request.status = status
    request.completed_at = now
    request.updated_at = now
