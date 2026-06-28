"""Duplicate detection and merging.

Two rows are duplicates if they share a normalized source URL, or the same
normalized (company, title, location) key. Normalization strips case,
punctuation, extra whitespace, and corporate suffixes, so
"  DATADOG Inc. | software engineer intern " matches
"Datadog | Software Engineer Intern".

When duplicates collide we keep the first row and copy any fields the kept row
was missing — duplicates often disagree on which columns they bothered to fill
in. The first-row rule is intentional because watcher direct-source rows are
fed before GitHub backstop rows, preserving the direct source tag.
"""

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .normalize import CANONICAL_COLUMNS

_CORP_SUFFIX = re.compile(r"\b(inc|llc|ltd|pvt|co|corp|corporation|company|gmbh)\b\.?", re.I)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def norm_company(name: str) -> str:
    return re.sub(r"\s+", " ", _CORP_SUFFIX.sub(" ", _squash(name))).strip()


def norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", _squash(title)).strip()


def norm_location(loc: str) -> str:
    # Compare on the city token only: "New York, NY" == "new york".
    return _squash((loc or "").split(",")[0])


def norm_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url if "://" in url else "https://" + url)
    except ValueError:
        return url.lower()
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith("utm_") and k.lower() != "ref"
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def canonical_key(row: dict) -> str:
    return "|".join([norm_company(row.get("company", "")), norm_title(row.get("title", "")), norm_location(row.get("location", ""))])


def job_id(row: dict) -> str:
    """Stable id derived from content, so shortlists survive re-ingestion."""
    return hashlib.sha1(canonical_key(row).encode("utf-8")).hexdigest()[:10]


def _completeness(row: dict) -> int:
    return sum(1 for c in CANONICAL_COLUMNS if row.get(c))


def dedupe(rows):
    """Returns (unique_rows, duplicate_report_entries).

    Each report entry: {row_number, duplicate_of, matched_on, merged_fields}.
    Row numbers are 1-based positions in the cleaned input (header excluded).
    """
    kept = []
    by_key = {}
    by_url = {}
    report = []

    def index_row(row: dict) -> None:
        key = canonical_key(row)
        url = norm_url(row.get("source_url", ""))
        if key.strip("|"):
            by_key.setdefault(key, row)
        if url:
            by_url.setdefault(url, row)

    for row in rows:
        key = canonical_key(row)
        url = norm_url(row.get("source_url", ""))

        existing = None
        matched_on = None
        if url and url in by_url:
            existing, matched_on = by_url[url], "source_url"
        elif key.strip("|") and key in by_key:
            existing, matched_on = by_key[key], "company+title+location"

        if existing is None:
            kept.append(row)
            index_row(row)
            continue

        merged_fields = []
        for col in CANONICAL_COLUMNS:
            if not existing.get(col) and row.get(col):
                existing[col] = row[col]
                merged_fields.append(col)
        if merged_fields:
            index_row(existing)
        report.append({
            "row_number": row.get("_row_number"),
            "duplicate_of": existing.get("_row_number"),
            "company": row.get("company", ""),
            "title": row.get("title", ""),
            "matched_on": matched_on,
            "merged_fields": merged_fields,
        })

    return kept, report
