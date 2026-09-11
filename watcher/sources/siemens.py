"""Practical, explicitly incomplete coverage for the official Siemens job search.

Siemens publishes jobs through its own Avature portal at ``jobs.siemens.com``.
The unfiltered board reports ``999+`` results and cannot prove exhaustive
enumeration, so Siemens is deliberately never direct-complete.

A narrow slice of that portal is bounded and useful. The portal's own pagination
links address a keyword search as a path segment and page it with
``folderOffset``, and for a slice below the portal's display cap the result
legend reports an exact count instead of ``999+``. This adapter enumerates the
official ``Internship`` slice through exactly those first-party URLs.

The slice is a relevance search, not an authoritative early-career partition:
Siemens exposes an ``Experience Level`` filter only through an options API bound
to opaque per-session tokens, so it cannot be addressed by a stable contract.
This adapter therefore permanently publishes ``complete=False`` and
``degraded=True`` with ``scope_not_completeness_proven``.

Enumeration is bounded and terminating but not perfectly deterministic: the
portal can reorder items across page boundaries, so a pass may return one
posting twice and omit another. Because the exact slice total is echoed on every
page, that shortfall is detected and published as ``truncated`` with
``listing_enumeration_shortfall`` rather than being hidden.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceError, SourceSchemaError, TextHttpResponse
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.rows import make_row
from watcher.sources.transport import get_text_response

HOST = "jobs.siemens.com"
PORTAL_PATH = "/en_US/externaljobs"
SEARCH_KEYWORD = "Internship"
# The portal ignores folderRecordsPerPage and always returns six cards per page.
PAGE_SIZE = 6
MAX_PAGES = 400
SOURCE_URL = f"https://{HOST}{PORTAL_PATH}/SearchJobs/{SEARCH_KEYWORD}"

_PARTIAL_REASON = "scope_not_completeness_proven"
_SHORTFALL_REASON = "listing_enumeration_shortfall"
_SCOPE = f"{HOST}:{PORTAL_PATH}:keyword:{SEARCH_KEYWORD}"

_JOB_ID = re.compile(r"[1-9][0-9]{0,17}")
_DETAIL_PATH = re.compile(r"^%s/JobDetail/([1-9][0-9]{0,17})$" % re.escape(PORTAL_PATH))
_TOTAL_LABEL = re.compile(r"^([0-9][0-9,]*)\s+results?$", re.IGNORECASE)
_RANGE = re.compile(r"([0-9][0-9,]*)\s*-\s*([0-9][0-9,]*)\s+of\s+([0-9][0-9,]*)\s*results?", re.IGNORECASE)
_CAPPED_TOTAL = re.compile(r"\b[0-9][0-9,]*\+")
_JOB_ID_LABEL = re.compile(r"^job id:\s*([1-9][0-9]{0,17})$", re.IGNORECASE)
_EMPTY_MARKER = re.compile(r"no (?:jobs|results|matches|positions)", re.IGNORECASE)


def _int(value: str) -> int:
    return int(value.replace(",", ""))


@dataclass(frozen=True)
class _Listing:
    job_id: str
    title: str
    city: str
    state: str
    country: str
    family: str

    @property
    def location(self) -> str:
        parts = [part for part in (self.city, self.state, self.country) if part]
        return ", ".join(parts)


@dataclass(frozen=True)
class _SearchPage:
    listings: tuple[_Listing, ...]
    total: int
    first: int
    last: int
    has_next: bool
    explicit_empty: bool


class SiemensSource(DirectDiagnosticsMixin):
    """Enumerate the official Siemens Internship slice without claiming completeness."""

    name = "siemens"

    def __init__(
        self,
        *,
        request_text: Callable[[str, str], Any] | None = None,
        max_pages: int = MAX_PAGES,
    ) -> None:
        if type(max_pages) is not int or not 1 <= max_pages <= MAX_PAGES:
            raise ValueError(f"siemens max_pages must be between 1 and {MAX_PAGES}")
        self._request_text = request_text
        self.max_pages = max_pages
        self.request_count = 0
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint(*, offset: int = 0) -> str:
        if type(offset) is not int or offset < 0:
            raise ValueError("siemens offset must be a nonnegative integer")
        query = urlencode(
            {
                "listFilterMode": 1,
                "folderRecordsPerPage": PAGE_SIZE,
                "folderOffset": offset,
            }
        )
        return f"https://{HOST}{PORTAL_PATH}/SearchJobs/{SEARCH_KEYWORD}?{query}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        _require_config(company)

        rows: list[dict] = []
        seen: dict[str, int] = {}
        duplicates = 0
        expected_total: int | None = None

        offset = 0
        for _ in range(self.max_pages):
            page = _search_page(self._fetch_text(self.endpoint(offset=offset)))

            if page.explicit_empty:
                if offset or page.listings:
                    raise SourceSchemaError(
                        "siemens empty-slice response was inconsistent"
                    )
                self._publish(rows, duplicate_count=0, expected_total=0)
                return []
            if not page.listings:
                raise SourceSchemaError("siemens listing page contained no postings")

            if expected_total is None:
                expected_total = page.total
            elif page.total != expected_total:
                raise SourceSchemaError(
                    "siemens slice total changed during pagination"
                )
            if page.first != offset + 1:
                raise SourceSchemaError(
                    f"siemens returned range starting at {page.first}; expected {offset + 1}"
                )
            if page.last < page.first or page.last > expected_total:
                raise SourceSchemaError("siemens pagination range is invalid")
            if page.last - page.first + 1 != len(page.listings):
                raise SourceSchemaError(
                    "siemens pagination range disagrees with returned postings"
                )

            for listing in page.listings:
                if listing.job_id in seen:
                    # The portal can reorder items across page boundaries. Keep the
                    # first copy and count the repeat; the shortfall it implies is
                    # published below rather than hidden.
                    duplicates += 1
                    continue
                seen[listing.job_id] = len(rows)
                rows.append(_row(listing, company))

            # Advance by the server's own reported position rather than a fixed
            # stride, so a short final page cannot desynchronise the walk.
            offset = page.last

            if page.last >= expected_total:
                if page.has_next:
                    raise SourceSchemaError(
                        "siemens offered another page past the slice total"
                    )
                self._publish(
                    rows, duplicate_count=duplicates, expected_total=expected_total
                )
                return rows
            if not page.has_next:
                raise SourceSchemaError(
                    "siemens pagination ended before the slice total"
                )

        raise SourceSchemaError("siemens reached the maximum page safeguard")

    def _fetch_text(self, url: str) -> str:
        self.request_count += 1
        response = (self._request_text or _get_text)(url, self.name)
        if isinstance(response, TextHttpResponse):
            return response.text
        if not isinstance(response, str):
            raise SourceSchemaError("siemens expected an HTML text response")
        return response

    def _publish(
        self, rows: list[dict], *, duplicate_count: int, expected_total: int
    ) -> None:
        """Publish a permanently incomplete result, never a complete one."""

        shortfall = expected_total - len(rows)
        reason_codes = [_PARTIAL_REASON]
        if shortfall:
            reason_codes.append(_SHORTFALL_REASON)
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=duplicate_count,
            incomplete=True,
            truncated=bool(shortfall),
            degraded=True,
            complete=False,
            reason_codes=reason_codes,
        )


def _get_text(url: str, source_name: str) -> TextHttpResponse:
    return get_text_response(url, source_name)


def _require_config(company: CompanyCfg) -> None:
    if str(company.source_url or "").strip() != SOURCE_URL:
        raise SourceError(
            f"siemens requires the official Internship search for {company.name}"
        )


def _row(listing: _Listing, company: CompanyCfg) -> dict:
    return make_row(
        source="direct",
        source_adapter="siemens",
        company=company.name,
        title=listing.title,
        location=listing.location,
        source_url=f"https://{HOST}{PORTAL_PATH}/JobDetail/{listing.job_id}",
        extra={
            "source_id": listing.job_id,
            "source_requisition_id": listing.job_id,
            "source_system": "siemens",
            "source_scope": _SCOPE,
            "official_search_keyword": SEARCH_KEYWORD,
            "source_completeness": "practical_partial",
            "job_family": listing.family,
            "country": listing.country,
            "active": True,
        },
    )


def _search_page(html: Any) -> _SearchPage:
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError("siemens listing response was empty")
    parser = _SearchParser()
    try:
        parser.feed(html)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError("siemens malformed listing HTML") from exc
    if parser.structural_error:
        raise SourceSchemaError("siemens malformed listing HTML")

    listings = tuple(_listing(record) for record in parser.records)

    if not parser.legends:
        if listings:
            raise SourceSchemaError("siemens listing lacks a result total")
        if not _EMPTY_MARKER.search(parser.text):
            raise SourceSchemaError("siemens listing lacks a result total")
        return _SearchPage((), 0, 0, 0, False, explicit_empty=True)

    totals: set[int] = set()
    ranges: set[tuple[int, int, int]] = set()
    for label, text in parser.legends:
        if _CAPPED_TOTAL.search(label) or _CAPPED_TOTAL.search(text):
            raise SourceSchemaError(
                "siemens slice reported a capped total instead of an exact count"
            )
        match = _TOTAL_LABEL.match(label.strip())
        if not match:
            raise SourceSchemaError("siemens listing lacks an exact result total")
        totals.add(_int(match.group(1)))
        found = _RANGE.search(text)
        if found:
            ranges.add(tuple(_int(value) for value in found.groups()))
    if len(totals) != 1:
        raise SourceSchemaError("siemens listing totals conflict")
    total = totals.pop()
    if len(ranges) != 1:
        raise SourceSchemaError("siemens listing ranges conflict")
    first, last, range_total = ranges.pop()
    if range_total != total:
        raise SourceSchemaError("siemens exact result totals conflict")
    if not listings:
        raise SourceSchemaError("siemens listing page contained no postings")
    return _SearchPage(listings, total, first, last, parser.has_next, False)


def _listing(record: dict[str, str]) -> _Listing:
    job_id = record.get("href_id", "")
    labelled = record.get("job_id", "")
    title = " ".join(record.get("title", "").split())
    if not _JOB_ID.fullmatch(job_id):
        raise SourceSchemaError("siemens posting lacks a valid posting URL")
    if labelled and labelled != job_id:
        raise SourceSchemaError("siemens posting id disagrees with its posting URL")
    if not title:
        raise SourceSchemaError("siemens posting lacks a title")
    return _Listing(
        job_id=job_id,
        title=title,
        city=" ".join(record.get("city", "").split()),
        state=" ".join(record.get("state", "").split()),
        country=" ".join(record.get("country", "").split()),
        family=" ".join(record.get("family", "").split()),
    )


_FIELD_CLASSES = {
    "list-item-jobCity": "city",
    "list-item-jobState": "state",
    "list-item-jobCountry": "country",
    "list-item-family": "family",
    "list-item-jobId": "job_id",
}


class _SearchParser(HTMLParser):
    """Read only the provider-owned result cards, legend, and pagination."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict[str, str]] = []
        self.legends: list[tuple[str, str]] = []
        self.has_next = False
        self.structural_error = False
        self.text = ""
        self._record: dict[str, str] | None = None
        self._field: str | None = None
        self._title_depth = 0
        self._legend: list[str] | None = None
        self._legend_label = ""
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())

        if "article--result" in classes:
            if self._record is not None:
                self.structural_error = True
            self._record = {}
            return
        if "paginationNextLink" in classes:
            self.has_next = True
        if "list-controls__text__legend" in classes:
            self._legend = []
            self._legend_label = values.get("aria-label", "")
            return
        if self._record is not None:
            if tag == "a" and "link" in classes and "href" in values:
                match = _DETAIL_PATH.match(_path(values["href"]))
                if match and "href_id" not in self._record:
                    self._record["href_id"] = match.group(1)
                    self._title_depth = 1
                    self._field = "title"
                return
            for name, field in _FIELD_CLASSES.items():
                if name in classes:
                    self._field = field
                    return
            if self._field == "title" and self._title_depth:
                self._title_depth += 1
        elif self._field is not None and self._legend is None:
            self._field = None

    def handle_endtag(self, tag: str) -> None:
        if self._legend is not None and tag == "div":
            text = " ".join("".join(self._legend).split())
            self.legends.append((self._legend_label, text))
            self._legend = None
            self._legend_label = ""
            return
        if self._record is None:
            return
        if self._field == "title" and self._title_depth:
            self._title_depth -= 1
            if not self._title_depth:
                self._field = None
            return
        if self._field is not None and tag == "span":
            self._field = None
            return
        if tag == "article":
            record = self._record
            self._record = None
            self._field = None
            self._title_depth = 0
            if record:
                self.records.append(record)

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)
        if self._legend is not None:
            self._legend.append(data)
            return
        if self._record is None or self._field is None:
            return
        if self._field == "job_id":
            match = _JOB_ID_LABEL.match(" ".join(data.split()))
            if match:
                self._record["job_id"] = match.group(1)
            return
        self._record[self._field] = self._record.get(self._field, "") + data

    def close(self) -> None:
        super().close()
        self.text = " ".join("".join(self._chunks).split())
        if self._record is not None or self._legend is not None:
            self.structural_error = True


def _path(href: str) -> str:
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(href.strip())
    except ValueError:
        return ""
    if parsed.scheme and parsed.scheme.casefold() != "https":
        return ""
    if parsed.netloc and parsed.netloc.casefold() != HOST:
        return ""
    return parsed.path.rstrip("/")
