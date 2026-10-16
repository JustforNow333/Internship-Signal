"""Shadow-mode tracking of stable posting identities and source evidence.

Phase 2 of the H1/H2 correctness plan. When ``HOSTED_IDENTITY_SHADOW_ENABLED``
is set to a true value, every successful hosted import also records, in a
separate transaction after the import has committed:

* the strong identity keys of each imported job in ``hosted_job_identities``:
  source-scoped requisition keys (a native requisition ID and a URL-derived
  one are both kept when they disagree), the posting-specific canonical URL,
  and the listing ID of a GitHub/Simplify listing. Company, title, and
  location are never an identity;
* which source owns each job (``hosted_jobs.source_key``) and when a direct
  source last listed it (``hosted_jobs.last_direct_seen_at``);
* what every source attempt reported about itself in
  ``hosted_source_observations``. Missing completeness metadata is stored as
  unknown, never as complete;
* which job the recorded identities would have selected, compared with the
  job the legacy ``watcher_job_id`` path actually selected. The comparison
  is reported as counts only and is never applied.

Nothing here changes a job's ``watcher_job_id``, business values, open or
closed state, ``updated_at``, matches, or notification work, and ambiguous
evidence never merges, reassigns, closes, or deletes anything: an identity
claimed by two jobs in one import is not written, and an identity already
owned by another job keeps its owner.

The flag is off unless explicitly enabled. When it is off, the import path
does not call into this module and never reads or writes the new tables or
columns. When it is on, shadow tracking cannot fail or partially commit the
import: the import has already committed, the shadow transaction is atomic,
and any error is reported as a bounded status line and swallowed. Shadow work
never waits for a job row that an import has locked; it skips those jobs.

Phase 3 adds guarded identity resolution (``decide_resolution``). It is
enforced only when ``HOSTED_IDENTITY_RESOLUTION_ENABLED`` is explicitly true
*and* shadow tracking is on: the import then updates the one existing job its
strong identities single out, instead of inserting another copy, and writes
identity ownership in the same transaction. With shadow tracking on and
resolution off, the shadow pass only reports, read-only, what the same guards
would have decided. Either way the result is one bounded
``HOSTED-IDENTITY-RESOLVE`` line of counts.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, literal_column, text, tuple_
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.orm import Session

from backend.app.dedupe import (
    non_specific_posting_urls,
    posting_specific_url_key,
    stable_requisition_key,
)
from watcher.collection_snapshot import CollectionBatch
from watcher.health.state import direct_health_key, github_feed_health_key
from watcher.source_health import (
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    SourceAttempt,
)

from .database import HostedDatabase
from .job_mapper import MappedJob
from .models import HostedJobIdentity, HostedSourceObservation

LOGGER = logging.getLogger(__name__)

SHADOW_FLAG_ENV = "HOSTED_IDENTITY_SHADOW_ENABLED"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

KIND_REQUISITION = "requisition"
KIND_URL = "url"
KIND_LISTING = "listing"
MAX_IDENTITY_KEY_LENGTH = 4096
_CHUNK = 500
_LOCK_TIMEOUT = "5s"
_STATEMENT_TIMEOUT = "120s"
_SOURCE_KINDS = frozenset({SOURCE_KIND_DIRECT, SOURCE_KIND_GITHUB_FEED})
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_LISTING_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_ERROR_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")

# Every counter a successful report carries, in output order, one line each.
REPORT_LINES = (
    (
        "HOSTED-IDENTITY-SHADOW status=ok",
        (
            "jobs",
            "agree",
            "disagree",
            "conflict",
            "new_legacy_inserted",
            "new_legacy_existing",
            "no_strong_identity",
            "jobs_skipped_locked",
        ),
    ),
    (
        "HOSTED-IDENTITY-SHADOW-IDENTITIES",
        (
            "identities_derived",
            "requisition",
            "url",
            "listing",
            "native_url_disagreements",
            "rejected",
            "batch_ambiguous",
            "inserted",
            "refreshed",
            "ownership_conflicts",
        ),
    ),
    (
        "HOSTED-IDENTITY-SHADOW-SOURCES",
        (
            "attempts",
            "observations_written",
            "observations_existing",
            "direct",
            "github_feed",
            "succeeded",
            "failed",
            "not_attempted",
            "complete",
            "complete_unknown",
            "incomplete",
            "truncated",
            "degraded",
            "inconsistent",
            "duplicate_source_keys",
            "invalid_attempts",
            "jobs_source_key_set",
            "jobs_source_unmatched",
            "last_direct_seen_updated",
        ),
    ),
)
COUNTER_NAMES = tuple(name for _prefix, names in REPORT_LINES for name in names)

RESOLUTION_FLAG_ENV = "HOSTED_IDENTITY_RESOLUTION_ENABLED"
RESOLVE = "resolve"
# Why a row whose identities name another job still takes the legacy path,
# in the order the guards are checked. Each is reported as ``reject_<name>``.
RESOLUTION_REASONS = (
    "conflict",
    "batch_ambiguous",
    "weak_identity",
    "row_native_url_disagreement",
    "primary_not_owned",
    "target_missing",
    "target_superseded",
    "target_closed",
    "target_ambiguous",
    "target_requisition_mismatch",
    "target_requisition_unverified",
    "target_contention",
)
RESOLVE_COUNTER_NAMES = (
    "rows",
    "would_resolve",
    "resolved",
    "agree",
    "unowned",
    "no_strong_identity",
    "fallback",
    *(f"reject_{reason}" for reason in RESOLUTION_REASONS),
    "legacy_requisition_mismatch",
)


def _flag_enabled(name: str) -> bool:
    raw = os.getenv(name)
    return raw is not None and raw.strip().casefold() in _TRUE_VALUES


def identity_shadow_enabled() -> bool:
    """Whether Phase 2 shadow tracking is explicitly enabled.

    Only an explicit true value enables it. Unset, empty, false, and any
    unrecognized value all leave it off, so a typo can never enable writes.
    """

    return _flag_enabled(SHADOW_FLAG_ENV)


def identity_resolution_enabled() -> bool:
    """Whether Phase 3 resolution is explicitly enabled and enforceable.

    Parsed like the shadow flag, and never true without shadow tracking:
    resolution relies on the ownership shadow tracking records.
    """

    return _flag_enabled(RESOLUTION_FLAG_ENV) and identity_shadow_enabled()


@dataclass(frozen=True)
class SourceEvidence:
    """Per-source attempt metadata that travelled with the collection."""

    attempts: tuple[SourceAttempt, ...]
    config_fingerprint: str | None = None

    @classmethod
    def from_batch(cls, batch: CollectionBatch) -> SourceEvidence:
        fingerprint = str(batch.collection_config_fingerprint or "")
        return cls(
            attempts=tuple(batch.source_attempts),
            config_fingerprint=(
                fingerprint if _FINGERPRINT_RE.fullmatch(fingerprint) else None
            ),
        )


@dataclass(frozen=True)
class ImportedJob:
    """One job the committed import stored, as the legacy path selected it."""

    mapped: MappedJob
    job_id: uuid.UUID
    outcome: str


@dataclass(frozen=True)
class JobIdentity:
    kind: str
    key: str

    @property
    def digest(self) -> str:
        return identity_digest(self.key)


@dataclass(frozen=True)
class DerivedIdentities:
    identities: tuple[JobIdentity, ...]
    native_url_disagreement: bool = False
    rejected: int = 0


@dataclass(frozen=True)
class IdentityShadowReport:
    """Bounded, privacy-safe outcome of one shadow pass: counts only."""

    status: str
    counters: Mapping[str, int] = field(default_factory=dict)
    error: str | None = None
    failed_stage: str | None = None
    # Read-only Phase 3 diagnostics, when this pass was asked for them. They
    # are reported on their own line, never in ``lines()``.
    resolution: IdentityResolutionReport | None = field(default=None, compare=False)

    def lines(self) -> list[str]:
        if self.status != "ok":
            return [
                "HOSTED-IDENTITY-SHADOW "
                f"status={self.status} "
                f"stage={self.failed_stage or 'unknown'} "
                f"error={self.error or 'unknown'}"
            ]
        return [
            " ".join(
                (prefix, *(f"{name}={int(self.counters.get(name, 0))}" for name in names))
            )
            for prefix, names in REPORT_LINES
        ]


@dataclass(frozen=True)
class IdentityResolutionReport:
    """Bounded Phase 3 outcome: what the guards decided, as counts only.

    ``mode`` is ``shadow`` when the decisions were only computed and
    ``enforced`` when the import applied them.
    """

    mode: str
    status: str = "ok"
    counters: Mapping[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        prefix = f"HOSTED-IDENTITY-RESOLVE mode={self.mode} status={self.status}"
        if self.status != "ok":
            return [prefix]
        return [
            " ".join(
                (
                    prefix,
                    *(
                        f"{name}={int(self.counters.get(name, 0))}"
                        for name in RESOLVE_COUNTER_NAMES
                    ),
                )
            )
        ]


@dataclass(frozen=True)
class ResolutionRow:
    """One incoming row as the resolver sees it.

    ``identities`` keep ``derive_identities`` order, strongest first, so the
    first is the row's primary identity. ``legacy_target`` is the job the
    legacy ``watcher_job_id`` path would update, or ``None`` if it inserts.
    """

    company_id: str
    identities: tuple[JobIdentity, ...]
    legacy_target: uuid.UUID | None
    rejected: int = 0
    backstop: bool = False

    @property
    def claims(self) -> tuple[tuple[str, str, str], ...]:
        return tuple(
            (self.company_id, identity.kind, identity.digest)
            for identity in self.identities
        )


@dataclass(frozen=True)
class ResolutionTarget:
    """The stored state of a job some row's identities single out."""

    job_id: uuid.UUID
    is_open: bool
    superseded: bool
    direct: bool
    requisitions: tuple[str, ...]


