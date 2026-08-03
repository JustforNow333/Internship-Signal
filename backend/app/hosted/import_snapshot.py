"""CLI for offline watcher snapshot replay into hosted PostgreSQL jobs."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from contextlib import suppress

from watcher.collection_snapshot import CollectionSnapshotError
from watcher.config import DEFAULT_WATCHLIST_PATH

from .catalog import CompanyCatalog
from .database import HostedDatabase
from .job_import import JobImportError, JobImportService
from .snapshot_jobs import SnapshotReplayError, replay_snapshot_jobs


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
    database_url = os.getenv("HOSTED_DATABASE_URL", "").strip()
    if not database_url:
        print("Snapshot import failed: hosted_database_not_configured", file=sys.stderr)
        return 2

    database: HostedDatabase | None = None
    try:
        replayed = replay_snapshot_jobs(
            args.snapshot,
            watchlist_path=args.watchlist,
            allow_collection_config_mismatch=args.allow_collection_config_mismatch,
        )
        database = HostedDatabase(database_url)
        service = JobImportService(
            database,
            CompanyCatalog.from_watcher_config(replayed.config),
        )
        result = service.import_jobs(
            replayed.jobs,
            source_fingerprint=replayed.source_fingerprint,
            source_identifier=replayed.source_identifier,
            source_type="collection_snapshot",
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
    finally:
        if database is not None:
            with suppress(Exception):
                database.dispose()

    counters = result.counters
    print(
        "HOSTED-JOB-IMPORT "
        f"outcome={result.outcome} "
        f"source={result.source_fingerprint[:12]} "
        f"received={counters.jobs_received} "
        f"inserted={counters.jobs_inserted} "
        f"updated={counters.jobs_updated} "
        f"unchanged={counters.jobs_unchanged} "
        f"skipped={counters.jobs_skipped} "
        f"matches_created={counters.matches_created}"
    )
    if result.skipped_reasons:
        reasons = ",".join(
            f"{reason}={count}"
            for reason, count in sorted(result.skipped_reasons.items())
        )
        print(f"HOSTED-JOB-IMPORT-SKIPS {reasons}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
