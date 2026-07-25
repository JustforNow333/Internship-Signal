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
    path = parts.path.rstrip("/")
    path_job_id = path.rsplit("/", 1)[-1]
    query = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query)
        if not k.lower().startswith("utm_")
        and k.lower() not in {"ref", "gh_src"}
        and not (k.lower() == "gh_jid" and v == path_job_id)
    )
    host = parts.netloc.lower()
    if host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def canonical_key(row: dict) -> str:
    return "|".join([norm_company(row.get("company", "")), norm_title(row.get("title", "")), norm_location(row.get("location", ""))])


def job_id(row: dict) -> str:
    """Stable id derived from content, so shortlists survive re-ingestion."""
    return hashlib.sha1(canonical_key(row).encode("utf-8")).hexdigest()[:10]


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

    ordered_rows = list(rows)
    if any(_has_source_metadata(row) for row in ordered_rows):
        ordered_rows.sort(key=_source_row_sort_key)

    for row in ordered_rows:
        _ensure_source_provenance(row)
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
        _merge_source_provenance(existing, row)
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


def _has_source_metadata(row: dict) -> bool:
    # Keyed off `source_adapter`, which every adapter row carries via
    # `make_row`. Plain `source` is not enough: a CSV column literally named
    # "source" collides with the source_url alias and lands in `extra`, and
    # user data must never drive dedupe ordering or provenance.
    extra = row.get("extra")
    return isinstance(extra, dict) and bool(
        extra.get("source_adapter")
        or extra.get("source_name")
        or extra.get("source_format")
        or extra.get("source_priority") is not None
    )


def _source_row_sort_key(row: dict) -> tuple:
    extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
    identity = _source_identity(extra)
    return (
        _source_priority(extra),
        identity.casefold(),
        norm_url(row.get("source_url", "")),
        canonical_key(row),
        str(row.get("_row_number") or ""),
    )


def _source_priority(extra: dict) -> int:
    try:
        return int(extra.get("source_priority"))
    except (TypeError, ValueError):
        pass
    if extra.get("source") == "direct":
        return 0
    source_format = str(extra.get("source_format") or "")
    if source_format == "simplify_json":
        return 10
    if source_format == "github_markdown_table":
        return 20
    if extra.get("source") == "github":
        return 50
    return 100


def _source_identity(extra: dict) -> str:
    if extra.get("source") == "direct":
        return "direct_ats"
    return str(
        extra.get("source_name")
        or extra.get("primary_source")
        or extra.get("source_adapter")
        or extra.get("source")
        or "unknown"
    ).strip()


def _ensure_source_provenance(row: dict) -> None:
    extra = row.get("extra")
    if not isinstance(extra, dict) or not _has_source_metadata(row):
        return
    identity = _source_identity(extra)
    priority = _source_priority(extra)
    if extra.get("source") == "direct" and not isinstance(extra.get("active"), bool):
        extra["active"] = True
    extra["primary_source"] = identity
    extra["sources"] = _ordered_source_names((*_source_names(extra), identity), extra)
    details = extra.get("source_details")
    if not isinstance(details, dict):
        details = {}
    details.setdefault(identity, _source_detail(extra, priority))
    extra["source_details"] = details


def _merge_source_provenance(existing: dict, duplicate: dict) -> None:
    existing_extra = existing.get("extra")
    duplicate_extra = duplicate.get("extra")
    if not isinstance(existing_extra, dict) or not isinstance(duplicate_extra, dict):
        return
    _ensure_source_provenance(existing)
    _ensure_source_provenance(duplicate)

    combined_details = {}
    for source_extra in (existing_extra, duplicate_extra):
        details = source_extra.get("source_details")
        if not isinstance(details, dict):
            continue
        for name, detail in details.items():
            if name not in combined_details:
                combined_details[name] = dict(detail) if isinstance(detail, dict) else {}
            elif isinstance(detail, dict):
                for key, value in detail.items():
                    if key not in combined_details[name] or combined_details[name][key] in ("", None):
                        combined_details[name][key] = value

    sources = _ordered_source_names(
        (*_source_names(existing_extra), *_source_names(duplicate_extra)),
        existing_extra,
        duplicate_extra,
        details=combined_details,
    )
    existing_extra["sources"] = sources
    existing_extra["source_details"] = combined_details
    if sources:
        existing_extra["primary_source"] = sources[0]

    if not existing_extra.get("source_added_date") and duplicate_extra.get("source_added_date"):
        existing_extra["source_added_date"] = duplicate_extra["source_added_date"]
    for flag in ("no_sponsorship", "us_citizenship_required"):
        if duplicate_extra.get(flag) is True:
            existing_extra[flag] = True

    primary_detail = combined_details.get(existing_extra.get("primary_source"), {})
    primary_active = primary_detail.get("active") if isinstance(primary_detail, dict) else None
    if isinstance(primary_active, bool):
        existing_extra["active"] = primary_active
        existing_extra["closed"] = not primary_active


def _source_names(extra: dict) -> tuple[str, ...]:
    values = extra.get("sources")
    if isinstance(values, (list, tuple)):
        return tuple(str(value).strip() for value in values if str(value).strip())
    identity = _source_identity(extra)
    return (identity,) if identity else ()


def _ordered_source_names(
    names,
    *extras: dict,
    details: dict | None = None,
) -> list[str]:
    unique = {str(name).strip() for name in names if str(name).strip()}
    priorities = {}
    for extra in extras:
        priorities[_source_identity(extra)] = _source_priority(extra)
    for name, detail in (details or {}).items():
        if isinstance(detail, dict):
            try:
                priorities[name] = int(detail.get("priority"))
            except (TypeError, ValueError):
                pass
    return sorted(unique, key=lambda name: (priorities.get(name, 100), name.casefold()))


def _source_detail(extra: dict, priority: int) -> dict:
    detail = {
        "priority": priority,
        "source": str(extra.get("source") or ""),
        "source_adapter": str(extra.get("source_adapter") or ""),
        "source_format": str(extra.get("source_format") or ""),
    }
    for key in (
        "feed_url",
        "source_added_date",
        "active",
        "closed",
        "no_sponsorship",
        "us_citizenship_required",
    ):
        if key in extra:
            detail[key] = extra[key]
    return detail
