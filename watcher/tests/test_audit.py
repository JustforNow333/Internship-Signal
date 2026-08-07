from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backend.app.ingest import analyze_rows
from watcher.audit import (
    audit_live,
    audit_state_only,
    main as audit_main,
    render_audit_console,
)
from watcher.audit_trace import (
    AuditQuery,
    build_posting_trace,
    enrich_duplicate_entries,
    query_matches_trace,
)
from watcher.config import CompanyCfg, WatcherConfig
from watcher.seen_store import SeenStore
from watcher.source_comparison import SourceComparisonStore, build_source_comparison
from watcher.sources.base import DirectSourceDiagnostics, make_row


def _config(tmp_path):
    return WatcherConfig(
        companies=(
            CompanyCfg(
                name="Google",
                ats="github_only",
                aliases=("Alphabet",),
                terms=("Summer 2027",),
            ),
            CompanyCfg(
                name="Uber",
                ats="greenhouse",
                aliases=("Uber Technologies",),
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        seen_db_path=tmp_path / "seen.sqlite",
    )


def _row(
    *,
    company="Google",
    title="Software Engineering Intern",
    location="New York, NY",
    url="https://example.com/jobs/123?utm_source=test",
    source="github",
    adapter="feed",
    requisition="123",
    active=True,
    terms=("Summer 2027",),
    internship_type="Summer 2027 Internship",
    description="Build Python services and APIs.",
    requirements="Currently pursuing a bachelor's degree.",
):
    return make_row(
        source=source,
        source_adapter=adapter,
        company=company,
        title=title,
        location=location,
        source_url=url,
        internship_type=internship_type,
        description=description,
        requirements=requirements,
        extra={
            "source_name": adapter,
            "source_priority": 0 if source == "direct" else 10,
            "source_requisition_id": requisition,
            "source_system": adapter,
            "source_scope": company,
            "active": active,
            "terms": list(terms),
        },
    )


def _analyze(*rows):
    copied = [dict(row) for row in rows]
    jobs, duplicates = analyze_rows(
        copied,
        today=date(2026, 7, 28),
        include_dedupe_report=True,
        include_audit_diagnostics=True,
    )
    return jobs, enrich_duplicate_entries(copied, duplicates)


@pytest.mark.parametrize(
    ("query_factory", "matched_field"),
    [
        (lambda trace: AuditQuery(company="Google"), "company"),
        (lambda trace: AuditQuery(title="Engineering Intern"), "title"),
        (
            lambda trace: AuditQuery(
                url="https://example.com/jobs/123?utm_campaign=x"
            ),
            "url",
        ),
        (lambda trace: AuditQuery(requisition_id="123"), "requisition_id"),
        (
            lambda trace: AuditQuery(
                job_id=str(trace.posting["analyzed_job_id"])
            ),
            "job_id",
        ),
        (
            lambda trace: AuditQuery(
                identity=str(trace.identity["canonical_identity_key"])
            ),
            "identity",
        ),
    ],
)
def test_trace_search_types(tmp_path, query_factory, matched_field):
    config = _config(tmp_path)
    jobs, duplicates = _analyze(_row())
    with SeenStore(config.seen_db_path) as seen:
        trace = build_posting_trace(
            jobs[0],
            config=config,
            seen_store=seen,
            posting_universe=jobs,
            duplicate_entries=duplicates,
        )
        matched, fields = query_matches_trace(trace, query_factory(trace))
    assert matched is True
    assert matched_field in fields


def test_audit_url_query_distinguishes_fragment_postings(tmp_path):
    config = _config(tmp_path)
    first = _row(
        requisition="",
        url="https://careers.example.test/jobs#/job/ABC123",
    )
    second = _row(
        requisition="",
        url="https://careers.example.test/jobs#/job/XYZ789",
    )
    jobs, duplicates = _analyze(first, second)
    with SeenStore(config.seen_db_path) as seen:
        traces = [
            build_posting_trace(
                posting,
                config=config,
                seen_store=seen,
                posting_universe=jobs,
                duplicate_entries=duplicates,
            )
            for posting in jobs
        ]

    matches = [
        query_matches_trace(
            trace,
            AuditQuery(
                url="https://careers.example.test/jobs?ref=audit#jobId=ABC123"
            ),
        )[0]
        for trace in traces
    ]

    assert matches == [True, False]


def test_state_only_finds_company_alias_and_multiple_titles(tmp_path):
    config = _config(tmp_path)
    jobs, duplicates = _analyze(
        _row(requisition="1", url="https://example.com/jobs/1"),
        _row(
            title="Backend Software Intern",
            requisition="2",
            url="https://example.com/jobs/2",
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
        )
        with SourceComparisonStore(seen.path) as store:
            store.save(report)
        traces = audit_state_only(
            config=config,
            seen_store=seen,
            query=AuditQuery(company="Alphabet"),
        )
    assert len(traces) == 2
    assert all(trace.query_match["mode"] == "state_only" for trace in traces)


def test_state_only_alias_search_finds_historical_seen_record_without_snapshot(
    tmp_path,
):
    config = _config(tmp_path)
    historical = {
        "id": "historical-id",
        "company": "Alphabet",
        "title": "Software Engineering Intern",
        "location": "United States",
        "source_url": "https://example.com/jobs/historical",
        "extra": {"source": "github"},
    }
    with SeenStore(config.seen_db_path) as seen:
        seen.mark_emailed(historical)
        traces = audit_state_only(
            config=config,
            seen_store=seen,
            query=AuditQuery(company="Alphabet"),
        )
    assert traces[0].final_result["reason"] == "already_emailed"
    assert traces[0].posting["company"] == "Alphabet"


def test_state_only_reports_not_collected_and_not_on_watchlist(tmp_path):
    config = _config(tmp_path)
    with SeenStore(config.seen_db_path) as seen:
        collected = audit_state_only(
            config=config,
            seen_store=seen,
            query=AuditQuery(company="Google"),
        )
        unknown = audit_state_only(
            config=config,
            seen_store=seen,
            query=AuditQuery(company="Unknown Corp"),
        )
    assert collected[0].final_result["reason"] == "not_collected"
    assert unknown[0].final_result["reason"] == "not_on_watchlist"


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_row(terms=("Fall 2026",)), "wrong_season"),
        (
            _row(
                title="Software Engineer",
                internship_type="Full Time",
                terms=("Summer 2027",),
            ),
            "not_internship",
        ),
        (_row(active=False), "closed"),
        (_row(location="Toronto, Canada"), "outside_us"),
        (
            _row(
                title="Marketing Intern",
                description="Create campaigns and social media content.",
                requirements="Strong writing skills.",
            ),
            "nontechnical_role",
        ),
        (
            _row(
                title="PhD Software Intern",
                requirements="Currently pursuing a PhD.",
            ),
            "phd_only",
        ),
        (
            _row(
                title="Graduate Software Intern",
                requirements="Must be enrolled in a master's program.",
            ),
            "graduate_only",
        ),
        (
            _row(
                title="Freshmen Only Software Internship",
                requirements="Must be a college freshman.",
            ),
            "freshman_only",
        ),
        (
            _row(
                title="Returning Intern Software Engineer",
                requirements="For returning interns only.",
            ),
            "returning_intern_only",
        ),
        (
            _row(
                title="Product Management Intern",
                description="Manage product roadmaps and requirements with engineering teams.",
                requirements="Product management coursework.",
            ),
            "watcher_role_ineligible",
        ),
    ],
)
def test_trace_reports_pipeline_exclusions(tmp_path, row, reason):
    config = _config(tmp_path)
    jobs, duplicates = _analyze(row)
    with SeenStore(config.seen_db_path) as seen:
        trace = build_posting_trace(
            jobs[0],
            config=config,
            seen_store=seen,
            duplicate_entries=duplicates,
        )
    assert trace.final_result["reason"] == reason
    assert trace.role["classification"]
    assert "confidence" in trace.role
    assert "evidence" in trace.role


