"""Workday incomplete collection must stay degraded across every health layer.

Each test drives the real ``run_once`` pipeline with an offline scripted Workday
transport, persists into a temporary SQLite database, and then reloads the store
so the assertion is about persisted state rather than in-memory state. No test
sends email, touches production state, or performs network I/O.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from watcher.collection_snapshot import (
    CollectionBatch,
    load_collection_snapshot,
    save_collection_snapshot,
)
from watcher.config import CompanyCfg, WatcherConfig
from watcher.health_alerts import HealthAlertPolicy, build_alert_candidates
from watcher.run import print_heartbeat, run_once
from watcher.seen_store import SeenStore
from watcher.source_health import (
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    SOURCE_KIND_DIRECT,
    SourceHealthStore,
    write_health_report,
)
from watcher.sources.base import SourceFetchError
from watcher.sources.workday import WorkdaySource
from watcher.tests.test_run import FakeDigestSender, FakeGithub

PAGE_SIZE = WorkdaySource.page_size
OBSERVED = datetime(2026, 8, 6, tzinfo=timezone.utc)


def company() -> CompanyCfg:
    return CompanyCfg(
        name="Merck",
        ats="workday",
        token="merck",
        workday_shard="wd5",
        workday_site="Search_Jobs",
        workday_detail_policy="none",
    )


def posting(index: int) -> dict:
    return {
        "title": f"Software Engineer Intern {index}",
        "externalPath": f"/job/Rahway-NJ/Software-Engineer-Intern_R{index}",
        "locationsText": "Rahway, NJ",
        "jobDescription": "Build Python backend APIs and SQL services.",
        "bulletFields": [f"R{index}"],
    }


def full_page(start: int) -> list[dict]:
    return [posting(start + index) for index in range(PAGE_SIZE)]


class ScriptedBoard:
    """Returns scripted pages, optionally raising for one requested offset."""

    def __init__(self, pages, *, fail_at_offset=None, repeat_after=None):
        self.pages = pages
        self.fail_at_offset = fail_at_offset
        self.repeat_after = repeat_after
        self.offsets: list[int] = []

    def __call__(self, _url, request, _source_name):
        offset = int(request["offset"])
        self.offsets.append(offset)
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            raise SourceFetchError(
                "workday POST failed",
                error_code="network_failure",
                retryable=False,
            )
        if self.repeat_after is not None and offset >= self.repeat_after:
            return self.pages[-1]
        index = offset // PAGE_SIZE
        if index < len(self.pages):
            return self.pages[index]
        return {"jobPostings": [], "total": 0}


def execute_run(tmp_path, monkeypatch, transport, *, run_id="run-1", observed=OBSERVED):
    """Run one collection and return the result plus the database path."""

    monkeypatch.setattr("watcher.sources.workday.post_json", transport)
    db_path = tmp_path / "seen.sqlite"
    config = WatcherConfig(companies=(company(),))
    with SeenStore(db_path) as seen_store, SourceHealthStore(db_path) as health_store:
        result = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"workday": WorkdaySource()},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id=run_id,
            health_observed_at=observed,
        )
    return result, db_path


def direct_attempt(result):
    return next(
        item
        for item in result.source_attempts
        if item.source_kind == SOURCE_KIND_DIRECT
    )


def persisted_state(db_path, company_name="Merck"):
    """Reload the store so assertions read persisted rather than in-run state."""

    with SourceHealthStore(db_path) as store:
        states = store.all_current_states()
    return next(
        state
        for state in states.values()
        if state.company == company_name
    )


def persisted_attempts(db_path, company_name="Merck"):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        return [
            dict(row)
            for row in connection.execute(
                "select * from source_health_attempts where company = ?"
                " order by attempt_id",
                (company_name,),
            )
        ]
    finally:
        connection.close()


# --- complete collections stay healthy -------------------------------------


def test_complete_collection_with_listings_persists_healthy(tmp_path, monkeypatch):
    board = ScriptedBoard(
        [
            {"jobPostings": full_page(0), "total": 25},
            {"jobPostings": [posting(20 + index) for index in range(5)], "total": 25},
        ]
    )

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    attempt = direct_attempt(result)
    assert attempt.succeeded is True
    assert attempt.rows_returned == 25
    assert attempt.incomplete is False
    assert attempt.degraded is False
    assert attempt.complete is True
    state = persisted_state(db_path)
    assert state.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert state.last_degraded is False
    assert state.last_complete is True
    assert result.health_summary.direct_healthy_with_listings == 1
    assert result.health_summary.direct_degraded == 0


def test_complete_empty_collection_persists_healthy_empty(tmp_path, monkeypatch):
    board = ScriptedBoard([{"jobPostings": [], "total": 0}])

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    attempt = direct_attempt(result)
    assert attempt.succeeded is True
    assert attempt.rows_returned == 0
    assert attempt.incomplete is False
    state = persisted_state(db_path)
    assert state.status == DIRECT_STATUS_HEALTHY_EMPTY
    assert result.health_summary.direct_healthy_empty == 1
    assert result.health_summary.direct_degraded == 0


def test_harmless_changing_totals_stay_healthy(tmp_path, monkeypatch):
    """Zero, missing, and shrinking later totals are diagnostics only."""

    board = ScriptedBoard(
        [
            {"jobPostings": full_page(0), "total": 45},
            {"jobPostings": full_page(20), "total": 0},
            {"jobPostings": [posting(40 + index) for index in range(5)]},
        ]
    )

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    attempt = direct_attempt(result)
    assert attempt.rows_returned == 45
    assert attempt.incomplete is False
    assert attempt.degraded is False
    assert attempt.reason_codes == ()
    assert persisted_state(db_path).status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS


def test_duplicate_removal_alone_stays_healthy(tmp_path, monkeypatch):
    """Cross-source duplicate merging never degrades a complete collection."""

    duplicate = posting(0)
    board = ScriptedBoard(
        [{"jobPostings": [duplicate, dict(duplicate), posting(1)], "total": 3}]
    )

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    attempt = direct_attempt(result)
    assert attempt.succeeded is True
    assert attempt.incomplete is False
    assert attempt.degraded is False
    assert persisted_state(db_path).status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS


# --- incomplete collections persist as degraded ----------------------------


def assert_persisted_degraded(result, db_path, expected_reason):
    attempt = direct_attempt(result)
    assert attempt.succeeded is True
    assert attempt.rows_returned > 0
    assert attempt.incomplete is True
    assert attempt.degraded is True
    assert attempt.complete is False
    assert expected_reason in attempt.reason_codes

    state = persisted_state(db_path)
    assert state.status == DIRECT_STATUS_DEGRADED
    assert state.last_incomplete is True
    assert state.last_degraded is True
    assert state.last_complete is False
    assert expected_reason in state.last_reason_codes

    stored = persisted_attempts(db_path)
    assert stored[-1]["incomplete"] == 1
    assert stored[-1]["degraded"] == 1
    assert stored[-1]["complete"] == 0
    assert expected_reason in json.loads(stored[-1]["reason_codes_json"])

    assert result.health_summary.direct_degraded == 1
    assert result.health_summary.direct_healthy_with_listings == 0
    return state


def test_early_partial_page_termination_persists_degraded(tmp_path, monkeypatch):
    board = ScriptedBoard(
        [
            {"jobPostings": full_page(0), "total": 500},
            {"jobPostings": [posting(20 + index) for index in range(5)], "total": 500},
        ]
    )

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    assert_persisted_degraded(result, db_path, "pagination_ended_early")


def test_early_empty_page_termination_persists_degraded(tmp_path, monkeypatch):
    board = ScriptedBoard(
        [
            {"jobPostings": full_page(0), "total": 500},
            {"jobPostings": [], "total": 500},
        ]
    )

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    assert_persisted_degraded(result, db_path, "pagination_ended_early")


def test_safety_limit_termination_persists_degraded(tmp_path, monkeypatch):
    class EndlessBoard:
        def __init__(self):
            self.offsets: list[int] = []

        def __call__(self, _url, request, _source_name):
            offset = int(request["offset"])
            self.offsets.append(offset)
            return {"jobPostings": full_page(offset), "total": 0}

    result, db_path = execute_run(tmp_path, monkeypatch, EndlessBoard())

    assert_persisted_degraded(result, db_path, "pagination_safety_limit_reached")


def test_repeated_page_termination_persists_degraded(tmp_path, monkeypatch):
    repeated = {"jobPostings": full_page(0), "total": 500}
    board = ScriptedBoard([repeated], repeat_after=0)

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    assert_persisted_degraded(result, db_path, "pagination_repeated_page")


def test_failed_continuation_request_persists_degraded(tmp_path, monkeypatch):
    board = ScriptedBoard(
        [{"jobPostings": full_page(0), "total": 500}],
        fail_at_offset=PAGE_SIZE,
    )

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    state = assert_persisted_degraded(result, db_path, "pagination_request_failed")
    assert state.last_failed_request_count >= 1
    assert result.errors == []


def test_failed_first_request_persists_as_failed_not_degraded(tmp_path, monkeypatch):
    board = ScriptedBoard([], fail_at_offset=0)

    result, db_path = execute_run(tmp_path, monkeypatch, board)

    attempt = direct_attempt(result)
    assert attempt.succeeded is False
    state = persisted_state(db_path)
    assert state.status == "failed"
    assert result.health_summary.direct_failed == 1


# --- consistency across every downstream surface ---------------------------


def test_degraded_state_survives_snapshot_serialization_and_replay(
    tmp_path, monkeypatch
):
    board = ScriptedBoard(
        [
            {"jobPostings": full_page(0), "total": 500},
            {"jobPostings": [], "total": 500},
        ]
    )
    result, _db_path = execute_run(tmp_path, monkeypatch, board)
    attempt = direct_attempt(result)

    batch = CollectionBatch.create(
        captured_at=OBSERVED,
        collection_config_fingerprint="f" * 64,
        rows=[],
        errors=[],
        source_attempts=tuple(result.source_attempts),
    )
    snapshot_path = tmp_path / "snapshot.json.gz"
    save_collection_snapshot(batch, snapshot_path)
    restored = load_collection_snapshot(snapshot_path)

    replayed = next(
        item
        for item in restored.source_attempts
        if item.source_kind == SOURCE_KIND_DIRECT
    )
    assert replayed.incomplete is True
    assert replayed.degraded is True
    assert replayed.complete is False
    assert replayed.reason_codes == attempt.reason_codes
    assert "pagination_ended_early" in replayed.reason_codes


def test_report_alerts_heartbeat_and_comparison_agree(tmp_path, monkeypatch, capsys):
    board = ScriptedBoard(
        [
            {"jobPostings": full_page(0), "total": 500},
            {"jobPostings": [], "total": 500},
        ]
    )
    result, db_path = execute_run(tmp_path, monkeypatch, board)
    state = persisted_state(db_path)

    report_path = tmp_path / "health.json"
    write_health_report(
        report_path,
        run_id="run-1",
        observed_at=OBSERVED,
        attempts=result.source_attempts,
        states=result.source_health_states,
        transitions=result.health_transitions,
        coverage=result.company_coverage,
        summary=result.health_summary,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    reported_state = next(
        item for item in report["states"] if item["company"] == "Merck"
    )
    assert reported_state["status"] == DIRECT_STATUS_DEGRADED
    assert report["summary"]["direct_degraded"] == 1
    reported_attempt = next(
        item for item in report["attempts"] if item["company"] == "Merck"
    )
    assert reported_attempt["degraded"] is True
    assert reported_attempt["incomplete"] is True

    candidates = build_alert_candidates(
        policy=HealthAlertPolicy(),
        run_id="run-1",
        observed_at=OBSERVED,
        states=result.source_health_states,
        transitions=result.health_transitions,
        coverage=result.company_coverage,
        previous_coverage=None,
    )
    degraded_alerts = [
        candidate
        for candidate in candidates
        if candidate.alert_type == "direct_source_degraded"
    ]
    assert [candidate.company for candidate in degraded_alerts] == ["Merck"]

    print_heartbeat(result)
    heartbeat = capsys.readouterr().out
    assert "direct_degraded=1" in heartbeat
    assert "direct_healthy_with_listings=0" in heartbeat

    comparison = result.source_comparison
    assert comparison is not None
    compared = next(
        item
        for item in comparison.health["direct_sources"]
        if item["company"] == "Merck"
    )
    assert compared["status"] == DIRECT_STATUS_DEGRADED
    assert compared["incomplete"] is True
    assert compared["degraded"] is True
    assert compared["complete"] is False
    assert "pagination_ended_early" in compared["reason_codes"]

    # Every surface agrees with the persisted state.
    assert state.status == compared["status"] == reported_state["status"]


def test_later_complete_run_returns_source_to_healthy(tmp_path, monkeypatch):
    degraded_board = ScriptedBoard(
        [
            {"jobPostings": full_page(0), "total": 500},
            {"jobPostings": [], "total": 500},
        ]
    )
    monkeypatch.setattr("watcher.sources.workday.post_json", degraded_board)
    db_path = tmp_path / "seen.sqlite"
    config = WatcherConfig(companies=(company(),))

    with SeenStore(db_path) as seen_store, SourceHealthStore(db_path) as health_store:
        first = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"workday": WorkdaySource()},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="run-degraded",
            health_observed_at=OBSERVED,
        )
        assert first.health_summary.direct_degraded == 1

        complete_board = ScriptedBoard(
            [{"jobPostings": full_page(0), "total": PAGE_SIZE}]
        )
        monkeypatch.setattr("watcher.sources.workday.post_json", complete_board)
        second = run_once(
            config,
            seen_store=seen_store,
            health_store=health_store,
            direct_sources={"workday": WorkdaySource()},
            github_source=FakeGithub([]),
            alumni_index={},
            digest_sender=FakeDigestSender(sent=False),
            run_id="run-recovered",
            health_observed_at=OBSERVED.replace(hour=1),
        )

    assert second.health_summary.direct_degraded == 0
    assert second.health_summary.direct_healthy_with_listings == 1
    state = persisted_state(db_path)
    assert state.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert state.previous_status == DIRECT_STATUS_DEGRADED
    assert state.last_incomplete is False
    assert state.last_degraded is False
    assert state.last_complete is True
    transition = next(
        item for item in second.health_transitions if item.company == "Merck"
    )
    assert transition.from_status == DIRECT_STATUS_DEGRADED
    assert transition.to_status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert transition.recovery is True

    stored = persisted_attempts(db_path)
    assert [row["degraded"] for row in stored] == [1, 0]
    assert [row["complete"] for row in stored] == [0, 1]
