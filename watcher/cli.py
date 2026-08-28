"""Command-line argument parsing and watcher process startup."""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import replace
from datetime import date
from pathlib import Path

from watcher.collection_snapshot import (
    CollectionSnapshotError,
    load_collection_snapshot,
)
from watcher.config import (
    DEFAULT_WATCHLIST_PATH,
    load_watchlist,
    resolve_analysis_cache_path,
)
from watcher.health_alerts import (
    MODE_OFF as HEALTH_EMAIL_OFF,
    HealthAlertPolicy,
    load_health_alert_policy,
)
from watcher.notify import email_sending_enabled
from watcher.pipeline import RUN_MODE_DRY, RUN_MODE_LIVE, RUN_MODE_PRIME, run_once
from watcher.reporting import print_heartbeat, print_report, _write_result_health_report
from watcher.run_logging import LOGGER, _timed_stage
from watcher.seen_store import SeenStore


def _parse_today(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--today must use YYYY-MM-DD"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    with _timed_stage("watcher_runtime"):
        with _timed_stage("configuration_startup"):
            parser = argparse.ArgumentParser(
                description="Run the internship watcher once and print new matches."
            )
            parser.add_argument(
                "--watchlist",
                default=str(DEFAULT_WATCHLIST_PATH),
                help="Path to watchlist.yml",
            )
            parser.add_argument("--seen-db", help="Path to SQLite seen-store")
            parser.add_argument(
                "--health-report",
                help="Write the sanitized machine-readable source-health JSON report to this path.",
            )
            parser.add_argument(
                "--prime-seen",
                "--mark-seen-without-send",
                dest="prime_seen",
                action="store_true",
                help="Explicitly prime/suppress current matches without sending email.",
            )
            snapshot_group = parser.add_mutually_exclusive_group()
            snapshot_group.add_argument(
                "--capture-collection-snapshot",
                metavar="PATH",
                help=(
                    "Save live collection as an atomic .json.gz snapshot, then "
                    "continue through the normal watcher pipeline."
                ),
            )
            snapshot_group.add_argument(
                "--replay-collection-snapshot",
                metavar="PATH",
                help=(
                    "Skip all network collection and process a saved .json.gz "
                    "collection snapshot in side-effect-free dry-run mode."
                ),
            )
            parser.add_argument(
                "--allow-collection-config-mismatch",
                action="store_true",
                help=(
                    "Intentionally replay a snapshot captured with different "
                    "collection-affecting configuration."
                ),
            )
            parser.add_argument(
                "--today",
                type=_parse_today,
                help=(
                    "Override the effective analysis date as YYYY-MM-DD; replay "
                    "otherwise uses the snapshot capture date."
                ),
            )
            args = parser.parse_args(argv)

            logging.basicConfig(
                level=logging.INFO,
                format="%(levelname)s %(name)s: %(message)s",
            )
            config = load_watchlist(args.watchlist)
            if args.seen_db:
                seen_db_path = Path(args.seen_db)
                config = replace(
                    config,
                    seen_db_path=seen_db_path,
                    analysis_cache_path=resolve_analysis_cache_path(
                        seen_db_path
                    ),
                )
            if (
                args.allow_collection_config_mismatch
                and not args.replay_collection_snapshot
            ):
                parser.error(
                    "--allow-collection-config-mismatch requires "
                    "--replay-collection-snapshot"
                )
            if args.replay_collection_snapshot and args.prime_seen:
                parser.error(
                    "--replay-collection-snapshot cannot be combined with --prime-seen"
                )
            replay_batch = None
            if args.replay_collection_snapshot:
                try:
                    replay_batch = load_collection_snapshot(
                        args.replay_collection_snapshot
                    )
                except CollectionSnapshotError as exc:
                    parser.error(str(exc))
            send_enabled = email_sending_enabled()
            if send_enabled and args.prime_seen:
                parser.error(
                    "WATCHER_SEND_EMAIL and --prime-seen cannot both be enabled"
                )
            notification_mode = (
                RUN_MODE_DRY
                if replay_batch is not None
                else
                RUN_MODE_PRIME
                if args.prime_seen
                else RUN_MODE_LIVE
                if send_enabled
                else RUN_MODE_DRY
            )

        try:
            with SeenStore(
                config.seen_db_path,
                read_only=replay_batch is not None,
            ) as seen_store:
                result = run_once(
                    config,
                    seen_store=seen_store,
                    notification_mode=notification_mode,
                    health_alert_policy=(
                        HealthAlertPolicy(mode=HEALTH_EMAIL_OFF)
                        if replay_batch is not None
                        else load_health_alert_policy()
                    ),
                    today=args.today,
                    collection_batch=replay_batch,
                    capture_collection_snapshot_path=(
                        args.capture_collection_snapshot
                    ),
                    replay_collection_snapshot_path=(
                        args.replay_collection_snapshot
                    ),
                    allow_collection_config_mismatch=(
                        args.allow_collection_config_mismatch
                    ),
                )
        except CollectionSnapshotError as exc:
            parser.error(str(exc))
        health_report_path = (
            args.health_report
            or os.getenv("WATCHER_HEALTH_REPORT_PATH", "").strip()
        )
        if health_report_path and replay_batch is not None:
            LOGGER.info(
                "Collection replay: source-health report was not written."
            )
        elif health_report_path:
            _write_result_health_report(result, health_report_path)
            LOGGER.info("Wrote source-health JSON report: %s", health_report_path)
        print_report(result)
        print_heartbeat(result)
        return 0
