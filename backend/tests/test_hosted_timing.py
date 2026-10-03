"""Offline coverage for hosted-safe ``HOSTED-TIMING`` telemetry.

These tests need no database: the hosted import boundary is replaced where a
test only concerns collection, snapshot, and telemetry behaviour. The
PostgreSQL-backed stage lines are covered in
``test_hosted_collect_and_import.py``.
"""

from __future__ import annotations

import io
import re
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.hosted import collect_and_import
from app.hosted.collect_and_import import main as collect_main
from app.hosted.job_import import (
    ImportCounters,
    InvalidFinalJobs,
    JobImportResult,
)
from app.hosted.snapshot_jobs import replay_snapshot_jobs
from app.hosted.timing import MAX_FAMILY_LINES, PREFIX, HostedTiming

from watcher.collection import (
    CollectionStats,
    SourceFamilyTiming,
    collect_batch,
)
from watcher.collection_snapshot import save_collection_snapshot
from watcher.config import (
    CollectionConcurrencyCfg,
    CompanyCfg,
    GitHubListingSourceCfg,
    WatcherConfig,
)
from watcher.sources.base import DirectSourceDiagnostics, SourceError, make_row

FIXED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
COMPANY_NAMES = (
    "ZephyrQuillWorks",
    "NimbusHarborLabs",
    "OrchidFerryCapital",
    "TundraBeaconSystems",
    "BrokenLanternCo",
    "BackstopMeadowInc",
)
FEED_URL = "https://raw.githubusercontent.test/owner/repo/listings.json"
LINE_RE = re.compile(r"^HOSTED-TIMING kind=[a-z]+( [a-z_]+=[a-z0-9_.]+)+$")


def _row(company: str, title: str, *, source: str = "direct", adapter: str = "greenhouse"):
    return make_row(
        source=source,
        source_adapter=adapter,
        company=company,
        title=title,
        location="New York, NY",
        description="Build services.",
        source_url=f"https://example.test/{company}/{title.replace(' ', '-')}",
        date_posted="2026-08-30",
        internship_type="Summer 2027 Internship",
    )


class FakeSource:
    """A direct adapter that publishes the same diagnostics real ones do."""

    def __init__(self, rows_by_company, *, errors=None, workday_details=None):
        self.rows_by_company = rows_by_company
        self.errors = errors or {}
        self.workday_details = workday_details or {}
        self.last_diagnostics = None
        self.last_health_diagnostics = DirectSourceDiagnostics(attempted=False)

    def fetch(self, company):
        detail = self.workday_details.get(company.name)
        self.last_diagnostics = (
            SimpleNamespace(
                request_attempts=detail["requests"],
                retry_attempts=detail["retries"],
                detail_requests=detail["detail_requests"],
                detail_degraded_reason=detail["reason"],
            )
            if detail
            else None
        )
        error = self.errors.get(company.name)
        if error is not None:
            raise error
        rows = list(self.rows_by_company.get(company.name, []))
        self.last_health_diagnostics = DirectSourceDiagnostics(
            succeeded=True,
            retained_row_count=len(rows),
            degraded=bool(detail and detail["reason"]),
            complete=not (detail and detail["reason"]),
        )
        return rows


class FakeGithub:
    feed_label = FEED_URL
    url = FEED_URL

    def fetch_many(self, _companies):
        return [_row("BackstopMeadowInc", "SWE Intern", source="github", adapter="")]


def fixture_config(concurrency: CollectionConcurrencyCfg | None = None) -> WatcherConfig:
    return WatcherConfig(
        companies=(
            CompanyCfg(name="ZephyrQuillWorks", ats="greenhouse", token="zephyr"),
            CompanyCfg(name="NimbusHarborLabs", ats="greenhouse", token="nimbus"),
            CompanyCfg(name="BrokenLanternCo", ats="greenhouse", token="broken"),
            CompanyCfg(
                name="OrchidFerryCapital",
                ats="workday",
                token="orchid",
                workday_shard="wd5",
                workday_site="Orchid",
            ),
            CompanyCfg(
                name="TundraBeaconSystems",
                ats="workday",
                token="tundra",
                workday_shard="wd1",
                workday_site="Tundra",
            ),
            CompanyCfg(name="BackstopMeadowInc", ats="github_only"),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(
            GitHubListingSourceCfg(
                name="fixture_feed", format="simplify_json", url=FEED_URL
            ),
        ),
        collection_concurrency=concurrency or CollectionConcurrencyCfg(),
    )


def fixture_sources():
    greenhouse = FakeSource(
        {
            "ZephyrQuillWorks": [_row("ZephyrQuillWorks", "Software Engineer Intern")],
            "NimbusHarborLabs": [_row("NimbusHarborLabs", "Backend Intern")],
        },
        errors={"BrokenLanternCo": SourceError("BrokenLanternCo exploded")},
    )
    workday = FakeSource(
        {
            "OrchidFerryCapital": [
                _row("OrchidFerryCapital", "Data Intern", adapter="workday")
            ],
            "TundraBeaconSystems": [
                _row("TundraBeaconSystems", "ML Intern", adapter="workday")
            ],
        },
        workday_details={
            "OrchidFerryCapital": {
                "requests": 4,
                "retries": 1,
                "detail_requests": 3,
                "reason": "",
            },
            "TundraBeaconSystems": {
                "requests": 2,
                "retries": 0,
                "detail_requests": 0,
                "reason": "detail_candidate_limit_exceeded",
            },
        },
    )
    return {"greenhouse": greenhouse, "workday": workday}


def fixture_collect(config: WatcherConfig, *, stats: CollectionStats | None):
    return collect_batch(
        config,
        direct_sources=fixture_sources(),
        github_source=FakeGithub(),
        stats=stats,
        run_id="fixed-run",
        observed_at=FIXED,
        captured_at=FIXED,
    )


def timing_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.startswith(PREFIX)]


