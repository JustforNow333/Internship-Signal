"""Duplicate detection, posting identity, and source-aware merging.

Watcher rows use the strongest reliable identity available: a stable
source-native requisition/posting ID, then a posting-specific normalized URL,
then normalized company/title/location. Plain CSV rows naturally use the latter
two levels. Similar titles alone never establish duplication.

When duplicates collide we keep the first row and copy any fields the kept row
was missing — duplicates often disagree on which columns they bothered to fill
in. The first-row rule is intentional because watcher direct-source rows are
fed before GitHub backstop rows, preserving the direct source tag.
"""

import hashlib
import re
from collections import Counter, defaultdict
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from .normalize import CANONICAL_COLUMNS

_CORP_SUFFIX = re.compile(r"\b(inc|llc|ltd|pvt|co|corp|corporation|company|gmbh)\b\.?", re.I)
_ATS_ADAPTERS = frozenset(
    {
        "ashby",
        "greenhouse",
        "lever",
        "oracle_hcm",
        "smartrecruiters",
        "talentbrew",
        "workable",
        "workday",
    }
)
_TRACKING_QUERY_KEYS = frozenset(
    {
        "gh_src",
        "lever-source",
        "ref",
        "referrer",
        "source",
        "src",
        "tracking",
        "trk",
    }
)
_POSTING_ID_QUERY_KEYS = frozenset(
    {
        "gh_jid",
        "jid",
        "job_id",
        "jobid",
        "posting_id",
        "postingid",
        "req_id",
        "reqid",
        "requisition_id",
        "requisitionid",
    }
)
_GENERIC_TERMINAL_SEGMENTS = frozenset(
    {
        "career",
        "careers",
        "early-career",
        "early-careers",
        "intern",
        "internship",
        "internships",
        "job",
        "jobs",
        "join-us",
        "openings",
        "opportunities",
        "positions",
        "results",
        "roles",
        "search",
        "students",
        "university",
    }
)
_GENERIC_PATH_MARKERS = (
    "/job-search",
    "/jobs/search",
    "/search-jobs",
    "/search-results",
)
_SEARCH_QUERY_KEYS = frozenset(
    {
        "keyword",
        "keywords",
        "location",
        "q",
        "query",
        "search",
    }
)
_FRAGMENT_POSTING_ROUTES = frozenset(
    {"job", "jobs", "position", "positions", "role", "roles"}
)


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
        and k.lower() not in _TRACKING_QUERY_KEYS
        and not (k.lower() == "gh_jid" and v == path_job_id)
    )
    host = parts.netloc.lower()
    if host == "boards.greenhouse.io":
        host = "job-boards.greenhouse.io"
    scheme = parts.scheme.lower()
    if scheme == "http" and host.endswith(
        (
            ".ashbyhq.com",
            ".greenhouse.io",
            ".lever.co",
            ".myworkdayjobs.com",
            ".smartrecruiters.com",
            ".workable.com",
        )
    ):
        scheme = "https"
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def _fragment_posting_id(url: str) -> str:
    """Extract only an explicitly recognized posting ID from a URL fragment."""

    try:
        fragment = unquote(urlsplit(str(url or "").strip()).fragment).strip()
    except (TypeError, ValueError):
        return ""
    if not fragment:
        return ""

    fragment_path, separator, fragment_query = fragment.partition("?")
    query_text = fragment_query if separator else fragment
    if query_text.startswith("?"):
        query_text = query_text[1:]
    for key, value in parse_qsl(query_text, keep_blank_values=True):
        if key.casefold() in _POSTING_ID_QUERY_KEYS and value.strip():
            return value.strip()

    path_segments = [segment.strip() for segment in fragment_path.split("/")]
    if path_segments and not path_segments[0]:
        path_segments = path_segments[1:]
    if (
        len(path_segments) == 2
        and path_segments[0].casefold() in _FRAGMENT_POSTING_ROUTES
        and path_segments[1]
    ):
        return path_segments[1]
    return ""


