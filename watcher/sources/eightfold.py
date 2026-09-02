"""Netflix's verified legacy Eightfold anonymous job-board adapter.

This intentionally supports only the legacy ``/api/apply/v2/jobs`` contract.
PCSX tenants have different, currently untrustworthy pagination semantics and
are outside this adapter's contract.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlencode, urlsplit

from watcher.config import CompanyCfg
from watcher.sources.contracts import JsonHttpResponse, SourceSchemaError
from watcher.sources.direct import DirectRecordAdapter
from watcher.sources.parsing import page_fingerprint
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import iso_date, make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import get_json_response

LEGACY_VARIANT = "legacy"
PAGE_SIZE = 10
DEFAULT_MAX_PAGES = 500
DEFAULT_PAGE_DELAY_SECONDS = 1.0
_POSTING_ID = re.compile(r"[1-9][0-9]{0,19}")


class EightfoldSource(DirectRecordAdapter):
    """Completely enumerate one legacy Eightfold board, failing closed."""

    name = "eightfold"

    def __init__(
        self,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_pages: int = DEFAULT_MAX_PAGES,
        page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS,
        request_json: Callable[[str, str], JsonHttpResponse] | None = None,
    ) -> None:
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        if not 0 <= page_delay_seconds <= 5:
            raise ValueError("page_delay_seconds must be between 0 and 5")
        self._sleeper = sleeper
        self._request_json = request_json
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts, max_crawl_retries=5),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.max_pages = max_pages
        self.page_delay_seconds = float(page_delay_seconds)
        self.pages_requested = 0
        self.raw_count = 0
        self.unique_count = 0
        self.advertised_total: int | None = None
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def endpoint(host: str, domain: str, start: int) -> str:
        query = urlencode({"domain": domain, "start": start, "num": PAGE_SIZE})
        return f"https://{host}/api/apply/v2/jobs?{query}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self.pages_requested = 0
        self.raw_count = 0
        self.unique_count = 0
        self.advertised_total = None
        host, domain = _required_config(company)
        rows: list[dict] = []
        rows_by_id: dict[str, dict] = {}
        ids_by_url: dict[str, str] = {}
        seen_pages: set[str] = set()

        for page_number in range(1, self.max_pages + 2):
            if page_number > 1 and self.page_delay_seconds:
                self._sleeper(self.page_delay_seconds)
            payload = self._fetch_page(host, domain, self.raw_count)
            records, total = _page_records(payload, domain)
            self.pages_requested += 1

            if self.advertised_total is None:
                self.advertised_total = total
                listing_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                if listing_pages > self.max_pages:
                    raise SourceSchemaError(
                        "eightfold advertised total exceeds the maximum page safeguard"
                    )
            elif total != self.advertised_total:
                raise SourceSchemaError("eightfold total changed during pagination")

            if total == 0:
                if page_number != 1 or records:
                    raise SourceSchemaError("eightfold zero-result response was inconsistent")
                return self._finish([])

            # A complete nonempty crawl requires one explicit terminal page.
            if self.raw_count == total:
                if records:
                    raise SourceSchemaError("eightfold terminal page was not empty")
                return self._finish(rows)
            if not records:
                raise SourceSchemaError(
                    "eightfold pagination ended before the advertised total"
                )
            if len(records) > PAGE_SIZE:
                raise SourceSchemaError("eightfold page exceeded the fixed page size")
            fingerprint = page_fingerprint(records)
            if fingerprint in seen_pages:
                raise SourceSchemaError("eightfold returned a repeated pagination page")
            seen_pages.add(fingerprint)

            expected_size = min(PAGE_SIZE, total - self.raw_count)
            if len(records) != expected_size:
                raise SourceSchemaError("eightfold returned invalid page arithmetic")
            self.raw_count += len(records)
            if self.raw_count > total:
                raise SourceSchemaError("eightfold returned more rows than its total")

            parsed = self._parse_direct_records(
                records,
                company,
                lambda record: _parse_posting(record, company, host=host, domain=domain),
            )
            if self._diagnostic_malformed_rows or self._diagnostic_schema_rows:
                raise SourceSchemaError("eightfold returned malformed posting records")
            for row in parsed:
                native_id = str(row["extra"]["eightfold_native_id"])
                old = rows_by_id.get(native_id)
                if old is not None:
                    label = "duplicate" if old == row else "conflicting"
                    raise SourceSchemaError(f"eightfold returned {label} posting IDs")
                other_id = ids_by_url.get(row["source_url"])
                if other_id is not None and other_id != native_id:
                    raise SourceSchemaError("eightfold returned one URL for conflicting IDs")
                rows_by_id[native_id] = row
                ids_by_url[row["source_url"]] = native_id
                rows.append(row)

        raise SourceSchemaError("eightfold exhausted its maximum page safeguard")

    def _fetch_page(self, host: str, domain: str, start: int) -> Any:
        url = self.endpoint(host, domain, start)

        def request() -> JsonHttpResponse:
            if self._request_json is not None:
                return self._request_json(url, self.name)
            return get_json_response(url, self.name)

        return self._retrier.run(request).payload

    def _finish(self, rows: list[dict]) -> list[dict]:
        if self.advertised_total is None or self.raw_count != self.advertised_total:
            raise SourceSchemaError("eightfold final raw count did not match its total")
        self.unique_count = len(rows)
        if self.unique_count != self.raw_count:
            raise SourceSchemaError("eightfold final unique count did not match its raw count")
        retries = self.retry_attempts
        self._finish_direct_diagnostics(
            rows,
            degraded=bool(retries),
            complete=not retries,
            reason_codes=("request_retry_recovered",) if retries else (),
        )
        return rows


def _required_config(company: CompanyCfg) -> tuple[str, str]:
    if company.eightfold_variant != LEGACY_VARIANT:
        raise SourceSchemaError("eightfold requires the legacy API variant")
    if not company.eightfold_host or not company.eightfold_domain:
        raise SourceSchemaError("eightfold requires host and domain")
    return company.eightfold_host, company.eightfold_domain


def _page_records(payload: Any, domain: str) -> tuple[list, int]:
    if not isinstance(payload, Mapping):
        raise SourceSchemaError("eightfold expected an object response")
    if payload.get("domain") != domain:
        raise SourceSchemaError("eightfold response domain did not match configuration")
    total = payload.get("count")
    records = payload.get("positions")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SourceSchemaError("eightfold count must be a nonnegative integer")
    if not isinstance(records, list):
        raise SourceSchemaError("eightfold positions must be a list")
    return records, total


def _parse_posting(
    record: Any,
    company: CompanyCfg,
    *,
    host: str,
    domain: str,
) -> dict:
    if not isinstance(record, Mapping):
        raise SourceSchemaError("eightfold posting must be an object")
    raw_id = record.get("id")
    if isinstance(raw_id, bool):
        raise SourceSchemaError("eightfold posting id was invalid")
    native_id = str(raw_id or "").strip()
    title = str(record.get("name") or "").strip()
    if not _POSTING_ID.fullmatch(native_id) or not title:
        raise SourceSchemaError("eightfold posting requires a valid id and title")
    expected_url = f"https://{host}/careers/job/{native_id}"
    source_url = str(record.get("canonicalPositionUrl") or "").strip()
    try:
        parsed_url = urlsplit(source_url)
    except ValueError as exc:
        raise SourceSchemaError("eightfold posting URL was invalid") from exc
    if (
        source_url != expected_url
        or parsed_url.hostname != host
        or parsed_url.username
        or parsed_url.password
    ):
        raise SourceSchemaError("eightfold posting URL did not match its configured host and id")
    locations = record.get("locations")
    if locations is None:
        location = str(record.get("location") or "").strip()
    elif isinstance(locations, list) and all(isinstance(item, str) for item in locations):
        location = " | ".join(item.strip() for item in locations if item.strip())
    else:
        raise SourceSchemaError("eightfold posting locations were malformed")
    description = record.get("job_description")
    if description is not None and not isinstance(description, str):
        raise SourceSchemaError("eightfold posting description was malformed")
    return make_row(
        company=company.name,
        title=title,
        location=location,
        description=html_to_text(description or ""),
        date_posted=iso_date(record.get("t_create")),
        source_url=source_url,
        source="direct",
        source_adapter="eightfold",
        extra={
            "eightfold_native_id": native_id,
            "eightfold_domain": domain,
            "eightfold_ats_job_id": str(record.get("ats_job_id") or "").strip(),
            "source_requisition_id": f"eightfold:{domain}:{native_id}",
            "department": str(record.get("department") or "").strip(),
            "business_unit": str(record.get("business_unit") or "").strip(),
        },
    )