@dataclass(frozen=True)
class ResolutionDecision:
    outcome: str
    target_id: uuid.UUID | None = None

    @property
    def resolves(self) -> bool:
        return self.outcome == RESOLVE


def decide_resolution(
    row: ResolutionRow,
    *,
    owners: Mapping[tuple[str, str, str], uuid.UUID],
    targets: Mapping[uuid.UUID, ResolutionTarget],
    batch_ambiguous: frozenset[tuple[str, str, str]] = frozenset(),
) -> ResolutionDecision:
    """Decide whether one row resolves to an existing job, and why not.

    Pure: ``owners`` maps ``(company_id, kind, digest)`` to the job that owned
    that identity before this import, and ``targets`` holds the stored state
    of candidate jobs. Only ``resolve`` redirects the row; every other outcome
    keeps the legacy ``watcher_job_id`` path. Batch-level contention is
    checked by ``decide_resolutions``.
    """

    if not row.identities:
        return ResolutionDecision("no_strong_identity")
    claims = row.claims
    selected = {owners[claim] for claim in claims if claim in owners}
    if not selected:
        return ResolutionDecision("unowned")
    if len(selected) > 1:
        return ResolutionDecision("conflict")
    [owner] = selected
    if owner == row.legacy_target:
        return ResolutionDecision("agree")
    reason = _refusal(row, claims, owner, owners, targets, batch_ambiguous)
    if reason is not None:
        return ResolutionDecision(reason)
    return ResolutionDecision(RESOLVE, owner)


