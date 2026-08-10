"""TalentBrew/Radancy public careers-site source adapter."""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlsplit

from watcher.config import CompanyCfg
from watcher.sources.base import (
    JsonHttpResponse,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    TextHttpResponse,
    get_json_response,
    get_text_response,
    html_to_text,
    make_row,
    page_fingerprint,
)
from watcher.sources.workday import DEFAULT_MAX_ATTEMPTS, workday_retry_delay

DEFAULT_PAGE_SIZE = 16
DEFAULT_MAX_PAGES = 100
_REFERENCE_CODE = re.compile(r"JR-[A-Z0-9-]+", re.IGNORECASE)
_CHALLENGE_MARKERS = (
    "access denied",
    "captcha",
    "checking your browser",
    "security check",
    "verify you are human",
)


@dataclass(frozen=True)
class TalentBrewDiagnostics:
    listing_pages_requested: int = 0
    detail_pages_requested: int = 0
    raw_postings_seen: int = 0
    duplicate_postings_skipped: int = 0
    valid_rows_retained: int = 0
    request_attempts: int = 0
    retry_attempts: int = 0


@dataclass(frozen=True)
class _Listing:
    posting_id: str
    title: str
    href: str
    location: str = ""
    date_label: str = ""


@dataclass(frozen=True)
class _SearchPage:
    listings: tuple[_Listing, ...]
    total_results: int
    total_pages: int
    current_page: int
    records_per_page: int


