from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

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
    CATEGORIES,
    SourceComparisonStore,
    build_source_comparison,
    render_markdown,
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


def test_comparison_store_caps_and_compacts_legacy_oversized_details(tmp_path):
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
    path = tmp_path / "bounded-comparison.sqlite"
    with SourceComparisonStore(
        path,
        max_postings_per_run=len(entries),
        routine_rejection_sample_limit=len(entries),
    ) as store:
        store.save(oversized)
        assert len(store.latest_report().entries) == len(entries)

    bounded = replace(
        oversized,
        run_id="bounded",
        observed_at=(
            datetime.fromisoformat(oversized.observed_at)
            + timedelta(hours=1)
        ).isoformat(),
    )
    statements = []
    with SourceComparisonStore(path) as store:
        store._conn.set_trace_callback(statements.append)
        store.save(bounded)
        latest = store.latest_report()
        assert len(latest.entries) == 25
        assert {
            entry.final_reason for entry in latest.entries
        } == {"not_internship"}

    with sqlite3.connect(path) as connection:
        per_run = connection.execute(
            """
            select run_id, count(*)
            from source_comparison_postings
            group by run_id
            """
        ).fetchall()
        page_count = connection.execute("pragma page_count").fetchone()[0]
        free_pages = connection.execute("pragma freelist_count").fetchone()[0]
    assert per_run == [("bounded", 25), ("oversized", 25)]
    assert free_pages * 4 < page_count
    assert any(
        statement.strip().casefold() == "vacuum"
        for statement in statements
    )


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
    return replace(
        report,
        run_id="high-volume",
        counts={
            **report.counts,
            CATEGORY_REJECTED: routine_count + len(unusual),
        },
        entries=(*routine, *unusual, *important),
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
    report = _high_volume_report(tmp_path, routine_count=200)
    reversed_report = replace(
        report,
        run_id="reversed",
        entries=tuple(reversed(report.entries)),
    )

    selected = []
    for name, value in (("first", report), ("second", reversed_report)):
        path = tmp_path / f"{name}.sqlite"
        with SourceComparisonStore(path) as store:
            store.save(value)
            selected.append(
                {
                    entry.identity_key
                    for entry in store.latest_report().entries
                    if entry.final_reason == "not_internship"
                }
            )

    assert len(selected[0]) == 25
    assert selected[0] == selected[1]


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
    before = path.stat().st_size

    next_report = replace(
        report,
        run_id="migration-cleanup",
        observed_at=(
            datetime.fromisoformat(report.observed_at) + timedelta(hours=1)
        ).isoformat(),
    )
    with SourceComparisonStore(path) as store:
        store.save(next_report)
    after = path.stat().st_size

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
    assert after < before


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
