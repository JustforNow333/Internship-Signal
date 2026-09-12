"""Complete first-party MediaTek careers inventory.

MediaTek's corporate site points at one official careers portal. That portal's
own published frontend bundle calls an anonymous tRPC query, ``job.getJobs``,
which returns the organization-wide posting inventory with an exact
``total_items``, one-based pagination, an explicit ``status`` marker, and an
empty terminal page past the last one. Posting identities are stable entity
prefixed codes that resolve to canonical ``/en/jobs/{id}`` detail routes.

The behavior stays MediaTek-specific: the tRPC input schema, the swapped
``label``/``code`` orientation of its property vocabulary, and the locale enum
are unique to this portal. The three published locales translate one shared
inventory rather than partitioning it, so a single locale is enumerated and the
adapter never treats locale as a scope dimension.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from watcher.config import CompanyCfg
from watcher.sources.contracts import JsonHttpResponse, SourceError, SourceSchemaError
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import iso_date, make_row
from watcher.sources.transport import get_json_response


HOST = "careers.mediatek.com"
SOURCE_URL = f"https://{HOST}/en/jobs"
LISTING_URL = f"https://{HOST}/api/trpc/job.getJobs"
JOB_URL_PREFIX = f"https://{HOST}/en/jobs/"

# The portal's own client sends this locale enum; all three published locales
# report one shared inventory, so exactly one is enumerated.
LOCALE = "en_US"
SORT_BY = "publishedDate"
SORT_ORDER = "DESC"
PAGE_SIZE = 100
DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_SNAPSHOT_PASSES = 3
DEFAULT_PAGE_DELAY_SECONDS = 0.55

# Postings are an entity prefix plus a numeric code, always fifteen characters.
_POSTING_ID = re.compile(r"[A-Z]{3,4}[0-9]{11,12}")
_POSTING_ID_LENGTH = 15
_NUMERIC_CODE = re.compile(r"[0-9]+")

_EMPTY_FILTERS: Mapping[str, tuple[()]] = {
    "categorys": (),
    "workExperiences": (),
    "locations": (),
    "programs": (),
}


class _SnapshotUnstable(SourceSchemaError):
    """A complete MediaTek inventory could not be captured in one pass."""


@dataclass(frozen=True)
class _Snapshot:
    rows: tuple[dict, ...]
    identities: frozenset[str]
    total: int


class MediaTekSource(DirectDiagnosticsMixin):
    """Enumerate MediaTek's complete anonymous global careers inventory."""

    name = "mediatek"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_snapshot_passes: int = DEFAULT_MAX_SNAPSHOT_PASSES,
        page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS,
    ) -> None:
        if type(max_pages) is not int or not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(
                f"mediatek max_pages must be between 1 and {DEFAULT_MAX_PAGES}"
            )
        if (
            type(max_snapshot_passes) is not int
            or not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES
        ):
            raise ValueError(
                "mediatek max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        if (
            isinstance(page_delay_seconds, bool)
            or not isinstance(page_delay_seconds, (int, float))
            or not 0 <= page_delay_seconds <= 5
        ):
            raise ValueError("mediatek page_delay_seconds must be between 0 and 5")
        self._request_json = request_json
        self._sleeper = sleeper
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.max_pages = max_pages
        self.max_snapshot_passes = max_snapshot_passes
        self.page_delay_seconds = float(page_delay_seconds)
        self.pages_requested = 0
        self.snapshot_passes_requested = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def endpoint(*, page: int = 1, limit: int = PAGE_SIZE) -> str:
        """Build the unfiltered listing request the portal's own client sends."""

        if type(page) is not int or page < 1:
            raise ValueError("mediatek page must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= PAGE_SIZE:
            raise ValueError(
                f"mediatek limit must be between 1 and {PAGE_SIZE}"
            )
        payload = {
            "json": {
                "locales": LOCALE,
                "page": page,
                "jobQueryInfo": {},
                "filters": {key: [] for key in _EMPTY_FILTERS},
                "sortBy": SORT_BY,
                "order": SORT_ORDER,
                "limit": limit,
            }
        }
        query = urlencode(
            (("input", json.dumps(payload, separators=(",", ":"), sort_keys=True)),)
        )
        return f"{LISTING_URL}?{query}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self.pages_requested = 0
        self.snapshot_passes_requested = 0
        self.last_response_metadata = {}
        _require_config(company)

        previous: _Snapshot | None = None
        last_instability: _SnapshotUnstable | None = None
        for _pass_number in range(1, self.max_snapshot_passes + 1):
            self.snapshot_passes_requested += 1
            try:
                snapshot = self._snapshot(company)
            except _SnapshotUnstable as exc:
                last_instability = exc
                previous = None
                continue
            if (
                previous is not None
                and snapshot.total == previous.total
                and snapshot.identities == previous.identities
            ):
                return self._finish(list(snapshot.rows))
            previous = snapshot

        detail = f": {last_instability}" if last_instability is not None else ""
        raise SourceSchemaError(
            "mediatek snapshot did not stabilize within the bounded pass limit"
            f"{detail}"
        )

    def _snapshot(self, company: CompanyCfg) -> _Snapshot:
        rows: list[dict] = []
        identities: set[str] = set()
        urls: set[str] = set()
        expected_total: int | None = None

        for page_number in range(1, self.max_pages + 2):
            if self.pages_requested and self.page_delay_seconds:
                self._sleeper(self.page_delay_seconds)
            self.pages_requested += 1
            payload = self._fetch_page(self.endpoint(page=page_number))
            records, total = _page(payload, page_number)

            if expected_total is None:
                expected_total = total
                listing_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                if listing_pages > self.max_pages:
                    raise SourceSchemaError(
                        "mediatek advertised total exceeds the maximum page "
                        "safeguard"
                    )
            elif total != expected_total:
                raise _SnapshotUnstable("mediatek total changed during pagination")

            if total == 0:
                if page_number != 1 or records:
                    raise SourceSchemaError(
                        "mediatek zero-result response was inconsistent"
                    )
                return _Snapshot((), frozenset(), 0)

            if len(rows) == total:
                if records:
                    raise SourceSchemaError("mediatek terminal page was not empty")
                if not len(rows) == len(identities) == len(urls) == total:
                    raise _SnapshotUnstable(
                        "mediatek final unique counts did not match the total"
                    )
                return _Snapshot(tuple(rows), frozenset(identities), total)

            if not records:
                raise _SnapshotUnstable(
                    "mediatek pagination ended before the advertised total"
                )
            expected_size = min(PAGE_SIZE, total - len(rows))
            if len(records) != expected_size:
                raise _SnapshotUnstable(
                    "mediatek listing page count disagreed with the total"
                )

            for record in records:
                row = _row(record, company)
                identity = str(row["extra"]["source_id"])
                source_url = str(row["source_url"])
                if identity in identities:
                    raise _SnapshotUnstable(
                        "mediatek returned a duplicate posting ID"
                    )
                if source_url in urls:
                    raise _SnapshotUnstable(
                        "mediatek returned a duplicate posting URL"
                    )
                identities.add(identity)
                urls.add(source_url)
                rows.append(row)

            if len(rows) > total:
                raise SourceSchemaError(
                    "mediatek returned more postings than its total"
                )

        raise SourceSchemaError("mediatek exhausted its maximum page safeguard")

    def _fetch_page(self, url: str) -> object:
        request = self._request_json or get_json_response

        def attempt() -> object:
            response = request(url, self.name)
            if isinstance(response, JsonHttpResponse):
                self.last_response_metadata = dict(response.metadata)
                return response.payload
            self.last_response_metadata = {}
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
            "mediatek requires the official global careers listing for "
            f"{company.name}"
        )


