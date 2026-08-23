"""SAP SuccessFactors Career Site Builder source adapter."""

from __future__ import annotations

import math
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable, Mapping
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from watcher.config import CompanyCfg, is_valid_hostname
from watcher.sources.base import (
    DirectDiagnosticsMixin,
    SourceError,
    SourceSchemaError,
    TextHttpResponse,
    get_text_response,
    make_row,
    page_fingerprint,
    parse_records,
)
from watcher.sources.retry import (
    DEFAULT_MAX_ATTEMPTS,
    RequestRetrier,
    RetryPolicy,
)

DEFAULT_MAX_PAGES = 1_000
# A completely enumerated board can need hundreds of sequential page requests,
# so a per-page attempt limit alone does not bound how long one crawl may run.
# This budget caps retries across the whole crawl; exhausting it fails the
# crawl rather than extending it.
DEFAULT_MAX_CRAWL_RETRIES = 5
_SITE_PREFIX = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,78}[A-Za-z0-9])?"
)
_LOCALE = re.compile(r"[a-z]{2}_[A-Z]{2}")
_RESULTS = re.compile(
    r"^Results\s+(\d+)\s*[\u2013-]\s*(\d+)\s+of\s+(\d+)$",
    re.IGNORECASE,
)
_PAGE = re.compile(r"^Page\s+(\d+)\s+of\s+(\d+)$", re.IGNORECASE)
_EMPTY_BOARD = re.compile(
    r"\bthere are currently no open positions matching\b",
    re.IGNORECASE,
)
_DATE_FORMATS = (
    "%b %d, %Y",
    "%B %d, %Y",
    "%d %b %Y",
    "%d %B %Y",
    "%m/%d/%y",
    "%m/%d/%Y",
)


@dataclass(frozen=True)
class _SearchPage:
    records: tuple[Mapping[str, object], ...]
    first_result: int
    last_result: int
    total_results: int
    current_page: int
    total_pages: int
    explicit_empty: bool = False

    @property
    def fingerprint(self) -> str:
        return page_fingerprint([dict(record) for record in self.records])


@dataclass
class _Capture:
    depth: int
    tag: str
    kind: str
    value: str
    text: list[str]


