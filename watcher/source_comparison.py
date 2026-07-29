"""Sanitized, bounded direct-versus-GitHub source comparison reports."""

from __future__ import annotations

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
    build_posting_audit_context,
    build_posting_trace,
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
DEFAULT_MAX_POSTINGS_PER_RUN = 20_000


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
class SourceComparisonReport:
    schema_version: int
    run_id: str
    observed_at: str
    counts: dict[str, int]
    entries: tuple[SourceComparisonEntry, ...]
    health: dict[str, object] = field(default_factory=dict)

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
            "health": dict(self.health),
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
) -> SourceComparisonReport:
    """Classify each retained production posting exactly once."""

    coverage_by_company: dict[str, dict[str, object]] = {}
    config_by_name = {
        norm_company(company.name): company
        for company in config.companies
    }
    for item in coverage:
        data = asdict(item)
        company = config_by_name.get(norm_company(item.company))
        names = company.match_names() if company else (item.company,)
        for name in names:
            coverage_by_company[norm_company(name)] = data
    audit_context = build_posting_audit_context(
        jobs,
        seen_store=seen_store,
    )
    entries: list[SourceComparisonEntry] = []
    observed_companies: set[str] = set()
    for job in jobs:
        company_key = norm_company(str(job.get("company") or ""))
        observed_companies.add(company_key)
        trace = build_posting_trace(
            job,
            config=config,
            seen_store=seen_store,
            posting_universe=jobs,
            duplicate_entries=duplicate_report,
            source_coverage=coverage_by_company.get(company_key),
            context=audit_context,
        )
        trace_data = _sanitize_trace(trace.as_dict())
        sources = tuple(str(value) for value in trace.collection.get("sources", ()))
        reason = str(trace.final_result.get("reason") or "unknown")
        category = comparison_category(sources, reason)
        company_coverage = trace_data["watchlist_match"]
        entries.append(
            SourceComparisonEntry(
                category=category,
                company=str(trace_data["posting"].get("company") or ""),
                title=str(trace_data["posting"].get("title") or ""),
                identity_key=str(
                    trace_data["identity"].get("canonical_identity_key") or ""
                ),
                analyzed_job_id=str(
                    trace_data["posting"].get("analyzed_job_id") or ""
                ),
                source_kinds=tuple(sanitize_error(source) for source in sources),
                final_reason=sanitize_error(reason),
                direct_status=_optional_string(
                    company_coverage.get("direct_status")
                ),
                direct_coverage=_optional_string(
                    company_coverage.get("direct_coverage")
                ),
                github_backstop_available=_optional_bool(
                    company_coverage.get("github_backstop_available")
                ),
                trace=trace_data,
            )
        )

    for company in config.companies:
        if any(
            norm_company(name) in observed_companies
            for name in company.match_names()
        ):
            continue
        company_coverage = coverage_by_company.get(norm_company(company.name), {})
        no_posting_trace = not_collected_trace(
            AuditQuery(company=company.name),
            config=config,
        ).as_dict()
        no_posting_trace["watchlist_match"].update(
            {
                "direct_coverage": company_coverage.get("state"),
                "direct_status": company_coverage.get("direct_status"),
                "github_backstop_available": company_coverage.get(
                    "github_backstop_available"
                ),
            }
        )
        entries.append(
            SourceComparisonEntry(
                category=CATEGORY_NO_POSTINGS,
                company=company.name,
                title="",
                identity_key="",
                analyzed_job_id="",
                source_kinds=(),
                final_reason="not_collected",
                direct_status=_optional_string(
                    company_coverage.get("direct_status")
                ),
                direct_coverage=_optional_string(
                    company_coverage.get("state")
                ),
                github_backstop_available=_optional_bool(
                    company_coverage.get("github_backstop_available")
                ),
                trace=no_posting_trace,
            )
        )

    entries.sort(
        key=lambda entry: (
            CATEGORIES.index(entry.category),
            entry.company.casefold(),
            entry.title.casefold(),
            entry.identity_key,
        )
    )
    counts = {
        category: sum(entry.category == category for entry in entries)
        for category in CATEGORIES
    }
    return SourceComparisonReport(
        schema_version=1,
        run_id=safe_run_id(run_id),
        observed_at=iso_utc(observed_at),
        counts=counts,
        entries=tuple(entries),
        health=_comparison_health(
            source_attempts,
            source_health_states or {},
        ),
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
    report_path.write_text(
        json.dumps(
            report.as_dict(example_limit=example_limit),
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
        initialize: bool = True,
        read_only: bool = False,
    ):
        self.path = Path(path)
        self.read_only = read_only
        self.run_retention = max(1, int(run_retention))
        self.detail_run_retention = max(1, int(detail_run_retention))
        self.max_postings_per_run = max(1, int(max_postings_per_run))
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
                "health": _sanitize_trace(report.health),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
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
            self._conn.execute(
                "delete from source_comparison_postings where run_id = ?",
                (safe_run_id_value,),
            )
            for sequence, entry in enumerate(
                report.entries[: self.max_postings_per_run]
            ):
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
                        safe_run_id_value,
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
                            _sanitize_trace(entry.trace),
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ),
                    ),
                )
            self._prune()

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
        else:  # schema-version-one snapshots written before health metadata
            counts = summary_payload
            health = {}
        return SourceComparisonReport(
            schema_version=1,
            run_id=run["run_id"],
            observed_at=run["observed_at"],
            counts={str(key): int(value) for key, value in counts.items()},
            entries=entries,
            health=dict(health),
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

    def _prune(self) -> None:
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
            self._conn.execute(
                f"delete from source_comparison_runs where run_id not in ({placeholders})",
                retained_runs,
            )
        detail_runs = retained_runs[: self.detail_run_retention]
        if detail_runs:
            placeholders = ",".join("?" for _ in detail_runs)
            self._conn.execute(
                f"delete from source_comparison_postings where run_id not in ({placeholders})",
                detail_runs,
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
