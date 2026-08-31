"""UKG (UltiPro) Recruiting public job-board source adapter.

The public board answers anonymous JSON POSTs with an authoritative
``totalCount`` plus ``Top``/``Skip`` offset pagination, so completeness is
proven from the board's own count rather than from repeated whole-board
snapshots. A single-request crawl is atomic; a multi-page crawl additionally
verifies its identity set against one reverse-ordered pass, because offset
pagination alone cannot detect a mid-crawl insert and delete that leaves the
total unchanged.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceError,
    SourceSchemaError,
)
from watcher.sources.direct import DirectRecordAdapter
from watcher.sources.parsing import page_fingerprint
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import iso_date, make_row
from watcher.sources.transport import post_json_response

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 200
MAX_PAGE_SIZE = 200

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_TENANT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?")
_MAX_FIELD_LENGTH = 500

# The board honors PostedDate ordering only; other sort properties fall back to
# the default descending order. The reverse pass therefore uses the ascending
# posted-date order, which is a genuinely independent traversal.
_ORDER_DESCENDING = ("postedDateDesc", "PostedDate", False)
_ORDER_ASCENDING = ("postedDateAsc", "PostedDate", True)


@dataclass(frozen=True)
class _SearchPage:
    # Records arrive unvalidated: a malformed entry must be skipped and
    # diagnosed by the shared parser, never crash page fingerprinting.
    records: tuple[Any, ...]
    total_count: int

    @property
    def fingerprint(self) -> str:
        return page_fingerprint(list(self.records))


class UkgSource(DirectRecordAdapter):
    """Fully enumerate one anonymous UKG Recruiting public job board."""

    name = "ukg"

    def __init__(
        self,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        request_json: Callable[[str, dict], JsonHttpResponse] | None = None,
    ) -> None:
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self._request_json = request_json
        self.page_size = page_size
        self.max_pages = max_pages
        self.pages_requested = 0
        self.verification_pages_requested = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def endpoint(host: str, tenant: str, board_id: str) -> str:
        return (
            f"https://{host}/{tenant}/JobBoard/{board_id}"
            "/JobBoardView/LoadSearchResults"
        )

    @staticmethod
    def board_url(host: str, tenant: str, board_id: str) -> str:
        return f"https://{host}/{tenant}/JobBoard/{board_id}/"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self.pages_requested = 0
        self.verification_pages_requested = 0
        self.last_response_metadata = {}
        host, tenant, board_id = _required_config(company)
        endpoint = self.endpoint(host, tenant, board_id)

        rows, identities, pages = self._enumerate(
            company,
            endpoint=endpoint,
            host=host,
            tenant=tenant,
            board_id=board_id,
            order=_ORDER_DESCENDING,
        )
        # One request is atomic, so offset skew cannot apply to it. A crawl that
        # spanned several offsets is verified against the opposite ordering.
        if pages > 1:
            self._verify_reverse_order(
                company,
                endpoint=endpoint,
                host=host,
                tenant=tenant,
                board_id=board_id,
                expected=identities,
            )
        return self._finish(rows)

    def _enumerate(
        self,
        company: CompanyCfg,
        *,
        endpoint: str,
        host: str,
        tenant: str,
        board_id: str,
        order: tuple[str, str, bool],
        collect_rows: bool = True,
    ) -> tuple[list[dict], frozenset[str], int]:
        expected_total: int | None = None
        raw_seen = 0
        seen_pages: set[str] = set()
        rows: list[dict] = []
        rows_by_id: dict[str, dict] = {}
        requisitions: dict[str, str] = {}
        ids_by_url: dict[str, str] = {}

        for page_number in range(1, self.max_pages + 1):
            skip = (page_number - 1) * self.page_size
            page = self._fetch_page(endpoint, skip=skip, order=order)
            if collect_rows:
                self.pages_requested += 1
            else:
                self.verification_pages_requested += 1

            if expected_total is None:
                expected_total = page.total_count
            elif page.total_count != expected_total:
                raise SourceSchemaError("ukg totalCount changed during pagination")

            if expected_total == 0:
                if page.records or page_number != 1:
                    raise SourceSchemaError(
                        "ukg zero-result response was inconsistent"
                    )
                return [], frozenset(), page_number
            if not page.records:
                raise SourceSchemaError(
                    "ukg pagination ended before the reported totalCount"
                )

            fingerprint = page.fingerprint
            if fingerprint in seen_pages:
                raise SourceSchemaError("ukg returned a repeated pagination page")
            seen_pages.add(fingerprint)

            raw_seen += len(page.records)
            if raw_seen > expected_total:
                raise SourceSchemaError(
                    "ukg returned more records than the reported totalCount"
                )
            if raw_seen < expected_total and len(page.records) != self.page_size:
                raise SourceSchemaError("ukg pagination ended prematurely")

            parsed = self._parse_direct_records(
                list(page.records),
                company,
                lambda record: _parse_posting(
                    record,
                    company,
                    host=host,
                    tenant=tenant,
                    board_id=board_id,
                ),
            )
            for row in parsed:
                extra = row["extra"]
                native_id = str(extra["ukg_native_id"])
                requisition = str(extra.get("ukg_requisition_number") or "")
                source_url = row["source_url"]
                if native_id in rows_by_id:
                    raise SourceSchemaError("ukg returned a duplicate posting Id")
                if requisition:
                    other = requisitions.get(requisition)
                    if other is not None and other != native_id:
                        raise SourceSchemaError(
                            "ukg returned one RequisitionNumber for conflicting Ids"
                        )
                    requisitions[requisition] = native_id
                other_id = ids_by_url.get(source_url)
                if other_id is not None and other_id != native_id:
                    raise SourceSchemaError(
                        "ukg returned one posting URL for conflicting Ids"
                    )
                rows_by_id[native_id] = row
                ids_by_url[source_url] = native_id
                if collect_rows:
                    rows.append(row)

            if raw_seen == expected_total:
                return rows, frozenset(rows_by_id), page_number
            if page_number == self.max_pages:
                raise SourceSchemaError(
                    "ukg reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable UKG pagination state")

    def _verify_reverse_order(
        self,
        company: CompanyCfg,
        *,
        endpoint: str,
        host: str,
        tenant: str,
        board_id: str,
        expected: frozenset[str],
    ) -> None:
        _rows, identities, _pages = self._enumerate(
            company,
            endpoint=endpoint,
            host=host,
            tenant=tenant,
            board_id=board_id,
            order=_ORDER_ASCENDING,
            collect_rows=False,
        )
        if identities != expected:
            raise SourceSchemaError(
                "ukg reverse-ordered pass did not agree on the posting identity set"
            )

    def _fetch_page(
        self,
        endpoint: str,
        *,
        skip: int,
        order: tuple[str, str, bool],
    ) -> _SearchPage:
        payload = _search_payload(self.page_size, skip, order)

        def attempt() -> JsonHttpResponse:
            if self._request_json is not None:
                return self._request_json(endpoint, payload)
            return post_json_response(endpoint, payload, self.name)

        response = self._retrier.run(attempt)
        self.last_response_metadata = dict(response.metadata)
        return _search_page(response.payload, page_size=self.page_size)

    def _finish(self, rows: list[dict]) -> list[dict]:
        parse_loss = bool(
            self._diagnostic_malformed_rows or self._diagnostic_schema_rows
        )
        recovered = bool(self.retry_attempts)
        reasons = ("request_retry_recovered",) if recovered else ()
        self._finish_direct_diagnostics(
            rows,
            failed_request_count=self.retry_attempts,
            incomplete=parse_loss,
            degraded=True if recovered else None,
            complete=True if recovered and not parse_loss else None,
            reason_codes=reasons,
        )
        return rows


def _required_config(company: CompanyCfg) -> tuple[str, str, str]:
    host = str(company.ukg_host or "").strip().casefold()
    tenant = str(company.ukg_tenant or "").strip()
    board_id = str(company.ukg_board_id or "").strip().casefold()
    if not host:
        raise SourceError(f"ukg requires ukg_host for {company.name}")
    if not _TENANT.fullmatch(tenant):
        raise SourceError(f"ukg requires a valid ukg_tenant for {company.name}")
    if not _UUID.fullmatch(board_id):
        raise SourceError(f"ukg requires a valid ukg_board_id for {company.name}")
    return host, tenant, board_id


def _search_payload(
    top: int,
    skip: int,
    order: tuple[str, str, bool],
) -> dict[str, object]:
    value, prop, ascending = order
    return {
        "opportunitySearch": {
            "Top": top,
            "Skip": skip,
            "QueryString": "",
            "OrderBy": [
                {"Value": value, "PropertyName": prop, "Ascending": ascending}
            ],
            "Filters": [],
        },
        "matchCriteria": {
            "PreferredJobs": [],
            "Educations": [],
            "LicenseAndCertifications": [],
            "Skills": [],
            "hasNoLicenses": False,
            "SkippedSkills": [],
        },
    }


def _search_page(payload: Any, *, page_size: int) -> _SearchPage:
    if not isinstance(payload, dict):
        raise SourceSchemaError("ukg expected a JSON object")
    total = payload.get("totalCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SourceSchemaError(
            "ukg expected totalCount to be a nonnegative integer"
        )
    opportunities = payload.get("opportunities")
    if not isinstance(opportunities, list):
        raise SourceSchemaError("ukg expected opportunities to be a list")
    if not isinstance(payload.get("locations", []), list):
        raise SourceSchemaError("ukg expected locations to be a list")
    if len(opportunities) > page_size:
        raise SourceSchemaError("ukg returned more records than the requested page")
    return _SearchPage(tuple(opportunities), total)


def _parse_posting(
    posting: Any,
    company: CompanyCfg,
    *,
    host: str,
    tenant: str,
    board_id: str,
) -> dict:
    if not isinstance(posting, Mapping):
        raise SourceSchemaError("ukg expected each opportunity to be an object")
    native_id = str(posting.get("Id") or "").strip().casefold()
    title = _text(posting.get("Title"), "Title")
    if not _UUID.fullmatch(native_id) or not title:
        raise SourceSchemaError("ukg posting missing a valid Id or Title")
    requisition = _text(posting.get("RequisitionNumber"), "RequisitionNumber")
    location, countries, states = _locations(posting.get("Locations"))
    source_id = f"ukg:{host}:{tenant}:{board_id}:{native_id}"
    query = urlencode((("opportunityId", native_id),))
    source_url = (
        f"https://{host}/{tenant}/JobBoard/{board_id}/OpportunityDetail?{query}"
    )
    extra = {
        "source_id": source_id,
        "source_requisition_id": source_id,
        "source_system": "ukg",
        "source_scope": f"{host}:{tenant}:{board_id}",
        "ukg_native_id": native_id,
        "ukg_tenant": tenant,
        "ukg_board_id": board_id,
        "job_category": _text(posting.get("JobCategoryName"), "JobCategoryName"),
        "active": True,
    }
    if requisition:
        extra["ukg_requisition_number"] = requisition
    if countries:
        extra["location_countries"] = countries
    if states:
        extra["location_states"] = states
    full_time = posting.get("FullTime")
    if isinstance(full_time, bool):
        extra["full_time"] = full_time
    return make_row(
        source="direct",
        source_adapter="ukg",
        company=company.name,
        title=title,
        location=location,
        description=_text(posting.get("BriefDescription"), "BriefDescription", 20_000),
        date_posted=_posted_date(posting.get("PostedDate")),
        source_url=source_url,
        extra=extra,
    )


def _text(value: Any, field: str, limit: int = _MAX_FIELD_LENGTH) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError(f"ukg {field} must be a string or null")
    return " ".join(value.split())[:limit]


def _posted_date(value: Any) -> str:
    """Return the normalized posting date, never inventing one."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError("ukg PostedDate must be a string or null")
    return iso_date(value.strip())


