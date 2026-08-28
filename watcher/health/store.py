"""Durable source-health and alert persistence."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from typing import Mapping, Sequence

from watcher.health.models import (
    ALERT_CONTINUED_FAILURE,
    ALERT_MINOR_DEGRADATION,
    ALERT_MINOR_RECOVERY,
    ALERT_NEW_FAILURE,
    ALERT_RECOVERY,
    DEGRADATION_ALERT_TYPES,
    MAX_COVERAGE_SNAPSHOTS,
    MAX_DIGEST_EVENTS,
    MAX_FLAP_HISTORY_EVENTS,
    CompanyCoverage,
    HealthAlertCandidate,
    HealthTransition,
    SourceAttempt,
    SourceHealthState,
)
from watcher.health.sanitize import (
    _bounded_reason_codes,
    iso_utc,
    parse_utc,
    safe_run_id,
    sanitize_error,
    utc_datetime,
)
from watcher.health.state import calculate_next_state, normalize_attempt, transition_for

class SourceHealthStore:
    """Persist health attempts and current state in the watcher's SQLite file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def current_state(self, health_key: str) -> SourceHealthState | None:
        row = self._conn.execute(
            "select * from source_health_current where health_key = ?",
            (health_key,),
        ).fetchone()
        return _state_from_row(row) if row else None

    def all_current_states(self) -> dict[str, SourceHealthState]:
        rows = self._conn.execute(
            "select * from source_health_current order by health_key"
        ).fetchall()
        return {row["health_key"]: _state_from_row(row) for row in rows}

    def record_attempts(
        self,
        attempts: Iterable[SourceAttempt],
    ) -> tuple[dict[str, SourceHealthState], tuple[HealthTransition, ...]]:
        normalized = tuple(normalize_attempt(attempt) for attempt in attempts)
        states: dict[str, SourceHealthState] = {}
        transitions: list[HealthTransition] = []
        with self._conn:
            for attempt in normalized:
                previous = self.current_state(attempt.health_key)
                current = calculate_next_state(previous, attempt)
                self._insert_attempt(attempt)
                self._upsert_state(current)
                states[current.health_key] = current
                transition = transition_for(previous, current)
                if transition:
                    transitions.append(transition)
        return states, tuple(transitions)

    def attempt_count(self, *, run_id: str | None = None) -> int:
        if run_id is None:
            row = self._conn.execute("select count(*) from source_health_attempts").fetchone()
        else:
            row = self._conn.execute(
                "select count(*) from source_health_attempts where run_id = ?", (run_id,)
            ).fetchone()
        return int(row[0])

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            create table if not exists source_health_attempts(
              attempt_id integer primary key autoincrement,
              run_id text not null,
              health_key text not null,
              observed_at text not null,
              source_kind text not null,
              company text,
              adapter text not null,
              feed_label text,
              unsupported_reason text,
              attempted integer not null,
              succeeded integer,
              rows_returned integer,
              error_kind text,
              error_message text,
              malformed_row_count integer,
              schema_error_row_count integer,
              duplicate_row_count integer,
              failed_request_count integer,
              incomplete integer,
              truncated integer,
              reason_codes_json text,
              degraded integer,
              complete integer,
              unique(run_id, health_key)
            );
            create index if not exists source_health_attempts_run_id_idx
              on source_health_attempts(run_id);
            create index if not exists source_health_attempts_key_idx
              on source_health_attempts(health_key, attempt_id);
            create table if not exists source_health_current(
              health_key text primary key,
              source_kind text not null,
              company text,
              adapter text not null,
              feed_label text,
              unsupported_reason text,
              status text not null,
              previous_status text,
              total_attempts integer not null,
              total_successes integer not null,
              consecutive_failures integer not null,
              consecutive_zero_successes integer not null,
              last_attempt_at text,
              last_success_at text,
              last_nonzero_at text,
              last_rows_returned integer,
              last_error_kind text,
              last_error_message text,
              last_malformed_row_count integer,
              last_schema_error_row_count integer,
              last_duplicate_row_count integer,
              last_failed_request_count integer,
              last_incomplete integer,
              last_truncated integer,
              last_reason_codes_json text,
              last_degraded integer,
              last_complete integer
            );
            """
        )
        self._ensure_diagnostic_columns()
        self._conn.commit()

    def _ensure_diagnostic_columns(self) -> None:
        attempt_columns = {
            "malformed_row_count": "integer",
            "schema_error_row_count": "integer",
            "duplicate_row_count": "integer",
            "failed_request_count": "integer",
            "incomplete": "integer",
            "truncated": "integer",
            "reason_codes_json": "text",
            "degraded": "integer",
            "complete": "integer",
        }
        state_columns = {
            "last_malformed_row_count": "integer",
            "last_schema_error_row_count": "integer",
            "last_duplicate_row_count": "integer",
            "last_failed_request_count": "integer",
            "last_incomplete": "integer",
            "last_truncated": "integer",
            "last_reason_codes_json": "text",
            "last_degraded": "integer",
            "last_complete": "integer",
        }
        for table, expected in (
            ("source_health_attempts", attempt_columns),
            ("source_health_current", state_columns),
        ):
            existing = {
                str(row[1])
                for row in self._conn.execute(f"pragma table_info({table})")
            }
            for name, sql_type in expected.items():
                if name not in existing:
                    self._conn.execute(
                        f"alter table {table} add column {name} {sql_type}"
                    )

    def _insert_attempt(self, attempt: SourceAttempt) -> None:
        self._conn.execute(
            """
            insert into source_health_attempts(
              run_id, health_key, observed_at, source_kind, company, adapter,
              feed_label, unsupported_reason, attempted, succeeded, rows_returned,
              error_kind, error_message, malformed_row_count,
              schema_error_row_count, duplicate_row_count, failed_request_count,
              incomplete, truncated, reason_codes_json, degraded, complete
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt.run_id,
                attempt.health_key,
                iso_utc(attempt.observed_at),
                attempt.source_kind,
                attempt.company,
                attempt.adapter,
                attempt.feed_label,
                attempt.unsupported_reason,
                int(attempt.attempted),
                None if attempt.succeeded is None else int(attempt.succeeded),
                attempt.rows_returned,
                attempt.error_kind,
                attempt.error_message,
                attempt.malformed_row_count,
                attempt.schema_error_row_count,
                attempt.duplicate_row_count,
                attempt.failed_request_count,
                _optional_bool_int(attempt.incomplete),
                _optional_bool_int(attempt.truncated),
                json.dumps(list(attempt.reason_codes), separators=(",", ":")),
                _optional_bool_int(attempt.degraded),
                _optional_bool_int(attempt.complete),
            ),
        )

    def _upsert_state(self, state: SourceHealthState) -> None:
        self._conn.execute(
            """
            insert into source_health_current(
              health_key, source_kind, company, adapter, feed_label,
              unsupported_reason, status, previous_status, total_attempts,
              total_successes, consecutive_failures, consecutive_zero_successes,
              last_attempt_at, last_success_at, last_nonzero_at, last_rows_returned,
              last_error_kind, last_error_message, last_malformed_row_count,
              last_schema_error_row_count, last_duplicate_row_count,
              last_failed_request_count, last_incomplete, last_truncated,
              last_reason_codes_json, last_degraded, last_complete
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(health_key) do update set
              source_kind=excluded.source_kind,
              company=excluded.company,
              adapter=excluded.adapter,
              feed_label=excluded.feed_label,
              unsupported_reason=excluded.unsupported_reason,
              status=excluded.status,
              previous_status=excluded.previous_status,
              total_attempts=excluded.total_attempts,
              total_successes=excluded.total_successes,
              consecutive_failures=excluded.consecutive_failures,
              consecutive_zero_successes=excluded.consecutive_zero_successes,
              last_attempt_at=excluded.last_attempt_at,
              last_success_at=excluded.last_success_at,
              last_nonzero_at=excluded.last_nonzero_at,
              last_rows_returned=excluded.last_rows_returned,
              last_error_kind=excluded.last_error_kind,
              last_error_message=excluded.last_error_message,
              last_malformed_row_count=excluded.last_malformed_row_count,
              last_schema_error_row_count=excluded.last_schema_error_row_count,
              last_duplicate_row_count=excluded.last_duplicate_row_count,
              last_failed_request_count=excluded.last_failed_request_count,
              last_incomplete=excluded.last_incomplete,
              last_truncated=excluded.last_truncated,
              last_reason_codes_json=excluded.last_reason_codes_json,
              last_degraded=excluded.last_degraded,
              last_complete=excluded.last_complete
            """,
            (
                state.health_key,
                state.source_kind,
                state.company,
                state.adapter,
                state.feed_label,
                state.unsupported_reason,
                state.status,
                state.previous_status,
                state.total_attempts,
                state.total_successes,
                state.consecutive_failures,
                state.consecutive_zero_successes,
                iso_utc(state.last_attempt_at) if state.last_attempt_at else None,
                iso_utc(state.last_success_at) if state.last_success_at else None,
                iso_utc(state.last_nonzero_at) if state.last_nonzero_at else None,
                state.last_rows_returned,
                state.last_error_kind,
                state.last_error_message,
                state.last_malformed_row_count,
                state.last_schema_error_row_count,
                state.last_duplicate_row_count,
                state.last_failed_request_count,
                _optional_bool_int(state.last_incomplete),
                _optional_bool_int(state.last_truncated),
                json.dumps(list(state.last_reason_codes), separators=(",", ":")),
                _optional_bool_int(state.last_degraded),
                _optional_bool_int(state.last_complete),
            ),
        )


