"""CLI entry point for one bounded hosted-notification delivery pass."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from contextlib import suppress

from .database import HostedDatabase
from .notification_mail import SMTPNotificationTransport
from .notification_worker import MAX_LIMIT, MIN_LIMIT, NotificationDeliveryWorker
from .settings import HostedSettings


def _limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not MIN_LIMIT <= parsed <= MAX_LIMIT:
        raise argparse.ArgumentTypeError(
            f"limit must be between {MIN_LIMIT} and {MAX_LIMIT}"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deliver one bounded pass of due hosted notification batches."
    )
    parser.add_argument("--limit", type=_limit, default=25)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    database: HostedDatabase | None = None
    try:
        settings = HostedSettings.from_env()
        if not settings.database_url:
            print("Notification delivery failed: hosted_database_not_configured", file=sys.stderr)
            return 2
        database = HostedDatabase(settings.database_url)
        worker = NotificationDeliveryWorker(
            database,
            SMTPNotificationTransport(settings),
            settings.public_frontend_url,
        )
        summary = worker.run(limit=args.limit)
    except (ValueError, RuntimeError):
        print("Notification delivery failed: hosted_delivery_unavailable", file=sys.stderr)
        return 1
    except Exception:  # noqa: BLE001 - final CLI boundary must not leak internals
        print("Notification delivery failed: hosted_delivery_unavailable", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            with suppress(Exception):
                database.dispose()

    print(
        "HOSTED-NOTIFICATION-DELIVERY "
        f"claimed={summary.claimed} sent={summary.sent} "
        f"retryable={summary.retryable_failures} "
        f"permanent_failed={summary.permanent_failures} "
        f"uncertain={summary.uncertain} cancelled={summary.cancelled} "
        f"recovered_pending={summary.recovered_pending} "
        f"recovered_uncertain={summary.recovered_uncertain}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
