"""CLI and reusable orchestration for watcher posting audits."""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from backend.app.ingest import analyze_rows
from backend.app.dedupe import norm_company, norm_title
from watcher.company_matching import company_matching_key
from watcher.audit_trace import (
    AuditQuery,
    PostingAuditTrace,
    build_posting_audit_context,
    build_posting_trace,
    enrich_duplicate_entries,
    evaluate_posting_outcome,
    match_watchlist_company,
    not_collected_trace,
    query_matches_trace,
)
from watcher.config import (
    DEFAULT_WATCHLIST_PATH,
    WatcherConfig,
    load_watchlist,
    resolve_analysis_cache_path,
)
from watcher.run import CollectionStats, collect_rows
from watcher.generation import TRIGGER_SUSTAINED_ABSENCE
from watcher.seen_store import SeenStore
from watcher.source_comparison import (
    SourceComparisonReport,
    SourceComparisonStore,
    _sanitize_trace,
    build_source_comparison,
    index_company_coverage,
    render_console as render_comparison_console,
    render_markdown as render_comparison_markdown,
    write_json as write_comparison_json,
)
from watcher.source_health import (
    calculate_company_coverage,
    calculate_next_state,
    utc_datetime,
)

DEFAULT_LIMIT = 25


def audit_state_only(
    *,
    config: WatcherConfig,
    seen_store: SeenStore,
    query: AuditQuery,
    limit: int = DEFAULT_LIMIT,
) -> list[PostingAuditTrace]:
    """Search the latest persisted sanitized comparison snapshot."""

    effective_query = _canonicalize_company_query(query, config)
    with SourceComparisonStore(
        seen_store.path,
        initialize=False,
        read_only=True,
    ) as store:
        report = store.latest_report()
    traces: list[PostingAuditTrace] = []
    if report is not None:
        for entry in report.entries:
            if not entry.trace:
                continue
            matched, fields = query_matches_trace(entry.trace, effective_query)
            if not matched:
                continue
            data = copy.deepcopy(entry.trace)
            data["query_match"] = {
                "query": query.as_dict(),
                "matched_fields": fields,
                "mode": "state_only",
            }
            _apply_duplicate_query_result(data, fields)
            traces.append(_trace_from_mapping(data))
            if len(traces) >= limit:
                break

    if not traces:
        traces.extend(
            _seen_only_matches(
                seen_store=seen_store,
                query=effective_query,
                original_query=query,
                config=config,
                limit=limit,
            )
        )
    return traces or [not_collected_trace(query, config=config)]


