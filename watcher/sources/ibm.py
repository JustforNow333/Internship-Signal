"""IBM official careers search-index direct source."""

from __future__ import annotations

import random
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from watcher.config import CompanyCfg
from watcher.sources.base import (
    DirectDiagnosticsMixin,
    JsonHttpResponse,
    SourceFetchError,
    SourceSchemaError,
    get_json_response,
    html_to_text,
    make_row,
    page_fingerprint,
    parse_records,
)

API_HOST = "www-api.ibm.com"
CAREERS_HOST = "careers.ibm.com"
SEARCH_URL = (
    f"https://{API_HOST}/search/api/v1-1/ibmcom/appid/"
    "careers/responseFormat/json"
)
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_SNAPSHOT_PASSES = 3
_NATIVE_ID = re.compile(r"[1-9][0-9]*")
_INDEX_ID = re.compile(r"[0-9a-f]{64}")
_POSTING_PATH = "/careers/JobDetail"
_MAPPED_ATTRIBUTES = frozenset(
    {
        "country",
        "effectivedate",
        "field_keyword_05",
        "field_keyword_08",
        "field_keyword_17",
        "field_keyword_18",
        "field_keyword_19",
        "field_text_01",
    }
)


class _IbmSnapshotUnstable(SourceSchemaError):
    """A complete IBM snapshot was not possible during this pass."""


@dataclass(frozen=True)
class _IbmSnapshot:
    rows: tuple[dict, ...]
    page_membership: tuple[str, ...]
    total_documents: int
    duplicate_count: int
    malformed_rows: int
    schema_error_rows: int
    reason_codes: tuple[str, ...]