def _refusal(
    row: ResolutionRow,
    claims: tuple[tuple[str, str, str], ...],
    owner: uuid.UUID,
    owners: Mapping[tuple[str, str, str], uuid.UUID],
    targets: Mapping[uuid.UUID, ResolutionTarget],
    batch_ambiguous: frozenset[tuple[str, str, str]],
) -> str | None:
    if any(claim in batch_ambiguous for claim in claims):
        return "batch_ambiguous"
    row_scopes = _requisition_scopes(
        identity.key for identity in row.identities if identity.kind == KIND_REQUISITION
    )
    if row.rejected or row_scopes is None:
        return "weak_identity"
    if any(len(keys) > 1 for keys in row_scopes.values()):
        # Native and URL-derived requisitions disagree in one provider scope.
        return "row_native_url_disagreement"
    if owners.get(claims[0]) != owner:
        # A URL or URL-derived requisition never overrides a native one.
        return "primary_not_owned"
    target = targets.get(owner)
    if target is None:
        return "target_missing"
    if target.superseded:
        return "target_superseded"
    if not target.is_open:
        return "target_closed"
    target_scopes = _requisition_scopes(target.requisitions)
    if target_scopes is None or any(len(keys) > 1 for keys in target_scopes.values()):
        return "target_ambiguous"
    if any(
        scope in target_scopes and target_scopes[scope] != keys
        for scope, keys in row_scopes.items()
    ):
        return "target_requisition_mismatch"
    if target_scopes and not row_scopes and not (row.backstop and target.direct):
        # Only a backstop sighting of a direct job, which never rewrites it,
        # may rely on a URL or listing against a job with a requisition.
        return "target_requisition_unverified"
    return None


def decide_resolutions(
    rows: Sequence[ResolutionRow],
    *,
    owners: Mapping[tuple[str, str, str], uuid.UUID],
    targets: Mapping[uuid.UUID, ResolutionTarget],
) -> list[ResolutionDecision]:
    """Decide every row of one import, then refuse contended targets.

    An identity claimed by two rows is ambiguous for both. A target is
    contended when more than one row would resolve to it, or when it is the
    legacy target of any row in the batch.
    """

    claimants: Counter[tuple[str, str, str]] = Counter(
        claim for row in rows for claim in set(row.claims)
    )
    ambiguous = frozenset(claim for claim, count in claimants.items() if count > 1)
    decisions = [
        decide_resolution(row, owners=owners, targets=targets, batch_ambiguous=ambiguous)
        for row in rows
    ]
    legacy_targets = {row.legacy_target for row in rows if row.legacy_target is not None}
    resolving = Counter(decision.target_id for decision in decisions if decision.resolves)
    return [
        ResolutionDecision("target_contention")
        if decision.resolves
        and (resolving[decision.target_id] > 1 or decision.target_id in legacy_targets)
        else decision
        for decision in decisions
    ]


def _requisition_scopes(
    keys: Iterable[str],
) -> dict[tuple[str, str], frozenset[str]] | None:
    """Group requisition keys by provider and scope; ``None`` if any is unscoped."""

    scopes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key in keys:
        parts = key.split("|", 2)
        if len(parts) != 3 or not all(parts):
            return None
        scopes[(parts[0], parts[1])].add(key)
    return {scope: frozenset(values) for scope, values in scopes.items()}


def identity_digest(key: str) -> str:
    """SHA-256 of the UTF-8 key, exactly as the database CHECK computes it."""

    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def derive_identities(
    job: Mapping[str, object],
    *,
    non_specific_urls: frozenset[str] = frozenset(),
) -> DerivedIdentities:
    """Return a final job's strong identities, strongest first.

    Never derives an identity from company, title, or location.
    """

    row = _row(job)
    extra = row["extra"]
    candidates: list[JobIdentity] = []
    native_url_disagreement = False

    requisition = stable_requisition_key(row)
    if requisition:
        candidates.append(JobIdentity(KIND_REQUISITION, requisition))
    # A URL shared by distinct requisitions names neither of them, so it
    # contributes neither a URL nor a URL-derived requisition identity.
    posting_url = posting_specific_url_key(row, non_specific_urls=non_specific_urls)
    url_requisition = (
        stable_requisition_key({"source_url": row["source_url"]}) if posting_url else ""
    )
    if url_requisition and url_requisition != requisition:
        candidates.append(JobIdentity(KIND_REQUISITION, url_requisition))
        native_url_disagreement = bool(requisition)
    if posting_url:
        candidates.append(JobIdentity(KIND_URL, posting_url))
    listing = _listing_identity(extra)
    if listing:
        candidates.append(JobIdentity(KIND_LISTING, listing))

    identities: list[JobIdentity] = []
    rejected = 0
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if (
            not candidate.key
            or len(candidate.key) > MAX_IDENTITY_KEY_LENGTH
            or _CONTROL_RE.search(candidate.key)
        ):
            rejected += 1
            continue
        marker = (candidate.kind, candidate.key)
        if marker not in seen:
            seen.add(marker)
            identities.append(candidate)
    return DerivedIdentities(
        identities=tuple(identities),
        native_url_disagreement=native_url_disagreement,
        rejected=rejected,
    )