@pytest.mark.parametrize(
    ("title", "requirements"),
    [
        (
            "Senior Thermal Analyst",
            "Currently pursuing or have obtained an MS or higher degree.",
        ),
        (
            "Senior Thermal Engineer",
            "Currently pursuing or have obtained an MS or higher degree.",
        ),
        (
            "Senior Mechanical Engineer, Thermal Analysis",
            "Currently pursuing or have obtained an MS or higher degree.",
        ),
        (
            "Design Manager, Engineering - Home Environment",
            "Advanced degree not required.",
        ),
    ],
)
def test_clear_nonintern_roles_do_not_report_categorical_exclusions(
    tmp_path,
    title,
    requirements,
):
    config = _config(tmp_path)
    jobs, duplicates = _analyze(
        _row(
            title=title,
            internship_type="Full Time",
            requirements=requirements,
        )
    )

    with SeenStore(config.seen_db_path) as seen:
        trace = build_posting_trace(
            jobs[0],
            config=config,
            seen_store=seen,
            duplicate_entries=duplicates,
        )

    assert trace.final_result["reason"] == "not_internship"
    assert trace.watcher_eligibility["exclusion_reason"] is None
    assert trace.watcher_eligibility["categorical_evaluation_applied"] is False
    assert "graduate" not in str(
        trace.watcher_eligibility["ineligible_reason"]
    ).casefold()


