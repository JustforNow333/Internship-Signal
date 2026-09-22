"""One-shot hosted collection and import, safe for an external scheduler.

This is the scheduled counterpart to the operator snapshot CLI. It runs live
collection with the existing watcher source infrastructure, freezes the result
as a validated collection snapshot in a private temporary directory, and
replays that snapshot through the one shared hosted import path.

It deliberately touches none of the legacy watcher's runtime state. Collection
in :mod:`watcher.collection` is network and parsing only: it opens no seen
store, writes no source-health or analysis-cache database, and never reaches
the digest sender. Nothing here marks a posting as seen, so enabling this
command cannot suppress or duplicate the legacy personal digest, and the legacy
workflow is unaffected.

Notification delivery is a separate command on purpose. See
``app.hosted.deliver_notifications``: a collection failure must never stop
already-created notification work from being delivered.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

from watcher.collection import collect_batch
from watcher.collection_snapshot import (
    CollectionBatch,
    CollectionSnapshotError,
    save_collection_snapshot,
)
from watcher.config import DEFAULT_WATCHLIST_PATH, WatcherConfig, load_watchlist

from .database import database_url_from_env
from .import_snapshot import import_snapshot_into_hosted, import_summary_lines
from .job_import import JobImportError
from .snapshot_jobs import SnapshotReplayError

# Distinguishes scheduled collection from an operator's manual snapshot replay
# in `hosted_job_import_runs.source_type`.
HOSTED_COLLECTION_SOURCE_TYPE = "hosted_collection"
SNAPSHOT_NAME = "hosted-collection.json.gz"

Collector = Callable[[WatcherConfig], CollectionBatch]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect once with the watcher sources and import the result into "
            "hosted PostgreSQL. Does not deliver notification email."
        )
    )
    parser.add_argument(
        "--watchlist",
        default=str(DEFAULT_WATCHLIST_PATH),
        help="Watcher configuration describing the companies and sources to collect",
    )
    return parser


def collection_summary_line(batch: CollectionBatch) -> str:
    """Counts only: no posting text, company names, URLs, or raw source errors."""

    attempted = sum(1 for attempt in batch.source_attempts if attempt.attempted)
    succeeded = sum(1 for attempt in batch.source_attempts if attempt.succeeded)
    return (
        "HOSTED-COLLECTION "
        f"rows={len(batch.rows)} "
        f"sources_attempted={attempted} "
        f"sources_succeeded={succeeded} "
        f"source_errors={len(batch.errors)} "
        f"github_feeds_configured={batch.github_feeds_configured} "
        f"github_feeds_succeeded={batch.github_feeds_succeeded} "
        f"workday_attempted={batch.workday_attempted} "
        f"workday_succeeded={batch.workday_succeeded} "
        f"workday_failed={batch.workday_failed}"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    collector: Collector = collect_batch,
) -> int:
    args = build_parser().parse_args(argv)
    database_url = database_url_from_env()
    if not database_url:
        print(
            "Hosted collection failed: hosted_database_not_configured",
            file=sys.stderr,
        )
        return 2

    try:
        config = load_watchlist(args.watchlist)
    except Exception:  # noqa: BLE001 - CLI boundary must not leak configuration
        print(
            "Hosted collection failed: invalid_watcher_configuration",
            file=sys.stderr,
        )
        return 1

    # A private temporary directory, never a repository path, so a runtime
    # snapshot can never be committed and never outlives the run.
    workspace = Path(tempfile.mkdtemp(prefix="hosted-collection-"))
    try:
        batch = collector(config)
        print(collection_summary_line(batch))
        snapshot = workspace / SNAPSHOT_NAME
        # Saving through the official writer keeps snapshot validation on the
        # path rather than handing unvalidated rows to the importer.
        save_collection_snapshot(batch, snapshot)
        result = import_snapshot_into_hosted(
            snapshot,
            database_url=database_url,
            watchlist_path=args.watchlist,
            source_type=HOSTED_COLLECTION_SOURCE_TYPE,
        )
    except (CollectionSnapshotError, SnapshotReplayError, OSError):
        print(
            "Hosted collection failed: invalid_collection_result", file=sys.stderr
        )
        return 1
    except JobImportError as exc:
        print(f"Hosted collection failed: {exc.code}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError):
        print(
            "Hosted collection failed: hosted_import_unavailable", file=sys.stderr
        )
        return 1
    except Exception:  # noqa: BLE001 - final CLI boundary must not leak internals
        print(
            "Hosted collection failed: hosted_import_unavailable", file=sys.stderr
        )
        return 1
    finally:
        # Runs on success, on failure, and on an unexpected error alike.
        shutil.rmtree(workspace, ignore_errors=True)

    # Only reached when the import transaction committed, so a failure can
    # never print a successful import summary.
    for line in import_summary_lines(result):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
