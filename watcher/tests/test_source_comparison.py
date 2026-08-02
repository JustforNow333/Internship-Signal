from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from io import StringIO

import pytest

import watcher.audit_trace as audit_trace_module
import watcher.seen_store as seen_store_module
import watcher.source_comparison as source_comparison_module
from backend.app.ingest import analyze_rows
from watcher.config import CompanyCfg, WatcherConfig
from watcher.seen_store import SeenStore
from watcher.source_comparison import (
    CATEGORY_BOTH,
    CATEGORY_DIRECT_ONLY,
    CATEGORY_GITHUB_ONLY,
    CATEGORY_NO_POSTINGS,
    CATEGORY_REJECTED,
    CATEGORIES,
    PostingComparisonSummary,
    SourceComparisonDetailPolicy,
    SourceComparisonStore,
    _select_detail_entries,
    build_source_comparison,
    render_console,
    render_markdown,
    select_comparison_details,
    write_json,
)
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT_EMPTY,
    COVERAGE_UNCOVERED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    CompanyCoverage,
    SourceAttempt,
)
from watcher.sources.base import make_row


def _config(tmp_path):
    return WatcherConfig(
        companies=tuple(
            CompanyCfg(name=name, ats=ats, terms=("Summer 2027",))
            for name, ats in (
                ("GitHub Co", "github_only"),
                ("Direct Co", "greenhouse"),
                ("Both Co", "greenhouse"),
                ("Rejected Co", "greenhouse"),
                ("Unsupported Co", "bespoke"),
                ("Failed Co", "lever"),
                ("Empty Co", "ashby"),
            )
        ),
        terms=("Summer 2027",),
        seen_db_path=tmp_path / "seen.sqlite",
    )


def _row(
    company,
    source,
    adapter,
    req,
    *,
    title="Software Engineering Intern",
    internship_type="Summer 2027 Internship",
):
    return make_row(
        source=source,
        source_adapter=adapter,
        company=company,
        title=title,
        location="United States",
        source_url=f"https://example.com/jobs/{req}",
        internship_type=internship_type,
        description="Build Python software APIs.",
        requirements="Pursuing a bachelor's degree.",
        extra={
            "source_name": adapter,
            "source_priority": 0 if source == "direct" else 10,
            "source_requisition_id": req,
            "source_system": "example",
            "source_scope": company,
            "active": True,
            "terms": ["Summer 2027"],
        },
    )


def _report(tmp_path):
    config = _config(tmp_path)
    rows = [
        _row("GitHub Co", "github", "feed", "g1"),
        _row("Direct Co", "direct", "greenhouse", "d1"),
        _row("Both Co", "direct", "greenhouse", "b1"),
        _row("Both Co", "github", "feed", "b1"),
        _row(
            "Rejected Co",
            "direct",
            "greenhouse",
            "r1",
            title="Marketing Intern",
        ),
    ]
    for index, row in enumerate(rows, 1):
        row["_row_number"] = index
    jobs, duplicates = analyze_rows(
        rows,
        today=date(2026, 7, 28),
        include_dedupe_report=True,
    )
    coverage = (
        CompanyCoverage(
            company="Unsupported Co",
            adapter="bespoke",
            state=COVERAGE_BACKSTOP_ONLY,
            direct_status="unsupported",
            direct_attempt_succeeded=None,
            direct_rows_returned=None,
            github_backstop_available=True,
        ),
        CompanyCoverage(
            company="Failed Co",
            adapter="lever",
            state=COVERAGE_UNCOVERED,
            direct_status="failing",
            direct_attempt_succeeded=False,
            direct_rows_returned=None,
            github_backstop_available=False,
        ),
        CompanyCoverage(
            company="Empty Co",
            adapter="ashby",
            state=COVERAGE_DIRECT_EMPTY,
            direct_status="empty",
            direct_attempt_succeeded=True,
            direct_rows_returned=0,
            github_backstop_available=True,
        ),
    )
    with SeenStore(config.seen_db_path) as seen:
        report = build_source_comparison(
            config=config,
            jobs=jobs,
            seen_store=seen,
            run_id="run-1",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            duplicate_report=duplicates,
            coverage=coverage,
        )
    return config, report