def _page(
    payload: object, page_number: int
) -> tuple[tuple[Mapping[str, object], ...], int]:
    if not isinstance(payload, Mapping):
        raise SourceSchemaError("mediatek expected an object response")
    if "error" in payload:
        raise SourceSchemaError("mediatek listing API reported failure")
    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise SourceSchemaError("mediatek listing response is missing a result")
    data = result.get("data")
    if not isinstance(data, Mapping):
        raise SourceSchemaError("mediatek listing response is missing data")
    body = data.get("json")
    if not isinstance(body, Mapping):
        raise SourceSchemaError("mediatek listing response is missing its payload")
    if body.get("status") != "complete":
        raise SourceSchemaError("mediatek listing response was not complete")
    if body.get("message") is not None:
        raise SourceSchemaError(
            "mediatek successful listing response carried a message"
        )

    pagination = body.get("pagination")
    if not isinstance(pagination, Mapping):
        raise SourceSchemaError("mediatek listing response is missing pagination")
    total = pagination.get("total_items")
    total_pages = pagination.get("total_pages")
    current_page = pagination.get("current_page")
    if type(total) is not int or total < 0:
        raise SourceSchemaError("mediatek total_items must be a nonnegative integer")
    if current_page != page_number:
        raise SourceSchemaError("mediatek listing echoed a different page number")
    if type(total_pages) is not int or total_pages != (
        (total + PAGE_SIZE - 1) // PAGE_SIZE
    ):
        raise SourceSchemaError(
            "mediatek total_pages disagreed with its total at the fixed page size"
        )

    records = body.get("jobs")
    if not isinstance(records, list):
        raise SourceSchemaError("mediatek jobs must be a list")
    if len(records) > PAGE_SIZE:
        raise SourceSchemaError("mediatek page exceeded the fixed page size")
    if any(not isinstance(record, Mapping) for record in records):
        raise SourceSchemaError("mediatek listing contained a malformed posting")
    return tuple(records), total