def test_notification_reports_emailed_primed_and_pending(tmp_path):
    config = _config(tmp_path)
    jobs, duplicates = _analyze(
        _row(requisition="1", url="https://example.com/jobs/1"),
        _row(requisition="2", url="https://example.com/jobs/2"),
        _row(requisition="3", url="https://example.com/jobs/3"),
    )
    with SeenStore(config.seen_db_path) as seen:
        seen.mark_emailed(jobs[0])
        seen.mark_primed(jobs[1])
        traces = [
            build_posting_trace(
                job,
                config=config,
                seen_store=seen,
                posting_universe=jobs,
                duplicate_entries=duplicates,
            )
            for job in jobs
        ]
    assert {trace.final_result["reason"] for trace in traces} == {
        "already_emailed",
        "explicitly_primed",
        "pending",
    }
    emailed = next(
        trace for trace in traces if trace.final_result["reason"] == "already_emailed"
    )
    assert emailed.notification["first_seen"]
    assert emailed.notification["stored_identity_keys"]


def test_direct_github_merge_reports_winner_and_exact_reason(tmp_path):
    config = _config(tmp_path)
    direct = _row(
        company="Uber",
        source="direct",
        adapter="greenhouse",
        requisition="300697",
        url="https://jobs.uber.com/en/jobs/300697/",
    )
    github = _row(
        company="Uber Technologies",
        source="github",
        adapter="simplify",
        requisition="300697",
        url="https://jobs.uber.com/en/jobs/300697/?utm_source=feed",
    )
    direct["extra"]["source_system"] = "uber"
    direct["extra"]["source_scope"] = "uber"
    github["extra"]["source_system"] = "uber"
    github["extra"]["source_scope"] = "uber"
    jobs, duplicates = _analyze(direct, github)
    with SeenStore(config.seen_db_path) as seen:
        trace = build_posting_trace(
            jobs[0],
            config=config,
            seen_store=seen,
            duplicate_entries=duplicates,
        )
    assert trace.collection["direct_source_found"] is True
    assert trace.collection["github_source_found"] is True
    assert trace.deduplication["winning_source"] == "direct_ats"
    assert trace.deduplication["merge_reasons"] == ["requisition_id"]


def test_audit_dedupe_instrumentation_is_opt_in_and_removed_from_trace():
    first = _row(requisition="", url="")
    second = _row(requisition="", url="")
    _jobs, normal_report = analyze_rows(
        [dict(first), dict(second)],
        today=date(2026, 7, 28),
        include_dedupe_report=True,
    )
    assert not any(
        key.startswith("_audit_")
        for key in normal_report[0]
    )
    rows = [dict(first), dict(second)]
    _jobs, audit_report = analyze_rows(
        rows,
        today=date(2026, 7, 28),
        include_dedupe_report=True,
        include_audit_diagnostics=True,
    )
    assert "_audit_row_index" in audit_report[0]
    enriched = enrich_duplicate_entries(rows, audit_report)
    assert not any(
        key.startswith("_audit_")
        for key in enriched[0]
    )


