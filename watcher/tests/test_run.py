import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from watcher.config import CompanyCfg, GitHubListingSourceCfg, WatcherConfig
from watcher.notify import render_digest
from watcher.run import (
    CollectionStats,
    RUN_MODE_DRY,
    RUN_MODE_LIVE,
    RUN_MODE_PRIME,
    WorkdayTransportSummary,
    collect_rows,
    main as watcher_main,
    print_heartbeat,
    print_report,
    run_once,
    summarize_workday_transport,
)
from watcher.seen_store import SeenStore
from watcher.source_health import (
    ERROR_MISSING_ADAPTER,
    ERROR_SCHEMA,
    ERROR_UNEXPECTED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_HEALTHY,
    SourceHealthStore,
    render_final_heartbeat,
)
from watcher.sources.base import SourceError, SourceFetchError, SourceSchemaError, make_row
from watcher.sources.workday import WorkdaySource


class FakeSource:
    def __init__(self, rows_by_company=None, *, error=None):
        self.rows_by_company = rows_by_company or {}
        self.error = error

    def fetch(self, company):
        if self.error:
            raise self.error
        return self.rows_by_company.get(company.name, [])


class DiagnosticFakeSource(FakeSource):
    def __init__(
        self,
        rows_by_company=None,
        *,
        error=None,
        requests=0,
        retries=0,
    ):
        super().__init__(rows_by_company, error=error)
        self.last_diagnostics = SimpleNamespace(
            request_attempts=requests,
            retry_attempts=retries,
        )


class FakeGithub:
    def __init__(self, rows):
        self.rows = rows

    def fetch_many(self, companies):
        return self.rows


class CountingGithub:
    def __init__(self, url, rows=None, *, error=None):
        self.feed_label = url
        self.rows = rows or []
        self.error = error
        self.calls = 0

    def fetch_many(self, companies):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.rows)


class FakeDigestSender:
    def __init__(self, *, sent=True):
        self.sent = sent
        self.calls = []

    def __call__(self, matches):
        self.calls.append(list(matches))
        return self.sent


def row(
    company,
    title,
    *,
    source="direct",
    url=None,
    deadline="",
    description="Build Python APIs with React.",
    requirements="Python, SQL, REST APIs, Git",
):
    return make_row(
        source=source,
        source_adapter="fake",
        company=company,
        title=title,
        location="New York, NY",
        description=description,
        requirements=requirements,
        source_url=url or f"https://example.com/{company}/{title}".replace(" ", "-"),
        deadline=deadline,
        internship_type="Summer",
    )


def github_row(
    company,
    title,
    *,
    source_name,
    source_format,
    priority,
    url,
    active=True,
    description="",
):
    result = row(
        company,
        title,
        source="github",
        url=url,
        description=description,
    )
    result["extra"].update(
        {
            "source_name": source_name,
            "source_format": source_format,
            "source_priority": priority,
            "active": active,
            "closed": not active,
        }
    )
    return result


def test_run_once_filters_marks_seen_and_second_run_is_empty(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),
            CompanyCfg(name="GitHub", ats="github_only"),
        )
    )
    duplicate_url = "https://example.com/jobs/shared"
    direct_rows = [
        row("DirectCo", "Software Engineer Intern", source="direct", url=duplicate_url),
        row("DirectCo", "Marketing Intern", source="direct", description="Run campaigns."),
        row("DirectCo", "Software Engineer New Grad", source="direct", description="Build Python APIs."),
        row("DirectCo", "Software Engineer Intern Expired", source="direct", deadline="2026-01-01"),
    ]
    github_rows = [
        row("DirectCo", "Software Engineer Intern", source="github", url=duplicate_url, description=""),
        row("GitHub", "Software Engineering Intern", source="github", description=""),
    ]
    digest_sender = FakeDigestSender(sent=True)
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        first = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub(github_rows),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_LIVE,
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub(github_rows),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_LIVE,
        )

    assert [job["title"] for job in first.new_matches] == [
        "Software Engineer Intern",
        "Software Engineering Intern",
    ]
    assert first.new_matches[0]["extra"]["source"] == "direct"
    assert first.new_matches[1]["extra"]["source"] == "github"
    assert second.new_matches == []
    assert first.analysis_cache_stats.hits == 0
    assert first.analysis_cache_stats.misses == first.jobs_scored
    assert second.analysis_cache_stats.hits == second.jobs_scored
    assert second.analysis_cache_stats.misses == 0
    assert first.digest_sent is True
    assert first.seen_marked == 2
    assert [len(call) for call in digest_sender.calls] == [2, 0]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("select emailed_at from seen order by job_id").fetchall()
    assert len(rows) == 2
    assert all(row[0] == "2026-06-09T00:00:00+00:00" for row in rows)


def test_categorical_exclusion_is_audited_but_never_emailed_or_marked_seen(
    tmp_path,
):
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="Northrop Grumman", ats="greenhouse", token="northrop"),
        )
    )
    direct_rows = [
        row(
            "Northrop Grumman",
            "2027 Returning Intern Software Engineer",
            url="https://example.test/jobs/returning-101",
        ),
        row(
            "Northrop Grumman",
            "2027 Software Engineer Intern",
            url="https://example.test/jobs/open-202",
        ),
    ]
    digest_sender = FakeDigestSender(sent=True)
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        dry = run_once(
            config,
            seen_store=store,
            direct_sources={
                "greenhouse": FakeSource({"Northrop Grumman": direct_rows})
            },
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=digest_sender,
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_DRY,
        )
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("select count(*) from seen").fetchone()[0] == 0

        live = run_once(
            config,
            seen_store=store,
            direct_sources={
                "greenhouse": FakeSource({"Northrop Grumman": direct_rows})
            },
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=digest_sender,
            today=date(2026, 7, 28),
            seen_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_LIVE,
        )

    for result in (dry, live):
        assert [job["title"] for job in result.new_matches] == [
            "2027 Software Engineer Intern"
        ]
        assert len(result.eligibility_exclusions) == 1
        exclusion = result.eligibility_exclusions[0]
        assert exclusion["title"] == "2027 Returning Intern Software Engineer"
        assert exclusion["exclusion_reason"] == "returning_intern_only"
        assert exclusion["evidence_source"] == "title"
        assert exclusion["role"] == "swe"
        assert exclusion["role_track"] == "general_swe"

    assert [len(call) for call in digest_sender.calls] == [1]
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("select title, url from seen").fetchall()
    assert stored == [
        ("2027 Software Engineer Intern", "https://example.test/jobs/open-202")
    ]


def test_run_once_does_not_mark_seen_when_digest_not_sent(tmp_path):
    config = WatcherConfig(companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),))
    direct_rows = [row("DirectCo", "Software Engineer Intern")]
    digest_sender = FakeDigestSender(sent=False)

    with SeenStore(tmp_path / "seen.sqlite") as store:
        first = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            notification_mode=RUN_MODE_LIVE,
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            notification_mode=RUN_MODE_LIVE,
        )

    assert [job["title"] for job in first.new_matches] == ["Software Engineer Intern"]
    assert [job["title"] for job in second.new_matches] == ["Software Engineer Intern"]
    assert first.digest_sent is False
    assert first.seen_marked == 0
    assert [len(call) for call in digest_sender.calls] == [1, 1]
    with sqlite3.connect(tmp_path / "seen.sqlite") as conn:
        assert conn.execute("select count(*) from seen").fetchone()[0] == 0


def test_run_once_failed_live_send_exception_does_not_mark_emailed(tmp_path):
    config = WatcherConfig(
        companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),)
    )
    db_path = tmp_path / "seen.sqlite"

    def failed_sender(_matches):
        raise RuntimeError("simulated SMTP failure")

    with SeenStore(db_path) as store:
        with pytest.raises(RuntimeError, match="simulated SMTP failure"):
            run_once(
                config,
                seen_store=store,
                direct_sources={
                    "greenhouse": FakeSource(
                        {"DirectCo": [row("DirectCo", "Software Engineer Intern")]}
                    )
                },
                github_source=FakeGithub([]),
                alumni_index={},
                digest_sender=failed_sender,
                today=date(2026, 6, 9),
                notification_mode=RUN_MODE_LIVE,
            )

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from seen").fetchone()[0] == 0


