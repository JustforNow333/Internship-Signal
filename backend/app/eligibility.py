"""Conservative categorical student-eligibility decisions.

These rules intentionally require explicit restriction evidence. Incidental,
preferred, mixed-group, and ambiguous mentions remain eligible.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Mapping

PHD_ONLY = "phd_only"
GRADUATE_ONLY = "graduate_only"
FRESHMAN_ONLY = "freshman_only"
RETURNING_INTERN_ONLY = "returning_intern_only"
CATEGORICAL_EXCLUSION_REASONS = frozenset(
    {PHD_ONLY, GRADUATE_ONLY, FRESHMAN_ONLY, RETURNING_INTERN_ONLY}
)

_PHD_TERM = r"(?:phd|doctoral|doctorate|post[- ]?doctoral|post[- ]?doc)"
_MASTERS_TERM = r"(?:master(?:'s|s)?|ms|master of (?:science|arts))"
_GRADUATE_TERM = (
    rf"(?:graduate (?:degree|program|student|students|candidate|candidates)|"
    rf"{_MASTERS_TERM}(?: degree| program| student| students| candidate| candidates)?|"
    r"mba(?: degree| program| student| students| candidate| candidates)?|"
    r"jd(?: degree| program| student| students| candidate| candidates)?|"
    r"juris doctor(?: degree| program| student| students| candidate| candidates)?|"
    r"law (?:degree|school|student|students|candidate|candidates)|"
    r"advanced[- ]degree(?: program| student| students| candidate| candidates)?|"
    r"post[- ]?baccalaureate(?: program| student| students)?|"
    r"postgraduate(?: degree| program| student| students)?)"
)
_UNDERGRAD_TERM = (
    r"(?:undergraduate|undergrad|bachelor(?:'s|s)?|"
    r"baccalaureate|associate(?:'s|s)? degree|\bbs\b|\bba\b)"
)
_FRESHMAN_TERM = (
    r"(?:freshman|freshmen|college freshman|college freshmen|"
    r"first[- ]year (?:college )?student|first[- ]year (?:college )?students)"
)
_SECOND_YEAR_TERM = (
    r"(?:sophomore|sophomores|second[- ]year (?:college )?student|"
    r"second[- ]year (?:college )?students|junior|juniors|senior|seniors)"
)
_PREFERENCE_RE = re.compile(
    r"\b(?:preferred|preference|preferably|desired|nice to have|a plus|"
    r"ideally|encouraged|welcome to apply)\b",
    re.I,
)
_PREFERRED_HEADING_RE = re.compile(
    r"^\s*(?:preferred|desired|bonus|nice[- ]to[- ]have)"
    r"(?:\s+(?:qualifications?|requirements?|skills?))?\s*:?\s*(.*)$",
    re.I,
)
_REQUIRED_HEADING_RE = re.compile(
    r"^\s*(?:minimum|required|basic|mandatory|must[- ]have)"
    r"(?:\s+(?:qualifications?|requirements?|skills?|education))?\s*:?\s*(.*)$",
    re.I,
)
_STRUCTURED_ELIGIBILITY_KEYS = frozenset(
    {
        "academiclevel",
        "academicdegree",
        "academicstanding",
        "academicstatus",
        "applicanteligibility",
        "candidatelevel",
        "candidateeligibility",
        "classyear",
        "degree",
        "degreelevel",
        "degreerequirement",
        "degreerequirements",
        "educationlevel",
        "educationrequirement",
        "educationrequirements",
        "eligibility",
        "eligibilitycriteria",
        "eligibilityrequirements",
        "minimumdegree",
        "minimumeducation",
        "minimumqualification",
        "minimumqualifications",
        "prioreligibility",
        "priorinternrequired",
        "programeligibility",
        "requireddegree",
        "requirededucation",
        "requiredqualification",
        "requiredqualifications",
        "returningintern",
        "returninginternonly",
        "studenteligibility",
        "studentlevel",
        "studentstatus",
        "targetdegree",
        "targetstudentlevel",
    }
)
_STRUCTURED_CONTAINER_KEYS = frozenset(
    {
        "additionalmetadata",
        "applicationmetadata",
        "candidate",
        "extra",
        "jobmetadata",
        "metadata",
        "postingmetadata",
        "qualifications",
        "requirementsmetadata",
        "sourcedetails",
        "sourcemetadata",
    }
)
_DIRECT_STRUCTURED_STATUS_KEYS = frozenset(
    {
        "academiclevel",
        "academicdegree",
        "academicstanding",
        "academicstatus",
        "applicanteligibility",
        "candidatelevel",
        "candidateeligibility",
        "classyear",
        "degree",
        "degreelevel",
        "degreerequirement",
        "degreerequirements",
        "educationlevel",
        "educationrequirement",
        "educationrequirements",
        "eligibility",
        "eligibilitycriteria",
        "eligibilityrequirements",
        "minimumdegree",
        "minimumeducation",
        "programeligibility",
        "requireddegree",
        "requirededucation",
        "studenteligibility",
        "studentlevel",
        "studentstatus",
        "targetdegree",
        "targetstudentlevel",
    }
)


@dataclass(frozen=True)
class StudentEligibilityDecision:
    eligible: bool
    exclusion_reason: str | None
    degree_level: str
    evidence_source: str | None
    evidence: str | None
    explanation: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _Evidence:
    source: str
    text: str
    kind: str


def assess_student_eligibility(row: Mapping[str, object]) -> StudentEligibilityDecision:
    """Apply explicit structured, title, required, then description evidence."""

    company = str(row.get("company") or "")
    evidence = [
        *_structured_evidence(row),
        _Evidence("title", str(row.get("title") or ""), "title"),
        *(
            _Evidence("requirements", segment, "requirements")
            for segment, preferred in _qualification_segments(row.get("requirements"))
            if not preferred
        ),
        *(
            _Evidence("description", segment, "description")
            for segment, preferred in _qualification_segments(row.get("description"))
            if not preferred
        ),
    ]
    structured_allowances: set[str] = set()
    for item in evidence:
        if not item.text.strip():
            continue
        normalized = _normalize_text(item.text)
        if item.kind == "structured":
            if _mixed_degree_eligibility(normalized):
                structured_allowances.update({PHD_ONLY, GRADUATE_ONLY})
            if _mixed_class_year_eligibility(normalized):
                structured_allowances.add(FRESHMAN_ONLY)
        restriction = _restriction(item, company=company)
        if restriction is None:
            continue
        reason, degree_level = restriction
        if reason in structured_allowances:
            continue
        snippet = _bounded_evidence(item.text)
        return StudentEligibilityDecision(
            eligible=False,
            exclusion_reason=reason,
            degree_level=degree_level,
            evidence_source=item.source,
            evidence=snippet,
            explanation=(
                f"Clear mandatory {reason} restriction found in {item.source}: "
                f'"{snippet}"'
            ),
        )

    return StudentEligibilityDecision(
        eligible=True,
        exclusion_reason=None,
        degree_level=_allowed_degree_level(row),
        evidence_source=None,
        evidence=None,
        explanation=(
            "No title, structured field, required qualification, or mandatory "
            "description language clearly limits eligibility to an excluded group."
        ),
    )


def _restriction(item: _Evidence, *, company: str) -> tuple[str, str] | None:
    text = _normalize_text(item.text)
    if not text or _PREFERENCE_RE.search(text):
        return None
    direct_structured = (
        item.kind == "structured"
        and _key_token(item.source.rsplit(".", 1)[-1])
        in _DIRECT_STRUCTURED_STATUS_KEYS
    )

    if _phd_only(text, kind=item.kind, direct_structured=direct_structured):
        degree_level = (
            "postdoctoral"
            if re.search(r"\bpost[- ]?doc(?:toral)?\b", text)
            else "phd"
        )
        return PHD_ONLY, degree_level
    graduate_level = _graduate_only(
        text,
        kind=item.kind,
        direct_structured=direct_structured,
    )
    if graduate_level is not None:
        return GRADUATE_ONLY, graduate_level
    if _freshman_only(
        text,
        kind=item.kind,
        direct_structured=direct_structured,
    ):
        return FRESHMAN_ONLY, "undergraduate"
    if _returning_intern_only(text, kind=item.kind, company=company):
        return RETURNING_INTERN_ONLY, "unspecified"
    return None


def _phd_only(text: str, *, kind: str, direct_structured: bool) -> bool:
    if not re.search(rf"\b{_PHD_TERM}\b", text) or _mixed_degree_eligibility(text):
        return False
    if kind == "title":
        return bool(
            re.search(rf"\b{_PHD_TERM}\b.*\b(?:intern|internship|co[- ]?op)\b", text)
            or re.search(rf"\b(?:intern|internship|co[- ]?op)\b.*\b{_PHD_TERM}\b", text)
        )
    if direct_structured:
        return True
    return bool(
        re.search(rf"\bcurrently (?:pursuing|enrolled in)\b.{{0,70}}\b{_PHD_TERM}\b", text)
        or re.search(rf"\b(?:must|required to|is required to)\b.{{0,70}}\b(?:pursue|be enrolled|hold|have)\b.{{0,40}}\b{_PHD_TERM}\b", text)
        or re.search(rf"\b(?:must be|required to be)\b.{{0,50}}\b{_PHD_TERM}\b.{{0,30}}\b(?:student|candidate)\b", text)
        or re.search(rf"\b{_PHD_TERM}\b.{{0,35}}\b(?:students?|candidates?)\b.{{0,20}}\bonly\b", text)
        or re.search(rf"\b(?:only|exclusively|restricted to)\b.{{0,45}}\b{_PHD_TERM}\b.{{0,30}}\b(?:students?|candidates?|program)\b", text)
        or re.search(rf"\bexpected graduation from\b.{{0,50}}\b{_PHD_TERM}\b.{{0,20}}\bprogram\b", text)
        or re.search(rf"\b(?:minimum|required) (?:degree|education|qualification)\b.{{0,35}}\b{_PHD_TERM}\b", text)
        or re.search(rf"\b{_PHD_TERM}\b.{{0,15}}\b(?:degree|enrollment)\b.{{0,15}}\b(?:required|only)\b", text)
        or re.search(rf"\beligible applicants are\b.{{0,40}}\b{_PHD_TERM}\b.{{0,30}}\b(?:students?|candidates?)\b", text)
    )


def _graduate_only(
    text: str,
    *,
    kind: str,
    direct_structured: bool,
) -> str | None:
    match = re.search(rf"\b{_GRADUATE_TERM}\b", text)
    if kind == "title" and re.search(
        r"\bgraduate(?: student| research)? intern(?:ship)?\b",
        text,
    ):
        return "graduate"
    if match is None or _mixed_degree_eligibility(text):
        return None
    degree_level = _graduate_level(text)
    if kind == "title":
        direct_title = bool(
            re.search(rf"\b{_GRADUATE_TERM}\b.*\b(?:intern|internship|co[- ]?op)\b", text)
            or re.search(rf"\b(?:intern|internship|co[- ]?op)\b.*\b{_GRADUATE_TERM}\b", text)
        )
        return degree_level if direct_title else None
    if direct_structured:
        return degree_level
    mandatory = bool(
        re.search(rf"\bcurrently (?:pursuing|enrolled in)\b.{{0,70}}\b{_GRADUATE_TERM}\b", text)
        or re.search(rf"\b(?:must|required to|is required to)\b.{{0,70}}\b(?:pursue|be enrolled|hold|have)\b.{{0,40}}\b{_GRADUATE_TERM}\b", text)
        or re.search(rf"\b{_GRADUATE_TERM}\b.{{0,35}}\b(?:students?|candidates?)\b.{{0,20}}\bonly\b", text)
        or re.search(rf"\b(?:only|exclusively|restricted to)\b.{{0,45}}\b{_GRADUATE_TERM}\b", text)
        or re.search(rf"\b(?:minimum|required) (?:degree|education|qualification)\b.{{0,35}}\b{_GRADUATE_TERM}\b", text)
        or re.search(rf"\b{_GRADUATE_TERM}\b.{{0,20}}\b(?:required|only)\b", text)
        or re.search(rf"\beligible applicants are\b.{{0,45}}\b{_GRADUATE_TERM}\b", text)
    )
    return degree_level if mandatory else None


def _freshman_only(text: str, *, kind: str, direct_structured: bool) -> bool:
    freshman = bool(re.search(rf"\b{_FRESHMAN_TERM}\b", text))
    rising_sophomore = bool(re.search(r"\brising sophomores?\b", text))
    if not freshman and not rising_sophomore:
        return False
    if _mixed_class_year_eligibility(text):
        return False
    if kind == "title":
        return freshman and bool(re.search(r"\b(?:intern|internship|co[- ]?op)\b", text))
    if direct_structured:
        return True
    return bool(
        re.search(rf"\b{_FRESHMAN_TERM}\b.{{0,25}}\bonly\b", text)
        or re.search(rf"\b(?:only|exclusively|restricted to)\b.{{0,40}}\b{_FRESHMAN_TERM}\b", text)
        or re.search(rf"\b(?:must be|required to be)\b.{{0,35}}\b{_FRESHMAN_TERM}\b", text)
        or re.search(rf"\beligible applicants are\b.{{0,35}}\b{_FRESHMAN_TERM}\b", text)
        or re.search(r"\brising sophomores?\b.{0,25}\bonly\b", text)
        or re.search(r"\b(?:must be|required to be|eligible applicants are)\b.{0,35}\brising sophomores?\b", text)
    )


def _returning_intern_only(text: str, *, kind: str, company: str) -> bool:
    returning_title = bool(
        re.search(r"\breturning intern(?:s|ship|ships)?\b", text)
        or re.search(r"\breturn internship\b", text)
    )
    if kind == "title":
        return returning_title
    if kind == "structured":
        compact = _key_token(text)
        explicitly_false = bool(re.search(r"\b(?:false|no|not required)\b", text))
        if not explicitly_false and (
            returning_title
            or "returningintern" in compact
            or "priorinternrequired" in compact
        ):
            return True
    if re.search(r"\b(?:former|returning) interns?\b.{0,25}\bonly\b", text):
        return True
    if re.search(r"\bfor returning interns?\b", text):
        return True
    if re.search(r"\binvitation[- ]only\b.{0,35}\b(?:return|returning) internship\b", text):
        return True
    if re.search(r"\b(?:return|returning) internship\b.{0,35}\binvitation[- ]only\b", text):
        return True
    prior_program_only = bool(re.search(
        r"\b(?:eligible|open|restricted)\b.{0,25}\bonly\b.{0,45}"
        r"\bprior (?:internship )?program participants?\b",
        text,
    ) or re.search(
        r"\bprior (?:internship )?program participants?\b.{0,25}\bonly\b",
        text,
    ))
    if prior_program_only and re.search(
        r"\b(?:internship|interned|returning interns?)\b",
        text,
    ):
        return True
    if not _same_employer_reference(text, company=company):
        return False
    return bool(
        re.search(
            r"\b(?:must|required to|is required to)\b.{0,80}"
            r"\b(?:previously (?:completed an? internship|interned)|prior internship)\b",
            text,
        )
        or re.search(
            r"\b(?:previously (?:completed an? internship|interned)|prior internship)\b"
            r".{0,80}\b(?:required|mandatory|must have)\b",
            text,
        )
    )


def _mixed_degree_eligibility(text: str) -> bool:
    if not re.search(rf"\b{_UNDERGRAD_TERM}\b", text):
        return False
    if not (
        re.search(rf"\b{_PHD_TERM}\b", text)
        or re.search(rf"\b{_GRADUATE_TERM}\b", text)
    ):
        return False
    return bool(
        re.search(
            rf"\b{_UNDERGRAD_TERM}\b.{{0,80}}(?:\b(?:or|and/or)\b|/|;).{{0,80}}"
            rf"\b(?:{_PHD_TERM}|{_GRADUATE_TERM})\b",
            text,
        )
        or re.search(
            rf"\b(?:{_PHD_TERM}|{_GRADUATE_TERM})\b.{{0,80}}"
            rf"(?:\b(?:or|and/or)\b|/|;).{{0,80}}\b{_UNDERGRAD_TERM}\b",
            text,
        )
        or (
            "/" in text
            and re.search(rf"\b{_UNDERGRAD_TERM}\b", text)
            and re.search(rf"\b(?:{_PHD_TERM}|{_GRADUATE_TERM})\b", text)
        )
    )


def _mixed_class_year_eligibility(text: str) -> bool:
    if not re.search(rf"\b{_FRESHMAN_TERM}\b", text):
        return False
    if not re.search(rf"\b{_SECOND_YEAR_TERM}\b", text):
        return False
    return bool(
        re.search(
            rf"\b{_FRESHMAN_TERM}\b.{{0,45}}\b(?:and|or|and/or)\b.{{0,45}}"
            rf"\b{_SECOND_YEAR_TERM}\b",
            text,
        )
        or re.search(
            rf"\b{_SECOND_YEAR_TERM}\b.{{0,45}}\b(?:and|or|and/or)\b.{{0,45}}"
            rf"\b{_FRESHMAN_TERM}\b",
            text,
        )
    )


def _same_employer_reference(text: str, *, company: str) -> bool:
    if re.search(
        r"\b(?:with|at) (?:us|our company|this company|the same company|this employer)\b"
        r"|\bhere\b",
        text,
    ):
        return True
    normalized_company = _normalize_text(company)
    return bool(
        normalized_company
        and re.search(
            rf"\b(?:with|at) {re.escape(normalized_company)}\b",
            text,
        )
    )


def _graduate_level(text: str) -> str:
    if re.search(r"\bmba\b", text):
        return "mba"
    if re.search(r"\b(?:jd|juris doctor|law (?:degree|school|student))\b", text):
        return "jd"
    if re.search(rf"\b{_MASTERS_TERM}\b", text):
        return "masters"
    return "graduate"


def _allowed_degree_level(row: Mapping[str, object]) -> str:
    text = _normalize_text(
        " ".join(
            str(row.get(field) or "")
            for field in ("title", "requirements", "description")
        )
    )
    undergraduate_evidence = bool(
        re.search(rf"\b{_UNDERGRAD_TERM}\b", text)
        or re.search(rf"\b{_FRESHMAN_TERM}\b", text)
        or re.search(rf"\b{_SECOND_YEAR_TERM}\b", text)
        or re.search(r"\brising sophomores?\b", text)
    )
    return "undergraduate" if undergraduate_evidence else "unspecified"


def _structured_evidence(row: Mapping[str, object]) -> list[_Evidence]:
    evidence: list[_Evidence] = []
    for key, value in row.items():
        token = _key_token(key)
        if _preferred_key(token):
            continue
        if token in _STRUCTURED_ELIGIBILITY_KEYS:
            text = _flatten_structured(value)
            if text:
                evidence.append(_Evidence(str(key), f"{key}: {text}", "structured"))
        elif token in _STRUCTURED_CONTAINER_KEYS and isinstance(value, Mapping):
            _walk_structured(value, str(key), evidence)
    return evidence


def _walk_structured(
    value: Mapping[object, object],
    path: str,
    evidence: list[_Evidence],
) -> None:
    for key, nested in value.items():
        token = _key_token(key)
        nested_path = f"{path}.{key}"
        if _preferred_key(token):
            continue
        if token in _STRUCTURED_ELIGIBILITY_KEYS:
            text = _flatten_structured(nested)
            if text:
                evidence.append(
                    _Evidence(nested_path, f"{key}: {text}", "structured")
                )
        elif isinstance(nested, Mapping):
            _walk_structured(nested, nested_path, evidence)


def _flatten_structured(value: object) -> str:
    values: list[str] = []
    _append_structured_values(value, values)
    return "; ".join(values)


def _append_structured_values(value: object, values: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _preferred_key(_key_token(key)):
                continue
            nested_values: list[str] = []
            _append_structured_values(nested, nested_values)
            if nested_values:
                values.append(f"{key}: {'; '.join(nested_values)}")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for nested in value:
            _append_structured_values(nested, values)
    elif value is not None:
        text = str(value).strip()
        if text:
            values.append(text)


def _qualification_segments(value: object) -> list[tuple[str, bool]]:
    if not isinstance(value, str) or not value.strip():
        return []
    text = _canonicalize_terms(value)
    text = re.sub(
        r"(?i)(?<!^)(?=\b(?:preferred|desired|minimum|required|basic|mandatory)"
        r"(?:\s+(?:qualifications?|requirements?|skills?|education))?\s*:)",
        "\n",
        text,
    )
    preferred = False
    segments: list[tuple[str, bool]] = []
    for line in re.split(r"[\r\n]+", text):
        line = re.sub(r"<[^>]+>", " ", line).strip(" \t-*•")
        if not line:
            continue
        preferred_heading = _PREFERRED_HEADING_RE.match(line)
        if preferred_heading:
            preferred = True
            line = preferred_heading.group(1).strip()
        else:
            required_heading = _REQUIRED_HEADING_RE.match(line)
            if required_heading:
                preferred = False
                line = required_heading.group(1).strip()
        for segment in re.split(r"[.!?;•]+", line):
            segment = segment.strip()
            if segment:
                segments.append((segment, preferred or bool(_PREFERENCE_RE.search(segment))))
    return segments


def _preferred_key(token: str) -> bool:
    return any(word in token for word in ("preferred", "desired", "bonus", "nicetohave"))


def _key_token(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _canonicalize_terms(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    text = re.sub(r"(?i)\bph\s*\.\s*d\s*\.?", "PhD", text)
    text = re.sub(r"(?i)\bm\s*\.\s*s\s*\.?", "MS", text)
    return text


def _normalize_text(value: object) -> str:
    text = _canonicalize_terms(value).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _bounded_evidence(value: object, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."
