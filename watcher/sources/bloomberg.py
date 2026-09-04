"""Bloomberg's authoritative careers-board direct source.

Bloomberg currently publishes jobs through one company-specific Avature portal,
but this module deliberately models Bloomberg's verified HTML contract rather
than Avature as a platform. Other Avature tenants do not necessarily expose an
exact total or the same fields; notably, Lenovo's total is capped and cannot
meet the watcher's completeness requirement.

This source is listing-only by design. Bloomberg's result cards already carry
every field the pipeline gates on - stable posting id, title, location, and a
posting-specific URL - and the board exposes an exact total, so one bounded
snapshot is provably complete without opening a single posting. The detail
pages add only a description, a reference number, and two optional labels;
none of them affect role classification, eligibility, identity, or dedupe.
Fetching them cost one request per posting, made the source depend on
hand-parsing rich text that varies per posting, and drove a normal run to
roughly 433 requests, at which point the portal began answering HTTP 406.
Bloomberg publishes no cheaper authoritative alternative: the board carries no
JSON-LD or JSON search endpoint, and its RSS feed is capped at 20 items,
ignores jobOffset, and returns the reference number in place of a description.
Listing-only keeps a verified run at roughly 62 requests.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode, urlsplit, urlunsplit

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceSchemaError, TextHttpResponse
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.parsing import page_fingerprint
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import make_row
from watcher.sources.transport import get_text_response


HOST = "bloomberg.avature.net"
SEARCH_PATH = "/careers/SearchJobs"
# Result cards link to posting detail pages; the prefix identifies a posting
# link and carries the stable numeric id this source keys identity on.
DETAIL_PREFIX = "/careers/JobDetail/"
PAGE_SIZE = 12
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_SNAPSHOT_PASSES = 3
MAX_LISTING_BYTES = 2 * 1024 * 1024

_NATIVE_ID = re.compile(r"[1-9][0-9]*")
_TOTAL_LABEL = re.compile(r"^(\d+)\s+results?$", re.IGNORECASE)
_RESULT_RANGE = re.compile(
    r"^(\d+)\s*[-\u2013]\s*(\d+)\s+of\s+(\d+)\s+results?$",
    re.IGNORECASE,
)
_EMPTY_MARKER = re.compile(
    r"\bno jobs found\b.*\bcurrently no open roles matching your search\b",
    re.IGNORECASE,
)
_HTML_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)


class _BloombergSnapshotUnstable(SourceSchemaError):
    """One listing pass crossed an unstable Bloomberg board snapshot."""


@dataclass(frozen=True)
class BloombergDiagnostics:
    listing_pages_requested: int = 0
    snapshot_passes_requested: int = 0
    raw_listing_records_seen: int = 0
    stable_snapshot_rows: int = 0
    request_attempts: int = 0
    retry_attempts: int = 0


@dataclass(frozen=True)
class _Listing:
    posting_id: str
    title: str
    location: str
    url: str


@dataclass(frozen=True)
class _SearchPage:
    listings: tuple[_Listing, ...]
    first_result: int
    last_result: int
    total_results: int
    explicit_empty: bool = False

    @property
    def membership_fingerprint(self) -> str:
        return page_fingerprint(
            [
                {"posting_id": listing.posting_id, "url": listing.url}
                for listing in self.listings
            ]
        )


@dataclass(frozen=True)
class _Snapshot:
    listings: tuple[_Listing, ...]
    page_membership: tuple[str, ...]
    total_results: int


@dataclass
class _Capture:
    depth: int
    tag: str
    kind: str
    value: str
    text: list[str]


class _SearchParser(HTMLParser):
    """Extract Bloomberg's exact total and bounded result-card structure."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.structural_error = False
        self.total_labels: list[tuple[str, str]] = []
        self.records: list[dict[str, object]] = []
        self._record: dict[str, object] | None = None
        self._record_stack: list[str] = []
        self._captures: list[_Capture] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = {key.casefold(): value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        is_void = tag in _HTML_VOID_ELEMENTS
        if not is_void:
            self.depth += 1

        if tag == "article" and "article--result" in classes:
            if self._record is not None:
                self.structural_error = True
            else:
                self._record = {
                    "titles": [],
                    "locations": [],
                    "links": [],
                    "text": [],
                }
                self._record_stack = [tag]
        elif self._record is not None and not is_void:
            self._record_stack.append(tag)

        if tag == "div" and "list-controls__text__legend" in classes:
            self._captures.append(
                _Capture(self.depth, tag, "total", values.get("aria-label", ""), [])
            )
        if self._record is None:
            return
        if tag == "h3" and "article__header__text__title" in classes:
            self._captures.append(_Capture(self.depth, tag, "title", "", []))
        elif tag == "span" and "list-item-location" in classes:
            self._captures.append(_Capture(self.depth, tag, "location", "", []))
        elif tag == "a":
            href = values.get("href", "").strip()
            if DETAIL_PREFIX in href:
                links = self._record["links"]
                assert isinstance(links, list)
                links.append(href)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in _HTML_VOID_ELEMENTS:
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for capture in self._captures:
            capture.text.append(data)
        if self._record is not None:
            text = self._record["text"]
            assert isinstance(text, list)
            text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in _HTML_VOID_ELEMENTS:
            return
        completed = [
            capture
            for capture in self._captures
            if capture.depth == self.depth and capture.tag == tag
        ]
        for capture in completed:
            self._finish_capture(capture)
            self._captures.remove(capture)

        if self._record is not None:
            if not self._record_stack or self._record_stack[-1] != tag:
                self.structural_error = True
            else:
                self._record_stack.pop()
                if not self._record_stack:
                    self.records.append(self._record)
                    self._record = None
        self.depth = max(0, self.depth - 1)

    def close(self) -> None:
        super().close()
        if self._record is not None or self._record_stack or self._captures:
            self.structural_error = True

    def _finish_capture(self, capture: _Capture) -> None:
        text = _clean_text(capture.text)
        if capture.kind == "total":
            self.total_labels.append((capture.value.strip(), text))
        elif self._record is not None and capture.kind == "title":
            titles = self._record["titles"]
            assert isinstance(titles, list)
            titles.append(text)
        elif self._record is not None and capture.kind == "location":
            locations = self._record["locations"]
            assert isinstance(locations, list)
            locations.append(text)


class BloombergSource(DirectDiagnosticsMixin):
    """Enumerate one internally consistent, complete Bloomberg listing snapshot."""

    name = "bloomberg"

    def __init__(
        self,
        *,
        request_text: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_snapshot_passes: int = DEFAULT_MAX_SNAPSHOT_PASSES,
    ) -> None:
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        if not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES:
            raise ValueError(
                "max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        self._request_text = request_text
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.max_pages = max_pages
        self.max_snapshot_passes = max_snapshot_passes
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()
        self._reset_counters()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @property
    def last_diagnostics(self) -> BloombergDiagnostics:
        return BloombergDiagnostics(
            listing_pages_requested=self._listing_pages_requested,
            snapshot_passes_requested=self._snapshot_passes_requested,
            raw_listing_records_seen=self._raw_listing_records_seen,
            stable_snapshot_rows=self._stable_snapshot_rows,
            request_attempts=self.request_attempts,
            retry_attempts=self.retry_attempts,
        )

    @staticmethod
    def endpoint(offset: int = 0) -> str:
        query = urlencode(
            {"jobOffset": str(offset), "jobRecordsPerPage": str(PAGE_SIZE)}
        )
        return f"https://{HOST}{SEARCH_PATH}?{query}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self._reset_counters()
        previous: _Snapshot | None = None
        stable: _Snapshot | None = None

        for _pass_number in range(1, self.max_snapshot_passes + 1):
            self._snapshot_passes_requested += 1
            try:
                snapshot = self._fetch_snapshot()
            except _BloombergSnapshotUnstable:
                previous = None
                continue
            if previous is not None and snapshot == previous:
                stable = snapshot
                break
            previous = snapshot

        if stable is None:
            raise SourceSchemaError(
                "bloomberg snapshot did not stabilize within the bounded pass limit"
            )

        self._stable_snapshot_rows = stable.total_results
        rows = [self._row(listing, company) for listing in stable.listings]
        recovered = bool(self.retry_attempts)
        reasons = ("request_retry_recovered",) if recovered else ()
        self._finish_direct_diagnostics(
            rows,
            failed_request_count=self.retry_attempts,
            degraded=True if recovered else None,
            complete=True if recovered else None,
            reason_codes=reasons,
        )
        return rows

    def _fetch_snapshot(self) -> _Snapshot:
        expected_total: int | None = None
        offset = 0
        listings: list[_Listing] = []
        page_membership: list[str] = []
        seen_pages: set[str] = set()
        seen_ids: dict[str, str] = {}
        seen_urls: dict[str, str] = {}

        for _page_number in range(1, self.max_pages + 1):
            self._listing_pages_requested += 1
            page = _search_page(self._fetch_text(self.endpoint(offset)), offset=offset)
            self._raw_listing_records_seen += len(page.listings)

            if expected_total is None:
                expected_total = page.total_results
                if expected_total > self.max_pages * PAGE_SIZE:
                    raise SourceSchemaError(
                        "bloomberg exact total exceeds the pagination safeguard"
                    )
            elif page.total_results != expected_total:
                raise _BloombergSnapshotUnstable(
                    "bloomberg exact total changed during pagination"
                )

            if expected_total == 0:
                if offset or page.listings or not page.explicit_empty:
                    raise SourceSchemaError(
                        "bloomberg zero-result response was inconsistent"
                    )
                return _Snapshot((), (page.membership_fingerprint,), 0)

            expected_count = min(PAGE_SIZE, expected_total - offset)
            if expected_count <= 0 or len(page.listings) != expected_count:
                raise _BloombergSnapshotUnstable(
                    "bloomberg pagination returned a premature short page"
                )
            if page.first_result != offset + 1:
                raise SourceSchemaError(
                    "bloomberg result range did not start at the requested offset"
                )
            if page.last_result != offset + len(page.listings):
                raise SourceSchemaError(
                    "bloomberg result range did not match page membership"
                )

            fingerprint = page.membership_fingerprint
            if fingerprint in seen_pages:
                raise _BloombergSnapshotUnstable(
                    "bloomberg returned a repeated pagination page"
                )
            seen_pages.add(fingerprint)
            page_membership.append(fingerprint)

            for listing in page.listings:
                previous_url = seen_ids.get(listing.posting_id)
                if previous_url is not None:
                    message = "bloomberg returned a duplicate posting ID"
                    if previous_url != listing.url:
                        message += " with conflicting URLs"
                    raise SourceSchemaError(message)
                previous_id = seen_urls.get(listing.url)
                if previous_id is not None and previous_id != listing.posting_id:
                    raise SourceSchemaError(
                        "bloomberg returned one canonical URL for conflicting IDs"
                    )
                seen_ids[listing.posting_id] = listing.url
                seen_urls[listing.url] = listing.posting_id
                listings.append(listing)

            offset += len(page.listings)
            if offset == expected_total:
                return _Snapshot(
                    tuple(listings), tuple(page_membership), expected_total
                )
            if offset > expected_total:
                raise _BloombergSnapshotUnstable(
                    "bloomberg returned more jobs than its exact total"
                )

        raise SourceSchemaError(
            "bloomberg reached the maximum page safeguard before completion"
        )

    def _row(self, listing: _Listing, company: CompanyCfg) -> dict:
        """Build one canonical row from a verified listing card.

        Identity keys on Bloomberg's stable numeric Avature id, which appears in
        the posting URL and is validated as unique and conflict-free across the
        whole snapshot before any row is built.
        """

        source_id = f"bloomberg:{listing.posting_id}"
        return make_row(
            source="direct",
            source_adapter="bloomberg",
            company=company.name,
            title=listing.title,
            location=listing.location,
            source_url=listing.url,
            extra={
                "source_id": source_id,
                "source_requisition_id": source_id,
                "source_system": "bloomberg",
                "bloomberg_avature_id": listing.posting_id,
                "active": True,
            },
        )

    def _fetch_text(self, url: str) -> Any:
        request = self._request_text

        def attempt() -> Any:
            if request is not None:
                response = request(url, self.name)
            else:
                response = get_text_response(
                    url,
                    self.name,
                    max_response_bytes=MAX_LISTING_BYTES,
                )
            if isinstance(response, TextHttpResponse):
                self.last_response_metadata = dict(response.metadata)
                return response.text
            self.last_response_metadata = {}
            return response

        return self._retrier.run(attempt)

    def _reset_counters(self) -> None:
        self._listing_pages_requested = 0
        self._snapshot_passes_requested = 0
        self._raw_listing_records_seen = 0
        self._stable_snapshot_rows = 0
        self.last_response_metadata = {}


def _search_page(html: Any, *, offset: int) -> _SearchPage:
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError("bloomberg listing response was empty")
    parser = _SearchParser()
    try:
        parser.feed(html)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError("bloomberg malformed listing HTML") from exc
    if parser.structural_error:
        raise SourceSchemaError("bloomberg malformed listing HTML")

    totals: list[int] = []
    ranges: list[tuple[int, int, int]] = []
    for aria_label, text in parser.total_labels:
        total_match = _TOTAL_LABEL.fullmatch(_clean_text([aria_label]))
        if not total_match:
            raise SourceSchemaError("bloomberg listing lacks an exact result total")
        total = int(total_match.group(1))
        totals.append(total)
        if total:
            range_match = _RESULT_RANGE.fullmatch(text)
            if not range_match:
                raise SourceSchemaError("bloomberg listing lacks an exact result total")
            ranges.append(tuple(int(value) for value in range_match.groups()))
        elif text:
            raise SourceSchemaError("bloomberg zero-result range was inconsistent")
    if not totals or len(set(totals)) != 1:
        raise SourceSchemaError("bloomberg listing lacks one consistent exact result total")
    total = totals[0]
    if total and (not ranges or len(set(ranges)) != 1):
        raise SourceSchemaError("bloomberg listing lacks one consistent exact result total")

    listings: list[_Listing] = []
    empty_markers = 0
    for record in parser.records:
        links = record.get("links")
        text = _clean_text(record.get("text", []))
        if isinstance(links, list) and not links and _EMPTY_MARKER.search(text):
            empty_markers += 1
            continue
        listings.append(_listing_record(record))

    if total == 0:
        if offset != 0 or listings or empty_markers != 1:
            raise SourceSchemaError("bloomberg zero-result response was inconsistent")
        return _SearchPage((), 0, 0, 0, explicit_empty=True)
    if empty_markers:
        raise SourceSchemaError("bloomberg nonempty results contained an empty marker")
    first, last, range_total = ranges[0]
    if range_total != total:
        raise SourceSchemaError("bloomberg exact result totals conflict")
    return _SearchPage(tuple(listings), first, last, total)


def _listing_record(record: dict[str, object]) -> _Listing:
    titles = [_clean_text([value]) for value in record.get("titles", [])]
    titles = [value for value in titles if value]
    locations = [_clean_text([value]) for value in record.get("locations", [])]
    locations = [value for value in locations if value]
    links = [str(value or "").strip() for value in record.get("links", [])]
    links = [value for value in links if value]
    unique_links = list(dict.fromkeys(links))
    if len(titles) != 1 or len(locations) != 1 or not unique_links:
        raise SourceSchemaError("bloomberg listing card is missing required fields")
    if len(unique_links) != 1:
        raise SourceSchemaError("bloomberg listing card has conflicting detail links")
    url, posting_id = _canonical_posting_url(unique_links[0])
    return _Listing(posting_id, titles[0], locations[0], url)


def _canonical_posting_url(value: Any) -> tuple[str, str]:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SourceSchemaError("bloomberg posting URL is malformed") from exc
    parts = parsed.path.split("/")
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != HOST
        or parsed.netloc.casefold() != HOST
        or parsed.username
        or parsed.password
        or port is not None
        or len(parts) != 5
        or parts[:3] != ["", "careers", "JobDetail"]
        or not parts[3]
        or not _NATIVE_ID.fullmatch(parts[4])
        or parsed.query
        or parsed.fragment
    ):
        raise SourceSchemaError(
            "bloomberg URL is not a posting-specific official detail URL"
        )
    posting_id = parts[-1]
    return urlunsplit(("https", HOST, parsed.path, "", "")), posting_id


def _clean_text(values: Any) -> str:
    if isinstance(values, (str, bytes)):
        values = [values]
    try:
        return " ".join(" ".join(str(value) for value in values).split())
    except TypeError:
        return ""