def test_comparison_all_required_categories_and_single_merged_count(tmp_path):
    _config_value, report = _report(tmp_path)
    assert report.schema_version == 2
    assert report.postings_evaluated == 4
    assert report.detail_entries_retained == len(report.entries)
    assert report.counts[CATEGORY_GITHUB_ONLY] == 1
    assert report.counts[CATEGORY_DIRECT_ONLY] == 1
    assert report.counts[CATEGORY_BOTH] == 1
    assert report.counts[CATEGORY_REJECTED] == 1
    assert report.counts[CATEGORY_NO_POSTINGS] == 3
    both = [entry for entry in report.entries if entry.category == CATEGORY_BOTH]
    assert len(both) == 1
    assert set(both[0].source_kinds) == {"direct_ats", "feed"}


def test_lightweight_pipeline_traces_and_sanitizes_only_selected_entries(
    tmp_path,
    monkeypatch,
):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Direct Co",
                ats="greenhouse",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        seen_db_path=tmp_path / "seen.sqlite",
    )
    jobs = analyze_rows(
        [
            _row(
                "Direct Co",
                "direct",
                "greenhouse",
                f"routine-{index}",
                title="Senior Marketing Manager",
                internship_type="",
            )
            for index in range(80)
        ],
        today=date(2026, 7, 28),
    )
    calls = {
        "outcomes": 0,
        "summaries": 0,
        "traces": 0,
        "as_dict": 0,
        "sanitized": 0,
        "deferred_outcomes": 0,
        "deferred_trace_inputs": 0,
    }
    context_sizes = []
    original_outcome = audit_trace_module.evaluate_posting_outcome
    original_summary = (
        source_comparison_module.build_posting_comparison_summary
    )
    original_trace = audit_trace_module.build_posting_trace
    original_as_dict = audit_trace_module.PostingAuditTrace.as_dict
    original_sanitizer = source_comparison_module._sanitize_trace

    def count_outcome(*args, **kwargs):
        calls["outcomes"] += 1
        outcome = original_outcome(*args, **kwargs)
        if not outcome.eligibility_evaluated:
            calls["deferred_outcomes"] += 1
        return outcome

    def count_summary(outcome):
        calls["summaries"] += 1
        return original_summary(outcome)

    def count_trace(*args, **kwargs):
        calls["traces"] += 1
        if not kwargs["outcome"].eligibility_evaluated:
            calls["deferred_trace_inputs"] += 1
        context = kwargs["context"]
        context_sizes.append(
            max(
                (
                    len(requisitions)
                    for requisitions in context.similar_requisitions.values()
                ),
                default=0,
            )
        )
        return original_trace(*args, **kwargs)

    def count_as_dict(self):
        calls["as_dict"] += 1
        return original_as_dict(self)

    def count_sanitizer(value):
        calls["sanitized"] += 1
        return original_sanitizer(value)

    monkeypatch.setattr(
        audit_trace_module.PostingAuditTrace,
        "as_dict",
        count_as_dict,
    )
    with SeenStore(config.seen_db_path) as seen:
        report = build_source_comparison(
            config=config,
            jobs=jobs,
            seen_store=seen,
            run_id="lightweight-spies",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            outcome_evaluator=count_outcome,
            summary_builder=count_summary,
            trace_builder=count_trace,
            trace_sanitizer=count_sanitizer,
        )

    assert report.counts[CATEGORY_REJECTED] == 80
    assert report.postings_evaluated == 80
    assert report.detail_entries_retained == 25
    assert calls == {
        "outcomes": 80,
        "summaries": 80,
        "traces": 25,
        "as_dict": 25,
        "sanitized": 25,
        "deferred_outcomes": 80,
        "deferred_trace_inputs": 25,
    }
    assert context_sizes == [80] * 25