def assert_no_company_identifiers(lines: list[str]) -> None:
    text = "\n".join(lines).casefold()
    for name in COMPANY_NAMES:
        assert name.casefold() not in text
    for token in ("zephyr", "nimbus", "orchid", "tundra", "lantern", "meadow", "http"):
        assert token not in text


# --- collection aggregates ---------------------------------------------------


def test_collection_records_family_aggregates_without_company_identifiers() -> None:
    stats = CollectionStats()
    fixture_collect(fixture_config(), stats=stats)

    assert set(stats.stage_seconds) == {"direct_collection", "github_collection"}
    assert set(stats.family_timing) == {
        ("direct", "greenhouse"),
        ("direct", "workday"),
        ("backstop", "simplify_json"),
    }
    greenhouse = stats.family_timing[("direct", "greenhouse")]
    assert (greenhouse.tasks, greenhouse.failed, greenhouse.rows) == (3, 1, 2)
    workday = stats.family_timing[("direct", "workday")]
    assert (workday.tasks, workday.failed, workday.degraded, workday.rows) == (
        2,
        0,
        1,
        2,
    )
    assert (workday.requests, workday.retries) == (6, 1)
    assert (workday.detail_requests, workday.detail_budget_skips) == (3, 1)
    assert workday.seconds_max <= workday.seconds_total

    timing = HostedTiming()
    timing.record_collection(fixture_config(), stats)
    lines = timing.lines(exit_code=0)
    assert_no_company_identifiers(lines)
    workday_line = next(line for line in lines if "family=workday" in line)
    assert "tasks=2" in workday_line and "degraded=1" in workday_line
    assert "requests=6" in workday_line and "retries=1" in workday_line
    assert "detail_requests=3" in workday_line
    assert "detail_budget_skips=1" in workday_line
    # Families that publish no request counters do not invent them.
    greenhouse_line = next(line for line in lines if "family=greenhouse" in line)
    assert "failed=1" in greenhouse_line and "requests=" not in greenhouse_line


def test_telemetry_does_not_change_the_collected_batch_or_snapshot_bytes(
    tmp_path,
) -> None:
    observed = fixture_collect(fixture_config(), stats=CollectionStats())
    unobserved = fixture_collect(fixture_config(), stats=None)
    first, second = tmp_path / "a.json.gz", tmp_path / "b.json.gz"
    save_collection_snapshot(observed, first)
    save_collection_snapshot(unobserved, second)

    assert first.read_bytes() == second.read_bytes()
    assert observed.rows == unobserved.rows
    assert observed.source_attempts == unobserved.source_attempts


# --- HostedTiming unit behaviour --------------------------------------------


def test_stage_lines_are_ordered_bounded_and_include_the_exit_code() -> None:
    ticks = iter(range(100))
    timing = HostedTiming(clock=lambda: float(next(ticks)))
    with timing.stage("analysis"):
        pass
    with timing.stage("snapshot_save"):
        pass
    timing.record("job_upsert", 2.5)

    lines = timing.lines(exit_code=0)
    assert lines == [
        f"{PREFIX} kind=stage stage=snapshot_save seconds=1.000",
        f"{PREFIX} kind=stage stage=analysis seconds=1.000",
        f"{PREFIX} kind=stage stage=job_upsert seconds=2.500",
        f"{PREFIX} kind=stage stage=total seconds=5.000 exit_code=0",
    ]
    assert all(LINE_RE.match(line) for line in lines)


def test_a_stage_is_recorded_when_its_block_raises_and_the_error_propagates() -> None:
    timing = HostedTiming()
    with pytest.raises(ValueError):
        with timing.stage("analysis"):
            raise ValueError("boom")
    assert any("stage=analysis" in line for line in timing.lines(exit_code=1))


