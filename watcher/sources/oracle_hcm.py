"""Oracle HCM Candidate Experience source adapter."""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlencode

from watcher.config import CompanyCfg
from watcher.sources.base import (
    JsonHttpResponse,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    get_json_response,
    html_to_text,
    iso_date,
    make_row,
    page_fingerprint,
    parse_records,
)
from watcher.sources.workday import DEFAULT_MAX_ATTEMPTS, workday_retry_delay

LOGGER = logging.getLogger(__name__)
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_PAGES = 1_000


@dataclass(frozen=True)
class OracleHcmDiagnostics:
    pages_requested: int = 0
    raw_postings_seen: int = 0
    valid_rows_retained: int = 0
    duplicate_postings_skipped: int = 0
    request_attempts: int = 0
    retry_attempts: int = 0
    last_transport_error: str = ""
    malformed_postings_skipped: int = 0
    schema_error_postings_skipped: int = 0


class OracleHcmSource:
    """Collect public Candidate Experience requisitions through Oracle REST."""

    name = "oracle_hcm"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if not 1 <= max_attempts <= DEFAULT_MAX_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be between 1 and {DEFAULT_MAX_ATTEMPTS}"
            )
        if not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {DEFAULT_PAGE_SIZE}")
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        self._request_json = request_json
        self._sleeper = sleeper
        self._jitter = jitter
        self._max_attempts = max_attempts
        self.page_size = page_size
        self.max_pages = max_pages
        self.last_response_metadata: dict[str, object] = {}
        self.last_diagnostics = OracleHcmDiagnostics()
        self._reset_diagnostics()

    @staticmethod
    def endpoint(host: str, site: str, *, limit: int, offset: int) -> str:
        query = urlencode(
            {
                "onlyData": "true",
                "expand": (
                    "requisitionList.secondaryLocations,"
                    "requisitionList.otherWorkLocations,"
                    "requisitionList.workLocation"
                ),
                "finder": (
                    f"findReqs;siteNumber={site},limit={limit},offset={offset}"
                ),
            }
        )
        return (
            f"https://{host}/hcmRestApi/resources/latest/"
            f"recruitingCEJobRequisitions?{query}"
        )

    @staticmethod
    def posting_url(host: str, site: str, posting_id: str) -> str:
        return (
            f"https://{host}/hcmUI/CandidateExperience/en/sites/"
            f"{quote(site, safe='')}/job/{quote(posting_id, safe='')}"
        )

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._reset_diagnostics()
        host, site = _required_config(company)
        postings_to_parse: list[Any] = []
        posting_index_by_id: dict[str, int] = {}
        seen_offsets: set[int] = set()
        seen_pages: set[str] = set()
        offset = 0

        for page_number in range(1, self.max_pages + 1):
            if offset in seen_offsets:
                raise SourceSchemaError(
                    f"oracle_hcm pagination requested duplicate offset {offset}"
                )
            seen_offsets.add(offset)
            self._pages_requested += 1
            payload = self._fetch_page(
                self.endpoint(host, site, limit=self.page_size, offset=offset)
            )
            postings, returned_offset, total = self._page(
                payload, expected_offset=offset
            )
            raw_count = len(postings)
            self._raw_postings_seen += raw_count

            if postings:
                fingerprint = page_fingerprint(postings)
                if fingerprint in seen_pages:
                    raise SourceSchemaError(
                        "oracle_hcm returned a repeated pagination page"
                    )
                seen_pages.add(fingerprint)

            duplicate_count = 0
            new_records = 0
            for posting in postings:
                posting_id = _posting_id(posting)
                existing_index = posting_index_by_id.get(posting_id) if posting_id else None
                if existing_index is not None:
                    duplicate_count += 1
                    if (
                        not _has_required_posting_fields(postings_to_parse[existing_index])
                        and _has_required_posting_fields(posting)
                    ):
                        postings_to_parse[existing_index] = posting
                        new_records += 1
                    continue
                if posting_id:
                    posting_index_by_id[posting_id] = len(postings_to_parse)
                postings_to_parse.append(posting)
                new_records += 1
            self._duplicate_postings_skipped += duplicate_count

            next_offset = returned_offset + raw_count
            if next_offset > total:
                raise SourceSchemaError(
                    "oracle_hcm pagination returned more postings than TotalJobsCount"
                )
            if not postings:
                if returned_offset < total:
                    raise SourceSchemaError(
                        "oracle_hcm pagination ended before TotalJobsCount"
                    )
                return self._complete_fetch(
                    postings_to_parse, company, host=host, site=site
                )
            if next_offset >= total:
                return self._complete_fetch(
                    postings_to_parse, company, host=host, site=site
                )
            if new_records == 0:
                raise SourceSchemaError(
                    "oracle_hcm pagination did not yield any new postings"
                )
            if next_offset <= offset:
                raise SourceSchemaError("oracle_hcm pagination did not advance")
            offset = next_offset

            if page_number == self.max_pages:
                raise SourceSchemaError(
                    "oracle_hcm reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable Oracle HCM pagination state")

    def _complete_fetch(
        self,
        postings: list[Any],
        company: CompanyCfg,
        *,
        host: str,
        site: str,
    ) -> list[dict]:
        rows = self._parse_postings(postings, company, host=host, site=site)
        self._finish(rows)
        return rows

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        self._reset_diagnostics()
        host, site = _required_config(company)
        postings, _, _ = self._page(payload)
        self._raw_postings_seen = len(postings)
        rows = self._parse_postings(postings, company, host=host, site=site)
        self._finish(rows)
        return rows

    def _fetch_page(self, url: str) -> Any:
        request_json = self._request_json or _get_json
        for attempt in range(1, self._max_attempts + 1):
            self._request_attempts += 1
            try:
                response = request_json(url, self.name)
                if isinstance(response, JsonHttpResponse):
                    self.last_response_metadata = dict(response.metadata)
                    return response.payload
                self.last_response_metadata = {}
                return response
            except SourceFetchError as exc:
                self._last_transport_error = exc.error_code
                exc.attempt_count = attempt
                exc.response_metadata.update(
                    {"attempt": attempt, "max_attempts": self._max_attempts}
                )
                will_retry = exc.retryable and attempt < self._max_attempts
                _log_transport_failure(exc, attempt, self._max_attempts, will_retry)
                if not will_retry:
                    self._snapshot_diagnostics()
                    raise
                self._retry_attempts += 1
                retry_after = exc.response_metadata.get("retry_after_seconds")
                self._sleeper(
                    workday_retry_delay(
                        attempt,
                        jitter=self._jitter,
                        retry_after=(
                            retry_after
                            if isinstance(retry_after, (int, float))
                            else None
                        ),
                    )
                )
        raise AssertionError("unreachable Oracle HCM retry state")

    def _page(
        self,
        payload: Any,
        *,
        expected_offset: int | None = None,
    ) -> tuple[list, int, int]:
        if not isinstance(payload, dict):
            raise SourceSchemaError("oracle_hcm expected a JSON object")
        items = payload.get("items")
        if not isinstance(items, list) or len(items) != 1:
            raise SourceSchemaError(
                "oracle_hcm expected items to contain one search result"
            )
        page = items[0]
        if not isinstance(page, dict):
            raise SourceSchemaError("oracle_hcm expected the search result to be an object")
        postings = page.get("requisitionList")
        if not isinstance(postings, list):
            raise SourceSchemaError("oracle_hcm expected requisitionList to be a list")
        returned_offset = _nonnegative_integer(page.get("Offset"), "Offset")
        limit = _positive_integer(page.get("Limit"), "Limit")
        total = _nonnegative_integer(page.get("TotalJobsCount"), "TotalJobsCount")
        if expected_offset is not None and returned_offset != expected_offset:
            raise SourceSchemaError(
                f"oracle_hcm returned offset {returned_offset}; expected {expected_offset}"
            )
        if len(postings) > limit:
            raise SourceSchemaError("oracle_hcm requisitionList exceeds the reported Limit")
        return postings, returned_offset, total

    def _parse_postings(
        self,
        postings: list,
        company: CompanyCfg,
        *,
        host: str,
        site: str,
    ) -> list[dict]:
        return parse_records(
            postings,
            lambda posting: self._parse_posting(
                posting, company, host=host, site=site
            ),
            source_name=self.name,
            company_name=company.name,
            diagnostics=self._record_parse_diagnostics,
        )

    def _record_parse_diagnostics(
        self,
        malformed_rows: int,
        schema_error_rows: int,
        _reason_codes,
    ) -> None:
        self._malformed_postings_skipped += max(0, int(malformed_rows))
        self._schema_error_postings_skipped += max(0, int(schema_error_rows))

    def _parse_posting(
        self,
        posting: Any,
        company: CompanyCfg,
        *,
        host: str,
        site: str,
    ) -> dict:
        if not isinstance(posting, dict):
            raise SourceSchemaError(
                "oracle_hcm expected each requisition to be an object"
            )
        posting_id = _posting_id(posting)
        title = str(posting.get("Title") or "").strip()
        if not posting_id or not title:
            raise SourceSchemaError(
                "oracle_hcm requisition missing required Id or Title"
            )

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=_locations(posting),
            description=html_to_text(
                posting.get("ExternalResponsibilitiesStr")
                or posting.get("ExternalDescriptionStr")
                or posting.get("ShortDescriptionStr")
            ),
            requirements=html_to_text(posting.get("ExternalQualificationsStr")),
            source_url=self.posting_url(host, site, posting_id),
            date_posted=iso_date(
                posting.get("ExternalPostedStartDate") or posting.get("PostedDate")
            ),
            deadline=iso_date(
                posting.get("ExternalPostedEndDate") or posting.get("PostingEndDate")
            ),
            remote_status=str(posting.get("WorkplaceType") or "").strip(),
            internship_type=_joined_values(
                posting,
                ("Category", "JobType", "WorkerType", "JobSchedule", "ContractType"),
            ),
            extra={
                "source_id": posting_id,
                "source_requisition_id": posting_id,
                "source_system": self.name,
                "source_scope": f"{host}:{site}",
                "oracle_hcm_host": host,
                "oracle_hcm_site": site,
                "active": _active(posting),
            },
        )

    def _reset_diagnostics(self) -> None:
        self._pages_requested = 0
        self._raw_postings_seen = 0
        self._duplicate_postings_skipped = 0
        self._request_attempts = 0
        self._retry_attempts = 0
        self._last_transport_error = ""
        self._malformed_postings_skipped = 0
        self._schema_error_postings_skipped = 0
        self.last_response_metadata = {}
        self.last_diagnostics = OracleHcmDiagnostics()

    def _snapshot_diagnostics(self, *, valid_rows: int = 0) -> None:
        self.last_diagnostics = OracleHcmDiagnostics(
            pages_requested=self._pages_requested,
            raw_postings_seen=self._raw_postings_seen,
            valid_rows_retained=valid_rows,
            duplicate_postings_skipped=self._duplicate_postings_skipped,
            request_attempts=self._request_attempts,
            retry_attempts=self._retry_attempts,
            last_transport_error=self._last_transport_error,
            malformed_postings_skipped=self._malformed_postings_skipped,
            schema_error_postings_skipped=self._schema_error_postings_skipped,
        )

    def _finish(self, rows: list[dict]) -> None:
        self._snapshot_diagnostics(valid_rows=len(rows))