def audit_live(
    *,
    config: WatcherConfig,
    seen_store: SeenStore,
    query: AuditQuery,
    limit: int = DEFAULT_LIMIT,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    observed_at: datetime | None = None,
) -> tuple[list[PostingAuditTrace], SourceComparisonReport]:
    """Collect and analyze without email, alumni loading, health writes, or seen writes."""

    timestamp = utc_datetime(observed_at or datetime.now(timezone.utc))
    run_id = f"audit-{timestamp.strftime('%Y%m%dT%H%M%SZ')}"
    stats = CollectionStats()
    rows, _errors = collect_rows(
        config,
        direct_sources=direct_sources,
        github_source=github_source,
        stats=stats,
        run_id=run_id,
        observed_at=timestamp,
    )
    rows_for_analysis = copy.deepcopy(rows)
    jobs, duplicate_report = analyze_rows(
        rows_for_analysis,
        include_dedupe_report=True,
        include_audit_diagnostics=True,
    )
    duplicate_report = enrich_duplicate_entries(
        rows_for_analysis,
        duplicate_report,
    )
    states = {
        attempt.health_key: calculate_next_state(None, attempt)
        for attempt in stats.source_attempts
    }
    coverage = calculate_company_coverage(
        config.companies,
        stats.source_attempts,
        states,
    )
    report = build_source_comparison(
        config=config,
        jobs=jobs,
        seen_store=seen_store,
        run_id=run_id,
        observed_at=timestamp,
        duplicate_report=duplicate_report,
        coverage=coverage,
        source_attempts=stats.source_attempts,
        source_health_states=states,
    )
    effective_query = _canonicalize_company_query(query, config)
    traces: list[PostingAuditTrace] = []
    for entry in report.entries:
        if not entry.trace:
            continue
        matched, fields = query_matches_trace(entry.trace, effective_query)
        if not matched:
            continue
        data = copy.deepcopy(entry.trace)
        data["query_match"] = {
            "query": query.as_dict(),
            "matched_fields": fields,
            "mode": "live_read_only",
        }
        _apply_duplicate_query_result(data, fields)
        traces.append(_trace_from_mapping(data))
        if len(traces) >= limit:
            break
    if len(traces) < limit:
        returned_identity_keys = {
            str(trace.identity.get("canonical_identity_key") or "")
            for trace in traces
        }
        returned_identity_keys.discard("")
        audit_context = build_posting_audit_context(
            jobs,
            seen_store=seen_store,
            duplicate_entries=duplicate_report,
        )
        coverage_by_company = index_company_coverage(config, coverage)
        for job_index, job in enumerate(jobs):
            if not _job_may_match_live_query(job, effective_query, config):
                continue
            company_key = norm_company(str(job.get("company") or ""))
            outcome = evaluate_posting_outcome(
                job,
                config=config,
                seen_store=seen_store,
                posting_universe=jobs,
                duplicate_entries=duplicate_report,
                source_coverage=coverage_by_company.get(company_key),
                context=audit_context,
                job_index=job_index,
            )
            if outcome.identity_key in returned_identity_keys:
                continue
            trace = build_posting_trace(
                job,
                config=config,
                seen_store=seen_store,
                posting_universe=jobs,
                duplicate_entries=duplicate_report,
                source_coverage=outcome.source_coverage,
                context=audit_context,
                outcome=outcome,
            )
            data = _sanitize_trace(trace.as_dict())
            matched, fields = query_matches_trace(data, effective_query)
            if not matched:
                continue
            data["query_match"] = {
                "query": query.as_dict(),
                "matched_fields": fields,
                "mode": "live_read_only",
            }
            _apply_duplicate_query_result(data, fields)
            traces.append(_trace_from_mapping(data))
            returned_identity_keys.add(outcome.identity_key)
            if len(traces) >= limit:
                break
    return traces or [not_collected_trace(query, config=config)], report


def _job_may_match_live_query(
    job: Mapping[str, object],
    query: AuditQuery,
    config: WatcherConfig,
) -> bool:
    """Cheaply reject jobs that cannot satisfy the requested rich trace."""

    if query.company:
        configured = match_watchlist_company(
            str(job.get("company") or ""),
            config.companies,
        )
        company_names = {
            company_matching_key(str(job.get("company") or "")),
            company_matching_key(configured.name) if configured else "",
        }
        if company_matching_key(query.company) not in company_names:
            return False
    if query.title and norm_title(query.title) not in norm_title(
        str(job.get("title") or "")
    ):
        return False
    if query.job_id and str(job.get("id") or "") != str(query.job_id).strip():
        return False
    return True


def render_audit_console(
    traces: Sequence[PostingAuditTrace],
    *,
    output: TextIO | None = None,
    limit: int = DEFAULT_LIMIT,
) -> None:
    output = output or sys.stdout
    bounded = list(traces[: max(1, limit)])
    if len(bounded) > 1:
        print(
            f"Multiple matches found: {len(bounded)}"
            + (
                " (bounded by --limit)"
                if len(traces) > len(bounded) or len(bounded) >= limit
                else ""
            ),
            file=output,
        )
        print("", file=output)
    for index, trace in enumerate(bounded, start=1):
        if len(bounded) > 1:
            print(f"Match {index}", file=output)
        _render_trace(trace, output)
        if index != len(bounded):
            print("", file=output)


