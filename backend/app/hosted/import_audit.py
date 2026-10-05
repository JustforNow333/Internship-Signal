"""Bounded, read-only integrity audit after a committed hosted import.

The import CLIs print two ``HOSTED-AUDIT`` lines after a successful import so
an operator can see, without hand-written SQL, why new matches did or did not
produce notification work:

* ``kind=summary`` - how this run's new matches split between notifying new
  openings and intentionally silent catch-up/backfill admissions.
* ``kind=integrity`` - deterministic anomalies (a notifiable new opening with
  no notification item, inconsistent item/batch relationships, pending work
  under a finished batch) plus two clearly heuristic signals: late
  discoveries and suspicious cross-source duplicate representations.

The audit runs strictly after the import transaction has committed, in its own
read-only transaction that is always rolled back. It never creates or changes
rows, never sends mail, never alters the import result or exit status, and
never feeds back into matching, eligibility, deduplication, or snapshots. It
prints aggregate counts only: no user, company, job, URL, or token values.

Duplicate detection is a heuristic. A non-zero count means "worth a look", not
"proven duplicate".
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select, text

from app.dedupe import _fallback_title, _squash

from .database import HostedDatabase
from .job_import import JobImportResult
from .models import (
    HostedJob,
    HostedNotificationBatch,
    HostedNotificationItem,
    User,
    UserJobMatch,
    UserPreference,
)
from .notification_enqueue import DELIVERY_FREQUENCIES

PREFIX = "HOSTED-AUDIT"

# A job first stored more than this many days after its posting date counts as
# a late discovery: it was collected long after it was posted.
LATE_DISCOVERY_DAYS = 14
# Per-statement ceiling so the audit can never stall a scheduled run.
STATEMENT_TIMEOUT_MS = 5000
# Above this many new matches the per-match checks are skipped, not truncated,
# so a reported count is always complete.
MAX_AUDITED_MATCHES = 5000
# Upper bound on open same-company rows loaded for duplicate comparison.
MAX_DUPLICATE_SCAN_ROWS = 20000
CHUNK_SIZE = 500

# A finished batch never legitimately keeps a pending item: a sent batch marks
# its delivered items sent and a cancelled batch cancels or re-homes them.
CLOSED_BATCH_STATUSES = ("sent", "cancelled")
# Failed and uncertain deliveries intentionally leave their items pending.
FAILED_BATCH_STATUSES = ("permanent_failed", "uncertain")


@dataclass
class ImportAudit:
    backfill: bool = False
    match_checks: str = "ok"
    duplicate_scan: str = "ok"
    matches_created: int = 0
    new_opening_matches: int = 0
    notification_eligible_matches: int = 0
    notification_items_created: int = 0
    notification_items_present: int = 0
    silent_catch_up_matches: int = 0
    backfill_silent_matches: int = 0
    new_opening_suppressed: int = 0
    backstop_matches: int = 0
    new_opening_missing_notification: int = 0
    silent_match_with_notification: int = 0
    late_discoveries: int = 0
    suspicious_cross_source_duplicates: int = 0
    duplicate_requisition_asymmetric: int = 0
    suspicious_duplicate_matches: int = 0
    batch_user_mismatch: int = 0
    batch_run_mismatch: int = 0
    pending_items_closed_batch: int = 0
    pending_items_failed_batch: int = 0

    @property
    def notification_integrity_anomalies(self) -> int:
        return (
            self.silent_match_with_notification
            + self.batch_user_mismatch
            + self.batch_run_mismatch
            + self.pending_items_closed_batch
        )

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.new_opening_missing_notification
            or self.notification_integrity_anomalies
        )


@dataclass(frozen=True)
class _JobView:
    id: uuid.UUID
    company_id: str
    title_key: str
    location_tokens: tuple[str, ...]
    source_type: str
    has_requisition: bool


def run_import_audit(
    database_url: str,
    result: JobImportResult,
    *,
    backfill: bool = False,
) -> list[str]:
    """CLI entry point: audit through a short-lived connection. Never raises."""

    if result.already_imported:
        return [_skipped_line("already_imported")]
    database: HostedDatabase | None = None
    try:
        database = HostedDatabase(database_url)
        return import_audit_lines(database, result, backfill=backfill)
    except Exception:  # noqa: BLE001 - telemetry must never fail an import
        return [_skipped_line("audit_unavailable")]
    finally:
        if database is not None:
            with suppress(Exception):
                database.dispose()


def import_audit_lines(
    database: object,
    result: JobImportResult,
    *,
    backfill: bool = False,
    clock: Callable[[], float] = time.perf_counter,
) -> list[str]:
    """Return the bounded ``HOSTED-AUDIT`` lines for one import. Never raises.

    ``database`` only needs a ``session_factory``.
    """

    try:
        started = clock()
        if result.already_imported:
            return [_skipped_line("already_imported")]
        audit = collect_import_audit(database, result, backfill=backfill)
        elapsed_ms = max(0, int((clock() - started) * 1000))
        return format_audit_lines(audit, elapsed_ms=elapsed_ms)
    except Exception:  # noqa: BLE001 - telemetry must never fail an import
        return [_skipped_line("audit_unavailable")]


def collect_import_audit(
    database: object,
    result: JobImportResult,
    *,
    backfill: bool = False,
) -> ImportAudit:
    audit = ImportAudit(backfill=backfill)
    created = list(dict.fromkeys(result.created_match_ids))
    new_openings = set() if backfill else set(result.new_opening_match_ids)
    audit.matches_created = len(created)
    with database.session_factory() as db:
        try:
            if db.get_bind().dialect.name == "postgresql":
                # Must be the first statement of the transaction.
                db.execute(text("SET TRANSACTION READ ONLY"))
                db.execute(
                    text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")
                )
            if len(created) > MAX_AUDITED_MATCHES:
                audit.match_checks = "skipped_over_budget"
                audit.duplicate_scan = "skipped_over_budget"
            elif created:
                jobs = _audit_matches(
                    db,
                    audit,
                    created,
                    new_openings=new_openings,
                    run_id=result.run_id,
                )
                _audit_duplicates(db, audit, jobs)
            _audit_pending_items(db, audit)
        finally:
            db.rollback()
    return audit


def format_audit_lines(audit: ImportAudit, *, elapsed_ms: int = 0) -> list[str]:
    summary = {
        "mode": "backfill" if audit.backfill else "standard",
        "match_checks": audit.match_checks,
        "matches_created": audit.matches_created,
        "new_opening_matches": audit.new_opening_matches,
        "notification_eligible_matches": audit.notification_eligible_matches,
        "notification_items_created": audit.notification_items_created,
        "notification_items_present": audit.notification_items_present,
        "silent_catch_up_matches": audit.silent_catch_up_matches,
        "backfill_silent_matches": audit.backfill_silent_matches,
        "new_opening_suppressed": audit.new_opening_suppressed,
        "backstop_matches": audit.backstop_matches,
        "elapsed_ms": elapsed_ms,
    }
    integrity = {
        "status": "attention" if audit.needs_attention else "ok",
        "new_opening_missing_notification": audit.new_opening_missing_notification,
        "notification_integrity_anomalies": audit.notification_integrity_anomalies,
        "silent_match_with_notification": audit.silent_match_with_notification,
        "batch_user_mismatch": audit.batch_user_mismatch,
        "batch_run_mismatch": audit.batch_run_mismatch,
        "pending_items_closed_batch": audit.pending_items_closed_batch,
        "pending_items_failed_batch": audit.pending_items_failed_batch,
        "late_discoveries": audit.late_discoveries,
        "late_discovery_days": LATE_DISCOVERY_DAYS,
        "duplicate_scan": audit.duplicate_scan,
        "suspicious_cross_source_duplicates": audit.suspicious_cross_source_duplicates,
        "duplicate_requisition_asymmetric": audit.duplicate_requisition_asymmetric,
        "suspicious_duplicate_matches": audit.suspicious_duplicate_matches,
    }
    return [
        f"{PREFIX} kind=summary {_fields(summary)}",
        f"{PREFIX} kind=integrity {_fields(integrity)}",
    ]


def _audit_matches(
    db,
    audit: ImportAudit,
    created: Sequence[uuid.UUID],
    *,
    new_openings: set[uuid.UUID],
    run_id: uuid.UUID,
) -> dict[uuid.UUID, tuple[_JobView, list[uuid.UUID]]]:
    """Classify this run's new matches; return their open jobs for dedupe."""

    jobs: dict[uuid.UUID, tuple[_JobView, list[uuid.UUID]]] = {}
    late_jobs: set[uuid.UUID] = set()
    for chunk in _chunks(created):
        items = {
            row.user_job_match_id: row
            for row in db.execute(
                select(
                    HostedNotificationItem.user_job_match_id,
                    HostedNotificationItem.source_import_run_id,
                    HostedNotificationBatch.user_id.label("batch_user_id"),
                    HostedNotificationBatch.frequency,
                    HostedNotificationBatch.source_import_run_id.label(
                        "batch_run_id"
                    ),
                )
                .join(
                    HostedNotificationBatch,
                    HostedNotificationBatch.id == HostedNotificationItem.batch_id,
                )
                .where(HostedNotificationItem.user_job_match_id.in_(chunk))
            )
        }
        rows = db.execute(
            select(
                UserJobMatch.id,
                UserJobMatch.user_id,
                UserJobMatch.no_longer_matches_at,
                UserJobMatch.dismissed_at,
                HostedJob.id.label("job_id"),
                HostedJob.company_id,
                HostedJob.title,
                HostedJob.location,
                HostedJob.posting_date,
                HostedJob.first_seen_at,
                HostedJob.is_open,
                HostedJob.source_metadata,
                User.is_active,
                User.email_verified_at,
                UserPreference.globally_paused,
                UserPreference.alert_frequency,
            )
            .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
            .outerjoin(User, User.id == UserJobMatch.user_id)
            .outerjoin(UserPreference, UserPreference.user_id == UserJobMatch.user_id)
            .where(UserJobMatch.id.in_(chunk))
        )
        for row in rows:
            item = items.get(row.id)
            if item is not None:
                audit.notification_items_present += 1
                if item.source_import_run_id == run_id:
                    audit.notification_items_created += 1
                if item.batch_user_id != row.user_id:
                    audit.batch_user_mismatch += 1
                if (
                    item.frequency == "as_detected"
                    and item.batch_run_id != item.source_import_run_id
                ):
                    audit.batch_run_mismatch += 1

            if row.id in new_openings:
                audit.new_opening_matches += 1
                if _notification_eligible(row):
                    audit.notification_eligible_matches += 1
                    if item is None:
                        audit.new_opening_missing_notification += 1
                else:
                    audit.new_opening_suppressed += 1
            else:
                if audit.backfill:
                    audit.backfill_silent_matches += 1
                else:
                    audit.silent_catch_up_matches += 1
                if item is not None:
                    audit.silent_match_with_notification += 1

            metadata = row.source_metadata if isinstance(row.source_metadata, dict) else {}
            if metadata.get("source_type") == "backstop":
                audit.backstop_matches += 1
            if _late_discovery(row.posting_date, row.first_seen_at):
                late_jobs.add(row.job_id)
            if row.is_open:
                entry = jobs.get(row.job_id)
                if entry is None:
                    entry = jobs[row.job_id] = (
                        _job_view(
                            row.job_id,
                            row.company_id,
                            row.title,
                            row.location,
                            metadata,
                        ),
                        [],
                    )
                entry[1].append(row.id)
    audit.late_discoveries = len(late_jobs)
    return jobs


