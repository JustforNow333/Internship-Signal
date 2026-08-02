"""Offline collection-snapshot replay for hosted job imports."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.app.ingest import analyze_rows
from watcher.collection_snapshot import (
    CollectionBatch,
    CollectionSnapshotError,
    collection_config_fingerprint,
    load_collection_snapshot,
)
from watcher.config import DEFAULT_WATCHLIST_PATH, WatcherConfig, load_watchlist

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class SnapshotReplayError(ValueError):
    """A validated snapshot could not produce final analyzed jobs."""


@dataclass(frozen=True)
class ReplayedSnapshot:
    source_fingerprint: str
    source_identifier: str
    config: WatcherConfig
    batch: CollectionBatch
    jobs: tuple[Mapping[str, object], ...]


def replay_snapshot_jobs(
    snapshot_path: str | Path,
    *,
    watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH,
    allow_collection_config_mismatch: bool = False,
    loader: Callable[[str | Path], CollectionBatch] = load_collection_snapshot,
    analyzer: Callable[..., list[dict]] = analyze_rows,
) -> ReplayedSnapshot:
    """Validate, replay, and fingerprint one immutable snapshot without I/O effects."""

    path = Path(snapshot_path)
    fingerprint_before = snapshot_sha256(path)
    batch = loader(path)
    fingerprint_after = snapshot_sha256(path)
    if fingerprint_before != fingerprint_after:
        raise CollectionSnapshotError("Collection snapshot changed while being loaded")

    config = load_watchlist(watchlist_path)
    expected_config = collection_config_fingerprint(config)
    if (
        batch.collection_config_fingerprint != expected_config
        and not allow_collection_config_mismatch
    ):
        raise CollectionSnapshotError(
            "Collection snapshot configuration does not match the current "
            "collection-affecting watchlist settings"
        )

    jobs = analyzer(
        batch.mutable_rows(),
        today=batch.captured_at.date(),
    )
    if not isinstance(jobs, list) or any(not isinstance(job, Mapping) for job in jobs):
        raise SnapshotReplayError("snapshot_analysis_invalid")
    return ReplayedSnapshot(
        source_fingerprint=fingerprint_before,
        source_identifier=safe_snapshot_identifier(path),
        config=config,
        batch=batch,
        jobs=tuple(jobs),
    )


def snapshot_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_snapshot_identifier(path: str | Path) -> str:
    name = Path(path).name.strip()
    if not name or _CONTROL_RE.search(name):
        return "collection-snapshot.json.gz"
    return name[:200]