def test_run_once_ordinary_dry_run_does_not_alter_notification_state(tmp_path):
    config = WatcherConfig(companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),))
    direct_rows = [row("DirectCo", "Software Engineer Intern")]
    digest_sender = FakeDigestSender(sent=True)
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            notification_mode=RUN_MODE_DRY,
        )

    assert len(result.new_matches) == 1
    assert result.dry_run_pending == 1
    assert result.digest_sent is False
    assert result.seen_marked == 0
    assert digest_sender.calls == []
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from seen").fetchone()[0] == 0


def test_run_once_can_prime_seen_store_without_sending(tmp_path):
    config = WatcherConfig(companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),))
    direct_rows = [row("DirectCo", "Software Engineer Intern")]
    digest_sender = FakeDigestSender(sent=False)
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        first = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_PRIME,
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            notification_mode=RUN_MODE_PRIME,
        )

    assert [job["title"] for job in first.new_matches] == ["Software Engineer Intern"]
    assert second.new_matches == []
    assert first.digest_sent is False
    assert first.seen_marked == 1
    assert digest_sender.calls == []
    with sqlite3.connect(db_path) as conn:
        seen_row = conn.execute("select first_seen, emailed_at, primed_at from seen").fetchone()
    assert seen_row == ("2026-06-09T00:00:00+00:00", None, "2026-06-09T00:00:00+00:00")


def test_six_distinct_requisitions_at_one_company_produce_six_matches(tmp_path):
    config = WatcherConfig(
        companies=(CompanyCfg(name="Google", ats="greenhouse", token="google"),)
    )
    google_rows = []
    for index in range(1, 7):
        posting = row(
            "Google",
            "Software Engineering Intern",
            url="https://careers.google.com/internships",
        )
        posting["extra"].update(
            {
                "source_adapter": "greenhouse",
                "source_system": "greenhouse",
                "source_requisition_id": f"GOOG-{index}",
            }
        )
        google_rows.append(posting)

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Google": google_rows})},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_DRY,
        )

    assert len(result.matches) == 6
    assert len(result.new_matches) == 6
    assert result.dry_run_pending == 6


def test_same_requisition_from_direct_and_github_produces_one_match(tmp_path):
    config = WatcherConfig(
        companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="directco"),)
    )
    direct = row(
        "DirectCo",
        "Software Engineering Intern",
        source="direct",
        url="https://careers.example.test/internships",
    )
    direct["extra"].update(
        {
            "source_adapter": "greenhouse",
            "source_system": "greenhouse",
            "source_requisition_id": "REQ-42",
        }
    )
    github = row(
        "DirectCo",
        "SWE Intern display wording",
        source="github",
        url="https://careers.example.test/internships",
    )
    github["extra"].update(
        {
            "source_adapter": "github_listings",
            "source_system": "greenhouse",
            "source_requisition_id": "REQ-42",
        }
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": [direct]})},
            github_source=FakeGithub([github]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_DRY,
        )

    assert len(result.matches) == 1
    assert len(result.new_matches) == 1
    assert result.cross_source_duplicates_merged == 1
    assert result.matches[0]["extra"]["source"] == "direct"


def test_notification_identity_integration_six_requisitions_and_cross_source_duplicate(
    tmp_path,
):
    config = WatcherConfig(
        companies=(CompanyCfg(name="Google", ats="greenhouse", token="google"),)
    )
    db_path = tmp_path / "notification-integration.sqlite"

    def google_posting(requisition_id):
        posting = row(
            "Google",
            "Software Engineering Intern",
            url="https://careers.google.com/internships",
        )
        posting["extra"].update(
            {
                "source_adapter": "greenhouse",
                "source_system": "greenhouse",
                "source_requisition_id": requisition_id,
            }
        )
        return posting

    first_six = [google_posting(f"GOOG-{index}") for index in range(1, 7)]
    github_duplicate = google_posting("GOOG-1")
    github_duplicate["title"] = "SWE Intern - GitHub display wording"
    github_duplicate["location"] = "Remote"
    github_duplicate["extra"].update(
        {
            "source": "github",
            "source_adapter": "github_listings",
        }
    )

    with SeenStore(db_path) as store:
        dry = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Google": first_six})},
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_DRY,
        )
        seen_after_dry = store._conn.execute("select count(*) from seen").fetchone()[0]

        live_sender = FakeDigestSender(sent=True)
        live = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Google": first_six})},
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=live_sender,
            today=date(2026, 7, 28),
            seen_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_LIVE,
        )
        emailed_after_live = store._conn.execute(
            "select count(*) from seen where emailed_at is not null"
        ).fetchone()[0]

        rerun = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"Google": first_six})},
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=True),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_LIVE,
        )

        seventh = google_posting("GOOG-7")
        primed = run_once(
            config,
            seen_store=store,
            direct_sources={
                "greenhouse": FakeSource({"Google": [*first_six, seventh]})
            },
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=True),
            today=date(2026, 7, 28),
            seen_at=datetime(2026, 7, 28, 1, tzinfo=timezone.utc),
            notification_mode=RUN_MODE_PRIME,
        )
        primed_rows = store._conn.execute(
            "select count(*) from seen where primed_at is not null"
        ).fetchone()[0]

        after_prime = run_once(
            config,
            seen_store=store,
            direct_sources={
                "greenhouse": FakeSource({"Google": [*first_six, seventh]})
            },
            github_source=FakeGithub([github_duplicate]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=True),
            today=date(2026, 7, 28),
            notification_mode=RUN_MODE_LIVE,
        )

    assert len(dry.matches) == 6
    assert len(dry.new_matches) == 6
    assert dry.cross_source_duplicates_merged == 1
    assert seen_after_dry == 0
    assert len(live.new_matches) == 6
    assert [len(batch) for batch in live_sender.calls] == [6]
    assert emailed_after_live == 6
    assert rerun.new_matches == []
    assert len(primed.new_matches) == 1
    assert primed_rows == 1
    assert after_prime.new_matches == []
    assert len(after_prime.previously_emailed) == 6
    assert len(after_prime.explicitly_primed) == 1

    print(
        "INTEGRATION "
        f"eligible={len(dry.matches)} "
        f"dry_new={len(dry.new_matches)} "
        f"seen_after_dry={seen_after_dry} "
        f"live_emailed={emailed_after_live} "
        f"rerun_new={len(rerun.new_matches)} "
        f"primed_seventh={primed_rows} "
        f"after_prime_new={len(after_prime.new_matches)} "
        f"cross_source_merged={dry.cross_source_duplicates_merged}"
    )


def test_run_once_passes_watchlist_aliases_to_alumni_join(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="AliasCo Software",
                ats="greenhouse",
                token="aliasco",
                alumni_match=("ShortCo",),
            ),
        )
    )
    direct_rows = [row("AliasCo Software", "Software Engineer Intern")]
    alumni_index = {
        "shortco": [{
            "name": "Ada Alias",
            "occupation": "Software Engineer",
            "linkedin_url": "https://www.linkedin.com/in/fake-ada-alias",
            "employer": "ShortCo",
        }]
    }

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"AliasCo Software": direct_rows})},
            github_source=FakeGithub([]),
            alumni_index=alumni_index,
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 6, 9),
        )

    assert [record["name"] for record in result.matches[0]["alumni"]] == ["Ada Alias"]


def test_collect_rows_logs_source_failure_and_keeps_going():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="BrokenCo", ats="greenhouse", token="broken"),
            CompanyCfg(name="GitHub", ats="github_only"),
        )
    )
    github_rows = [row("GitHub", "Software Engineering Intern", source="github")]

    rows, errors = collect_rows(
        config,
        direct_sources={"greenhouse": FakeSource(error=SourceError("boom"))},
        github_source=FakeGithub(github_rows),
    )

    assert rows == github_rows
    assert errors == ["BrokenCo: boom"]