def _audit_duplicates(
    db,
    audit: ImportAudit,
    jobs: dict[uuid.UUID, tuple[_JobView, list[uuid.UUID]]],
) -> None:
    """Count suspicious same-posting representations from different sources.

    Two open rows are suspicious only when they share a company, the dedupe
    fallback title normalization, and a compatible location, *and* their
    provenance differs: one direct and one backstop, or exactly one carrying a
    source requisition ID. Rows that both carry requisition IDs from the same
    source type are treated as distinct postings, never flagged.
    """

    if not jobs:
        return
    company_ids = sorted({view.company_id for view, _ in jobs.values()})
    others: dict[tuple[str, str], list[_JobView]] = defaultdict(list)
    loaded = 0
    for chunk in _chunks(company_ids):
        rows = db.execute(
            select(
                HostedJob.id,
                HostedJob.company_id,
                HostedJob.title,
                HostedJob.location,
                HostedJob.source_metadata,
            )
            .where(HostedJob.company_id.in_(chunk), HostedJob.is_open.is_(True))
            .limit(MAX_DUPLICATE_SCAN_ROWS - loaded + 1)
        ).all()
        loaded += len(rows)
        if loaded > MAX_DUPLICATE_SCAN_ROWS:
            audit.duplicate_scan = "skipped_over_budget"
            return
        for row in rows:
            metadata = row.source_metadata if isinstance(row.source_metadata, dict) else {}
            view = _job_view(row.id, row.company_id, row.title, row.location, metadata)
            if view.title_key:
                others[(view.company_id, view.title_key)].append(view)

    pairs: set[frozenset[uuid.UUID]] = set()
    asymmetric: set[frozenset[uuid.UUID]] = set()
    flagged_matches: set[uuid.UUID] = set()
    for view, match_ids in jobs.values():
        if not view.title_key:
            continue
        for other in others.get((view.company_id, view.title_key), ()):
            if other.id == view.id or not _locations_compatible(
                view.location_tokens, other.location_tokens
            ):
                continue
            requisition_asymmetric = view.has_requisition != other.has_requisition
            if view.source_type == other.source_type and not requisition_asymmetric:
                continue
            pair = frozenset((view.id, other.id))
            pairs.add(pair)
            if requisition_asymmetric:
                asymmetric.add(pair)
            flagged_matches.update(match_ids)
    audit.suspicious_cross_source_duplicates = len(pairs)
    audit.duplicate_requisition_asymmetric = len(asymmetric)
    audit.suspicious_duplicate_matches = len(flagged_matches)