def _row(record: Mapping[str, object], company: CompanyCfg) -> dict:
    identity = record.get("id")
    if (
        not isinstance(identity, str)
        or len(identity) != _POSTING_ID_LENGTH
        or not _POSTING_ID.fullmatch(identity)
    ):
        raise SourceSchemaError("mediatek posting ID was invalid")
    if record.get("jobPostStatus") != "posted":
        raise SourceSchemaError("mediatek listing returned an unposted posting")

    title = _required_text(record.get("title"), label="posting title")
    description = _required_text(record.get("description"), label="description")
    published = _required_text(record.get("publishedDate"), label="published date")
    date_posted = iso_date(published)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_posted):
        raise SourceSchemaError("mediatek published date was invalid")

    properties = record.get("properties")
    if not isinstance(properties, Mapping):
        raise SourceSchemaError("mediatek posting properties were invalid")

    # This portal stores its vocabulary with the human label and the numeric
    # code on opposite sides per property, so the readable side is selected by
    # shape rather than by key name.
    category = _vocabulary(properties.get("category"), label="category")
    work_experience = _vocabulary(
        properties.get("workExperience"), label="work experience"
    )
    location = _vocabulary(properties.get("location"), label="location")
    program = _vocabulary(properties.get("program"), label="program", optional=True)
    education = _education(properties.get("jobEducationInfos"))

    if not location:
        raise SourceSchemaError("mediatek posting location was missing")

    return make_row(
        source="direct",
        source_adapter="mediatek",
        company=company.name,
        title=title,
        location=location,
        description=description,
        source_url=f"{JOB_URL_PREFIX}{identity}",
        date_posted=date_posted,
        extra={
            "source_id": identity,
            "source_requisition_id": f"mediatek:{identity}",
            "source_system": "mediatek_careers_trpc",
            "mediatek_posting_id": identity,
            "category": category,
            "work_experience": work_experience,
            "program": program,
            "education": list(education),
            "active": True,
        },
    )


def _vocabulary(
    value: object, *, label: str, optional: bool = False
) -> str:
    """Return the human-readable side of a ``label``/``code`` property pair."""

    if value is None:
        if optional:
            return ""
        raise SourceSchemaError(f"mediatek {label} was missing")
    if not isinstance(value, Mapping) or set(value) != {"label", "code"}:
        raise SourceSchemaError(f"mediatek {label} had an unexpected shape")

    raw_label = value.get("label")
    raw_code = value.get("code")
    if raw_label is None and raw_code is None:
        if optional:
            return ""
        raise SourceSchemaError(f"mediatek {label} was missing")
    if not isinstance(raw_label, str) or not isinstance(raw_code, str):
        raise SourceSchemaError(f"mediatek {label} was invalid")

    left, right = raw_label.strip(), raw_code.strip()
    if not left or not right:
        raise SourceSchemaError(f"mediatek {label} was invalid")
    left_numeric = bool(_NUMERIC_CODE.fullmatch(left))
    right_numeric = bool(_NUMERIC_CODE.fullmatch(right))
    if left_numeric == right_numeric:
        raise SourceSchemaError(
            f"mediatek {label} did not pair one code with one readable value"
        )
    return right if left_numeric else left


def _education(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SourceSchemaError("mediatek education requirements were invalid")
    entries: list[str] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise SourceSchemaError("mediatek education requirements were invalid")
        degree = _optional_text(
            item.get("educationDegree"), label="education degree"
        )
        major = _optional_text(item.get("educationMajor"), label="education major")
        entry = " ".join(part for part in (degree, major) if part)
        if entry and entry not in entries:
            entries.append(entry)
    return tuple(entries)


def _required_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceSchemaError(f"mediatek {label} was invalid")
    return value.strip()


def _optional_text(value: object, *, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError(f"mediatek {label} was invalid")
    return value.strip()
