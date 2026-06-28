from datetime import date, datetime, timezone

from watcher.config import CompanyCfg, WatcherConfig
from watcher.run import collect_rows, print_report, run_once
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


class FakeDigestSender:
    def __init__(self, *, sent=True):
        self.sent = sent
        self.calls = []

    def __call__(self, matches):
        self.calls.append(list(matches))
        return self.sent


def row(company, title, *, source="direct", url=None, deadline="", description="Build Python APIs with React."):
    return make_row(
        source=source,
        source_adapter="fake",
        company=company,
        title=title,
        location="New York, NY",
        description=description,
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

    with SeenStore(tmp_path / "seen.sqlite") as store:
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
    assert [len(call) for call in digest_sender.calls] == [2, 0]


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
    assert [len(call) for call in digest_sender.calls] == [1, 1]


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
    assert "[direct] DirectCo" in output
    assert "Strong role match" in output

    empty = type("Result", (), {"errors": [], "new_matches": []})()
    print_report(empty)
    assert "No new matches." in capsys.readouterr().out