def _fragment_url_requisition(url: str) -> tuple[str, str, str] | None:
    native_id = _fragment_posting_id(url)
    normalized = norm_url(url)
    if not native_id or not normalized:
        return None
    try:
        parts = urlsplit(normalized)
        host = (parts.hostname or "").casefold()
        if not host:
            return None
        port = parts.port
    except (TypeError, ValueError):
        return None
    normalized_host = f"{host}:{port}" if port is not None else host
    scope_material = "\0".join(
        (normalized_host, parts.path or "/", parts.query)
    )
    scope = hashlib.sha256(scope_material.encode("utf-8")).hexdigest()
    return "url_fragment", scope, native_id


def _normalized_posting_url(url: str) -> str:
    """Normalize a URL and retain only a recognized posting fragment ID."""

    normalized = norm_url(url)
    fragment_id = _fragment_posting_id(url)
    if not normalized or not fragment_id:
        return normalized
    normalized_id = re.sub(r"\s+", "", fragment_id).casefold()
    return f"{normalized}#posting_id={quote(normalized_id, safe='')}"


def canonical_key(row: dict) -> str:
    return "|".join([norm_company(row.get("company", "")), norm_title(row.get("title", "")), norm_location(row.get("location", ""))])


def fallback_posting_key(row: dict) -> str:
    """Return the conservative company/title/location posting identity.

    This is intentionally separate from ``canonical_key`` so historical job
    IDs remain stable. Posting identity retains programming-language markers
    that materially distinguish titles and uses the complete location rather
    than the historical city-only representation.
    """

    return "|".join(
        [
            norm_company(row.get("company", "")),
            _fallback_title(row.get("title", "")),
            _fallback_location(row.get("location", "")),
        ]
    )


def _fallback_title(title: str) -> str:
    protected = re.sub(
        r"(?<![a-z0-9])c\s*\+\+(?![a-z0-9+])",
        " cplusplus ",
        str(title or ""),
        flags=re.I,
    )
    protected = re.sub(
        r"(?<![a-z0-9])c\s*#(?![a-z0-9#])",
        " csharp ",
        protected,
        flags=re.I,
    )
    return _squash(protected)


def _fallback_location(location: str) -> str:
    return _squash(location)


def job_id(row: dict) -> str:
    """Stable id derived from content, so shortlists survive re-ingestion."""
    return hashlib.sha1(canonical_key(row).encode("utf-8")).hexdigest()[:10]


def analyzed_job_ids(rows) -> list[str]:
    """Return deterministic unique IDs for canonical-identity collisions.

    The historical 10-character ID remains unchanged unless multiple retained
    postings share it. Distinct watcher postings can legitimately have the same
    historical normalized company, title, and location, so colliding rows with
    unique posting identities receive a deterministic suffix derived from the
    strongest available identity.

    If a collision group lacks unique strong identities, its legacy IDs are
    left intact so downstream structural validation continues to reject the
    ambiguous collection instead of inventing identity.
    """

    ordered_rows = list(rows)
    legacy_ids = [job_id(row) for row in ordered_rows]
    counts = Counter(legacy_ids)
    if not any(count > 1 for count in counts.values()):
        return legacy_ids

    non_specific_urls = non_specific_posting_urls(ordered_rows)
    grouped_indexes: dict[str, list[int]] = defaultdict(list)
    for index, legacy_id in enumerate(legacy_ids):
        if counts[legacy_id] > 1:
            grouped_indexes[legacy_id].append(index)

    resolved = list(legacy_ids)
    for legacy_id, indexes in grouped_indexes.items():
        identities = [
            posting_identity_key(
                ordered_rows[index],
                non_specific_urls=non_specific_urls,
            )
            for index in indexes
        ]
        if not all(identities) or len(set(identities)) != len(identities):
            continue
        for index, identity in zip(indexes, identities):
            digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            resolved[index] = f"{legacy_id}-{digest}"
    return resolved


