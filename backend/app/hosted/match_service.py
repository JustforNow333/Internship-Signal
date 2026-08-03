"""Persistence and reconciliation for hosted per-user job matches.

Reconciliation is idempotent and retry-safe: repeating it with unchanged
inputs performs no writes and never rewrites timestamps. Historical rows are
retained forever; losing a match only stamps ``no_longer_matches_at``. User
actions (``saved_at`` / ``dismissed_at``) are never modified here.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from .matching import (
    MatchDecision,
    evaluate_match,
    job_from_model,
    preferences_from_model,
)
from .models import (
    HostedJob,
    UserCompanyWatch,
    UserJobMatch,
    UserPreference,
)

# Bounds the identifier lists sent in a single statement during large imports.
CHUNK_SIZE = 500


@dataclass(frozen=True)
class ReconciliationOutcome:
    """Counts for one reconciliation pass.

    ``created`` counts only newly inserted ``(user_id, job_id)`` rows. It never
    counts reactivated history, timestamp-only updates, or saved/dismissed
    changes.
    """

    created: int = 0
    reactivated: int = 0
    deactivated: int = 0
    refreshed: int = 0
    created_match_ids: tuple[uuid.UUID, ...] = ()

    def merged(self, other: ReconciliationOutcome) -> ReconciliationOutcome:
        return ReconciliationOutcome(
            created=self.created + other.created,
            reactivated=self.reactivated + other.reactivated,
            deactivated=self.deactivated + other.deactivated,
            refreshed=self.refreshed + other.refreshed,
            created_match_ids=self.created_match_ids + other.created_match_ids,
        )


def reconcile_jobs(
    db: Session,
    job_ids: Iterable[uuid.UUID],
    *,
    now: datetime,
) -> ReconciliationOutcome:
    """Reconcile the given jobs against every user who could match them.

    Candidate users are restricted to watchers of the jobs' companies plus
    users that already hold a match row for those jobs, so this never performs
    an all-users-by-all-jobs scan.
    """

    unique_ids = _ordered_unique(job_ids)
    outcome = ReconciliationOutcome()
    for chunk in _chunks(unique_ids):
        outcome = outcome.merged(_reconcile_job_chunk(db, chunk, now=now))
    return outcome


def reconcile_user(
    db: Session,
    user_id: uuid.UUID,
    *,
    now: datetime,
    company_ids: Sequence[str] | None = None,
) -> ReconciliationOutcome:
    """Reconcile one user after a preference or company-watch change.

    ``company_ids`` narrows the pass to the affected companies. Jobs the user
    already has a match row for are always included so removing a watch or a
    role can deactivate the stale rows.
    """

    preferences = db.get(UserPreference, user_id)
    if preferences is None:
        # Signup always creates preferences; refuse to act on unexpected state
        # rather than deactivating a user's entire match history.
        return ReconciliationOutcome()

    watches = {
        watch.company_id: watch
        for watch in db.scalars(
            select(UserCompanyWatch).where(UserCompanyWatch.user_id == user_id)
        )
    }
    scoped = (
        [company for company in watches if company in set(company_ids)]
        if company_ids is not None
        else list(watches)
    )

    candidate_ids: list[uuid.UUID] = []
    if scoped:
        for chunk in _chunks(sorted(scoped)):
            candidate_ids.extend(
                db.scalars(
                    select(HostedJob.id).where(HostedJob.company_id.in_(chunk))
                )
            )
    existing_query = select(UserJobMatch.job_id).where(
        UserJobMatch.user_id == user_id
    )
    if company_ids is not None:
        scope = set(company_ids)
        existing_query = existing_query.join(
            HostedJob, HostedJob.id == UserJobMatch.job_id
        ).where(HostedJob.company_id.in_(sorted(scope)))
    candidate_ids.extend(db.scalars(existing_query))

    unique_ids = _ordered_unique(candidate_ids)
    pure_preferences = preferences_from_model(preferences)
    outcome = ReconciliationOutcome()
    for chunk in _chunks(unique_ids):
        jobs = {
            job.id: job
            for job in db.scalars(select(HostedJob).where(HostedJob.id.in_(chunk)))
        }
        existing = {
            match.job_id: match
            for match in db.scalars(
                select(UserJobMatch).where(
                    UserJobMatch.user_id == user_id,
                    UserJobMatch.job_id.in_(chunk),
                )
            )
        }
        for job_id in chunk:
            job = jobs.get(job_id)
            if job is None:
                continue
            watch = watches.get(job.company_id)
            decision = evaluate_match(
                job_from_model(job),
                pure_preferences,
                watching=watch is not None,
                watch_paused=bool(watch is not None and watch.paused),
            )
            outcome = outcome.merged(
                _apply(
                    db,
                    existing.get(job_id),
                    user_id=user_id,
                    job_id=job_id,
                    decision=decision,
                    now=now,
                )
            )
    return outcome


def _reconcile_job_chunk(
    db: Session,
    job_ids: Sequence[uuid.UUID],
    *,
    now: datetime,
) -> ReconciliationOutcome:
    jobs = {
        job.id: job
        for job in db.scalars(select(HostedJob).where(HostedJob.id.in_(job_ids)))
    }
    if not jobs:
        return ReconciliationOutcome()

    company_ids = sorted({job.company_id for job in jobs.values()})
    watches: dict[str, list[UserCompanyWatch]] = {}
    for chunk in _chunks(company_ids):
        for watch in db.scalars(
            select(UserCompanyWatch).where(UserCompanyWatch.company_id.in_(chunk))
        ):
            watches.setdefault(watch.company_id, []).append(watch)

    existing: dict[tuple[uuid.UUID, uuid.UUID], UserJobMatch] = {}
    for match in db.scalars(
        select(UserJobMatch).where(UserJobMatch.job_id.in_(job_ids))
    ):
        existing[(match.user_id, match.job_id)] = match

    pairs: list[tuple[uuid.UUID, uuid.UUID]] = []
    seen: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for job_id in job_ids:
        job = jobs.get(job_id)
        if job is None:
            continue
        for watch in watches.get(job.company_id, ()):
            key = (watch.user_id, job_id)
            if key not in seen:
                seen.add(key)
                pairs.append(key)
    for key in existing:
        if key not in seen:
            seen.add(key)
            pairs.append(key)

    user_ids = sorted({user_id for user_id, _ in pairs}, key=str)
    preferences: dict[uuid.UUID, UserPreference] = {}
    for chunk in _chunks(user_ids):
        for row in db.scalars(
            select(UserPreference).where(UserPreference.user_id.in_(chunk))
        ):
            preferences[row.user_id] = row
    pure_preferences = {
        user_id: preferences_from_model(row) for user_id, row in preferences.items()
    }
    watch_index = {
        (watch.user_id, watch.company_id): watch
        for company_watches in watches.values()
        for watch in company_watches
    }

    outcome = ReconciliationOutcome()
    for user_id, job_id in pairs:
        user_preferences = pure_preferences.get(user_id)
        if user_preferences is None:
            continue
        job = jobs[job_id]
        watch = watch_index.get((user_id, job.company_id))
        decision = evaluate_match(
            job_from_model(job),
            user_preferences,
            watching=watch is not None,
            watch_paused=bool(watch is not None and watch.paused),
        )
        outcome = outcome.merged(
            _apply(
                db,
                existing.get((user_id, job_id)),
                user_id=user_id,
                job_id=job_id,
                decision=decision,
                now=now,
            )
        )
    return outcome


def _apply(
    db: Session,
    existing: UserJobMatch | None,
    *,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    decision: MatchDecision,
    now: datetime,
) -> ReconciliationOutcome:
    reasons = [dict(reason) for reason in decision.reasons]
    if decision.matches:
        if existing is None:
            # ON CONFLICT DO NOTHING keeps concurrent reconciliation safe and
            # makes "created" mean exactly "a new row was inserted here".
            inserted = db.scalar(
                postgresql_insert(UserJobMatch)
                .values(
                    id=uuid.uuid4(),
                    user_id=user_id,
                    job_id=job_id,
                    match_reasons=reasons,
                    matched_at=now,
                    last_matched_at=now,
                    no_longer_matches_at=None,
                    saved_at=None,
                    dismissed_at=None,
                    created_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=["user_id", "job_id"])
                .returning(UserJobMatch.id)
            )
            if inserted is None:
                return ReconciliationOutcome()
            return ReconciliationOutcome(created=1, created_match_ids=(inserted,))
        if existing.no_longer_matches_at is not None:
            existing.no_longer_matches_at = None
            existing.match_reasons = reasons
            existing.last_matched_at = now
            existing.updated_at = now
            return ReconciliationOutcome(reactivated=1)
        if list(existing.match_reasons or []) != reasons:
            # A still-active match whose reasons changed is a meaningful new
            # observation; an identical re-observation writes nothing.
            existing.match_reasons = reasons
            existing.last_matched_at = now
            existing.updated_at = now
            return ReconciliationOutcome(refreshed=1)
        return ReconciliationOutcome()

    if existing is not None and existing.no_longer_matches_at is None:
        existing.no_longer_matches_at = now
        existing.updated_at = now
        return ReconciliationOutcome(deactivated=1)
    return ReconciliationOutcome()


def _ordered_unique(values: Iterable[uuid.UUID]) -> list[uuid.UUID]:
    return list(dict.fromkeys(values))


def _chunks(values: Sequence[object]) -> list[list[object]]:
    return [
        list(values[index : index + CHUNK_SIZE])
        for index in range(0, len(values), CHUNK_SIZE)
    ]