def _row(job: Mapping[str, object]) -> dict:
    extra = job.get("extra")
    source_url = job.get("source_url")
    return {
        "company": str(job.get("company") or ""),
        "title": str(job.get("title") or ""),
        "location": str(job.get("location") or ""),
        "source_url": source_url if isinstance(source_url, str) else "",
        "extra": dict(extra) if isinstance(extra, Mapping) else {},
    }


def _listing_identity(extra: Mapping[str, object]) -> str:
    """A GitHub/Simplify listing ID, scoped to the feed that published it."""

    if extra.get("source") != "github":
        return ""
    listing_id = str(extra.get("listing_id") or extra.get("source_id") or "").strip()
    if not _LISTING_ID_RE.fullmatch(listing_id):
        return ""
    feed = extra.get("feed_url")
    scope = github_feed_health_key(feed) if isinstance(feed, str) and feed else ""
    if not scope:
        adapter = str(extra.get("source_adapter") or "").strip().casefold()
        scope = f"adapter:{adapter}" if adapter else ""
    return f"{scope}|{listing_id.casefold()}" if scope else ""


def row_source_key(
    job: Mapping[str, object],
    mapped: MappedJob,
    attempt_keys: frozenset[str],
) -> str | None:
    """The health key of the source attempt that produced this row, if known."""

    extra = job.get("extra")
    if not isinstance(extra, Mapping):
        return None
    if extra.get("source") == "direct":
        adapter = str(extra.get("source_adapter") or "")
        for company in (job.get("company"), mapped.company_name):
            if isinstance(company, str) and company.strip():
                key = direct_health_key(company, adapter)
                if key in attempt_keys:
                    return key
        return None
    if extra.get("source") == "github":
        feed = extra.get("feed_url")
        if isinstance(feed, str) and feed:
            key = github_feed_health_key(feed)
            if key in attempt_keys:
                return key
    return None


def run_identity_shadow(
    database: HostedDatabase,
    *,
    import_run_id: uuid.UUID,
    observed_at: datetime,
    final_jobs: Sequence[Mapping[str, object]],
    imported: Sequence[ImportedJob],
    evidence: SourceEvidence | None,
    resolution_diagnostics: bool = False,
) -> IdentityShadowReport:
    """Record one committed import's identity and source evidence.

    Never raises: every failure rolls back the whole shadow transaction and is
    reported as a bounded status. The import itself is already committed.
    ``resolution_diagnostics`` also reports, read-only, what Phase 3 would
    have decided from the ownership recorded before this import.
    """

    stage = "derive"
    try:
        plan = _plan(final_jobs, imported, evidence, observed_at)
        with database.session_factory.begin() as db:
            db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
            db.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
            stage = "lock_jobs"
            locked = _lock_jobs(db, plan)
            stage = "resolve"
            owners = _existing_owners(db, plan, locked)
            _resolve(plan, owners, locked)
            resolution = (
                _diagnose_resolution(db, plan) if resolution_diagnostics else None
            )
            stage = "identity_upsert"
            written = _upsert_identities(db, plan, locked, observed_at)
            stage = "job_evidence"
            _record_job_evidence(db, plan, locked)
            stage = "source_observations"
            _record_observations(
                db, plan, import_run_id, observed_at, written
            )
        return IdentityShadowReport(
            status="ok",
            counters={name: plan.counters[name] for name in COUNTER_NAMES},
            resolution=resolution,
        )
    except Exception as exc:  # noqa: BLE001 - shadow tracking must never fail an import
        name = type(exc).__name__
        LOGGER.warning("Identity shadow tracking failed at %s (%s).", stage, name)
        return IdentityShadowReport(
            status="failed",
            error=name if _ERROR_NAME_RE.fullmatch(name) else "error",
            failed_stage=stage,
            resolution=(
                IdentityResolutionReport(mode="shadow", status="failed")
                if resolution_diagnostics
                else None
            ),
        )


@dataclass(frozen=True)
class ResolutionPlan:
    """Per mapped row, the existing job to update instead, or ``None``."""

    targets: tuple[uuid.UUID | None, ...]
    report: IdentityResolutionReport


def plan_identity_resolution(
    db: Session,
    final_jobs: Sequence[Mapping[str, object]],
    mapped_jobs: Sequence[MappedJob],
) -> ResolutionPlan:
    """Decide, inside the import transaction, which rows resolve.

    The caller must hold the import advisory lock: ownership is then stable,
    because only this transaction or a shadow pass adds it and neither ever
    reassigns or deletes it. Candidate targets are locked ``FOR UPDATE`` before
    their state is read.
    """

    raw_by_id, non_specific = _batch_rows(final_jobs)
    legacy = _legacy_targets(db, [job.watcher_job_id for job in mapped_jobs])
    rows = [
        _resolution_row(
            raw_by_id.get(job.watcher_job_id, {}),
            job,
            non_specific,
            legacy.get(job.watcher_job_id),
        )
        for job in mapped_jobs
    ]
    decisions, counters = _decide(db, rows, lock=True)
    counters["resolved"] = counters["would_resolve"]
    return ResolutionPlan(
        targets=tuple(
            decision.target_id if decision.resolves else None for decision in decisions
        ),
        report=IdentityResolutionReport(
            mode="enforced",
            counters={name: counters[name] for name in RESOLVE_COUNTER_NAMES},
        ),
    )


def record_import_identities(
    db: Session,
    *,
    final_jobs: Sequence[Mapping[str, object]],
    imported: Sequence[ImportedJob],
    evidence: SourceEvidence | None,
    observed_at: datetime,
) -> None:
    """Write identity ownership inside the import transaction.

    Same rules as the shadow pass: an identity two jobs claim is not written
    and an existing owner is never replaced. Every job here is locked by the
    import itself.
    """

    plan = _plan(final_jobs, imported, evidence, observed_at)
    _upsert_identities(
        db, plan, frozenset(job.imported.job_id for job in plan.jobs), observed_at
    )