class TalentBrewSource:
    """Collect a filtered TalentBrew search and its structured job details.

    TalentBrew's reusable public contract is the JSON envelope returned by
    ``/search-jobs/results``. Its ``results`` member is a platform-owned HTML
    fragment with pagination metadata and stable posting IDs; official detail
    pages add schema.org JobPosting JSON-LD and labelled job facts.
    """

    name = "talentbrew"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        request_text: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if not 1 <= max_attempts <= DEFAULT_MAX_ATTEMPTS:
            raise ValueError("max_attempts is outside the supported retry bound")
        if not 1 <= page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError("max_pages is outside the supported safeguard")
        self._request_json = request_json
        self._request_text = request_text
        self._sleeper = sleeper
        self._jitter = jitter
        self._max_attempts = max_attempts
        self.page_size = page_size
        self.max_pages = max_pages
        self.last_response_metadata: dict[str, object] = {}
        self.last_diagnostics = TalentBrewDiagnostics()
        self._reset_diagnostics()

    @staticmethod
    def search_endpoint(company: CompanyCfg, page: int, page_size: int) -> str:
        host, _site, category_id, category_name = _required_config(company)
        # These are the same public Search Criteria fields serialized by
        # TalentBrew's first-party search.js client.
        params = {
            "ActiveFacetID": "0",
            "CurrentPage": str(page),
            "RecordsPerPage": str(page_size),
            "Distance": "50",
            "RadiusUnitType": "0",
            "Keywords": "",
            "Location": "",
            "Latitude": "",
            "Longitude": "",
            "ShowRadius": "False",
            "IsPagination": "True" if page > 1 else "False",
            "CustomFacetName": "",
            "FacetTerm": "",
            "FacetType": "0",
            "SearchResultsModuleName": "Search Results",
            "SearchFiltersModuleName": "Search Filters",
            "SortCriteria": "0",
            "SortDirection": "0",
            "SearchType": "6",
            "CategoryFacetTerm": "",
            "CategoryFacetType": "0",
            "LocationFacetTerm": "",
            "LocationFacetType": "0",
            "KeywordType": "",
            "LocationType": "",
            "LocationPath": "",
            "OrganizationIds": "",
            "PostalCode": "",
            "ResultsType": "0",
            "fc": "",
            "fl": "",
            "fcf": "",
            "afc": "",
            "afl": "",
            "afcf": "",
            "FacetFilters[0].ID": category_id,
            "FacetFilters[0].FacetType": "1",
            "FacetFilters[0].Count": "0",
            "FacetFilters[0].Display": category_name,
            "FacetFilters[0].IsApplied": "true",
            "FacetFilters[0].FieldName": "",
        }
        return f"https://{host}/search-jobs/results?{urlencode(params)}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._reset_diagnostics()
        listings: list[_Listing] = []
        seen_ids: set[str] = set()
        seen_pages: set[str] = set()
        expected_total: int | None = None

        for requested_page in range(1, self.max_pages + 1):
            self._listing_pages_requested += 1
            payload = self._fetch_json(
                self.search_endpoint(company, requested_page, self.page_size)
            )
            page = self._search_page(payload, expected_page=requested_page)
            if expected_total is None:
                expected_total = page.total_results
            elif page.total_results != expected_total:
                raise SourceSchemaError(
                    "talentbrew total-results count changed during pagination"
                )
            fingerprint = page_fingerprint(
                [{"id": item.posting_id, "href": item.href} for item in page.listings]
            )
            if page.listings and fingerprint in seen_pages:
                raise SourceSchemaError(
                    "talentbrew returned a repeated pagination page"
                )
            seen_pages.add(fingerprint)
            new_count = 0
            for listing in page.listings:
                self._raw_postings_seen += 1
                if listing.posting_id in seen_ids:
                    self._duplicate_postings_skipped += 1
                    continue
                seen_ids.add(listing.posting_id)
                listings.append(listing)
                new_count += 1
            if page.total_results == 0:
                self._finish([])
                return []
            if requested_page >= page.total_pages:
                if len(listings) < page.total_results:
                    raise SourceSchemaError(
                        "talentbrew pagination completed before every unique result was collected"
                    )
                return self._details(listings, company)
            if not page.listings or new_count == 0:
                raise SourceSchemaError("talentbrew pagination did not advance")
            if requested_page == self.max_pages:
                raise SourceSchemaError(
                    "talentbrew reached the maximum page safeguard before completion"
                )
        raise AssertionError("unreachable TalentBrew pagination state")

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        self._reset_diagnostics()
        page = self._search_page(payload)
        self._raw_postings_seen = len(page.listings)
        return self._details(list(page.listings), company)

    def _search_page(
        self,
        payload: Any,
        expected_page: int | None = None,
    ) -> _SearchPage:
        if not isinstance(payload, dict):
            raise SourceSchemaError("talentbrew expected a JSON object")
        required = ("filters", "results", "hasJobs", "hasContent")
        if any(key not in payload for key in required):
            raise SourceSchemaError("talentbrew search envelope is missing required fields")
        if not isinstance(payload["results"], str) or not isinstance(
            payload["filters"], str
        ):
            raise SourceSchemaError("talentbrew search fragments must be strings")
        if not isinstance(payload["hasJobs"], bool) or not isinstance(
            payload["hasContent"], bool
        ):
            raise SourceSchemaError("talentbrew search flags must be booleans")
        parser = _SearchResultsParser()
        parser.feed(payload["results"])
        page = parser.page()
        if expected_page is not None and page.current_page != expected_page:
            raise SourceSchemaError(
                f"talentbrew returned current page {page.current_page}; expected {expected_page}"
            )
        if not payload["hasJobs"]:
            if page.total_results != 0 or page.listings:
                raise SourceSchemaError("talentbrew hasJobs conflicts with result metadata")
        elif page.total_results <= 0 or not page.listings:
            raise SourceSchemaError("talentbrew reported jobs without valid listing records")
        return page

    def _details(self, listings: list[_Listing], company: CompanyCfg) -> list[dict]:
        rows = [self._detail(listing, company) for listing in listings]
        self._finish(rows)
        return rows

    def _detail(self, listing: _Listing, company: CompanyCfg) -> dict:
        host, site, _category_id, category_name = _required_config(company)
        listing_url = urljoin(f"https://{host}/", listing.href)
        self._detail_pages_requested += 1
        try:
            html = self._fetch_text(listing_url)
            if _looks_like_challenge(html):
                raise SourceFetchError(
                    "talentbrew returned an access challenge",
                    error_code="html_challenge",
                    retryable=False,
                )
            data, facts = _detail_data(html)
        except SourceFetchError as exc:
            raise SourceFetchError(
                f"talentbrew detail {listing.posting_id} failed: {exc}",
                error_code=exc.error_code,
                status_code=exc.status_code,
                retryable=exc.retryable,
                response_metadata=exc.response_metadata,
                attempt_count=exc.attempt_count,
            ) from exc
        except SourceSchemaError as exc:
            self._snapshot()
            raise SourceSchemaError(
                f"talentbrew detail {listing.posting_id} failed schema validation: {exc}"
            ) from exc
        canonical_url = _canonical_url(
            data.get("url"), listing_url, host, site, listing.posting_id
        )
        reference = _reference_code(
            facts.get("Reference Code") or data.get("identifier")
        )
        requisition_id = reference or listing.posting_id
        title = html_to_text(data.get("title")) or listing.title
        if not title or not requisition_id:
            raise SourceSchemaError(
                f"talentbrew detail {listing.posting_id} lacks stable identity"
            )
        description_html = str(data.get("description") or "")
        contract = facts.get("Contract", "")
        programme = facts.get("Programme", "")
        work_pattern = facts.get("Work Pattern", "")
        row = make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=_locations(data.get("jobLocation")) or listing.location,
            compensation=_compensation(data.get("baseSalary")),
            description=html_to_text(description_html),
            requirements=_requirements(data, description_html),
            source_url=canonical_url,
            date_posted=_date(data.get("datePosted") or facts.get("Date live")),
            deadline=_date(
                data.get("validThrough")
                or facts.get("Application Deadline")
                or facts.get("Closing Date")
            ),
            remote_status=work_pattern or _remote_status(data.get("jobLocationType")),
            internship_type=_joined(
                data.get("employmentType"),
                data.get("workHours"),
                programme,
                data.get("occupationalCategory"),
            ),
            extra={
                "source_id": listing.posting_id,
                "source_requisition_id": requisition_id,
                "source_system": self.name,
                "source_scope": f"{host}:{site}",
                "talentbrew_host": host,
                "talentbrew_site_id": site,
                "talentbrew_posting_id": listing.posting_id,
                "active": True,
                "official_category": facts.get("Area of Expertise") or category_name,
                "official_programme": programme,
                "official_contract": contract or html_to_text(data.get("employmentType")),
                "official_reference_code": reference,
                "official_work_pattern": work_pattern,
            },
        )
        return row

    def _fetch_json(self, url: str) -> Any:
        response = self._request_with_retry(self._request_json or _get_json, url)
        if isinstance(response, JsonHttpResponse):
            self.last_response_metadata = dict(response.metadata)
            return response.payload
        return response

    def _fetch_text(self, url: str) -> str:
        response = self._request_with_retry(self._request_text or _get_text, url)
        if isinstance(response, TextHttpResponse):
            self.last_response_metadata = dict(response.metadata)
            return response.text
        if not isinstance(response, str):
            raise SourceSchemaError("talentbrew detail response must be text")
        return response

    def _request_with_retry(self, request: Callable[[str, str], Any], url: str) -> Any:
        for attempt in range(1, self._max_attempts + 1):
            self._request_attempts += 1
            try:
                return request(url, self.name)
            except SourceFetchError as exc:
                exc.attempt_count = attempt
                if not exc.retryable or attempt >= self._max_attempts:
                    self._snapshot()
                    raise
                self._retry_attempts += 1
                retry_after = exc.response_metadata.get("retry_after_seconds")
                delay = workday_retry_delay(
                    attempt,
                    retry_after=retry_after,
                    jitter=self._jitter,
                )
                self._sleeper(delay)
        raise AssertionError("unreachable TalentBrew retry state")

    def _reset_diagnostics(self) -> None:
        self._listing_pages_requested = 0
        self._detail_pages_requested = 0
        self._raw_postings_seen = 0
        self._duplicate_postings_skipped = 0
        self._valid_rows_retained = 0
        self._request_attempts = 0
        self._retry_attempts = 0
        self.last_diagnostics = TalentBrewDiagnostics()

    def _finish(self, rows: list[dict]) -> None:
        self._valid_rows_retained = len(rows)
        self._snapshot()

    def _snapshot(self) -> None:
        self.last_diagnostics = TalentBrewDiagnostics(
            listing_pages_requested=self._listing_pages_requested,
            detail_pages_requested=self._detail_pages_requested,
            raw_postings_seen=self._raw_postings_seen,
            duplicate_postings_skipped=self._duplicate_postings_skipped,
            valid_rows_retained=self._valid_rows_retained,
            request_attempts=self._request_attempts,
            retry_attempts=self._retry_attempts,
        )