def test_collect_rows_logs_successful_and_failed_source_timings(caplog):
    caplog.set_level("INFO", logger="watcher.run")
    successful_github = GitHubListingSourceCfg(
        name="safe_feed",
        format="simplify_json",
        url="https://example.test/safe.json",
    )
    failed_github = GitHubListingSourceCfg(
        name="failed_feed",
        format="github_markdown_table",
        url="https://example.test/failed.md",
        default_term="Summer 2027",
    )
    direct_row = row("Timed Direct", "Software Engineer Intern")
    github_row_value = row(
        "Timed Direct",
        "Backend Intern",
        source="github",
    )
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="Timed Direct", ats="workday", token="timed"),
            CompanyCfg(name="Broken Direct", ats="greenhouse", token="broken"),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(successful_github, failed_github),
    )

    rows, errors = collect_rows(
        config,
        direct_sources={
            "workday": DiagnosticFakeSource(
                {"Timed Direct": [direct_row]},
                requests=3,
                retries=1,
            ),
            "greenhouse": FakeSource(error=SourceError("fetch failed")),
        },
        github_source=[
            CountingGithub(successful_github.url, [github_row_value]),
            CountingGithub(failed_github.url, error=SourceFetchError("backstop failed")),
        ],
    )

    assert rows == [direct_row, github_row_value]
    assert errors == [
        "Broken Direct: fetch failed",
        "github listings (failed_feed [https://example.test/failed.md]): backstop failed",
    ]
    timing_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("SOURCE-TIMING ")
    ]
    assert len(timing_lines) == 4
    assert any(
        line.startswith(
            "SOURCE-TIMING company=Timed_Direct adapter=workday "
            "success=true "
        )
        and " rows=1 requests=3 retries=1" in line
        for line in timing_lines
    )
    assert any(
        line.startswith(
            "SOURCE-TIMING company=Broken_Direct adapter=greenhouse "
            "success=false "
        )
        and " rows=0" in line
        for line in timing_lines
    )
    assert any(
        "company=all adapter=simplify_json source=safe_feed success=true "
        in line
        and " rows=1" in line
        for line in timing_lines
    )
    assert any(
        "company=all adapter=github_markdown_table source=failed_feed "
        "success=false "
        in line
        and " rows=0" in line
        for line in timing_lines
    )
    assert all("https://" not in line for line in timing_lines)
    assert all(
        len(line.split(" seconds=", 1)[1].split(" ", 1)[0].split(".")[1]) == 3
        for line in timing_lines
    )


def test_collect_rows_skips_bespoke_and_github_only_for_direct_fetch():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="BespokeCo", ats="bespoke"),
            CompanyCfg(name="GitHub", ats="github_only"),
        )
    )
    github_rows = [row("GitHub", "Software Engineering Intern", source="github")]

    rows, errors = collect_rows(
        config,
        direct_sources={},
        github_source=FakeGithub(github_rows),
    )

    assert rows == github_rows
    assert errors == []


def test_run_once_logs_every_pipeline_stage_without_changing_results(tmp_path, caplog):
    caplog.set_level("INFO", logger="watcher.run")
    config = WatcherConfig(
        companies=(CompanyCfg(name="StageCo", ats="greenhouse", token="stage"),)
    )
    expected = row("StageCo", "Software Engineer Intern")

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"StageCo": [expected]})},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 30),
            run_id="timing-stage-test",
        )

    assert result.rows_fetched == 1
    assert result.jobs_scored == 1
    assert [match["company"] for match in result.matches] == ["StageCo"]
    stage_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("STAGE-TIMING ")
    ]
    expected_stages = {
        "direct_source_collection",
        "github_backstop_collection",
        "collection",
        "health_state_persistence",
        "analysis",
        "filtering_eligibility",
        "alumni_loading_matching",
        "seen_store_partitioning",
        "digest_email_handling",
        "source_comparison_generation_persistence",
        "health_alert_evaluation",
    }
    assert {
        line.split("stage=", 1)[1].split(" ", 1)[0]
        for line in stage_lines
    } == expected_stages
    assert all(
        len(line.rsplit("seconds=", 1)[1].split(".")[1]) == 3
        for line in stage_lines
    )


def test_main_logs_startup_and_total_runtime_stages(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="watcher.run")
    config = WatcherConfig(companies=())
    sentinel = object()
    monkeypatch.setattr("watcher.run.load_watchlist", lambda _path: config)
    monkeypatch.setattr("watcher.run.email_sending_enabled", lambda: False)
    monkeypatch.setattr("watcher.run.load_health_alert_policy", lambda: None)
    monkeypatch.setattr("watcher.run.run_once", lambda *_args, **_kwargs: sentinel)
    monkeypatch.setattr("watcher.run.print_report", lambda result: None)
    monkeypatch.setattr("watcher.run.print_heartbeat", lambda result: None)

    exit_code = watcher_main(
        [
            "--watchlist",
            str(tmp_path / "unused.yml"),
            "--seen-db",
            str(tmp_path / "seen.sqlite"),
        ]
    )

    assert exit_code == 0
    assert [
        record.getMessage().split("stage=", 1)[1].split(" ", 1)[0]
        for record in caplog.records
        if record.getMessage().startswith("STAGE-TIMING ")
    ] == ["configuration_startup", "watcher_runtime"]


def test_collect_rows_records_exactly_one_direct_outcome_per_company_and_one_per_feed():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="HealthyCo", ats="greenhouse", token="healthy"),
            CompanyCfg(name="BespokeCo", ats="bespoke"),
            CompanyCfg(name="GitHubOnlyCo", ats="github_only"),
        ),
        github_listing_urls=("https://example.test/listings.json",),
    )
    stats = CollectionStats()
    observed = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)

    collect_rows(
        config,
        direct_sources={"greenhouse": FakeSource({"HealthyCo": [row("HealthyCo", "Intern")]})},
        github_source=CountingGithub("https://example.test/listings.json"),
        stats=stats,
        run_id="fixed-run",
        observed_at=observed,
    )

    direct_attempts = [item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_DIRECT]
    github_attempts = [item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_GITHUB_FEED]
    assert [item.company for item in direct_attempts] == ["HealthyCo", "BespokeCo", "GitHubOnlyCo"]
    assert len(github_attempts) == 1
    assert {item.run_id for item in stats.source_attempts} == {"fixed-run"}
    assert {item.observed_at for item in stats.source_attempts} == {observed}
    assert direct_attempts[0].rows_returned == 1
    assert direct_attempts[1].unsupported_reason == "bespoke"
    assert direct_attempts[2].unsupported_reason == "github_only"


def test_collect_rows_classifies_missing_schema_and_unexpected_failures():
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="MissingCo", ats="lever", token="missing"),
            CompanyCfg(name="SchemaCo", ats="greenhouse", token="schema"),
            CompanyCfg(name="UnexpectedCo", ats="ashby", token="unexpected"),
        )
    )
    stats = CollectionStats()

    collect_rows(
        config,
        direct_sources={
            "greenhouse": FakeSource(error=SourceSchemaError("bad payload")),
            "ashby": FakeSource(error=ValueError("query_secret=hidden")),
        },
        github_source=FakeGithub([]),
        stats=stats,
        run_id="fixed-run",
        observed_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )

    errors = {item.company: item.error_kind for item in stats.source_attempts if item.company}
    assert errors == {
        "MissingCo": ERROR_MISSING_ADAPTER,
        "SchemaCo": ERROR_SCHEMA,
        "UnexpectedCo": ERROR_UNEXPECTED,
    }
    unexpected = next(item for item in stats.source_attempts if item.company == "UnexpectedCo")
    assert "hidden" not in unexpected.error_message


def test_collect_rows_preserves_an_explicitly_empty_source_registry(monkeypatch):
    config = WatcherConfig(
        companies=(CompanyCfg(name="NoAdapterCo", ats="greenhouse", token="unused"),)
    )
    def fail_if_defaults_are_built():
        raise AssertionError("default adapters should not be constructed")

    monkeypatch.setattr("watcher.run._default_direct_sources", fail_if_defaults_are_built)

    rows, errors = collect_rows(
        config,
        direct_sources={},
        github_source=FakeGithub([]),
    )

    assert rows == []
    assert errors == ["NoAdapterCo: no source registered for ats 'greenhouse'"]


