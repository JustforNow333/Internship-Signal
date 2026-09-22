"""Offline watcher snapshot replay into hosted PostgreSQL jobs.

``import_snapshot_into_hosted`` is the single snapshot-to-hosted import path.
The operator CLI below and the scheduled ``collect_and_import`` command both go
through it, so validation, fingerprinting, matching, and notification enqueueing
can never drift apart between them.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from watcher.collection_snapshot import CollectionSnapshotError
from watcher.config import DEFAULT_WATCHLIST_PATH

from .catalog import CompanyCatalog
from .database import HostedDatabase, database_url_from_env
from .job_import import JobImportError, JobImportResult, JobImportService
from .snapshot_jobs import SnapshotReplayError, replay_snapshot_jobs

DEFAULT_SOURCE_TYPE = "collection_snapshot"


def import_snapshot_into_hosted(
    snapshot_path: str | Path,
    *,
    database_url: str,
    watchlist_path: str | Path = DEFAULT_WATCHLIST_PATH,
    allow_collection_config_mismatch: bool = False,
    retry_failed: bool = False,
    source_type: str = DEFAULT_SOURCE_TYPE,
) -> JobImportResult:
    """Replay one validated snapshot file into hosted PostgreSQL.

    The snapshot is validated and fingerprinted before anything is written, and
    the whole import - jobs, matches, and notification work - commits or rolls
    back as one transaction inside ``JobImportService``. Reusing a succeeded
    fingerprint is an idempotent no-op.
    """

    replayed = replay_snapshot_jobs(
        snapshot_path,
        watchlist_path=watchlist_path,
        allow_collection_config_mismatch=allow_collection_config_mismatch,
    )
    database = HostedDatabase(database_url)
    try:
        service = JobImportService(
            database,
            CompanyCatalog.from_watcher_config(replayed.config),
        )
        return service.import_jobs(
            replayed.jobs,
            source_fingerprint=replayed.source_fingerprint,
            source_identifier=replayed.source_identifier,
            source_type=source_type,
            retry_failed=retry_failed,
        )
    finally:
        with suppress(Exception):
            database.dispose()


def import_summary_lines(result: JobImportResult) -> list[str]:
    """Bounded operational summary: counts and a truncated fingerprint only."""

    counters = result.counters
    lines = [
        "HOSTED-JOB-IMPORT "
        f"outcome={result.outcome} "
        f"source={result.source_fingerprint[:12]} "
        f"received={counters.jobs_received} "
        f"inserted={counters.jobs_inserted} "
        f"updated={counters.jobs_updated} "
        f"unchanged={counters.jobs_unchanged} "
        f"skipped={counters.jobs_skipped} "
        f"matches_created={counters.matches_created}"
    ]
    if result.skipped_reasons:
        reasons = ",".join(
            f"{reason}={count}"
            for reason, count in sorted(result.skipped_reasons.items())
        )
        lines.append(f"HOSTED-JOB-IMPORT-SKIPS {reasons}")
    return lines


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay a watcher collection snapshot into hosted PostgreSQL."
    )
    parser.add_argument("--snapshot", required=True, help="Validated .json.gz snapshot")
    parser.add_argument(
        "--watchlist",
        default=str(DEFAULT_WATCHLIST_PATH),
        help="Watcher configuration used to validate replay compatibility",
    )
    parser.add_argument(
        "--allow-collection-config-mismatch",
        action="store_true",
        help="Intentionally replay a snapshot captured with different collection settings",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Explicitly retry a prior failed import for the same source fingerprint",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database_url = database_url_from_env()
    if not database_url:
        print("Snapshot import failed: hosted_database_not_configured", file=sys.stderr)
        return 2

    try:
        result = import_snapshot_into_hosted(
            args.snapshot,
            database_url=database_url,
            watchlist_path=args.watchlist,
            allow_collection_config_mismatch=args.allow_collection_config_mismatch,
            retry_failed=args.retry_failed,
        )
    except (CollectionSnapshotError, OSError, SnapshotReplayError):
        print("Snapshot import failed: invalid_collection_snapshot", file=sys.stderr)
        return 1
    except JobImportError as exc:
        print(f"Snapshot import failed: {exc.code}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError):
        print("Snapshot import failed: hosted_import_unavailable", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - final CLI boundary must not leak internals
        print("Snapshot import failed: hosted_import_unavailable", file=sys.stderr)
        return 1

    for line in import_summary_lines(result):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
