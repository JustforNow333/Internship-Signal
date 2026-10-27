"""Apple's official careers search, enumerated as one verified ``newest`` walk.

Apple publishes one global inventory behind an anonymous CSRF-guarded search
API. A token is read from ``GET /api/v1/CSRFToken`` (returned in the
``X-Apple-CSRF-Token`` response header over an empty body) and replayed on
``POST /api/v1/search``. No cookie, credential, or browser identity is used.

``totalRecords`` is authoritative and the page size is fixed at 20, so the
exact page count is arithmetic rather than something to discover. A request
past the end answers ``totalRecords: 0`` with no results, which is
byte-identical to a genuinely empty search, so a terminal request can never
prove completion and this adapter never issues one.

A live audit on 2026-10-26 (about 6,250 postings, roughly 313 pages) found
three behaviors that the earlier crawl of two unioned sorts could not survive:

* The inventory moves while it is walked. Postings closed every few minutes,
  so the total changed partway through about one walk in three, and the
  roughly 620 requests the two-sort crawl needed almost never fit inside one
  stable window. Offset pages from before and after a change cannot be mixed.
* Apple intermittently answers an in-range page with that same zero-total
  empty envelope; one ``locationAsc`` walk received 65 of them in 313 pages.
  The old crawl read each one as the total changing.
* Tied sort keys reorder between requests. ``locationAsc`` ties on whole
  locations and repeated 331 rows in one walk while adding nothing ``newest``
  lacked, so it is no longer crawled. ``newest`` ties only on evergreen
  ``PIPE-*`` rows, whose ``postDateInGMT`` is regenerated per request, and on
  requisition families posted at one identical timestamp: a walk occasionally
  repeats one such posting across a page boundary and displaces another.

Completeness therefore rests on one ``newest`` walk in which every response,
including repair re-reads, reports one unchanged total, and in which the
unique posting ids reach that total exactly. A repeated id marks a boundary
where a tie reordered; the pages around it are re-read under a bounded budget,
and the walk completes only once the missing ids appear. Each transient empty
envelope is retried a bounded number of times and is never accepted, because
an empty Apple inventory cannot be told apart from that anomaly. A changed
total, or a repair that cannot close the gap, discards the walk and starts a
fresh one from page one, at most ``MAX_CRAWL_PASSES`` times. Rows are only ever
published from one accepted walk; nothing is inferred, back-filled, or carried
over from an abandoned one.

Repeated ids are collapsed only after their invariant fields agree.
``postDateInGMT`` (and the ``postingDate`` rendered from it) is request-time
metadata on ``PIPE-*`` rows, so it is excluded from identity comparison and is
never published as a posting date.

A narrower official slice was measured and rejected: the Students team filter
held 225 postings but only 110 of 153 intern-titled postings and 4 of 25
new-graduate titles, so it cannot stand in for the whole inventory.

This is deliberately Apple-specific. It is not a generic recovery facility.
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
from watcher.sources.retry import (
    DEFAULT_MAX_ATTEMPTS,
    RequestRetrier,
    RetryPolicy,
    retry_delay,
)
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

# The one official sort whose ties are rare enough to repair. It is offered by
# Apple's own search UI.
SORT_NEWEST = "newest"
CRAWL_SORT = SORT_NEWEST

LOCALE = "en-us"
# Ceiling for one collection, sized well above Apple's observed inventory so a
# malformed or hostile total can never drive an unbounded page walk.
MAX_TOTAL_RECORDS = 50_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_FIELD_LENGTH = 20_000
_MAX_CSRF_TOKEN_LENGTH = 256
_CSRF_BODY_LIMIT = 4_096

# Fresh walks one collection may start. A walk takes about a minute; live
# measurement saw the total move in roughly one walk in three.
MAX_CRAWL_PASSES = 3
# Extra requests for one page that keeps answering with the empty envelope.
MAX_EMPTY_RETRIES_PER_PAGE = 2
# Empty-envelope retries one collection may spend across every page and walk.
MAX_EMPTY_RETRIES = 12
# Re-reads one walk may spend closing gaps left by tied sort keys, and the
# rounds they are spread over. Live repairs needed 4 to 24 reads.
MAX_REPAIR_READS = 40
MAX_REPAIR_ROUNDS = 4

# Fields that must agree when the same posting id is returned twice.
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
    crawl_sort: str = CRAWL_SORT
    passes_attempted: int = 0
    restarts: int = 0
    empty_retries: int = 0
    walk_rows: int = 0
    walk_unique_ids: int = 0
    walk_duplicate_ids: int = 0
    repair_reads: int = 0
    repaired_ids: int = 0
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
class _Walk:
    """One accepted walk: its total, its counts, and its postings in order."""

    total: int
    walk_rows: int
    walk_unique_ids: int
    repair_reads: int
    postings: tuple[_Posting, ...]


class _WalkUnstable(Exception):
    """The current walk cannot prove completeness; a fresh walk may."""


class AppleSource(DirectDiagnosticsMixin):
    """Collect Apple's whole inventory from one verified ``newest`` walk."""

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
        self._sleeper = sleeper
        self._jitter = jitter
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
            crawl_sort=CRAWL_SORT,
            passes_attempted=self._passes_attempted,
            restarts=max(0, self._passes_attempted - 1),
            empty_retries=self._empty_retries,
            walk_rows=self._walk_rows,
            walk_unique_ids=self._walk_unique_ids,
            walk_duplicate_ids=self._walk_rows - self._walk_unique_ids,
            repair_reads=self._repair_reads,
            repaired_ids=self._retained_rows - self._walk_unique_ids
            if self._retained_rows
            else 0,
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

        walk: _Walk | None = None
        reason = ""
        while walk is None:
            if self._passes_attempted >= MAX_CRAWL_PASSES:
                raise SourceSchemaError(
                    f"apple did not complete within {MAX_CRAWL_PASSES} "
                    f"bounded walks: {reason}"
                )
            self._passes_attempted += 1
            try:
                walk = self._walk()
            except _WalkUnstable as exc:
                # Pages from an abandoned walk are never mixed into a later one.
                reason = str(exc)

        self._authoritative_total = walk.total
        self._walk_rows = walk.walk_rows
        self._walk_unique_ids = walk.walk_unique_ids
        self._repair_reads = walk.repair_reads
        rows = [self._row(posting, company) for posting in walk.postings]
        self._retained_rows = len(rows)

        restarted = self._passes_attempted > 1
        retried = bool(self.retry_attempts or self._empty_retries)
        reason_codes = []
        if retried:
            reason_codes.append("request_retry_recovered")
        if restarted:
            reason_codes.append("pagination_restart_recovered")
        recovered = retried or restarted
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=walk.walk_rows - walk.walk_unique_ids,
            failed_request_count=self.retry_attempts + self._empty_retries,
            degraded=True if recovered else None,
            complete=True if recovered else None,
            reason_codes=reason_codes,
        )
        return rows

    def _walk(self) -> _Walk:
        """Walk ``newest`` once, repair tie gaps, and prove the unique count."""

        total, first_page = self._search(page=1)
        if total > MAX_TOTAL_RECORDS:
            raise SourceSchemaError("apple total exceeds the collection safeguard")
        pages = math.ceil(total / PAGE_SIZE)

        by_id: dict[str, _Posting] = {}
        first_seen_on: dict[str, int] = {}
        repeats: list[tuple[int, int]] = []
        walk_rows = 0
        records = first_page
        for number in range(1, pages + 1):
            if number > 1:
                records = self._page_under(total, number)
            walk_rows += self._absorb(
                number, pages, total, records, by_id, first_seen_on, repeats
            )
        walk_unique_ids = len(by_id)

        repair_reads = 0
        rounds = 0
        while len(by_id) < total and repeats:
            if rounds >= MAX_REPAIR_ROUNDS or repair_reads >= MAX_REPAIR_READS:
                break
            rounds += 1
            for number in _repair_targets(repeats, pages):
                if repair_reads >= MAX_REPAIR_READS or len(by_id) >= total:
                    break
                repair_reads += 1
                records = self._page_under(total, number)
                self._absorb(number, pages, total, records, by_id, first_seen_on, [])

        if len(by_id) > total:
            raise _WalkUnstable(
                f"apple listed more postings than its total of {total}"
            )
        if len(by_id) < total:
            raise _WalkUnstable(
                f"apple walk recovered {len(by_id)} of {total} advertised postings"
            )
        return _Walk(
            total=total,
            walk_rows=walk_rows,
            walk_unique_ids=walk_unique_ids,
            repair_reads=repair_reads,
            postings=tuple(by_id.values()),
        )

    def _page_under(self, total: int, number: int) -> list:
        """Return one page's records, requiring the walk's unchanged total."""

        page_total, records = self._search(page=number)
        if page_total != total:
            raise _WalkUnstable("apple total changed during the walk")
        return records

    def _absorb(
        self,
        number: int,
        pages: int,
        total: int,
        records: list,
        by_id: dict[str, _Posting],
        first_seen_on: dict[str, int],
        repeats: list[tuple[int, int]],
    ) -> int:
        """Validate one page's length against the arithmetic and keep its rows.

        Every page but the last must be exactly full. A short or rowless page
        before the end is truncation, never a terminal signal. A repeated id is
        collapsed only when its invariant fields agree, and its boundary is
        remembered so the pages around it can be re-read.
        """

        expected = PAGE_SIZE if number < pages else total - PAGE_SIZE * (pages - 1)
        if len(records) != expected:
            raise SourceSchemaError(
                f"apple {CRAWL_SORT} page {number} returned {len(records)} of "
                f"{expected} expected records"
            )
        for record in records:
            posting = _posting(record)
            existing = by_id.get(posting.posting_id)
            if existing is None:
                by_id[posting.posting_id] = posting
                first_seen_on[posting.posting_id] = number
                continue
            if existing.invariants != posting.invariants:
                raise SourceSchemaError(
                    "apple returned conflicting records for one posting id"
                )
            repeats.append((first_seen_on[posting.posting_id], number))
        return len(records)

    # --- requests ---------------------------------------------------------

    def _search(self, *, page: int) -> tuple[int, list]:
        """Return one page, retrying Apple's transient empty envelope.

        Apple answers past the end, and sometimes for an in-range page, with
        ``totalRecords: 0`` and no results. Apple's inventory is never empty,
        and the two cases are byte-identical, so the envelope is retried a
        bounded number of times and never accepted as a result.
        """

        for attempt in range(1, MAX_EMPTY_RETRIES_PER_PAGE + 2):
            payload = self._request(self.request_body(page=page, sort=CRAWL_SORT))
            total, records = _search_payload(payload)
            if total or records:
                if not total:
                    raise SourceSchemaError(
                        "apple reported an empty search while returning records"
                    )
                return total, records
            if (
                attempt > MAX_EMPTY_RETRIES_PER_PAGE
                or self._empty_retries >= MAX_EMPTY_RETRIES
            ):
                break
            self._empty_retries += 1
            self._sleeper(retry_delay(attempt, self._jitter))
        raise SourceSchemaError(
            f"apple answered page {page} with an empty result where postings "
            "were expected"
        )

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
        self._passes_attempted = 0
        self._empty_retries = 0
        self._walk_rows = 0
        self._walk_unique_ids = 0
        self._repair_reads = 0
        self._retained_rows = 0
        self.last_response_metadata = {}


# --- provider-local payload decoding ---------------------------------------


def _repair_targets(repeats: Iterable[tuple[int, int]], pages: int) -> list[int]:
    """Return the pages around each repeated id, in page order.

    A tie that repeats a posting on page ``b`` after page ``a`` displaced some
    other posting near that boundary, so both pages and their outer neighbors
    are re-read.
    """

    targets: set[int] = set()
    for first, later in repeats:
        for number in range(max(1, first - 1), min(pages, later + 1) + 1):
            targets.add(number)
    return sorted(targets)


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