def test_collect_rows_fetches_and_aggregates_every_configured_github_feed_once(monkeypatch):
    duplicate = row("GitHub", "Software Engineering Intern", source="github")
    sources = {
        "https://example.test/one.json": CountingGithub("https://example.test/one.json", [duplicate]),
        "https://example.test/two.json": CountingGithub("https://example.test/two.json", [duplicate]),
    }
    config = WatcherConfig(
        companies=(CompanyCfg(name="GitHub", ats="github_only", terms=("Summer 2027",)),),
        terms=("Summer 2027",),
        github_listing_urls=tuple(sources),
    )
    monkeypatch.setattr(
        "watcher.run.GitHubListingsSource",
        lambda url, **_kwargs: sources[url],
    )
    stats = CollectionStats()

    rows, errors = collect_rows(config, direct_sources={}, stats=stats)

    assert rows == [duplicate, duplicate]
    assert errors == []
    assert [source.calls for source in sources.values()] == [1, 1]
    assert stats.github_feeds_configured == 2
    assert stats.github_feeds_succeeded == 2


def test_one_failed_github_feed_keeps_successful_feed_rows_and_records_url(monkeypatch):
    good_row = row("GitHub", "Software Engineering Intern", source="github")
    sources = {
        "https://example.test/broken.json": CountingGithub(
            "https://example.test/broken.json",
            error=SourceError("request failed"),
        ),
        "https://example.test/good.json": CountingGithub("https://example.test/good.json", [good_row]),
    }
    config = WatcherConfig(
        companies=(CompanyCfg(name="GitHub", ats="github_only", terms=("Summer 2027",)),),
        terms=("Summer 2027",),
        github_listing_urls=tuple(sources),
    )
    monkeypatch.setattr(
        "watcher.run.GitHubListingsSource",
        lambda url, **_kwargs: sources[url],
    )
    stats = CollectionStats()

    rows, errors = collect_rows(config, direct_sources={}, stats=stats)

    assert rows == [good_row]
    assert errors == ["github listings (https://example.test/broken.json): request failed"]
    assert stats.github_feeds_configured == 2
    assert stats.github_feeds_succeeded == 1
    github_attempts = [item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_GITHUB_FEED]
    assert len(github_attempts) == 2
    assert [item.succeeded for item in github_attempts] == [False, True]


def test_collect_rows_accepts_multiple_injected_github_sources():
    urls = ("https://example.test/one.json", "https://example.test/two.json")
    sources = [CountingGithub(url) for url in urls]
    config = WatcherConfig(
        companies=(CompanyCfg(name="GitHub", ats="github_only"),),
        github_listing_urls=urls,
    )
    stats = CollectionStats()

    collect_rows(config, direct_sources={}, github_source=sources, stats=stats, run_id="fixed-run")

    assert [source.calls for source in sources] == [1, 1]
    assert len([item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_GITHUB_FEED]) == 2


def test_all_github_feeds_failing_does_not_remove_direct_rows(monkeypatch):
    direct_row = row("DirectCo", "Software Engineer Intern", source="direct")
    urls = ("https://example.test/one.json", "https://example.test/two.json")
    sources = {url: CountingGithub(url, error=SourceError("boom")) for url in urls}
    config = WatcherConfig(
        companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="direct"),),
        terms=("Summer 2027",),
        github_listing_urls=urls,
    )
    monkeypatch.setattr(
        "watcher.run.GitHubListingsSource",
        lambda url, **_kwargs: sources[url],
    )
    stats = CollectionStats()

    rows, errors = collect_rows(
        config,
        direct_sources={"greenhouse": FakeSource({"DirectCo": [direct_row]})},
        stats=stats,
    )

    assert rows == [direct_row]
    assert len(errors) == 2
    assert all(url in error for url, error in zip(urls, errors))
    assert stats.github_feeds_succeeded == 0


def test_run_merges_direct_simplify_and_markdown_by_fixed_priority(tmp_path):
    shared_url = "https://example.test/jobs/shared"
    simplify_config = GitHubListingSourceCfg(
        name="simplify",
        format="simplify_json",
        url="https://example.test/simplify.json",
    )
    markdown_config = GitHubListingSourceCfg(
        name="sndsh404_summer_2027",
        format="github_markdown_table",
        url="https://example.test/README.md",
        default_term="Summer 2027",
    )
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="DirectCo",
                ats="greenhouse",
                token="direct",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(markdown_config, simplify_config),
    )
    direct = row(
        "DirectCo",
        "Software Engineer Intern",
        source="direct",
        url=shared_url,
        description="",
    )
    simplify = github_row(
        "Simplify Company Name",
        "SWE Intern",
        source_name="simplify",
        source_format="simplify_json",
        priority=10,
        url=f"{shared_url}?utm_source=simplify",
        active=True,
        description="Structured feed description",
    )
    markdown = github_row(
        "Markdown Company Name",
        "Engineering Intern",
        source_name="sndsh404_summer_2027",
        source_format="github_markdown_table",
        priority=20,
        url=f"{shared_url}?ref=readme",
        active=False,
    )
    markdown["extra"]["source_added_date"] = "2026-07-20"
    sources = [
        CountingGithub(markdown_config.url, [markdown]),
        CountingGithub(simplify_config.url, [simplify]),
    ]

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": [direct]})},
            github_source=sources,
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 24),
        )

    assert result.rows_fetched == 3
    assert result.jobs_scored == 1
    assert len(result.matches) == 1
    merged = result.matches[0]
    assert merged["company"] == "DirectCo"
    assert merged["title"] == "Software Engineer Intern"
    assert merged["description"] == "Structured feed description"
    assert merged["extra"]["primary_source"] == "direct_ats"
    assert merged["extra"]["sources"] == [
        "direct_ats",
        "simplify",
        "sndsh404_summer_2027",
    ]
    assert merged["extra"]["active"] is True
    assert merged["extra"]["closed"] is False
    assert (
        merged["extra"]["source_details"]["sndsh404_summer_2027"]["active"]
        is False
    )


def test_typed_source_order_does_not_change_deterministic_results(tmp_path):
    simplify_config = GitHubListingSourceCfg(
        name="simplify",
        format="simplify_json",
        url="https://example.test/simplify.json",
    )
    markdown_config = GitHubListingSourceCfg(
        name="sndsh404_summer_2027",
        format="github_markdown_table",
        url="https://example.test/README.md",
        default_term="Summer 2027",
    )
    shared_url = "https://example.test/jobs/shared"
    simplify = github_row(
        "GitHub",
        "Software Engineering Intern",
        source_name="simplify",
        source_format="simplify_json",
        priority=10,
        url=shared_url,
        active=True,
    )
    markdown = github_row(
        "Different Markdown Name",
        "SWE Intern",
        source_name="sndsh404_summer_2027",
        source_format="github_markdown_table",
        priority=20,
        url=shared_url,
        active=True,
    )

    outputs = []
    for index, configured in enumerate(
        (
            (simplify_config, markdown_config),
            (markdown_config, simplify_config),
        )
    ):
        config = WatcherConfig(
            companies=(CompanyCfg(name="GitHub", ats="github_only", terms=("Summer 2027",)),),
            terms=("Summer 2027",),
            github_listing_sources=configured,
        )
        injected = [
            CountingGithub(markdown_config.url, [markdown]),
            CountingGithub(simplify_config.url, [simplify]),
        ]
        with SeenStore(tmp_path / f"seen-{index}.sqlite") as store:
            result = run_once(
                config,
                seen_store=store,
                direct_sources={},
                github_source=injected,
                alumni_index={},
                digest_sender=FakeDigestSender(sent=False),
                today=date(2026, 7, 24),
            )
        outputs.append(
            [
                {
                    "id": job["id"],
                    "company": job["company"],
                    "title": job["title"],
                    "extra": job["extra"],
                }
                for job in result.matches
            ]
        )

    assert outputs[0] == outputs[1]
    assert outputs[0][0]["company"] == "GitHub"
    assert outputs[0][0]["extra"]["primary_source"] == "simplify"


