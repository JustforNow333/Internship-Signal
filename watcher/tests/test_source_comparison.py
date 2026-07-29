from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import watcher.audit_trace as audit_trace_module
import watcher.seen_store as seen_store_module
from backend.app.ingest import analyze_rows
from watcher.config import CompanyCfg, WatcherConfig
from watcher.seen_store import SeenStore
from watcher.source_comparison import (
    CATEGORY_BOTH,
    CATEGORY_DIRECT_ONLY,
    CATEGORY_GITHUB_ONLY,
    CATEGORY_NO_POSTINGS,
    CATEGORY_REJECTED,
    SourceComparisonStore,
    build_source_comparison,
    render_markdown,
)
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT_EMPTY,
    COVERAGE_UNCOVERED,
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


def _row(company, source, adapter, req, *, title="Software Engineering Intern"):
    return make_row(
        source=source,
        source_adapter=adapter,
        company=company,
        title=title,
        location="United States",
        source_url=f"https://example.com/jobs/{req}",
        internship_type="Summer 2027 Internship",
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
    assert report.counts[CATEGORY_GITHUB_ONLY] == 1
    assert report.counts[CATEGORY_DIRECT_ONLY] == 1
    assert report.counts[CATEGORY_BOTH] == 1
    assert report.counts[CATEGORY_REJECTED] == 1
    assert report.counts[CATEGORY_NO_POSTINGS] == 3
    both = [entry for entry in report.entries if entry.category == CATEGORY_BOTH]
    assert len(both) == 1
    assert set(both[0].source_kinds) == {"direct_ats", "feed"}


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


def test_comparison_distinguishes_failed_feed_from_valid_zero_row_feed(tmp_path):
    config = _config(tmp_path)
    attempts = (
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