def _locations(value: Any) -> tuple[str, str, str]:
    """Return the joined location text plus bounded country/state metadata.

    Country and state come from the board's structured ``Address`` block; they
    are never derived from one another.
    """

    if value is None:
        return "", "", ""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SourceSchemaError("ukg Locations must be a list")
    labels: list[str] = []
    countries: list[str] = []
    states: list[str] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise SourceSchemaError("ukg Locations entries must be objects")
        address = entry.get("Address")
        if address is None:
            address = {}
        if not isinstance(address, Mapping):
            raise SourceSchemaError("ukg Location Address must be an object or null")
        city = _text(address.get("City"), "City", 120)
        state = _named(address.get("State"), "State")
        country = _named(address.get("Country"), "Country")
        parts = [part for part in (city, state, country) if part]
        label = ", ".join(parts) or _text(
            entry.get("LocalizedDescription"), "LocalizedDescription", 120
        )
        if label and label not in labels:
            labels.append(label)
        _append_unique(countries, country)
        _append_unique(states, state)
    return (
        "; ".join(labels)[:_MAX_FIELD_LENGTH],
        "; ".join(countries)[:_MAX_FIELD_LENGTH],
        "; ".join(states)[:_MAX_FIELD_LENGTH],
    )


def _named(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, Mapping):
        raise SourceSchemaError(f"ukg {field} must be an object or null")
    return _text(value.get("Name"), f"{field}.Name", 120)


def _append_unique(target: list[str], value: str) -> None:
    if value and value not in target:
        target.append(value)