class _SearchResultsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}
        self.listings: list[_Listing] = []
        self.current: dict[str, str] | None = None
        self.current_depth = 0
        self.capture = ""
        self.capture_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = set(values.get("class", "").split())
        if values.get("id") == "search-results":
            self.metadata = values
        is_card = tag == "div" and "list-item" in classes
        if self.current is None and (tag == "li" or is_card):
            self.current = {}
            self.current_depth = 1
        elif self.current is not None:
            self.current_depth += 1
        if self.current is None:
            return
        if self.capture:
            self.capture_depth += 1
        if tag == "a" and "job-title--link" in classes:
            self.current["posting_id"] = values.get("data-job-id", "").strip()
            self.current["href"] = values.get("href", "").strip()
            self.capture, self.capture_depth = "title", 1
        elif tag == "div" and "job-location" in classes:
            self.capture, self.capture_depth = "location", 1
        elif tag == "div" and "job-date" in classes:
            self.capture, self.capture_depth = "date_label", 1

    def handle_data(self, data: str) -> None:
        if self.current is not None and self.capture:
            self.current[self.capture] = self.current.get(self.capture, "") + data

    def handle_endtag(self, tag: str) -> None:
        if self.capture:
            self.capture_depth -= 1
            if self.capture_depth == 0:
                self.capture = ""
        if self.current is not None:
            self.current_depth -= 1
        if self.current is not None and self.current_depth == 0:
            values = {key: html_to_text(value) for key, value in self.current.items()}
            if values.get("posting_id") and values.get("href") and values.get("title"):
                self.listings.append(_Listing(**values))
            self.current = None

    def page(self) -> _SearchPage:
        if not self.metadata:
            raise SourceSchemaError("talentbrew results lack search pagination metadata")
        try:
            total_results = int(self.metadata["data-total-results"])
            total_pages = int(self.metadata["data-total-pages"])
            current_page = int(self.metadata["data-current-page"])
            records_per_page = int(self.metadata["data-records-per-page"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceSchemaError("talentbrew pagination metadata is malformed") from exc
        if (
            min(total_results, total_pages, records_per_page) < 0
            or current_page < 1
            or records_per_page < 1
        ):
            raise SourceSchemaError("talentbrew pagination metadata is out of range")
        if total_results == 0 and total_pages != 0:
            raise SourceSchemaError("talentbrew zero results reported nonzero pages")
        if total_results > 0 and (total_pages < 1 or current_page > total_pages):
            raise SourceSchemaError("talentbrew pagination metadata is inconsistent")
        return _SearchPage(tuple(self.listings), total_results, total_pages, current_page, records_per_page)


class _DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.json_scripts: list[str] = []
        self._script: list[str] | None = None
        self._fact: dict[str, str] | None = None
        self._fact_field = ""
        self.facts: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._script = []
        elif tag == "p":
            self._fact = {}
        elif self._fact is not None and tag == "span":
            classes = set(values.get("class", "").split())
            self._fact_field = (
                "value" if "job-info-label-text" in classes else "label"
            )
        elif self._fact is not None and tag == "strong":
            self._fact_field = "value"

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        if self._fact is not None and self._fact_field:
            self._fact[self._fact_field] = self._fact.get(self._fact_field, "") + data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.json_scripts.append("".join(self._script))
            self._script = None
        elif tag in {"span", "strong"}:
            self._fact_field = ""
        elif tag == "p" and self._fact is not None:
            label = html_to_text(self._fact.get("label")).rstrip(":").strip()
            value = html_to_text(self._fact.get("value"))
            if label and value:
                self.facts[label] = value
            self._fact = None


def _detail_data(html: str) -> tuple[dict[str, Any], dict[str, str]]:
    parser = _DetailParser()
    parser.feed(html)
    for script in parser.json_scripts:
        try:
            candidate = json.loads(script)
        except json.JSONDecodeError:
            continue
        candidates = candidate if isinstance(candidate, list) else [candidate]
        for item in candidates:
            if (
                isinstance(item, dict)
                and str(item.get("@type", "")).casefold() == "jobposting"
            ):
                if not item.get("title"):
                    raise SourceSchemaError("talentbrew JobPosting JSON-LD lacks a title")
                return item, parser.facts
    raise SourceSchemaError("talentbrew detail lacks valid JobPosting JSON-LD")


def _required_config(company: CompanyCfg) -> tuple[str, str, str, str]:
    values = (
        str(company.talentbrew_host or "").strip().casefold(),
        str(company.talentbrew_site_id or "").strip(),
        str(company.talentbrew_category_id or "").strip(),
        str(company.talentbrew_category_name or "").strip(),
    )
    if not all(values):
        raise SourceError(f"talentbrew configuration is incomplete for {company.name}")
    return values


def _get_json(url: str, source_name: str) -> JsonHttpResponse:
    return get_json_response(url, source_name)


def _get_text(url: str, source_name: str) -> TextHttpResponse:
    return get_text_response(url, source_name)


def _looks_like_challenge(html: str) -> bool:
    lowered = html[:16_384].casefold()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def _reference_code(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("name")
    match = _REFERENCE_CODE.fullmatch(str(value or "").strip())
    return match.group(0).upper() if match else ""


def _canonical_url(
    value: Any,
    fallback: str,
    host: str,
    site: str,
    posting_id: str,
) -> str:
    candidate = str(value or "").strip() or fallback
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        parsed = urlsplit(fallback)
    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or len(segments) < 2
        or segments[-2] != site
        or segments[-1] != posting_id
    ):
        candidate = fallback
    return candidate.split("#", 1)[0]


def _date(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match:
        try:
            return datetime(*(int(part) for part in match.groups())).date().isoformat()
        except ValueError:
            return ""
    return ""


def _locations(value: Any) -> str:
    items = value if isinstance(value, list) else [value]
    locations: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        if not isinstance(address, dict):
            continue
        parts = [
            html_to_text(address.get("addressLocality")),
            html_to_text(address.get("addressRegion")),
            html_to_text(address.get("addressCountry")),
        ]
        location = ", ".join(part for part in parts if part)
        if location and location not in locations:
            locations.append(location)
    return "; ".join(locations)


def _requirements(data: dict[str, Any], description_html: str) -> str:
    explicit = _joined(
        data.get("qualifications"),
        data.get("experienceRequirements"),
        data.get("skills"),
    )
    if explicit:
        return explicit
    match = re.search(
        r"(?is)<(?:h[1-6]|b|strong)[^>]*>\s*"
        r"(?:who we are looking for|requirements|qualifications)\s*"
        r"</(?:h[1-6]|b|strong)>\s*(.*?)(?=<(?:h[1-6]|b|strong|i)\b|$)",
        description_html,
    )
    return html_to_text(match.group(1)) if match else ""


def _joined(*values: Any) -> str:
    result: list[str] = []
    for value in values:
        items = value if isinstance(value, list) else [value]
        for item in items:
            text = html_to_text(item)
            if text and text not in result:
                result.append(text)
    return "; ".join(result)


def _remote_status(value: Any) -> str:
    text = html_to_text(value).replace("_", " ").title()
    return "Hybrid" if text == "Hybrid" else text


def _compensation(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    currency = html_to_text(value.get("currency"))
    amount = value.get("value")
    if isinstance(amount, dict):
        low = amount.get("minValue")
        high = amount.get("maxValue")
        unit = html_to_text(amount.get("unitText"))
        if low not in (None, "") and high not in (None, ""):
            return f"{currency} {low}–{high} per {unit}".strip()
        amount = low if low not in (None, "") else high
    return f"{currency} {amount}".strip() if amount not in (None, "") else ""
