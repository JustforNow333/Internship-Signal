"""Apple's official careers search, enumerated across two verified sorts.

Apple publishes one global inventory behind an anonymous CSRF-guarded search
API. A token is read from ``GET /api/v1/CSRFToken`` (returned in the
``X-Apple-CSRF-Token`` response header over an empty body) and replayed on
``POST /api/v1/search``. No cookie, credential, or browser identity is used.

``totalRecords`` is authoritative and the page size is fixed at 20, so the
exact page count is arithmetic rather than something to discover. That matters
because a request past the end answers ``totalRecords: 0`` with no results,
which is byte-identical to a genuinely empty search: a terminal request can
never prove completion here, so this adapter never issues one.

A single offset sort is *not* complete. Apple's ordering is unstable across
requests wherever the sort key ties, so consecutive pages can repeat a row at
their boundary while silently dropping a different one, leaving the raw row
count and ``totalRecords`` both correct. A repository audit measured this
directly: ``newest`` alone recovered 6,106 of 6,107 postings and ``locationAsc``
alone recovered 5,471, while the union of exactly those two official sorts
recovered all 6,107. This module therefore crawls both sorts, validates each
independently, unions them by stable posting id, and treats the collection as
complete only when the union size equals the authoritative total. A short union
fails closed; nothing is inferred, repaired, or back-filled.

Repeated ids across sorts are expected and are collapsed, but only after their
invariant fields agree. ``postDateInGMT`` (and the ``postingDate`` rendered
from it) is request-time metadata on ``PIPE-*`` evergreen rows -- the audit saw
it change between requests for the same posting -- so it is excluded from
identity comparison and is never published as a posting date.

This is deliberately Apple-specific. It is not a generic multi-sort facility.
"""

from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    SourceFetchError,
    SourceSchemaError,
    TextHttpResponse,
)
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import make_row
from watcher.sources.sanitize import _safe_url
from watcher.sources.transport import (
    DEFAULT_TIMEOUT_SECONDS,
    USER_AGENT,
    _http_error_code,
    _network_error_code,
    post_json_response,
)

BASE_URL = "https://jobs.apple.com"
SEARCH_PATH = "/api/v1/search"
CSRF_PATH = "/api/v1/CSRFToken"
CSRF_HEADER = "X-Apple-CSRF-Token"
DETAILS_BASE = f"{BASE_URL}/en-us/details"

# Apple's search page size is fixed by the service; it is not a request field.
PAGE_SIZE = 20

# The verified minimum complete set, in crawl order. `newest` has by far the
# better single-sort recall, so it runs first and `locationAsc` supplies the
# remainder. Both are official values offered by Apple's own search UI.
SORT_NEWEST = "newest"
SORT_LOCATION_ASC = "locationAsc"
REQUIRED_SORTS: tuple[str, ...] = (SORT_NEWEST, SORT_LOCATION_ASC)

LOCALE = "en-us"
# Ceiling for one collection, sized well above Apple's observed inventory so a
# malformed or hostile total can never drive an unbounded page walk.
MAX_TOTAL_RECORDS = 50_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_FIELD_LENGTH = 20_000
_MAX_CSRF_TOKEN_LENGTH = 256
_CSRF_BODY_LIMIT = 4_096

# Fields that must agree when the same posting id is returned by both sorts.
# `postDateInGMT`/`postingDate` are excluded on purpose: they are regenerated
# per request for evergreen `PIPE-*` rows and are not posting identity.
_INVARIANT_FIELDS: tuple[str, ...] = (
    "position_id",
    "req_id",
    "title",
    "slug",
    "locations",
    "team",
)


@dataclass(frozen=True)
class AppleDiagnostics:
    """Bounded, payload-free view of one Apple collection."""

    authoritative_total: int = 0
    requests_made: int = 0
    sorts_crawled: tuple[str, ...] = ()
    raw_rows_per_sort: tuple[tuple[str, int], ...] = ()
    unique_ids_per_sort: tuple[tuple[str, int], ...] = ()
    duplicate_ids_per_sort: tuple[tuple[str, int], ...] = ()
    union_unique_ids: int = 0
    retained_rows: int = 0
    request_attempts: int = 0
    retry_attempts: int = 0


@dataclass(frozen=True)
class _Posting:
    """One search result reduced to the fields this source publishes."""

    posting_id: str
    position_id: str
    req_id: str
    title: str
    slug: str
    locations: tuple[str, ...]
    team: str
    summary: str
    home_office: bool
    weekly_hours: str

    @property
    def invariants(self) -> tuple:
        return tuple(getattr(self, field) for field in _INVARIANT_FIELDS)


