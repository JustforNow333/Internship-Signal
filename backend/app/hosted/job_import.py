"""Transactional, idempotent persistence for final watcher jobs."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .catalog import CompanyCatalog
from .database import HostedDatabase
from .job_mapper import (
    FinalJobsStructureError,
    MappedJob,
    MappedJobs,
    map_final_jobs,
)
from .match_service import reconcile_jobs
from .models import HostedJob, HostedJobImportAttempt, HostedJobImportRun
from .services import utc_now

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class JobImportError(RuntimeError):
    code = "job_import_failed"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.code)


class ImportAlreadyRunning(JobImportError):
    code = "source_import_already_running"


class FailedImportRetryRequired(JobImportError):
    code = "failed_source_requires_explicit_retry"


class InvalidImportSource(JobImportError):
    code = "invalid_import_source"


class InvalidFinalJobs(JobImportError):
    code = "invalid_final_jobs"


class JobUpsertFailed(JobImportError):
    code = "job_upsert_failed"


@dataclass(frozen=True)
class ImportCounters:
    jobs_received: int
    jobs_inserted: int
    jobs_updated: int
    jobs_unchanged: int
    jobs_skipped: int
    matches_created: int = 0


@dataclass(frozen=True)
class JobImportResult:
    run_id: uuid.UUID
    source_fingerprint: str
    outcome: str
    counters: ImportCounters
    skipped_reasons: dict[str, int]

    @property
    def already_imported(self) -> bool:
        return self.outcome == "already_imported"


@dataclass(frozen=True)
class _Claim:
    run_id: uuid.UUID
    attempt_id: uuid.UUID | None
    existing_result: JobImportResult | None = None


class JobImportService:
    def __init__(
        self,
        database: HostedDatabase,
        catalog: CompanyCatalog,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database = database
        self.catalog = catalog
        self.clock = clock

    def import_jobs(
        self,
        final_jobs: Sequence[Mapping[str, object]],
        *,
        source_fingerprint: str,
        source_identifier: str,
        source_type: str,
        retry_failed: bool = False,
    ) -> JobImportResult:
        fingerprint = _validated_fingerprint(source_fingerprint)
        identifier = _validated_source_identifier(source_identifier)
        import_source_type = _validated_source_type(source_type)
        observed_at = _observation_time(self.clock())
        claim = self._claim_source(
            fingerprint=fingerprint,
            identifier=identifier,
            source_type=import_source_type,
            observed_at=observed_at,
            retry_failed=retry_failed,
        )
        if claim.existing_result is not None:
            return claim.existing_result
        assert claim.attempt_id is not None

        mapped: MappedJobs | None = None
        try:
            mapped = map_final_jobs(final_jobs, self.catalog)
            inserted = 0
            updated = 0
            unchanged = 0
            with self.database.session_factory.begin() as db:
                run = db.scalar(
                    select(HostedJobImportRun)
                    .where(HostedJobImportRun.id == claim.run_id)
                    .with_for_update()
                )
                attempt = db.scalar(
                    select(HostedJobImportAttempt)
                    .where(HostedJobImportAttempt.id == claim.attempt_id)
                    .with_for_update()
                )
                if (
                    run is None
                    or attempt is None
                    or run.status != "running"
                    or attempt.status != "running"
                ):
                    raise ImportAlreadyRunning()
                affected_job_ids: list[uuid.UUID] = []
                for mapped_job in mapped.jobs:
                    outcome, job_id = self._upsert_job(db, mapped_job, observed_at)
                    if outcome == "inserted":
                        inserted += 1
                        affected_job_ids.append(job_id)
                    elif outcome == "updated":
                        updated += 1
                        affected_job_ids.append(job_id)
                    else:
                        unchanged += 1

                # Reconciliation shares the job-persistence transaction, so a
                # succeeded run can never report partially applied matches.
                reconciliation = reconcile_jobs(
                    db, affected_job_ids, now=observed_at
                )

                run.status = "succeeded"
                run.completed_at = observed_at
                run.jobs_received = mapped.jobs_received
                run.jobs_inserted = inserted
                run.jobs_updated = updated
                run.jobs_unchanged = unchanged
                run.jobs_skipped = mapped.jobs_skipped
                run.matches_created = reconciliation.created
                run.failure_summary = None
                run.updated_at = observed_at
                attempt.status = "succeeded"
                attempt.completed_at = observed_at
                attempt.failure_summary = None

            return JobImportResult(
                run_id=claim.run_id,
                source_fingerprint=fingerprint,
                outcome="imported",
                counters=ImportCounters(
                    jobs_received=mapped.jobs_received,
                    jobs_inserted=inserted,
                    jobs_updated=updated,
                    jobs_unchanged=unchanged,
                    jobs_skipped=mapped.jobs_skipped,
                    matches_created=reconciliation.created,
                ),
                skipped_reasons=mapped.skipped_reasons,
            )
        except Exception as exc:
            received = mapped.jobs_received if mapped is not None else _safe_length(final_jobs)
            skipped = mapped.jobs_skipped if mapped is not None else 0
            failure = _failure_code(exc)
            self._record_failure(
                claim,
                observed_at=observed_at,
                jobs_received=received,
                jobs_skipped=skipped,
                failure_summary=failure,
            )
            if isinstance(exc, FinalJobsStructureError):
                raise InvalidFinalJobs() from exc
            if isinstance(exc, JobImportError):
                raise
            raise JobUpsertFailed() from exc

    def _claim_source(
        self,
        *,
        fingerprint: str,
        identifier: str,
        source_type: str,
        observed_at: datetime,
        retry_failed: bool,
    ) -> _Claim:
        try:
            with self.database.session_factory.begin() as db:
                run = db.scalar(
                    select(HostedJobImportRun).where(
                        HostedJobImportRun.source_fingerprint == fingerprint
                    )
                )
                if run is not None:
                    if run.status == "succeeded":
                        return _Claim(
                            run_id=run.id,
                            attempt_id=None,
                            existing_result=_result_from_run(run),
                        )
                    if run.status == "running":
                        raise ImportAlreadyRunning()
                    if not retry_failed:
                        raise FailedImportRetryRequired()
                    run = db.scalar(
                        select(HostedJobImportRun)
                        .where(HostedJobImportRun.id == run.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    assert run is not None
                    return self._claim_existing(
                        db,
                        run,
                        observed_at=observed_at,
                        retry_failed=retry_failed,
                    )
                run = HostedJobImportRun(
                    source_fingerprint=fingerprint,
                    source_identifier=identifier,
                    source_type=source_type,
                    started_at=observed_at,
                    status="running",
                    jobs_received=0,
                    jobs_inserted=0,
                    jobs_updated=0,
                    jobs_unchanged=0,
                    jobs_skipped=0,
                    matches_created=0,
                    created_at=observed_at,
                    updated_at=observed_at,
                )
                db.add(run)
                db.flush()
                attempt = HostedJobImportAttempt(
                    import_run_id=run.id,
                    attempt_number=1,
                    started_at=observed_at,
                    status="running",
                )
                db.add(attempt)
                db.flush()
                return _Claim(run_id=run.id, attempt_id=attempt.id)
        except IntegrityError:
            # A concurrent creator won the unique-fingerprint claim. Re-read
            # its committed state rather than weakening the database guard.
            with self.database.session_factory() as db:
                run = db.scalar(
                    select(HostedJobImportRun).where(
                        HostedJobImportRun.source_fingerprint == fingerprint
                    )
                )
                if run is None:
                    raise JobUpsertFailed() from None
                if run.status == "succeeded":
                    return _Claim(
                        run_id=run.id,
                        attempt_id=None,
                        existing_result=_result_from_run(run),
                    )
                if run.status == "running":
                    raise ImportAlreadyRunning() from None
                raise FailedImportRetryRequired() from None

    def _claim_existing(
        self,
        db: Session,
        run: HostedJobImportRun,
        *,
        observed_at: datetime,
        retry_failed: bool,
    ) -> _Claim:
        if run.status == "succeeded":
            return _Claim(
                run_id=run.id,
                attempt_id=None,
                existing_result=_result_from_run(run),
            )
        if run.status == "running":
            raise ImportAlreadyRunning()
        if not retry_failed:
            raise FailedImportRetryRequired()
        attempt_number = (
            db.scalar(
                select(func.max(HostedJobImportAttempt.attempt_number)).where(
                    HostedJobImportAttempt.import_run_id == run.id
                )
            )
            or 0
        ) + 1
        run.status = "running"
        run.started_at = observed_at
        run.completed_at = None
        run.jobs_received = 0
        run.jobs_inserted = 0
        run.jobs_updated = 0
        run.jobs_unchanged = 0
        run.jobs_skipped = 0
        run.matches_created = 0
        run.failure_summary = None
        run.updated_at = observed_at
        attempt = HostedJobImportAttempt(
            import_run_id=run.id,
            attempt_number=attempt_number,
            started_at=observed_at,
            status="running",
        )
        db.add(attempt)
        db.flush()
        return _Claim(run_id=run.id, attempt_id=attempt.id)

    def _upsert_job(
        self,
        db: Session,
        mapped: MappedJob,
        observed_at: datetime,
    ) -> tuple[str, uuid.UUID]:
        values = mapped.business_values()
        inserted_id = db.scalar(
            postgresql_insert(HostedJob)
            .values(
                watcher_job_id=mapped.watcher_job_id,
                **values,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
                closed_at=None if mapped.is_open else observed_at,
                created_at=observed_at,
                updated_at=observed_at,
            )
            .on_conflict_do_nothing(index_elements=[HostedJob.watcher_job_id])
            .returning(HostedJob.id)
        )
        if inserted_id is not None:
            return "inserted", inserted_id
        existing = db.scalar(
            select(HostedJob)
            .where(HostedJob.watcher_job_id == mapped.watcher_job_id)
            .with_for_update()
        )
        if existing is None:
            raise JobUpsertFailed()
        if observed_at < existing.last_seen_at:
            raise InvalidImportSource("observation_before_last_seen")

        changed = any(getattr(existing, field) != value for field, value in values.items())
        was_open = existing.is_open
        for field, value in values.items():
            setattr(existing, field, value)
        if was_open and not mapped.is_open:
            if existing.closed_at is None:
                existing.closed_at = observed_at
        elif not was_open and mapped.is_open:
            existing.closed_at = None
        elif not mapped.is_open and existing.closed_at is None:
            existing.closed_at = observed_at
            changed = True
        existing.last_seen_at = observed_at
        existing.updated_at = observed_at
        return ("updated" if changed else "unchanged"), existing.id

    def _record_failure(
        self,
        claim: _Claim,
        *,
        observed_at: datetime,
        jobs_received: int,
        jobs_skipped: int,
        failure_summary: str,
    ) -> None:
        with self.database.session_factory.begin() as db:
            run = db.scalar(
                select(HostedJobImportRun)
                .where(HostedJobImportRun.id == claim.run_id)
                .with_for_update()
            )
            attempt = db.scalar(
                select(HostedJobImportAttempt)
                .where(HostedJobImportAttempt.id == claim.attempt_id)
                .with_for_update()
            )
            if run is None or attempt is None:
                return
            run.status = "failed"
            run.completed_at = observed_at
            run.jobs_received = max(0, jobs_received)
            run.jobs_inserted = 0
            run.jobs_updated = 0
            run.jobs_unchanged = 0
            run.jobs_skipped = max(0, jobs_skipped)
            run.matches_created = 0
            run.failure_summary = failure_summary[:400]
            run.updated_at = observed_at
            attempt.status = "failed"
            attempt.completed_at = observed_at
            attempt.failure_summary = failure_summary[:400]


def _validated_fingerprint(value: str) -> str:
    fingerprint = str(value or "").strip()
    if not _FINGERPRINT_RE.fullmatch(fingerprint):
        raise InvalidImportSource("invalid_source_fingerprint")
    return fingerprint


def _validated_source_identifier(value: str) -> str:
    identifier = str(value or "").strip()
    if (
        not identifier
        or len(identifier) > 200
        or _CONTROL_RE.search(identifier)
        or "/" in identifier
        or "\\" in identifier
        or re.match(r"^[A-Za-z]:", identifier)
    ):
        raise InvalidImportSource("invalid_source_identifier")
    return identifier


def _validated_source_type(value: str) -> str:
    source_type = str(value or "").strip()
    if not _SOURCE_TYPE_RE.fullmatch(source_type):
        raise InvalidImportSource("invalid_source_type")
    return source_type


def _observation_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidImportSource("invalid_observation_time")
    return value.astimezone(UTC)


def _result_from_run(run: HostedJobImportRun) -> JobImportResult:
    return JobImportResult(
        run_id=run.id,
        source_fingerprint=run.source_fingerprint,
        outcome="already_imported",
        counters=ImportCounters(
            jobs_received=run.jobs_received,
            jobs_inserted=run.jobs_inserted,
            jobs_updated=run.jobs_updated,
            jobs_unchanged=run.jobs_unchanged,
            jobs_skipped=run.jobs_skipped,
            matches_created=run.matches_created,
        ),
        skipped_reasons={},
    )


def _safe_length(value: object) -> int:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return len(value)
    return 0


def _failure_code(exc: Exception) -> str:
    if isinstance(exc, FinalJobsStructureError):
        return f"invalid_final_jobs:{exc.reason}"
    if isinstance(exc, JobImportError):
        return exc.code
    return "job_upsert_failed"
