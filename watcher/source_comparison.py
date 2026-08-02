"""Sanitized, bounded direct-versus-GitHub source comparison reports."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from backend.app.dedupe import norm_company
from watcher.audit_trace import (
    AuditQuery,
    PostingAuditOutcome,
    build_posting_audit_context,
    build_posting_trace,
    evaluate_posting_outcome,
    not_collected_reason,
    not_collected_trace,
    safe_posting_url,
)
from watcher.config import WatcherConfig
from watcher.seen_store import SeenStore
from watcher.source_health import (
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    CompanyCoverage,
    SourceAttempt,
    SourceHealthState,
    iso_utc,
    sanitize_error,
    safe_error_kind,
    safe_run_id,
)

CATEGORY_GITHUB_ONLY = "github_only"
CATEGORY_DIRECT_ONLY = "direct_only"
CATEGORY_BOTH = "both"
CATEGORY_REJECTED = "collected_rejected"
CATEGORY_NO_POSTINGS = "no_postings"
CATEGORIES = (
    CATEGORY_GITHUB_ONLY,
    CATEGORY_DIRECT_ONLY,
    CATEGORY_BOTH,
    CATEGORY_REJECTED,
    CATEGORY_NO_POSTINGS,
)

DEFAULT_EXAMPLE_LIMIT = 10
DEFAULT_RUN_RETENTION = 30
DEFAULT_DETAIL_RUN_RETENTION = 3
DEFAULT_MAX_POSTINGS_PER_RUN = 2_000
DEFAULT_ROUTINE_REJECTION_SAMPLE_LIMIT = 25
DEFAULT_COMPACTION_MIN_DELETED_ROWS = 500
DEFAULT_COMPACTION_FREE_PAGE_RATIO = 0.25
ROUTINE_REJECTION_REASONS = frozenset(
    {
        "not_internship",
        "closed",
        "wrong_season",
        "nontechnical_role",
        "watcher_role_ineligible",
        "outside_us",
        "below_min_score",
    }
)


@dataclass(frozen=True)
class SourceComparisonEntry:
    category: str
    company: str
    title: str
    identity_key: str
    analyzed_job_id: str
    source_kinds: tuple[str, ...]
    final_reason: str
    direct_status: str | None
    direct_coverage: str | None
    github_backstop_available: bool | None
    trace: dict[str, object]


@dataclass(frozen=True)
class PostingComparisonSummary:
    """Small per-posting outcome used for counts and detail selection."""

    job_index: int | None
    company: str
    title: str
    identity_key: str
    analyzed_job_id: str
    source_kinds: tuple[str, ...]
    final_reason: str
    category: str
    direct_status: str | None
    direct_coverage: str | None
    github_backstop_available: bool | None
    generic_or_shared_url: bool
    duplicate_sightings: int
    deduplicated_into_another: bool
    has_merge_diagnostics: bool


@dataclass(frozen=True)
class SourceComparisonDetailPolicy:
    """One owner for deterministic source-comparison detail retention."""

    routine_rejection_sample_limit: int = (
        DEFAULT_ROUTINE_REJECTION_SAMPLE_LIMIT
    )
    maximum_retained_details: int = DEFAULT_MAX_POSTINGS_PER_RUN
    example_limit: int = DEFAULT_EXAMPLE_LIMIT


@dataclass(frozen=True)
class SourceComparisonReport:
    schema_version: int
    run_id: str
    observed_at: str
    counts: dict[str, int]
    entries: tuple[SourceComparisonEntry, ...]
    health: dict[str, object] = field(default_factory=dict)
    aggregates: dict[str, int] = field(default_factory=dict)
    postings_evaluated: int = 0
    detail_entries_retained: int = 0

    def __post_init__(self) -> None:
        if self.postings_evaluated == 0:
            object.__setattr__(
                self,
                "postings_evaluated",
                sum(
                    int(self.counts.get(category, 0))
                    for category in CATEGORIES
                    if category != CATEGORY_NO_POSTINGS
                ),
            )
        if self.detail_entries_retained == 0 and self.entries:
            object.__setattr__(
                self,
                "detail_entries_retained",
                len(self.entries),
            )

    def as_dict(self, *, example_limit: int | None = None) -> dict[str, object]:
        entries = self.entries
        if example_limit is not None:
            bounded: list[SourceComparisonEntry] = []
            for category in CATEGORIES:
                category_entries = [
                    entry
                    for entry in entries
                    if entry.category == category
                ][:example_limit]
                bounded.extend(category_entries)
            entries = tuple(bounded)
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "observed_at": self.observed_at,
            "counts": dict(self.counts),
            "aggregates": _aggregate_counts(self.counts, self.health),
            "health": dict(self.health),
            "postings_evaluated": self.postings_evaluated,
            "detail_entries_retained": self.detail_entries_retained,
            "entries": [asdict(entry) for entry in entries],
        }


def build_source_comparison(
    *,
    config: WatcherConfig,
    jobs: Sequence[dict],
    seen_store: SeenStore,
    run_id: str,
    observed_at: datetime,
    duplicate_report: Sequence[Mapping[str, object]] = (),
    coverage: Sequence[CompanyCoverage] = (),
    source_attempts: Sequence[SourceAttempt] = (),
    source_health_states: Mapping[str, SourceHealthState] | None = None,
    detail_policy: SourceComparisonDetailPolicy | None = None,
    outcome_evaluator=None,
    summary_builder=None,
    trace_builder=None,
    trace_sanitizer=None,
) -> SourceComparisonReport:
    """Evaluate every posting lightly, then trace only retained details."""

    detail_policy = detail_policy or SourceComparisonDetailPolicy()
    outcome_evaluator = outcome_evaluator or evaluate_posting_outcome
    summary_builder = summary_builder or build_posting_comparison_summary
    trace_builder = trace_builder or build_posting_trace
    trace_sanitizer = trace_sanitizer or _sanitize_trace
    coverage_by_company = index_company_coverage(config, coverage)
    audit_context = build_posting_audit_context(
        jobs,
        seen_store=seen_store,
        duplicate_entries=duplicate_report,
    )
    outcomes: list[PostingAuditOutcome] = []
    summaries: list[PostingComparisonSummary] = []
    observed_companies: set[str] = set()
    for job_index, job in enumerate(jobs):
        company_key = norm_company(str(job.get("company") or ""))
        observed_companies.add(company_key)
        outcome = outcome_evaluator(
            job,
            config=config,
            seen_store=seen_store,
            posting_universe=jobs,
            duplicate_entries=duplicate_report,
            source_coverage=coverage_by_company.get(company_key),
            context=audit_context,
            job_index=job_index,
            defer_nonessential_decisions=True,
        )
        outcomes.append(outcome)
        summaries.append(summary_builder(outcome))

    for company in config.companies:
        if any(
            norm_company(name) in observed_companies
            for name in company.match_names()
        ):
            continue
        company_coverage = coverage_by_company.get(norm_company(company.name), {})
        query = AuditQuery(company=company.name)
        summaries.append(
            PostingComparisonSummary(
                job_index=None,
                category=CATEGORY_NO_POSTINGS,
                company=company.name,
                title="",
                identity_key="",
                analyzed_job_id="",
                source_kinds=(),
                final_reason=not_collected_reason(query, config=config),
                direct_status=_optional_string(
                    company_coverage.get("direct_status")
                ),
                direct_coverage=_optional_string(
                    company_coverage.get("state")
                ),
                github_backstop_available=_optional_bool(
                    company_coverage.get("github_backstop_available")
                ),
                generic_or_shared_url=False,
                duplicate_sightings=0,
                deduplicated_into_another=False,
                has_merge_diagnostics=False,
            )
        )

    selected_summaries = select_comparison_details(
        summaries,
        policy=detail_policy,
    )
    counts = {
        category: sum(summary.category == category for summary in summaries)
        for category in CATEGORIES
    }
    health = _comparison_health(
        source_attempts,
        source_health_states or {},
    )
    entries: list[SourceComparisonEntry] = []
    for summary in selected_summaries:
        if summary.job_index is None:
            query = AuditQuery(company=summary.company)
            trace = not_collected_trace(query, config=config)
            trace_data = trace.as_dict()
            trace_data["watchlist_match"].update(
                {
                    "direct_coverage": summary.direct_coverage,
                    "direct_status": summary.direct_status,
                    "github_backstop_available": (
                        summary.github_backstop_available
                    ),
                }
            )
        else:
            outcome = outcomes[summary.job_index]
            trace = trace_builder(
                outcome.job,
                config=config,
                seen_store=seen_store,
                posting_universe=jobs,
                duplicate_entries=duplicate_report,
                source_coverage=outcome.source_coverage,
                context=audit_context,
                outcome=outcome,
            )
            trace_data = trace.as_dict()
        entries.append(
            _entry_from_summary(
                summary,
                trace_sanitizer(trace_data),
            )
        )
    return SourceComparisonReport(
        schema_version=2,
        run_id=safe_run_id(run_id),
        observed_at=iso_utc(observed_at),
        counts=counts,
        entries=tuple(entries),
        health=health,
        aggregates=_aggregate_counts(counts, health),
        postings_evaluated=len(jobs),
        detail_entries_retained=len(entries),
    )


def build_posting_comparison_summary(
    outcome: PostingAuditOutcome,
) -> PostingComparisonSummary:
    """Project the shared posting outcome into the retention model."""

    coverage = outcome.source_coverage
    return PostingComparisonSummary(
        job_index=outcome.job_index,
        company=sanitize_error(str(outcome.job.get("company") or "")),
        title=sanitize_error(str(outcome.job.get("title") or "")),
        identity_key=sanitize_error(outcome.identity_key),
        analyzed_job_id=sanitize_error(
            str(outcome.job.get("id") or "")
        ),
        source_kinds=tuple(
            sanitize_error(source) for source in outcome.sources
        ),
        final_reason=sanitize_error(outcome.final_reason),
        category=comparison_category(
            outcome.sources,
            outcome.final_reason,
        ),
        direct_status=_optional_sanitized_string(
            coverage.get("direct_status")
        ),
        direct_coverage=_optional_sanitized_string(coverage.get("state")),
        github_backstop_available=_optional_bool(
            coverage.get("github_backstop_available")
        ),
        generic_or_shared_url=outcome.generic_or_shared_url,
        duplicate_sightings=outcome.duplicate_sightings,
        deduplicated_into_another=False,
        has_merge_diagnostics=outcome.has_merge_diagnostics,
    )


def _entry_from_summary(
    summary: PostingComparisonSummary,
    trace: dict[str, object],
) -> SourceComparisonEntry:
    return SourceComparisonEntry(
        category=summary.category,
        company=sanitize_error(summary.company),
        title=sanitize_error(summary.title),
        identity_key=sanitize_error(summary.identity_key),
        analyzed_job_id=sanitize_error(summary.analyzed_job_id),
        source_kinds=tuple(
            sanitize_error(source) for source in summary.source_kinds
        ),
        final_reason=sanitize_error(summary.final_reason),
        direct_status=summary.direct_status,
        direct_coverage=summary.direct_coverage,
        github_backstop_available=summary.github_backstop_available,
        trace=trace,
    )


def comparison_category(sources: Sequence[str], final_reason: str) -> str:
    if final_reason not in {"pending", "already_emailed", "explicitly_primed"}:
        return CATEGORY_REJECTED
    direct = "direct_ats" in sources
    github = any(source != "direct_ats" for source in sources)
    if direct and github:
        return CATEGORY_BOTH
    if direct:
        return CATEGORY_DIRECT_ONLY
    return CATEGORY_GITHUB_ONLY


def index_company_coverage(
    config: WatcherConfig,
    coverage: Sequence[CompanyCoverage],
) -> dict[str, dict[str, object]]:
    """Index coverage by configured company name and every alias."""

    indexed: dict[str, dict[str, object]] = {}
    config_by_name = {
        norm_company(company.name): company
        for company in config.companies
    }
    for item in coverage:
        data = asdict(item)
        company = config_by_name.get(norm_company(item.company))
        names = company.match_names() if company else (item.company,)
        for name in names:
            indexed[norm_company(name)] = data
    return indexed


def render_console(
    report: SourceComparisonReport,
    *,
    output: TextIO | None = None,
    example_limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> None:
    output = output or sys.stdout
    print("Source comparison", file=output)
    print("", file=output)
    labels = (
        ("GitHub-only eligible postings", CATEGORY_GITHUB_ONLY),
        ("Direct-only eligible postings", CATEGORY_DIRECT_ONLY),
        ("Both sources merged", CATEGORY_BOTH),
        ("Collected but watcher-ineligible", CATEGORY_REJECTED),
        ("Companies with no postings from any source", CATEGORY_NO_POSTINGS),
    )
    for label, category in labels:
        print(f"{label}: {report.counts.get(category, 0)}", file=output)
    github = report.health.get("github_feeds", [])
    if isinstance(github, list):
        print(
            "GitHub feeds healthy/failed: "
            f"{sum(item.get('succeeded') is True for item in github)}/"
            f"{sum(item.get('succeeded') is False for item in github)}",
            file=output,
        )
    for category in CATEGORIES:
        examples = [
            entry for entry in report.entries if entry.category == category
        ][: max(0, example_limit)]
        if not examples:
            continue
        print("", file=output)
        print(f"{category} examples:", file=output)
        for entry in examples:
            suffix = f" — {entry.final_reason}" if entry.final_reason else ""
            title = f" — {entry.title}" if entry.title else ""
            print(f"  - {entry.company}{title}{suffix}", file=output)


def render_markdown(
    report: SourceComparisonReport,
    *,
    example_limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> str:
    lines = [
        "## Source comparison",
        "",
        "| Category | Count |",
        "|---|---:|",
        f"| GitHub-only eligible postings | {report.counts.get(CATEGORY_GITHUB_ONLY, 0)} |",
        f"| Direct-only eligible postings | {report.counts.get(CATEGORY_DIRECT_ONLY, 0)} |",
        f"| Both sources merged | {report.counts.get(CATEGORY_BOTH, 0)} |",
        f"| Collected but watcher-ineligible | {report.counts.get(CATEGORY_REJECTED, 0)} |",
        f"| Companies with no postings from any source | {report.counts.get(CATEGORY_NO_POSTINGS, 0)} |",
    ]
    github = report.health.get("github_feeds", [])
    if isinstance(github, list):
        lines.extend(
            [
                "",
                f"GitHub feeds healthy/failed: "
                f"{sum(item.get('succeeded') is True for item in github)} / "
                f"{sum(item.get('succeeded') is False for item in github)}.",
            ]
        )
    for category in CATEGORIES:
        examples = [
            entry for entry in report.entries if entry.category == category
        ][: max(0, example_limit)]
        if not examples:
            continue
        lines.extend(["", f"### {category}", ""])
        for entry in examples:
            title = f" — {entry.title}" if entry.title else ""
            lines.append(
                f"- {sanitize_error(entry.company)}{sanitize_error(title)} "
                f"(`{sanitize_error(entry.final_reason)}`)"
            )
    return "\n".join(lines) + "\n"


def write_json(
    report: SourceComparisonReport,
    path: str | Path,
    *,
    example_limit: int | None = None,
) -> None:
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = report.as_dict(example_limit=example_limit)
    report_path.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )


class SourceComparisonStore:
    """Persist sanitized snapshots independently of internship seen rows."""

    def __init__(
        self,
        path: str | Path,
        *,
        run_retention: int = DEFAULT_RUN_RETENTION,
        detail_run_retention: int = DEFAULT_DETAIL_RUN_RETENTION,
        max_postings_per_run: int = DEFAULT_MAX_POSTINGS_PER_RUN,
        routine_rejection_sample_limit: int = DEFAULT_ROUTINE_REJECTION_SAMPLE_LIMIT,
        initialize: bool = True,
        read_only: bool = False,
    ):
        self.path = Path(path)
        self.read_only = read_only
        self.run_retention = max(1, int(run_retention))
        self.detail_run_retention = max(1, int(detail_run_retention))
        self.max_postings_per_run = max(1, int(max_postings_per_run))
        # Kept as a constructor argument for compatible callers. The report
        # builder is now the only owner of routine-rejection sampling.
        _ = routine_rejection_sample_limit
        if read_only and self.path.is_file():
            self._conn = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True,
            )
        elif read_only:
            self._conn = sqlite3.connect(":memory:")
        else:
            if self.path.parent:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        if initialize and not read_only:
            self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def save(self, report: SourceComparisonReport) -> None:
        if self.read_only:
            raise RuntimeError("SourceComparisonStore was opened read-only")
        safe_run_id_value = safe_run_id(report.run_id)
        summary_json = json.dumps(
            {
                "counts": report.counts,
                "aggregates": _aggregate_counts(
                    report.counts,
                    report.health,
                ),
                "health": _sanitize_trace(report.health),
                "schema_version": report.schema_version,
                "postings_evaluated": report.postings_evaluated,
                "detail_entries_retained": min(
                    len(report.entries),
                    self.max_postings_per_run,
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        deleted_rows = 0
        with self._conn:
            self._conn.execute(
                """
                insert into source_comparison_runs(
                  run_id, observed_at, summary_json
                ) values (?, ?, ?)
                on conflict(run_id) do update set
                  observed_at=excluded.observed_at,
                  summary_json=excluded.summary_json
                """,
                (
                    safe_run_id_value,
                    sanitize_error(report.observed_at),
                    summary_json,
                ),
            )
            cursor = self._conn.execute(
                "delete from source_comparison_postings where run_id = ?",
                (safe_run_id_value,),
            )
            deleted_rows += max(0, cursor.rowcount)
            # Selection belongs to the report builder. This slice is only a
            # defensive persistence ceiling for malformed external reports.
            persisted_entries = report.entries[: self.max_postings_per_run]
            self._insert_entries(safe_run_id_value, persisted_entries)
            deleted_rows += self._prune()
        self._compact_if_needed(deleted_rows=deleted_rows)

    def latest_report(self) -> SourceComparisonReport | None:
        table = self._conn.execute(
            """
            select 1
            from sqlite_master
            where type = 'table' and name = 'source_comparison_runs'
            """
        ).fetchone()
        if table is None:
            return None
        run = self._conn.execute(
            """
            select run_id, observed_at, summary_json
            from source_comparison_runs
            order by observed_at desc, run_id desc
            limit 1
            """
        ).fetchone()
        if run is None:
            return None
        rows = self._conn.execute(
            """
            select *
            from source_comparison_postings
            where run_id = ?
            order by sequence
            """,
            (run["run_id"],),
        ).fetchall()
        entries = tuple(_entry_from_row(row) for row in rows)
        summary_payload = json.loads(run["summary_json"])
        if "counts" in summary_payload:
            counts = summary_payload["counts"]
            health = summary_payload.get("health", {})
            aggregates = summary_payload.get("aggregates", {})
            schema_version = int(summary_payload.get("schema_version", 1))
            postings_evaluated = int(
                summary_payload.get(
                    "postings_evaluated",
                    sum(
                        int(counts.get(category, 0))
                        for category in CATEGORIES
                        if category != CATEGORY_NO_POSTINGS
                    ),
                )
            )
            detail_entries_retained = int(
                summary_payload.get("detail_entries_retained", len(entries))
            )
        else:  # schema-version-one snapshots written before health metadata
            counts = summary_payload
            health = {}
            aggregates = {}
            schema_version = 1
            postings_evaluated = sum(
                int(counts.get(category, 0))
                for category in CATEGORIES
                if category != CATEGORY_NO_POSTINGS
            )
            detail_entries_retained = len(entries)
        return SourceComparisonReport(
            schema_version=schema_version,
            run_id=run["run_id"],
            observed_at=run["observed_at"],
            counts={str(key): int(value) for key, value in counts.items()},
            entries=entries,
            health=dict(health),
            aggregates={
                str(key): int(value)
                for key, value in aggregates.items()
            } or _aggregate_counts(counts, health),
            postings_evaluated=postings_evaluated,
            detail_entries_retained=detail_entries_retained,
        )

    def run_count(self) -> int:
        return int(
            self._conn.execute(
                "select count(*) from source_comparison_runs"
            ).fetchone()[0]
        )

    def detail_run_count(self) -> int:
        return int(
            self._conn.execute(
                "select count(distinct run_id) from source_comparison_postings"
            ).fetchone()[0]
        )

    def _prune(self) -> int:
        deleted_rows = 0
        retained_runs = [
            row[0]
            for row in self._conn.execute(
                """
                select run_id
                from source_comparison_runs
                order by observed_at desc, run_id desc
                limit ?
                """,
                (self.run_retention,),
            )
        ]
        if retained_runs:
            placeholders = ",".join("?" for _ in retained_runs)
            cursor = self._conn.execute(
                f"delete from source_comparison_runs where run_id not in ({placeholders})",
                retained_runs,
            )
            deleted_rows += max(0, cursor.rowcount)
        detail_runs = retained_runs[: self.detail_run_retention]
        if detail_runs:
            placeholders = ",".join("?" for _ in detail_runs)
            cursor = self._conn.execute(
                f"delete from source_comparison_postings where run_id not in ({placeholders})",
                detail_runs,
            )
            deleted_rows += max(0, cursor.rowcount)
        return deleted_rows

    def _compact_if_needed(self, *, deleted_rows: int) -> bool:
        if deleted_rows < DEFAULT_COMPACTION_MIN_DELETED_ROWS:
            return False
        page_count = int(
            self._conn.execute("pragma page_count").fetchone()[0]
        )
        free_pages = int(
            self._conn.execute("pragma freelist_count").fetchone()[0]
        )
        if (
            page_count
            and free_pages / page_count
            >= DEFAULT_COMPACTION_FREE_PAGE_RATIO
        ):
            self._conn.execute("vacuum")
            return True
        return False

    def _insert_entries(
        self,
        run_id: str,
        entries: Sequence[SourceComparisonEntry],
    ) -> None:
        for sequence, entry in enumerate(entries):
            self._conn.execute(
                """
                insert into source_comparison_postings(
                  run_id, sequence, category, company, title, identity_key,
                  analyzed_job_id, source_kinds_json, final_reason,
                  direct_status, direct_coverage,
                  github_backstop_available, trace_json
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    sequence,
                    entry.category,
                    sanitize_error(entry.company),
                    sanitize_error(entry.title),
                    sanitize_error(entry.identity_key),
                    sanitize_error(entry.analyzed_job_id),
                    json.dumps(
                        tuple(
                            sanitize_error(source)
                            for source in entry.source_kinds
                        ),
                        separators=(",", ":"),
                    ),
                    sanitize_error(entry.final_reason),
                    sanitize_error(entry.direct_status),
                    sanitize_error(entry.direct_coverage),
                    (
                        None
                        if entry.github_backstop_available is None
                        else int(entry.github_backstop_available)
                    ),
                    json.dumps(
                        entry.trace,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                ),
            )

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            create table if not exists source_comparison_runs(
              run_id text primary key,
              observed_at text not null,
              summary_json text not null
            );
            create table if not exists source_comparison_postings(
              run_id text not null,
              sequence integer not null,
              category text not null,
              company text not null,
              title text not null,
              identity_key text not null,
              analyzed_job_id text not null,
              source_kinds_json text not null,
              final_reason text not null,
              direct_status text,
              direct_coverage text,
              github_backstop_available integer,
              trace_json text not null,
              primary key(run_id, sequence),
              foreign key(run_id) references source_comparison_runs(run_id)
                on delete cascade
            );
            create index if not exists source_comparison_postings_identity_idx
              on source_comparison_postings(identity_key);
            create index if not exists source_comparison_postings_company_idx
              on source_comparison_postings(company);
            """
        )
        self._conn.commit()


def select_comparison_details(
    summaries: Sequence[PostingComparisonSummary],
    *,
    policy: SourceComparisonDetailPolicy,
) -> tuple[PostingComparisonSummary, ...]:
    """Apply the report-owned retention policy to lightweight summaries."""

    return _select_details(
        summaries,
        routine_sample_limit=max(
            0,
            int(policy.routine_rejection_sample_limit),
        ),
        limit=max(1, int(policy.maximum_retained_details)),
    )


def _select_detail_entries(
    entries: Sequence[SourceComparisonEntry],
    *,
    routine_sample_limit: int,
    limit: int,
) -> tuple[SourceComparisonEntry, ...]:
    """Compatibility helper for validating selection against legacy entries."""

    return _select_details(
        entries,
        routine_sample_limit=routine_sample_limit,
        limit=limit,
    )


def _select_details(entries, *, routine_sample_limit: int, limit: int):
    important = []
    routine = {}
    for entry in entries:
        if _is_routine_rejection(entry):
            routine.setdefault(entry.final_reason, []).append(entry)
        else:
            important.append(entry)
    sampled = [
        entry
        for reason in sorted(routine)
        for entry in sorted(routine[reason], key=_stable_sample_key)[
            :routine_sample_limit
        ]
    ]
    candidates = [*important, *sampled]
    if len(candidates) > limit:
        candidates = sorted(
            candidates,
            key=lambda entry: (
                _detail_priority(entry),
                _stable_sample_key(entry),
            ),
        )[:limit]
    return tuple(sorted(candidates, key=_detail_display_key))


def _is_routine_rejection(
    entry: SourceComparisonEntry | PostingComparisonSummary,
) -> bool:
    return bool(
        entry.category == CATEGORY_REJECTED
        and entry.final_reason in ROUTINE_REJECTION_REASONS
        and not _has_operational_anomaly(entry)
    )


def _has_operational_anomaly(
    entry: SourceComparisonEntry | PostingComparisonSummary,
) -> bool:
    if entry.direct_status in {"degraded", "failing"}:
        return True
    if entry.direct_coverage and any(
        marker in entry.direct_coverage
        for marker in ("degraded", "failing", "uncovered")
    ):
        return True
    if isinstance(entry, PostingComparisonSummary):
        return bool(
            entry.generic_or_shared_url
            or entry.deduplicated_into_another
            or entry.duplicate_sightings
            or entry.has_merge_diagnostics
        )
    identity = entry.trace.get("identity")
    if isinstance(identity, Mapping) and identity.get("generic_or_shared_url"):
        return True
    dedupe = entry.trace.get("deduplication")
    if not isinstance(dedupe, Mapping):
        return False
    return bool(
        dedupe.get("deduplicated_into_another")
        or dedupe.get("duplicate_sightings")
        or dedupe.get("merge_diagnostics")
    )


def _detail_priority(
    entry: SourceComparisonEntry | PostingComparisonSummary,
) -> int:
    if entry.category in {
        CATEGORY_GITHUB_ONLY,
        CATEGORY_DIRECT_ONLY,
        CATEGORY_BOTH,
    }:
        return 0
    if _has_operational_anomaly(entry):
        return 1
    if entry.category == CATEGORY_NO_POSTINGS:
        return 2
    if entry.final_reason not in ROUTINE_REJECTION_REASONS:
        return 3
    return 4


def _stable_sample_key(
    entry: SourceComparisonEntry | PostingComparisonSummary,
) -> tuple[str, ...]:
    seed = "\x1f".join(
        (
            entry.identity_key,
            entry.analyzed_job_id,
            entry.company.casefold(),
            entry.title.casefold(),
            entry.final_reason,
        )
    )
    return (
        hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        entry.identity_key,
        entry.analyzed_job_id,
        entry.company.casefold(),
        entry.title.casefold(),
    )


def _detail_display_key(
    entry: SourceComparisonEntry | PostingComparisonSummary,
) -> tuple[object, ...]:
    category_index = (
        CATEGORIES.index(entry.category)
        if entry.category in CATEGORIES
        else len(CATEGORIES)
    )
    return (
        category_index,
        entry.company.casefold(),
        entry.title.casefold(),
        entry.identity_key,
        entry.final_reason,
    )


def _entry_from_row(row: sqlite3.Row) -> SourceComparisonEntry:
    return SourceComparisonEntry(
        category=row["category"],
        company=row["company"],
        title=row["title"],
        identity_key=row["identity_key"],
        analyzed_job_id=row["analyzed_job_id"],
        source_kinds=tuple(json.loads(row["source_kinds_json"])),
        final_reason=row["final_reason"],
        direct_status=row["direct_status"],
        direct_coverage=row["direct_coverage"],
        github_backstop_available=(
            None
            if row["github_backstop_available"] is None
            else bool(row["github_backstop_available"])
        ),
        trace=json.loads(row["trace_json"]),
    )


def _optional_string(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _optional_sanitized_string(value: object) -> str | None:
    text = _optional_string(value)
    return None if text is None else sanitize_error(text)


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _sanitize_trace(trace: Mapping[str, object]) -> dict[str, object]:
    """Remove secrets and bound arbitrary source text before persistence."""

    return {
        str(key): _sanitize_trace_value(item, field_name=str(key))
        for key, item in trace.items()
    }


def _sanitize_trace_value(value: object, *, field_name: str) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _sanitize_trace_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_trace_value(item, field_name=field_name)
            for item in value
        ]
    if isinstance(value, str):
        if field_name == "url" or field_name.endswith("_url"):
            return safe_posting_url(value)
        return sanitize_error(value)
    return value


def _aggregate_counts(
    counts: Mapping[str, object],
    health: Mapping[str, object],
) -> dict[str, int]:
    direct_sources = health.get("direct_sources")
    if not isinstance(direct_sources, list):
        direct_sources = []
    github_feeds = health.get("github_feeds")
    if not isinstance(github_feeds, list):
        github_feeds = []
    return {
        "github_only_eligible": int(counts.get(CATEGORY_GITHUB_ONLY, 0)),
        "direct_only_eligible": int(counts.get(CATEGORY_DIRECT_ONLY, 0)),
        "both_found_and_merged": int(counts.get(CATEGORY_BOTH, 0)),
        "collected_rejected": int(counts.get(CATEGORY_REJECTED, 0)),
        "no_postings": int(counts.get(CATEGORY_NO_POSTINGS, 0)),
        "direct_healthy": sum(
            item.get("attempted") is True
            and item.get("succeeded") is True
            and int(item.get("rows_returned") or 0) > 0
            for item in direct_sources
            if isinstance(item, Mapping)
        ),
        "direct_empty": sum(
            item.get("attempted") is True
            and item.get("succeeded") is True
            and int(item.get("rows_returned") or 0) == 0
            for item in direct_sources
            if isinstance(item, Mapping)
        ),
        "direct_failed": sum(
            item.get("attempted") is True
            and item.get("succeeded") is False
            for item in direct_sources
            if isinstance(item, Mapping)
        ),
        "direct_unsupported": sum(
            item.get("attempted") is False
            for item in direct_sources
            if isinstance(item, Mapping)
        ),
        "github_healthy": sum(
            item.get("succeeded") is True
            for item in github_feeds
            if isinstance(item, Mapping)
        ),
        "github_failed": sum(
            item.get("succeeded") is False
            for item in github_feeds
            if isinstance(item, Mapping)
        ),
    }


def _comparison_health(
    attempts: Sequence[SourceAttempt],
    states: Mapping[str, SourceHealthState],
) -> dict[str, object]:
    direct_sources = []
    github_feeds = []
    for attempt in attempts:
        state = states.get(attempt.health_key)
        if attempt.source_kind == SOURCE_KIND_DIRECT:
            direct_sources.append(
                {
                    "health_key": attempt.health_key,
                    "company": sanitize_error(attempt.company),
                    "adapter": attempt.adapter,
                    "attempted": attempt.attempted,
                    "succeeded": attempt.succeeded,
                    "rows_returned": attempt.rows_returned,
                    "status": state.status if state else "unknown",
                    "unsupported_reason": attempt.unsupported_reason,
                    "error_kind": (
                        safe_error_kind(attempt.error_kind)
                        if attempt.error_kind
                        else None
                    ),
                }
            )
            continue
        if attempt.source_kind != SOURCE_KIND_GITHUB_FEED:
            continue
        github_feeds.append(
            {
                "health_key": attempt.health_key,
                "feed_label": sanitize_error(
                    attempt.feed_label or attempt.adapter
                ),
                "attempted": attempt.attempted,
                "succeeded": attempt.succeeded,
                "rows_returned": attempt.rows_returned,
                "status": state.status if state else "unknown",
                "error_kind": (
                    safe_error_kind(attempt.error_kind)
                    if attempt.error_kind
                    else None
                ),
            }
        )
    github_feeds.sort(
        key=lambda item: (
            item["succeeded"] is not False,
            str(item["feed_label"]).casefold(),
        )
    )
    direct_sources.sort(
        key=lambda item: (
            item["succeeded"] is not False,
            str(item["company"]).casefold(),
        )
    )
    return {
        "direct_sources": direct_sources,
        "github_feeds": github_feeds,
        "github_feeds_failed": sum(
            item["succeeded"] is False for item in github_feeds
        ),
        "github_feeds_healthy": sum(
            item["succeeded"] is True for item in github_feeds
        ),
    }
