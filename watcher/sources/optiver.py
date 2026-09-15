"""Complete Optiver inventory from its first-party paginated jobs API.

Optiver's official jobs page publishes its unfiltered API endpoint, total, and
initial records in the server-rendered ``JobsFiltered`` component.  The same
first-party frontend implements offset pagination with ``from`` and ``size``.
Each collection pass reconciles that rendered representation with an exact,
exhausted API crawl, and two consecutive complete passes must match before any
rows are returned.

The pagination and rendered-component contract are Optiver-specific.  This
adapter deliberately does not turn them into a generic JSON pagination layer.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from internship_signal.domain.identity import norm_url
from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceError,
    SourceSchemaError,
    TextHttpResponse,
)
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import make_row
from watcher.sources.transport import get_json_response, get_text_response


HOST = "www.optiver.com"
SOURCE_URL = f"https://{HOST}/join-us/jobs/"
API_URL = f"https://{HOST}/en/api/v1/jobs"
PAGE_SIZE = 16
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_SNAPSHOT_PASSES = 3
DEFAULT_PAGE_DELAY_SECONDS = 0.55
MAX_JOBS_PAGE_BYTES = 8 * 1024 * 1024
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024

_COMPONENT_MARKER = "React.createElement(Components.JobsFiltered,"
_RECORD_FIELDS = frozenset(
    {
        "reactComponentName",
        "serverOnlyRender",
        "clientOnlyRender",
        "title",
        "location",
        "experience",
        "domain",
        "href",
        "jobClickTracking",
        "componentID",
        "culture",
        "anchorId",
    }
)
_TRACKING_FIELDS = frozenset(
    {"vacancyName", "vacancyOffice", "vacancyDomain", "vacancyLevel"}
)
_JOB_PATH = re.compile(
    r"^/join-us/jobs/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+/?$"
)


class _SnapshotUnstable(SourceSchemaError):
    """One otherwise complete inventory changed while it was collected."""


@dataclass(frozen=True)
class _RenderedInventory:
    records: tuple[Mapping[str, object], ...]
    total: int


@dataclass(frozen=True)
class _Snapshot:
    rows: tuple[dict, ...]
    identities: frozenset[str]
    canonical_urls: frozenset[str]
    total: int


class OptiverSource(DirectDiagnosticsMixin):
    """Enumerate Optiver's complete anonymous global careers inventory."""

    name = "optiver"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        request_text: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_snapshot_passes: int = DEFAULT_MAX_SNAPSHOT_PASSES,
        page_size: int = PAGE_SIZE,
        page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS,
    ) -> None:
        if type(max_pages) is not int or not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(
                f"optiver max_pages must be between 1 and {DEFAULT_MAX_PAGES}"
            )
        if (
            type(max_snapshot_passes) is not int
            or not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES
        ):
            raise ValueError(
                "optiver max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        if type(page_size) is not int or not 1 <= page_size <= PAGE_SIZE:
            raise ValueError(f"optiver page_size must be between 1 and {PAGE_SIZE}")
        if (
            isinstance(page_delay_seconds, bool)
            or not isinstance(page_delay_seconds, (int, float))
            or not 0 <= page_delay_seconds <= 5
        ):
            raise ValueError("optiver page_delay_seconds must be between 0 and 5")

        self._request_json = request_json
        self._request_text = request_text
        self._sleeper = sleeper
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.max_pages = max_pages
        self.max_snapshot_passes = max_snapshot_passes
        self.page_size = page_size
        self.page_delay_seconds = float(page_delay_seconds)
        self.pages_requested = 0
        self.snapshot_passes_requested = 0
        self.last_response_metadata: dict[str, Mapping[str, object]] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def endpoint(*, offset: int = 0, size: int = PAGE_SIZE) -> str:
        if type(offset) is not int or offset < 0:
            raise ValueError("optiver offset must be a nonnegative integer")
        if type(size) is not int or not 1 <= size <= PAGE_SIZE:
            raise ValueError(f"optiver size must be between 1 and {PAGE_SIZE}")
        return f"{API_URL}?from={offset}&size={size}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self.pages_requested = 0
        self.snapshot_passes_requested = 0
        self.last_response_metadata = {}
        _require_config(company)

        previous: _Snapshot | None = None
        last_instability: _SnapshotUnstable | None = None
        for pass_number in range(1, self.max_snapshot_passes + 1):
            if pass_number > 1 and self.page_delay_seconds:
                self._sleeper(self.page_delay_seconds)
            self.snapshot_passes_requested += 1
            try:
                snapshot = self._snapshot(company)
            except _SnapshotUnstable as exc:
                previous = None
                last_instability = exc
                continue
            if snapshot == previous:
                return self._finish(list(snapshot.rows))
            previous = snapshot

        detail = f": {last_instability}" if last_instability is not None else ""
        raise SourceSchemaError(
            "optiver snapshot did not stabilize within the bounded pass limit"
            f"{detail}"
        )

    def _snapshot(self, company: CompanyCfg) -> _Snapshot:
        rendered = _rendered_inventory(self._fetch_text(SOURCE_URL), self.page_size)
        rows: list[dict] = []
        identities: set[str] = set()
        urls: set[str] = set()
        page_fingerprints: set[str] = set()
        offset = 0

        while True:
            if self.pages_requested and self.page_delay_seconds:
                self._sleeper(self.page_delay_seconds)
            self.pages_requested += 1
            payload = self._fetch_json(self.endpoint(offset=offset, size=self.page_size))
            records, total = _page(payload, self.page_size)

            listing_pages = (total + self.page_size - 1) // self.page_size
            if listing_pages > self.max_pages:
                raise SourceSchemaError(
                    "optiver advertised total exceeds the maximum page safeguard"
                )
            if total != rendered.total:
                raise _SnapshotUnstable(
                    "optiver rendered inventory and API totals disagreed"
                )

            if offset == 0 and tuple(records) != rendered.records:
                raise _SnapshotUnstable(
                    "optiver rendered inventory and first API page disagreed"
                )

            if offset == total:
                if records:
                    raise SourceSchemaError("optiver terminal page was not empty")
                if not len(rows) == len(identities) == len(urls) == total:
                    raise _SnapshotUnstable(
                        "optiver final unique counts did not match the total"
                    )
                return _Snapshot(
                    tuple(rows), frozenset(identities), frozenset(urls), total
                )

            if offset > total:
                raise SourceSchemaError("optiver pagination exceeded its total")
            if not records:
                raise _SnapshotUnstable(
                    "optiver pagination ended before the advertised total"
                )
            expected_size = min(self.page_size, total - offset)
            if len(records) != expected_size:
                raise _SnapshotUnstable(
                    "optiver listing page count disagreed with the total"
                )

            fingerprint = json.dumps(
                records, ensure_ascii=True, separators=(",", ":"), sort_keys=True
            )
            if fingerprint in page_fingerprints:
                raise SourceSchemaError("optiver returned a repeated pagination page")
            page_fingerprints.add(fingerprint)

            for record in records:
                row = _row(record, company)
                identity = str(row["extra"]["source_id"])
                canonical_url = norm_url(str(row["source_url"]))
                if identity in identities:
                    raise SourceSchemaError("optiver returned a duplicate posting ID")
                if canonical_url in urls:
                    raise SourceSchemaError("optiver returned a duplicate posting URL")
                identities.add(identity)
                urls.add(canonical_url)
                rows.append(row)

            offset += len(records)
            if len(rows) > total:
                raise SourceSchemaError("optiver returned more postings than its total")
            if (offset + self.page_size - 1) // self.page_size > self.max_pages:
                raise SourceSchemaError("optiver exhausted its maximum page safeguard")

    def _fetch_json(self, url: str) -> object:
        request = self._request_json

        def attempt() -> object:
            response = (
                request(url, self.name)
                if request is not None
                else get_json_response(
                    url,
                    self.name,
                    max_response_bytes=MAX_API_RESPONSE_BYTES,
                )
            )
            if isinstance(response, JsonHttpResponse):
                self.last_response_metadata["api"] = dict(response.metadata)
                return response.payload
            self.last_response_metadata["api"] = {}
            return response

        return self._retrier.run(attempt)

    def _fetch_text(self, url: str) -> str:
        request = self._request_text

        def attempt() -> str:
            response = (
                request(url, self.name)
                if request is not None
                else get_text_response(
                    url,
                    self.name,
                    max_response_bytes=MAX_JOBS_PAGE_BYTES,
                )
            )
            if isinstance(response, TextHttpResponse):
                self.last_response_metadata["html"] = dict(response.metadata)
                return response.text
            self.last_response_metadata["html"] = {}
            if not isinstance(response, str):
                raise SourceSchemaError("optiver jobs page was not text")
            return response

        return self._retrier.run(attempt)

    def _finish(self, rows: list[dict]) -> list[dict]:
        recovered = bool(self.retry_attempts)
        self._finish_direct_diagnostics(
            rows,
            failed_request_count=self.retry_attempts,
            degraded=recovered,
            complete=True,
            reason_codes=("request_retry_recovered",) if recovered else (),
        )
        return rows


def _require_config(company: CompanyCfg) -> None:
    if str(company.source_url or "").strip() != SOURCE_URL:
        raise SourceError(
            f"optiver requires the official jobs inventory for {company.name}"
        )


def _rendered_inventory(html: str, page_size: int) -> _RenderedInventory:
    if not isinstance(html, str):
        raise SourceSchemaError("optiver jobs page was not text")
    if html.count(_COMPONENT_MARKER) != 1:
        raise SourceSchemaError(
            "optiver jobs page did not publish one rendered inventory"
        )
    start = html.index(_COMPONENT_MARKER) + len(_COMPONENT_MARKER)
    try:
        props, _end = json.JSONDecoder().raw_decode(html, start)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SourceSchemaError(
            "optiver rendered inventory properties were malformed"
        ) from exc
    if not isinstance(props, Mapping):
        raise SourceSchemaError("optiver rendered inventory was not an object")
    if props.get("apiEndpoint") != "/en/api/v1/jobs":
        raise SourceSchemaError(
            "optiver rendered inventory published an unexpected API endpoint"
        )
    total = props.get("totalCount")
    if type(total) is not int or total < 0:
        raise SourceSchemaError(
            "optiver rendered inventory total must be a nonnegative integer"
        )
    raw_records = props.get("items")
    if not isinstance(raw_records, list) or any(
        not isinstance(record, Mapping) for record in raw_records
    ):
        raise SourceSchemaError("optiver rendered inventory items were malformed")
    if len(raw_records) != min(page_size, total):
        raise SourceSchemaError(
            "optiver rendered inventory page count disagreed with its total"
        )
    records: list[Mapping[str, object]] = []
    for raw_record in raw_records:
        keys = set(raw_record)
        # The server-rendered serializer omits the API's null anchorId.  No
        # other field may disappear, and a present anchorId must still be null.
        if keys not in {_RECORD_FIELDS, _RECORD_FIELDS - {"anchorId"}}:
            raise SourceSchemaError(
                "optiver rendered inventory posting had an unexpected schema"
            )
        record = dict(raw_record)
        record.setdefault("anchorId", None)
        records.append(record)
    return _RenderedInventory(tuple(records), total)


def _page(
    payload: object, page_size: int
) -> tuple[tuple[Mapping[str, object], ...], int]:
    if not isinstance(payload, Mapping) or set(payload) != {"items", "totalCount"}:
        raise SourceSchemaError("optiver API response had an unexpected schema")
    total = payload.get("totalCount")
    if type(total) is not int or total < 0:
        raise SourceSchemaError("optiver totalCount must be a nonnegative integer")
    records = payload.get("items")
    if not isinstance(records, list) or any(
        not isinstance(record, Mapping) for record in records
    ):
        raise SourceSchemaError("optiver API items must be a list of objects")
    if len(records) > page_size:
        raise SourceSchemaError("optiver API page exceeded the requested size")
    return tuple(records), total


def _row(record: Mapping[str, object], company: CompanyCfg) -> dict:
    if set(record) != _RECORD_FIELDS:
        raise SourceSchemaError("optiver posting had an unexpected schema")
    identity = record.get("componentID")
    if type(identity) is not int or identity <= 0:
        raise SourceSchemaError("optiver posting ID was invalid")
    if record.get("reactComponentName") != "Components.JobsListItem":
        raise SourceSchemaError("optiver posting component type was invalid")
    if record.get("serverOnlyRender") is not False:
        raise SourceSchemaError("optiver posting server-render marker was invalid")
    if record.get("clientOnlyRender") is not False:
        raise SourceSchemaError("optiver posting client-render marker was invalid")
    if record.get("culture") != "en" or record.get("anchorId") is not None:
        raise SourceSchemaError("optiver posting locale metadata was invalid")

    title = _required_text(record.get("title"), "posting title")
    location = _required_text(record.get("location"), "posting location")
    experience = _required_text(record.get("experience"), "experience")
    domain = _required_text(record.get("domain"), "domain")
    href = _required_text(record.get("href"), "posting URL")
    parts = urlsplit(href)
    if (
        parts.scheme
        or parts.netloc
        or parts.query
        or parts.fragment
        or not _JOB_PATH.fullmatch(parts.path)
    ):
        raise SourceSchemaError("optiver posting URL was not canonical")
    canonical_url = f"https://{HOST}{parts.path.rstrip('/')}/"

    tracking = record.get("jobClickTracking")
    if not isinstance(tracking, Mapping) or set(tracking) != _TRACKING_FIELDS:
        raise SourceSchemaError("optiver posting tracking metadata was invalid")
    tracking_values = {
        key: _required_text(tracking.get(key), f"tracking {key}")
        for key in sorted(_TRACKING_FIELDS)
    }
    if tracking_values["vacancyName"] != title:
        raise SourceSchemaError("optiver posting title and tracking title disagreed")

    return make_row(
        source="direct",
        source_adapter="optiver",
        company=company.name,
        title=title,
        location=location,
        source_url=canonical_url,
        internship_type=experience,
        extra={
            "source_id": f"optiver:{identity}",
            "source_requisition_id": f"optiver:{identity}",
            "source_system": "optiver_careers_api",
            "optiver_component_id": identity,
            "domain": domain,
            "experience": experience,
            **tracking_values,
            "active": True,
        },
    )


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceSchemaError(f"optiver {label} was invalid")
    return value.strip()
