"""Shadow-mode posting-generation detection.

A "generation" is one seasonal instance of a posting identity. Companies reuse
requisition IDs and evergreen careers URLs across years, so a genuinely new
seasonal posting can reuse an identity that notification state already holds.

Everything in this module is **shadow only**: it reports what *would* qualify as
a new generation. Nothing here participates in `SeenStore.partition`, email
selection, priming, or suppression, and no alert or email is produced.

Two triggers are allowed, both deliberately conservative:

* an explicit resolved season/year change under one stable identity, where the
  stored *and* current season keys both resolve and differ; and
* reappearance after sustained absence, counted only across collections that
  succeeded for that company, so a source outage can never manufacture one.

Title, location, description, URL-slug, and formatting changes never qualify.
Missing or unresolved season data fails closed to the same generation.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from watcher.season_terms import season_term_key

DEFAULT_GENERATION_ABSENCE_DAYS = 14
MIN_GENERATION_ABSENCE_DAYS = 1
MAX_GENERATION_ABSENCE_DAYS = 365
GENERATION_ABSENCE_DAYS_ENV = "WATCHER_GENERATION_ABSENCE_DAYS"

TRIGGER_SEASON_CHANGE = "season_change"
TRIGGER_SUSTAINED_ABSENCE = "sustained_absence"

MAX_SHADOW_TEXT_LENGTH = 120

_SEASON_WORDS = r"spring|summer|fall|autumn|winter"
_YEAR = r"(?:\d{4}|['’]\d{2})"
_SEASON_SPAN_RES = (
    re.compile(rf"\b(?:{_SEASON_WORDS})(?:\s*[-–—/]\s*|\s+){_YEAR}", re.I),
    re.compile(rf"\b\d{{4}}(?:\s*[-–—/]\s*|\s+)(?:{_SEASON_WORDS})\b", re.I),
)


class GenerationConfigError(ValueError):
    """Raised when the shadow-generation absence threshold is invalid."""


def generation_absence_days(value: str | int | None = None) -> int:
    """Return the validated healthy-absence threshold in whole days."""

    raw = os.getenv(GENERATION_ABSENCE_DAYS_ENV) if value is None else value
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_GENERATION_ABSENCE_DAYS
    if isinstance(raw, bool):
        raise GenerationConfigError(
            f"{GENERATION_ABSENCE_DAYS_ENV} must be an integer between "
            f"{MIN_GENERATION_ABSENCE_DAYS} and {MAX_GENERATION_ABSENCE_DAYS}"
        )
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise GenerationConfigError(
            f"{GENERATION_ABSENCE_DAYS_ENV} must be an integer between "
            f"{MIN_GENERATION_ABSENCE_DAYS} and {MAX_GENERATION_ABSENCE_DAYS}"
        ) from exc
    if days < MIN_GENERATION_ABSENCE_DAYS or days > MAX_GENERATION_ABSENCE_DAYS:
        raise GenerationConfigError(
            f"{GENERATION_ABSENCE_DAYS_ENV} must be an integer between "
            f"{MIN_GENERATION_ABSENCE_DAYS} and {MAX_GENERATION_ABSENCE_DAYS}"
        )
    return days


def season_key_for_title(title: object) -> str | None:
    """Return a stable ``season|<season>|<year>`` key, or ``None``.

    Season expressions are located inside free-form titles and then validated by
    the existing :func:`season_term_key`, so this never broadens what counts as a
    season. A bare year with no season word stays unresolved, and a title holding
    two different seasons fails closed to ``None`` rather than guessing.
    """

    text = str(title or "")
    if not text.strip():
        return None
    resolved: set[tuple[str, str, int]] = set()
    for pattern in _SEASON_SPAN_RES:
        for match in pattern.finditer(text):
            key = season_term_key(match.group(0))
            if key is not None:
                resolved.add(key)
    if len(resolved) != 1:
        return None
    _label, season, year = resolved.pop()
    return f"season|{season}|{year}"


@dataclass(frozen=True)
class ShadowGenerationCandidate:
    """One bounded, secret-free shadow-generation observation."""

    identity_key: str
    company: str
    title: str
    stored_season_key: str | None
    current_season_key: str | None
    current_generation: int
    proposed_generation: int
    trigger: str
    absence_days: float | None = None
    last_seen: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "identity_key": self.identity_key,
            "company": self.company,
            "title": self.title,
            "stored_season_key": self.stored_season_key,
            "current_season_key": self.current_season_key,
            "current_generation": self.current_generation,
            "proposed_generation": self.proposed_generation,
            "trigger": self.trigger,
            "absence_days": self.absence_days,
            "last_seen": self.last_seen,
        }

    def console_line(self) -> str:
        detail = (
            f"absence_days={self.absence_days:.2f}"
            if self.absence_days is not None
            else f"season {self.stored_season_key} -> {self.current_season_key}"
        )
        return (
            f"{bounded_text(self.company)} - {bounded_text(self.title)}: "
            f"{self.trigger} generation {self.current_generation} -> "
            f"{self.proposed_generation} ({detail}) identity={self.identity_key}"
        )


def bounded_text(value: object, limit: int = MAX_SHADOW_TEXT_LENGTH) -> str:
    """Return single-line text bounded for diagnostics output."""

    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 3)].rstrip() + "..."


def evaluate_season_change(
    stored_season_key: object,
    current_season_key: object,
) -> bool:
    """Return whether both season keys resolve and genuinely differ."""

    stored = str(stored_season_key or "").strip()
    current = str(current_season_key or "").strip()
    if not stored or not current:
        return False
    return stored != current
