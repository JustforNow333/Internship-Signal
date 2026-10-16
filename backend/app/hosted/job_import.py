"""Transactional, idempotent persistence for final watcher jobs."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from backend.app.dedupe import posting_specific_url_key, stable_requisition_key

from .catalog import CompanyCatalog
from .database import HostedDatabase
from .identity_shadow import (
    IdentityResolutionReport,
    IdentityShadowReport,
    ImportedJob,
    ResolutionPlan,
    SourceEvidence,
    plan_identity_resolution,
    record_import_identities,
    run_identity_shadow,
)
from .job_mapper import (
    FinalJobsStructureError,
    MappedJob,
    MappedJobs,
    map_final_jobs,
)
from .match_service import reconcile_jobs
from .models import HostedJob, HostedJobImportAttempt, HostedJobImportRun
from .notification_enqueue import enqueue_import_notifications
from .services import utc_now
from .timing import HostedTiming, stage

_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_SOURCE_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# Serializes enforced identity-resolving imports. The wait is bounded so an
# overlapping run fails cleanly instead of queueing indefinitely.
IMPORT_LOCK_KEY = "hosted-job-import"
_IMPORT_LOCK_TIMEOUT = "300s"
_LOCK_NOT_AVAILABLE = "55P03"


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


class ImportLockUnavailable(JobImportError):
    code = "hosted_import_lock_unavailable"


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
    # This run's newly inserted match rows and their new-opening subset, kept
    # in memory only for the post-import audit (``app.hosted.import_audit``).
    # They are excluded from equality and repr, and an already-imported
    # result carries none.
    created_match_ids: tuple[uuid.UUID, ...] = field(
        default=(), compare=False, repr=False
    )
    new_opening_match_ids: tuple[uuid.UUID, ...] = field(
        default=(), compare=False, repr=False
    )
    # Phase 2 shadow tracking outcome; ``None`` whenever it is disabled or
    # the import did not run. It never affects the import's own result.
    identity_shadow: IdentityShadowReport | None = field(
        default=None, compare=False, repr=False
    )
    # Phase 3 resolution outcome: enforced decisions, or shadow diagnostics
    # when only shadow tracking is on; ``None`` when shadow tracking is off.
    identity_resolution: IdentityResolutionReport | None = field(
        default=None, compare=False, repr=False
    )

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
        identity_shadow: bool = False,
        identity_resolution: bool = False,
    ) -> None:
        self.database = database
        self.catalog = catalog
        self.clock = clock
        # Phase 2 shadow tracking of stable identities and source evidence.
        # Off by default; see ``app.hosted.identity_shadow``.
        self.identity_shadow = identity_shadow
        # Phase 3 enforced resolution. Off by default, and never on without
        # shadow tracking, whose ownership records it relies on.
        self.identity_resolution = identity_resolution and identity_shadow

    def import_jobs(
        self,
        final_jobs: Sequence[Mapping[str, object]],
        *,
        source_fingerprint: str,
        source_identifier: str,
        source_type: str,
        retry_failed: bool = False,
        backfill: bool = False,
        timing: HostedTiming | None = None,
        source_evidence: SourceEvidence | None = None,
    ) -> JobImportResult:
        """Persist one final-job sequence, reconcile matches, and enqueue alerts.

        ``backfill`` marks an explicit, operator-requested controlled import,
        such as the first import after a new career stage becomes storable.
        Its matches still reconcile so stored state is accurate, but it creates
        no notification work, and the jobs it first stores are marked so their
        new ``first_seen_at`` never makes them look newly posted later. It is
        never inferred from dates. ``timing`` only records stage durations.

        When the service was built with ``identity_shadow``, a separate
        transaction after the import commits records identity and source
        evidence (``source_evidence``). It never changes the import, and its
        failure is only reported in ``JobImportResult.identity_shadow``.

        With ``identity_resolution`` as well, the import transaction first
        takes the import advisory lock, then updates the existing job a row's
        strong identities unambiguously single out instead of the legacy
        ``watcher_job_id`` target, and writes identity ownership before it
        commits. Without it, the shadow pass only reports what would have
        resolved.
        """

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
        shadow_jobs: list[ImportedJob] | None = [] if self.identity_shadow else None
        resolution: ResolutionPlan | None = None
        try:
            with stage(timing, "job_mapping"):
                mapped = map_final_jobs(final_jobs, self.catalog)
            inserted = 0
            updated = 0
            unchanged = 0
            with self.database.session_factory.begin() as db:
                if self.identity_resolution:
                    _acquire_import_lock(db)
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
                with stage(timing, "job_upsert"):
                    if self.identity_resolution:
                        resolution = plan_identity_resolution(
                            db, final_jobs, mapped.jobs
                        )
                    for index, mapped_job in enumerate(mapped.jobs):
                        target_id = (
                            resolution.targets[index] if resolution is not None else None
                        )
                        if target_id is None:
                            outcome, job_id = self._upsert_job(
                                db, mapped_job, observed_at, backfill=backfill
                            )
                        else:
                            outcome, job_id = self._update_resolved(
                                db, target_id, mapped_job, observed_at
                            )
                        if shadow_jobs is not None:
                            shadow_jobs.append(
                                ImportedJob(mapped_job, job_id, outcome)
                            )
                        if outcome == "inserted":
                            inserted += 1
                            affected_job_ids.append(job_id)
                        elif outcome == "updated":
                            updated += 1
                            affected_job_ids.append(job_id)
                        else:
                            unchanged += 1
                    if resolution is not None and shadow_jobs is not None:
                        # The ownership that justified a resolution commits or
                        # rolls back with it.
                        record_import_identities(
                            db,
                            final_jobs=final_jobs,
                            imported=shadow_jobs,
                            evidence=source_evidence,
                            observed_at=observed_at,
                        )

                # Reconciliation shares the job-persistence transaction, so a
                # succeeded run can never report partially applied matches.
                with stage(timing, "match_reconciliation"):
                    reconciliation = reconcile_jobs(
                        db, affected_job_ids, now=observed_at
                    )
                # Only genuinely new openings notify. Recent-opening catch-up
                # and every match an explicit backfill creates stay silent.
                if not backfill:
                    with stage(timing, "notification_enqueue"):
                        enqueue_import_notifications(
                            db,
                            reconciliation.new_opening_match_ids,
                            import_run_id=run.id,
                            now=observed_at,
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

            result = JobImportResult(
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
                created_match_ids=reconciliation.created_match_ids,
                new_opening_match_ids=reconciliation.new_opening_match_ids,
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

        # Outside the import's failure handling on purpose: the import has
        # committed, and shadow tracking reports its own failures and never
        # raises, so it can neither fail nor mark failed a committed run.
        if shadow_jobs is not None:
            with stage(timing, "identity_shadow"):
                report = run_identity_shadow(
                    self.database,
                    import_run_id=claim.run_id,
                    observed_at=observed_at,
                    final_jobs=final_jobs,
                    imported=shadow_jobs,
                    evidence=source_evidence,
                    resolution_diagnostics=resolution is None,
                )
            result = replace(
                result,
                identity_shadow=report,
                identity_resolution=(
                    resolution.report if resolution is not None else report.resolution
                ),
            )
        return result

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
        *,
        backfill: bool,
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
                first_seen_in_backfill=backfill,
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
        return self._update_existing(db, existing, mapped, observed_at)

    def _update_resolved(
        self,
        db: Session,
        target_id: uuid.UUID,
        mapped: MappedJob,
        observed_at: datetime,
    ) -> tuple[str, uuid.UUID]:
        """Apply a row to the existing job its identities resolved to.

        The target keeps its ``id`` and ``watcher_job_id``; every ordinary
        update, source-precedence, and open/close rule applies unchanged.
        """

        existing = db.scalar(
            select(HostedJob).where(HostedJob.id == target_id).with_for_update()
        )
        if existing is None:
            raise JobUpsertFailed()
        return self._update_existing(db, existing, mapped, observed_at)

    def _update_existing(
        self,
        db: Session,
        existing: HostedJob,
        mapped: MappedJob,
        observed_at: datetime,
    ) -> tuple[str, uuid.UUID]:
        values = mapped.business_values()
        if observed_at < existing.last_seen_at:
            raise InvalidImportSource("observation_before_last_seen")
        if _is_backstop_over_direct(existing, mapped):
            return self._observe_backstop_of_direct(db, existing, mapped, observed_at)

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

    @staticmethod
    def _observe_backstop_of_direct(
        db: Session,
        existing: HostedJob,
        mapped: MappedJob,
        observed_at: datetime,
    ) -> tuple[str, uuid.UUID]:
        """Record a backstop sighting without touching direct-source content.

        Dedupe keeps the direct row whenever both are collected, so a backstop
        row arrives alone only when the direct source failed or no longer
        lists the posting. It confirms the posting is still listed, but it
        never rewrites or reopens a direct-source record. It closes one only
        when the listing explicitly reports the posting closed and is verified
        to be the same posting: the two may otherwise share only the legacy
        company/title/city ID, and closing cancels pending alerts for good.
        """

        changed = False
        if (
            existing.is_open
            and mapped.explicitly_closed
            and _verified_same_posting(db, existing, mapped)
        ):
            existing.is_open = False
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


def _acquire_import_lock(db: Session) -> None:
    """Take the import advisory lock as the transaction's first statements.

    Waits at most ``_IMPORT_LOCK_TIMEOUT``, then restores the session's lock
    timeout so the rest of the import behaves exactly as before.
    """

    previous = db.scalar(text("SELECT current_setting('lock_timeout')"))
    db.execute(
        text("SELECT set_config('lock_timeout', :value, true)"),
        {"value": _IMPORT_LOCK_TIMEOUT},
    )
    try:
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": IMPORT_LOCK_KEY},
        )
    except OperationalError as exc:
        if getattr(exc.orig, "sqlstate", None) == _LOCK_NOT_AVAILABLE:
            raise ImportLockUnavailable() from None
        raise
    db.execute(
        text("SELECT set_config('lock_timeout', :value, true)"), {"value": previous}
    )


def _is_backstop_over_direct(existing: HostedJob, mapped: MappedJob) -> bool:
    stored = existing.source_metadata if isinstance(existing.source_metadata, Mapping) else {}
    return (
        stored.get("source_type") == "direct"
        and mapped.source_metadata.get("source_type") == "backstop"
    )


def _verified_same_posting(db: Session, existing: HostedJob, mapped: MappedJob) -> bool:
    """Whether a backstop row provably names the stored job's posting.

    Only posting-specific evidence counts: equal source-scoped requisition
    identities parsed from both URLs, or the same posting-specific URL. Any
    missing, generic, conflicting, or shared URL leaves the identity unproven.
    """

    stored_url = existing.application_url or ""
    incoming_url = mapped.application_url or ""
    if not stored_url or not incoming_url:
        return False
    stored_requisition = stable_requisition_key({"source_url": stored_url})
    incoming_requisition = stable_requisition_key({"source_url": incoming_url})
    if stored_requisition and incoming_requisition:
        verified = stored_requisition == incoming_requisition
    else:
        stored_posting = posting_specific_url_key({"source_url": stored_url})
        verified = bool(stored_posting) and stored_posting == posting_specific_url_key(
            {"source_url": incoming_url}
        )
    if not verified:
        return False
    # A URL that another stored job also uses cannot single out this posting.
    shared = db.scalar(
        select(func.count())
        .select_from(HostedJob)
        .where(
            HostedJob.application_url == existing.application_url,
            HostedJob.id != existing.id,
        )
    )
    return not shared


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