def test_markdown_failure_is_independent_in_health_and_keeps_other_results(tmp_path):
    simplify_config = GitHubListingSourceCfg(
        name="simplify",
        format="simplify_json",
        url="https://example.test/simplify.json",
    )
    markdown_config = GitHubListingSourceCfg(
        name="sndsh404_summer_2027",
        format="github_markdown_table",
        url="https://example.test/README.md",
        default_term="Summer 2027",
    )
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="DirectCo",
                ats="greenhouse",
                token="direct",
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(markdown_config, simplify_config),
    )
    direct = row("DirectCo", "Software Engineer Intern", source="direct")
    sources = [
        CountingGithub(markdown_config.url, error=SourceSchemaError("missing expected table")),
        CountingGithub(simplify_config.url, rows=[]),
    ]

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": [direct]})},
            github_source=sources,
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 24),
            run_id="typed-health-run",
        )

    github_attempts = [
        attempt
        for attempt in result.source_attempts
        if attempt.source_kind == SOURCE_KIND_GITHUB_FEED
    ]
    assert len(result.matches) == 1
    assert result.github_feeds_configured == 2
    assert result.github_feeds_succeeded == 1
    assert result.health_summary.github_feeds_healthy == 1
    assert result.health_summary.github_feeds_degraded == 1
    assert [(attempt.adapter, attempt.succeeded) for attempt in github_attempts] == [
        ("simplify_json", True),
        ("github_markdown_table", False),
    ]
    assert "simplify" in github_attempts[0].feed_label
    assert "sndsh404_summer_2027" in github_attempts[1].feed_label
    assert len(result.errors) == 1


def test_closed_markdown_only_row_is_scored_but_not_notified(tmp_path):
    markdown_config = GitHubListingSourceCfg(
        name="sndsh404_summer_2027",
        format="github_markdown_table",
        url="https://example.test/README.md",
        default_term="Summer 2027",
    )
    config = WatcherConfig(
        companies=(CompanyCfg(name="GitHub", ats="github_only", terms=("Summer 2027",)),),
        terms=("Summer 2027",),
        github_listing_sources=(markdown_config,),
    )
    closed = github_row(
        "GitHub",
        "Software Engineering Intern",
        source_name="sndsh404_summer_2027",
        source_format="github_markdown_table",
        priority=20,
        url="https://example.test/jobs/closed",
        active=False,
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={},
            github_source=CountingGithub(markdown_config.url, [closed]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 7, 24),
        )

    assert result.jobs_scored == 1
    assert result.matches == []
    assert result.new_matches == []
    assert result.health_summary.github_feeds_healthy == 1


def test_run_result_exposes_season_feed_counts_and_stale_company_warning(tmp_path, caplog):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Stale Override Co",
                ats="github_only",
                terms=("Summer 2026",),
            ),
        ),
        terms=("Summer 2027",),
        github_listing_urls=("https://example.test/listings.json",),
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            today=date(2027, 7, 15),
        )

    assert result.configured_terms == ("Summer 2027",)
    assert result.season_status == "rollover_due"
    assert result.github_feeds_configured == 1
    assert result.github_feeds_succeeded == 1
    assert result.company_season_warnings == (
        "Stale Override Co: stale company terms override (Summer 2026)",
    )
    assert "SEASON WARNING: rollover_due" in caplog.text
    assert "Stale Override Co: stale company terms override" in caplog.text


def test_run_once_persists_health_without_matches_email_or_seen_marking(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="EmptyCo", ats="greenhouse", token="empty"),
            CompanyCfg(name="BackstopCo", ats="github_only"),
        ),
        github_listing_urls=("https://example.test/listings.json",),
    )
    digest_sender = FakeDigestSender(sent=False)
    observed = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)

    with SeenStore(db_path) as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"EmptyCo": []})},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=digest_sender,
            today=date(2026, 7, 16),
            run_id="fixed-run",
            health_observed_at=observed,
        )

    assert result.run_id == "fixed-run"
    assert result.health_summary.direct_empty == 1
    assert result.health_summary.direct_unsupported == 1
    assert result.health_summary.github_feeds_healthy == 1
    assert result.health_summary.backstop_only_companies == 1
    assert result.matches == []
    assert result.seen_marked == 0
    assert digest_sender.calls == [[]]
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("select count(*) from seen").fetchone()[0] == 0
        assert conn.execute(
            "select count(*) from source_health_attempts where run_id = ?", ("fixed-run",)
        ).fetchone()[0] == 3


def test_run_once_reuses_injected_health_store_and_detects_recovery(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    config = WatcherConfig(companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="direct"),))
    observed = datetime(2026, 7, 16, tzinfo=timezone.utc)

    with SeenStore(db_path) as seen_store, SourceHealthStore(db_path) as health_store:
        failed = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"greenhouse": FakeSource(error=SourceError("boom"))},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="run-failed",
            health_observed_at=observed,
        )
        recovered = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": [row("DirectCo", "Intern")]})},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="run-recovered",
            health_observed_at=observed.replace(hour=1),
        )

    assert failed.health_summary.direct_degraded == 1
    assert recovered.health_summary.direct_healthy == 1
    assert recovered.health_summary.health_recoveries == 1
    assert recovered.health_transitions[0].recovery is True
    assert recovered.source_health_states[recovered.source_attempts[0].health_key].status == STATUS_HEALTHY


def test_partial_workday_fetch_is_successful_and_recovers_prior_degraded_state(tmp_path, monkeypatch):
    db_path = tmp_path / "seen.sqlite"
    company = CompanyCfg(
        name="Merck",
        ats="workday",
        token="merck",
        workday_shard="wd5",
        workday_site="Search_Jobs",
    )
    config = WatcherConfig(companies=(company,))
    observed = datetime(2026, 7, 16, tzinfo=timezone.utc)
    payload = {
        "jobPostings": [
            {"title": "Malformed Workday Posting", "externalPath": ""},
            {
                "title": "Software Engineer Intern",
                "externalPath": "/job/Rahway-NJ/Software-Engineer-Intern_R123",
                "locationsText": "Rahway, NJ",
                "jobDescription": "Build Python backend APIs and SQL services.",
                "bulletFields": ["R123"],
            },
        ],
        "total": 2,
    }
    monkeypatch.setattr("watcher.sources.workday.post_json", lambda url, data, source_name: payload)

    with SeenStore(db_path) as seen_store, SourceHealthStore(db_path) as health_store:
        failed = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"workday": FakeSource(error=SourceSchemaError("malformed posting"))},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="merck-failed",
            health_observed_at=observed,
        )
        recovered = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"workday": WorkdaySource()},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="merck-recovered",
            health_observed_at=observed.replace(hour=1),
        )

    direct_attempt = next(
        item for item in recovered.source_attempts if item.source_kind == SOURCE_KIND_DIRECT
    )
    assert failed.health_summary.direct_degraded == 1
    assert recovered.errors == []
    assert direct_attempt.succeeded is True
    assert direct_attempt.rows_returned == 1
    assert recovered.health_summary.direct_healthy == 1
    assert recovered.health_summary.health_recoveries == 1
    assert recovered.health_transitions[0].company == "Merck"
    assert recovered.health_transitions[0].recovery is True


def test_twenty_four_identical_workday_transport_failures_are_shared_incident():
    stats = CollectionStats(
        workday_attempted=59,
        workday_succeeded=35,
        workday_failed=24,
        workday_retry_attempts=48,
    )
    stats.workday_failure_codes["html_challenge"] = 24

    summary = summarize_workday_transport(stats)

    assert summary == WorkdayTransportSummary(
        attempted_tenants=59,
        successful_tenants=35,
        failed_tenants=24,
        retry_attempts=48,
        dominant_error="html_challenge",
        dominant_error_count=24,
        likely_shared_incident=True,
    )


