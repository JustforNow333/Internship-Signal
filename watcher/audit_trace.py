"""Structured, read-only watcher posting diagnostics.

The helpers in this module consume production analysis results.  They do not
reimplement classification, location, internship, open-state, notification, or
posting-identity decisions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

from backend.app.dedupe import (
    fallback_posting_key,
    non_specific_posting_urls,
    norm_company,
    norm_title,
    norm_url,
    posting_identity_key,
    posting_specific_url_key,
    stable_requisition_key,
)
from watcher.config import CompanyCfg, WatcherConfig
from watcher.eligibility import OUTSIDE_US, determine_watcher_eligibility
from watcher.filters import is_internship, is_open
from watcher.seen_store import SeenStore

FINAL_REASONS = frozenset(
    {
        "outside_us",
        "wrong_season",
        "not_internship",
        "closed",
        "phd_only",
        "graduate_only",
        "freshman_only",
        "returning_intern_only",
        "nontechnical_role",
        "watcher_role_ineligible",
        "below_min_score",
        "already_emailed",
        "explicitly_primed",
        "not_on_watchlist",
        "not_collected",
        "deduplicated_duplicate",
        "pending",
    }
)


@dataclass(frozen=True)
class AuditQuery:
    company: str | None = None
    title: str | None = None
    url: str | None = None
    requisition_id: str | None = None
    job_id: str | None = None
    identity: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class PostingAuditTrace:
    schema_version: int
    query_match: dict[str, object]
    posting: dict[str, object]
    collection: dict[str, object]
    watchlist_match: dict[str, object]
    identity: dict[str, object]
    deduplication: dict[str, object]
    season: dict[str, object]
    internship_status: dict[str, object]
    open_status: dict[str, object]
    location: dict[str, object]
    role: dict[str, object]
    watcher_eligibility: dict[str, object]
    scoring: dict[str, object]
    notification: dict[str, object]
    final_result: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class PostingAuditContext:
    """Run-wide identity data reused by every posting trace."""

    non_specific_urls: frozenset[str]
    notification_records: tuple[dict[str, object], ...]
    similar_requisitions: dict[
        tuple[str, str],
        tuple[dict[str, str], ...],
    ]
    duplicate_entries: tuple[dict[str, object], ...] = ()
    duplicate_positions_by_identity: dict[str, tuple[int, ...]] = field(
        default_factory=dict
    )
    duplicate_positions_by_role: dict[
        tuple[str, str],
        tuple[int, ...],
    ] = field(default_factory=dict)
    duplicate_positions_by_title: dict[str, tuple[int, ...]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class PostingAuditOutcome:
    """One immutable evaluation shared by summaries and rich traces."""

    job_index: int | None
    job: dict[str, object]
    company_cfg: CompanyCfg | None
    extra: dict[str, object]
    non_specific_urls: frozenset[str]
    identity_key: str
    requisition_key: str
    normalized_url: str
    posting_url_key: str
    fallback_key: str
    generic_or_shared_url: bool
    sources: tuple[str, ...]
    source_details: dict[str, object]
    direct_found: bool
    github_found: bool
    season: dict[str, object]
    internship: bool
    open_now: bool
    eligibility: dict[str, object]
    eligibility_evaluated: bool
    location_status: str
    role_classification: dict[str, object]
    score: dict[str, object]
    fit_score: int
    watcher_eligible: bool
    threshold_eligible: bool
    matching_records: tuple[dict[str, object], ...]
    notification_evaluated: bool
    emailed: bool
    primed: bool
    pending: bool
    final_reason: str
    related_duplicates: tuple[dict[str, object], ...]
    source_coverage: dict[str, object]

    @property
    def duplicate_sightings(self) -> int:
        return len(self.related_duplicates)

    @property
    def has_merge_diagnostics(self) -> bool:
        return bool(self.related_duplicates)


def build_posting_audit_context(
    posting_universe: Sequence[dict],
    *,
    seen_store: SeenStore,
    duplicate_entries: Sequence[Mapping[str, object]] = (),
) -> PostingAuditContext:
    """Precompute whole-run identity inputs in linear time."""

    universe = tuple(posting_universe)
    non_specific_urls = non_specific_posting_urls(universe)
    requisitions_by_role: dict[
        tuple[str, str],
        dict[str, dict[str, str]],
    ] = {}
    for posting in universe:
        requisition_key = stable_requisition_key(posting)
        if not requisition_key:
            continue
        role_key = (
            norm_company(str(posting.get("company") or "")),
            norm_title(str(posting.get("title") or "")),
        )
        requisitions_by_role.setdefault(role_key, {}).setdefault(
            requisition_key,
            {
                "identity_key": posting_identity_key(
                    posting,
                    non_specific_urls=non_specific_urls,
                ),
                "requisition_key": requisition_key,
                "normalized_url": safe_posting_url(
                    norm_url(str(posting.get("source_url") or ""))
                ),
            },
        )
    similar_requisitions = {
        role_key: tuple(
            sorted(
                requisitions.values(),
                key=lambda item: item["identity_key"],
            )
        )
        for role_key, requisitions in requisitions_by_role.items()
    }
    normalized_duplicates = tuple(dict(entry) for entry in duplicate_entries)
    duplicate_positions_by_identity: dict[str, list[int]] = {}
    duplicate_positions_by_role: dict[
        tuple[str, str],
        list[int],
    ] = {}
    duplicate_positions_by_title: dict[str, list[int]] = {}
    for index, entry in enumerate(normalized_duplicates):
        kept_identity = str(entry.get("kept_identity_key") or "")
        if kept_identity:
            duplicate_positions_by_identity.setdefault(
                kept_identity,
                [],
            ).append(index)
        role_key = (
            norm_company(str(entry.get("company") or "")),
            norm_title(str(entry.get("title") or "")),
        )
        duplicate_positions_by_role.setdefault(role_key, []).append(index)
        duplicate_positions_by_title.setdefault(role_key[1], []).append(index)
    return PostingAuditContext(
        non_specific_urls=non_specific_urls,
        notification_records=tuple(seen_store.records()),
        similar_requisitions=similar_requisitions,
        duplicate_entries=normalized_duplicates,
        duplicate_positions_by_identity={
            key: tuple(value)
            for key, value in duplicate_positions_by_identity.items()
        },
        duplicate_positions_by_role={
            key: tuple(value)
            for key, value in duplicate_positions_by_role.items()
        },
        duplicate_positions_by_title={
            key: tuple(value)
            for key, value in duplicate_positions_by_title.items()
        },
    )


def evaluate_posting_outcome(
    job: dict,
    *,
    config: WatcherConfig,
    seen_store: SeenStore,
    posting_universe: Sequence[dict] = (),
    duplicate_entries: Sequence[Mapping[str, object]] = (),
    source_coverage: Mapping[str, object] | None = None,
    context: PostingAuditContext | None = None,
    job_index: int | None = None,
    defer_nonessential_decisions: bool = False,
) -> PostingAuditOutcome:
    """Evaluate one posting once for lightweight and rich audit consumers."""

    if context is None:
        non_specific_urls = non_specific_posting_urls([job, *posting_universe])
    else:
        non_specific_urls = context.non_specific_urls
    identity_key = posting_identity_key(
        job,
        non_specific_urls=non_specific_urls,
    )
    requisition_key = stable_requisition_key(job)
    normalized_url = norm_url(str(job.get("source_url") or ""))
    posting_url_key = posting_specific_url_key(
        job,
        non_specific_urls=non_specific_urls,
    )
    company_cfg = match_watchlist_company(
        str(job.get("company") or ""),
        config.companies,
    )
    extra = job.get("extra") if isinstance(job.get("extra"), dict) else {}
    source_list, source_details = source_sightings(extra)
    sources = tuple(source_list)
    direct_found = "direct_ats" in sources
    github_found = any(source != "direct_ats" for source in sources)

    season = posting_season(job, config, company_cfg)
    internship = is_internship(job)
    open_now = is_open(job)
    role_value = job.get("role_classification")
    role_classification = (
        dict(role_value) if isinstance(role_value, Mapping) else {}
    )
    score_value = job.get("score")
    score = dict(score_value) if isinstance(score_value, Mapping) else {}
    fit_score = _integer(score.get("fit_score", score.get("total", 0)))
    threshold_eligible = config.min_score is None or fit_score >= config.min_score

    preliminary_reason = final_reason_for(
        collected=True,
        watchlist_matched=company_cfg is not None,
        season_eligible=bool(season["eligible"]),
        internship=internship,
        open_now=open_now,
        eligibility=None,
        role=str(role_classification.get("role") or "unknown"),
        fit_score=fit_score,
        min_score=config.min_score,
        emailed=False,
        primed=False,
    )
    eligibility_evaluated = (
        not defer_nonessential_decisions or preliminary_reason is None
    )
    if eligibility_evaluated:
        eligibility = determine_watcher_eligibility(
            job,
            config.target_roles,
            apply_student_restrictions=internship and open_now,
        )
        watcher_eligible = bool(eligibility.get("watcher_eligible"))
        reason_without_notification = final_reason_for(
            collected=True,
            watchlist_matched=company_cfg is not None,
            season_eligible=bool(season["eligible"]),
            internship=internship,
            open_now=open_now,
            eligibility=eligibility,
            role=str(role_classification.get("role") or "unknown"),
            fit_score=fit_score,
            min_score=config.min_score,
            emailed=False,
            primed=False,
        )
    else:
        eligibility = {}
        watcher_eligible = False
        reason_without_notification = preliminary_reason
    if reason_without_notification is None:
        raise AssertionError("posting outcome did not resolve a final reason")

    notification_evaluated = (
        not defer_nonessential_decisions
        or reason_without_notification == "pending"
    )
    matching_records = (
        _matching_notification_records(
            job,
            seen_store=seen_store,
            posting_universe=posting_universe,
            non_specific_urls=non_specific_urls,
            context=context,
        )
        if notification_evaluated
        else ()
    )
    emailed = any(record.get("emailed_at") for record in matching_records)
    primed = any(record.get("primed_at") for record in matching_records)
    final_reason = (
        final_reason_for(
            collected=True,
            watchlist_matched=company_cfg is not None,
            season_eligible=bool(season["eligible"]),
            internship=internship,
            open_now=open_now,
            eligibility=eligibility,
            role=str(role_classification.get("role") or "unknown"),
            fit_score=fit_score,
            min_score=config.min_score,
            emailed=emailed,
            primed=primed,
        )
        if notification_evaluated
        else reason_without_notification
    )
    if final_reason is None:
        raise AssertionError("posting outcome did not resolve a final reason")
    related_duplicates = _related_duplicate_entries(
        job,
        identity_key=identity_key,
        sources=sources,
        duplicate_entries=duplicate_entries,
        context=context,
    )
    return PostingAuditOutcome(
        job_index=job_index,
        job=job,
        company_cfg=company_cfg,
        extra=extra,
        non_specific_urls=non_specific_urls,
        identity_key=identity_key,
        requisition_key=requisition_key,
        normalized_url=normalized_url,
        posting_url_key=posting_url_key,
        fallback_key=fallback_posting_key(job),
        generic_or_shared_url=bool(normalized_url and not posting_url_key),
        sources=sources,
        source_details=source_details,
        direct_found=direct_found,
        github_found=github_found,
        season=season,
        internship=internship,
        open_now=open_now,
        eligibility=eligibility,
        eligibility_evaluated=eligibility_evaluated,
        location_status=str(
            eligibility.get("location_status") or "not_evaluated"
        ),
        role_classification=role_classification,
        score=score,
        fit_score=fit_score,
        watcher_eligible=watcher_eligible,
        threshold_eligible=threshold_eligible,
        matching_records=matching_records,
        notification_evaluated=notification_evaluated,
        emailed=emailed,
        primed=primed,
        pending=final_reason == "pending",
        final_reason=final_reason,
        related_duplicates=related_duplicates,
        source_coverage=dict(source_coverage or {}),
    )


def _matching_notification_records(
    job: dict,
    *,
    seen_store: SeenStore,
    posting_universe: Sequence[dict],
    non_specific_urls: frozenset[str],
    context: PostingAuditContext | None,
) -> tuple[dict[str, object], ...]:
    if context is None:
        records = seen_store.matching_records(
            job,
            posting_universe=posting_universe,
        )
    else:
        records = seen_store.matching_records(
            job,
            precomputed_non_specific_urls=non_specific_urls,
            preloaded_records=context.notification_records,
        )
    return tuple(records)


def complete_posting_outcome(
    outcome: PostingAuditOutcome,
    *,
    config: WatcherConfig,
    seen_store: SeenStore,
    posting_universe: Sequence[dict] = (),
    context: PostingAuditContext | None = None,
) -> PostingAuditOutcome:
    """Complete deferred rich-trace decisions without repeating base work."""

    if outcome.eligibility_evaluated and outcome.notification_evaluated:
        return outcome
    eligibility = outcome.eligibility
    if not outcome.eligibility_evaluated:
        eligibility = determine_watcher_eligibility(
            outcome.job,
            config.target_roles,
            apply_student_restrictions=(
                outcome.internship and outcome.open_now
            ),
        )
    matching_records = outcome.matching_records
    if not outcome.notification_evaluated:
        matching_records = _matching_notification_records(
            outcome.job,
            seen_store=seen_store,
            posting_universe=posting_universe,
            non_specific_urls=outcome.non_specific_urls,
            context=context,
        )
    emailed = any(record.get("emailed_at") for record in matching_records)
    primed = any(record.get("primed_at") for record in matching_records)
    final_reason = final_reason_for(
        collected=True,
        watchlist_matched=outcome.company_cfg is not None,
        season_eligible=bool(outcome.season["eligible"]),
        internship=outcome.internship,
        open_now=outcome.open_now,
        eligibility=eligibility,
        role=str(outcome.role_classification.get("role") or "unknown"),
        fit_score=outcome.fit_score,
        min_score=config.min_score,
        emailed=emailed,
        primed=primed,
    )
    if final_reason is None:
        raise AssertionError("posting outcome did not resolve a final reason")
    return replace(
        outcome,
        eligibility=eligibility,
        eligibility_evaluated=True,
        location_status=str(
            eligibility.get("location_status") or "ambiguous"
        ),
        watcher_eligible=bool(eligibility.get("watcher_eligible")),
        matching_records=matching_records,
        notification_evaluated=True,
        emailed=emailed,
        primed=primed,
        pending=final_reason == "pending",
        final_reason=final_reason,
    )


def build_posting_trace(
    job: dict,
    *,
    config: WatcherConfig,
    seen_store: SeenStore,
    posting_universe: Sequence[dict] = (),
    duplicate_entries: Sequence[Mapping[str, object]] = (),
    source_coverage: Mapping[str, object] | None = None,
    query_match: Mapping[str, object] | None = None,
    context: PostingAuditContext | None = None,
    outcome: PostingAuditOutcome | None = None,
) -> PostingAuditTrace:
    """Build one structured trace from a production-analyzed job."""

    if outcome is None:
        outcome = evaluate_posting_outcome(
            job,
            config=config,
            seen_store=seen_store,
            posting_universe=posting_universe,
            duplicate_entries=duplicate_entries,
            source_coverage=source_coverage,
            context=context,
        )
    outcome = complete_posting_outcome(
        outcome,
        config=config,
        seen_store=seen_store,
        posting_universe=posting_universe,
        context=context,
    )
    job = outcome.job
    company_cfg = outcome.company_cfg
    extra = outcome.extra
    sources = list(outcome.sources)
    source_details = outcome.source_details
    identity_key = outcome.identity_key
    requisition_key = outcome.requisition_key
    normalized_url = outcome.normalized_url
    posting_url_key = outcome.posting_url_key
    fallback_key = outcome.fallback_key
    direct_found = outcome.direct_found
    github_found = outcome.github_found
    season = outcome.season
    internship = outcome.internship
    open_now = outcome.open_now
    eligibility = outcome.eligibility
    location_status = outcome.location_status
    role_cls = outcome.role_classification
    score = outcome.score
    fit_score = outcome.fit_score
    watcher_eligible = outcome.watcher_eligible
    threshold_eligible = outcome.threshold_eligible
    matching_records = outcome.matching_records
    emailed = outcome.emailed
    primed = outcome.primed
    pending = outcome.pending
    final_reason = outcome.final_reason
    related_duplicates = [dict(entry) for entry in outcome.related_duplicates]
    similar_distinct = (
        _similar_distinct_requisitions(
            job,
            posting_universe,
            non_specific_urls=outcome.non_specific_urls,
        )
        if context is None
        else _context_similar_distinct_requisitions(context, job)
    )

    coverage = outcome.source_coverage
    return PostingAuditTrace(
        schema_version=1,
        query_match=dict(query_match or {}),
        posting={
            "company": str(job.get("company") or ""),
            "title": str(job.get("title") or ""),
            "location": str(job.get("location") or ""),
            "url": safe_posting_url(job.get("source_url")),
            "analyzed_job_id": str(job.get("id") or ""),
        },
        collection={
            "collected": True,
            "sources": sources,
            "source_details": source_details,
            "direct_source_found": direct_found,
            "github_source_found": github_found,
        },
        watchlist_match={
            "matched": company_cfg is not None,
            "configured_company": company_cfg.name if company_cfg else None,
            "matched_alias": _matched_alias(str(job.get("company") or ""), company_cfg),
            "direct_ats_mode": company_cfg.ats if company_cfg else None,
            "direct_coverage": coverage.get("state"),
            "direct_status": coverage.get("direct_status"),
            "github_backstop_available": coverage.get("github_backstop_available"),
        },
        identity={
            "canonical_identity_key": identity_key,
            "source_native_requisition_id": _native_requisition_id(extra),
            "requisition_key": requisition_key,
            "normalized_posting_url": safe_posting_url(normalized_url),
            "normalized_posting_url_hash": normalized_url_hash(normalized_url),
            "posting_specific_url_key": safe_posting_url(posting_url_key),
            "posting_specific_url_key_hash": normalized_url_hash(posting_url_key),
            "fallback_key": fallback_key,
            "generic_or_shared_url": bool(normalized_url and not posting_url_key),
        },
        deduplication={
            "deduplicated_into_another": False,
            "duplicate_sightings": len(related_duplicates),
            "all_source_sightings": sources,
            "winning_source": extra.get("primary_source")
            or (sources[0] if sources else None),
            "source_priority": _winning_priority(extra),
            "merge_reasons": sorted(
                {
                    str(entry.get("matched_on") or "unknown")
                    for entry in related_duplicates
                }
            ),
            "merge_diagnostics": [
                {
                    "matched_on": entry.get("matched_on"),
                    "genuine_cross_source_duplicate": bool(
                        entry.get("cross_source")
                    ),
                    "exact_fallback_duplicate": (
                        entry.get("matched_on")
                        == "company+title+location"
                    ),
                    "tracking_parameter_url_duplicate": bool(
                        entry.get("tracking_parameter_url_duplicate")
                    ),
                }
                for entry in related_duplicates
            ],
            "similar_distinct_requisitions": similar_distinct,
            "duplicates": related_duplicates,
            "merged_into": identity_key,
        },
        season=season,
        internship_status={
            "eligible": internship,
            "recognized_as": (
                "internship_or_coop" if internship else "not_internship_or_coop"
            ),
        },
        open_status={
            "open": open_now,
            "active_metadata": extra.get("active"),
            "deadline_days_left": job.get("deadline_days_left"),
        },
        location={
            "status": location_status,
            "eligible": location_status != OUTSIDE_US,
            "explanation": eligibility.get("location_explanation"),
        },
        role={
            "classification": role_cls.get("role") or "unknown",
            "role_track": score.get("role_track")
            or role_cls.get("role_track")
            or "unknown",
            "confidence": role_cls.get("confidence"),
            "evidence": _bounded_strings(role_cls.get("evidence"), limit=5),
            "software_evidence": _bounded_strings(
                role_cls.get("software_evidence"),
                limit=6,
            ),
            "non_swe_evidence": _bounded_strings(
                role_cls.get("non_swe_evidence"),
                limit=6,
            ),
        },
        watcher_eligibility={
            "eligible": watcher_eligible,
            "fit_score": fit_score,
            "eligible_reason": eligibility.get("eligible_reason"),
            "ineligible_reason": eligibility.get("ineligible_reason"),
            "exclusion_reason": eligibility.get("eligibility_exclusion_reason"),
            "evidence_source": eligibility.get("eligibility_evidence_source"),
            "evidence": eligibility.get("eligibility_evidence"),
            "categorical_evaluation_applied": eligibility.get(
                "categorical_evaluation_applied"
            ),
            "mandatory_language_detected": eligibility.get(
                "eligibility_mandatory_language_detected"
            ),
            "negation_detected": eligibility.get(
                "eligibility_negation_detected"
            ),
            "mixed_eligibility_detected": eligibility.get(
                "eligibility_mixed_eligibility_detected"
            ),
        },
        scoring={
            "fit_score": fit_score,
            "total_score": _integer(score.get("total")),
            "recommendation": score.get("watcher_action_label")
            or score.get("action_label")
            or score.get("action")
            or "unknown",
            "fit_explanation": score.get("fit_explanation"),
            "minimum_score": config.min_score,
            "passes_minimum_score": threshold_eligible,
        },
        notification={
            "historical_match": bool(matching_records),
            "records": [
                _safe_notification_record(record)
                for record in matching_records
            ],
            "first_seen": _earliest(matching_records, "first_seen"),
            "emailed": emailed,
            "emailed_at": _earliest(matching_records, "emailed_at"),
            "primed": primed,
            "primed_at": _earliest(matching_records, "primed_at"),
            "pending": pending,
            "stored_analyzed_job_ids": sorted(
                {
                    str(record.get("analyzed_job_id"))
                    for record in matching_records
                    if record.get("analyzed_job_id")
                }
            ),
            "stored_identity_keys": sorted(
                {
                    str(record.get("identity_key"))
                    for record in matching_records
                    if record.get("identity_key")
                }
            ),
            "stored_requisition_keys": sorted(
                {
                    str(record.get("requisition_key"))
                    for record in matching_records
                    if record.get("requisition_key")
                }
            ),
        },
        final_result={
            "reason": final_reason,
            "emailed_now": False,
            "summary": final_summary(final_reason),
        },
    )


def enrich_duplicate_entries(
    rows: Sequence[Mapping[str, object]],
    entries: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], ...]:
    """Attach audit-only identity diagnostics to backend dedupe reports."""

    non_specific_urls = non_specific_posting_urls(rows)
    by_position = {
        index: row
        for index, row in enumerate(rows, start=1)
    }
    by_number = {
        row.get("_row_number"): row
        for row in rows
        if row.get("_row_number") is not None
    }
    enriched: list[dict[str, object]] = []
    for entry in entries:
        data = dict(entry)
        duplicate = by_position.get(entry.get("_audit_row_index"))
        kept = by_position.get(entry.get("_audit_duplicate_of_index"))
        if duplicate is None:
            duplicate = by_number.get(entry.get("row_number"))
        if kept is None:
            kept = by_number.get(entry.get("duplicate_of"))
        data.pop("_audit_row_index", None)
        data.pop("_audit_duplicate_of_index", None)
        for prefix, row in (("duplicate", duplicate), ("kept", kept)):
            if not isinstance(row, Mapping):
                continue
            data[f"{prefix}_identity_key"] = posting_identity_key(
                dict(row),
                non_specific_urls=non_specific_urls,
            )
            data[f"{prefix}_requisition_key"] = stable_requisition_key(dict(row))
            normalized = norm_url(str(row.get("source_url") or ""))
            data[f"{prefix}_normalized_url"] = safe_posting_url(normalized)
            data[f"{prefix}_normalized_url_hash"] = normalized_url_hash(
                normalized
            )
            data[f"{prefix}_fallback_key"] = fallback_posting_key(dict(row))
            posting_url_key = posting_specific_url_key(
                dict(row),
                non_specific_urls=non_specific_urls,
            )
            data[f"{prefix}_posting_specific_url_key_hash"] = (
                normalized_url_hash(posting_url_key)
            )
            data[f"{prefix}_generic_or_shared_url"] = bool(
                normalized
                and not posting_url_key
            )
        if isinstance(duplicate, Mapping) and isinstance(kept, Mapping):
            duplicate_raw_url = str(duplicate.get("source_url") or "").strip()
            kept_raw_url = str(kept.get("source_url") or "").strip()
            data["tracking_parameter_url_duplicate"] = bool(
                data.get("matched_on") == "source_url"
                and duplicate_raw_url
                and kept_raw_url
                and duplicate_raw_url != kept_raw_url
                and norm_url(duplicate_raw_url) == norm_url(kept_raw_url)
            )
        enriched.append(data)
    return tuple(enriched)


def final_reason_for(
    *,
    collected: bool,
    watchlist_matched: bool,
    season_eligible: bool,
    internship: bool,
    open_now: bool,
    eligibility: Mapping[str, object] | None,
    role: str,
    fit_score: int,
    min_score: int | None,
    emailed: bool,
    primed: bool,
) -> str | None:
    if not collected:
        return "not_collected"
    if not watchlist_matched:
        return "not_on_watchlist"
    if not season_eligible:
        return "wrong_season"
    if not internship:
        return "not_internship"
    if not open_now:
        return "closed"
    if eligibility is None:
        return None
    categorical = eligibility.get("eligibility_exclusion_reason")
    if categorical in {
        "phd_only",
        "graduate_only",
        "freshman_only",
        "returning_intern_only",
    }:
        return str(categorical)
    if eligibility.get("location_status") == OUTSIDE_US:
        return "outside_us"
    if not eligibility.get("watcher_eligible"):
        return (
            "nontechnical_role"
            if role in {"nontechnical", "non_technical", "unknown", ""}
            else "watcher_role_ineligible"
        )
    if min_score is not None and fit_score < min_score:
        return "below_min_score"
    if emailed:
        return "already_emailed"
    if primed:
        return "explicitly_primed"
    return "pending"


def posting_season(
    job: Mapping[str, object],
    config: WatcherConfig,
    company: CompanyCfg | None,
) -> dict[str, object]:
    extra = job.get("extra")
    if not isinstance(extra, Mapping):
        extra = {}
    raw_terms = extra.get("terms")
    if isinstance(raw_terms, str):
        observed = (raw_terms.strip(),) if raw_terms.strip() else ()
    elif isinstance(raw_terms, (list, tuple, set, frozenset)):
        observed = tuple(str(value).strip() for value in raw_terms if str(value).strip())
    else:
        observed = ()
    configured = tuple(company.terms) if company else tuple(config.terms)
    configured_tokens = {_term_token(term) for term in configured}
    observed_tokens = {_term_token(term) for term in observed}
    explicit = bool(observed_tokens)
    eligible = not explicit or bool(configured_tokens & observed_tokens)
    return {
        "eligible": eligible,
        "evidence_kind": "structured_terms" if explicit else "not_explicit",
        "configured_terms": list(configured),
        "observed_terms": list(observed),
        "explanation": (
            "Structured posting terms match the configured company season."
            if explicit and eligible
            else "Structured posting terms do not match the configured company season."
            if explicit
            else "No conflicting structured season was retained by collection."
        ),
    }


def match_watchlist_company(
    company_name: str,
    companies: Iterable[CompanyCfg],
) -> CompanyCfg | None:
    normalized = norm_company(company_name)
    for company in companies:
        if any(normalized == norm_company(name) for name in company.match_names()):
            return company
    return None


def source_sightings(extra: Mapping[str, object]) -> tuple[list[str], dict[str, object]]:
    raw_sources = extra.get("sources")
    if isinstance(raw_sources, (list, tuple)):
        sources = [str(value) for value in raw_sources if str(value).strip()]
    else:
        source = (
            "direct_ats"
            if extra.get("source") == "direct"
            else str(
                extra.get("source_name")
                or extra.get("source_adapter")
                or extra.get("source")
                or ""
            ).strip()
        )
        sources = [source] if source else []
    details = extra.get("source_details")
    safe_details = {}
    if isinstance(details, Mapping):
        for name, value in details.items():
            if isinstance(value, Mapping):
                safe_details[str(name)] = {
                    key: value.get(key)
                    for key in (
                        "priority",
                        "source",
                        "source_adapter",
                        "source_format",
                        "source_requisition_id",
                        "source_system",
                        "active",
                        "closed",
                    )
                    if key in value
                }
    return sources, safe_details


def query_matches_trace(
    trace: PostingAuditTrace | Mapping[str, object],
    query: AuditQuery,
) -> tuple[bool, list[str]]:
    data = trace.as_dict() if isinstance(trace, PostingAuditTrace) else dict(trace)
    posting = data.get("posting") if isinstance(data.get("posting"), Mapping) else {}
    identity = data.get("identity") if isinstance(data.get("identity"), Mapping) else {}
    deduplication = (
        data.get("deduplication")
        if isinstance(data.get("deduplication"), Mapping)
        else {}
    )
    duplicates = (
        deduplication.get("duplicates")
        if isinstance(deduplication.get("duplicates"), list)
        else []
    )
    matched: list[str] = []

    if query.company:
        wanted = norm_company(query.company)
        configured = data.get("watchlist_match")
        configured_name = (
            str(configured.get("configured_company") or "")
            if isinstance(configured, Mapping)
            else ""
        )
        if wanted not in {
            norm_company(str(posting.get("company") or "")),
            norm_company(configured_name),
        }:
            return False, []
        matched.append("company")
    if query.title:
        if norm_title(query.title) not in norm_title(str(posting.get("title") or "")):
            return False, []
        matched.append("title")
    if query.url:
        wanted = norm_url(query.url)
        wanted_url_key = posting_specific_url_key(
            {"source_url": query.url}
        )
        wanted_requisition = stable_requisition_key(
            {"source_url": query.url}
        )
        stored_url = str(posting.get("url") or "")
        stored_normalized = str(identity.get("normalized_posting_url") or "")
        stored_hash = str(identity.get("normalized_posting_url_hash") or "")
        stored_url_key_hash = str(
            identity.get("posting_specific_url_key_hash") or ""
        )
        if wanted_url_key and stored_url_key_hash:
            main_url_match = (
                normalized_url_hash(wanted_url_key) == stored_url_key_hash
            )
            duplicate_url_match = any(
                normalized_url_hash(wanted_url_key)
                == str(
                    item.get("duplicate_posting_specific_url_key_hash") or ""
                )
                for item in duplicates
                if isinstance(item, Mapping)
            )
        elif wanted_requisition:
            main_url_match = wanted_requisition == str(
                identity.get("requisition_key") or ""
            )
            duplicate_url_match = any(
                wanted_requisition
                == str(item.get("duplicate_requisition_key") or "")
                for item in duplicates
                if isinstance(item, Mapping)
            )
        else:
            main_url_match = (
                wanted not in {norm_url(stored_url), norm_url(stored_normalized)}
                and normalized_url_hash(wanted) != stored_hash
            ) is False
            duplicate_url_match = any(
                (
                    wanted
                    == norm_url(str(item.get("duplicate_normalized_url") or ""))
                    or normalized_url_hash(wanted)
                    == str(item.get("duplicate_normalized_url_hash") or "")
                )
                for item in duplicates
                if isinstance(item, Mapping)
            )
        if not main_url_match and not duplicate_url_match:
            return False, []
        matched.append("url")
        if duplicate_url_match and not main_url_match:
            matched.append("deduplicated_duplicate")
    if query.requisition_id:
        wanted = str(query.requisition_id).strip().casefold()
        values = {
            str(identity.get("source_native_requisition_id") or "").casefold(),
            str(identity.get("requisition_key") or "").casefold(),
            str(identity.get("canonical_identity_key") or "").casefold(),
        }
        main_req_match = any(
            value == wanted or value.endswith(f"|{wanted}")
            for value in values
        )
        duplicate_req_match = any(
            (
                str(item.get("duplicate_requisition_key") or "").casefold()
                == wanted
                or str(
                    item.get("duplicate_requisition_key") or ""
                ).casefold().endswith(f"|{wanted}")
            )
            for item in duplicates
            if isinstance(item, Mapping)
        )
        if not main_req_match and not duplicate_req_match:
            return False, []
        matched.append("requisition_id")
        if duplicate_req_match and not main_req_match:
            matched.append("deduplicated_duplicate")
    if query.job_id:
        if str(posting.get("analyzed_job_id") or "") != str(query.job_id).strip():
            return False, []
        matched.append("job_id")
    if query.identity:
        wanted = str(query.identity).strip()
        main_identity_match = wanted in {
            str(identity.get("canonical_identity_key") or ""),
            str(identity.get("requisition_key") or ""),
            str(identity.get("fallback_key") or ""),
        }
        duplicate_identity_match = any(
            wanted
            in {
                str(item.get("duplicate_identity_key") or ""),
                str(item.get("duplicate_requisition_key") or ""),
                str(item.get("duplicate_fallback_key") or ""),
            }
            for item in duplicates
            if isinstance(item, Mapping)
        )
        if not main_identity_match and not duplicate_identity_match:
            return False, []
        matched.append("identity")
        if duplicate_identity_match and not main_identity_match:
            matched.append("deduplicated_duplicate")
    return bool(matched), matched


def not_collected_trace(
    query: AuditQuery,
    *,
    config: WatcherConfig,
) -> PostingAuditTrace:
    company_cfg = (
        match_watchlist_company(query.company, config.companies)
        if query.company
        else None
    )
    reason = not_collected_reason(query, config=config)
    empty = {}
    return PostingAuditTrace(
        schema_version=1,
        query_match={"matched_fields": [], "query": query.as_dict()},
        posting={
            "company": query.company or "",
            "title": query.title or "",
            "location": "",
            "url": safe_posting_url(query.url),
            "analyzed_job_id": query.job_id or "",
        },
        collection={"collected": False, "sources": []},
        watchlist_match={
            "matched": company_cfg is not None,
            "configured_company": company_cfg.name if company_cfg else None,
            "direct_ats_mode": company_cfg.ats if company_cfg else None,
        },
        identity={
            "canonical_identity_key": query.identity,
            "source_native_requisition_id": query.requisition_id,
            "normalized_posting_url": safe_posting_url(norm_url(query.url or "")),
            "normalized_posting_url_hash": normalized_url_hash(norm_url(query.url or "")),
        },
        deduplication=empty,
        season=empty,
        internship_status=empty,
        open_status=empty,
        location=empty,
        role=empty,
        watcher_eligibility=empty,
        scoring=empty,
        notification={
            "historical_match": False,
            "emailed": False,
            "primed": False,
            "pending": False,
        },
        final_result={"reason": reason, "emailed_now": False, "summary": final_summary(reason)},
    )


def not_collected_reason(
    query: AuditQuery,
    *,
    config: WatcherConfig,
) -> str:
    """Return the shared final reason for a query absent from collection."""

    company_cfg = (
        match_watchlist_company(query.company, config.companies)
        if query.company
        else None
    )
    return "not_collected" if company_cfg or not query.company else "not_on_watchlist"


def final_summary(reason: str) -> str:
    return {
        "already_emailed": "emailed previously",
        "explicitly_primed": "explicitly primed and suppressed",
        "pending": "eligible and still pending for email",
        "not_collected": "no collected or persisted posting matched the query",
        "not_on_watchlist": "company is not in the configured watchlist",
        "deduplicated_duplicate": "duplicate sighting merged into the retained posting",
    }.get(reason, f"excluded or suppressed: {reason}")


def safe_posting_url(value: object) -> str:
    """Return a credential-free URL while retaining a safe posting path."""

    normalized = norm_url(str(value or ""))
    if not normalized:
        return ""
    try:
        parsed = urlsplit(normalized)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return ""


def normalized_url_hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest() if value else ""


def _duplicate_relates(entry: Mapping[str, object], job: Mapping[str, object]) -> bool:
    return (
        norm_company(str(entry.get("company") or ""))
        == norm_company(str(job.get("company") or ""))
        and norm_title(str(entry.get("title") or ""))
        == norm_title(str(job.get("title") or ""))
    )


def _related_duplicate_entries(
    job: Mapping[str, object],
    *,
    identity_key: str,
    sources: Sequence[str],
    duplicate_entries: Sequence[Mapping[str, object]],
    context: PostingAuditContext | None,
) -> tuple[dict[str, object], ...]:
    if context is None or not context.duplicate_entries:
        return tuple(
            dict(entry)
            for entry in duplicate_entries
            if (
                entry.get("kept_identity_key")
                and entry.get("kept_identity_key") == identity_key
            )
            or _duplicate_relates(entry, job)
            or (
                len(sources) > 1
                and norm_title(str(entry.get("title") or ""))
                == norm_title(str(job.get("title") or ""))
            )
        )

    role_key = (
        norm_company(str(job.get("company") or "")),
        norm_title(str(job.get("title") or "")),
    )
    positions = set(
        context.duplicate_positions_by_identity.get(identity_key, ())
    )
    positions.update(context.duplicate_positions_by_role.get(role_key, ()))
    if len(sources) > 1:
        positions.update(
            context.duplicate_positions_by_title.get(role_key[1], ())
        )
    return tuple(
        dict(context.duplicate_entries[position])
        for position in sorted(positions)
    )


def _similar_distinct_requisitions(
    job: Mapping[str, object],
    posting_universe: Sequence[Mapping[str, object]],
    *,
    non_specific_urls: frozenset[str],
) -> list[dict[str, str]]:
    current_requisition = stable_requisition_key(dict(job))
    if not current_requisition:
        return []
    results = []
    for other in posting_universe:
        if other is job:
            continue
        other_requisition = stable_requisition_key(dict(other))
        if not other_requisition or other_requisition == current_requisition:
            continue
        if (
            norm_company(str(other.get("company") or ""))
            != norm_company(str(job.get("company") or ""))
            or norm_title(str(other.get("title") or ""))
            != norm_title(str(job.get("title") or ""))
        ):
            continue
        results.append(
            {
                "identity_key": posting_identity_key(
                    dict(other),
                    non_specific_urls=non_specific_urls,
                ),
                "requisition_key": other_requisition,
                "normalized_url": safe_posting_url(
                    norm_url(str(other.get("source_url") or ""))
                ),
            }
        )
    return sorted(results, key=lambda item: item["identity_key"])[:25]


def _context_similar_distinct_requisitions(
    context: PostingAuditContext,
    job: Mapping[str, object],
) -> list[dict[str, str]]:
    current_requisition = stable_requisition_key(dict(job))
    if not current_requisition:
        return []
    role_key = (
        norm_company(str(job.get("company") or "")),
        norm_title(str(job.get("title") or "")),
    )
    return [
        dict(item)
        for item in context.similar_requisitions.get(role_key, ())
        if item["requisition_key"] != current_requisition
    ][:25]


def _native_requisition_id(extra: Mapping[str, object]) -> str:
    value = extra.get("source_requisition_id") or extra.get("source_id")
    return str(value or "")


def _winning_priority(extra: Mapping[str, object]) -> int | None:
    details = extra.get("source_details")
    primary = extra.get("primary_source")
    if isinstance(details, Mapping) and isinstance(details.get(primary), Mapping):
        return _integer(details[primary].get("priority"))
    value = extra.get("source_priority")
    return _integer(value) if value is not None else None


def _matched_alias(posting_company: str, company: CompanyCfg | None) -> str | None:
    if company is None:
        return None
    normalized = norm_company(posting_company)
    for name in company.match_names():
        if norm_company(name) == normalized:
            return name
    return None


def _term_token(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _bounded_strings(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item)[:320] for item in value[:limit]]


def _earliest(records: Sequence[Mapping[str, object]], field: str) -> object:
    values = sorted(str(record[field]) for record in records if record.get(field))
    return values[0] if values else None


def _safe_notification_record(
    record: Mapping[str, object],
) -> dict[str, object]:
    return {
        key: (
            safe_posting_url(value)
            if key == "url"
            else value
        )
        for key, value in record.items()
        if key
        in {
            "job_id",
            "analyzed_job_id",
            "identity_key",
            "requisition_key",
            "company",
            "title",
            "location",
            "url",
            "first_source",
            "first_seen",
            "emailed_at",
            "primed_at",
        }
    }


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def stable_json(trace: PostingAuditTrace | Mapping[str, object]) -> str:
    data = trace.as_dict() if isinstance(trace, PostingAuditTrace) else trace
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