def stable_requisition_key(row: dict) -> str:
    """Return a normalized, source-scoped stable requisition identity."""

    extra = row.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    preserved = str(extra.get("posting_requisition_key") or "").strip()
    if preserved:
        return preserved

    source_url = row.get("source_url", "")
    url_identity = _ats_url_requisition(source_url)
    fragment_identity = _fragment_url_requisition(source_url)
    source_adapter = str(extra.get("source_adapter") or "").strip().casefold()
    source_system = str(extra.get("source_system") or "").strip().casefold()
    native_id = str(extra.get("source_requisition_id") or "").strip()
    if not native_id and extra.get("source") == "direct" and source_adapter in _ATS_ADAPTERS:
        native_id = str(extra.get("source_id") or "").strip()

    if native_id:
        provider = (
            source_system
            or (url_identity[0] if url_identity else "")
            or source_adapter
            or "source_native"
        )
        if provider:
            scope = (
                url_identity[1]
                if url_identity and url_identity[0] == provider
                else _squash(str(extra.get("source_scope") or ""))
                or norm_company(row.get("company", ""))
            )
            return _requisition_key(provider, scope, native_id)

    if url_identity:
        return _requisition_key(*url_identity)
    if fragment_identity:
        return _requisition_key(*fragment_identity)
    return ""


def is_posting_specific_url(url: str) -> bool:
    """Conservatively distinguish posting URLs from landing/search URLs."""

    raw_url = url
    normalized = norm_url(raw_url)
    if not normalized:
        return False
    try:
        parts = urlsplit(normalized)
        segments = [
            unquote(segment).strip().casefold()
            for segment in parts.path.split("/")
            if unquote(segment).strip()
        ]
        query = {
            key.casefold(): value
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        }
    except (TypeError, ValueError):
        return False

    if _ats_url_requisition(normalized) or _fragment_url_requisition(raw_url):
        return True
    if any(key in _POSTING_ID_QUERY_KEYS and str(value).strip() for key, value in query.items()):
        return True

    path = "/" + "/".join(segments)
    if any(marker in path for marker in _GENERIC_PATH_MARKERS):
        return False
    if any(key in _SEARCH_QUERY_KEYS for key in query):
        return False
    if not segments or segments[-1] in _GENERIC_TERMINAL_SEGMENTS:
        return False
    if (
        any(
            segment in {"early-career", "early-careers", "internships", "students", "university"}
            for segment in segments[:-1]
        )
        and not re.search(r"\d", segments[-1])
        and segments[-2] not in {"job", "jobs", "position", "positions", "roles"}
    ):
        return False
    if len(segments) >= 2 and segments[-2] in {"job", "jobs", "position", "positions", "roles"}:
        return True
    if len(segments) >= 2:
        return True
    terminal = segments[-1]
    return bool(
        re.search(r"\d", terminal)
        or (
            "-" in terminal
            and any(token in terminal for token in ("engineer", "intern", "developer", "analyst"))
        )
    )


def non_specific_posting_urls(rows) -> frozenset[str]:
    """Return generic URLs, including URLs shared by distinct requisitions."""

    requisitions_by_url: dict[str, set[str]] = defaultdict(set)
    non_specific: set[str] = set()
    for row in rows:
        raw_url = row.get("source_url", "")
        normalized = _normalized_posting_url(raw_url)
        if not normalized:
            continue
        if not is_posting_specific_url(raw_url):
            non_specific.add(normalized)
        requisition = stable_requisition_key(row)
        if requisition:
            requisitions_by_url[normalized].add(requisition)
    non_specific.update(
        url
        for url, requisitions in requisitions_by_url.items()
        if len(requisitions) > 1
    )
    return frozenset(non_specific)


def posting_specific_url_key(
    row: dict,
    *,
    non_specific_urls: frozenset[str] = frozenset(),
) -> str:
    raw_url = row.get("source_url", "")
    normalized = _normalized_posting_url(raw_url)
    if not normalized or normalized in non_specific_urls:
        return ""
    return normalized if is_posting_specific_url(raw_url) else ""


