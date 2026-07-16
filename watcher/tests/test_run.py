import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from watcher.config import CompanyCfg, WatcherConfig
from watcher.notify import render_digest
from watcher.run import CollectionStats, collect_rows, print_heartbeat, print_report, run_once
from watcher.seen_store import SeenStore
from watcher.sources.base import SourceError, make_row


class FakeSource:
    def __init__(self, rows_by_company=None, *, error=None):
        self.rows_by_company = rows_by_company or {}
        self.error = error

    def fetch(self, company):
        if self.error:
            raise self.error
        return self.rows_by_company.get(company.name, [])


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
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub(github_rows),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc),
        )

    assert [job["title"] for job in first.new_matches] == [
        "Software Engineer Intern",
        "Software Engineering Intern",
    ]
    assert first.new_matches[0]["extra"]["source"] == "direct"
    assert first.new_matches[1]["extra"]["source"] == "github"
    assert second.new_matches == []
    assert first.digest_sent is True
    assert first.seen_marked == 2
    assert [len(call) for call in digest_sender.calls] == [2, 0]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("select emailed_at from seen order by job_id").fetchall()
    assert len(rows) == 2
    assert all(row[0] == "2026-06-09T00:00:00+00:00" for row in rows)


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
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
        )

    assert [job["title"] for job in first.new_matches] == ["Software Engineer Intern"]
    assert [job["title"] for job in second.new_matches] == ["Software Engineer Intern"]
    assert first.digest_sent is False
    assert first.seen_marked == 0
    assert [len(call) for call in digest_sender.calls] == [1, 1]


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
            mark_seen_without_send=True,
        )
        second = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource({"DirectCo": direct_rows})},
            github_source=FakeGithub([]),
            digest_sender=digest_sender,
            today=date(2026, 6, 9),
            mark_seen_without_send=True,
        )

    assert [job["title"] for job in first.new_matches] == ["Software Engineer Intern"]
    assert second.new_matches == []
    assert first.digest_sent is False
    assert first.seen_marked == 1
    assert [len(call) for call in digest_sender.calls] == [1, 0]
    with sqlite3.connect(db_path) as conn:
        seen_row = conn.execute("select first_seen, emailed_at from seen").fetchone()
    assert seen_row == ("2026-06-09T00:00:00+00:00", None)


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
    monkeypatch.setattr("watcher.run.GitHubListingsSource", lambda url: sources[url])
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
    monkeypatch.setattr("watcher.run.GitHubListingsSource", lambda url: sources[url])
    stats = CollectionStats()

    rows, errors = collect_rows(config, direct_sources={}, stats=stats)

    assert rows == [good_row]
    assert errors == ["github listings (https://example.test/broken.json): request failed"]
    assert stats.github_feeds_configured == 2
    assert stats.github_feeds_succeeded == 1


def test_all_github_feeds_failing_does_not_remove_direct_rows(monkeypatch):
    direct_row = row("DirectCo", "Software Engineer Intern", source="direct")
    urls = ("https://example.test/one.json", "https://example.test/two.json")
    sources = {url: CountingGithub(url, error=SourceError("boom")) for url in urls}
    config = WatcherConfig(
        companies=(CompanyCfg(name="DirectCo", ats="greenhouse", token="direct"),),
        terms=("Summer 2027",),
        github_listing_urls=urls,
    )
    monkeypatch.setattr("watcher.run.GitHubListingsSource", lambda url: sources[url])
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
    })()

    print_report(result)
    output = capsys.readouterr().out
    assert "New matches: 1" in output
    assert "Configured internship terms: (none)" in output
    assert "Season status: unknown" in output
    assert "GitHub backstop feeds: 0 configured, 0 succeeded" in output
    assert "[direct] DirectCo" in output
    assert "Strong role match" in output

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
    })()

    print_heartbeat(result)

    assert capsys.readouterr().out == (
        "HEARTBEAT: ran, rows_fetched=3, jobs_scored=2, matches=2, "
        "new=1, errors=1, season_status=ok, configured_terms=Fall_2026|Summer_2027, "
        "github_feeds_configured=2, github_feeds_succeeded=1, "
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
        assert f"steps.run_watcher.outputs.{field}" in workflow


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
