"""iCIMS source adapter for Modern Jibe JSON and classic iframe boards."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from watcher.config import CompanyCfg, is_valid_hostname
from watcher.sources.base import (
    DirectDiagnosticsMixin,
    JsonHttpResponse,
    SourceError,
    SourceSchemaError,
    TextHttpResponse,
    get_json_response,
    get_text_response,
    html_to_text,
    iso_date,
    make_row,
    page_fingerprint,
    parse_records,
)

JIBE_JSON = "jibe_json"
CLASSIC = "classic"
SUPPORTED_VARIANTS = frozenset({JIBE_JSON, CLASSIC})
DEFAULT_JIBE_PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 1_000
_NATIVE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")
_CLASSIC_DETAIL_PATH = re.compile(r"^/jobs/([1-9][0-9]*)/[^/]+/job/?$")
_EMPTY_CLASSIC_MESSAGE = re.compile(
    r"\bsorry,\s+no jobs were found that match your search criteria\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ClassicPage:
    cards: tuple[Mapping[str, object], ...]
    current_page: int | None
    total_pages: int | None
    explicit_empty: bool
    listing_contract: bool
    outer_shell: bool

    @property
    def fingerprint(self) -> str:
        return page_fingerprint([dict(card) for card in self.cards])


class IcimsSource(DirectDiagnosticsMixin):
    """Collect one completely enumerable anonymous iCIMS board."""

    name = "icims"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        request_text: Callable[[str, str], Any] | None = None,
        jibe_page_size: int = DEFAULT_JIBE_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if not 1 <= jibe_page_size <= DEFAULT_JIBE_PAGE_SIZE:
            raise ValueError(
                f"jibe_page_size must be between 1 and {DEFAULT_JIBE_PAGE_SIZE}"
            )
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        self._request_json = request_json
        self._request_text = request_text
        self.jibe_page_size = jibe_page_size
        self.max_pages = max_pages
        self.last_response_metadata: dict[str, Mapping[str, object]] = {}
        self._begin_direct_diagnostics()

    @staticmethod
    def jibe_endpoint(host: str, *, limit: int, page: int) -> str:
        return f"https://{host}/api/jobs?{urlencode((('limit', limit), ('page', page)))}"

    @staticmethod
    def classic_endpoint(host: str, *, page: int) -> str:
        return (
            f"https://{host}/jobs/search?"
            f"{urlencode((('ss', 1), ('in_iframe', 1), ('pr', page)))}"
        )

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.last_response_metadata = {}
        self._duplicate_count = 0
        variant, portals = _required_config(company)
        rows: list[dict] = []
        row_index: dict[str, int] = {}
        duplicate_count = 0

        for portal in portals:
            if variant == JIBE_JSON:
                portal_rows = self._fetch_jibe_portal(company, portal)
            else:
                portal_rows = self._fetch_classic_portal(company, portal)
            duplicate_count += _merge_rows(rows, row_index, portal_rows)

        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=duplicate_count + self._duplicate_count,
        )
        return rows

    def _fetch_jibe_portal(self, company: CompanyCfg, portal: str) -> list[dict]:
        expected_total: int | None = None
        raw_seen = 0
        seen_pages: set[str] = set()
        rows: list[dict] = []
        row_index: dict[str, int] = {}

        for page_number in range(1, self.max_pages + 1):
            payload = self._request_json_page(
                self.jibe_endpoint(
                    portal,
                    limit=self.jibe_page_size,
                    page=page_number,
                ),
                portal,
            )
            postings, total = _jibe_page(payload, page_size=self.jibe_page_size)
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise SourceSchemaError(
                    "icims jibe totalCount changed during pagination"
                )

            if total == 0:
                if postings or page_number != 1:
                    raise SourceSchemaError(
                        "icims jibe zero-result response was inconsistent"
                    )
                return []
            if not postings:
                raise SourceSchemaError(
                    "icims jibe pagination ended before totalCount"
                )

            fingerprint = page_fingerprint(postings)
            if fingerprint in seen_pages:
                raise SourceSchemaError("icims jibe returned a repeated pagination page")
            seen_pages.add(fingerprint)
            raw_seen += len(postings)
            if raw_seen > total:
                raise SourceSchemaError(
                    "icims jibe returned more records than totalCount"
                )

            parsed = parse_records(
                postings,
                lambda posting: _parse_jibe_posting(posting, company, portal),
                source_name=self.name,
                company_name=company.name,
                diagnostics=self._record_parse_diagnostics,
            )
            self._duplicate_count += _merge_rows(rows, row_index, parsed)

            if raw_seen == total:
                return rows
            if page_number == self.max_pages:
                raise SourceSchemaError(
                    "icims jibe reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable iCIMS Jibe pagination state")

    def _fetch_classic_portal(self, company: CompanyCfg, portal: str) -> list[dict]:
        expected_pages: int | None = None
        seen_pages: set[str] = set()
        rows: list[dict] = []
        row_index: dict[str, int] = {}

        for page_index in range(self.max_pages):
            html = self._request_text_page(
                self.classic_endpoint(portal, page=page_index),
                portal,
            )
            page = _parse_classic_page(html)
            if page.outer_shell:
                raise SourceSchemaError(
                    "icims classic received the outer iframe shell instead of a listing response"
                )
            if page.explicit_empty:
                if page_index != 0 or page.cards:
                    raise SourceSchemaError(
                        "icims classic empty-board response was inconsistent"
                    )
                return []
            if not page.listing_contract:
                raise SourceSchemaError("icims classic response lacks the listing contract")
            if not page.cards:
                raise SourceSchemaError(
                    "icims classic populated listing page contained no posting records"
                )

            fingerprint = page.fingerprint
            if fingerprint in seen_pages:
                raise SourceSchemaError(
                    "icims classic returned a repeated pagination page"
                )
            seen_pages.add(fingerprint)

            current_page = page.current_page
            total_pages = page.total_pages
            if current_page is None or total_pages is None:
                raise SourceSchemaError(
                    "icims classic listing is missing pagination metadata"
                )
            if current_page != page_index + 1:
                raise SourceSchemaError(
                    f"icims classic returned page {current_page}; expected {page_index + 1}"
                )
            if expected_pages is None:
                expected_pages = total_pages
            elif total_pages != expected_pages:
                raise SourceSchemaError(
                    "icims classic total page count changed during pagination"
                )
            if current_page > total_pages:
                raise SourceSchemaError("icims classic pagination metadata is invalid")

            parsed = parse_records(
                list(page.cards),
                lambda card: _parse_classic_card(card, company, portal),
                source_name=self.name,
                company_name=company.name,
                diagnostics=self._record_parse_diagnostics,
            )
            self._duplicate_count += _merge_rows(rows, row_index, parsed)

            if current_page == total_pages:
                return rows
            if page_index + 1 == self.max_pages:
                raise SourceSchemaError(
                    "icims classic reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable iCIMS classic pagination state")

    def _request_json_page(self, url: str, portal: str) -> Any:
        response = (self._request_json or _get_json)(url, self.name)
        if isinstance(response, JsonHttpResponse):
            self.last_response_metadata[portal] = dict(response.metadata)
            return response.payload
        return response

    def _request_text_page(self, url: str, portal: str) -> str:
        response = (self._request_text or _get_text)(url, self.name)
        if isinstance(response, TextHttpResponse):
            self.last_response_metadata[portal] = dict(response.metadata)
            return response.text
        if not isinstance(response, str):
            raise SourceSchemaError("icims classic expected an HTML text response")
        return response


def _get_json(url: str, source_name: str) -> JsonHttpResponse:
    return get_json_response(url, source_name)


def _get_text(url: str, source_name: str) -> TextHttpResponse:
    return get_text_response(url, source_name)


def _required_config(company: CompanyCfg) -> tuple[str, tuple[str, ...]]:
    variant = str(company.icims_variant or "").strip().casefold()
    host = str(company.icims_host or "").strip().casefold()
    portals = tuple(
        str(portal or "").strip().casefold() for portal in company.icims_portals
    )
    if variant not in SUPPORTED_VARIANTS:
        raise SourceError(f"icims requires a supported icims_variant for {company.name}")
    if not is_valid_hostname(host):
        raise SourceError(f"icims requires a valid icims_host for {company.name}")
    if portals:
        if host not in portals or len(portals) != len(set(portals)):
            raise SourceError(f"icims portal scope is invalid for {company.name}")
        if any(not is_valid_hostname(portal) for portal in portals):
            raise SourceError(f"icims portal scope is invalid for {company.name}")
    else:
        portals = (host,)
    return variant, portals


def _jibe_page(payload: Any, *, page_size: int) -> tuple[list, int]:
    if not isinstance(payload, dict):
        raise SourceSchemaError("icims jibe expected a JSON object")
    jobs = payload.get("jobs")
    total = payload.get("totalCount")
    if not isinstance(jobs, list):
        raise SourceSchemaError("icims jibe expected jobs to be a list")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SourceSchemaError(
            "icims jibe expected totalCount to be a nonnegative integer"
        )
    if len(jobs) > page_size:
        raise SourceSchemaError("icims jibe jobs exceeded the requested page limit")
    if total == 0 and jobs:
        raise SourceSchemaError("icims jibe zero totalCount included posting records")
    return jobs, total


def _parse_jibe_posting(posting: Any, company: CompanyCfg, portal: str) -> dict:
    if not isinstance(posting, dict):
        raise SourceSchemaError("icims jibe expected each job wrapper to be an object")
    data = posting.get("data")
    if not isinstance(data, dict):
        raise SourceSchemaError("icims jibe expected each job data field to be an object")
    native_id = str(data.get("req_id") or "").strip()
    title = str(data.get("title") or "").strip()
    if not _NATIVE_ID.fullmatch(native_id) or not title:
        raise SourceSchemaError("icims jibe job missing a valid req_id or title")
    meta = data.get("meta_data")
    if not isinstance(meta, dict):
        raise SourceSchemaError("icims jibe job meta_data must be an object")
    canonical_url = _jibe_canonical_url(
        meta.get("canonical_url"),
        portal=portal,
        native_id=native_id,
    )
    application_url = _application_url(
        data.get("apply_url"),
        portal=portal,
        native_id=native_id,
    )
    source_id = _namespaced_id(portal, native_id)
    description = _joined_text(
        data.get("description"),
        data.get("responsibilities"),
    )

    return make_row(
        source="direct",
        source_adapter="icims",
        company=company.name,
        title=title,
        location=_jibe_locations(data),
        description=description,
        requirements=html_to_text(data.get("qualifications")),
        source_url=canonical_url,
        date_posted=iso_date(data.get("posted_date")),
        deadline=iso_date(data.get("posting_expiry_date")),
        remote_status=str(data.get("location_type") or "").strip(),
        internship_type=str(data.get("employment_type") or "").strip(),
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "icims",
            "source_scope": portal,
            "icims_variant": JIBE_JSON,
            "icims_portal": portal,
            "icims_native_id": native_id,
            "application_url": application_url,
            "active": _jibe_active(data),
        },
    )


def _jibe_canonical_url(value: Any, *, portal: str, native_id: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise SourceSchemaError("icims jibe canonical URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != portal
        or parsed.netloc.casefold() != portal
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.path.rstrip("/") != f"/jobs/{native_id}"
    ):
        raise SourceSchemaError(
            "icims jibe canonical URL is not a posting on the configured portal"
        )
    return urlunsplit(("https", portal, parsed.path, parsed.query, ""))


def _application_url(value: Any, *, portal: str, native_id: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.rstrip("/") == f"{portal}/prelogin/{native_id}":
        raw = f"https://{raw.rstrip('/')}"
    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise SourceSchemaError("icims application URL is invalid") from exc
    application_host = (parsed.hostname or "").casefold()
    normal_login = bool(
        re.fullmatch(r"/jobs/[^/]+/(?:login|job)/?", parsed.path)
        and (application_host == portal or application_host.endswith(".icims.com"))
    )
    portal_prelogin = bool(
        application_host == portal
        and parsed.netloc.casefold() == portal
        and parsed.path.rstrip("/") == f"/prelogin/{native_id}"
    )
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not (normal_login or portal_prelogin)
    ):
        raise SourceSchemaError("icims application URL is not posting-specific")
    return raw


def _jibe_locations(data: Mapping[str, object]) -> str:
    values: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text.casefold() not in {item.casefold() for item in values}:
            values.append(text)

    add(data.get("full_location") or data.get("location_name"))
    additional = data.get("additional_locations")
    if additional in (None, ""):
        return "; ".join(values)
    if not isinstance(additional, list):
        raise SourceSchemaError("icims jibe additional_locations must be a list")
    for location in additional:
        if not isinstance(location, dict):
            raise SourceSchemaError(
                "icims jibe additional location must be an object"
            )
        name = location.get("full_location") or location.get("location_name")
        if not name:
            name = ", ".join(
                str(location.get(key) or "").strip()
                for key in ("city", "state", "country")
                if str(location.get(key) or "").strip()
            )
        add(name)
    return "; ".join(values)


def _joined_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        text = html_to_text(value)
        if text and text.casefold() not in {part.casefold() for part in parts}:
            parts.append(text)
    return " ".join(parts)


def _jibe_active(data: Mapping[str, object]) -> bool:
    for field in ("applyable", "external", "searchable"):
        if isinstance(data.get(field), bool):
            return bool(data[field])
    return True


def _parse_classic_page(html: str) -> _ClassicPage:
    parser = _ClassicListingParser()
    try:
        parser.feed(html)
        parser.close()
    except (ValueError, TypeError) as exc:
        raise SourceSchemaError("icims classic HTML could not be parsed") from exc

    page_markers = {
        (int(current), int(total))
        for current, total in re.findall(
            r"\bPage\s+([0-9]+)\s+of\s+([0-9]+)\b",
            " ".join(parser.page_headers),
            flags=re.IGNORECASE,
        )
    }
    if len(page_markers) > 1:
        raise SourceSchemaError("icims classic pagination metadata conflicts")
    current_page, total_pages = next(iter(page_markers), (None, None))
    explicit_empty = bool(
        parser.listing_page
        and parser.generic_messages
        and any(_EMPTY_CLASSIC_MESSAGE.search(message) for message in parser.generic_messages)
    )
    return _ClassicPage(
        cards=tuple(parser.cards),
        current_page=current_page,
        total_pages=total_pages,
        explicit_empty=explicit_empty,
        listing_contract=parser.listing_page and parser.jobs_table,
        outer_shell=bool(parser.search_iframes and not parser.listing_page),
    )


def _parse_classic_card(card: Any, company: CompanyCfg, portal: str) -> dict:
    if not isinstance(card, Mapping):
        raise SourceSchemaError("icims classic posting card must be an object")
    title = str(card.get("title") or "").strip()
    source_url, native_id = _classic_detail_url(card.get("href"), portal=portal)
    if not title:
        raise SourceSchemaError("icims classic posting is missing a title")
    fields = card.get("fields")
    if not isinstance(fields, Mapping):
        fields = {}
    normalized_fields = {
        re.sub(r"\s+", " ", str(key or "")).strip().casefold():
        re.sub(r"\s+", " ", str(value or "")).strip()
        for key, value in fields.items()
        if str(key or "").strip()
    }
    locations = _unique_text(
        normalized_fields.get("location", ""),
        normalized_fields.get("seat location", ""),
        separator="; ",
    )
    source_id = _namespaced_id(portal, native_id)
    employment_type = _unique_text(
        normalized_fields.get("employment type", ""),
        normalized_fields.get("position type", ""),
        separator="; ",
    )

    return make_row(
        source="direct",
        source_adapter="icims",
        company=company.name,
        title=title,
        location=locations,
        description=str(card.get("description") or "").strip(),
        requirements="",
        source_url=source_url,
        date_posted="",
        deadline=_classic_date(normalized_fields.get("post end date")),
        remote_status="",
        internship_type=employment_type,
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "icims",
            "source_scope": portal,
            "icims_variant": CLASSIC,
            "icims_portal": portal,
            "icims_native_id": native_id,
            "icims_display_id": normalized_fields.get("id", ""),
            "category": normalized_fields.get("category", ""),
            "department": normalized_fields.get("department", "")
            or normalized_fields.get("dept id", ""),
            "shift": normalized_fields.get("shift", ""),
            "active": True,
        },
    )


def _classic_detail_url(value: Any, *, portal: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if not raw:
        raise SourceSchemaError("icims classic posting is missing a detail URL")
    try:
        parsed = urlsplit(urljoin(f"https://{portal}/", raw))
    except ValueError as exc:
        raise SourceSchemaError("icims classic detail URL is invalid") from exc
    match = _CLASSIC_DETAIL_PATH.fullmatch(parsed.path)
    if (
        not match
        or parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != portal
        or parsed.netloc.casefold() != portal
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise SourceSchemaError(
            "icims classic detail URL is not a posting on the configured portal"
        )
    return urlunsplit(("https", portal, parsed.path.rstrip("/"), "", "")), match.group(1)


def _classic_date(value: Any) -> str:
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if match:
        month, day, year = (int(part) for part in match.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}"
        return ""
    return iso_date(raw) if raw else ""


def _namespaced_id(portal: str, native_id: str) -> str:
    return f"{portal}:{native_id}"


def _merge_rows(
    rows: list[dict],
    row_index: dict[str, int],
    additions: Iterable[dict],
) -> int:
    duplicates = 0
    for row in additions:
        source_id = str(row.get("extra", {}).get("source_id") or "")
        source_url = str(row.get("source_url") or "")
        if not source_id or not source_url:
            raise SourceSchemaError("icims canonical row lacks stable identity")
        existing_index = row_index.get(source_id)
        if existing_index is None:
            row_index[source_id] = len(rows)
            rows.append(row)
            continue
        if rows[existing_index] != row:
            raise SourceSchemaError("icims returned a conflicting duplicate posting ID")
        duplicates += 1
    return duplicates


def _unique_text(*values: Any, separator: str) -> str:
    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text.casefold() not in {item.casefold() for item in unique}:
            unique.append(text)
    return separator.join(unique)


class _ClassicListingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.listing_page = False
        self.jobs_table = False
        self.search_iframes: list[str] = []
        self.visible_text: list[str] = []
        self.page_headers: list[str] = []
        self.generic_messages: list[str] = []
        self.cards: list[dict[str, object]] = []
        self._skip_depth = 0
        self._card: dict[str, object] | None = None
        self._card_depth: int | None = None
        self._title_depth: int | None = None
        self._description_depth: int | None = None
        self._field_label_depth: int | None = None
        self._field_value_depth: int | None = None
        self._field_label_parts: list[str] = []
        self._field_value_parts: list[str] = []
        self._pending_label = ""
        self._message_depth: int | None = None
        self._message_parts: list[str] = []
        self._page_header_depth: int | None = None
        self._page_header_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        is_void = tag in {
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
        self.depth += 1
        values = {key.casefold(): str(value or "") for key, value in attrs}
        classes = set(values.get("class", "").split())
        if tag in {"script", "style"}:
            self._skip_depth += 1
        if "iCIMS_ListingsPage" in classes:
            self.listing_page = True
        if "iCIMS_JobsTable" in classes:
            self.jobs_table = True
        if tag == "iframe" and "/jobs/search" in values.get("src", ""):
            self.search_iframes.append(values["src"])
        if "iCIMS_GenericMessage" in classes:
            self._message_depth = self.depth
            self._message_parts = []
        if "iCIMS_SubHeader_Jobs" in classes:
            self._page_header_depth = self.depth
            self._page_header_parts = []
        if "iCIMS_JobCardItem" in classes and self._card is None:
            self._card = {"title": "", "href": "", "description": "", "fields": {}}
            self._card_depth = self.depth
        if self._card is not None:
            if (
                tag == "a"
                and "/jobs/" in values.get("href", "")
                and not self._card.get("href")
            ):
                self._card["href"] = values["href"]
            if tag == "h3":
                self._title_depth = self.depth
            if tag == "div" and "description" in classes:
                self._description_depth = self.depth
            if tag == "dt" and "iCIMS_JobHeaderField" in classes:
                self._field_label_depth = self.depth
                self._field_label_parts = []
            if tag == "dd" and "iCIMS_JobHeaderData" in classes:
                self._field_value_depth = self.depth
                self._field_value_parts = []
        if is_void:
            self.depth = max(0, self.depth - 1)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        if self._title_depth == self.depth and tag == "h3":
            self._title_depth = None
        if self._description_depth == self.depth and tag == "div":
            self._description_depth = None
        if self._field_label_depth == self.depth and tag == "dt":
            self._pending_label = _normalized_parts(self._field_label_parts)
            self._field_label_depth = None
        if self._field_value_depth == self.depth and tag == "dd":
            if self._card is not None and self._pending_label:
                fields = self._card["fields"]
                assert isinstance(fields, dict)
                fields[self._pending_label] = _normalized_parts(self._field_value_parts)
            self._pending_label = ""
            self._field_value_depth = None
        if self._message_depth == self.depth:
            message = _normalized_parts(self._message_parts)
            if message:
                self.generic_messages.append(message)
            self._message_depth = None
        if self._page_header_depth == self.depth:
            header = _normalized_parts(self._page_header_parts)
            if header:
                self.page_headers.append(header)
            self._page_header_depth = None
        if self._card_depth == self.depth and self._card is not None:
            self.cards.append(self._card)
            self._card = None
            self._card_depth = None
            self._title_depth = None
            self._description_depth = None
            self._field_label_depth = None
            self._field_value_depth = None
            self._pending_label = ""
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if not self._skip_depth:
            self.visible_text.append(text)
        if self._message_depth is not None:
            self._message_parts.append(text)
        if self._page_header_depth is not None:
            self._page_header_parts.append(text)
        if self._card is None:
            return
        if self._title_depth is not None:
            current = str(self._card.get("title") or "")
            self._card["title"] = f"{current} {text}".strip()
        if self._description_depth is not None:
            current = str(self._card.get("description") or "")
            self._card["description"] = f"{current} {text}".strip()
        if self._field_label_depth is not None:
            self._field_label_parts.append(text)
        if self._field_value_depth is not None:
            self._field_value_parts.append(text)


def _normalized_parts(values: Iterable[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(values)).strip()