def test_telemetry_failures_never_raise() -> None:
    def broken_clock():
        raise RuntimeError("clock unavailable")

    timing = HostedTiming(clock=broken_clock)
    with timing.stage("analysis"):
        pass
    timing.record("analysis", "not-a-number")
    timing.record_collection(object(), object())
    assert timing.lines(exit_code=0)[-1].endswith("exit_code=0")

    class BrokenStream(io.StringIO):
        def write(self, _text):
            raise OSError("closed")

    timing.emit(exit_code=0, stream=BrokenStream())


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (
            CollectionConcurrencyCfg(),
            "mode=serial max_workers=1 per_origin_limit=1 workday_limit=1",
        ),
        (
            CollectionConcurrencyCfg(
                mode="concurrent",
                max_workers=8,
                workday_max_concurrency=2,
                per_origin_max_concurrency=3,
            ),
            "mode=concurrent max_workers=8 per_origin_limit=3 workday_limit=2",
        ),
    ],
)
def test_concurrency_mode_and_limits_are_reported(settings, expected) -> None:
    timing = HostedTiming()
    timing.record_collection(SimpleNamespace(collection_concurrency=settings), None)
    assert timing.lines(exit_code=0)[0] == f"{PREFIX} kind=concurrency {expected}"


def test_observed_concurrency_peaks_are_reported_when_collection_ran() -> None:
    stats = CollectionStats()
    concurrency = CollectionConcurrencyCfg(
        mode="concurrent",
        max_workers=4,
        workday_max_concurrency=1,
        per_origin_max_concurrency=2,
    )
    fixture_collect(fixture_config(concurrency), stats=stats)
    timing = HostedTiming()
    timing.record_collection(fixture_config(concurrency), stats)
    line = timing.lines(exit_code=0)[0]
    assert line.startswith(
        f"{PREFIX} kind=concurrency mode=concurrent max_workers=4 "
        "per_origin_limit=2 workday_limit=1 observed_global="
    )
    assert "observed_workday=1" in line


def test_family_lines_are_bounded_and_sanitized() -> None:
    families = {
        ("direct", f"Family {index} https://evil.test/?q=Name"): SourceFamilyTiming(
            tasks=1, seconds_total=1.0, seconds_max=1.0
        )
        for index in range(MAX_FAMILY_LINES * 2)
    }
    timing = HostedTiming()
    timing.record_collection(
        SimpleNamespace(collection_concurrency=CollectionConcurrencyCfg()),
        SimpleNamespace(stage_seconds={}, family_timing=families),
    )
    family_lines = [line for line in timing.lines(exit_code=0) if "kind=family" in line]
    assert len(family_lines) == MAX_FAMILY_LINES
    for line in family_lines:
        assert LINE_RE.match(line)
        family = re.search(r" family=(\S+)", line).group(1)
        assert len(family) <= 40


# --- collect_and_import entry point -----------------------------------------


def _import_result() -> JobImportResult:
    return JobImportResult(
        run_id=uuid.uuid4(),
        source_fingerprint="a" * 64,
        outcome="imported",
        counters=ImportCounters(
            jobs_received=1,
            jobs_inserted=1,
            jobs_updated=0,
            jobs_unchanged=0,
            jobs_skipped=0,
        ),
        skipped_reasons={},
    )