@dataclass(frozen=True)
class _SortPass:
    """One complete single-sort crawl and its own raw counts."""

    sort: str
    total: int
    raw_rows: int
    postings: tuple[_Posting, ...]

    @property
    def unique_ids(self) -> int:
        return len({posting.posting_id for posting in self.postings})

    @property
    def duplicate_ids(self) -> int:
        return self.raw_rows - self.unique_ids


class AppleSource(DirectDiagnosticsMixin):
    """Collect Apple's whole inventory from its two verified official sorts."""

    name = "apple"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str, dict, dict], Any] | None = None,
        fetch_csrf_token: Callable[[], str] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._request_json = request_json
        self._fetch_csrf_token = fetch_csrf_token
        self._timeout = timeout
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()
        self._reset_counters()

    # --- introspection ----------------------------------------------------

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @property
    def last_diagnostics(self) -> AppleDiagnostics:
        return AppleDiagnostics(
            authoritative_total=self._authoritative_total,
            requests_made=self._requests_made,
            sorts_crawled=tuple(self._sorts_crawled),
            raw_rows_per_sort=tuple(self._raw_rows_per_sort),
            unique_ids_per_sort=tuple(self._unique_ids_per_sort),
            duplicate_ids_per_sort=tuple(self._duplicate_ids_per_sort),
            union_unique_ids=self._union_unique_ids,
            retained_rows=self._retained_rows,
            request_attempts=self.request_attempts,
            retry_attempts=self.retry_attempts,
        )

    @staticmethod
    def endpoint() -> str:
        return f"{BASE_URL}{SEARCH_PATH}"

    @staticmethod
    def csrf_endpoint() -> str:
        return f"{BASE_URL}{CSRF_PATH}"

    @staticmethod
    def posting_url(posting_id: str, slug: str) -> str:
        """Return the posting's canonical careers URL."""

        tail = f"/{slug}" if slug else ""
        return f"{DETAILS_BASE}/{posting_id}{tail}"

    @staticmethod
    def request_body(*, page: int, sort: str) -> dict[str, Any]:
        """Return the verified public search body for one page of one sort."""

        return {
            "query": "",
            "filters": {},
            "page": page,
            "locale": LOCALE,
            "sort": sort,
            "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"},
        }

    # --- collection -------------------------------------------------------

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self._reset_counters()
        self._token = None

        passes: list[_SortPass] = []
        authoritative_total: int | None = None
        for sort in REQUIRED_SORTS:
            sort_pass = self._crawl_sort(sort, expected_total=authoritative_total)
            if authoritative_total is None:
                authoritative_total = sort_pass.total
                self._authoritative_total = authoritative_total
            passes.append(sort_pass)
            self._sorts_crawled.append(sort)
            self._raw_rows_per_sort.append((sort, sort_pass.raw_rows))
            self._unique_ids_per_sort.append((sort, sort_pass.unique_ids))
            self._duplicate_ids_per_sort.append((sort, sort_pass.duplicate_ids))

        assert authoritative_total is not None  # REQUIRED_SORTS is non-empty
        union = self._union(passes)
        self._union_unique_ids = len(union)
        if len(union) != authoritative_total:
            raise SourceSchemaError(
                "apple union recovered "
                f"{len(union)} of {authoritative_total} advertised postings"
            )

        rows = [self._row(posting, company) for posting in union.values()]
        self._retained_rows = len(rows)
        recovered = bool(self.retry_attempts)
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=sum(item.duplicate_ids for item in passes),
            failed_request_count=self.retry_attempts,
            degraded=True if recovered else None,
            complete=True if recovered else None,
            reason_codes=("request_retry_recovered",) if recovered else (),
        )
        return rows

    def _crawl_sort(self, sort: str, *, expected_total: int | None) -> _SortPass:
        """Walk one sort completely, validating its own page arithmetic."""

        total, first_page = self._search(sort, page=1)
        if total > MAX_TOTAL_RECORDS:
            raise SourceSchemaError(
                "apple total exceeds the collection safeguard"
            )
        if expected_total is not None and total != expected_total:
            raise SourceSchemaError(
                "apple total changed between sorts during collection"
            )
        if total == 0:
            # A genuinely empty search is a valid, complete result; a page with
            # records alongside a zero total is not.
            if first_page:
                raise SourceSchemaError(
                    "apple reported an empty search while returning records"
                )
            return _SortPass(sort=sort, total=0, raw_rows=0, postings=())

        pages = math.ceil(total / PAGE_SIZE)
        postings: list[_Posting] = []
        self._absorb_page(sort, 1, pages, total, first_page, postings)
        for page in range(2, pages + 1):
            page_total, records = self._search(sort, page=page)
            if page_total != total:
                raise SourceSchemaError(
                    "apple total changed during a sort crawl"
                )
            self._absorb_page(sort, page, pages, total, records, postings)

        if len(postings) != total:
            raise SourceSchemaError(
                "apple returned a row count other than its advertised total"
            )
        return _SortPass(
            sort=sort,
            total=total,
            raw_rows=len(postings),
            postings=tuple(postings),
        )

    def _absorb_page(
        self,
        sort: str,
        page: int,
        pages: int,
        total: int,
        records: list,
        postings: list[_Posting],
    ) -> None:
        """Validate one page's length against the arithmetic and keep its rows.

        Every page but the last must be exactly full. A short or empty page
        before the end is truncation, never a terminal signal.
        """

        expected = PAGE_SIZE if page < pages else total - PAGE_SIZE * (pages - 1)
        if len(records) != expected:
            raise SourceSchemaError(
                f"apple {sort} page {page} returned {len(records)} of "
                f"{expected} expected records"
            )
        postings.extend(_posting(record) for record in records)

    def _union(self, passes: Iterable[_SortPass]) -> dict[str, _Posting]:
        """Union every sort by stable posting id, failing closed on conflict."""

        union: dict[str, _Posting] = {}
        for sort_pass in passes:
            for posting in sort_pass.postings:
                existing = union.get(posting.posting_id)
                if existing is None:
                    union[posting.posting_id] = posting
                elif existing.invariants != posting.invariants:
                    raise SourceSchemaError(
                        "apple returned conflicting records for one posting id"
                    )
        return union

    # --- requests ---------------------------------------------------------

    def _search(self, sort: str, *, page: int) -> tuple[int, list]:
        payload = self._request(self.request_body(page=page, sort=sort))
        return _search_payload(payload)

    def _request(self, body: dict) -> Any:
        self._requests_made += 1

        def attempt() -> Any:
            headers = {CSRF_HEADER: self._csrf_token()}
            if self._request_json is not None:
                response = self._request_json(
                    self.endpoint(), self.name, body, headers
                )
            else:
                response = post_json_response(
                    self.endpoint(),
                    body,
                    self.name,
                    timeout=self._timeout,
                    max_response_bytes=MAX_RESPONSE_BYTES,
                    request_headers=headers,
                )
            payload = getattr(response, "payload", None)
            if payload is None and isinstance(response, TextHttpResponse):
                payload = json.loads(response.text)
            if payload is None:
                payload = response
            metadata = getattr(response, "metadata", None)
            self.last_response_metadata = dict(metadata or {})
            return payload

        return self._retrier.run(attempt)

    def _csrf_token(self) -> str:
        if self._token is None:
            fetch = self._fetch_csrf_token or self._fetch_csrf_token_over_http
            self._token = _safe_csrf_token(fetch())
        return self._token

    def _fetch_csrf_token_over_http(self) -> str:
        """Read the anonymous CSRF token from its own response header.

        The shared transport deliberately never surfaces response headers, and
        this endpoint answers with an empty body, so the request is made here.
        Only the one documented header is read; nothing else is retained.
        """

        url = self.csrf_endpoint()
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            method="GET",
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                response.read(_CSRF_BODY_LIMIT)
                return _header_value(response.headers, CSRF_HEADER)
        except HTTPError as exc:
            code = _http_error_code(exc.code)
            raise SourceFetchError(
                f"apple CSRF request failed with HTTP {exc.code}: "
                f"{_safe_url(url)}",
                error_code=code,
                status_code=exc.code,
                retryable=code in {"rate_limited", "transient_http_error"},
            ) from exc
        except (TimeoutError, URLError, OSError) as exc:
            code = _network_error_code(exc)
            raise SourceFetchError(
                f"apple CSRF request failed: code={code} "
                f"endpoint={_safe_url(url)}",
                error_code=code,
                retryable=True,
            ) from exc

    # --- rows -------------------------------------------------------------

    def _row(self, posting: _Posting, company: CompanyCfg) -> dict:
        extra: dict[str, Any] = {
            "source_id": posting.posting_id,
            "source_requisition_id": posting.posting_id,
            "source_system": "apple",
            "apple_posting_id": posting.posting_id,
            "active": True,
        }
        if posting.position_id:
            extra["apple_position_id"] = posting.position_id
        if posting.team:
            extra["job_category"] = posting.team
        if posting.home_office:
            extra["home_office"] = True
        if posting.weekly_hours:
            extra["standard_weekly_hours"] = posting.weekly_hours
        # `date_posted` is intentionally omitted: Apple's only posting-date
        # field is regenerated per request for evergreen rows.
        return make_row(
            source="direct",
            source_adapter="apple",
            company=company.name,
            title=posting.title,
            location="; ".join(posting.locations),
            description=posting.summary,
            source_url=self.posting_url(posting.posting_id, posting.slug),
            extra=extra,
        )

    def _reset_counters(self) -> None:
        self._token: str | None = None
        self._requests_made = 0
        self._authoritative_total = 0
        self._sorts_crawled: list[str] = []
        self._raw_rows_per_sort: list[tuple[str, int]] = []
        self._unique_ids_per_sort: list[tuple[str, int]] = []
        self._duplicate_ids_per_sort: list[tuple[str, int]] = []
        self._union_unique_ids = 0
        self._retained_rows = 0
        self.last_response_metadata = {}


