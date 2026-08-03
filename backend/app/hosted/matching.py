"""Deterministic, database-free hosted match decisions.

The decision below is intentionally pure: it never touches the database,
never consults watcher fit scores or model output, and never inspects
notification settings. Alert frequency and global notification pause govern
Phase 3 delivery only, so they must not change whether a match exists.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from watcher.eligibility import OUTSIDE_US, assess_us_location

# Reason codes are an allowlist. Nothing derived from posting descriptions,
# raw preference payloads, or source metadata may be persisted here.
REASON_COMPANY_WATCHED = "company_watched"
REASON_ROLE_SELECTED = "role_selected"
REASON_LOCATION_ANY = "location_any"
REASON_LOCATION_PREFERRED = "location_preferred"
REASON_LOCATION_UNITED_STATES = "location_united_states"
REASON_REMOTE_INCLUDED = "remote_included"
REASON_SEASON_ANY = "season_any"
REASON_SEASON_MATCH = "season_match"
REASON_SEASON_UNSPECIFIED = "season_unspecified"

REASON_CODES = frozenset(
    {
        REASON_COMPANY_WATCHED,
        REASON_ROLE_SELECTED,
        REASON_LOCATION_ANY,
        REASON_LOCATION_PREFERRED,
        REASON_LOCATION_UNITED_STATES,
        REASON_REMOTE_INCLUDED,
        REASON_SEASON_ANY,
        REASON_SEASON_MATCH,
        REASON_SEASON_UNSPECIFIED,
    }
)

# One reason per decided dimension (company, role, location, season) plus
# headroom. Persisted reason lists are truncated to this bound.
MAX_REASONS = 6
MAX_REASON_VALUE_LENGTH = 120

_US_COUNTRY_KEYS = frozenset(
    {"united states", "united states of america", "usa", "u s a", "us", "u s"}
)
_SEASON_ALIASES = {
    "spring": "spring",
    "summer": "summer",
    "fall": "fall",
    "autumn": "fall",
    "winter": "winter",
}
_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class MatchJob:
    """The normalized hosted job fields a match decision may read."""

    company_id: str
    role_id: str
    title: str = ""
    location: str = ""
    remote_status: str = ""
    description: str = ""
    is_open: bool = True


@dataclass(frozen=True)
class MatchPreferences:
    """The hosted preferences a match decision may read."""

    role_ids: frozenset[str] = field(default_factory=frozenset)
    preferred_locations: tuple[str, ...] = ()
    include_remote: bool = True
    internship_season: str = "Any season"


@dataclass(frozen=True)
class MatchDecision:
    matches: bool
    reasons: tuple[dict[str, str], ...] = ()


def evaluate_match(
    job: MatchJob,
    preferences: MatchPreferences,
    *,
    watching: bool,
    watch_paused: bool,
) -> MatchDecision:
    """Return whether ``job`` matches ``preferences`` and why.

    Every hard constraint must pass. Reasons are deterministic, ordered
    company -> role -> location -> season, and bounded by ``MAX_REASONS``.
    """

    if not watching or watch_paused or not job.is_open:
        return MatchDecision(False)
    if job.role_id not in preferences.role_ids:
        return MatchDecision(False)

    location_reason = _location_reason(job, preferences)
    if location_reason is None:
        return MatchDecision(False)
    season_reason = _season_reason(job, preferences)
    if season_reason is None:
        return MatchDecision(False)

    reasons = (
        _reason(REASON_COMPANY_WATCHED, job.company_id),
        _reason(REASON_ROLE_SELECTED, job.role_id),
        _reason(location_reason),
        _reason(season_reason),
    )
    return MatchDecision(True, reasons[:MAX_REASONS])


def bounded_reasons(value: object) -> tuple[dict[str, str], ...]:
    """Normalize stored reason payloads back into the bounded public shape."""

    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        code = str(item.get("code") or "")
        if code not in REASON_CODES:
            continue
        entry = {"code": code}
        raw_value = item.get("value")
        if isinstance(raw_value, str) and raw_value:
            entry["value"] = raw_value[:MAX_REASON_VALUE_LENGTH]
        result.append(entry)
        if len(result) >= MAX_REASONS:
            break
    return tuple(result)


def _reason(code: str, value: str | None = None) -> dict[str, str]:
    entry = {"code": code}
    if value:
        entry["value"] = str(value)[:MAX_REASON_VALUE_LENGTH]
    return entry


def _location_reason(job: MatchJob, preferences: MatchPreferences) -> str | None:
    """Return the location/remote reason code, or ``None`` when incompatible."""

    remote = _is_remote(job)
    # ``include_remote`` is an explicit opt-out, so a remote posting cannot
    # match a user who excluded remote work regardless of its city text.
    if remote and not preferences.include_remote:
        return None
    if not preferences.preferred_locations:
        return REASON_LOCATION_ANY

    job_tokens = _tokens(job.location)
    for preference in preferences.preferred_locations:
        key = _normalized(preference)
        if not key:
            continue
        if key in _US_COUNTRY_KEYS:
            if _united_states_compatible(job):
                return REASON_LOCATION_UNITED_STATES
            continue
        if job_tokens and _token_overlap(_tokens(preference), job_tokens):
            return REASON_LOCATION_PREFERRED
    if remote:
        return REASON_REMOTE_INCLUDED
    return None


def _season_reason(job: MatchJob, preferences: MatchPreferences) -> str | None:
    """Return the season reason code, or ``None`` when the seasons conflict.

    Season is not a stored hosted job column, so compatibility is derived from
    explicit season evidence in the posting title only. Titles without season
    evidence stay compatible, matching the repository's conservative rule that
    ambiguity passes rather than silently excluding postings.
    """

    preferred_terms, preferred_years = _season_signature(preferences.internship_season)
    if not preferred_terms and not preferred_years:
        return REASON_SEASON_ANY
    job_terms, job_years = _season_signature(job.title)
    if not job_terms and not job_years:
        return REASON_SEASON_UNSPECIFIED
    if preferred_terms and job_terms and preferred_terms.isdisjoint(job_terms):
        return None
    if preferred_years and job_years and preferred_years.isdisjoint(job_years):
        return None
    return REASON_SEASON_MATCH


def _united_states_compatible(job: MatchJob) -> bool:
    """Reuse the canonical watcher location gate; only explicit foreign fails."""

    decision = assess_us_location(
        {"location": job.location, "description": job.description}
    )
    return decision.status != OUTSIDE_US


def is_remote(job: MatchJob) -> bool:
    """Public remote classification so responses never re-derive it elsewhere."""

    return _is_remote(job)


def _is_remote(job: MatchJob) -> bool:
    return "remote" in _tokens(job.remote_status) or "remote" in _tokens(job.location)


def _season_signature(value: str) -> tuple[frozenset[str], frozenset[int]]:
    tokens = _tokens(value)
    terms = {_SEASON_ALIASES[token] for token in tokens if token in _SEASON_ALIASES}
    years = {int(match) for match in _YEAR_RE.findall(str(value or ""))}
    return frozenset(terms), frozenset(years)


def _normalized(value: str) -> str:
    return " ".join(_tokens(value))


def _tokens(value: str) -> tuple[str, ...]:
    text = _NON_ALNUM_RE.sub(" ", str(value or "").casefold())
    return tuple(token for token in text.split() if token)


def _token_overlap(
    needle: Sequence[str],
    haystack: Sequence[str],
) -> bool:
    """Whole-token containment in either direction.

    Token comparison avoids substring false positives such as the ``us``
    country abbreviation matching inside ``Austin``.
    """

    if not needle or not haystack:
        return False
    return _contains(haystack, needle) or _contains(needle, haystack)


def _contains(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    span = len(needle)
    if span > len(haystack):
        return False
    return any(
        tuple(haystack[index : index + span]) == tuple(needle)
        for index in range(len(haystack) - span + 1)
    )


def preferences_from_model(preferences: object) -> MatchPreferences:
    """Build the pure input from the stored ``UserPreference`` row."""

    role_ids = getattr(preferences, "role_ids", None) or []
    locations = getattr(preferences, "preferred_locations", None) or []
    return MatchPreferences(
        role_ids=frozenset(_strings(role_ids)),
        preferred_locations=tuple(_strings(locations)),
        include_remote=bool(getattr(preferences, "include_remote", True)),
        internship_season=str(getattr(preferences, "internship_season", "") or ""),
    )


def job_from_model(job: object) -> MatchJob:
    """Build the pure input from the stored ``HostedJob`` row."""

    return MatchJob(
        company_id=str(getattr(job, "company_id", "") or ""),
        role_id=str(getattr(job, "role_id", "") or ""),
        title=str(getattr(job, "title", "") or ""),
        location=str(getattr(job, "location", "") or ""),
        remote_status=str(getattr(job, "remote_status", "") or ""),
        description=str(getattr(job, "description", "") or ""),
        is_open=bool(getattr(job, "is_open", False)),
    )


def _strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(value for value in values if isinstance(value, str) and value)
