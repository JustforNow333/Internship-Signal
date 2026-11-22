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

from internship_signal.domain.eligibility import (
    CATEGORICAL_EXCLUSION_REASONS,
    FRESHMAN_ONLY,
    GRADUATE_ONLY,
    PHD_ONLY,
)
from watcher.eligibility import OUTSIDE_US, assess_us_location

from .career_level import DEFAULT_CAREER_LEVELS, INTERNSHIP

# Reason codes are an allowlist. Nothing derived from posting descriptions,
# raw preference payloads, or source metadata may be persisted here.
REASON_COMPANY_WATCHED = "company_watched"
REASON_ROLE_SELECTED = "role_selected"
REASON_CAREER_LEVEL_SELECTED = "career_level_selected"
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
        REASON_CAREER_LEVEL_SELECTED,
        REASON_LOCATION_ANY,
        REASON_LOCATION_PREFERRED,
        REASON_LOCATION_UNITED_STATES,
        REASON_REMOTE_INCLUDED,
        REASON_SEASON_ANY,
        REASON_SEASON_MATCH,
        REASON_SEASON_UNSPECIFIED,
    }
)

# One reason per decided dimension (company, role, career level, location,
# season) plus headroom. Persisted reason lists are truncated to this bound.
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
# Education rules (Stage 3). Ids mirror models.EDUCATION_LEVELS and
# models.CLASS_STANDINGS; the database layer is not imported here.
EDUCATION_NOT_SPECIFIED = "not_specified"
EDUCATION_UNDERGRADUATE = "undergraduate"
EDUCATION_MASTERS = "masters"
EDUCATION_DOCTORAL = "doctoral"
# Which classifier exclusion reasons each specified education level refuses.
# doctoral, not_specified and unknown levels refuse nothing, and
# returning_intern_only never excludes: there is no employment-history profile.
_EDUCATION_EXCLUSIONS = {
    EDUCATION_UNDERGRADUATE: frozenset({PHD_ONLY, GRADUATE_ONLY}),
    EDUCATION_MASTERS: frozenset({PHD_ONLY}),
}
# Owner-approved correction to AC7: freshman_only refuses every undergraduate
# standing past the first year; first_year and not_specified stay eligible.
_FRESHMAN_EXCLUDED_STANDINGS = frozenset({"sophomore", "junior", "senior"})
# The classifier reports mixed postings such as "Master's or PhD" or "MS/PhD"
# as phd_only and "BS, MS" as graduate_only. A degree restriction never
# excludes a level its own stored evidence names; no other text is read.
_LEVEL_MENTION_RES = {
    EDUCATION_UNDERGRADUATE: re.compile(
        r"\b(?:undergrad(?:uate)?s?|bachelor'?s?|b\.?s\.?c?|b\.?a\.?|b\.?eng)\b",
        re.IGNORECASE,
    ),
    EDUCATION_MASTERS: re.compile(
        r"\b(?:master'?s?|m\.?s\.?c?|m\.?eng|mba|mfe|mse|mcs|mph|mpa|mfa|mfin)\b",
        re.IGNORECASE,
    ),
}
# Bounds of the stored verdict shape written by job_mapper.
_ELIGIBILITY_TEXT_FIELDS = ("degree_level", "evidence_source", "evidence")
_MAX_ELIGIBILITY_ID_LENGTH = 64
_MAX_ELIGIBILITY_EVIDENCE_LENGTH = 300

_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class MatchJob:
    """The normalized hosted job fields a match decision may read."""

    company_id: str
    role_id: str
    career_level: str = INTERNSHIP
    title: str = ""
    location: str = ""
    remote_status: str = ""
    description: str = ""
    is_open: bool = True
    # Stored classifier verdict; None (or anything malformed) is unknown.
    student_eligibility: Mapping[str, object] | None = None