# --- provider-local payload decoding ---------------------------------------


def _search_payload(payload: Any) -> tuple[int, list]:
    """Return one response's authoritative total and records, failing closed."""

    if not isinstance(payload, dict):
        raise SourceSchemaError("apple response was not an object")
    result = payload.get("res")
    if not isinstance(result, dict):
        raise SourceSchemaError("apple response lacked its result object")

    total = result.get("totalRecords")
    if isinstance(total, bool) or not isinstance(total, int):
        raise SourceSchemaError("apple totalRecords was not an integer")
    if total < 0:
        raise SourceSchemaError("apple totalRecords was negative")

    records = result.get("searchResults")
    if records is None:
        records = []
    if not isinstance(records, list):
        raise SourceSchemaError("apple searchResults was not a list")
    return total, records


def _posting(record: Any) -> _Posting:
    if not isinstance(record, dict):
        raise SourceSchemaError("apple record was not an object")
    posting_id = _text(record.get("id"), "id")
    if not posting_id:
        raise SourceSchemaError("apple record lacked a posting id")
    title = _text(record.get("postingTitle"), "postingTitle")
    if not title:
        raise SourceSchemaError("apple record lacked a title")
    return _Posting(
        posting_id=posting_id,
        position_id=_text(record.get("positionId"), "positionId"),
        req_id=_text(record.get("reqId"), "reqId"),
        title=title,
        slug=_text(record.get("transformedPostingTitle"), "transformedPostingTitle"),
        locations=_locations(record.get("locations")),
        team=_team(record.get("team")),
        summary=_text(record.get("jobSummary"), "jobSummary"),
        home_office=_flag(record.get("homeOffice"), "homeOffice"),
        weekly_hours=_hours(record.get("standardWeeklyHours")),
    )