def test_lightweight_report_matches_full_trace_reference_for_selected_details(
    tmp_path,
):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Direct Co",
                ats="greenhouse",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        seen_db_path=tmp_path / "seen.sqlite",
    )
    rows = [
            _row(
                "Direct Co",
                "direct",
                "greenhouse",
                f"reference-{index}",
                title=(
                    "Software Engineering Intern"
                    if index < 3
                    else "Marketing Intern"
                ),
            )
            for index in range(70)
        ]
    rows.append(
        _row(
            "Direct Co",
            "github",
            "feed",
            "reference-0",
            title="Software Engineering Intern",
        )
    )
    for row_number, row in enumerate(rows, 1):
        row["_row_number"] = row_number
    jobs, duplicate_report = analyze_rows(
        rows,
        today=date(2026, 7, 28),
        include_dedupe_report=True,
        include_audit_diagnostics=True,
    )
    duplicate_report = audit_trace_module.enrich_duplicate_entries(
        rows,
        duplicate_report,
    )
    policy = SourceComparisonDetailPolicy()
    with SeenStore(config.seen_db_path) as seen:
        context = audit_trace_module.build_posting_audit_context(
            jobs,
            seen_store=seen,
            duplicate_entries=duplicate_report,
        )
        full_entries = []
        full_reasons = []
        for index, job in enumerate(jobs):
            outcome = audit_trace_module.evaluate_posting_outcome(
                job,
                config=config,
                seen_store=seen,
                posting_universe=jobs,
                duplicate_entries=duplicate_report,
                context=context,
                job_index=index,
            )
            full_reasons.append(outcome.final_reason)
            summary = (
                source_comparison_module.build_posting_comparison_summary(
                    outcome
                )
            )
            trace = audit_trace_module.build_posting_trace(
                job,
                config=config,
                seen_store=seen,
                posting_universe=jobs,
                duplicate_entries=duplicate_report,
                context=context,
                outcome=outcome,
            )
            assert trace.final_result["reason"] == outcome.final_reason
            full_entries.append(
                source_comparison_module._entry_from_summary(
                    summary,
                    source_comparison_module._sanitize_trace(
                        trace.as_dict()
                    ),
                )
            )
        legacy_selected = _select_detail_entries(
            full_entries,
            routine_sample_limit=policy.routine_rejection_sample_limit,
            limit=policy.maximum_retained_details,
        )
        lightweight_reasons = []

        def capture_summary(outcome):
            lightweight_reasons.append(outcome.final_reason)
            return (
                source_comparison_module.build_posting_comparison_summary(
                    outcome
                )
            )

        report = build_source_comparison(
            config=config,
            jobs=jobs,
            seen_store=seen,
            run_id="lightweight-reference",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            duplicate_report=duplicate_report,
            detail_policy=policy,
            summary_builder=capture_summary,
        )

    assert lightweight_reasons == full_reasons
    assert sum(report.counts.values()) == len(jobs)
    legacy_counts = {
        category: sum(
            entry.category == category for entry in full_entries
        )
        for category in CATEGORIES
    }
    assert report.counts == legacy_counts
    assert report.aggregates == source_comparison_module._aggregate_counts(
        legacy_counts,
        report.health,
    )
    assert [
        (
            entry.category,
            entry.identity_key,
            entry.analyzed_job_id,
            entry.final_reason,
        )
        for entry in report.entries
    ] == [
        (
            entry.category,
            entry.identity_key,
            entry.analyzed_job_id,
            entry.final_reason,
        )
        for entry in legacy_selected
    ]
    assert [
        json.dumps(
            entry.trace,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for entry in report.entries
    ] == [
        json.dumps(
            entry.trace,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for entry in legacy_selected
    ]
    legacy_report = replace(
        report,
        entries=legacy_selected,
        detail_entries_retained=len(legacy_selected),
    )
    assert render_markdown(report) == render_markdown(legacy_report)
    current_console = StringIO()
    legacy_console = StringIO()
    render_console(report, output=current_console)
    render_console(legacy_report, output=legacy_console)
    assert current_console.getvalue() == legacy_console.getvalue()
    assert [
        entry.final_reason for entry in full_entries
    ] == full_reasons


def test_detail_policy_always_retains_eligible_anomalous_and_nonroutine():
    base = PostingComparisonSummary(
        job_index=0,
        company="Example",
        title="Role",
        identity_key="identity",
        analyzed_job_id="job",
        source_kinds=("direct_ats",),
        final_reason="not_internship",
        category=CATEGORY_REJECTED,
        direct_status="healthy",
        direct_coverage="covered",
        github_backstop_available=True,
        generic_or_shared_url=False,
        duplicate_sightings=0,
        deduplicated_into_another=False,
        has_merge_diagnostics=False,
    )
    eligible = [
        replace(
            base,
            job_index=index,
            identity_key=f"eligible-{category}",
            analyzed_job_id=f"eligible-{category}",
            final_reason="pending",
            category=category,
        )
        for index, category in enumerate(
            (
                CATEGORY_GITHUB_ONLY,
                CATEGORY_DIRECT_ONLY,
                CATEGORY_BOTH,
            ),
            1,
        )
    ]
    anomalies = [
        replace(
            base,
            job_index=10,
            identity_key="anomaly-status",
            analyzed_job_id="anomaly-status",
            direct_status="failing",
        ),
        replace(
            base,
            job_index=11,
            identity_key="anomaly-coverage",
            analyzed_job_id="anomaly-coverage",
            direct_coverage="uncovered_for_run",
        ),
        replace(
            base,
            job_index=12,
            identity_key="anomaly-url",
            analyzed_job_id="anomaly-url",
            generic_or_shared_url=True,
        ),
        replace(
            base,
            job_index=13,
            identity_key="anomaly-deduplicated",
            analyzed_job_id="anomaly-deduplicated",
            deduplicated_into_another=True,
        ),
        replace(
            base,
            job_index=14,
            identity_key="anomaly-duplicates",
            analyzed_job_id="anomaly-duplicates",
            duplicate_sightings=1,
        ),
        replace(
            base,
            job_index=15,
            identity_key="anomaly-merge",
            analyzed_job_id="anomaly-merge",
            has_merge_diagnostics=True,
        ),
    ]
    nonroutine = replace(
        base,
        job_index=3,
        identity_key="nonroutine",
        analyzed_job_id="nonroutine",
        final_reason="graduate_only",
    )
    no_postings = replace(
        base,
        job_index=None,
        identity_key="",
        analyzed_job_id="",
        final_reason="not_collected",
        category=CATEGORY_NO_POSTINGS,
    )
    routine = replace(
        base,
        job_index=4,
        identity_key="routine",
        analyzed_job_id="routine",
    )

    selected = select_comparison_details(
        [routine, nonroutine, *anomalies, *eligible, no_postings],
        policy=SourceComparisonDetailPolicy(
            routine_rejection_sample_limit=0,
            maximum_retained_details=100,
        ),
    )

    assert {
        summary.identity_key or summary.company
        for summary in selected
    } == {
        "nonroutine",
        "Example",
    } | {
        summary.identity_key for summary in eligible
    } | {
        summary.identity_key for summary in anomalies
    }


def test_comparison_distinguishes_unsupported_failed_and_healthy_empty(tmp_path):
    _config_value, report = _report(tmp_path)
    empty = {
        entry.company: entry
        for entry in report.entries
        if entry.category == CATEGORY_NO_POSTINGS
    }
    assert empty["Unsupported Co"].direct_status == "unsupported"
    assert empty["Unsupported Co"].direct_coverage == COVERAGE_BACKSTOP_ONLY
    assert (
        empty["Unsupported Co"].trace["watchlist_match"]["direct_coverage"]
        == COVERAGE_BACKSTOP_ONLY
    )
    assert empty["Failed Co"].direct_status == "failing"
    assert empty["Failed Co"].direct_coverage == COVERAGE_UNCOVERED
    assert empty["Empty Co"].direct_status == "empty"
    assert empty["Empty Co"].direct_coverage == COVERAGE_DIRECT_EMPTY


def test_comparison_is_sanitized_and_contains_no_alumni_or_credentials(tmp_path):
    _config_value, report = _report(tmp_path)
    markdown = render_markdown(report)
    serialized = str(report.as_dict())
    assert "alumni" not in serialized.casefold()
    assert "password" not in serialized.casefold()
    assert "@" not in markdown


def test_comparison_redacts_credentials_from_posting_identity_and_trace(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Credential Test",
                ats="github_only",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        seen_db_path=tmp_path / "seen.sqlite",
    )
    row = make_row(
        source="github",
        source_adapter="feed",
        company="Credential Test",
        title="Software Engineering Intern",
        location="United States",
        source_url=(
            "https://feed-user:feed-password@example.test/jobs/1"
            "?access_token=DO_NOT_STORE"
        ),
        internship_type="Summer 2027 Internship",
        description="Build Python software APIs.",
        extra={"active": True, "terms": ["Summer 2027"]},
    )
    jobs = analyze_rows([row], today=date(2026, 7, 28))
    with SeenStore(config.seen_db_path) as seen:
        report = build_source_comparison(
            config=config,
            jobs=jobs,
            seen_store=seen,
            run_id="credential-test",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )
    with SourceComparisonStore(config.seen_db_path) as store:
        store.save(report)
        report = store.latest_report()

    serialized = json.dumps(report.as_dict(), sort_keys=True)
    assert "feed-user" not in serialized
    assert "feed-password" not in serialized
    assert "DO_NOT_STORE" not in serialized
    assert "https://example.test/jobs/1" in serialized


def test_comparison_store_retention_is_bounded(tmp_path):
    _config_value, report = _report(tmp_path)
    path = tmp_path / "comparison.sqlite"
    with SourceComparisonStore(
        path,
        run_retention=3,
        detail_run_retention=2,
    ) as store:
        for offset in range(6):
            next_report = type(report)(
                schema_version=1,
                run_id=f"run-{offset}",
                observed_at=(
                    datetime(2026, 7, 28, tzinfo=timezone.utc)
                    + timedelta(hours=offset)
                ).isoformat(),
                counts=report.counts,
                entries=report.entries,
            )
            store.save(next_report)
        assert store.run_count() == 3
        assert store.detail_run_count() == 2
        assert store.latest_report().run_id == "run-5"


def test_schema_version_one_persisted_report_remains_readable(tmp_path):
    _config_value, report = _report(tmp_path)
    path = tmp_path / "legacy-comparison.sqlite"
    with SourceComparisonStore(path) as store:
        store.save(report)
        store._conn.execute(
            """
            update source_comparison_runs
            set summary_json = ?
            where run_id = ?
            """,
            (
                json.dumps(
                    {
                        "counts": report.counts,
                        "aggregates": report.aggregates,
                        "health": report.health,
                    }
                ),
                report.run_id,
            ),
        )
        store._conn.commit()
        loaded = store.latest_report()

    assert loaded is not None
    assert loaded.schema_version == 1
    assert loaded.counts == report.counts
    assert [
        (entry.category, entry.identity_key, entry.final_reason, entry.trace)
        for entry in loaded.entries
    ] == [
        (entry.category, entry.identity_key, entry.final_reason, entry.trace)
        for entry in report.entries
    ]
    assert loaded.postings_evaluated == report.postings_evaluated
    assert loaded.detail_entries_retained == len(report.entries)


def test_comparison_store_applies_only_a_defensive_hard_cap(
    tmp_path,
    monkeypatch,
):
    _config_value, report = _report(tmp_path)
    rejected = next(
        entry for entry in report.entries
        if entry.category == CATEGORY_REJECTED
    )
    entries = tuple(
        replace(
            rejected,
            identity_key=f"routine-{offset}",
            analyzed_job_id=f"routine-job-{offset}",
            final_reason="not_internship",
        )
        for offset in range(4_000)
    )
    oversized = replace(
        report,
        run_id="oversized",
        counts={
            **report.counts,
            CATEGORY_REJECTED: len(entries),
        },
        entries=entries,
    )
    def fail_if_store_selects(*args, **kwargs):
        raise AssertionError("the store must not apply detail-selection policy")

    monkeypatch.setattr(
        source_comparison_module,
        "_select_detail_entries",
        fail_if_store_selects,
    )
    path = tmp_path / "bounded-comparison.sqlite"
    with SourceComparisonStore(path) as store:
        store.save(oversized)
        latest = store.latest_report()
        assert len(latest.entries) == 2_000
        assert [
            entry.identity_key for entry in latest.entries
        ] == [
            entry.identity_key for entry in entries[:2_000]
        ]


def test_comparison_distinguishes_failed_feed_from_valid_zero_row_feed(tmp_path):
    config = _config(tmp_path)
    attempts = (
        SourceAttempt(
            health_key="direct:healthy",
            run_id="run-feed",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            source_kind=SOURCE_KIND_DIRECT,
            company="Healthy Co",
            adapter="greenhouse",
            attempted=True,
            succeeded=True,
            rows_returned=2,
        ),
        SourceAttempt(
            health_key="direct:empty",
            run_id="run-feed",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            source_kind=SOURCE_KIND_DIRECT,
            company="Empty Co",
            adapter="ashby",
            attempted=True,
            succeeded=True,
            rows_returned=0,
        ),
        SourceAttempt(
            health_key="direct:failed",
            run_id="run-feed",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            source_kind=SOURCE_KIND_DIRECT,
            company="Failed Co",
            adapter="lever",
            attempted=True,
            succeeded=False,
            rows_returned=None,
            error_kind="fetch_failure",
        ),
        SourceAttempt(
            health_key="direct:unsupported",
            run_id="run-feed",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            source_kind=SOURCE_KIND_DIRECT,
            company="Unsupported Co",
            adapter="bespoke",
            attempted=False,
            succeeded=False,
            rows_returned=None,
            unsupported_reason="manual integration",
        ),
        SourceAttempt(
            health_key="github_feed:healthy",
            run_id="run-feed",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            source_kind=SOURCE_KIND_GITHUB_FEED,
            company=None,
            adapter="simplify_json",
            attempted=True,
            succeeded=True,
            rows_returned=0,
            feed_label="healthy feed",
        ),
        SourceAttempt(
            health_key="github_feed:failed",
            run_id="run-feed",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            source_kind=SOURCE_KIND_GITHUB_FEED,
            company=None,
            adapter="github_markdown_table",
            attempted=True,
            succeeded=False,
            rows_returned=None,
            error_kind="schema_failure",
            error_message="safe schema failure",
            feed_label="failed feed",
        ),
    )
    with SeenStore(config.seen_db_path) as seen:
        report = build_source_comparison(
            config=config,
            jobs=[],
            seen_store=seen,
            run_id="run-feed",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            source_attempts=attempts,
        )
    by_label = {
        item["feed_label"]: item
        for item in report.health["github_feeds"]
    }
    assert by_label["healthy feed"]["succeeded"] is True
    assert by_label["healthy feed"]["rows_returned"] == 0
    assert by_label["failed feed"]["succeeded"] is False
    assert by_label["failed feed"]["error_kind"] == "schema_failure"
    assert report.aggregates["direct_healthy"] == 1
    assert report.aggregates["direct_empty"] == 1
    assert report.aggregates["direct_failed"] == 1
    assert report.aggregates["direct_unsupported"] == 1
    assert report.aggregates["github_healthy"] == 1
    assert report.aggregates["github_failed"] == 1


def test_comparison_precomputes_posting_universe_once(tmp_path, monkeypatch):
    config = _config(tmp_path)
    jobs = analyze_rows(
        [
            _row("Direct Co", "direct", "greenhouse", f"scale-{index}")
            for index in range(40)
        ],
        today=date(2026, 7, 28),
    )
    audit_universe_scans = 0
    seen_universe_scans = 0
    similarity_fallback_scans = 0
    original_audit_scan = audit_trace_module.non_specific_posting_urls
    original_seen_scan = seen_store_module.non_specific_posting_urls
    original_similarity_scan = (
        audit_trace_module._similar_distinct_requisitions
    )

    def count_audit_scan(postings):
        nonlocal audit_universe_scans
        audit_universe_scans += 1
        return original_audit_scan(postings)

    def count_seen_scan(postings):
        nonlocal seen_universe_scans
        seen_universe_scans += 1
        return original_seen_scan(postings)

    def count_similarity_scan(*args, **kwargs):
        nonlocal similarity_fallback_scans
        similarity_fallback_scans += 1
        return original_similarity_scan(*args, **kwargs)

    monkeypatch.setattr(
        audit_trace_module,
        "non_specific_posting_urls",
        count_audit_scan,
    )
    monkeypatch.setattr(
        seen_store_module,
        "non_specific_posting_urls",
        count_seen_scan,
    )
    monkeypatch.setattr(
        audit_trace_module,
        "_similar_distinct_requisitions",
        count_similarity_scan,
    )

    with SeenStore(config.seen_db_path) as seen:
        report = build_source_comparison(
            config=config,
            jobs=jobs,
            seen_store=seen,
            run_id="scale-regression",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )

    assert audit_universe_scans == 1
    assert seen_universe_scans == 0
    assert similarity_fallback_scans == 0
    posting_entries = [
        entry for entry in report.entries if entry.company == "Direct Co"
    ]
    assert len(posting_entries) == 40
    assert all(
        len(entry.trace["deduplication"]["similar_distinct_requisitions"])
        == 25
        for entry in posting_entries
    )


def _high_volume_report(tmp_path, *, routine_count=20_000):
    _config_value, report = _report(tmp_path)
    rejected = next(
        entry for entry in report.entries
        if entry.category == CATEGORY_REJECTED
    )
    routine = tuple(
        replace(
            rejected,
            company="Routine Co",
            title=f"Senior Full-Time Role {index:05d}",
            identity_key=f"routine-{index:05d}",
            analyzed_job_id=f"routine-job-{index:05d}",
            final_reason="not_internship",
        )
        for index in range(routine_count)
    )
    unusual = tuple(
        replace(
            rejected,
            company="Unusual Co",
            title=f"Graduate Internship {index:03d}",
            identity_key=f"unusual-{index:03d}",
            analyzed_job_id=f"unusual-job-{index:03d}",
            final_reason="graduate_only",
        )
        for index in range(60)
    )
    important = tuple(
        replace(
            entry,
            identity_key=f"important-{entry.category}",
            analyzed_job_id=f"important-job-{entry.category}",
        )
        for entry in report.entries
        if entry.category in {
            CATEGORY_GITHUB_ONLY,
            CATEGORY_DIRECT_ONLY,
            CATEGORY_BOTH,
            CATEGORY_NO_POSTINGS,
        }
    )
    all_entries = (*routine, *unusual, *important)
    selected_entries = _select_detail_entries(
        all_entries,
        routine_sample_limit=25,
        limit=2_000,
    )
    return replace(
        report,
        run_id="high-volume",
        counts={
            **report.counts,
            CATEGORY_REJECTED: routine_count + len(unusual),
        },
        entries=selected_entries,
        detail_entries_retained=len(selected_entries),
    )


def test_high_volume_routine_rejections_are_sampled_but_aggregates_are_exact(
    tmp_path,
):
    report = _high_volume_report(tmp_path)
    path = tmp_path / "high-volume.sqlite"

    with SourceComparisonStore(path) as store:
        store.save(report)
        saved = store.latest_report()

    routine = [
        entry for entry in saved.entries
        if entry.final_reason == "not_internship"
    ]
    unusual = [
        entry for entry in saved.entries
        if entry.final_reason == "graduate_only"
    ]
    assert saved.counts[CATEGORY_REJECTED] == 20_060
    assert len(routine) == 25
    assert len(unusual) == 60
    assert {
        CATEGORY_GITHUB_ONLY,
        CATEGORY_DIRECT_ONLY,
        CATEGORY_BOTH,
        CATEGORY_NO_POSTINGS,
    } <= {entry.category for entry in saved.entries}


def test_routine_rejection_sampling_is_deterministic(tmp_path):
    _config_value, base_report = _report(tmp_path)
    rejected = next(
        entry for entry in base_report.entries
        if entry.category == CATEGORY_REJECTED
    )
    entries = tuple(
        replace(
            rejected,
            identity_key=f"routine-{index:05d}",
            analyzed_job_id=f"routine-job-{index:05d}",
            final_reason="not_internship",
        )
        for index in range(200)
    )
    first = _select_detail_entries(
        entries,
        routine_sample_limit=25,
        limit=2_000,
    )
    second = _select_detail_entries(
        tuple(reversed(entries)),
        routine_sample_limit=25,
        limit=2_000,
    )

    assert len(first) == 25
    assert first == second


def test_similar_distinct_requisitions_do_not_make_routine_rows_anomalies(
    tmp_path,
):
    report = _high_volume_report(tmp_path, routine_count=200)
    entries = tuple(
        replace(
            entry,
            trace={
                **entry.trace,
                "deduplication": {
                    **entry.trace.get("deduplication", {}),
                    "similar_distinct_requisitions": [
                        {
                            "identity_key": "stable:other-requisition",
                            "requisition_key": "example:other-requisition",
                        }
                    ],
                },
            },
        )
        if entry.final_reason == "not_internship"
        else entry
        for entry in report.entries
    )
    report = replace(report, entries=entries)
    path = tmp_path / "similar-requisitions.sqlite"

    with SourceComparisonStore(path) as store:
        store.save(report)
        saved = store.latest_report()

    assert sum(
        entry.final_reason == "not_internship"
        for entry in saved.entries
    ) == 25


def test_json_artifact_uses_the_same_bounded_detail_policy(tmp_path):
    report = _high_volume_report(tmp_path, routine_count=200)
    path = tmp_path / "comparison.json"

    write_json(report, path)

    payload = json.loads(path.read_text(encoding="utf-8"))
    routine = [
        entry for entry in payload["entries"]
        if entry["final_reason"] == "not_internship"
    ]
    assert payload["counts"][CATEGORY_REJECTED] == 260
    assert len(routine) == 25
    assert payload["aggregates"]["collected_rejected"] == 260
    assert "description" not in json.dumps(payload).casefold()


def test_comparison_cleanup_preserves_notification_and_health_alert_state(
    tmp_path,
):
    report = _high_volume_report(tmp_path, routine_count=1_000)
    path = tmp_path / "migration.sqlite"
    emailed = {
        "id": "emailed-job",
        "company": "Email Co",
        "title": "Software Intern",
        "source_url": "https://example.test/jobs/emailed",
        "extra": {"source": "direct"},
    }
    primed = {
        "id": "primed-job",
        "company": "Prime Co",
        "title": "Software Intern",
        "source_url": "https://example.test/jobs/primed",
        "extra": {"source": "direct"},
    }
    with SeenStore(path) as seen:
        seen.mark_emailed(emailed)
        seen.mark_primed(primed)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            create table source_health_current(
              health_key text primary key,
              status text not null
            );
            insert into source_health_current values ('direct:test', 'failing');
            create table source_health_alert_state(
              fingerprint text primary key,
              last_sent_at text
            );
            insert into source_health_alert_state
              values ('failure:direct:test', '2026-07-28T12:00:00+00:00');
            """
        )
    with SourceComparisonStore(
        path,
        max_postings_per_run=len(report.entries),
        routine_rejection_sample_limit=len(report.entries),
    ) as store:
        store.save(report)
    next_report = replace(
        report,
        run_id="migration-cleanup",
        observed_at=(
            datetime.fromisoformat(report.observed_at) + timedelta(hours=1)
        ).isoformat(),
    )
    with SourceComparisonStore(path) as store:
        store.save(next_report)
    with sqlite3.connect(path) as connection:
        seen_rows = connection.execute(
            "select job_id, emailed_at, primed_at from seen order by job_id"
        ).fetchall()
        health = connection.execute(
            "select health_key, status from source_health_current"
        ).fetchall()
        alert = connection.execute(
            "select fingerprint, last_sent_at from source_health_alert_state"
        ).fetchall()
        details = connection.execute(
            "select count(*) from source_comparison_postings"
        ).fetchone()[0]
        check = connection.execute("pragma quick_check").fetchone()[0]
    assert len(seen_rows) == 2
    assert sum(row[1] is not None for row in seen_rows) == 1
    assert sum(row[2] is not None for row in seen_rows) == 1
    assert health == [("direct:test", "failing")]
    assert alert == [
        ("failure:direct:test", "2026-07-28T12:00:00+00:00")
    ]
    assert details < 200
    assert check == "ok"


def test_failed_comparison_cleanup_rolls_back_transactionally(
    tmp_path,
    monkeypatch,
):
    _config_value, report = _report(tmp_path)
    path = tmp_path / "rollback.sqlite"
    with SourceComparisonStore(path) as store:
        store.save(report)
        before = store.latest_report()

        def fail_cleanup():
            raise RuntimeError("injected cleanup failure")

        monkeypatch.setattr(store, "_prune", fail_cleanup)
        with pytest.raises(RuntimeError, match="injected cleanup failure"):
            store.save(
                replace(
                    report,
                    run_id="must-roll-back",
                    observed_at=(
                        datetime.fromisoformat(report.observed_at)
                        + timedelta(hours=1)
                    ).isoformat(),
                )
            )
        after = store.latest_report()

    assert after.run_id == before.run_id
    assert after.entries == before.entries


def test_compaction_does_not_run_for_small_hourly_saves(tmp_path):
    _config_value, report = _report(tmp_path)
    path = tmp_path / "no-routine-vacuum.sqlite"
    statements = []
    with SourceComparisonStore(path) as store:
        store._conn.set_trace_callback(statements.append)
        store.save(report)
        store.save(replace(report, run_id="next-small-run"))

    assert not any(statement.strip().casefold() == "vacuum" for statement in statements)
