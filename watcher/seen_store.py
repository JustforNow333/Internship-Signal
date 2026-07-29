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


@dataclass(frozen=True)
class NotificationSelection:
    """Current eligible postings partitioned by persisted notification state."""

    pending: list[dict]
    emailed: list[dict]
    primed: list[dict]


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
                primed_at = coalesce(primed_at, ?)
            where job_id = ?
            """,
            (
                metadata["analyzed_job_id"],
                metadata["identity_key"],
                metadata["requisition_key"],
                metadata["location"],
                _iso(emailed_at) if emailed_at else None,
                _iso(primed_at) if primed_at else None,
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
    ) -> None:
        metadata = self._job_metadata(
            job,
            non_specific_urls=non_specific_urls,
        )
        self._conn.execute(
            """
            insert into seen(
              job_id, analyzed_job_id, identity_key, requisition_key,
              company, title, location, url, first_source, first_seen,
              emailed_at, primed_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(job_id) do update set
              analyzed_job_id = coalesce(nullif(excluded.analyzed_job_id, ''), seen.analyzed_job_id),
              identity_key = coalesce(nullif(excluded.identity_key, ''), seen.identity_key),
              requisition_key = coalesce(nullif(excluded.requisition_key, ''), seen.requisition_key),
              location = coalesce(nullif(excluded.location, ''), seen.location),
              emailed_at = coalesce(seen.emailed_at, excluded.emailed_at),
              primed_at = coalesce(seen.primed_at, excluded.primed_at)
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

        current_requisition = stable_requisition_key(job)
        if (
            not current_requisition
            and row["analyzed_job_id"]
            and str(job.get("id") or "") == row["analyzed_job_id"]
        ):
            return True
        if (
            not current_requisition
            and not row["identity_key"]
            and str(job.get("id") or "") == row["job_id"]
        ):
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
                  location text
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
            )
            for column, sql_type in migrations:
                if column not in existing_columns:
                    self._conn.execute(
                        f"alter table seen add column {column} {sql_type}"
                    )
            self._conn.execute(
                "create index if not exists seen_identity_key_idx on seen(identity_key)"
            )
            self._conn.execute(
                "create index if not exists seen_analyzed_job_id_idx on seen(analyzed_job_id)"
            )

    def _require_writable(self) -> None:
        if self.read_only:
            raise RuntimeError("SeenStore was opened read-only")


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