def test_trace_distinguishes_direct_only_and_github_only(tmp_path):
    config = _config(tmp_path)
    jobs, duplicates = _analyze(
        _row(
            company="Uber",
            source="direct",
            adapter="greenhouse",
            requisition="direct-1",
            url="https://example.com/jobs/direct-1",
        ),
        _row(
            company="Google",
            source="github",
            adapter="feed",
            requisition="github-1",
            url="https://example.com/jobs/github-1",
        ),
    )
    with SeenStore(config.seen_db_path) as seen:
        traces = {
            job["company"]: build_posting_trace(
                job,
                config=config,
                seen_store=seen,
                posting_universe=jobs,
                duplicate_entries=duplicates,
            )
            for job in jobs
        }
    assert traces["Uber"].collection["direct_source_found"] is True
    assert traces["Uber"].collection["github_source_found"] is False
    assert traces["Google"].collection["direct_source_found"] is False
    assert traces["Google"].collection["github_source_found"] is True


def test_querying_duplicate_sighting_reports_deduplicated_duplicate(tmp_path):
    config = _config(tmp_path)
    direct = _row(
        company="Uber",
        source="direct",
        adapter="greenhouse",
        requisition="same-req",
        url="https://direct.example.com/jobs/same-req",
    )
    github = _row(
        company="Uber",
        source="github",
        adapter="feed",
        requisition="same-req",
        url="https://feed.example.com/jobs/same-req",
    )
    for row_value in (direct, github):
        row_value["extra"]["source_system"] = "employer"
        row_value["extra"]["source_scope"] = "uber"
    jobs, duplicates = _analyze(direct, github)
    with SeenStore(config.seen_db_path) as seen:
        report = build_source_comparison(
            config=config,
            jobs=jobs,
            seen_store=seen,
            run_id="dedupe-query",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            duplicate_report=duplicates,
        )
        with SourceComparisonStore(seen.path) as store:
            store.save(report)
        traces = audit_state_only(
            config=config,
            seen_store=seen,
            query=AuditQuery(url="https://feed.example.com/jobs/same-req"),
        )
    assert traces[0].final_result["reason"] == "deduplicated_duplicate"
    assert traces[0].deduplication["deduplicated_into_another"] is True


def test_generic_url_keeps_distinct_requisitions(tmp_path):
    config = _config(tmp_path)
    rows = [
        _row(
            company="Uber",
            source="direct",
            adapter="greenhouse",
            requisition=str(req),
            url="https://jobs.example.com/careers",
        )
        for req in (100, 200)
    ]
    jobs, duplicates = _analyze(*rows)
    assert len(jobs) == 2
    assert duplicates == ()
    with SeenStore(config.seen_db_path) as seen:
        traces = [
            build_posting_trace(
                job,
                config=config,
                seen_store=seen,
                posting_universe=jobs,
            )
            for job in jobs
        ]
    assert len({trace.identity["canonical_identity_key"] for trace in traces}) == 2
    assert all(trace.identity["generic_or_shared_url"] for trace in traces)
    assert all(
        len(trace.deduplication["similar_distinct_requisitions"]) == 1
        for trace in traces
    )


def test_trace_distinguishes_tracking_url_and_exact_fallback_duplicates(tmp_path):
    config = _config(tmp_path)
    tracking_first = _row(
        requisition="",
        url="https://example.com/jobs/tracking?utm_source=direct",
        source="direct",
        adapter="greenhouse",
    )
    tracking_second = _row(
        requisition="",
        url="https://example.com/jobs/tracking?utm_campaign=feed",
        source="github",
        adapter="feed",
    )
    jobs, duplicates = _analyze(tracking_first, tracking_second)
    with SeenStore(config.seen_db_path) as seen:
        tracking_trace = build_posting_trace(
            jobs[0],
            config=config,
            seen_store=seen,
            duplicate_entries=duplicates,
        )
    assert tracking_trace.deduplication["merge_reasons"] == ["source_url"]
    assert (
        tracking_trace.deduplication["merge_diagnostics"][0][
            "tracking_parameter_url_duplicate"
        ]
        is True
    )

    fallback_first = _row(requisition="", url="")
    fallback_second = _row(requisition="", url="")
    jobs, duplicates = _analyze(fallback_first, fallback_second)
    with SeenStore(config.seen_db_path) as seen:
        fallback_trace = build_posting_trace(
            jobs[0],
            config=config,
            seen_store=seen,
            duplicate_entries=duplicates,
        )
    assert fallback_trace.deduplication["merge_reasons"] == [
        "company+title+location"
    ]
    assert (
        fallback_trace.deduplication["merge_diagnostics"][0][
            "exact_fallback_duplicate"
        ]
        is True
    )