def _get_json(url: str, source_name: str) -> JsonHttpResponse:
    return get_json_response(url, source_name)


def _required_config(company: CompanyCfg) -> tuple[str, str]:
    host = str(company.oracle_hcm_host or "").strip().casefold()
    site = str(company.oracle_hcm_site or "").strip()
    if not host:
        raise SourceError(f"oracle_hcm requires oracle_hcm_host for {company.name}")
    if not site:
        raise SourceError(f"oracle_hcm requires oracle_hcm_site for {company.name}")
    return host, site


def _posting_id(posting: Any) -> str:
    if not isinstance(posting, dict):
        return ""
    return str(posting.get("Id") or "").strip()


def _has_required_posting_fields(posting: Any) -> bool:
    return bool(
        isinstance(posting, dict)
        and _posting_id(posting)
        and str(posting.get("Title") or "").strip()
    )


def _locations(posting: dict) -> str:
    values: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, dict):
            value = next(
                (
                    value.get(key)
                    for key in ("Name", "LocationName", "FullName", "DisplayName")
                    if value.get(key)
                ),
                "",
            )
        text = str(value or "").strip()
        if text and text.casefold() not in {item.casefold() for item in values}:
            values.append(text)

    add(posting.get("PrimaryLocation"))
    for field in ("secondaryLocations", "otherWorkLocations"):
        locations = posting.get(field)
        if locations in (None, ""):
            continue
        if not isinstance(locations, list):
            raise SourceSchemaError(f"oracle_hcm expected {field} to be a list")
        for location in locations:
            add(location)
    return "; ".join(values)