@dataclass(frozen=True)
class MatchPreferences:
    """The hosted preferences a match decision may read."""

    role_ids: frozenset[str] = field(default_factory=frozenset)
    career_levels: frozenset[str] = frozenset(DEFAULT_CAREER_LEVELS)
    preferred_locations: tuple[str, ...] = ()
    include_remote: bool = True
    internship_season: str = "Any season"
    education_level: str = EDUCATION_NOT_SPECIFIED
    class_standing: str = EDUCATION_NOT_SPECIFIED


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
    company -> role -> career level -> location -> season, and bounded by
    ``MAX_REASONS``. Internship season applies to internships only, so other
    career levels carry no season reason.
    """

    if not watching or watch_paused or not job.is_open:
        return MatchDecision(False)
    if job.role_id not in preferences.role_ids:
        return MatchDecision(False)
    if job.career_level not in preferences.career_levels:
        return MatchDecision(False)

    location_reason = _location_reason(job, preferences)
    if location_reason is None:
        return MatchDecision(False)
    season_reason = None
    if job.career_level == INTERNSHIP:
        season_reason = _season_reason(job, preferences)
        if season_reason is None:
            return MatchDecision(False)
        # Education adds a hard constraint for internships only and no reason.
        if _education_excludes(job, preferences):
            return MatchDecision(False)

    reasons = [
        _reason(REASON_COMPANY_WATCHED, job.company_id),
        _reason(REASON_ROLE_SELECTED, job.role_id),
        _reason(REASON_CAREER_LEVEL_SELECTED, job.career_level),
        _reason(location_reason),
    ]
    if season_reason is not None:
        reasons.append(_reason(season_reason))
    return MatchDecision(True, tuple(reasons[:MAX_REASONS]))


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


def _education_excludes(job: MatchJob, preferences: MatchPreferences) -> bool:
    """Whether the stored verdict clearly conflicts with the user's education.

    Callers apply this to internships only. Unknown verdicts never exclude.
    """

    reason = _stored_exclusion_reason(job.student_eligibility)
    if reason is None:
        return False
    level = preferences.education_level
    if reason in _EDUCATION_EXCLUSIONS.get(level, frozenset()):
        # Mixed evidence ("Master's or PhD", "MS/PhD", "BS, MS") is ambiguous
        # for a level it names, so it never excludes that level.
        mentions = _LEVEL_MENTION_RES.get(level)
        evidence = job.student_eligibility.get("evidence")
        return not (
            mentions is not None
            and isinstance(evidence, str)
            and mentions.search(evidence)
        )
    return (
        reason == FRESHMAN_ONLY
        and level == EDUCATION_UNDERGRADUATE
        and preferences.class_standing in _FRESHMAN_EXCLUDED_STANDINGS
    )


def _stored_exclusion_reason(value: object) -> str | None:
    """Return a recognized exclusion reason from a well-formed verdict.

    Anything outside the stored verdict shape is unknown (None) and therefore
    permissive, even when it names a recognized reason. No classification
    happens here.
    """

    if not isinstance(value, Mapping) or "exclusion_reason" not in value:
        return None
    reason = value["exclusion_reason"]
    if not isinstance(reason, str) or reason not in CATEGORICAL_EXCLUSION_REASONS:
        return None
    if "eligible" in value and value["eligible"] is not False:
        return None
    for name in _ELIGIBILITY_TEXT_FIELDS:
        text = value.get(name)
        if text is None:
            continue
        limit = (
            _MAX_ELIGIBILITY_EVIDENCE_LENGTH
            if name == "evidence"
            else _MAX_ELIGIBILITY_ID_LENGTH
        )
        if not isinstance(text, str) or len(text) > limit:
            return None
    return reason


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
    career_levels = getattr(preferences, "career_levels", None) or list(
        DEFAULT_CAREER_LEVELS
    )
    locations = getattr(preferences, "preferred_locations", None) or []
    return MatchPreferences(
        role_ids=frozenset(_strings(role_ids)),
        career_levels=frozenset(_strings(career_levels)),
        preferred_locations=tuple(_strings(locations)),
        include_remote=bool(getattr(preferences, "include_remote", True)),
        internship_season=str(getattr(preferences, "internship_season", "") or ""),
        education_level=_preference_id(preferences, "education_level"),
        class_standing=_preference_id(preferences, "class_standing"),
    )


def job_from_model(job: object, *, include_description: bool = True) -> MatchJob:
    """Build the pure input from the stored ``HostedJob`` row.

    ``description`` is read only by the United States location check, so a
    caller that knows no preference needs it may pass
    ``include_description=False`` and avoid loading the posting text.
    """

    return MatchJob(
        company_id=str(getattr(job, "company_id", "") or ""),
        role_id=str(getattr(job, "role_id", "") or ""),
        career_level=str(getattr(job, "career_level", "") or ""),
        title=str(getattr(job, "title", "") or ""),
        location=str(getattr(job, "location", "") or ""),
        remote_status=str(getattr(job, "remote_status", "") or ""),
        description=(
            str(getattr(job, "description", "") or "")
            if include_description
            else ""
        ),
        is_open=bool(getattr(job, "is_open", False)),
        student_eligibility=_eligibility_mapping(
            getattr(job, "student_eligibility", None)
        ),
    )


def needs_job_description(preferences: MatchPreferences) -> bool:
    """Whether any preferred location triggers the United States check."""

    return any(
        _normalized(location) in _US_COUNTRY_KEYS
        for location in preferences.preferred_locations
    )


def _preference_id(preferences: object, name: str) -> str:
    value = getattr(preferences, name, None)
    return value if isinstance(value, str) and value else EDUCATION_NOT_SPECIFIED


def _eligibility_mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


def _strings(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(value for value in values if isinstance(value, str) and value)