def test_live_audit_sends_no_email_and_changes_no_seen_rows(tmp_path):
    config = _config(tmp_path)

    class Source:
        def fetch(self, company):
            return [_row(company=company.name)]

    with SeenStore(config.seen_db_path) as seen:
        before = seen.records()
        traces, _report = audit_live(
            config=config,
            seen_store=seen,
            query=AuditQuery(company="Google"),
            direct_sources={},
            github_source=Source(),
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )
        after = seen.records()
    assert traces[0].collection["collected"] is True
    assert before == after


def test_live_audit_can_build_an_unretained_routine_trace_on_demand(tmp_path):
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
            company="Direct Co",
            title="Senior Marketing Manager",
            url=f"https://example.com/jobs/routine-{index}",
            source="direct",
            adapter="greenhouse",
            requisition=f"routine-{index}",
            internship_type="",
        )
        for index in range(80)
    ]
    jobs, duplicates = _analyze(*rows)

    class DirectSource:
        last_health_diagnostics = DirectSourceDiagnostics(
            succeeded=True,
            retained_row_count=80,
            complete=True,
        )

        def fetch(self, _company):
            return rows

    class EmptyGithubSource:
        def fetch(self, _companies):
            return []

    with SeenStore(config.seen_db_path) as seen:
        report = build_source_comparison(
            config=config,
            jobs=jobs,
            seen_store=seen,
            run_id="find-unretained",
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            duplicate_report=duplicates,
        )
        retained_urls = {
            entry.trace["posting"]["url"] for entry in report.entries
        }
        target = next(
            job for job in jobs
            if str(job["source_url"]) not in retained_urls
        )
        before = seen.records()
        traces, live_report = audit_live(
            config=config,
            seen_store=seen,
            query=AuditQuery(url=str(target["source_url"])),
            direct_sources={"greenhouse": DirectSource()},
            github_source=EmptyGithubSource(),
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )
        after = seen.records()

    assert live_report.detail_entries_retained == 25
    assert traces[0].posting["url"] == target["source_url"]
    assert traces[0].query_match["mode"] == "live_read_only"
    assert before == after


def test_live_audit_fills_broad_query_to_limit_beyond_retained_details(tmp_path):
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
            company="Direct Co",
            title="Senior Marketing Manager",
            url=f"https://example.com/jobs/broad-{index}",
            source="direct",
            adapter="greenhouse",
            requisition=f"broad-{index}",
            internship_type="",
        )
        for index in range(80)
    ]

    class DirectSource:
        last_health_diagnostics = DirectSourceDiagnostics(
            succeeded=True,
            retained_row_count=80,
            complete=True,
        )

        def fetch(self, _company):
            return rows

    class EmptyGithubSource:
        def fetch(self, _companies):
            return []

    with SeenStore(config.seen_db_path) as seen:
        traces, report = audit_live(
            config=config,
            seen_store=seen,
            query=AuditQuery(company="Direct Co"),
            limit=50,
            direct_sources={"greenhouse": DirectSource()},
            github_source=EmptyGithubSource(),
            observed_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        )

    assert report.detail_entries_retained == 25
    assert len(traces) == 50
    assert len(
        {trace.identity["canonical_identity_key"] for trace in traces}
    ) == 50


def test_state_only_performs_no_collection(tmp_path, monkeypatch):
    config = _config(tmp_path)

    def explode(*args, **kwargs):
        raise AssertionError("network collection must not run")

    monkeypatch.setattr("watcher.audit.collect_rows", explode)
    with SeenStore(config.seen_db_path) as seen:
        traces = audit_state_only(
            config=config,
            seen_store=seen,
            query=AuditQuery(company="Google"),
        )
    assert traces[0].final_result["reason"] == "not_collected"


