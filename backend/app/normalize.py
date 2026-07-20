"""CSV normalization: header mapping, cell cleaning, field inference, dates.

Real-world job CSVs arrive with inconsistent headers ("Pay", "Comp",
"salary "), null-ish placeholder values, and missing fields that can
often be inferred from context. Everything done here is recorded so the
frontend can show an honest cleaning report.
"""

import re
from datetime import date, datetime

CANONICAL_COLUMNS = [
    "company", "title", "location", "compensation", "description",
    "requirements", "source_url", "date_posted", "deadline",
    "remote_status", "internship_type",
]

ALIASES = {
    "company": {"company", "company name", "employer", "org", "organization", "firm", "hiring company"},
    "title": {"title", "job title", "role", "position", "job", "posting title", "role title"},
    "location": {"location", "city", "loc", "job location", "office", "where"},
    "compensation": {"compensation", "comp", "pay", "salary", "stipend", "wage", "rate", "pay rate", "hourly rate", "pay range"},
    "description": {"description", "desc", "summary", "about", "job description", "details", "overview", "about the role"},
    "requirements": {"requirements", "qualifications", "quals", "skills", "required skills", "must have", "preferred qualifications", "requirements qualifications"},
    "source_url": {"source url", "url", "link", "apply link", "posting url", "application link", "href", "source", "job url"},
    "date_posted": {"date posted", "posted", "posting date", "posted on", "listed", "date listed"},
    "deadline": {"deadline", "apply by", "due", "due date", "closing date", "applications close", "apply before", "close date"},
    "remote_status": {"remote status", "remote", "work mode", "location type", "onsite remote", "workplace", "arrangement", "work arrangement"},
    "internship_type": {"internship type", "type", "term", "season", "duration", "program", "internship term"},
}

NULLISH = {"", "-", "--", "n/a", "na", "none", "null", "nil", "unknown"}


def norm_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (header or "").strip().lower()).strip()


def map_headers(headers):
    """Map raw CSV headers to canonical column names.

    Returns (mapping, report) where mapping is {original_header: canonical_or_None}
    and report describes what was mapped, left unmapped, or collided.
    """
    exact = {}
    for canon, aliases in ALIASES.items():
        for a in aliases:
            exact[a] = canon

    mapping = {}
    taken = set()
    report = {"mapped": {}, "unmapped": [], "collisions": []}

    for header in headers:
        n = norm_header(header)
        canon = exact.get(n)
        if canon is None and n and not re.search(r"\b(?:id|identifier)\b", n):
            # Substring pass: pick the longest alias contained in the header,
            # so "company name (cleaned)" -> company, "apply by date" -> deadline.
            candidates = [
                (len(alias), c)
                for c, aliases in ALIASES.items()
                for alias in aliases
                if len(alias) >= 3 and alias in n
            ]
            if candidates:
                canon = max(candidates)[1]
        if canon is None:
            mapping[header] = None
            report["unmapped"].append(header)
        elif canon in taken:
            mapping[header] = None
            report["collisions"].append({"header": header, "already_mapped_to": canon})
            report["unmapped"].append(header)
        else:
            mapping[header] = canon
            taken.add(canon)
            report["mapped"][header] = canon
    return mapping, report


def clean_cell(value, single_line: bool = False) -> str:
    if value is None:
        return ""
    s = str(value).replace("\u00a0", " ").strip()
    # Normalize fancy dashes/quotes so downstream regexes stay simple.
    s = s.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    if single_line:
        s = re.sub(r"\s+", " ", s)
    if s.lower() in NULLISH:
        return ""
    return s


SINGLE_LINE_FIELDS = {"company", "title", "location", "remote_status", "internship_type", "date_posted", "deadline"}


def build_row(raw_row: dict, mapping: dict) -> dict:
    """Build a canonical row dict from a raw csv.DictReader row."""
    row = {c: "" for c in CANONICAL_COLUMNS}
    extra = {}
    for header, value in raw_row.items():
        if header is None:
            continue
        canon = mapping.get(header)
        cleaned = clean_cell(value, single_line=(canon in SINGLE_LINE_FIELDS))
        if canon:
            row[canon] = cleaned
        elif cleaned:
            extra[header.strip()] = cleaned
    row["extra"] = extra
    return row


# ---------------------------------------------------------------------------
# Field inference
# ---------------------------------------------------------------------------

def infer_fields(row: dict) -> list:
    """Fill missing fields when the answer is clearly present elsewhere.

    Returns the list of field names that were inferred (for the report).
    """
    inferred = []
    blob = " ".join([row.get("location", ""), row.get("title", ""), row.get("description", "")]).lower()

    if not row.get("remote_status"):
        if re.search(r"\bhybrid\b", blob):
            row["remote_status"] = "Hybrid"
            inferred.append("remote_status")
        elif re.search(r"\bremote\b", blob):
            row["remote_status"] = "Remote"
            inferred.append("remote_status")
        elif re.search(r"\bon[- ]?site\b", blob):
            row["remote_status"] = "On-site"
            inferred.append("remote_status")

    if not row.get("location") and row.get("remote_status", "").lower() == "remote":
        row["location"] = "Remote"
        inferred.append("location")

    if not row.get("internship_type"):
        m = re.search(r"\b(summer|fall|spring|winter)\b", (row.get("title", "") + " " + row.get("description", "")).lower())
        if m:
            row["internship_type"] = m.group(1).capitalize()
            inferred.append("internship_type")
        elif re.search(r"\bco[- ]?op\b", blob):
            row["internship_type"] = "Co-op"
            inferred.append("internship_type")

    return inferred


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

_DATE_FORMATS = [
    "%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y",
    "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
]


def parse_date(value: str):
    """Parse a date string into a date, or None if unparseable/rolling."""
    s = clean_cell(value, single_line=True)
    if not s or re.search(r"rolling|open until filled|ongoing", s, re.I):
        return None
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)  # "June 21st" -> "June 21"
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def days_until(deadline: str, today: date):
    d = parse_date(deadline)
    if d is None:
        return None
    return (d - today).days
