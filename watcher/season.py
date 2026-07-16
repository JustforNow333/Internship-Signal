"""Pure internship-season status checks for watcher configuration."""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Sequence

SEASON_OK = "ok"
SEASON_ROLLOVER_DUE = "rollover_due"
SEASON_STALE = "stale"
SEASON_UNKNOWN = "unknown"
YEAR_PATTERN = re.compile(r"(?<!\d)(\d{4})(?!\d)")


def season_status(terms: Iterable[str], *, today: date | None = None) -> str:
    """Classify configured terms by their recognized four-digit years."""

    current = today or date.today()
    years = [int(match) for term in terms for match in YEAR_PATTERN.findall(str(term))]
    if not years:
        return SEASON_UNKNOWN

    newest = max(years)
    if newest < current.year:
        return SEASON_STALE
    if newest > current.year:
        return SEASON_OK
    if current.month >= 7:
        return SEASON_ROLLOVER_DUE
    return SEASON_OK


def company_season_warnings(
    companies: Sequence[object],
    default_terms: Sequence[str],
    *,
    today: date | None = None,
) -> tuple[str, ...]:
    """Return identifiable warnings for stale company-specific overrides."""

    default_normalized = _normalized_terms(default_terms)
    warnings = []
    for company in companies:
        terms = tuple(getattr(company, "terms", ()) or ())
        if _normalized_terms(terms) == default_normalized:
            continue
        if season_status(terms, today=today) == SEASON_STALE:
            name = str(getattr(company, "name", "(unnamed company)"))
            rendered = ", ".join(str(term) for term in terms)
            warnings.append(f"{name}: stale company terms override ({rendered})")
    return tuple(warnings)


def _normalized_terms(terms: Sequence[str]) -> tuple[str, ...]:
    return tuple(str(term).strip().casefold() for term in terms)
