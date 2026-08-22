"""Bain & Company official careers-search direct source."""

from __future__ import annotations

import random
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from watcher.config import CompanyCfg
from watcher.sources.base import (
    DirectDiagnosticsMixin,
    JsonHttpResponse,
    SourceSchemaError,
    get_json_response,
    html_to_text,
    make_row,
    page_fingerprint,
    parse_records,
)
from watcher.sources.retry import (
    DEFAULT_MAX_ATTEMPTS,
    RequestRetrier,
    RetryPolicy,
)

HOST = "www.bain.com"
SEARCH_URL = f"https://{HOST}/en/api/jobsearch/keyword/get"
CAREERS_REFERER = f"https://{HOST}/careers/find-a-role/"
DEFAULT_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 1_000
_NATIVE_ID = re.compile(r"[1-9][0-9]*")
_POSITION_PATH = "/careers/find-a-role/position/"
_PROGRAM_PREFIX = "/careers/work-with-us/internships-programs/"


class BainSource(DirectDiagnosticsMixin):
    """Fully enumerate Bain's anonymous, referer-gated careers search API."""

    name = "bain"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str, Mapping[str, str]], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        if not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {DEFAULT_PAGE_SIZE}")
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        self._request_json = request_json
        self._retrier = retrier
        self.page_size = page_size
        self.max_pages = max_pages
        self.pages_requested = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def endpoint(*, page: int, results: int) -> str:
        query = urlencode(
            (
                ("start", page),
                ("results", results),
                ("filters", ""),
                ("searchValue", ""),
                ("sortValue", ""),
            )
        )
        return f"{SEARCH_URL}?{query}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.pages_requested = 0
        self._retrier.reset()
        self.last_response_metadata = {}
        expected_total: int | None = None
        raw_seen = 0
        seen_pages: set[str] = set()
        rows: list[dict] = []
        rows_by_id: dict[str, dict] = {}
        ids_by_url: dict[str, str] = {}
        duplicate_count = 0

        for page_number in range(self.max_pages):
            self.pages_requested += 1
            payload = self._fetch_page(
                self.endpoint(page=page_number, results=self.page_size)
            )
            records, total = _page(payload, page_size=self.page_size)
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise SourceSchemaError("bain totalResults changed during pagination")

            if total == 0:
                if records or page_number != 0:
                    raise SourceSchemaError("bain zero-result response was inconsistent")
                return self._finish(rows, duplicate_count)
            if not records:
                raise SourceSchemaError("bain pagination ended before totalResults")

            fingerprint = page_fingerprint(records)
            if fingerprint in seen_pages:
                raise SourceSchemaError("bain returned a repeated pagination page")
            seen_pages.add(fingerprint)
            raw_seen += len(records)
            if raw_seen > total:
                raise SourceSchemaError("bain returned more records than totalResults")
            if raw_seen < total and len(records) != self.page_size:
                raise SourceSchemaError("bain pagination ended prematurely")

            parsed = parse_records(
                records,
                lambda record: _parse_posting(record, company),
                source_name=self.name,
                company_name=company.name,
                diagnostics=self._record_parse_diagnostics,
            )
            for row in parsed:
                source_id = row["extra"]["source_requisition_id"]
                source_url = row["source_url"]
                existing = rows_by_id.get(source_id)
                if existing is not None:
                    if existing != row:
                        raise SourceSchemaError("bain returned a conflicting posting ID")
                    duplicate_count += 1
                    continue
                other_id = ids_by_url.get(source_url)
                if other_id is not None and other_id != source_id:
                    raise SourceSchemaError(
                        "bain returned one posting URL for conflicting IDs"
                    )
                rows_by_id[source_id] = row
                ids_by_url[source_url] = source_id
                rows.append(row)

            if raw_seen == total:
                return self._finish(rows, duplicate_count)
            if page_number + 1 == self.max_pages:
                raise SourceSchemaError(
                    "bain reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable Bain pagination state")

    def _fetch_page(self, url: str) -> Any:
        request = self._request_json or _get_json
        headers = {"Referer": CAREERS_REFERER}

        def attempt() -> Any:
            response = request(url, self.name, headers)
            if isinstance(response, JsonHttpResponse):
                self.last_response_metadata = dict(response.metadata)
                return response.payload
            self.last_response_metadata = {}
            return response

        return self._retrier.run(attempt)

    def _finish(self, rows: list[dict], duplicate_count: int) -> list[dict]:
        reasons = ("request_retry_recovered",) if self.retry_attempts else ()
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=duplicate_count,
            failed_request_count=self.retry_attempts,
            degraded=bool(self.retry_attempts) or None,
            reason_codes=reasons,
        )
        return rows