class IbmSource(DirectDiagnosticsMixin):
    """Fully enumerate IBM's anonymous public careers search index."""

    name = "ibm"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_snapshot_passes: int = DEFAULT_MAX_SNAPSHOT_PASSES,
    ) -> None:
        if not 1 <= max_attempts <= DEFAULT_MAX_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be between 1 and {DEFAULT_MAX_ATTEMPTS}"
            )
        if not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {DEFAULT_PAGE_SIZE}")
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        if not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES:
            raise ValueError(
                "max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        self._request_json = request_json
        self._sleeper = sleeper
        self._jitter = jitter
        self._max_attempts = max_attempts
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_snapshot_passes = max_snapshot_passes
        self.pages_requested = 0
        self.documents_seen = 0
        self.snapshot_passes_requested = 0
        self.request_attempts = 0
        self.retry_attempts = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint(*, start: int, results: int, page: int) -> str:
        query = urlencode(
            (
                ("scope", "careers2"),
                ("rmdt", "ALL"),
                ("appid", "careers"),
                ("sortby", "url"),
                ("fr", start),
                ("nr", results),
                ("page", page),
                ("query", ""),
            )
        )
        return f"{SEARCH_URL}?{query}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.pages_requested = 0
        self.documents_seen = 0
        self.snapshot_passes_requested = 0
        self.request_attempts = 0
        self.retry_attempts = 0
        self.last_response_metadata = {}
        previous_snapshot: _IbmSnapshot | None = None
        for pass_number in range(1, self.max_snapshot_passes + 1):
            self.snapshot_passes_requested += 1
            try:
                snapshot = self._fetch_snapshot(company)
            except _IbmSnapshotUnstable:
                previous_snapshot = None
                if pass_number == self.max_snapshot_passes:
                    break
                continue
            if previous_snapshot is not None and snapshot == previous_snapshot:
                self._record_parse_diagnostics(
                    snapshot.malformed_rows,
                    snapshot.schema_error_rows,
                    snapshot.reason_codes,
                )
                return self._finish(list(snapshot.rows), snapshot.duplicate_count)
            previous_snapshot = snapshot

        raise SourceSchemaError(
            "ibm snapshot did not stabilize within the bounded pass limit"
        )

    def _fetch_snapshot(self, company: CompanyCfg) -> _IbmSnapshot:
        expected_total: int | None = None
        seen_pages: set[str] = set()
        page_membership: list[str] = []
        rows: list[dict] = []
        rows_by_job_id: dict[str, dict] = {}
        job_id_by_document_id: dict[str, str] = {}
        duplicate_count = 0
        raw_seen = 0
        ordered_urls: list[str] = []
        malformed_rows = 0
        schema_error_rows = 0
        reason_codes: list[str] = []

        def record_parse_diagnostics(
            malformed: int,
            schema_errors: int,
            reasons: Iterable[str],
        ) -> None:
            nonlocal malformed_rows, schema_error_rows
            malformed_rows += max(0, int(malformed))
            schema_error_rows += max(0, int(schema_errors))
            for reason in reasons:
                if reason not in reason_codes:
                    reason_codes.append(reason)

        for page_number in range(1, self.max_pages + 1):
            start = (page_number - 1) * self.page_size
            self.pages_requested += 1
            payload = self._fetch_page(
                self.endpoint(
                    start=start,
                    results=self.page_size,
                    page=page_number,
                )
            )
            documents, total = _page(
                payload,
                expected_start=start,
                page_size=self.page_size,
            )
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise _IbmSnapshotUnstable(
                    "ibm totalresults changed during pagination"
                )

            if total == 0:
                if documents or page_number != 1:
                    raise SourceSchemaError("ibm zero-result response was inconsistent")
                return _IbmSnapshot(
                    rows=(),
                    page_membership=(_page_membership_fingerprint(documents),),
                    total_documents=0,
                    duplicate_count=0,
                    malformed_rows=0,
                    schema_error_rows=0,
                    reason_codes=(),
                )
            if not documents:
                raise _IbmSnapshotUnstable(
                    "ibm pagination ended before totalresults"
                )

            fingerprint = _page_membership_fingerprint(documents)
            if fingerprint in seen_pages:
                raise _IbmSnapshotUnstable(
                    "ibm returned a repeated pagination page"
                )
            seen_pages.add(fingerprint)
            page_membership.append(fingerprint)
            self.documents_seen += len(documents)
            raw_seen += len(documents)
            if raw_seen > total:
                raise _IbmSnapshotUnstable(
                    "ibm returned more documents than totalresults"
                )
            if raw_seen < total and len(documents) != self.page_size:
                raise _IbmSnapshotUnstable("ibm pagination ended prematurely")

            parsed = parse_records(
                documents,
                lambda document: _parse_document(document, company),
                source_name=self.name,
                company_name=company.name,
                diagnostics=record_parse_diagnostics,
            )
            for row in parsed:
                ordered_urls.append(row["source_url"])
                job_id = row["extra"]["source_requisition_id"]
                document_id = row["extra"]["ibm_index_document_id"]
                document_job_id = job_id_by_document_id.get(document_id)
                if document_job_id is not None and document_job_id != job_id:
                    raise SourceSchemaError(
                        "ibm index document ID identified conflicting jobs"
                    )
                existing = rows_by_job_id.get(job_id)
                if existing is not None:
                    if existing != row:
                        raise SourceSchemaError("ibm returned a conflicting jobId")
                    duplicate_count += 1
                    continue
                job_id_by_document_id[document_id] = job_id
                rows_by_job_id[job_id] = row
                rows.append(row)

            if raw_seen == total:
                if ordered_urls != sorted(ordered_urls):
                    raise SourceSchemaError(
                        "ibm response did not honor deterministic URL ordering"
                    )
                return _IbmSnapshot(
                    rows=tuple(rows),
                    page_membership=tuple(page_membership),
                    total_documents=total,
                    duplicate_count=duplicate_count,
                    malformed_rows=malformed_rows,
                    schema_error_rows=schema_error_rows,
                    reason_codes=tuple(reason_codes),
                )
            if page_number == self.max_pages:
                raise SourceSchemaError(
                    "ibm reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable IBM pagination state")

    def _fetch_page(self, url: str) -> Any:
        request = self._request_json or _get_json
        for attempt in range(1, self._max_attempts + 1):
            self.request_attempts += 1
            try:
                response = request(url, self.name)
                if isinstance(response, JsonHttpResponse):
                    self.last_response_metadata = dict(response.metadata)
                    return response.payload
                self.last_response_metadata = {}
                return response
            except SourceFetchError as exc:
                exc.attempt_count = attempt
                exc.response_metadata.update(
                    {"attempt": attempt, "max_attempts": self._max_attempts}
                )
                if not exc.retryable or attempt == self._max_attempts:
                    raise
                self.retry_attempts += 1
                self._sleeper(_retry_delay(attempt, self._jitter))
        raise AssertionError("unreachable IBM retry state")

    def _finish(self, rows: list[dict], duplicate_count: int) -> list[dict]:
        parse_loss = bool(
            self._diagnostic_malformed_rows or self._diagnostic_schema_rows
        )
        recovered = bool(self.retry_attempts) and not parse_loss
        reasons = ("request_retry_recovered",) if self.retry_attempts else ()
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=duplicate_count,
            failed_request_count=self.retry_attempts,
            degraded=True if recovered else None,
            complete=True if recovered else None,
            reason_codes=reasons,
        )
        return rows


def _get_json(url: str, source_name: str) -> JsonHttpResponse:
    return get_json_response(url, source_name)


def _retry_delay(
    attempt: int,
    jitter: Callable[[float, float], float],
) -> float:
    base = 1.0 if attempt == 1 else 3.0
    return min(5.0, base + max(0.0, float(jitter(0.0, 1.0))))