def posting_identity_key(
    row: dict,
    *,
    non_specific_urls: frozenset[str] = frozenset(),
) -> str:
    """Return the strongest notification/dedupe identity for one posting."""

    requisition = stable_requisition_key(row)
    if requisition:
        return f"requisition|{requisition}"
    url = posting_specific_url_key(row, non_specific_urls=non_specific_urls)
    if url:
        return f"url|{url}"
    fallback = fallback_posting_key(row)
    return f"fallback|{fallback}" if fallback.strip("|") else ""


def posting_match_reason(
    first: dict,
    second: dict,
    *,
    non_specific_urls: frozenset[str] = frozenset(),
) -> str | None:
    """Return the shared identity tier, or ``None`` for distinct postings."""

    first_req = stable_requisition_key(first)
    second_req = stable_requisition_key(second)
    if first_req and second_req:
        return "requisition_id" if first_req == second_req else None

    first_url = posting_specific_url_key(first, non_specific_urls=non_specific_urls)
    second_url = posting_specific_url_key(second, non_specific_urls=non_specific_urls)
    if first_req or second_req:
        if first_url and second_url and first_url == second_url:
            return "source_url"
        return None

    if first_url and second_url:
        return "source_url" if first_url == second_url else None

    first_fallback = fallback_posting_key(first)
    second_fallback = fallback_posting_key(second)
    if (
        first_fallback.strip("|")
        and first_fallback == second_fallback
        and not (first_url and second_url)
    ):
        return "company+title+location"
    return None


def postings_match(
    first: dict,
    second: dict,
    *,
    non_specific_urls: frozenset[str] = frozenset(),
) -> bool:
    return posting_match_reason(
        first,
        second,
        non_specific_urls=non_specific_urls,
    ) is not None


def dedupe(rows, *, include_identity_diagnostics: bool = False):
    """Returns (unique_rows, duplicate_report_entries).

    Each report entry: {row_number, duplicate_of, matched_on, merged_fields}.
    Row numbers are 1-based positions in the cleaned input (header excluded).
    """
    kept = []
    by_requisition: dict[str, list[dict]] = defaultdict(list)
    by_key: dict[str, list[dict]] = defaultdict(list)
    by_url: dict[str, list[dict]] = defaultdict(list)
    report = []
    ordered_rows = list(rows)
    input_positions = {
        id(row): index
        for index, row in enumerate(ordered_rows, start=1)
    }
    non_specific_urls = non_specific_posting_urls(ordered_rows)

    def index_row(row: dict) -> None:
        requisition = stable_requisition_key(row)
        key = fallback_posting_key(row)
        url = posting_specific_url_key(row, non_specific_urls=non_specific_urls)
        if requisition:
            by_requisition[requisition].append(row)
        if key.strip("|"):
            by_key[key].append(row)
        if url:
            by_url[url].append(row)

    if any(_has_source_metadata(row) for row in ordered_rows):
        ordered_rows.sort(key=_source_row_sort_key)

    for row in ordered_rows:
        _ensure_source_provenance(row)
        requisition = stable_requisition_key(row)
        key = fallback_posting_key(row)
        url = posting_specific_url_key(row, non_specific_urls=non_specific_urls)

        existing = None
        matched_on = None
        candidates = []
        if requisition:
            candidates.extend(by_requisition.get(requisition, ()))
            if url:
                candidates.extend(by_url.get(url, ()))
        elif url:
            candidates.extend(by_url.get(url, ()))
            if key.strip("|"):
                candidates.extend(by_key.get(key, ()))
        elif key.strip("|"):
            candidates.extend(by_key.get(key, ()))

        checked: set[int] = set()
        for candidate in candidates:
            candidate_marker = id(candidate)
            if candidate_marker in checked:
                continue
            checked.add(candidate_marker)
            reason = posting_match_reason(
                candidate,
                row,
                non_specific_urls=non_specific_urls,
            )
            if reason:
                existing, matched_on = candidate, reason
                break

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
        existing_source = _source_identity(
            existing.get("extra") if isinstance(existing.get("extra"), dict) else {}
        )
        duplicate_source = _source_identity(
            row.get("extra") if isinstance(row.get("extra"), dict) else {}
        )
        report_entry = {
            "row_number": row.get("_row_number"),
            "duplicate_of": existing.get("_row_number"),
            "company": row.get("company", ""),
            "title": row.get("title", ""),
            "matched_on": matched_on,
            "merged_fields": merged_fields,
            "cross_source": existing_source != duplicate_source,
            "kept_source": existing_source,
            "duplicate_source": duplicate_source,
        }
        if include_identity_diagnostics:
            report_entry["_audit_row_index"] = input_positions[id(row)]
            report_entry["_audit_duplicate_of_index"] = input_positions[
                id(existing)
            ]
        report.append(report_entry)

    return kept, report