def test_state_only_cli_does_not_create_a_missing_state_database(
    tmp_path,
    monkeypatch,
):
    config = _config(tmp_path)
    missing_db = tmp_path / "missing" / "audit.sqlite"
    monkeypatch.setattr(
        "watcher.audit.load_watchlist",
        lambda _path: config,
    )

    status = audit_main(
        [
            "--state-only",
            "--company",
            "Google",
            "--seen-db",
            str(missing_db),
        ]
    )

    assert status == 0
    assert not missing_db.exists()
    assert not missing_db.parent.exists()


def test_json_stable_serializable_and_console_bounded(tmp_path, capsys):
    config = _config(tmp_path)
    jobs, duplicates = _analyze(_row())
    with SeenStore(config.seen_db_path) as seen:
        trace = build_posting_trace(
            jobs[0],
            config=config,
            seen_store=seen,
            duplicate_entries=duplicates,
        )
    encoded = json.dumps(trace.as_dict(), sort_keys=True)
    assert json.loads(encoded)["schema_version"] == 1
    render_audit_console([trace], limit=1)
    output = capsys.readouterr().out
    assert "Collection" in output
    assert "Watcher eligibility" in output
    assert "Notification" in output
    assert "Final result" in output


def _shadow_seed(db_path, count=3):
    base = datetime(2026, 7, 1, tzinfo=timezone.utc)
    with SeenStore(db_path) as store:
        for index in range(count):
            store._conn.execute(
                "insert into shadow_generation_events(event_id, identity_key, "
                "company, trigger, stored_season_key, current_season_key, "
                "current_generation, proposed_generation, absence_epoch, "
                "absence_days, observed_at) "
                "values (?, ?, ?, 'season_change', 'season|summer|2026', "
                "'season|summer|2027', 1, 2, 0, null, ?)",
                (
                    f"evt-{index:03d}",
                    f"requisition|greenhouse|example|{index}",
                    "Example",
                    (base + timedelta(days=index)).isoformat(),
                ),
            )
        store._conn.commit()
    return base


def test_shadow_generations_mode_is_read_only_and_newest_first(
    tmp_path,
    monkeypatch,
    capsys,
):
    db_path = tmp_path / "seen.sqlite"
    _shadow_seed(db_path, count=3)
    monkeypatch.setattr("watcher.audit.load_watchlist", lambda _path: _config(tmp_path))
    before = db_path.read_bytes()

    exit_code = audit_main(["--seen-db", str(db_path), "--shadow-generations"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "Persisted shadow-generation events" in out
    # Newest first: the last seeded event precedes the first.
    assert out.index("example|2") < out.index("example|0")
    # State-only inspection writes nothing back to the database.
    assert db_path.read_bytes() == before


def test_shadow_generations_mode_honours_the_limit_bound(
    tmp_path,
    monkeypatch,
    capsys,
):
    db_path = tmp_path / "seen.sqlite"
    _shadow_seed(db_path, count=5)
    monkeypatch.setattr("watcher.audit.load_watchlist", lambda _path: _config(tmp_path))

    audit_main(["--seen-db", str(db_path), "--shadow-generations", "--limit", "2"])

    assert "Events shown: 2 (limit 2)" in capsys.readouterr().out


def test_shadow_generations_rejects_live_mode(tmp_path, monkeypatch):
    db_path = tmp_path / "seen.sqlite"
    _shadow_seed(db_path, count=1)
    monkeypatch.setattr("watcher.audit.load_watchlist", lambda _path: _config(tmp_path))

    with pytest.raises(SystemExit):
        audit_main(["--seen-db", str(db_path), "--shadow-generations", "--live"])


def test_shadow_generations_reports_an_empty_history_cleanly(
    tmp_path,
    monkeypatch,
    capsys,
):
    db_path = tmp_path / "seen.sqlite"
    with SeenStore(db_path):
        pass
    monkeypatch.setattr("watcher.audit.load_watchlist", lambda _path: _config(tmp_path))

    assert audit_main(["--seen-db", str(db_path), "--shadow-generations"]) == 0
    assert "No shadow-generation events have been recorded." in capsys.readouterr().out