def _state_from_row(row: sqlite3.Row) -> SourceHealthState:
    keys = set(row.keys())
    return SourceHealthState(
        health_key=row["health_key"],
        source_kind=row["source_kind"],
        company=row["company"],
        adapter=row["adapter"],
        feed_label=row["feed_label"],
        unsupported_reason=row["unsupported_reason"],
        status=row["status"],
        previous_status=row["previous_status"],
        total_attempts=int(row["total_attempts"]),
        total_successes=int(row["total_successes"]),
        consecutive_failures=int(row["consecutive_failures"]),
        consecutive_zero_successes=int(row["consecutive_zero_successes"]),
        last_attempt_at=parse_utc(row["last_attempt_at"]),
        last_success_at=parse_utc(row["last_success_at"]),
        last_nonzero_at=parse_utc(row["last_nonzero_at"]),
        last_rows_returned=row["last_rows_returned"],
        last_error_kind=row["last_error_kind"],
        last_error_message=row["last_error_message"],
        last_malformed_row_count=_row_value(row, keys, "last_malformed_row_count"),
        last_schema_error_row_count=_row_value(row, keys, "last_schema_error_row_count"),
        last_duplicate_row_count=_row_value(row, keys, "last_duplicate_row_count"),
        last_failed_request_count=_row_value(row, keys, "last_failed_request_count"),
        last_incomplete=_row_bool(row, keys, "last_incomplete"),
        last_truncated=_row_bool(row, keys, "last_truncated"),
        last_reason_codes=_row_reason_codes(row, keys),
        last_degraded=_row_bool(row, keys, "last_degraded"),
        last_complete=_row_bool(row, keys, "last_complete"),
    )


