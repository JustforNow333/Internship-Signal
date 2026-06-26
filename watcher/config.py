"""Configuration primitives for watcher sources.

Full watchlist.yml loading is a later build step. The source layer only needs a
small company config object so adapters can be used and tested independently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class CompanyCfg:
    """Per-company source configuration used by adapters."""

    name: str
    ats: str = ""
    token: str = ""
    aliases: Sequence[str] = field(default_factory=tuple)
    terms: Sequence[str] = field(default_factory=lambda: ("Summer 2026",))

    def match_names(self) -> tuple[str, ...]:
        return (self.name, *tuple(self.aliases))