def _get_json(
    url: str,
    source_name: str,
    headers: Mapping[str, str],
) -> JsonHttpResponse:
    return get_json_response(url, source_name, request_headers=headers)


def _page(payload: Any, *, page_size: int) -> tuple[list, int]:
    if not isinstance(payload, dict):
        raise SourceSchemaError("bain expected a JSON object")
    records = payload.get("results")
    total = payload.get("totalResults")
    if not isinstance(records, list):
        raise SourceSchemaError("bain expected results to be a list")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SourceSchemaError(
            "bain expected totalResults to be a nonnegative integer"
        )
    if len(records) > page_size:
        raise SourceSchemaError("bain results exceeded the requested page size")
    if total == 0 and records:
        raise SourceSchemaError("bain zero totalResults included posting records")
    return records, total


def _parse_posting(posting: Any, company: CompanyCfg) -> dict:
    if not isinstance(posting, dict):
        raise SourceSchemaError("bain expected each posting to be an object")
    native_id = str(posting.get("JobId") or "").strip()
    title = str(posting.get("JobTitle") or "").strip()
    if not _NATIVE_ID.fullmatch(native_id) or not title:
        raise SourceSchemaError("bain posting missing a numeric JobId or title")
    source_url = _posting_url(posting.get("Link"), native_id=native_id)
    source_id = f"bain:{native_id}"
    return make_row(
        source="direct",
        source_adapter="bain",
        company=company.name,
        title=title,
        location=_locations(posting.get("Location")),
        description=html_to_text(posting.get("JobDescription")),
        source_url=source_url,
        internship_type=str(posting.get("EmployeeType") or "").strip(),
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "bain",
            "source_scope": HOST,
            "bain_native_id": native_id,
            "categories": _text_values(posting.get("Categories")),
            "active": True,
        },
    )


def _posting_url(value: Any, *, native_id: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(urljoin(f"https://{HOST}/", raw))
        port = parsed.port
    except ValueError as exc:
        raise SourceSchemaError("bain posting URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != HOST
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.fragment
    ):
        raise SourceSchemaError("bain posting URL is not on the official host")

    path = parsed.path
    if path.rstrip("/") == _POSITION_PATH.rstrip("/"):
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query != {"jobid": [native_id]}:
            raise SourceSchemaError("bain position URL does not match the posting ID")
    elif path.startswith(_PROGRAM_PREFIX):
        slug = path[len(_PROGRAM_PREFIX) :].strip("/")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or parsed.query:
            raise SourceSchemaError("bain program URL is not posting-specific")
    else:
        raise SourceSchemaError("bain URL is not a posting detail or program page")
    return urlunsplit(("https", HOST, path, parsed.query, ""))


def _locations(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, list):
        raise SourceSchemaError("bain expected Location to be a list")
    return "; ".join(_text_values(value))


def _text_values(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise SourceSchemaError("bain expected list metadata")
    values: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise SourceSchemaError("bain expected list metadata to contain strings")
        text = item.strip()
        folded = text.casefold()
        if text and folded not in seen:
            values.append(text)
            seen.add(folded)
    return values