def test_non_workday_collection_does_not_invoke_workday_pacing(monkeypatch):
    def unexpected_pacing(self):
        pytest.fail("Workday pacing was used for a non-Workday adapter")

    monkeypatch.setattr(
        "watcher.sources.workday.WorkdayPacer.wait_for_tenant",
        unexpected_pacing,
    )
    config = WatcherConfig(
        companies=(CompanyCfg(name="Greenhouse Co", ats="greenhouse", token="board"),)
    )

    rows, errors = collect_rows(
        config,
        direct_sources={
            "greenhouse": FakeSource(
                {"Greenhouse Co": [row("Greenhouse Co", "Software Engineer Intern")]}
            )
        },
        github_source=FakeGithub([]),
    )

    assert len(rows) == 1
    assert errors == []


def test_isolated_or_mixed_workday_failures_do_not_create_false_incident():
    isolated = CollectionStats(workday_attempted=2, workday_failed=2)
    isolated.workday_failure_codes["html_challenge"] = 2
    assert summarize_workday_transport(isolated).likely_shared_incident is False

    mixed = CollectionStats(workday_attempted=10, workday_failed=10)
    mixed.workday_failure_codes.update(
        {"html_challenge": 5, "rate_limited": 3, "timeout": 2}
    )
    assert summarize_workday_transport(mixed).likely_shared_incident is False


def test_workday_shared_incident_rule_is_deterministic_at_sixty_percent():
    stats = CollectionStats(workday_attempted=10, workday_failed=10)
    stats.workday_failure_codes.update({"html_challenge": 6, "timeout": 4})

    first = summarize_workday_transport(stats)
    second = summarize_workday_transport(stats)

    assert first == second
    assert first.likely_shared_incident is True


def test_collect_rows_persists_each_workday_attempt_and_stable_transport_subtype():
    companies = tuple(
        CompanyCfg(
            name=f"Workday Co {index}",
            ats="workday",
            token=f"tenant{index}",
            workday_shard="wd5",
            workday_site="Site",
        )
        for index in range(5)
    )
    error = SourceFetchError(
        "challenge response",
        error_code="html_challenge",
        retryable=True,
        attempt_count=3,
    )
    stats = CollectionStats()

    collect_rows(
        WatcherConfig(companies=companies),
        direct_sources={"workday": FakeSource(error=error)},
        github_source=FakeGithub([]),
        stats=stats,
        run_id="workday-incident",
    )

    direct_attempts = [
        item for item in stats.source_attempts if item.source_kind == SOURCE_KIND_DIRECT
    ]
    assert len(direct_attempts) == 5
    assert all(item.error_kind == "fetch_failure/html_challenge" for item in direct_attempts)
    assert all(item.succeeded is False for item in direct_attempts)
    assert summarize_workday_transport(stats).likely_shared_incident is True


def test_print_report_for_matches_and_empty(capsys):
    result = type("Result", (), {
        "errors": [],
        "new_matches": [{
            "company": "DirectCo",
            "title": "Software Engineer Intern",
            "location": "New York, NY",
            "source_url": "https://example.com/jobs/1",
            "extra": {"source": "direct"},
            "score": {"total": 80, "action_label": "Apply now", "reasons": ["Strong role match"]},
            "red_flags": [{"label": "Compensation unclear or unstated"}],
        }],
        "eligibility_exclusions": ({
            "company": "Northrop Grumman",
            "title": "2027 Returning Intern Software Engineer",
            "exclusion_reason": "returning_intern_only",
            "evidence_source": "title",
            "evidence": "2027 Returning Intern Software Engineer",
        },),
    })()

    print_report(result)
    output = capsys.readouterr().out
    assert "New matches: 1" in output
    assert "Configured internship terms: (none)" in output
    assert "Season status: unknown" in output
    assert "GitHub backstop feeds: 0 configured, 0 succeeded" in output
    assert "[direct] DirectCo" in output
    assert "Strong role match" in output
    assert "Categorical eligibility exclusions: 1" in output
    assert "returning_intern_only [title]" in output

    empty = type("Result", (), {"errors": [], "new_matches": []})()
    print_report(empty)
    assert "No new matches." in capsys.readouterr().out


def test_print_heartbeat(capsys):
    result = type("Result", (), {
        "rows_fetched": 3,
        "jobs_scored": 2,
        "matches": [{}, {}],
        "new_matches": [{}],
        "errors": ["BrokenCo: boom"],
        "season_status": "ok",
        "configured_terms": ("Fall 2026", "Summer 2027"),
        "github_feeds_configured": 2,
        "github_feeds_succeeded": 1,
        "alumni_csv_status": "loaded",
        "alumni_records_loaded": 124,
        "alumni_employers_indexed": 80,
        "digest_sent": False,
        "seen_marked": 1,
        "workday_transport": WorkdayTransportSummary(
            attempted_tenants=3,
            successful_tenants=2,
            failed_tenants=1,
            retry_attempts=2,
        ),
    })()

    print_heartbeat(result)

    assert capsys.readouterr().out == (
        "HEARTBEAT: ran, rows_fetched=3, jobs_scored=2, matches=2, "
        "new=1, emailed_suppressed=0, primed_suppressed=0, dry_run_pending=0, "
        "cross_source_duplicates_merged=0, errors=1, notification_mode=unknown, "
        "season_status=ok, configured_terms=Fall_2026|Summer_2027, "
        "github_feeds_configured=2, github_feeds_succeeded=1, "
        "companies_configured=0, direct_healthy=0, direct_empty=0, direct_degraded=0, "
        "direct_failing=0, direct_unsupported=0, github_feeds_healthy=0, "
        "backstop_only_companies=0, uncovered_companies=0, health_transitions=0, "
        "health_recoveries=0, health_email_mode=off, health_alert_candidates=0, "
        "health_alert_sent=no, health_alert_suppressed_by_cooldown=0, "
        "health_recovery_alerts=0, health_alert_error=no, "
        "source_comparison_github_only=0, source_comparison_direct_only=0, "
        "source_comparison_both=0, source_comparison_persisted=no, "
        "workday_attempted=3, workday_succeeded=2, workday_failed=1, "
        "workday_retry_attempts=2, workday_shared_incident=0, "
        "collection_mode=serial, collection_max_workers=0, "
        "collection_max_observed_concurrency=0, "
        "collection_max_observed_origin_concurrency=0, "
        "collection_max_observed_workday_concurrency=0, "
        "collection_unexpected_task_exceptions=0, "
        "alumni_csv_status=loaded, alumni_records_loaded=124, "
        "alumni_employers_indexed=80, sent=no, seen_marked=1\n"
    )


def test_workflow_preserves_season_and_feed_heartbeat_fields():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    for field in (
        "season_status",
        "configured_terms",
        "github_feeds_configured",
        "github_feeds_succeeded",
    ):
        assert f"{field}=\\([^,]*\\)" in workflow or f"extract_count {field}" in workflow
        assert f'echo "{field}=' in workflow


def test_workflow_preserves_health_fields_validates_db_and_renders_summary():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    health_fields = (
        "companies_configured",
        "direct_healthy",
        "direct_empty",
        "direct_degraded",
        "direct_failing",
        "direct_unsupported",
        "github_feeds_healthy",
        "backstop_only_companies",
        "uncovered_companies",
        "health_transitions",
        "health_recoveries",
    )
    for field in health_fields:
        assert f"extract_count {field}" in workflow
        assert f'echo "{field}=' in workflow
    assert "WATCHER_HEALTH_REPORT_PATH" in workflow
    assert "$GITHUB_STEP_SUMMARY" in workflow
    assert "python -m watcher.source_health workflow-report" in workflow
    assert "source_health_attempts" in workflow
    assert "source_health_current" in workflow
    assert "select count(*) from seen" in workflow
    assert "where run_id = ?" in workflow
    assert "::error::SEEN-STORE" in workflow
    assert "git worktree add -B \"$DATA_BRANCH\"" in workflow
    assert "checkout --orphan \"$DATA_BRANCH\"" in workflow
    assert "push origin \"HEAD:$DATA_BRANCH\"" in workflow
    assert "git branch -D watcher-data" not in workflow