def _audit_pending_items(db, audit: ImportAudit) -> None:
    """Count pending items left under a finished or failed batch."""

    rows = db.execute(
        select(HostedNotificationBatch.status, func.count())
        .select_from(HostedNotificationItem)
        .join(
            HostedNotificationBatch,
            HostedNotificationBatch.id == HostedNotificationItem.batch_id,
        )
        .where(
            HostedNotificationItem.status == "pending",
            HostedNotificationBatch.status.in_(
                CLOSED_BATCH_STATUSES + FAILED_BATCH_STATUSES
            ),
        )
        .group_by(HostedNotificationBatch.status)
    )
    for status, count in rows:
        if status in CLOSED_BATCH_STATUSES:
            audit.pending_items_closed_batch += int(count)
        else:
            audit.pending_items_failed_batch += int(count)


def _notification_eligible(row) -> bool:
    """Mirror of ``enqueue_import_notifications``' recipient filter (read only)."""

    return bool(
        row.is_active
        and row.email_verified_at is not None
        and row.globally_paused is False
        and row.alert_frequency in DELIVERY_FREQUENCIES
        and row.no_longer_matches_at is None
        and row.dismissed_at is None
        and row.is_open
    )


def _late_discovery(posting_date: date | None, first_seen_at: datetime | None) -> bool:
    if posting_date is None or first_seen_at is None:
        return False
    return (first_seen_at.date() - posting_date).days > LATE_DISCOVERY_DAYS


