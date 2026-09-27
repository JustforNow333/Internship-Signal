"""Conservative, deterministic, title-only hosted career-level classification.

The hosted catalog stores a job only when its career stage is known with high
confidence. Precision matters far more than recall here: a user who selects
"Senior+" must never receive a new-grad posting, so ambiguous and contradictory
titles are ``unknown`` rather than forced into a stage.

Only the posting title is read, plus the existing internship semantics of
``watcher.filters.is_internship`` (title and ``internship_type``) so the
internship catalog admitted before this module existed keeps its meaning.
Descriptions, requirements, years-of-experience text, and other ATS metadata
are deliberately ignored: they are missing for whole source classes and use
"junior", "senior", and "graduate" for class years and degrees.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping

from watcher.filters import NEW_GRAD_RE, STRONG_INTERNSHIP_RE, is_internship

INTERNSHIP = "internship"
NEW_GRAD_JUNIOR = "new_grad_junior"
MID_LEVEL = "mid_level"
SENIOR_PLUS = "senior_plus"
UNKNOWN = "unknown"

CAREER_LEVELS = (INTERNSHIP, NEW_GRAD_JUNIOR, MID_LEVEL, SENIOR_PLUS, UNKNOWN)
# The only stages a user may select. mid_level and unknown stay internal so an
# ambiguous posting can never be sent to anyone.
SELECTABLE_CAREER_LEVELS = (INTERNSHIP, NEW_GRAD_JUNIOR, SENIOR_PLUS)
# The stages the hosted catalog persists.
PERSISTED_CAREER_LEVELS = frozenset(SELECTABLE_CAREER_LEVELS)
DEFAULT_CAREER_LEVELS = (INTERNSHIP,)

_DASHES_RE = re.compile(r"[‐-―−]")
_WHITESPACE_RE = re.compile(r"\s+")

# A role noun that a level word must directly modify. Up to three words may
# sit between the level and the noun ("Lead Machine Learning Engineer").
_ROLE_NOUN = r"(?:engineer|developer|scientist|programmer)"
_MODIFIES_ROLE = rf"(?:\s+[a-z0-9/&+.-]+){{0,3}}?\s+{_ROLE_NOUN}\b"

_MANAGEMENT_RE = re.compile(
    r"\b(?:manager|director|head of|vp|vice president|chief|president)\b"
)
# "Member of Technical Staff" is a generic individual-contributor title and
# "Chief of Staff" is an operations role; neither is a staff-level signal.
_GENERIC_STAFF_RE = re.compile(
    r"\bchief of staff\b|\b(?:member of (?:the )?)?technical staff\b"
)
# Abbreviations must stand alone: "JR-0101608" is a requisition code.
_JR = r"\bjr\b\.?(?![-\w])"
_SR = r"\bsr\b\.?(?![-\w])"
# Some employers use "Senior Associate" and "Principal Associate" as mid-level
# rungs of an associate ladder, so neither word implies seniority there.
_LADDER_ASSOCIATE_RE = re.compile(rf"(?:\bsenior|{_SR}|\bprincipal)\s+associate\b")
_AMBIGUOUS_LEVEL_RE = re.compile(
    rf"\b{_ROLE_NOUN}\s+(?:iii|iv|v)\b"
    rf"|\b{_ROLE_NOUN}\s+i+\s*/\s*i+\b"
    r"|\bl[1-9]\b"
)
_MID_LEVEL_RE = re.compile(rf"\b{_ROLE_NOUN}\s+ii\b")
_ENTRY_LEVEL_NUMERAL_RE = re.compile(rf"\b{_ROLE_NOUN}\s+i\b")
_NEW_GRAD_PROGRAM_RE = re.compile(
    r"\bnew[- ]?(?:college[- ])?grad(?:uate)?s?\b"
    r"|\b(?:recent|university|college)[- ]grad(?:uate)?s?\b"
)
_NEW_GRAD_JUNIOR_RE = re.compile(
    r"\bearly[- ]career\b"
    r"|\bentry[- ]level\b"
    rf"|\bjunior{_MODIFIES_ROLE}"
    rf"|{_JR}"
    r"|\bgraduate engineer\b"
    r"|\b(?:campus|university) hire\b"
    rf"|(?<!senior )(?<!sr )(?<!sr\. )(?<!principal )\bassociate{_MODIFIES_ROLE}"
)
_SENIOR_PLUS_RE = re.compile(
    r"\bsenior\b"
    rf"|{_SR}"
    r"|\bstaff\b"
    r"|\bprincipal\b"
    r"|\bdistinguished\b"
    rf"|\b(?:engineering|technical) fellow\b|\bfellow{_MODIFIES_ROLE}"
    r"|\btech(?:nical)? lead\b"
    rf"|\blead{_MODIFIES_ROLE}"
)


def normalize_title(title: object) -> str:
    text = unicodedata.normalize("NFKC", str(title or ""))
    text = _DASHES_RE.sub("-", text).casefold()
    return _WHITESPACE_RE.sub(" ", text).strip()


def classify_career_level(job: Mapping[str, object]) -> str:
    """Return one of ``CAREER_LEVELS`` from the title alone.

    Evaluation order is fixed so the result is deterministic:

    1. Explicit new-grad program wording together with internship wording is
       contradictory and therefore ``unknown``.
    2. Existing internship semantics win, unless the title also carries a
       senior signal, which is contradictory.
    3. Management titles are ``unknown``; they are never ``senior_plus``.
    4. Ambiguous levels (III/IV/V, L-numbers) are ``unknown``.
    5. The remaining new-grad, mid-level, and senior signals must agree;
       any combination of different stages is ``unknown``.
    """

    title = normalize_title(job.get("title"))
    if not title:
        return UNKNOWN
    intern_wording = bool(STRONG_INTERNSHIP_RE.search(title))
    new_grad_program = bool(
        NEW_GRAD_RE.search(title) or _NEW_GRAD_PROGRAM_RE.search(title)
    )
    if intern_wording and new_grad_program:
        return UNKNOWN

    ladder_associate = bool(_LADDER_ASSOCIATE_RE.search(title))
    # Strip phrases that look like seniority but are not before the senior
    # check, so "Principal Associate" and "Member of Technical Staff" never
    # contribute a senior signal.
    seniority_text = _LADDER_ASSOCIATE_RE.sub(" ", title)
    seniority_text = _GENERIC_STAFF_RE.sub(" ", seniority_text)
    senior = bool(_SENIOR_PLUS_RE.search(seniority_text))

    if is_internship(dict(job)):
        return UNKNOWN if senior else INTERNSHIP
    if _MANAGEMENT_RE.search(title):
        return UNKNOWN
    if _AMBIGUOUS_LEVEL_RE.search(title):
        return UNKNOWN

    stages = set()
    if senior:
        stages.add(SENIOR_PLUS)
    if ladder_associate or _MID_LEVEL_RE.search(title):
        stages.add(MID_LEVEL)
    if (
        new_grad_program
        or _NEW_GRAD_JUNIOR_RE.search(title)
        or _ENTRY_LEVEL_NUMERAL_RE.search(title)
    ):
        stages.add(NEW_GRAD_JUNIOR)
    if len(stages) != 1:
        return UNKNOWN
    return stages.pop()
