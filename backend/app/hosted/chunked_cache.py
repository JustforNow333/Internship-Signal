"""Bounded-transaction wrapper around the deployed analysis cache.

The deployed ``watcher.analysis_cache.AnalysisCache.lookup_many`` refreshes
``last_accessed_at`` for every hit in one transaction, and ``store_many``
upserts every artifact in one transaction. SQLite's rollback journal keeps a
copy of each page a transaction changes until it commits, so on the hosted
volume one warm run's lookup needs roughly 4 KB of free disk per hit (about
514 MB for 130,000 hits), and re-storing every row after a failed lookup needs
as much again.

``ChunkedAnalysisCache`` changes only how much work one transaction carries:
it calls the deployed methods unchanged on consecutive chunks of at most
``chunk_rows`` keys, so each journal holds at most one chunk. It is injected
through the ``cache_factory`` parameter of ``analyze_rows_with_cache``, so no
fingerprinted module changes and the cache version (and every cached entry)
stays valid.

Results and fallback are the deployed ones:

* Lookups remove duplicate keys exactly as the deployed method does and use
  one timestamp for every chunk. If any chunk fails, the whole lookup reports
  ``failed`` with no hits, exactly as a deployed single-transaction failure
  does, so the run re-analyzes every row. Chunks that already committed keep
  their newer ``last_accessed_at``, which only delays their expiry.
* Stores write each chunk independently. A failed chunk writes nothing (the
  deployed method rolls it back and returns 0); committed chunks hold correct
  artifacts under their own fingerprints, which later runs simply reuse.

``HOSTED_ANALYSIS_CACHE_CHUNK_ROWS`` sets the chunk size: blank means the
10,000-row default, ``0`` or ``off`` restores the exact deployed call, and an
unparseable value uses the default and logs only a fixed reason.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from backend.app.ingest import static_analysis_artifact_is_valid
from watcher.analysis_cache import (
    STATIC_ANALYSIS_CACHE_VERSION,
    AnalysisCache,
    CacheLookupResult,
)

CHUNK_ENV = "HOSTED_ANALYSIS_CACHE_CHUNK_ROWS"
# ~40 MB of rollback journal per lookup chunk on the production-shaped cache,
# with the same lookup time as one transaction.
DEFAULT_CHUNK_ROWS = 10_000

AfterChunk = Callable[[str, int, "ChunkedAnalysisCache"], None]


@dataclass
class ChunkStats:
    """Per-run chunk counters for telemetry (counts and fixed tokens only)."""

    chunk_rows: int
    lookup_chunks: int = 0
    store_chunks: int = 0
    failure: str = "none"  # none, lookup, store, lookup_and_store


def chunk_rows_setting(value: str | None = None) -> tuple[int, str]:
    """Return ``(chunk_rows, reason)``; ``0`` means the deployed single call."""

    raw = os.getenv(CHUNK_ENV, "") if value is None else value
    text = raw.strip().lower()
    if not text:
        return DEFAULT_CHUNK_ROWS, ""
    if text == "off":
        return 0, ""
    # ASCII digits only: str.isdigit() also accepts characters such as "²"
    # that int() rejects, and int() rejects more than 4,300 digits.
    if text.isascii() and text.isdigit():
        try:
            return int(text), ""
        except ValueError:
            pass
    return DEFAULT_CHUNK_ROWS, "invalid_chunk_rows"


class ChunkedAnalysisCache(AnalysisCache):
    """``AnalysisCache`` whose bulk methods commit at most one chunk at a time."""

    def __init__(
        self,
        path: str | Path,
        *,
        cache_version: int = STATIC_ANALYSIS_CACHE_VERSION,
        chunk_rows: int = DEFAULT_CHUNK_ROWS,
        stats: ChunkStats | None = None,
        after_chunk: AfterChunk | None = None,
    ) -> None:
        if chunk_rows < 1:
            raise ValueError("chunk_rows must be positive")
        self.chunk_rows = int(chunk_rows)
        self.stats = stats
        self._after_chunk = after_chunk
        super().__init__(path, cache_version=cache_version)

    def lookup_many(
        self,
        fingerprints: Sequence[str],
        *,
        accessed_at: datetime | None = None,
    ) -> CacheLookupResult:
        # The deployed method removes duplicates the same way, so chunks never
        # repeat a key and the merged hits equal one deployed call's hits.
        keys = list(dict.fromkeys(str(value) for value in fingerprints))
        if not keys:
            return super().lookup_many(keys, accessed_at=accessed_at)
        # One timestamp for every chunk, as one deployed call would use.
        when = accessed_at or datetime.now(timezone.utc)
        artifacts: dict[str, dict] = {}
        invalid = 0
        for index, start in enumerate(range(0, len(keys), self.chunk_rows), start=1):
            self._count("lookup")
            part = super().lookup_many(keys[start:start + self.chunk_rows], accessed_at=when)
            if part.failed:
                self._fail("lookup")
                return CacheLookupResult({}, failed=True)
            artifacts.update(part.artifacts)
            invalid += part.invalid
            self._notify("lookup", index)
        return CacheLookupResult(artifacts, invalid=invalid)

    def store_many(
        self,
        artifacts: Mapping[str, Mapping[str, object]],
        *,
        accessed_at: datetime | None = None,
    ) -> int:
        if not artifacts:
            return 0
        when = accessed_at or datetime.now(timezone.utc)
        items = list(artifacts.items())
        written = 0
        for index, start in enumerate(range(0, len(items), self.chunk_rows), start=1):
            self._count("store")
            chunk = dict(items[start:start + self.chunk_rows])
            stored = super().store_many(chunk, accessed_at=when)
            if stored == 0 and _has_storable(chunk):
                # The deployed store returns 0 both when its transaction
                # failed and when it had nothing valid to write; only the
                # first is a failure. Later chunks still try: each is
                # independent and bounded.
                self._fail("store")
            written += stored
            self._notify("store", index)
        return written

    def _count(self, kind: str) -> None:
        if self.stats is not None:
            if kind == "lookup":
                self.stats.lookup_chunks += 1
            else:
                self.stats.store_chunks += 1

    def _fail(self, kind: str) -> None:
        # A run looks up before it stores, so a failed lookup followed by a
        # failed re-store (the disk-full path) reports both.
        if self.stats is None or self.stats.failure in (kind, "lookup_and_store"):
            return
        self.stats.failure = kind if self.stats.failure == "none" else "lookup_and_store"

    def _notify(self, kind: str, index: int) -> None:
        if self._after_chunk is not None:
            self._after_chunk(kind, index, self)


def _has_storable(chunk: Mapping[str, Mapping[str, object]]) -> bool:
    """Whether the deployed ``store_many`` would have written any artifact.

    Mirrors its validity and encoding checks, and runs only after a chunk
    wrote nothing, so the normal path never serializes twice.
    """

    for artifact in chunk.values():
        if not static_analysis_artifact_is_valid(artifact):
            continue
        try:
            json.dumps(artifact, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            continue
        return True
    return False


def chunked_cache_factory(
    chunk_rows: int,
    stats: ChunkStats | None = None,
) -> Callable[..., ChunkedAnalysisCache]:
    """A ``cache_factory`` for ``analyze_rows_with_cache``."""

    def factory(path: str | Path, *, cache_version: int) -> ChunkedAnalysisCache:
        return ChunkedAnalysisCache(
            path,
            cache_version=cache_version,
            chunk_rows=chunk_rows,
            stats=stats,
        )

    return factory
