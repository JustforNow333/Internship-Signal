"""SQLite notification state for watcher runs."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from backend.app.dedupe import (
    non_specific_posting_urls,
    posting_identity_key,
    postings_match,
    stable_requisition_key,
)
from watcher.company_matching import company_matching_key
from watcher.generation import (
    TRIGGER_SEASON_CHANGE,
    TRIGGER_SUSTAINED_ABSENCE,
    ShadowGenerationCandidate,
    bounded_text,
    evaluate_season_change,
    generation_absence_days,
    season_key_for_title,
)


# Bumped when the additive shadow-generation columns need a fresh backfill pass.
SEEN_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class NotificationSelection:
    """Current eligible postings partitioned by persisted notification state."""

    pending: list[dict]
    emailed: list[dict]
    primed: list[dict]


@dataclass(frozen=True)
class ObservationResult:
    """Outcome of one shadow observation pass. Never affects suppression."""

    observed: int
    rows_created: int
    rows_updated: int
    rows_skipped: int = 0
    shadow_candidates: tuple[ShadowGenerationCandidate, ...] = ()


class SeenStore:
    def __init__(
        self,
        path: str | Path,
        *,
        read_only: bool = False,
    ):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            self._conn = sqlite3.connect(":memory:")
            if self.path.is_file():
                source = sqlite3.connect(
                    f"{self.path.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
                try:
                    source.backup(self._conn)
                finally:
                    source.close()
        else:
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

    def has_seen(self, job_id: str) -> bool:
        row = self._conn.execute(
            """
            select 1
            from seen
            where job_id = ? or analyzed_job_id = ?
            limit 1
            """,
            (job_id, job_id),
        ).fetchone()
        return row is not None

    def unseen(self, jobs: Iterable[dict]) -> list[dict]:
        return self.partition(jobs).pending

    def records(self) -> list[dict[str, object]]:
        """Return notification records for read-only diagnostics."""

        rows = self._conn.execute(
            """
            select job_id, analyzed_job_id, identity_key, requisition_key,
                   company, title, location, url, first_source, first_seen,
                   emailed_at, primed_at
            from seen
            order by first_seen desc, job_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def matching_records(
        self,
        job: dict,
        *,
        posting_universe: Iterable[dict] = (),
        precomputed_non_specific_urls: frozenset[str] | None = None,
        preloaded_records: Iterable[Mapping[str, object]] | None = None,
    ) -> list[dict[str, object]]:
        """Return every historical row matching a current posting.

        This deliberately calls the same identity matcher used by
        :meth:`partition`; audit code therefore cannot drift from notification
        suppression semantics.
        """

        if precomputed_non_specific_urls is None:
            universe = [job, *list(posting_universe)]
            active_non_specific_urls = non_specific_posting_urls(universe)
        else:
            active_non_specific_urls = precomputed_non_specific_urls
        rows = (
            self._conn.execute(
                """
                select job_id, analyzed_job_id, identity_key, requisition_key,
                       company, title, location, url, first_source, first_seen,
                       emailed_at, primed_at
                from seen
                """
            ).fetchall()
            if preloaded_records is None
            else list(preloaded_records)
        )
        return [
            dict(row)
            for row in rows
            if self._row_matches_job(
                row,
                job,
                non_specific_urls=active_non_specific_urls,
            )
        ]

    def partition(self, jobs: Iterable[dict]) -> NotificationSelection:
        candidates = list(jobs)
        non_specific_urls = non_specific_posting_urls(candidates)
        rows = self._conn.execute(
            """
            select job_id, analyzed_job_id, identity_key, requisition_key,
                   company, title, location, url, first_source,
                   emailed_at, primed_at
            from seen
            where emailed_at is not null or primed_at is not null
            """
        ).fetchall()

        pending: list[dict] = []
        emailed: list[dict] = []
        primed: list[dict] = []
        for job in candidates:
            matching_rows = [
                row
                for row in rows
                if self._row_matches_job(
                    row,
                    job,
                    non_specific_urls=non_specific_urls,
                )
            ]
            if any(row["emailed_at"] for row in matching_rows):
                emailed.append(job)
            elif any(row["primed_at"] for row in matching_rows):
                primed.append(job)
            else:
                pending.append(job)
        return NotificationSelection(
            pending=pending,
            emailed=emailed,
            primed=primed,
        )

    def observe(
        self,
        jobs: Iterable[dict],
        *,
        observed_at: datetime | None = None,
        absence_days: int | None = None,
        collection_health: Mapping[str, bool] | None = None,
        create_rows_for: Iterable[dict] | None = None,
    ) -> ObservationResult:
        """Record that postings were collected and report shadow generations.

        This is the non-suppressing observation path. It writes only `last_seen`,
        `season_key`, and `absence_epoch`; `emailed_at` and `primed_at` are never
        set, cleared, or read here, so a posting observed for the first time gets
        a row that `partition()` ignores exactly like any other pending row.

        Every collected posting that already has a row has its `last_seen`
        advanced. `create_rows_for` bounds which *new* identities may add a row;
        callers pass the notification-eligible set so the durable store keeps
        growing only with the surface that can ever suppress, instead of with
        every posting the watchlist companies have ever published. Passing
        ``None`` allows every observed posting to create a row.

        Shadow candidates are computed against the *stored* state before this
        pass writes, then the stored state is advanced, which makes repeated runs
        idempotent: a season change or a credited absence is reported once and
        stays quiet afterwards.
        """

        self._require_writable()
        postings = list(jobs)
        timestamp = observed_at or datetime.now(timezone.utc)
        threshold_days = (
            generation_absence_days() if absence_days is None else int(absence_days)
        )
        non_specific_urls = non_specific_posting_urls(postings)
        creatable = (
            None
            if create_rows_for is None
            else {id(job) for job in create_rows_for}
        )
        candidates: list[ShadowGenerationCandidate] = []
        created = 0
        updated = 0
        skipped = 0

        with self._conn:
            health = collection_health or {}
            healthy_since = self._refresh_collection_health(
                health,
                observed_at=timestamp,
            )
            healthy_now = {
                company_matching_key(company)
                for company, healthy in health.items()
                if healthy and company_matching_key(company)
            }
            rows = self._conn.execute(
                """
                select job_id, analyzed_job_id, identity_key, requisition_key,
                       company, title, location, url, first_source,
                       emailed_at, primed_at, last_seen, season_key,
                       generation, absence_epoch
                from seen
                """
            ).fetchall()
            by_identity = {
                row["identity_key"]: row for row in rows if row["identity_key"]
            }
            for job in postings:
                identity = posting_identity_key(
                    job,
                    non_specific_urls=non_specific_urls,
                )
                existing = by_identity.get(identity) if identity else None
                if existing is None:
                    existing = next(
                        (
                            row
                            for row in rows
                            if self._row_matches_job(
                                row,
                                job,
                                non_specific_urls=non_specific_urls,
                            )
                        ),
                        None,
                    )
                current_season_key = season_key_for_title(job.get("title"))
                if existing is None:
                    if creatable is not None and id(job) not in creatable:
                        skipped += 1
                        continue
                    self._insert_or_update(
                        job,
                        first_seen=timestamp,
                        emailed_at=None,
                        primed_at=None,
                        non_specific_urls=non_specific_urls,
                        last_seen=timestamp,
                        season_key=current_season_key,
                    )
                    created += 1
                    continue

                candidate = self._shadow_candidate(
                    existing,
                    job,
                    identity_key=identity,
                    current_season_key=current_season_key,
                    observed_at=timestamp,
                    threshold_days=threshold_days,
                    healthy_since=healthy_since,
                    healthy_now=healthy_now,
                )
                if candidate is not None:
                    candidates.append(candidate)
                self._record_observation(
                    existing["job_id"],
                    observed_at=timestamp,
                    season_key=current_season_key,
                    advance_absence_epoch=(
                        candidate is not None
                        and candidate.trigger == TRIGGER_SUSTAINED_ABSENCE
                    ),
                )
                updated += 1

        return ObservationResult(
            observed=len(postings),
            rows_created=created,
            rows_updated=updated,
            rows_skipped=skipped,
            shadow_candidates=tuple(candidates),
        )

    def _shadow_candidate(
        self,
        row: sqlite3.Row,
        job: dict,
        *,
        identity_key: str,
        current_season_key: str | None,
        observed_at: datetime,
        threshold_days: int,
        healthy_since: dict[str, datetime],
        healthy_now: set[str],
    ) -> ShadowGenerationCandidate | None:
        """Return the single shadow candidate for one posting, or ``None``.

        Season change wins over sustained absence so one identity yields at most
        one candidate per run.
        """

        stored_season_key = row["season_key"]
        generation = int(row["generation"] or 1)
        base = {
            "identity_key": identity_key or (row["identity_key"] or ""),
            "company": bounded_text(job.get("company")),
            "title": bounded_text(job.get("title")),
            "stored_season_key": stored_season_key,
            "current_season_key": current_season_key,
            "current_generation": generation,
            "proposed_generation": generation + 1,
            "last_seen": row["last_seen"],
        }
        if evaluate_season_change(stored_season_key, current_season_key):
            return ShadowGenerationCandidate(trigger=TRIGGER_SEASON_CHANGE, **base)

        last_seen = _parse_iso(row["last_seen"])
        if last_seen is None:
            return None
        company_key = company_matching_key(job.get("company"))
        # Absence is credited only when this run collected the company cleanly
        # *and* the healthy streak covers the whole gap. A failed or degraded run
        # resets the streak past `last_seen`, and a run that reports no coverage
        # at all fails closed, so an outage can never look like a disappearance.
        if company_key not in healthy_now:
            return None
        streak_started = healthy_since.get(company_key)
        if streak_started is None or last_seen < streak_started:
            return None
        gap_days = (observed_at - last_seen).total_seconds() / 86400.0
        if gap_days < threshold_days:
            return None
        return ShadowGenerationCandidate(
            trigger=TRIGGER_SUSTAINED_ABSENCE,
            absence_days=round(gap_days, 2),
            **base,
        )

    def _record_observation(
        self,
        job_id: str,
        *,
        observed_at: datetime,
        season_key: str | None,
        advance_absence_epoch: bool,
    ) -> None:
        """Advance shadow columns only. Notification columns are never touched."""

        self._conn.execute(
            """
            update seen
            set last_seen = max(coalesce(last_seen, ''), ?),
                season_key = coalesce(nullif(?, ''), season_key),
                absence_epoch = coalesce(absence_epoch, 0) + ?
            where job_id = ?
            """,
            (
                _iso(observed_at),
                season_key or "",
                1 if advance_absence_epoch else 0,
                job_id,
            ),
        )

    def _refresh_collection_health(
        self,
        collection_health: Mapping[str, bool],
        *,
        observed_at: datetime,
    ) -> dict[str, datetime]:
        """Persist per-company healthy streaks and return their start times.

        A company that did not collect cleanly this run has its streak restarted
        at `observed_at`, which withdraws absence credit for every posting last
        seen before now.
        """

        timestamp = _iso(observed_at)
        for company, healthy in collection_health.items():
            key = company_matching_key(company)
            if not key:
                continue
            if healthy:
                self._conn.execute(
                    """
                    insert into seen_collection_health(
                      company_key, healthy_streak_started_at, last_healthy_at
                    )
                    values (?, ?, ?)
                    on conflict(company_key) do update set
                      last_healthy_at = excluded.last_healthy_at
                    """,
                    (key, timestamp, timestamp),
                )
            else:
                self._conn.execute(
                    """
                    insert into seen_collection_health(
                      company_key, healthy_streak_started_at, last_healthy_at
                    )
                    values (?, ?, null)
                    on conflict(company_key) do update set
                      healthy_streak_started_at = excluded.healthy_streak_started_at
                    """,
                    (key, timestamp),
                )
        return {
            row["company_key"]: parsed
            for row in self._conn.execute(
                "select company_key, healthy_streak_started_at "
                "from seen_collection_health"
            ).fetchall()
            if (parsed := _parse_iso(row["healthy_streak_started_at"])) is not None
        }

    def mark_emailed(
        self,
        job: dict,
        *,
        emailed_at: datetime | None = None,
    ) -> None:
        self.mark_many_emailed([job], emailed_at=emailed_at)

    def mark_many_emailed(
        self,
        jobs: Iterable[dict],
        *,
        emailed_at: datetime | None = None,
    ) -> None:
        timestamp = emailed_at or datetime.now(timezone.utc)
        self._mark_many(jobs, state="emailed", timestamp=timestamp)

    def mark_primed(
        self,
        job: dict,
        *,
        primed_at: datetime | None = None,
    ) -> None:
        self.mark_many_primed([job], primed_at=primed_at)

    def mark_many_primed(
        self,
        jobs: Iterable[dict],
        *,
        primed_at: datetime | None = None,
    ) -> None:
        timestamp = primed_at or datetime.now(timezone.utc)
        self._mark_many(jobs, state="primed", timestamp=timestamp)

    def mark_seen(
        self,
        job: dict,
        *,
        seen_at: datetime | None = None,
        emailed_at: datetime | None = None,
    ) -> None:
        """Backward-compatible discovery write.

        A row without ``emailed_at`` is deliberately pending and does not
        suppress a future notification. New watcher code should use
        ``mark_emailed`` or ``mark_primed`` explicitly.
        """

        if emailed_at is not None:
            self.mark_emailed(job, emailed_at=emailed_at)
            return
        self._record_many_discovered([job], seen_at=seen_at)

    def mark_many_seen(
        self,
        jobs: Iterable[dict],
        *,
        seen_at: datetime | None = None,
        emailed_at: datetime | None = None,
    ) -> None:
        if emailed_at is not None:
            self.mark_many_emailed(jobs, emailed_at=emailed_at)
            return
        self._record_many_discovered(jobs, seen_at=seen_at)

    def _mark_many(
        self,
        jobs: Iterable[dict],
        *,
        state: str,
        timestamp: datetime,
    ) -> None:
        self._require_writable()
        postings = list(jobs)
        non_specific_urls = non_specific_posting_urls(postings)
        with self._conn:
            for job in postings:
                self._upsert_state(
                    job,
                    state=state,
                    timestamp=timestamp,
                    non_specific_urls=non_specific_urls,
                )

    def _record_many_discovered(
        self,
        jobs: Iterable[dict],
        *,
        seen_at: datetime | None,
    ) -> None:
        self._require_writable()
        postings = list(jobs)
        timestamp = seen_at or datetime.now(timezone.utc)
        non_specific_urls = non_specific_posting_urls(postings)
        with self._conn:
            for job in postings:
                self._insert_or_update(
                    job,
                    first_seen=timestamp,
                    emailed_at=None,
                    primed_at=None,
                    non_specific_urls=non_specific_urls,
                )

    def _upsert_state(
        self,
        job: dict,
        *,
        state: str,
        timestamp: datetime,
        non_specific_urls: frozenset[str],
    ) -> None:
        emailed_at = timestamp if state == "emailed" else None
        primed_at = timestamp if state == "primed" else None
        existing = self._matching_row(
            job,
            non_specific_urls=non_specific_urls,
        )
        if existing is None:
            self._insert_or_update(
                job,
                first_seen=timestamp,
                emailed_at=emailed_at,
                primed_at=primed_at,
                non_specific_urls=non_specific_urls,
            )
            return

        metadata = self._job_metadata(
            job,
            non_specific_urls=non_specific_urls,
        )
        self._conn.execute(
            """
            update seen
            set analyzed_job_id = coalesce(nullif(?, ''), analyzed_job_id),
                identity_key = coalesce(nullif(?, ''), identity_key),
                requisition_key = coalesce(nullif(?, ''), requisition_key),
                location = coalesce(nullif(?, ''), location),
                emailed_at = coalesce(emailed_at, ?),
                primed_at = coalesce(primed_at, ?),
                last_seen = max(coalesce(last_seen, ''), ?),
                season_key = coalesce(nullif(?, ''), season_key)
            where job_id = ?
            """,
            (
                metadata["analyzed_job_id"],
                metadata["identity_key"],
                metadata["requisition_key"],
                metadata["location"],
                _iso(emailed_at) if emailed_at else None,
                _iso(primed_at) if primed_at else None,
                _iso(timestamp),
                season_key_for_title(job.get("title")) or "",
                existing["job_id"],
            ),
        )

    def _insert_or_update(
        self,
        job: dict,
        *,
        first_seen: datetime,
        emailed_at: datetime | None,
        primed_at: datetime | None,
        non_specific_urls: frozenset[str],
        last_seen: datetime | None = None,
        season_key: str | None = None,
    ) -> None:
        metadata = self._job_metadata(
            job,
            non_specific_urls=non_specific_urls,
        )
        observed = last_seen or first_seen
        resolved_season_key = (
            season_key
            if season_key is not None
            else season_key_for_title(job.get("title"))
        )
        self._conn.execute(
            """
            insert into seen(
              job_id, analyzed_job_id, identity_key, requisition_key,
              company, title, location, url, first_source, first_seen,
              emailed_at, primed_at, last_seen, season_key,
              generation, absence_epoch
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
            on conflict(job_id) do update set
              analyzed_job_id = coalesce(nullif(excluded.analyzed_job_id, ''), seen.analyzed_job_id),
              identity_key = coalesce(nullif(excluded.identity_key, ''), seen.identity_key),
              requisition_key = coalesce(nullif(excluded.requisition_key, ''), seen.requisition_key),
              location = coalesce(nullif(excluded.location, ''), seen.location),
              emailed_at = coalesce(seen.emailed_at, excluded.emailed_at),
              primed_at = coalesce(seen.primed_at, excluded.primed_at),
              last_seen = max(coalesce(seen.last_seen, ''), excluded.last_seen),
              season_key = coalesce(nullif(excluded.season_key, ''), seen.season_key)
            """,
            (
                metadata["storage_id"],
                metadata["analyzed_job_id"],
                metadata["identity_key"],
                metadata["requisition_key"],
                metadata["company"],
                metadata["title"],
                metadata["location"],
                metadata["url"],
                metadata["first_source"],
                _iso(first_seen),
                _iso(emailed_at) if emailed_at else None,
                _iso(primed_at) if primed_at else None,
                _iso(observed),
                resolved_season_key,
            ),
        )

    def _job_metadata(
        self,
        job: dict,
        *,
        non_specific_urls: frozenset[str],
    ) -> dict[str, str]:
        analyzed_job_id = str(job["id"])
        identity_key = posting_identity_key(
            job,
            non_specific_urls=non_specific_urls,
        )
        storage_seed = identity_key or f"analyzed|{analyzed_job_id}"
        storage_id = "posting:" + hashlib.sha1(
            storage_seed.encode("utf-8")
        ).hexdigest()[:20]
        extra = job.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        return {
            "storage_id": storage_id,
            "analyzed_job_id": analyzed_job_id,
            "identity_key": identity_key,
            "requisition_key": stable_requisition_key(job),
            "company": str(job.get("company") or ""),
            "title": str(job.get("title") or ""),
            "location": str(job.get("location") or ""),
            "url": str(job.get("source_url") or ""),
            "first_source": str(extra.get("source") or ""),
        }

    def _matching_row(
        self,
        job: dict,
        *,
        non_specific_urls: frozenset[str],
    ) -> sqlite3.Row | None:
        rows = self._conn.execute(
            """
            select job_id, analyzed_job_id, identity_key, requisition_key,
                   company, title, location, url, first_source,
                   emailed_at, primed_at
            from seen
            """
        ).fetchall()
        for row in rows:
            if self._row_matches_job(
                row,
                job,
                non_specific_urls=non_specific_urls,
            ):
                return row
        return None

    @staticmethod
    def _row_matches_job(
        row: sqlite3.Row | Mapping[str, object],
        job: dict,
        *,
        non_specific_urls: frozenset[str],
    ) -> bool:
        current_identity = posting_identity_key(
            job,
            non_specific_urls=non_specific_urls,
        )
        if row["identity_key"] and current_identity == row["identity_key"]:
            return True

        stored_extra = {"source": row["first_source"] or ""}
        if row["requisition_key"]:
            stored_extra["posting_requisition_key"] = row["requisition_key"]
        stored = {
            "company": row["company"] or "",
            "title": row["title"] or "",
            "location": row["location"] or "",
            "source_url": row["url"] or "",
            "extra": stored_extra,
        }
        return postings_match(
            stored,
            job,
            non_specific_urls=non_specific_urls,
        )

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                create table if not exists seen(
                  job_id text primary key,
                  company text,
                  title text,
                  url text,
                  first_source text,
                  first_seen text,
                  emailed_at text,
                  primed_at text,
                  analyzed_job_id text,
                  identity_key text,
                  requisition_key text,
                  location text,
                  last_seen text,
                  season_key text,
                  generation integer default 1,
                  absence_epoch integer default 0
                )
                """
            )
            existing_columns = {
                row["name"]
                for row in self._conn.execute("pragma table_info(seen)").fetchall()
            }
            migrations = (
                ("primed_at", "text"),
                ("analyzed_job_id", "text"),
                ("identity_key", "text"),
                ("requisition_key", "text"),
                ("location", "text"),
                # Shadow-generation columns. Additive and backward compatible:
                # older binaries simply ignore them, and suppression never reads
                # them.
                ("last_seen", "text"),
                ("season_key", "text"),
                ("generation", "integer default 1"),
                ("absence_epoch", "integer default 0"),
            )
            for column, sql_type in migrations:
                if column not in existing_columns:
                    self._conn.execute(
                        f"alter table seen add column {column} {sql_type}"
                    )
            self._conn.execute(
                """
                create table if not exists seen_collection_health(
                  company_key text primary key,
                  healthy_streak_started_at text,
                  last_healthy_at text
                )
                """
            )
            self._conn.execute(
                "create index if not exists seen_identity_key_idx on seen(identity_key)"
            )
            self._conn.execute(
                "create index if not exists seen_analyzed_job_id_idx on seen(analyzed_job_id)"
            )
            # `pragma user_version` records that the one-time backfill already
            # ran. Without this gate every store open would rescan and re-derive
            # a season key for every row whose title has none, which is unbounded
            # repeated work on a growing table.
            version = self._conn.execute("pragma user_version").fetchone()[0]
            if int(version or 0) < SEEN_SCHEMA_VERSION:
                self._backfill_generation_columns()
                self._conn.execute(f"pragma user_version = {SEEN_SCHEMA_VERSION}")

    def _backfill_generation_columns(self) -> None:
        """Populate shadow columns once, without touching notification state.

        `emailed_at`, `primed_at`, `identity_key`, and every other suppression
        input are deliberately untouched; only the four additive shadow columns
        are written, and only where they are still null. Re-running this is
        harmless, but the schema-version gate keeps it to one pass per upgrade.
        """

        self._conn.execute(
            "update seen set last_seen = first_seen "
            "where last_seen is null and first_seen is not null"
        )
        self._conn.execute("update seen set generation = 1 where generation is null")
        self._conn.execute(
            "update seen set absence_epoch = 0 where absence_epoch is null"
        )
        pending = self._conn.execute(
            "select job_id, title from seen where season_key is null and title is not null"
        ).fetchall()
        for row in pending:
            season_key = season_key_for_title(row["title"])
            if season_key:
                self._conn.execute(
                    "update seen set season_key = ? where job_id = ?",
                    (season_key, row["job_id"]),
                )

    def _require_writable(self) -> None:
        if self.read_only:
            raise RuntimeError("SeenStore was opened read-only")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: object) -> datetime | None:
    """Parse a stored UTC timestamp, tolerating legacy naive values."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