def _diagnose_resolution(db: Session, plan: _Plan) -> IdentityResolutionReport:
    """What enforced resolution would decide, without writing anything.

    Runs in a savepoint so a failure leaves the shadow pass intact. Reads take
    no row locks, so every row is evaluated, including skipped ones.
    """

    rows = [
        ResolutionRow(
            company_id=job.imported.mapped.company_id,
            identities=job.identities,
            legacy_target=(
                None if job.imported.outcome == "inserted" else job.imported.job_id
            ),
            rejected=job.rejected,
            backstop=_is_backstop(job.imported.mapped),
        )
        for job in plan.jobs
    ]
    try:
        with db.begin_nested():
            _decisions, counters = _decide(db, rows, lock=False)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never fail shadow work
        LOGGER.warning("Identity resolution diagnostics failed (%s).", type(exc).__name__)
        return IdentityResolutionReport(mode="shadow", status="failed")
    return IdentityResolutionReport(
        mode="shadow",
        counters={name: counters[name] for name in RESOLVE_COUNTER_NAMES},
    )


def _decide(
    db: Session,
    rows: Sequence[ResolutionRow],
    *,
    lock: bool,
) -> tuple[list[ResolutionDecision], Counter[str]]:
    owners = _owners_for(db, {claim for row in rows for claim in row.claims})
    candidates: set[uuid.UUID] = set()
    for row in rows:
        selected = {owners[claim] for claim in row.claims if claim in owners}
        if len(selected) == 1 and row.legacy_target not in selected:
            candidates |= selected
    legacy_ids = {
        row.legacy_target
        for row in rows
        if row.legacy_target is not None
        and any(identity.kind == KIND_REQUISITION for identity in row.identities)
    }
    targets, requisitions = _resolution_targets(db, candidates, legacy_ids, lock=lock)
    decisions = decide_resolutions(rows, owners=owners, targets=targets)
    return decisions, _count_decisions(rows, decisions, requisitions)


def _count_decisions(
    rows: Sequence[ResolutionRow],
    decisions: Sequence[ResolutionDecision],
    requisitions: Mapping[uuid.UUID, tuple[str, ...]],
) -> Counter[str]:
    counters: Counter[str] = Counter()
    for row, decision in zip(rows, decisions, strict=True):
        counters["rows"] += 1
        if decision.resolves:
            counters["would_resolve"] += 1
        elif decision.outcome in RESOLUTION_REASONS:
            counters["fallback"] += 1
            counters[f"reject_{decision.outcome}"] += 1
        else:
            counters[decision.outcome] += 1
        if _legacy_requisition_mismatch(row, requisitions):
            counters["legacy_requisition_mismatch"] += 1
    return counters


def _legacy_requisition_mismatch(
    row: ResolutionRow,
    requisitions: Mapping[uuid.UUID, tuple[str, ...]],
) -> bool:
    """Whether the legacy target owns a different requisition in a row scope.

    That is the legacy title/city ID merging two distinct requisitions.
    """

    if row.legacy_target is None:
        return False
    row_scopes = _requisition_scopes(
        identity.key for identity in row.identities if identity.kind == KIND_REQUISITION
    )
    stored = _requisition_scopes(requisitions.get(row.legacy_target, ()))
    if not row_scopes or not stored:
        return False
    return any(
        scope in stored and not stored[scope] <= keys for scope, keys in row_scopes.items()
    )


def _resolution_row(
    raw: Mapping[str, object],
    mapped: MappedJob,
    non_specific: frozenset[str],
    legacy_target: uuid.UUID | None,
) -> ResolutionRow:
    derived = derive_identities(raw, non_specific_urls=non_specific)
    return ResolutionRow(
        company_id=mapped.company_id,
        identities=derived.identities,
        legacy_target=legacy_target,
        rejected=derived.rejected,
        backstop=_is_backstop(mapped),
    )


def _is_backstop(mapped: MappedJob) -> bool:
    return mapped.source_metadata.get("source_type") == "backstop"


def _legacy_targets(db: Session, watcher_ids: Sequence[str]) -> dict[str, uuid.UUID]:
    found: dict[str, uuid.UUID] = {}
    wanted = sorted(set(watcher_ids))
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        for watcher_id, job_id in db.execute(
            text(
                "SELECT watcher_job_id, id FROM hosted_jobs "
                "WHERE watcher_job_id = ANY(CAST(:ids AS varchar[]))"
            ),
            {"ids": chunk},
        ):
            found[watcher_id] = job_id
    return found


