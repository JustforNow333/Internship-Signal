"""ByteDance's public careers API, scoped per careers portal.

ByteDance operates several careers portals over one public search API. Each
portal is its own company-scoped inventory with its own authoritative count, and
the portal is selected by the ``website-path`` request header the portal's own
site sends. This module models that verified contract; it is not a generic
careers or JSON-API abstraction, and it does not generalize beyond the portals
this repository configures explicitly.

Two portals are configured. TikTok is served from ``api.lifeattiktok.com`` under
``website-path: tiktok``; ByteDance is served from ``jobs.bytedance.com`` under
``website-path: en``. The portal is authoritative scope: a posting belongs to the
company whose portal published it, and nothing here infers a company from a
posting's contents. A small number of requisitions are genuinely cross-listed on
both portals, so each portal keeps its own row for them and the watcher's
existing identity and dedupe rules decide what that means downstream.

The API answers a form of ``{"code": 0, "data": {"count": N, "job_post_list":
[...]}}``. ``count`` is advertised on every response and the service returns
``min(limit, count)`` rows, so one bounded request sized from a previously read
count can carry a whole portal while still proving its own completeness. Posting
date fields are present but always null, so no posting date is claimed.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceSchemaError, TextHttpResponse
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import make_row
from watcher.sources.transport import post_json_response


SEARCH_PATH = "/api/v1/public/supplier/search/job/posts"

# Each configured portal, with the host and header value its own site uses.
PORTAL_TIKTOK = "tiktok"
PORTAL_BYTEDANCE = "en"
PORTALS: dict[str, dict[str, str]] = {
    PORTAL_TIKTOK: {
        "host": "api.lifeattiktok.com",
        "website_path": PORTAL_TIKTOK,
        "origin": "https://lifeattiktok.com",
        "posting_base": "https://lifeattiktok.com/position",
    },
    PORTAL_BYTEDANCE: {
        "host": "jobs.bytedance.com",
        "website_path": PORTAL_BYTEDANCE,
        "origin": "https://joinbytedance.com",
        "posting_base": "https://joinbytedance.com/position",
    },
}

DEFAULT_MAX_SNAPSHOT_PASSES = 3
# A whole portal arrives in one bounded request, so the ceiling only has to sit
# safely above the largest inventory this source is configured to collect.
MAX_TOTAL_RESULTS = 50_000
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_FIELD_LENGTH = 20_000
_COUNT_PROBE_LIMIT = 1
_OK_CODE = 0


class _PortalSnapshotUnstable(SourceSchemaError):
    """One pass observed the portal changing under it and must be discarded."""


@dataclass(frozen=True)
class ByteDanceCareersDiagnostics:
    portal: str = ""
    requests_made: int = 0
    snapshot_passes_requested: int = 0
    advertised_count: int = 0
    raw_records_seen: int = 0
    retained_rows: int = 0
    request_attempts: int = 0
    retry_attempts: int = 0


@dataclass(frozen=True)
class _Posting:
    posting_id: str
    code: str
    title: str
    locations: tuple[str, ...]
    category: str
    recruit_type: str
    description: str
    requirements: str


@dataclass(frozen=True)
class _Snapshot:
    postings: tuple[_Posting, ...]
    count: int

    @property
    def identity(self) -> tuple:
        """Count plus id-keyed records, so ordering differences do not matter."""

        return (
            self.count,
            tuple(sorted((p.posting_id, p.title) for p in self.postings)),
        )


def portal_for(company: CompanyCfg) -> str:
    """Return the configured portal, refusing anything not explicitly supported."""

    portal = str(getattr(company, "bytedance_careers_portal", "") or "").strip()
    if portal not in PORTALS:
        raise SourceSchemaError(
            "bytedance careers company is not configured with a supported portal"
        )
    return portal


class ByteDanceCareersSource(DirectDiagnosticsMixin):
    """Enumerate one configured ByteDance careers portal completely."""

    name = "bytedance_careers"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str, dict, dict], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_snapshot_passes: int = DEFAULT_MAX_SNAPSHOT_PASSES,
    ) -> None:
        if not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES:
            raise ValueError(
                "max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        self._request_json = request_json
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
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
    def last_diagnostics(self) -> ByteDanceCareersDiagnostics:
        return ByteDanceCareersDiagnostics(
            portal=self._portal,
            requests_made=self._requests_made,
            snapshot_passes_requested=self._snapshot_passes_requested,
            advertised_count=self._advertised_count,
            raw_records_seen=self._raw_records_seen,
            retained_rows=self._retained_rows,
            request_attempts=self.request_attempts,
            retry_attempts=self.retry_attempts,
        )

    @staticmethod
    def endpoint(portal: str) -> str:
        if portal not in PORTALS:
            raise SourceSchemaError("bytedance careers portal is not supported")
        return f"https://{PORTALS[portal]['host']}{SEARCH_PATH}"

    @staticmethod
    def posting_url(portal: str, posting_id: str) -> str:
        return f"{PORTALS[portal]['posting_base']}/{posting_id}/detail"

    @staticmethod
    def request_body(*, offset: int, limit: int) -> dict[str, Any]:
        """Return the portal's verified public search body."""

        return {
            "keyword": "",
            "limit": limit,
            "offset": offset,
            "job_category_id_list": [],
            "tag_id_list": [],
            "location_code_list": [],
            "subject_id_list": [],
            "recruitment_id_list": [],
            "job_function_id_list": [],
            "storefront_id_list": [],
            "portal_type": 6,
            "portal_entrance": 1,
        }

    @staticmethod
    def request_headers(portal: str) -> dict[str, str]:
        """Return the portal-selecting headers the portal's own site sends."""

        config = PORTALS[portal]
        return {
            "website-path": config["website_path"],
            "accept-language": "en-US",
            "origin": config["origin"],
        }

    def fetch(self, company: CompanyCfg) -> list[dict]:
        portal = portal_for(company)
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self._reset_counters()
        self._portal = portal

        previous: _Snapshot | None = None
        stable: _Snapshot | None = None
        for _pass_number in range(1, self.max_snapshot_passes + 1):
            self._snapshot_passes_requested += 1
            try:
                snapshot = self._fetch_snapshot(portal)
            except _PortalSnapshotUnstable:
                previous = None
                continue
            if previous is not None and snapshot.identity == previous.identity:
                stable = snapshot
                break
            previous = snapshot

        if stable is None:
            raise SourceSchemaError(
                "bytedance careers snapshot did not stabilize within the pass limit"
            )

        self._advertised_count = stable.count
        self._raw_records_seen = len(stable.postings)
        rows = [self._row(posting, portal, company) for posting in stable.postings]
        self._retained_rows = len(rows)
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

    def _fetch_snapshot(self, portal: str) -> _Snapshot:
        """Return one whole-portal pass, or discard it if the portal moved.

        The count is read first so the inventory request can be sized from it,
        and the pass is only accepted when the returned rows, their unique ids,
        and the count all agree and the boundary past the end is empty.
        """

        probe_count, probe_rows = self._search(portal, offset=0, limit=_COUNT_PROBE_LIMIT)
        if probe_count > MAX_TOTAL_RESULTS:
            raise SourceSchemaError(
                "bytedance careers count exceeds the collection safeguard"
            )
        if probe_count == 0:
            if probe_rows:
                raise SourceSchemaError(
                    "bytedance careers reported an empty portal while returning records"
                )
            return _Snapshot((), 0)

        count, records = self._search(portal, offset=0, limit=probe_count)
        if count != probe_count:
            raise _PortalSnapshotUnstable(
                "bytedance careers count changed during collection"
            )
        if len(records) != count:
            raise _PortalSnapshotUnstable(
                "bytedance careers returned a row count other than its advertised count"
            )

        postings: list[_Posting] = []
        seen: dict[str, str] = {}
        for record in records:
            posting = _posting(record)
            if posting.posting_id in seen:
                raise SourceSchemaError(
                    "bytedance careers returned a duplicate posting id"
                )
            seen[posting.posting_id] = posting.title
            postings.append(posting)

        self._verify_terminal_boundary(portal, count)
        return _Snapshot(tuple(postings), count)

    def _verify_terminal_boundary(self, portal: str, count: int) -> None:
        """A request past the end must be empty while still advertising the count.

        That is the portal's truncation signal, and confirming it keeps an empty
        response from ever being mistaken for a healthy empty portal.
        """

        terminal_count, terminal_rows = self._search(
            portal, offset=count, limit=_COUNT_PROBE_LIMIT
        )
        if terminal_count != count:
            raise _PortalSnapshotUnstable(
                "bytedance careers count changed at the terminal boundary"
            )
        if terminal_rows:
            raise SourceSchemaError(
                "bytedance careers returned records past its advertised count"
            )

    def _search(self, portal: str, *, offset: int, limit: int) -> tuple[int, list]:
        payload = self._request(
            self.endpoint(portal),
            self.request_body(offset=offset, limit=limit),
            self.request_headers(portal),
        )
        return _search_payload(payload)

    def _row(self, posting: _Posting, portal: str, company: CompanyCfg) -> dict:
        source_id = f"bytedance_careers:{portal}:{posting.posting_id}"
        extra: dict[str, Any] = {
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "bytedance_careers",
            "bytedance_careers_portal": portal,
            "bytedance_posting_id": posting.posting_id,
            "active": True,
        }
        if posting.code:
            extra["bytedance_posting_code"] = posting.code
        if posting.category:
            extra["job_category"] = posting.category
        if posting.recruit_type:
            extra["recruit_type"] = posting.recruit_type
        return make_row(
            source="direct",
            source_adapter="bytedance_careers",
            company=company.name,
            title=posting.title,
            location="; ".join(posting.locations),
            description=posting.description,
            requirements=posting.requirements,
            source_url=self.posting_url(portal, posting.posting_id),
            extra=extra,
        )

    def _request(self, url: str, body: dict, headers: dict) -> Any:
        self._requests_made += 1

        def attempt() -> Any:
            if self._request_json is not None:
                response = self._request_json(url, self.name, body, headers)
            else:
                response = post_json_response(
                    url,
                    body,
                    self.name,
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

    def _reset_counters(self) -> None:
        self._portal = ""
        self._requests_made = 0
        self._snapshot_passes_requested = 0
        self._advertised_count = 0
        self._raw_records_seen = 0
        self._retained_rows = 0
        self.last_response_metadata = {}


# --- provider-local payload decoding ---------------------------------------


def _search_payload(payload: Any) -> tuple[int, list]:
    """Return one response's advertised count and records, failing closed.

    The service reports application-level failures inside a successful HTTP
    response, so a non-zero ``code`` is an error rather than an empty portal.
    """

    if not isinstance(payload, dict):
        raise SourceSchemaError("bytedance careers response was not an object")
    code = payload.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        raise SourceSchemaError("bytedance careers response lacked a numeric code")
    if code != _OK_CODE:
        raise SourceSchemaError("bytedance careers response reported an error code")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise SourceSchemaError("bytedance careers response lacked its data object")

    count = data.get("count")
    if isinstance(count, bool) or not isinstance(count, int):
        raise SourceSchemaError("bytedance careers count was not an integer")
    if count < 0:
        raise SourceSchemaError("bytedance careers count was negative")

    records = data.get("job_post_list")
    if records is None:
        records = []
    if not isinstance(records, list):
        raise SourceSchemaError("bytedance careers job list was not a list")
    return count, records


def _posting(record: Any) -> _Posting:
    if not isinstance(record, dict):
        raise SourceSchemaError("bytedance careers record was not an object")
    posting_id = record.get("id")
    if not isinstance(posting_id, str) or not posting_id.strip().isdigit():
        raise SourceSchemaError("bytedance careers record lacked a valid posting id")
    title = _text(record.get("title"), "title")
    if not title:
        raise SourceSchemaError("bytedance careers record lacked a title")
    return _Posting(
        posting_id=posting_id.strip(),
        code=_text(record.get("code"), "code"),
        title=title,
        locations=_locations(record.get("city_info")),
        category=_named(record.get("job_category"), "job_category"),
        recruit_type=_named(record.get("recruit_type"), "recruit_type"),
        description=_text(record.get("description"), "description"),
        requirements=_text(record.get("requirement"), "requirement"),
    )


def _locations(value: Any) -> tuple[str, ...]:
    """Return the posting's concrete locations from its city information."""

    if value is None:
        return ()
    entries = value if isinstance(value, list) else [value]
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise SourceSchemaError("bytedance careers city entry was malformed")
        label = _text(entry.get("en_name"), "en_name") or _text(
            entry.get("name"), "name"
        )
        if label and label not in out:
            out.append(label)
    return tuple(out)


def _named(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, dict):
        raise SourceSchemaError(f"bytedance careers {field} was malformed")
    return _text(value.get("en_name"), field) or _text(value.get("name"), field)


def _text(value: Any, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str):
        raise SourceSchemaError(f"bytedance careers {field} was not a string")
    return " ".join(value.split())[:_MAX_FIELD_LENGTH]