def _locations(value: Any) -> tuple[str, ...]:
    """Return the posting's concrete location labels."""

    if value is None:
        return ()
    entries = value if isinstance(value, list) else [value]
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceSchemaError("apple location entry was malformed")
        label = _text(entry.get("name"), "name")
        country = _text(entry.get("countryName"), "countryName")
        if label and country and country != label:
            label = f"{label}, {country}"
        elif not label:
            label = country
        if label and label not in out:
            out.append(label)
    return tuple(out)


def _team(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, dict):
        raise SourceSchemaError("apple team was malformed")
    return _text(value.get("teamName"), "teamName")


def _flag(value: Any, field: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise SourceSchemaError(f"apple {field} was not a boolean")
    return value


def _hours(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return _text(value, "standardWeeklyHours")


def _text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise SourceSchemaError(f"apple {field} was not a string")
    return " ".join(value.split())[:_MAX_FIELD_LENGTH]


def _safe_csrf_token(value: Any) -> str:
    """Accept only a bounded opaque token; never echo it into an error."""

    token = "" if value is None else str(value).strip()
    if not token or len(token) > _MAX_CSRF_TOKEN_LENGTH:
        raise SourceFetchError(
            "apple CSRF token was missing or malformed",
            error_code="csrf_token_unavailable",
            retryable=True,
        )
    if not all(character.isalnum() or character in "-_" for character in token):
        raise SourceFetchError(
            "apple CSRF token was missing or malformed",
            error_code="csrf_token_unavailable",
            retryable=True,
        )
    return token


def _header_value(headers: Any, name: str) -> str:
    getter = getattr(headers, "get", None)
    if getter is None:
        return ""
    return str(getter(name) or "")