def test_workflow_caches_analysis_database_without_committing_it():
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml"
    ).read_text(encoding="utf-8")

    assert "WATCHER_ANALYSIS_CACHE_PATH:" in workflow
    assert "actions/cache@v4" in workflow
    assert "STATIC_ANALYSIS_CACHE_VERSION" in workflow
    assert "date -u +%Y-%m-%d" in workflow
    assert "watcher-analysis-${{ runner.os }}-v" in workflow
    assert "restore-keys:" in workflow
    assert "Validate restored analysis cache" in workflow
    assert "pragma quick_check" in workflow
    assert "corrupt-analysis-cache.sqlite" in workflow
    assert "scripts/migrate_analysis_cache.py" in workflow
    assert "--remove-source-table" in workflow
    assert '"analysis_cache" not in tables' in workflow
    save_step = workflow.split(
        "- name: Save seen-store to data branch",
        1,
    )[1].split("- name: Source-health summary", 1)[0]
    assert 'cp "$SEEN_DB_PATH" "$data_worktree/$DATA_DB_FILE"' in save_step
    assert "WATCHER_ANALYSIS_CACHE_PATH" not in save_step


def test_workflow_workday_probe_is_isolated_from_email_seen_and_data_branch():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    assert "workday_transport_probe" in workflow
    assert "python scripts/probe_workday_transport.py" in workflow
    assert 'WATCHER_SEND_EMAIL: "0"' in workflow
    assert 'WATCHER_HEALTH_EMAIL_MODE: "off"' in workflow
    assert "WATCHER_WORKDAY_MIN_INTERVAL_SECONDS" in workflow
    probe_job = workflow.split("  workday-transport-probe:", 1)[1].split("  watcher:", 1)[0]
    assert "--mark-seen-without-send" not in probe_job
    assert "watcher-data" not in probe_job
    assert "WATCHER_SEEN_DB" not in probe_job
    assert "SMTP_" not in probe_job


def test_workflow_health_email_is_independent_and_comparison_is_reported():
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml"
    ).read_text(encoding="utf-8")

    assert "health_email_mode:" in workflow
    assert "WATCHER_HEALTH_EMAIL_MODE" in workflow
    assert "WATCHER_HEALTH_EMAIL_HOUR_UTC" in workflow
    assert "WATCHER_HEALTH_ALERT_COOLDOWN_HOURS" in workflow
    assert "WATCHER_FEED_STALE_HOURS" in workflow
    for mode in ("off", "transitions_only", "failure_only", "daily_summary"):
        assert f'- "{mode}"' in workflow
    for field in (
        "health_alert_candidates",
        "health_alert_suppressed_by_cooldown",
        "health_recovery_alerts",
        "source_comparison_github_only",
        "source_comparison_direct_only",
        "source_comparison_both",
    ):
        assert f"extract_count {field}" in workflow
        assert f'echo "{field}=' in workflow
    assert "python -m watcher.audit" in workflow
    assert "--comparison-json" in workflow
    assert "--comparison-markdown" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert (
        "Source-health email delivery failed; internship-match outcome is unaffected."
        in workflow
    )


def test_workflow_forwards_exact_application_heartbeat_and_keeps_existing_outputs():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    assert "grep '^HEARTBEAT:' \"$RUNNER_TEMP/watcher-run.log\" | tail -n 1" in workflow
    assert 'echo "application_heartbeat<<WATCHER_HEARTBEAT_EOF"' in workflow
    assert "printf '%s\\n' \"$heartbeat\"" in workflow
    assert "[[ \"$heartbeat\" == *$'\\n'* || \"$heartbeat\" == *$'\\r'* ]]" in workflow
    assert "APPLICATION_HEARTBEAT: ${{ steps.run_watcher.outputs.application_heartbeat }}" in workflow
    assert "python -m watcher.source_health final-heartbeat" in workflow
    assert 'echo "HEARTBEAT: ran, rows_fetched=' not in workflow
    assert "application heartbeat unavailable; no final success heartbeat was fabricated" in workflow
    assert "watcher.run did not emit an application heartbeat" in workflow
    assert "Watcher completed with $ERRORS source error(s)" in workflow
    assert "eval " not in workflow
    assert 'source "$' not in workflow

    application = (
        "HEARTBEAT: ran, rows_fetched=10, jobs_scored=9, matches=2, new=1, errors=0, "
        "season_status=ok, configured_terms=Summer_2027, github_feeds_configured=1, "
        "github_feeds_succeeded=1, companies_configured=3, direct_healthy=2, "
        "direct_empty=0, direct_degraded=0, direct_failing=0, direct_unsupported=1, "
        "github_feeds_healthy=1, backstop_only_companies=1, uncovered_companies=0, "
        "health_transitions=0, health_recoveries=0, alumni_csv_status=loaded-json-map, "
        "alumni_records_loaded=2, alumni_employers_indexed=2, sent=no, seen_marked=0, "
        "future_metric=123"
    )
    final = render_final_heartbeat(
        application,
        seen_loaded=7,
        seen_saved=8,
        load_status="loaded",
        save_status="pushed",
    )
    assert final.startswith(application)
    assert "future_metric=123" in final
    assert "season_status=ok" in final
    assert "github_feeds_succeeded=1" in final
    assert "direct_healthy=2" in final
    assert "alumni_csv_status=loaded-json-map" in final
    assert "sent=no, seen_marked=0" in final
    assert final.endswith("seen_loaded=7, seen_saved=8, seen_store=loaded/pushed")
    assert "\n" not in final and "\r" not in final

    for field in (
        "rows_fetched",
        "season_status",
        "github_feeds_configured",
        "direct_healthy",
        "alumni_csv_status",
        "sent",
        "seen_marked",
    ):
        assert f'echo "{field}=' in workflow


def test_workflow_dry_and_prime_modes_are_explicit_and_incompatible_with_live_send():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    assert "prime_seen:" in workflow
    assert "inputs.prime_seen" in workflow
    assert "ACTIONS_WATCHER_PRIME_SEEN" in workflow
    assert "export WATCHER_SEND_EMAIL=0" in workflow
    assert "export WATCHER_SUPPRESS_DRY_RUN_DIGEST=1" in workflow
    assert "unset WATCHER_SEND_EMAIL" not in workflow
    assert 'if [ "${{ steps.mode.outputs.prime_seen }}" = "true" ]; then' in workflow
    assert "args+=(--prime-seen)" in workflow
    assert "cannot both be true" in workflow

    run_step = workflow.split("      - name: Run watcher", 1)[1].split(
        "      - name: Save seen-store", 1
    )[0]
    dry_branch = run_step.split("else", 1)[1]
    assert "--mark-seen-without-send" not in workflow
    assert "args+=(--prime-seen)" in dry_branch


def test_workflow_scheduled_dry_runs_do_not_silently_prime():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    assert "raw_prime=\"${ACTIONS_WATCHER_PRIME_SEEN:-}\"" in workflow
    assert 'ACTIONS_WATCHER_PRIME_SEEN: ${{ vars.WATCHER_PRIME_SEEN }}' in workflow
    assert "prime_seen=false" in workflow


def test_workflow_yaml_parses_successfully():
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert isinstance(document, dict)
    assert "jobs" in document
    assert "watcher" in document["jobs"]


