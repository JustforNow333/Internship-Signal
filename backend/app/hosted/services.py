"""Application-owned hosted dependencies and injectable clocks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from .catalog import CompanyCatalog
from .database import HostedDatabase
from .mailer import Mailer, configured_mailer
from .settings import HostedSettings


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class HostedServices:
    settings: HostedSettings
    database: HostedDatabase | None
    mailer: Mailer
    clock: Callable[[], datetime]
    catalog: CompanyCatalog

    @classmethod
    def build(
        cls,
        *,
        settings: HostedSettings | None = None,
        mailer: Mailer | None = None,
        clock: Callable[[], datetime] = utc_now,
        catalog: CompanyCatalog | None = None,
    ) -> HostedServices:
        resolved = settings or HostedSettings.from_env()
        return cls(
            settings=resolved,
            database=(
                HostedDatabase(resolved.database_url) if resolved.database_url else None
            ),
            mailer=mailer or configured_mailer(resolved),
            clock=clock,
            catalog=catalog or CompanyCatalog.from_watcher_config(),
        )