def _requisition_key(provider: str, scope: str, native_id: str) -> str:
    normalized_provider = _squash(provider).replace(" ", "_")
    normalized_scope = _squash(scope).replace(" ", "_") or "global"
    normalized_id = re.sub(r"\s+", "", str(native_id or "")).casefold()
    if not normalized_provider or not normalized_id:
        return ""
    return f"{normalized_provider}|{normalized_scope}|{normalized_id}"


def _ats_url_requisition(url: str) -> tuple[str, str, str] | None:
    normalized = norm_url(url)
    if not normalized:
        return None
    try:
        parts = urlsplit(normalized)
        host = (parts.hostname or "").casefold()
        segments = [unquote(segment).strip() for segment in parts.path.split("/") if segment.strip()]
        query = {
            key.casefold(): value.strip()
            for key, value in parse_qsl(parts.query)
            if value.strip()
        }
    except (TypeError, ValueError):
        return None

    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        if len(segments) >= 3 and segments[-2].casefold() == "jobs":
            return "greenhouse", segments[0], segments[-1]
        gh_jid = query.get("gh_jid")
        if gh_jid:
            return "greenhouse", segments[0] if segments else host, gh_jid
    if host == "jobs.lever.co" and len(segments) >= 2:
        candidate = segments[1]
        if candidate.casefold() != "apply":
            return "lever", segments[0], candidate
    if host == "jobs.ashbyhq.com" and len(segments) >= 2:
        return "ashby", segments[0], segments[1]
    if host == "jobs.smartrecruiters.com" and len(segments) >= 2:
        candidate = re.match(r"([A-Za-z0-9]+)", segments[1])
        if candidate:
            return "smartrecruiters", segments[0], candidate.group(1)
    if host == "apply.workable.com" and len(segments) >= 3 and segments[1].casefold() == "j":
        return "workable", segments[0], segments[2]
    if host.endswith(".myworkdayjobs.com") and segments:
        match = re.search(r"_([A-Za-z]+\d[A-Za-z0-9-]*)$", segments[-1])
        if match:
            scope = f"{host.split('.', 1)[0]}:{segments[0] if segments else ''}"
            native_id = re.sub(r"(?<=\d)-\d+$", "", match.group(1))
            return "workday", scope, native_id
    if host.endswith(".fa.oraclecloud.com"):
        lowered = [segment.casefold() for segment in segments]
        try:
            sites_index = lowered.index("sites")
        except ValueError:
            sites_index = -1
        if sites_index >= 0 and len(segments) > sites_index + 3:
            route = lowered[sites_index + 2]
            if route in {"job", "jobs", "requisition", "requisitions"}:
                site = segments[sites_index + 1]
                native_id = segments[sites_index + 3]
                if site and native_id:
                    return "oracle_hcm", f"{host}:{site}", native_id
    if host in {"careers.google.com", "www.google.com"} and "results" in {
        segment.casefold() for segment in segments
    }:
        for index, segment in enumerate(segments[:-1]):
            if segment.casefold() == "results":
                candidate = re.match(r"([A-Za-z0-9]+)", segments[index + 1])
                if candidate:
                    return "google_careers", host, candidate.group(1)
    return None


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
        "source_requisition_id",
        "source_system",
        "source_added_date",
        "active",
        "closed",
        "no_sponsorship",
        "us_citizenship_required",
    ):
        if key in extra:
            detail[key] = extra[key]
    return detail