def _page(
    payload: Any,
    *,
    expected_start: int,
    page_size: int,
) -> tuple[list, int]:
    if not isinstance(payload, dict):
        raise SourceSchemaError("ibm expected a JSON object")
    resultset = payload.get("resultset")
    if not isinstance(resultset, dict):
        raise SourceSchemaError("ibm expected resultset to be an object")
    searchresults = resultset.get("searchresults")
    if not isinstance(searchresults, dict):
        raise SourceSchemaError("ibm expected searchresults to be an object")
    documents = searchresults.get("searchresultlist")
    total = _nonnegative_integer(searchresults.get("totalresults"), "totalresults")
    start = _nonnegative_integer(searchresults.get("startindex"), "startindex")
    count = _nonnegative_integer(searchresults.get("numresults"), "numresults")
    if not isinstance(documents, list):
        raise SourceSchemaError("ibm expected searchresultlist to be a list")
    if start != expected_start:
        raise SourceSchemaError(
            f"ibm returned startindex {start}; expected {expected_start}"
        )
    if count != len(documents):
        raise SourceSchemaError("ibm numresults did not match searchresultlist")
    if len(documents) > page_size:
        raise SourceSchemaError("ibm searchresultlist exceeded the requested page size")
    if total == 0 and (documents or count or start):
        raise SourceSchemaError("ibm zero-result response was inconsistent")
    return documents, total


def _parse_document(document: Any, company: CompanyCfg) -> dict:
    if not isinstance(document, dict):
        raise SourceSchemaError("ibm expected each search document to be an object")
    title = document.get("title")
    document_id = document.get("id")
    if not isinstance(title, str) or not title.strip():
        raise SourceSchemaError("ibm search document is missing a title")
    if not isinstance(document_id, str) or not _INDEX_ID.fullmatch(document_id):
        raise SourceSchemaError("ibm search document has an invalid index ID")
    attributes = _attributes(document.get("docattributes"))
    native_id = _required_attribute(attributes, "field_text_01")
    if not _NATIVE_ID.fullmatch(native_id):
        raise SourceSchemaError("ibm search document has an invalid numeric jobId")
    source_url = _posting_url(document.get("url"), native_id=native_id)
    source_id = f"ibm:{native_id}"
    description = document.get("description")

    return make_row(
        source="direct",
        source_adapter="ibm",
        company=company.name,
        title=title.strip(),
        location=_location(attributes),
        description=html_to_text(description if isinstance(description, str) else ""),
        source_url=source_url,
        date_posted=_safe_date(_optional_text(attributes, "effectivedate")),
        remote_status=_optional_text(attributes, "field_keyword_17"),
        internship_type=_optional_text(attributes, "field_keyword_18"),
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "ibm",
            "source_scope": "careers:careers2",
            "ibm_native_id": native_id,
            "ibm_index_document_id": document_id,
            "ibm_index_document_fingerprint": _document_fingerprint(document),
            "country_code": _optional_text(attributes, "country").casefold(),
            "team": _optional_text(attributes, "field_keyword_08"),
            "active": True,
        },
    )


def _document_fingerprint(document: dict) -> str:
    """Hash one payload-free document shape, ignoring only its page position."""

    stable_document = {
        key: value for key, value in document.items() if key != "resultnum"
    }
    return page_fingerprint([stable_document])


def _page_membership_fingerprint(documents: list) -> str:
    """Hash page membership without retaining payloads or result positions."""

    fingerprints = [
        _document_fingerprint(document)
        if isinstance(document, dict)
        else page_fingerprint([document])
        for document in documents
    ]
    return page_fingerprint(fingerprints)


def _attributes(value: Any) -> dict[str, object]:
    if not isinstance(value, list):
        raise SourceSchemaError("ibm expected docattributes to be a list")
    attributes: dict[str, object] = {}
    for item in value:
        if not isinstance(item, dict):
            raise SourceSchemaError("ibm docattributes entries must be objects")
        for key, item_value in item.items():
            if key not in _MAPPED_ATTRIBUTES:
                continue
            if key in attributes and attributes[key] != item_value:
                raise SourceSchemaError(
                    f"ibm docattributes contained conflicting {key} values"
                )
            attributes[key] = item_value
    return attributes


def _required_attribute(attributes: dict[str, object], name: str) -> str:
    value = attributes.get(name)
    if not isinstance(value, str) or not value.strip():
        raise SourceSchemaError(f"ibm search document is missing {name}")
    return value.strip()


def _optional_text(attributes: dict[str, object], name: str) -> str:
    value = attributes.get(name)
    return value.strip() if isinstance(value, str) else ""


def _location(attributes: dict[str, object]) -> str:
    values: list[str] = []
    seen: set[str] = set()
    for name in ("field_keyword_19", "field_keyword_05"):
        value = _optional_text(attributes, name)
        folded = value.casefold()
        if value and folded not in seen:
            values.append(value)
            seen.add(folded)
    return "; ".join(values)


def _safe_date(value: str) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:T.*)?$", value)
    if not match:
        return ""
    try:
        return date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return ""


def _posting_url(value: Any, *, native_id: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SourceSchemaError("ibm posting URL is invalid") from exc
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != CAREERS_HOST
        or parsed.netloc.casefold() != CAREERS_HOST
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.path != _POSTING_PATH
        or query != {"jobId": [native_id]}
        or parsed.fragment
    ):
        raise SourceSchemaError("ibm URL is not a posting-specific official job URL")
    return urlunsplit(("https", CAREERS_HOST, parsed.path, parsed.query, ""))


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceSchemaError(f"ibm expected {field} to be a nonnegative integer")
    return value