def _resolution_targets(
    db: Session,
    candidates: set[uuid.UUID],
    legacy_ids: set[uuid.UUID],
    *,
    lock: bool,
) -> tuple[dict[uuid.UUID, ResolutionTarget], dict[uuid.UUID, tuple[str, ...]]]:
    """Candidate targets' state, and every listed job's requisition keys."""

    states: dict[uuid.UUID, tuple[bool, bool, bool]] = {}
    ordered = sorted(candidates)
    for start in range(0, len(ordered), _CHUNK):
        chunk = ordered[start : start + _CHUNK]
        for job_id, is_open, superseded, direct in db.execute(
            text(
                "SELECT id, is_open, superseded_by_job_id IS NOT NULL, "
                "COALESCE(source_metadata ->> 'source_type' = 'direct', false) "
                "FROM hosted_jobs WHERE id = ANY(:ids) ORDER BY id"
                + (" FOR UPDATE" if lock else "")
            ),
            {"ids": chunk},
        ):
            states[job_id] = (is_open, superseded, direct)

    requisitions: dict[uuid.UUID, list[str]] = defaultdict(list)
    listed = sorted(candidates | legacy_ids)
    table = HostedJobIdentity.__table__
    for start in range(0, len(listed), _CHUNK):
        chunk = listed[start : start + _CHUNK]
        for job_id, key in db.execute(
            table.select()
            .with_only_columns(table.c.job_id, table.c.identity_key)
            .where(table.c.job_id.in_(chunk), table.c.kind == KIND_REQUISITION)
            .order_by(table.c.job_id, table.c.identity_key)
        ):
            requisitions[job_id].append(key)

    targets = {
        job_id: ResolutionTarget(
            job_id=job_id,
            is_open=bool(is_open),
            superseded=bool(superseded),
            direct=bool(direct),
            requisitions=tuple(requisitions.get(job_id, ())),
        )
        for job_id, (is_open, superseded, direct) in states.items()
    }
    return targets, {job_id: tuple(keys) for job_id, keys in requisitions.items()}


@dataclass
class _JobPlan:
    imported: ImportedJob
    identities: tuple[JobIdentity, ...]
    source_key: str | None
    source_observed_at: datetime | None
    direct: bool
    rejected: int = 0


@dataclass
class _Plan:
    jobs: list[_JobPlan]
    # (company_id, kind, digest) -> (identity_key, job_id, source_key)
    claims: dict[tuple[str, str, str], tuple[str, uuid.UUID, str | None]]
    attempts: list[SourceAttempt]
    config_fingerprint: str | None
    counters: Counter[str]


def _plan(
    final_jobs: Sequence[Mapping[str, object]],
    imported: Sequence[ImportedJob],
    evidence: SourceEvidence | None,
    observed_at: datetime,
) -> _Plan:
    counters: Counter[str] = Counter()
    raw_by_id, non_specific = _batch_rows(final_jobs)

    attempts = list(evidence.attempts) if evidence is not None else []
    attempt_keys = frozenset(attempt.health_key for attempt in attempts)
    attempt_times: dict[str, datetime] = {}
    for attempt in attempts:
        seen_at = attempt.observed_at
        if (
            attempt.succeeded is True
            and isinstance(seen_at, datetime)
            and seen_at.tzinfo is not None
        ):
            # Collection precedes import; a skewed future attempt time must
            # never pin last-direct-seen ahead of later real sightings.
            attempt_times.setdefault(
                attempt.health_key, min(seen_at, observed_at)
            )

    jobs: list[_JobPlan] = []
    claimants: dict[tuple[str, str, str], set[uuid.UUID]] = defaultdict(set)
    keys: dict[tuple[str, str, str], str] = {}
    sources: dict[tuple[str, str, str], str | None] = {}
    for item in imported:
        raw = raw_by_id.get(item.mapped.watcher_job_id, {})
        derived = derive_identities(raw, non_specific_urls=non_specific)
        counters["rejected"] += derived.rejected
        counters["native_url_disagreements"] += int(derived.native_url_disagreement)
        source_key = row_source_key(raw, item.mapped, attempt_keys)
        direct = item.mapped.source_metadata.get("source_type") == "direct"
        if source_key is None and attempts:
            counters["jobs_source_unmatched"] += 1
        jobs.append(
            _JobPlan(
                imported=item,
                identities=derived.identities,
                source_key=source_key,
                source_observed_at=(
                    attempt_times.get(source_key) if source_key is not None else None
                ),
                direct=direct,
                rejected=derived.rejected,
            )
        )
        for identity in derived.identities:
            counters["identities_derived"] += 1
            counters[identity.kind] += 1
            claim = (item.mapped.company_id, identity.kind, identity.digest)
            claimants[claim].add(item.job_id)
            keys[claim] = identity.key
            sources.setdefault(claim, source_key)

    claims: dict[tuple[str, str, str], tuple[str, uuid.UUID, str | None]] = {}
    for claim, job_ids in claimants.items():
        if len(job_ids) != 1:
            # Two jobs in one import name the same posting: write neither.
            counters["batch_ambiguous"] += 1
            continue
        claims[claim] = (keys[claim], next(iter(job_ids)), sources[claim])
    counters["jobs"] = len(jobs)
    return _Plan(
        jobs=jobs,
        claims=claims,
        attempts=attempts,
        config_fingerprint=evidence.config_fingerprint if evidence else None,
        counters=counters,
    )


def _batch_rows(
    final_jobs: Sequence[Mapping[str, object]],
) -> tuple[dict[str, Mapping[str, object]], frozenset[str]]:
    """Raw rows by watcher ID, and the URLs that name no single posting."""

    rows = [job for job in final_jobs if isinstance(job, Mapping)]
    raw_by_id: dict[str, Mapping[str, object]] = {}
    for job in rows:
        watcher_id = job.get("id")
        if isinstance(watcher_id, str):
            raw_by_id.setdefault(watcher_id.strip(), job)
    return raw_by_id, non_specific_posting_urls([_row(job) for job in rows])


