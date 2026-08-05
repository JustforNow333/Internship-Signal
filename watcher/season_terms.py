"""Conservative matching for configured GitHub internship-season terms."""

from __future__ import annotations

import re
from typing import Iterable


_SEASON_ALIASES = {
    "spring": "spring",
    "summer": "summer",
    "fall": "fall",
    "autumn": "fall",
    "winter": "winter",
}
_SEASON = r"spring|summer|fall|autumn|winter"
_SEASON_YEAR_RE = re.compile(
    rf"^(?P<season>{_SEASON})(?:\s*-\s*|\s+)(?P<year>\d{{4}})"
    r"(?:\s+internship)?$",
    re.IGNORECASE,
)
_YEAR_SEASON_RE = re.compile(
    rf"^(?P<year>\d{{4}})(?:\s*-\s*|\s+)(?P<season>{_SEASON})"
    r"(?:\s+internship)?$",
    re.IGNORECASE,
)
_SHORT_YEAR_RE = re.compile(
    rf"^(?P<season>{_SEASON})\s+['\u2019](?P<year>\d{{2}})"
    r"(?:\s+internship)?$",
    re.IGNORECASE,
)


def normalize_term(value: object) -> str:
    """Normalize generic terms without broadening their exact semantics."""

    return " ".join(str(value).split()).casefold()


def season_term_key(value: object) -> tuple[str, str, int] | None:
    """Return a season/year key only for a complete supported expression."""

    normalized = normalize_term(value)
    for pattern in (_SEASON_YEAR_RE, _YEAR_SEASON_RE, _SHORT_YEAR_RE):
        match = pattern.fullmatch(normalized)
        if match is None:
            continue
        year_text = match.group("year")
        year = 2000 + int(year_text) if len(year_text) == 2 else int(year_text)
        season = _SEASON_ALIASES[match.group("season").casefold()]
        return ("season", season, year)
    return None


def season_term_matches(left: object, right: object) -> bool:
    """Compare season expressions by key and all other terms exactly."""

    left_season = season_term_key(left)
    right_season = season_term_key(right)
    if left_season is not None or right_season is not None:
        return left_season is not None and left_season == right_season
    return normalize_term(left) == normalize_term(right)


def terms_match(source_terms: Iterable[object], configured_terms: Iterable[object]) -> bool:
    """Return whether any source term exactly or season-equivalently matches."""

    wanted = [term for term in configured_terms if normalize_term(term)]
    if not wanted:
        return True
    available = [term for term in source_terms if normalize_term(term)]
    return any(
        season_term_matches(source_term, configured_term)
        for source_term in available
        for configured_term in wanted
    )