class _CareerSiteParser(HTMLParser):
    """Extract only bounded listing structure from one CSB search page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.listing_contract = False
        self.structural_error = False
        self.records: list[dict[str, object]] = []
        self.result_labels: list[str] = []
        self.page_labels: list[str] = []
        self.alerts: list[str] = []
        self._record: dict[str, object] | None = None
        self._record_depth = 0
        self._captures: list[_Capture] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.depth += 1
        attributes = {key: value or "" for key, value in attrs}
        classes = set(attributes.get("class", "").split())
        if tag == "table" and "searchResults" in classes:
            self.listing_contract = True
        if tag == "tr" and "data-row" in classes:
            if self._record is not None:
                self.structural_error = True
            self._record = {"links": [], "locations": [], "dates": []}
            self._record_depth = self.depth

        if self._record is not None:
            if tag == "a" and "jobTitle-link" in classes:
                self._capture(tag, "link", attributes.get("href", ""))
            elif tag == "span" and "jobLocation" in classes:
                self._capture(tag, "location")
            elif tag == "span" and "jobDate" in classes:
                self._capture(tag, "date")
        if tag == "span" and "paginationLabel" in classes:
            self._capture(tag, "results")
        elif tag == "span" and "srHelp" in classes:
            self._capture(tag, "page")
        elif "alert" in classes:
            self._capture(tag, "alert")

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for capture in self._captures:
            capture.text.append(data)

    def handle_endtag(self, tag: str) -> None:
        completed = [
            capture
            for capture in self._captures
            if capture.depth == self.depth and capture.tag == tag
        ]
        for capture in completed:
            self._finish_capture(capture)
            self._captures.remove(capture)

        if tag == "tr" and self._record is not None and self._record_depth == self.depth:
            self.records.append(self._record)
            self._record = None
            self._record_depth = 0
        self.depth = max(0, self.depth - 1)

    def close(self) -> None:
        super().close()
        if self._record is not None or self._captures:
            self.structural_error = True

    def _capture(self, tag: str, kind: str, value: str = "") -> None:
        self._captures.append(_Capture(self.depth, tag, kind, value, []))

    def _finish_capture(self, capture: _Capture) -> None:
        text = _clean_text("".join(capture.text))
        if capture.kind == "link" and self._record is not None:
            self._record["links"].append(
                {"href": capture.value.strip(), "title": text}
            )
        elif capture.kind == "location" and self._record is not None:
            self._record["locations"].append(text)
        elif capture.kind == "date" and self._record is not None:
            self._record["dates"].append(text)
        elif capture.kind == "results":
            self.result_labels.append(text)
        elif capture.kind == "page":
            self.page_labels.append(text)
        elif capture.kind == "alert":
            self.alerts.append(text[:500])


class SuccessFactorsSource(DirectDiagnosticsMixin):
    """Collect a completely enumerable anonymous Career Site Builder board."""

    name = "successfactors"

    def __init__(
        self,
        *,
        request_text: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_crawl_retries: int = DEFAULT_MAX_CRAWL_RETRIES,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if not 1 <= max_attempts <= DEFAULT_MAX_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be between 1 and {DEFAULT_MAX_ATTEMPTS}"
            )
        if not 0 <= max_crawl_retries <= DEFAULT_MAX_CRAWL_RETRIES:
            raise ValueError(
                f"max_crawl_retries must be between 0 and {DEFAULT_MAX_CRAWL_RETRIES}"
            )
        retrier = RequestRetrier(
            policy=RetryPolicy(
                max_attempts=max_attempts,
                max_crawl_retries=max_crawl_retries,
            ),
            sleeper=sleeper,
            jitter=jitter,
        )
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        self._request_text = request_text
        self._retrier = retrier
        self.max_pages = max_pages
        self.request_count = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def endpoint(
        host: str,
        site_prefix: str,
        locale: str,
        *,
        startrow: int,
    ) -> str:
        root = f"/{site_prefix}/" if site_prefix else "/"
        query: list[tuple[str, object]] = [
            ("q", ""),
            ("locationsearch", ""),
            ("startrow", startrow),
        ]
        if locale:
            query.append(("locale", locale))
        return f"https://{host}{root}search/?{urlencode(query)}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        # The crawl-wide retry budget is spent per collection, so it resets
        # here rather than persisting across fetches.
        self._retrier.reset()
        self.last_response_metadata = {}
        host, prefix, locale = _required_config(company)
        expected_total: int | None = None
        expected_pages: int | None = None
        page_size: int | None = None
        raw_seen = 0
        seen_pages: set[str] = set()
        rows: list[dict] = []
        row_index: dict[str, int] = {}
        duplicates = 0

        for page_number in range(1, self.max_pages + 1):
            html = self._request_page(
                self.endpoint(host, prefix, locale, startrow=raw_seen)
            )
            page = _parse_search_page(html)
            if page.explicit_empty:
                if page_number != 1 or raw_seen or rows:
                    raise SourceSchemaError(
                        "successfactors empty-board response was inconsistent"
                    )
                return self._finish([], duplicate_count=0)

            if expected_total is None:
                expected_total = page.total_results
                expected_pages = page.total_pages
                page_size = len(page.records)
                if page.first_result != 1 or page.current_page != 1:
                    raise SourceSchemaError(
                        "successfactors pagination did not start at the first result"
                    )
                calculated_pages = math.ceil(expected_total / page_size)
                if expected_pages != calculated_pages:
                    raise SourceSchemaError(
                        "successfactors pagination metadata is inconsistent"
                    )
            elif page.total_results != expected_total:
                raise SourceSchemaError(
                    "successfactors total changed during pagination"
                )
            elif page.total_pages != expected_pages:
                raise SourceSchemaError(
                    "successfactors total pages changed during pagination"
                )

            if page.fingerprint in seen_pages:
                raise SourceSchemaError(
                    "successfactors returned a repeated pagination page"
                )
            seen_pages.add(page.fingerprint)
            if page.current_page != page_number:
                raise SourceSchemaError(
                    "successfactors returned an unexpected pagination page"
                )
            if page.first_result != raw_seen + 1:
                raise SourceSchemaError(
                    "successfactors pagination result range did not advance"
                )
            if page.last_result != raw_seen + len(page.records):
                raise SourceSchemaError(
                    "successfactors pagination result range is incomplete"
                )
            if page_number < page.total_pages and len(page.records) != page_size:
                raise SourceSchemaError(
                    "successfactors pagination ended a full page prematurely"
                )

            parsed = parse_records(
                list(page.records),
                lambda record: _parse_posting(record, company, host, prefix),
                source_name=self.name,
                company_name=company.name,
                diagnostics=self._record_parse_diagnostics,
            )
            duplicates += _merge_rows(rows, row_index, parsed)
            raw_seen += len(page.records)
            if raw_seen > page.total_results:
                raise SourceSchemaError(
                    "successfactors returned more records than its total"
                )
            if raw_seen == page.total_results:
                if page.current_page != page.total_pages:
                    raise SourceSchemaError(
                        "successfactors pagination completed before the final page"
                    )
                return self._finish(rows, duplicate_count=duplicates)
            if page.current_page == page.total_pages:
                raise SourceSchemaError(
                    "successfactors pagination ended before all results were collected"
                )
            if page_number == self.max_pages:
                raise SourceSchemaError(
                    "successfactors reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable SuccessFactors pagination state")

    def _request_page(self, url: str) -> str:
        self.request_count += 1
        request = self._request_text or _get_text

        # A retry re-requests the identical startrow, so every pagination and
        # completeness check still applies to whatever the retry returns.
        # Exhausting the per-page bound or the crawl budget fails the crawl.
        def attempt() -> str:
            response = request(url, self.name)
            if isinstance(response, TextHttpResponse):
                self.last_response_metadata = dict(response.metadata)
                return response.text
            if not isinstance(response, str):
                raise SourceSchemaError(
                    "successfactors expected an HTML text response"
                )
            return response

        return self._retrier.run(attempt)

    def _finish(self, rows: list[dict], *, duplicate_count: int) -> list[dict]:
        """Publish diagnostics for a crawl that satisfied every check.

        A recovered retry is real degradation and must stay visible, but the
        crawl still enumerated the whole board: it only returns here after the
        collected count matched the expected total on the final page. Record
        loss keeps its existing contract, so a crawl that both retried and
        dropped records is not reported as whole.
        """

        parse_loss = bool(
            self._diagnostic_malformed_rows or self._diagnostic_schema_rows
        )
        recovered = bool(self.retry_attempts) and not parse_loss
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=duplicate_count,
            failed_request_count=self.retry_attempts,
            degraded=True if recovered else None,
            complete=True if recovered else None,
            reason_codes=(
                ("request_retry_recovered",) if self.retry_attempts else ()
            ),
        )
        return rows


def _get_text(url: str, source_name: str) -> TextHttpResponse:
    return get_text_response(url, source_name)


def _required_config(company: CompanyCfg) -> tuple[str, str, str]:
    host = str(company.successfactors_host or "").strip().casefold()
    prefix = str(company.successfactors_site_prefix or "").strip()
    locale = str(company.successfactors_locale or "").strip()
    if not is_valid_hostname(host):
        raise SourceError(
            f"successfactors requires a valid successfactors_host for {company.name}"
        )
    if prefix and not _SITE_PREFIX.fullmatch(prefix):
        raise SourceError(
            f"successfactors site prefix is invalid for {company.name}"
        )
    if locale and not _LOCALE.fullmatch(locale):
        raise SourceError(f"successfactors locale is invalid for {company.name}")
    return host, prefix, locale


def _parse_search_page(html: Any) -> _SearchPage:
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError("successfactors search response was empty")
    parser = _CareerSiteParser()
    try:
        parser.feed(html)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError(
            "successfactors search response contained malformed HTML structure"
        ) from exc
    if parser.structural_error:
        raise SourceSchemaError(
            "successfactors search response contained malformed HTML structure"
        )
    explicit_empty = any(_EMPTY_BOARD.search(alert) for alert in parser.alerts)
    if not parser.records:
        if explicit_empty:
            return _SearchPage((), 0, 0, 0, 1, 1, explicit_empty=True)
        raise SourceSchemaError(
            "successfactors response lacks a populated or explicit empty listing contract"
        )
    if not parser.listing_contract:
        raise SourceSchemaError("successfactors response lacks the listing contract")

    result_label = _one_label(parser.result_labels, "result")
    page_label = _one_label(parser.page_labels, "page")
    result_match = _RESULTS.fullmatch(result_label)
    page_match = _PAGE.fullmatch(page_label)
    if not result_match or not page_match:
        raise SourceSchemaError(
            "successfactors pagination metadata is malformed"
        )
    first, last, total = (int(value) for value in result_match.groups())
    current_page, total_pages = (int(value) for value in page_match.groups())
    if (
        first < 1
        or last < first
        or total < last
        or current_page < 1
        or total_pages < current_page
        or len(parser.records) != last - first + 1
    ):
        raise SourceSchemaError(
            "successfactors pagination metadata is out of range"
        )
    return _SearchPage(
        tuple(parser.records),
        first,
        last,
        total,
        current_page,
        total_pages,
    )


def _one_label(values: list[str], kind: str) -> str:
    unique = {value for value in values if value}
    if len(unique) != 1:
        raise SourceSchemaError(
            f"successfactors {kind} pagination metadata is missing or inconsistent"
        )
    return unique.pop()


def _parse_posting(
    record: Mapping[str, object],
    company: CompanyCfg,
    host: str,
    prefix: str,
) -> dict:
    links = record.get("links")
    if not isinstance(links, list) or not links:
        raise SourceSchemaError("successfactors posting lacks a detail link")
    identities: set[tuple[str, str, str]] = set()
    for link in links:
        if not isinstance(link, Mapping):
            raise SourceSchemaError("successfactors posting link is malformed")
        title = _clean_text(link.get("title"))
        posting_id, url = _posting_identity(link.get("href"), host, prefix)
        if not title:
            raise SourceSchemaError("successfactors posting title is blank")
        identities.add((posting_id, url, title))
    if len(identities) != 1:
        raise SourceSchemaError("successfactors posting links conflict")
    posting_id, source_url, title = identities.pop()
    scope = prefix or "root"
    source_id = f"{host}:{scope}:{posting_id}"
    locations = _unique_text(record.get("locations"))
    dates = {_posting_date(value) for value in _string_values(record.get("dates"))}
    dates.discard("")
    if len(dates) > 1:
        raise SourceSchemaError("successfactors posting dates conflict")
    return make_row(
        source="direct",
        source_adapter="successfactors",
        company=company.name,
        title=title,
        location="; ".join(locations),
        source_url=source_url,
        date_posted=next(iter(dates), ""),
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "native_posting_id": posting_id,
            "successfactors_host": host,
            "successfactors_site_prefix": prefix,
        },
    )


def _posting_identity(value: object, host: str, prefix: str) -> tuple[str, str]:
    href = str(value or "").strip()
    root = f"https://{host}/" + (f"{prefix}/" if prefix else "")
    try:
        parsed = urlsplit(urljoin(root, href))
        parsed_port = parsed.port
    except ValueError as exc:
        raise SourceSchemaError(
            "successfactors posting detail URL is invalid"
        ) from exc
    path_root = (
        f"/{re.escape(prefix)}"
        if prefix
        else rf"(?:/{_SITE_PREFIX.pattern})?"
    )
    match = re.fullmatch(
        rf"{path_root}/job/[^/]+/([1-9][0-9]*)(?:/[^/?#]+)?/?",
        parsed.path,
    )
    if (
        not match
        or parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or parsed_port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SourceSchemaError(
            "successfactors posting requires a same-site posting-specific detail URL"
        )
    return match.group(1), urlunsplit(("https", host, parsed.path, "", ""))


def _merge_rows(rows: list[dict], row_index: dict[str, int], incoming: list[dict]) -> int:
    duplicates = 0
    for row in incoming:
        source_id = str(row.get("extra", {}).get("source_requisition_id") or "")
        existing_index = row_index.get(source_id)
        if existing_index is None:
            row_index[source_id] = len(rows)
            rows.append(row)
            continue
        existing = rows[existing_index]
        if (
            existing.get("source_url") != row.get("source_url")
            or existing.get("title") != row.get("title")
        ):
            raise SourceSchemaError(
                "successfactors returned a conflicting posting identity"
            )
        duplicates += 1
    return duplicates


def _unique_text(value: object) -> list[str]:
    values: list[str] = []
    for item in _string_values(value):
        cleaned = _clean_text(item)
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return values


def _string_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "") for item in value]


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _posting_date(value: object) -> str:
    raw = _clean_text(value)
    for pattern in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    return ""