def _lock_jobs(db: Session, plan: _Plan) -> frozenset[uuid.UUID]:
    """Lock this import's job rows without ever waiting for one.

    Rows another transaction holds (a concurrent import) are skipped rather
    than waited for, so shadow work can never join a lock cycle with an
    ordinary import. Identity foreign keys and the evidence updates below only
    touch rows locked here.
    """

    job_ids = sorted({job.imported.job_id for job in plan.jobs})
    locked: set[uuid.UUID] = set()
    for start in range(0, len(job_ids), _CHUNK):
        chunk = job_ids[start : start + _CHUNK]
        locked.update(
            db.scalars(
                text(
                    "SELECT id FROM hosted_jobs WHERE id = ANY(:ids) "
                    "ORDER BY id FOR NO KEY UPDATE SKIP LOCKED"
                ),
                {"ids": chunk},
            )
        )
    plan.counters["jobs_skipped_locked"] = sum(
        1 for job in plan.jobs if job.imported.job_id not in locked
    )
    return frozenset(locked)


def _existing_owners(
    db: Session,
    plan: _Plan,
    locked: frozenset[uuid.UUID],
) -> dict[tuple[str, str, str], uuid.UUID]:
    return _owners_for(
        db,
        {
            (job.imported.mapped.company_id, identity.kind, identity.digest)
            for job in plan.jobs
            if job.imported.job_id in locked
            for identity in job.identities
        },
    )


def _owners_for(
    db: Session,
    claims: set[tuple[str, str, str]],
) -> dict[tuple[str, str, str], uuid.UUID]:
    """The job that currently owns each ``(company_id, kind, digest)``."""

    wanted = sorted(claims)
    table = HostedJobIdentity.__table__
    owners: dict[tuple[str, str, str], uuid.UUID] = {}
    for start in range(0, len(wanted), _CHUNK):
        chunk = wanted[start : start + _CHUNK]
        rows = db.execute(
            table.select()
            .with_only_columns(
                table.c.company_id,
                table.c.kind,
                table.c.identity_digest,
                table.c.job_id,
            )
            .where(
                tuple_(
                    table.c.company_id, table.c.kind, table.c.identity_digest
                ).in_(chunk)
            )
        )
        for company_id, kind, digest, job_id in rows:
            owners[(company_id, kind, digest)] = job_id
    return owners


def _resolve(
    plan: _Plan,
    owners: Mapping[tuple[str, str, str], uuid.UUID],
    locked: frozenset[uuid.UUID],
) -> None:
    """Compare the job identities would select with the legacy selection.

    Uses only identities recorded before this import. The result is counted,
    never applied.
    """

    for job in plan.jobs:
        if job.imported.job_id not in locked:
            continue
        if not job.identities:
            plan.counters["no_strong_identity"] += 1
            continue
        company_id = job.imported.mapped.company_id
        selected = {
            owners[key]
            for identity in job.identities
            if (key := (company_id, identity.kind, identity.digest)) in owners
        }
        if not selected:
            outcome = (
                "new_legacy_inserted"
                if job.imported.outcome == "inserted"
                else "new_legacy_existing"
            )
        elif len(selected) > 1:
            outcome = "conflict"
        elif job.imported.job_id in selected:
            outcome = "agree"
        else:
            outcome = "disagree"
        plan.counters[outcome] += 1


def _upsert_identities(
    db: Session,
    plan: _Plan,
    locked: frozenset[uuid.UUID],
    observed_at: datetime,
) -> set[tuple[str, str, str]]:
    """Insert or refresh identities; an identity's owner never changes."""

    table = HostedJobIdentity.__table__
    pending = sorted(
        (claim, value)
        for claim, value in plan.claims.items()
        if value[1] in locked
    )
    written: set[tuple[str, str, str]] = set()
    for start in range(0, len(pending), _CHUNK):
        chunk = pending[start : start + _CHUNK]
        statement = postgresql_insert(table).values(
            [
                {
                    "id": uuid.uuid4(),
                    "job_id": job_id,
                    "company_id": company_id,
                    "kind": kind,
                    "identity_key": key,
                    "identity_digest": digest,
                    "source_key": source_key,
                    "first_seen_at": observed_at,
                    "last_seen_at": observed_at,
                    "created_at": observed_at,
                    "updated_at": observed_at,
                }
                for (company_id, kind, digest), (key, job_id, source_key) in chunk
            ]
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_hosted_job_identities_company_kind_digest",
            set_={
                "last_seen_at": func.greatest(
                    table.c.last_seen_at, statement.excluded.last_seen_at
                ),
                "source_key": func.coalesce(
                    statement.excluded.source_key, table.c.source_key
                ),
                "updated_at": statement.excluded.updated_at,
            },
            # Another job already owns this identity: keep its owner.
            where=table.c.job_id == statement.excluded.job_id,
        ).returning(
            table.c.company_id,
            table.c.kind,
            table.c.identity_digest,
            literal_column("(xmax = 0)").label("inserted"),
        )
        for company_id, kind, digest, inserted in db.execute(statement):
            written.add((company_id, kind, digest))
            plan.counters["inserted" if inserted else "refreshed"] += 1
    plan.counters["ownership_conflicts"] += len(pending) - len(written)
    return written


