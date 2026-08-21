"""SQLite persistence for health state, alert events, and coverage snapshots.

Covers :mod:`watcher.health.store`: additive schema migration, attempt history,
sanitized persistence, transactional rollback, and the bounded coverage-snapshot
retention that backs persisted GitHub fallback evidence.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from watcher.source_health import (
    COVERAGE_FAILING_BACKSTOP,
    ERROR_FETCH,
    MAX_ERROR_LENGTH,
    DIRECT_STATUS_FAILED,
    SourceHealthStore,
    direct_health_key,
)
from watcher.health_alerts import (
    GITHUB_EVIDENCE_HORIZON_DAYS,
    MAX_COVERAGE_SNAPSHOTS,
    HealthAlertStore,
)
from watcher.source_health import (
    COVERAGE_FAILING_BACKSTOP,
    DIRECT_STATUS_FAILED,
    SourceHealthStore,
    direct_health_key,
)
from watcher.tests.health_state_helpers import (
    NOW,
    attempt,
)
from watcher.tests.health_alert_helpers import (
    _failed_coverage,
)


def test_legacy_seen_database_upgrades_without_changing_seen_rows(tmp_path):
    path = tmp_path / "seen.sqlite"
    expected = ("job-1", "Example", "Intern", "https://example.test/1", "direct", "old", None)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "create table seen(job_id text primary key, company text, title text, url text, first_source text, first_seen text, emailed_at text)"
        )
        conn.execute("insert into seen values (?, ?, ?, ?, ?, ?, ?)", expected)

    with SourceHealthStore(path) as store:
        assert store.attempt_count() == 0

    with sqlite3.connect(path) as conn:
        assert conn.execute("select * from seen").fetchone() == expected
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    assert {"seen", "source_health_attempts", "source_health_current"} <= tables


def test_attempt_history_appends_current_state_upserts_and_reopen_preserves_counters(tmp_path):
    path = tmp_path / "seen.sqlite"
    with SourceHealthStore(path) as store:
        first_states, first_transitions = store.record_attempts([attempt(run_id="run-1", succeeded=False, rows=None)])
        second_states, second_transitions = store.record_attempts([attempt(run_id="run-2", succeeded=False, rows=None)])
        assert first_transitions == ()
        assert second_transitions == ()
        assert first_states[next(iter(first_states))].consecutive_failures == 1
        assert second_states[next(iter(second_states))].consecutive_failures == 2
        assert store.attempt_count() == 2

    with SourceHealthStore(path) as reopened:
        state = reopened.current_state(direct_health_key("Example Co", "greenhouse"))
        assert state.total_attempts == 2
        assert state.total_successes == 0
        assert state.consecutive_failures == 2


def test_timestamps_are_normalized_to_utc(tmp_path):
    local_time = datetime(2026, 7, 16, 10, 30, tzinfo=timezone(timedelta(hours=-4)))
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        states, _ = store.record_attempts([attempt(observed_at=local_time)])
        state = next(iter(states.values()))
        assert state.last_attempt_at == NOW
        stored = store._conn.execute("select observed_at from source_health_attempts").fetchone()[0]
    assert stored == "2026-07-16T14:30:00+00:00"


def test_long_errors_and_sensitive_urls_are_bounded_and_sanitized(tmp_path):
    message = "token=supersecret https://user:pass@example.test/jobs?auth=private " + "x" * 1000
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        states, _ = store.record_attempts(
            [attempt(succeeded=False, rows=None, error_kind=ERROR_FETCH, error_message=message)]
        )
        state = next(iter(states.values()))
        stored = store._conn.execute("select error_message from source_health_attempts").fetchone()[0]
    assert stored == state.last_error_message
    assert len(stored) <= MAX_ERROR_LENGTH
    assert "supersecret" not in stored
    assert "private" not in stored
    assert "user:pass" not in stored


def test_transport_subtype_is_persisted_without_raw_html_or_metadata(tmp_path):
    raw_marker = "<html><input value=PRIVATE_CHALLENGE_TOKEN></html>"
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        states, _ = store.record_attempts(
            [
                attempt(
                    succeeded=False,
                    rows=None,
                    error_kind="fetch_failure/html_challenge",
                    error_message="SourceFetchError: workday non-JSON code=html_challenge",
                )
            ]
        )
        state = next(iter(states.values()))
        stored = store._conn.execute(
            "select error_kind, error_message from source_health_attempts"
        ).fetchone()

    assert stored[0] == "fetch_failure/html_challenge"
    assert state.last_error_kind == "fetch_failure/html_challenge"
    assert state.status == DIRECT_STATUS_FAILED
    assert state.consecutive_failures == 1
    assert raw_marker not in stored[1]
    assert "PRIVATE_CHALLENGE_TOKEN" not in stored[1]


def test_transaction_failure_rolls_back_attempt_and_current_state(tmp_path):
    duplicate = attempt(run_id="same-run")
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.record_attempts([duplicate, duplicate])
        assert store.attempt_count() == 0
        assert store.current_state(duplicate.health_key) is None


def test_parameterized_values_accept_sql_metacharacters(tmp_path):
    quoted = attempt(company="O'Reilly; drop table seen; --", rows=1)
    with SourceHealthStore(tmp_path / "seen.sqlite") as store:
        states, _ = store.record_attempts([quoted])
        assert next(iter(states.values())).company == "O'Reilly; drop table seen; --"
        assert store.attempt_count() == 1


def test_legacy_coverage_snapshots_stay_readable(tmp_path):
    """A pre-change production database keeps working without a wipe."""

    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        store._conn.execute(
            """
            insert into source_health_coverage_snapshots(
              run_id, observed_at, coverage_json
            ) values (?, ?, ?)
            """,
            (
                "legacy-run",
                (NOW - timedelta(hours=1)).isoformat(),
                '{"Legacy Co": "direct_covered"}',
            ),
        )
        store._conn.commit()
        assert store.latest_coverage_snapshot() == {"Legacy Co": "direct_covered"}
        # A legacy entry carries no company-level evidence, so it proves nothing.
        assert store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        ) == frozenset()


def test_new_snapshots_persist_company_github_evidence(tmp_path):
    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        store.record_coverage_snapshot(
            run_id="run-1",
            observed_at=NOW - timedelta(hours=2),
            coverage=(
                _failed_coverage(company="Covered Co", github_rows=3),
                _failed_coverage(company="Quiet Co", github_rows=0),
                _failed_coverage(company="Unknown Co", github_rows=None),
            ),
        )
        # States still read back exactly as before.
        assert store.latest_coverage_snapshot() == {
            "Covered Co": COVERAGE_FAILING_BACKSTOP,
            "Quiet Co": COVERAGE_FAILING_BACKSTOP,
            "Unknown Co": COVERAGE_FAILING_BACKSTOP,
        }
        # Only a positive count is evidence.
        assert store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        ) == frozenset({"Covered Co"})


def test_snapshot_retention_covers_the_evidence_horizon(tmp_path):
    """Hourly runs must not evict evidence inside the seven-day horizon."""

    assert MAX_COVERAGE_SNAPSHOTS >= GITHUB_EVIDENCE_HORIZON_DAYS * 24
    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        oldest = NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS - 1)
        store.record_coverage_snapshot(
            run_id="oldest-run",
            observed_at=oldest,
            coverage=(_failed_coverage(company="Covered Co", github_rows=2),),
        )
        for index in range(GITHUB_EVIDENCE_HORIZON_DAYS * 24):
            store.record_coverage_snapshot(
                run_id=f"run-{index}",
                observed_at=oldest + timedelta(hours=index + 1),
                coverage=(_failed_coverage(company="Other Co", github_rows=0),),
            )
        retained = store._conn.execute(
            "select count(*) from source_health_coverage_snapshots"
        ).fetchone()[0]
        assert retained == GITHUB_EVIDENCE_HORIZON_DAYS * 24 + 1
        assert store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        ) == frozenset({"Covered Co"})


def test_replaying_a_run_id_does_not_duplicate_or_corrupt_evidence(tmp_path):
    db = tmp_path / "state.sqlite"
    with HealthAlertStore(db) as store:
        for github_rows in (5, 0):
            store.record_coverage_snapshot(
                run_id="repeated-run",
                observed_at=NOW - timedelta(hours=1),
                coverage=(_failed_coverage(company="Covered Co", github_rows=github_rows),),
            )
        stored = store._conn.execute(
            "select count(*) from source_health_coverage_snapshots"
        ).fetchone()[0]
        assert stored == 1
        # The replayed run replaced its own entry rather than adding one.
        assert store.companies_with_recent_github_rows(
            since=NOW - timedelta(days=GITHUB_EVIDENCE_HORIZON_DAYS)
        ) == frozenset()