def _joined_values(posting: dict, fields: tuple[str, ...]) -> str:
    values: list[str] = []
    for field in fields:
        value = str(posting.get(field) or "").strip()
        if value and value.casefold() not in {item.casefold() for item in values}:
            values.append(value)
    return "; ".join(values)


def _active(posting: dict) -> bool:
    for key in ("Active", "IsActive"):
        if isinstance(posting.get(key), bool):
            return bool(posting[key])
    status = str(posting.get("Status") or posting.get("PostingStatus") or "").strip()
    if status and re.search(r"\b(?:closed|inactive|filled|cancelled|canceled)\b", status, re.I):
        return False
    return True


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceSchemaError(
            f"oracle_hcm expected {field} to be a nonnegative integer"
        )
    return value


def _positive_integer(value: Any, field: str) -> int:
    result = _nonnegative_integer(value, field)
    if result == 0:
        raise SourceSchemaError(f"oracle_hcm expected {field} to be positive")
    return result


def _log_transport_failure(
    error: SourceFetchError,
    attempt: int,
    max_attempts: int,
    will_retry: bool,
) -> None:
    LOGGER.warning(
        "Oracle HCM transport %s: code=%s status=%s attempt=%d/%d transient=%s",
        "retry" if will_retry else "failure",
        error.error_code,
        error.status_code if error.status_code is not None else "none",
        attempt,
        max_attempts,
        "yes" if error.retryable else "no",
    )