@pytest.fixture
def hosted_entrypoint(monkeypatch):
    """Run the real command with fixture sources and no database."""

    monkeypatch.setenv(
        "HOSTED_DATABASE_URL", "postgresql+psycopg://user:pw@localhost:1/unused"
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    state: dict[str, object] = {}

    def fake_collect_batch(config, *, stats=None):
        state["config"] = config
        batch = fixture_collect(fixture_config(), stats=stats)
        state["batch"] = batch
        return batch

    def fake_import(snapshot_path, **kwargs):
        state["snapshot_bytes"] = snapshot_path.read_bytes()
        replayed = replay_snapshot_jobs(
            snapshot_path,
            allow_collection_config_mismatch=True,
            timing=kwargs["timing"],
        )
        state["jobs"] = replayed.jobs
        return _import_result()

    monkeypatch.setattr(collect_and_import, "collect_batch", fake_collect_batch)
    monkeypatch.setattr(
        collect_and_import, "import_snapshot_into_hosted", fake_import
    )
    return state


def test_entrypoint_emits_stage_concurrency_and_family_timing(
    hosted_entrypoint, monkeypatch, capsys, tmp_path
) -> None:
    monkeypatch.setenv("WATCHER_COLLECTION_MODE", "concurrent")
    monkeypatch.setenv("WATCHER_COLLECTION_MAX_WORKERS", "6")
    monkeypatch.setenv("WATCHER_WORKDAY_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY", "3")

    assert collect_main([]) == 0
    captured = capsys.readouterr()
    lines = timing_lines(captured.out)

    assert lines[0].startswith(
        f"{PREFIX} kind=concurrency mode=concurrent max_workers=6 "
        "per_origin_limit=3 workday_limit=2 observed_global="
    )
    stages = [
        re.search(r" stage=(\S+)", line).group(1)
        for line in lines
        if "kind=stage" in line
    ]
    assert stages == [
        "direct_collection",
        "github_collection",
        "collection",
        "snapshot_save",
        "snapshot_verify_load",
        "analysis",
        "total",
    ]
    assert lines[-1].endswith("exit_code=0")
    assert {"family=greenhouse", "family=workday", "family=simplify_json"} <= {
        token for line in lines for token in line.split() if token.startswith("family=")
    }
    assert all(LINE_RE.match(line) for line in lines)
    assert_no_company_identifiers(lines)
    # Existing summary lines are unchanged and come first.
    assert captured.out.splitlines()[0].startswith("HOSTED-COLLECTION ")
    assert any(
        line.startswith("HOSTED-JOB-IMPORT ") for line in captured.out.splitlines()
    )
    assert captured.err == ""

    # The snapshot the importer received is byte-identical to saving the
    # collected batch directly, so telemetry never alters snapshot content.
    reference = tmp_path / "reference.json.gz"
    save_collection_snapshot(hosted_entrypoint["batch"], reference)
    assert hosted_entrypoint["snapshot_bytes"] == reference.read_bytes()


def test_replay_with_timing_produces_identical_jobs(tmp_path) -> None:
    batch = fixture_collect(fixture_config(), stats=None)
    snapshot = tmp_path / "replay.json.gz"
    save_collection_snapshot(batch, snapshot)

    timing = HostedTiming()
    timed = replay_snapshot_jobs(
        snapshot, allow_collection_config_mismatch=True, timing=timing
    )
    untimed = replay_snapshot_jobs(snapshot, allow_collection_config_mismatch=True)

    assert timed.jobs == untimed.jobs
    assert timed.source_fingerprint == untimed.source_fingerprint
    recorded = [line for line in timing.lines(exit_code=0) if "kind=stage" in line]
    assert any("stage=snapshot_verify_load" in line for line in recorded)
    assert any("stage=analysis" in line for line in recorded)


def test_a_collection_failure_keeps_its_exit_status_and_still_reports_timing(
    hosted_entrypoint, monkeypatch, capsys
) -> None:
    def failing(_config, *, stats=None):
        raise RuntimeError("ZephyrQuillWorks https://private.test secret")

    monkeypatch.setattr(collect_and_import, "collect_batch", failing)

    assert collect_main([]) == 1
    captured = capsys.readouterr()
    assert captured.err == "Hosted collection failed: hosted_import_unavailable\n"
    lines = timing_lines(captured.out)
    assert lines == [line for line in captured.out.splitlines()]
    assert any("stage=collection " in line for line in lines)
    assert lines[-1].endswith("exit_code=1")
    assert_no_company_identifiers(lines)
    assert "secret" not in captured.out


def test_an_import_failure_keeps_its_exit_status_and_error_code(
    hosted_entrypoint, monkeypatch, capsys
) -> None:
    def failing_import(_snapshot_path, **_kwargs):
        raise InvalidFinalJobs()

    monkeypatch.setattr(
        collect_and_import, "import_snapshot_into_hosted", failing_import
    )

    assert collect_main([]) == 1
    captured = capsys.readouterr()
    assert captured.err == "Hosted collection failed: invalid_final_jobs\n"
    assert not any(
        line.startswith("HOSTED-JOB-IMPORT") for line in captured.out.splitlines()
    )
    assert timing_lines(captured.out)[-1].endswith("exit_code=1")


def test_a_missing_database_keeps_exit_code_two(monkeypatch, capsys) -> None:
    monkeypatch.delenv("HOSTED_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def unreachable(_config):
        raise AssertionError("collection must not start without a database")

    assert collect_main([], collector=unreachable) == 2
    captured = capsys.readouterr()
    assert captured.err == "Hosted collection failed: hosted_database_not_configured\n"
    assert timing_lines(captured.out) == [
        line for line in captured.out.splitlines()
    ]
    assert captured.out.splitlines()[-1].endswith("exit_code=2")


def test_broken_telemetry_never_changes_the_command_result(
    hosted_entrypoint, monkeypatch, capsys
) -> None:
    def broken_clock():
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(
        collect_and_import, "HostedTiming", lambda: HostedTiming(clock=broken_clock)
    )
    assert collect_main([]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert hosted_entrypoint["jobs"]