def _row_value(row: sqlite3.Row, keys: set[str], name: str) -> object:
    return row[name] if name in keys else None


def _row_bool(row: sqlite3.Row, keys: set[str], name: str) -> bool | None:
    value = _row_value(row, keys, name)
    return None if value is None else bool(value)


def _row_reason_codes(row: sqlite3.Row, keys: set[str]) -> tuple[str, ...]:
    raw = _row_value(row, keys, "last_reason_codes_json")
    if not raw:
        return ()
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    return _bounded_reason_codes(value if isinstance(value, list) else ())


def _optional_bool_int(value: bool | None) -> int | None:
    return None if value is None else int(bool(value))


class HealthAlertStore:
    """Alert cooldown and daily-summary state, separate from ``seen``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def record_detected(
        self,
        candidate: HealthAlertCandidate,
        *,
        detected_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_alert_state(
                  fingerprint, alert_type, health_key, current_status,
                  first_detected_at, last_detected_at, last_sent_at,
                  send_count, resolved_at
                ) values (?, ?, ?, ?, ?, ?, null, 0, null)
                on conflict(fingerprint) do update set
                  current_status=excluded.current_status,
                  last_detected_at=excluded.last_detected_at
                """,
                (
                    candidate.fingerprint,
                    candidate.alert_type,
                    candidate.health_key,
                    candidate.current_status,
                    iso_utc(detected_at),
                    iso_utc(detected_at),
                ),
            )
            self._conn.execute(
                """
                insert into source_health_alert_events(
                  run_id, fingerprint, detected_at, alert_type, payload_json
                ) values (?, ?, ?, ?, ?)
                on conflict(run_id, fingerprint) do nothing
                """,
                (
                    candidate.run_id,
                    candidate.fingerprint,
                    iso_utc(detected_at),
                    candidate.alert_type,
                    json.dumps(
                        asdict(candidate),
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            self._conn.execute(
                """
                delete from source_health_alert_events
                where detected_at < ?
                """,
                (iso_utc(utc_datetime(detected_at) - timedelta(days=30)),),
            )

    def should_send(
        self,
        candidate: HealthAlertCandidate,
        *,
        now: datetime,
        cooldown: timedelta,
    ) -> bool:
        row = self._conn.execute(
            """
            select last_sent_at, resolved_at
            from source_health_alert_state
            where fingerprint = ?
            """,
            (candidate.fingerprint,),
        ).fetchone()
        if row is None or not row["last_sent_at"] or row["resolved_at"]:
            return True
        return now - _parse_datetime(row["last_sent_at"]) >= cooldown

    def record_sent(
        self,
        candidate: HealthAlertCandidate,
        *,
        sent_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set last_sent_at = ?,
                    send_count = send_count + 1,
                    resolved_at = null
                where fingerprint = ?
                """,
                (iso_utc(sent_at), candidate.fingerprint),
            )

    def resolve_source_failures(
        self,
        health_key: str,
        *,
        resolved_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set resolved_at = ?
                where health_key = ?
                  and alert_type in (
                    'new_failure', 'continued_failure',
                    'direct_source_silence', 'direct_source_degraded',
                    'minor_degradation', 'unknown_diagnostics', 'feed_stale'
                  )
                  and resolved_at is null
                """,
                (iso_utc(resolved_at), health_key),
            )

    def resolve_source_recoveries(
        self,
        health_key: str,
        *,
        resolved_at: datetime,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                update source_health_alert_state
                set resolved_at = ?
                where health_key = ?
                  and alert_type in (?, ?)
                  and resolved_at is null
                """,
                (
                    iso_utc(resolved_at),
                    health_key,
                    ALERT_RECOVERY,
                    ALERT_MINOR_RECOVERY,
                ),
            )

    def open_minor_incident_keys(self) -> frozenset[str]:
        """Return keys whose only unresolved degradation is minor."""

        placeholders = ", ".join("?" * len(DEGRADATION_ALERT_TYPES))
        rows = self._conn.execute(
            f"""
            select health_key,
                   sum(case when alert_type = ? then 0 else 1 end) as actionable
            from source_health_alert_state
            where (resolved_at is null or last_detected_at > resolved_at)
              and alert_type in ({placeholders})
            group by health_key
            having actionable = 0
            """,
            (ALERT_MINOR_DEGRADATION, *DEGRADATION_ALERT_TYPES),
        ).fetchall()
        return frozenset(str(row["health_key"]) for row in rows)

    def recent_digest_events(
        self,
        *,
        since: datetime,
        inclusive: bool = True,
    ) -> tuple[tuple[datetime, HealthAlertCandidate], ...]:
        rows = self._conn.execute(
            f"""
            select detected_at, payload_json
            from source_health_alert_events
            where detected_at {'>=' if inclusive else '>'} ?
            order by detected_at, event_id
            limit ?
            """,
            (iso_utc(since), MAX_DIGEST_EVENTS),
        ).fetchall()
        return tuple(
            (
                _parse_datetime(row["detected_at"]),
                _candidate_from_payload(row["payload_json"]),
            )
            for row in rows
        )

    def digest_sent(self, digest_date: str) -> bool:
        return (
            self._conn.execute(
                """
                select 1
                from source_health_digest
                where digest_date = ?
                """,
                (digest_date,),
            ).fetchone()
            is not None
        )

    def last_digest_sent_at(self) -> datetime | None:
        row = self._conn.execute(
            """
            select sent_at
            from source_health_digest
            order by digest_date desc
            limit 1
            """
        ).fetchone()
        if row is None or not row["sent_at"]:
            return None
        return _parse_datetime(row["sent_at"])

    def mark_digest_sent(
        self,
        *,
        digest_date: str,
        sent_at: datetime,
        run_id: str,
        incident_count: int,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_digest(
                  digest_date, sent_at, run_id, incident_count
                ) values (?, ?, ?, ?)
                on conflict(digest_date) do nothing
                """,
                (
                    digest_date,
                    iso_utc(sent_at),
                    safe_run_id(run_id),
                    max(0, int(incident_count)),
                ),
            )

    def daily_summary_sent(self, summary_date: str) -> bool:
        return (
            self._conn.execute(
                """
                select 1
                from source_health_daily_summary
                where summary_date = ?
                """,
                (summary_date,),
            ).fetchone()
            is not None
        )

    def mark_daily_summary_sent(
        self,
        *,
        summary_date: str,
        sent_at: datetime,
        run_id: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_daily_summary(
                  summary_date, sent_at, run_id
                ) values (?, ?, ?)
                on conflict(summary_date) do nothing
                """,
                (summary_date, iso_utc(sent_at), safe_run_id(run_id)),
            )

    def latest_coverage_snapshot(self) -> dict[str, str] | None:
        row = self._conn.execute(
            """
            select coverage_json
            from source_health_coverage_snapshots
            order by observed_at desc, run_id desc
            limit 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            company: state
            for company, (state, _) in _coverage_snapshot_entries(
                row["coverage_json"]
            ).items()
        }

    def companies_with_recent_github_rows(
        self,
        *,
        since: datetime,
    ) -> frozenset[str]:
        rows = self._conn.execute(
            """
            select coverage_json
            from source_health_coverage_snapshots
            where observed_at >= ?
            """,
            (iso_utc(since),),
        ).fetchall()
        companies: set[str] = set()
        for row in rows:
            for company, (_, github_rows) in _coverage_snapshot_entries(
                row["coverage_json"]
            ).items():
                if github_rows is not None and github_rows > 0:
                    companies.add(company)
        return frozenset(companies)

    def recent_failure_occurrences(
        self,
        *,
        since: datetime,
    ) -> dict[tuple[str, str], tuple[HealthAlertCandidate, ...]]:
        """Group recent failure events by health key and sanitized error kind.

        Only the failure family is read, because a repeat is evidence about one
        recurring failure mode; recoveries and degradations never count toward
        it. Groups arrive oldest first, so the last entry of each is the most
        recent qualifying occurrence. Events without an error kind cannot be
        matched to a mode and are skipped rather than grouped together.
        """

        rows = self._conn.execute(
            """
            select payload_json
            from source_health_alert_events
            where detected_at >= ?
              and alert_type in (?, ?)
            order by detected_at, event_id
            limit ?
            """,
            (
                iso_utc(since),
                ALERT_NEW_FAILURE,
                ALERT_CONTINUED_FAILURE,
                MAX_FLAP_HISTORY_EVENTS,
            ),
        ).fetchall()
        grouped: dict[tuple[str, str], list[HealthAlertCandidate]] = {}
        for row in rows:
            candidate = _candidate_from_payload(row["payload_json"])
            if not candidate.error_kind:
                continue
            grouped.setdefault(
                (candidate.health_key, candidate.error_kind), []
            ).append(candidate)
        return {key: tuple(items) for key, items in grouped.items()}

    def recent_candidates(
        self,
        *,
        since: datetime,
    ) -> tuple[HealthAlertCandidate, ...]:
        rows = self._conn.execute(
            """
            select payload_json
            from source_health_alert_events
            where detected_at >= ?
            order by detected_at, run_id, fingerprint
            """,
            (iso_utc(since),),
        ).fetchall()
        return tuple(
            _candidate_from_payload(row["payload_json"])
            for row in rows
        )

    def record_coverage_snapshot(
        self,
        *,
        run_id: str,
        observed_at: datetime,
        coverage: Sequence[CompanyCoverage],
    ) -> None:
        data = {
            sanitize_error(item.company): _coverage_snapshot_value(item)
            for item in coverage
        }
        with self._conn:
            self._conn.execute(
                """
                insert into source_health_coverage_snapshots(
                  run_id, observed_at, coverage_json
                ) values (?, ?, ?)
                on conflict(run_id) do update set
                  observed_at=excluded.observed_at,
                  coverage_json=excluded.coverage_json
                """,
                (
                    safe_run_id(run_id),
                    iso_utc(observed_at),
                    json.dumps(data, sort_keys=True, separators=(",", ":")),
                ),
            )
            self._conn.execute(
                f"""
                delete from source_health_coverage_snapshots
                where run_id not in (
                  select run_id
                  from source_health_coverage_snapshots
                  order by observed_at desc, run_id desc
                  limit {MAX_COVERAGE_SNAPSHOTS}
                )
                """
            )

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            create table if not exists source_health_alert_state(
              fingerprint text primary key,
              alert_type text not null,
              health_key text not null,
              current_status text not null,
              first_detected_at text not null,
              last_detected_at text not null,
              last_sent_at text,
              send_count integer not null,
              resolved_at text
            );
            create index if not exists source_health_alert_health_key_idx
              on source_health_alert_state(health_key);
            create table if not exists source_health_daily_summary(
              summary_date text primary key,
              sent_at text not null,
              run_id text not null
            );
            create table if not exists source_health_alert_events(
              event_id integer primary key autoincrement,
              run_id text not null,
              fingerprint text not null,
              detected_at text not null,
              alert_type text not null,
              payload_json text not null,
              unique(run_id, fingerprint)
            );
            create index if not exists source_health_alert_events_detected_idx
              on source_health_alert_events(detected_at);
            create table if not exists source_health_coverage_snapshots(
              run_id text primary key,
              observed_at text not null,
              coverage_json text not null
            );
            create table if not exists source_health_minor_digest(
              digest_date text primary key,
              sent_at text not null,
              run_id text not null,
              incident_count integer not null
            );
            create table if not exists source_health_digest(
              digest_date text primary key,
              sent_at text not null,
              run_id text not null,
              incident_count integer not null
            );
            insert or ignore into source_health_digest(
              digest_date, sent_at, run_id, incident_count
            )
            select digest_date, sent_at, run_id, incident_count
            from source_health_minor_digest;
            """
        )
        self._conn.commit()


def _candidate_from_payload(payload: str) -> HealthAlertCandidate:
    """Rebuild stored candidates while tolerating older payload fields."""

    data = json.loads(payload)
    known = {field.name for field in fields(HealthAlertCandidate)}
    values = {key: value for key, value in data.items() if key in known}
    values["reason_codes"] = tuple(values.get("reason_codes") or ())
    return HealthAlertCandidate(**values)


def _coverage_snapshot_value(coverage: CompanyCoverage) -> object:
    if coverage.github_rows_returned is None:
        return coverage.state
    return {
        "state": coverage.state,
        "github_rows": max(0, int(coverage.github_rows_returned)),
    }


def _coverage_snapshot_entries(
    payload: object,
) -> dict[str, tuple[str, int | None]]:
    """Read legacy state strings and current evidence-bearing snapshots."""

    try:
        data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    entries: dict[str, tuple[str, int | None]] = {}
    for company, value in data.items():
        if isinstance(value, Mapping):
            state = str(value.get("state") or "")
            raw_rows = value.get("github_rows")
            github_rows = (
                max(0, int(raw_rows))
                if isinstance(raw_rows, int) and not isinstance(raw_rows, bool)
                else None
            )
        else:
            state, github_rows = str(value), None
        entries[str(company)] = (state, github_rows)
    return entries


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return utc_datetime(parsed)