def _render_trace(trace: PostingAuditTrace, output: TextIO) -> None:
    posting = trace.posting
    print(f"Company: {posting.get('company') or '(unknown)'}", file=output)
    print(f"Title: {posting.get('title') or '(unknown)'}", file=output)
    print(
        "Canonical identity: "
        f"{trace.identity.get('canonical_identity_key') or '(unknown)'}",
        file=output,
    )
    print("", file=output)
    sections = (
        ("Collection", trace.collection),
        ("Watchlist", trace.watchlist_match),
        ("Identity", trace.identity),
        ("Deduplication", trace.deduplication),
        ("Season", trace.season),
        ("Internship / co-op", trace.internship_status),
        ("Open status", trace.open_status),
        ("Location", trace.location),
        ("Role", trace.role),
        ("Watcher eligibility", trace.watcher_eligibility),
        ("Scoring", trace.scoring),
        ("Notification", trace.notification),
    )
    for label, values in sections:
        print(label, file=output)
        if not values:
            print("  status: unknown in state-only data", file=output)
            continue
        for key, value in values.items():
            if key in {"records", "source_details", "duplicates"}:
                if value:
                    print(f"  {key}: {json.dumps(value, sort_keys=True)}", file=output)
                continue
            print(f"  {key}: {_console_value(value)}", file=output)
    print("Final result", file=output)
    print(f"  reason: {trace.final_result.get('reason')}", file=output)
    print(f"  {trace.final_result.get('summary')}", file=output)