def test_synthetic_digest_excludes_non_swe_engineering_and_ranks_backend_java(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="Bosch", ats="greenhouse", token="bosch"),
            CompanyCfg(name="HackerRank", ats="greenhouse", token="hackerrank"),
            CompanyCfg(name="Anduril Industries", ats="greenhouse", token="anduril"),
        )
    )
    direct_rows = {
        "Bosch": [
            row(
                "Bosch",
                "IT Internship (BackEnd, Java)",
                description="Build BackEnd services and REST APIs in Java.",
                requirements="Java, SQL, Git",
            ),
            row(
                "Bosch",
                "Cloud Developer Internship",
                description="Build cloud APIs and platform services in Python.",
                requirements="AWS, Python, Docker",
            ),
            row(
                "Bosch",
                "DevOps Engineering Intern",
                description="Own developer tooling and automation code for backend infrastructure APIs.",
                requirements="Python, Docker, Linux",
            ),
            row(
                "Bosch",
                "Mechanical Design Engineer",
                description="Design mechanical components for manufacturing.",
                requirements="CAD, fixtures, manufacturing",
            ),
            row(
                "Bosch",
                "Factory Automation Engineering Intern",
                description="Support PLCs and plant automation equipment.",
                requirements="PLC, manufacturing, electrical systems",
            ),
        ],
        "HackerRank": [
            row(
                "HackerRank",
                "Customer Experience Engineer - Intern",
                description="Help customers troubleshoot issues and answer support tickets.",
                requirements="Customer support, SQL",
            ),
        ],
        "Anduril Industries": [
            row(
                "Anduril Industries",
                "2027 Electrical Engineer Intern",
                description="Design and test electrical hardware.",
                requirements="Circuits, PCB, lab equipment",
            ),
            row(
                "Anduril Industries",
                "2027 Manufacturing Engineer Intern",
                description="Improve manufacturing processes on the factory floor.",
                requirements="Manufacturing, process engineering",
            ),
            row(
                "Anduril Industries",
                "2027 Software Engineer Intern",
                description="Build backend APIs and production services.",
                requirements="Python, Java, SQL, REST APIs",
            ),
        ],
    }

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource(direct_rows)},
            github_source=FakeGithub([]),
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 6, 9),
        )

    subject, body = render_digest(result.new_matches)

    assert subject == "Internship Watcher: 4 new SWE-intern matches"
    assert "IT Internship (BackEnd, Java)" in body
    assert "Cloud Developer Internship" in body
    assert "DevOps Engineering Intern" in body
    assert "2027 Software Engineer Intern" in body
    for excluded in (
        "Mechanical Design Engineer",
        "Factory Automation Engineering Intern",
        "Customer Experience Engineer - Intern",
        "2027 Electrical Engineer Intern",
        "2027 Manufacturing Engineer Intern",
    ):
        assert excluded not in body

    assert body.index("IT Internship (BackEnd, Java)") < body.index("Cloud Developer Internship")
    assert body.index("IT Internship (BackEnd, Java)") < body.index("DevOps Engineering Intern")


def test_synthetic_digest_excludes_graduate_roles_and_attaches_alumni(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Bosch",
                ats="greenhouse",
                token="bosch",
                aliases=("Bosch Group",),
                alumni_match=("bosch group",),
            ),
            CompanyCfg(
                name="Tesla",
                ats="greenhouse",
                token="tesla",
                aliases=("Tesla Motors",),
                alumni_match=("tesla", "tesla motors"),
            ),
            CompanyCfg(name="ResearchCo", ats="greenhouse", token="researchco"),
            CompanyCfg(name="UndergradCo", ats="greenhouse", token="undergradco"),
        )
    )
    direct_rows = {
        "Bosch": [
            row(
                "Bosch",
                "IT Internship (BackEnd, Java)",
                description="Build BackEnd services and REST APIs in Java.",
                requirements="Java, SQL, Git",
            ),
            row(
                "Bosch",
                "Machine Learning Engineer PhD Intern",
                description="Build Python ML services and data pipelines.",
                requirements="Python, SQL, Pandas",
            ),
        ],
        "Tesla": [
            row(
                "Tesla",
                "Fullstack Software Engineer Intern",
                description="Build full-stack web apps with React, TypeScript, Python APIs, and SQL.",
                requirements="React, TypeScript, Python, SQL, GitHub",
            ),
            row(
                "Tesla",
                "Software Engineer Intern - Masters",
                description="Build Python backend APIs with SQL.",
                requirements="Python, SQL, REST APIs",
            ),
        ],
        "ResearchCo": [
            row(
                "ResearchCo",
                "Graduate Research Intern",
                description="Research software systems.",
                requirements="Python, SQL",
            ),
            row(
                "ResearchCo",
                "Postdoctoral Research Intern",
                description="Research ML systems.",
                requirements="Python, SQL",
            ),
        ],
        "UndergradCo": [
            row(
                "UndergradCo",
                "Undergraduate Software Engineer Intern",
                description="Build Python backend services and REST APIs.",
                requirements="Python, SQL, REST APIs, Git",
            ),
        ],
    }
    alumni_index = {
        "bosch group": [{
            "name": "Ada Bosch",
            "occupation": "Backend Engineer",
            "linkedin_url": "https://www.linkedin.com/in/fake-bosch",
            "employer": "Bosch Group",
        }],
        "tesla": [{
            "name": "Nikola Tesla",
            "occupation": "Software Engineer",
            "linkedin_url": "https://www.linkedin.com/in/fake-tesla",
            "employer": "Tesla",
        }],
    }

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource(direct_rows)},
            github_source=FakeGithub([]),
            alumni_index=alumni_index,
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 6, 9),
        )

    subject, body = render_digest(
        result.new_matches,
        alumni_summary={
            "status": result.alumni_csv_status,
            "records_loaded": result.alumni_records_loaded,
            "employers_indexed": result.alumni_employers_indexed,
        },
    )

    assert subject == "Internship Watcher: 3 new SWE-intern matches"
    assert "IT Internship (BackEnd, Java)" in body
    assert "Fullstack Software Engineer Intern" in body
    assert "Undergraduate Software Engineer Intern" in body
    assert "Ada Bosch - Backend Engineer - https://www.linkedin.com/in/fake-bosch" in body
    assert "Nikola Tesla - Software Engineer - https://www.linkedin.com/in/fake-tesla" in body
    assert "Alumni index: 2 records across 2 employers" in body
    for excluded in (
        "Machine Learning Engineer PhD Intern",
        "Software Engineer Intern - Masters",
        "Graduate Research Intern",
        "Postdoctoral Research Intern",
    ):
        assert excluded not in body


def test_mixed_digest_reserves_above_94_fit_for_near_perfect_resume_matches(tmp_path):
    config = WatcherConfig(companies=(CompanyCfg(name="FitCo", ats="greenhouse", token="fitco"),))
    direct_rows = {
        "FitCo": [
            row(
                "FitCo",
                "Backend Engineer Intern",
                description="Build Python FastAPI REST APIs with SQLAlchemy and PostgreSQL.",
                requirements="Python, FastAPI, SQLAlchemy, SQL, PostgreSQL, GitHub, Pytest",
            ),
            row(
                "FitCo",
                "Full Stack Engineer Intern",
                description="Build full-stack web apps with React, TypeScript, Next.js, Python APIs and SQL.",
                requirements="React, TypeScript, Next.js, Python, SQL, GitHub",
            ),
            row(
                "FitCo",
                "Data Engineer Intern",
                description="Build Python data ingestion pipelines and data analytics apps with Pandas.",
                requirements="Python, SQL, Pandas, Pytest",
            ),
            row(
                "FitCo",
                "Backend Java Intern",
                description="Build backend REST APIs and database-backed services.",
                requirements="Java, SQL, Git",
            ),
            row(
                "FitCo",
                "Cloud Developer Internship",
                description="Build cloud APIs and platform services in Python.",
                requirements="AWS, Python, Docker",
            ),
            row(
                "FitCo",
                "Software Engineer Intern",
                description="Build simulation infrastructure.",
                requirements="Rust, Go, C++",
            ),
        ]
    }

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource(direct_rows)},
            github_source=FakeGithub([]),
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 6, 9),
        )

    subject, body = render_digest(result.new_matches)
    high_fit_titles = {
        job["title"]
        for job in result.new_matches
        if job["score"]["fit_score"] > 94
    }

    assert subject == "Internship Watcher: 6 new SWE-intern matches"
    assert high_fit_titles == {
        "Backend Engineer Intern",
        "Full Stack Engineer Intern",
    }
    assert body.index("Backend Engineer Intern") < body.index("Backend Java Intern")
    assert body.index("Backend Java Intern") < body.index("Cloud Developer Internship")