def _record_job_evidence(
    db: Session,
    plan: _Plan,
    locked: frozenset[uuid.UUID],
) -> None:
    """Set source ownership and last-direct-seen; nothing else on the job.

    Source precedence matches the import: a backstop row never takes
    ownership of a job whose stored provenance is a direct source.
    """

    owners = sorted(
        (job.imported.job_id, job.source_key, job.direct)
        for job in plan.jobs
        if job.imported.job_id in locked and job.source_key is not None
    )
    for start in range(0, len(owners), _CHUNK):
        chunk = owners[start : start + _CHUNK]
        updated = db.scalars(
            text(
                "UPDATE hosted_jobs AS job SET source_key = incoming.source_key "
                "FROM unnest(CAST(:ids AS uuid[]), CAST(:keys AS varchar[]), "
                "CAST(:direct AS boolean[])) AS incoming(id, source_key, direct) "
                "WHERE job.id = incoming.id "
                "AND job.source_key IS DISTINCT FROM incoming.source_key "
                "AND (incoming.direct "
                "OR job.source_metadata ->> 'source_type' IS DISTINCT FROM 'direct') "
                "RETURNING job.id"
            ),
            {
                "ids": [item[0] for item in chunk],
                "keys": [item[1] for item in chunk],
                "direct": [item[2] for item in chunk],
            },
        ).all()
        plan.counters["jobs_source_key_set"] += len(updated)

    seen = sorted(
        (job.imported.job_id, job.source_observed_at)
        for job in plan.jobs
        if job.imported.job_id in locked
        and job.direct
        and job.source_observed_at is not None
    )
    for start in range(0, len(seen), _CHUNK):
        chunk = seen[start : start + _CHUNK]
        updated = db.scalars(
            text(
                "UPDATE hosted_jobs AS job SET last_direct_seen_at = incoming.seen_at "
                "FROM unnest(CAST(:ids AS uuid[]), CAST(:seen AS timestamptz[])) "
                "AS incoming(id, seen_at) "
                "WHERE job.id = incoming.id "
                "AND (job.last_direct_seen_at IS NULL "
                "OR job.last_direct_seen_at < incoming.seen_at) "
                "RETURNING job.id"
            ),
            {
                "ids": [item[0] for item in chunk],
                "seen": [item[1] for item in chunk],
            },
        ).all()
        plan.counters["last_direct_seen_updated"] += len(updated)


def _record_observations(
    db: Session,
    plan: _Plan,
    import_run_id: uuid.UUID,
    observed_at: datetime,
    written: set[tuple[str, str, str]],
) -> None:
    """One row per source attempt; unknown completeness stays unknown."""

    identity_counts: Counter[str] = Counter()
    for claim, (_key, _job_id, source_key) in plan.claims.items():
        if source_key is not None and claim in written:
            identity_counts[source_key] += 1

    rows: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    counters = plan.counters
    for attempt in plan.attempts:
        counters["attempts"] += 1
        values = _observation_values(attempt)
        if values is None:
            counters["invalid_attempts"] += 1
            continue
        if values["source_key"] in seen_keys:
            counters["duplicate_source_keys"] += 1
            continue
        seen_keys.add(values["source_key"])
        _count_observation(values, attempt, counters)
        values.update(
            id=uuid.uuid4(),
            import_run_id=import_run_id,
            identity_count=(
                identity_counts.get(values["source_key"], 0)
                if values["succeeded"] is True
                else None
            ),
            config_fingerprint=plan.config_fingerprint,
            created_at=observed_at,
            updated_at=observed_at,
        )
        rows.append(values)

    table = HostedSourceObservation.__table__
    for start in range(0, len(rows), _CHUNK):
        chunk = rows[start : start + _CHUNK]
        inserted = db.scalars(
            postgresql_insert(table)
            .values(chunk)
            .on_conflict_do_nothing(
                constraint="uq_hosted_source_observations_run_source"
            )
            .returning(table.c.id)
        ).all()
        counters["observations_written"] += len(inserted)
        counters["observations_existing"] += len(chunk) - len(inserted)


def _observation_values(attempt: SourceAttempt) -> dict[str, object] | None:
    source_key = attempt.health_key
    adapter = attempt.adapter
    if (
        not isinstance(source_key, str)
        or not source_key
        or len(source_key) > 255
        or _CONTROL_RE.search(source_key)
        or attempt.source_kind not in _SOURCE_KINDS
        or not isinstance(adapter, str)
        or not adapter
        or len(adapter) > 64
        or _CONTROL_RE.search(adapter)
        or not isinstance(attempt.observed_at, datetime)
        or attempt.observed_at.tzinfo is None
        or not isinstance(attempt.attempted, bool)
    ):
        return None
    company = attempt.company
    if not isinstance(company, str) or len(company) > 200 or _CONTROL_RE.search(company):
        company = None

    attempted = attempt.attempted
    succeeded = _optional_bool(attempt.succeeded) if attempted else None
    rows_returned = attempt.rows_returned if attempted else None
    if not isinstance(rows_returned, int) or isinstance(rows_returned, bool) or rows_returned < 0:
        rows_returned = None
    complete = _optional_bool(attempt.complete) if attempted else None
    if complete is True and succeeded is not True:
        # Contradictory evidence is unknown evidence, never completeness.
        complete = None

    return {
        "source_key": source_key,
        "source_kind": attempt.source_kind,
        "company_name": company,
        "adapter": adapter,
        "observed_at": attempt.observed_at,
        "attempted": attempted,
        "succeeded": succeeded,
        "complete": complete,
        "incomplete": _optional_bool(attempt.incomplete),
        "truncated": _optional_bool(attempt.truncated),
        "degraded": _optional_bool(attempt.degraded),
        "rows_returned": rows_returned,
    }


def _count_observation(
    values: Mapping[str, object],
    attempt: SourceAttempt,
    counters: Counter[str],
) -> None:
    counters[str(values["source_kind"])] += 1
    if not values["attempted"]:
        counters["not_attempted"] += 1
    elif values["succeeded"] is True:
        counters["succeeded"] += 1
    else:
        counters["failed"] += 1
    if values["complete"] is True:
        counters["complete"] += 1
    else:
        if values["complete"] is None:
            counters["complete_unknown"] += 1
        if attempt.attempted and attempt.complete is True:
            counters["inconsistent"] += 1
    for name in ("incomplete", "truncated", "degraded"):
        if values[name] is True:
            counters[name] += 1


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None