def _job_view(
    job_id: uuid.UUID,
    company_id: str,
    title: str,
    location: str,
    metadata: dict,
) -> _JobView:
    return _JobView(
        id=job_id,
        company_id=company_id,
        title_key=_fallback_title(title or ""),
        location_tokens=tuple(_squash(location or "").split()),
        source_type=str(metadata.get("source_type") or "unknown"),
        has_requisition=bool(metadata.get("requisition_id")),
    )


def _locations_compatible(first: tuple[str, ...], second: tuple[str, ...]) -> bool:
    """Equal, either unknown, or one a leading-token refinement of the other.

    "New York, NY" and "New York, NY, United States" are compatible;
    "New York, NY" and "Phoenix, AZ" are not.
    """

    if not first or not second:
        return True
    shorter, longer = sorted((first, second), key=len)
    return longer[: len(shorter)] == shorter


def _skipped_line(reason: str) -> str:
    return f"{PREFIX} kind=summary status=skipped reason={reason}"


def _fields(values: dict[str, object]) -> str:
    return " ".join(f"{key}={value}" for key, value in values.items())


def _chunks(values: Sequence[object] | Iterable[object]) -> list[list[object]]:
    items = list(values)
    return [items[index : index + CHUNK_SIZE] for index in range(0, len(items), CHUNK_SIZE)]
