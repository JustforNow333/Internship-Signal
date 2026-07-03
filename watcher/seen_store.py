"""SQLite seen-store for watcher runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class SeenStore:
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

    def has_seen(self, job_id: str) -> bool:
        row = self._conn.execute("select 1 from seen where job_id = ?", (job_id,)).fetchone()
        return row is not None

    def unseen(self, jobs: Iterable[dict]) -> list[dict]:
        return [job for job in jobs if not self.has_seen(job["id"])]

    def mark_seen(self, job: dict, *, seen_at: datetime | None = None, emailed_at: datetime | None = None) -> None:
        seen_at = seen_at or datetime.now(timezone.utc)
        extra = job.get("extra", {})
        self._conn.execute(
            """
            insert or ignore into seen(job_id, company, title, url, first_source, first_seen, emailed_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job["id"],
                job.get("company", ""),
                job.get("title", ""),
                job.get("source_url", ""),
                extra.get("source", ""),
                _iso(seen_at),
                _iso(emailed_at) if emailed_at else None,
            ),
        )
        self._conn.commit()

    def mark_many_seen(
        self,
        jobs: Iterable[dict],
        *,
        seen_at: datetime | None = None,
        emailed_at: datetime | None = None,
    ) -> None:
        timestamp = seen_at or datetime.now(timezone.utc)
        for job in jobs:
            self.mark_seen(job, seen_at=timestamp, emailed_at=emailed_at)

    def _init_schema(self) -> None:
        self._conn.execute(
            """
            create table if not exists seen(
              job_id text primary key,
              company text,
              title text,
              url text,
              first_source text,
              first_seen text,
              emailed_at text
            )
            """
        )
        self._conn.commit()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