def render_shadow_generation_events(
    events: Sequence[Mapping[str, object]],
    *,
    limit: int = DEFAULT_LIMIT,
    output: TextIO | None = None,
) -> None:
    """Print bounded persisted shadow-generation events, newest first.

    These are diagnostics for reused requisitions and evergreen postings. They
    never influenced notification selection and are not being acted on.
    """

    stream = output or sys.stdout
    print("Persisted shadow-generation events (diagnostic only):", file=stream)
    print(f"  Events shown: {len(events)} (limit {limit})", file=stream)
    if not events:
        print("  No shadow-generation events have been recorded.", file=stream)
        return
    for event in events:
        detail = (
            f"absence_days={event['absence_days']}"
            if event.get("trigger") == TRIGGER_SUSTAINED_ABSENCE
            else f"season {event.get('stored_season_key')} -> "
            f"{event.get('current_season_key')}"
        )
        print(
            f"  - {event.get('observed_at')} {event.get('company') or '(unknown)'}: "
            f"{event.get('trigger')} generation {event.get('current_generation')} -> "
            f"{event.get('proposed_generation')} ({detail})",
            file=stream,
        )
        print(
            f"      identity={event.get('identity_key') or '(none)'} "
            f"absence_epoch={event.get('absence_epoch')} "
            f"event_id={event.get('event_id')}",
            file=stream,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Explain why a watched posting was or was not emailed. "
            "The safe default reads persisted state and makes no network requests."
        )
    )
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH))
    parser.add_argument("--seen-db", help="SQLite watcher state path")
    parser.add_argument("--company")
    parser.add_argument("--title")
    parser.add_argument("--url")
    parser.add_argument("--requisition-id")
    parser.add_argument("--job-id")
    parser.add_argument("--identity", help="Canonical identity or fallback key")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help="Perform a fresh read-only collection and analysis.",
    )
    mode.add_argument(
        "--state-only",
        action="store_true",
        help="Read only the SQLite snapshot (default).",
    )
    parser.add_argument(
        "--json",
        nargs="?",
        const="-",
        metavar="PATH",
        help="Write stable JSON to PATH, or stdout when no path is supplied.",
    )
    parser.add_argument(
        "--comparison",
        action="store_true",
        help="Render the current source-comparison report instead of posting traces.",
    )
    parser.add_argument(
        "--shadow-generations",
        action="store_true",
        help=(
            "Print persisted shadow-generation events, newest first. "
            "Read-only: makes no network requests and writes nothing."
        ),
    )
    parser.add_argument("--comparison-json", help="Write comparison JSON.")
    parser.add_argument("--comparison-markdown", help="Write comparison Markdown.")
    args = parser.parse_args(argv)
    if args.limit < 1 or args.limit > 1000:
        parser.error("--limit must be between 1 and 1000")

    query = AuditQuery(
        company=args.company,
        title=args.title,
        url=args.url,
        requisition_id=args.requisition_id,
        job_id=args.job_id,
        identity=args.identity,
    )
    if args.shadow_generations and args.live:
        parser.error("--shadow-generations is read-only and cannot be used with --live")
    if (
        not args.comparison
        and not args.shadow_generations
        and not any(query.as_dict().values())
    ):
        parser.error(
            "provide at least one posting query, --comparison, or --shadow-generations"
        )

    config = load_watchlist(args.watchlist)
    if args.seen_db:
        seen_db_path = Path(args.seen_db)
        config = replace(
            config,
            seen_db_path=seen_db_path,
            analysis_cache_path=resolve_analysis_cache_path(seen_db_path),
        )

    if args.shadow_generations:
        with SeenStore(config.seen_db_path, read_only=True) as seen_store:
            events = seen_store.shadow_generation_events(limit=args.limit)
        render_shadow_generation_events(events, limit=args.limit)
        return 0

    report: SourceComparisonReport | None = None
    with SeenStore(config.seen_db_path, read_only=True) as seen_store:
        if args.live:
            traces, report = audit_live(
                config=config,
                seen_store=seen_store,
                query=query,
                limit=args.limit,
            )
        else:
            traces = audit_state_only(
                config=config,
                seen_store=seen_store,
                query=query,
                limit=args.limit,
            )
            with SourceComparisonStore(
                seen_store.path,
                initialize=False,
                read_only=True,
            ) as comparison_store:
                report = comparison_store.latest_report()

    if args.comparison:
        if report is None:
            print(
                "No persisted source-comparison snapshot is available. Use --live.",
                file=sys.stderr,
            )
            return 1
        render_comparison_console(report, example_limit=args.limit)
        if args.comparison_json:
            write_comparison_json(report, args.comparison_json)
        if args.comparison_markdown:
            Path(args.comparison_markdown).write_text(
                render_comparison_markdown(report, example_limit=args.limit),
                encoding="utf-8",
            )
        return 0

    matched_posting_count = sum(
        trace.collection.get("collected") is not False
        for trace in traces
    )
    payload = {
        "schema_version": 1,
        "mode": "live_read_only" if args.live else "state_only",
        "query": query.as_dict(),
        "match_count": matched_posting_count,
        "ambiguous": matched_posting_count > 1,
        "limit": args.limit,
        "limit_reached": len(traces) >= args.limit,
        "results": [trace.as_dict() for trace in traces],
    }
    if args.json == "-":
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        render_audit_console(traces, limit=args.limit)
        if args.json:
            Path(args.json).write_text(
                json.dumps(
                    payload,
                    indent=2,
                    sort_keys=True,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
    if report is not None and args.comparison_json:
        write_comparison_json(report, args.comparison_json)
    if report is not None and args.comparison_markdown:
        Path(args.comparison_markdown).write_text(
            render_comparison_markdown(report, example_limit=args.limit),
            encoding="utf-8",
        )
    return 0


def _canonicalize_company_query(
    query: AuditQuery,
    config: WatcherConfig,
) -> AuditQuery:
    if not query.company:
        return query
    company = match_watchlist_company(query.company, config.companies)
    return replace(query, company=company.name) if company else query


def _seen_only_matches(
    *,
    seen_store: SeenStore,
    query: AuditQuery,
    original_query: AuditQuery,
    config: WatcherConfig,
    limit: int,
) -> list[PostingAuditTrace]:
    traces: list[PostingAuditTrace] = []
    for record in seen_store.records():
        if not _seen_record_matches(record, query, config=config):
            continue
        reason = (
            "already_emailed"
            if record.get("emailed_at")
            else "explicitly_primed"
            if record.get("primed_at")
            else "pending"
        )
        traces.append(
            _trace_from_seen_record(
                record,
                original_query=original_query,
                reason=reason,
            )
        )
        if len(traces) >= limit:
            break
    return traces


def _seen_record_matches(
    record: Mapping[str, object],
    query: AuditQuery,
    *,
    config: WatcherConfig,
) -> bool:
    if query.company:
        record_company = str(record.get("company") or "")
        configured = match_watchlist_company(record_company, config.companies)
        if (
            query.company.casefold() not in record_company.casefold()
            and (
                configured is None
                or configured.name.casefold() != query.company.casefold()
            )
        ):
            return False
    if query.title and query.title.casefold() not in str(
        record.get("title") or ""
    ).casefold():
        return False
    if query.url:
        from backend.app.dedupe import (
            norm_url,
            posting_specific_url_key,
            stable_requisition_key,
        )

        query_requisition = stable_requisition_key({"source_url": query.url})
        stored_url = str(record.get("url") or "")
        query_url_key = posting_specific_url_key({"source_url": query.url})
        stored_url_key = posting_specific_url_key({"source_url": stored_url})
        stored_requisition = str(record.get("requisition_key") or "")
        if not stored_requisition:
            stored_requisition = stable_requisition_key(
                {"source_url": stored_url}
            )
        if query_url_key or stored_url_key:
            if query_url_key != stored_url_key:
                return False
        elif query_requisition or stored_requisition:
            if query_requisition != stored_requisition:
                return False
        elif norm_url(query.url) != norm_url(stored_url):
            return False
    if query.requisition_id:
        value = str(record.get("requisition_key") or "").casefold()
        wanted = query.requisition_id.casefold()
        if value != wanted and not value.endswith(f"|{wanted}"):
            return False
    if query.job_id and query.job_id not in {
        str(record.get("job_id") or ""),
        str(record.get("analyzed_job_id") or ""),
    }:
        return False
    if query.identity and query.identity not in {
        str(record.get("identity_key") or ""),
        str(record.get("requisition_key") or ""),
    }:
        return False
    return any(query.as_dict().values())


def _trace_from_seen_record(
    record: Mapping[str, object],
    *,
    original_query: AuditQuery,
    reason: str,
) -> PostingAuditTrace:
    from watcher.audit_trace import final_summary, safe_posting_url

    empty: dict[str, object] = {}
    return PostingAuditTrace(
        schema_version=1,
        query_match={
            "query": original_query.as_dict(),
            "matched_fields": ["historical_seen_record"],
            "mode": "state_only",
        },
        posting={
            "company": record.get("company") or "",
            "title": record.get("title") or "",
            "location": record.get("location") or "",
            "url": safe_posting_url(record.get("url")),
            "analyzed_job_id": record.get("analyzed_job_id") or "",
        },
        collection={
            "collected": "historically",
            "sources": [record.get("first_source")]
            if record.get("first_source")
            else [],
        },
        watchlist_match=empty,
        identity={
            "canonical_identity_key": record.get("identity_key"),
            "requisition_key": record.get("requisition_key"),
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
            "historical_match": True,
            "records": [
                {
                    **dict(record),
                    "url": safe_posting_url(record.get("url")),
                }
            ],
            "first_seen": record.get("first_seen"),
            "emailed": bool(record.get("emailed_at")),
            "emailed_at": record.get("emailed_at"),
            "primed": bool(record.get("primed_at")),
            "primed_at": record.get("primed_at"),
            "pending": reason == "pending",
            "stored_analyzed_job_ids": [record.get("analyzed_job_id")],
            "stored_identity_keys": [record.get("identity_key")],
            "stored_requisition_keys": [record.get("requisition_key")],
        },
        final_result={
            "reason": reason,
            "emailed_now": False,
            "summary": final_summary(reason),
        },
    )


def _trace_from_mapping(data: Mapping[str, object]) -> PostingAuditTrace:
    return PostingAuditTrace(
        schema_version=int(data.get("schema_version") or 1),
        query_match=dict(data.get("query_match") or {}),
        posting=dict(data.get("posting") or {}),
        collection=dict(data.get("collection") or {}),
        watchlist_match=dict(data.get("watchlist_match") or {}),
        identity=dict(data.get("identity") or {}),
        deduplication=dict(data.get("deduplication") or {}),
        season=dict(data.get("season") or {}),
        internship_status=dict(data.get("internship_status") or {}),
        open_status=dict(data.get("open_status") or {}),
        location=dict(data.get("location") or {}),
        role=dict(data.get("role") or {}),
        watcher_eligibility=dict(data.get("watcher_eligibility") or {}),
        scoring=dict(data.get("scoring") or {}),
        notification=dict(data.get("notification") or {}),
        final_result=dict(data.get("final_result") or {}),
    )


def _apply_duplicate_query_result(
    data: dict[str, object],
    matched_fields: Sequence[str],
) -> None:
    if "deduplicated_duplicate" not in matched_fields:
        return
    deduplication = data.get("deduplication")
    if isinstance(deduplication, dict):
        deduplication["deduplicated_into_another"] = True
        deduplication["duplicate_sighting_query"] = True
    data["final_result"] = {
        "reason": "deduplicated_duplicate",
        "emailed_now": False,
        "summary": "duplicate sighting merged into the retained posting",
    }


def _console_value(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None:
        return "unknown"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) if value else "(none)"
    if isinstance(value, Mapping):
        return json.dumps(value, sort_keys=True)
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
