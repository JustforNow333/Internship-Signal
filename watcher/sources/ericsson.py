"""Complete first-party Ericsson careers inventory.

Ericsson's official careers navigation sends every job search to
``jobs.ericsson.com``. Its current PCSX frontend publishes an anonymous,
unfiltered search contract with an exact total, fixed ten-row pagination,
stable PCSX and ATS IDs, and posting-specific URLs. This adapter keeps that
provider behavior Ericsson-local because the generic Eightfold adapter remains
limited to the separately verified legacy contract.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from watcher.config import CompanyCfg
from watcher.sources.contracts import JsonHttpResponse, SourceError, SourceSchemaError
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import iso_date, make_row
from watcher.sources.transport import get_json_response


HOST = "jobs.ericsson.com"
DOMAIN = "ericsson.com"
SOURCE_URL = f"https://{HOST}/careers"
SEARCH_URL = f"https://{HOST}/api/pcsx/search"
PAGE_SIZE = 10
DEFAULT_MAX_PAGES = 200
DEFAULT_MAX_SNAPSHOT_PASSES = 3
DEFAULT_PAGE_DELAY_SECONDS = 0.05

_PCSX_ID = re.compile(r"[1-9][0-9]{0,19}")
_ATS_ID = re.compile(r"[1-9][0-9]{0,17}")


class _SnapshotUnstable(SourceSchemaError):
    """A complete Ericsson inventory could not be captured in one pass."""


@dataclass(frozen=True)
class _Snapshot:
    rows: tuple[dict, ...]
    identities: frozenset[str]
    requisition_ids: frozenset[str]
    total: int


class EricssonSource(DirectDiagnosticsMixin):
    """Enumerate Ericsson's complete anonymous global careers inventory."""

    name = "ericsson"

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
                f"ericsson max_pages must be between 1 and {DEFAULT_MAX_PAGES}"
            )
        if (
            type(max_snapshot_passes) is not int
            or not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES
        ):
            raise ValueError(
                "ericsson max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        if (
            isinstance(page_delay_seconds, bool)
            or not isinstance(page_delay_seconds, (int, float))
            or not 0 <= page_delay_seconds <= 5
        ):
            raise ValueError("ericsson page_delay_seconds must be between 0 and 5")
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
    def endpoint(*, start: int = 0) -> str:
        if type(start) is not int or start < 0:
            raise ValueError("ericsson start must be a nonnegative integer")
        query = urlencode(
            (
                ("domain", DOMAIN),
                ("query", ""),
                ("location", ""),
                ("start", start),
            )
        )
        return f"{SEARCH_URL}?{query}"

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
                and snapshot.requisition_ids == previous.requisition_ids
            ):
                return self._finish(list(snapshot.rows))
            previous = snapshot

        detail = f": {last_instability}" if last_instability is not None else ""
        raise SourceSchemaError(
            "ericsson snapshot did not stabilize within the bounded pass limit"
            f"{detail}"
        )

    def _snapshot(self, company: CompanyCfg) -> _Snapshot:
        rows: list[dict] = []
        identities: set[str] = set()
        requisition_ids: set[str] = set()
        urls: set[str] = set()
        expected_total: int | None = None
        raw_count = 0

        for page_number in range(1, self.max_pages + 2):
            if self.pages_requested and self.page_delay_seconds:
                self._sleeper(self.page_delay_seconds)
            self.pages_requested += 1
            payload = self._fetch_page(self.endpoint(start=raw_count))
            records, total = _page(payload)

            if expected_total is None:
                expected_total = total
                listing_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                if listing_pages > self.max_pages:
                    raise SourceSchemaError(
                        "ericsson advertised total exceeds the maximum page safeguard"
                    )
            elif total != expected_total:
                raise _SnapshotUnstable(
                    "ericsson total changed during pagination"
                )

            if total == 0:
                if page_number != 1 or records or raw_count:
                    raise SourceSchemaError(
                        "ericsson zero-result response was inconsistent"
                    )
                return _Snapshot((), frozenset(), frozenset(), 0)

            if raw_count == total:
                if records:
                    raise SourceSchemaError("ericsson terminal page was not empty")
                if not (
                    len(rows)
                    == len(identities)
                    == len(requisition_ids)
                    == len(urls)
                    == total
                ):
                    raise _SnapshotUnstable(
                        "ericsson final unique counts did not match the total"
                    )
                return _Snapshot(
                    tuple(rows),
                    frozenset(identities),
                    frozenset(requisition_ids),
                    total,
                )

            if not records:
                raise _SnapshotUnstable(
                    "ericsson pagination ended before the advertised total"
                )
            expected_size = min(PAGE_SIZE, total - raw_count)
            if len(records) != expected_size:
                raise _SnapshotUnstable(
                    "ericsson listing page count disagreed with the total"
                )
            raw_count += len(records)
            if raw_count > total:
                raise SourceSchemaError(
                    "ericsson returned more postings than its total"
                )

            for record in records:
                row = _row(record, company)
                identity = str(row["extra"]["ericsson_pcsx_id"])
                requisition_id = str(row["extra"]["source_requisition_id"])
                source_url = str(row["source_url"])
                if identity in identities:
                    raise _SnapshotUnstable(
                        "ericsson returned a duplicate posting ID"
                    )
                if requisition_id in requisition_ids:
                    raise _SnapshotUnstable(
                        "ericsson returned a duplicate ATS job ID"
                    )
                if source_url in urls:
                    raise _SnapshotUnstable(
                        "ericsson returned a duplicate posting URL"
                    )
                identities.add(identity)
                requisition_ids.add(requisition_id)
                urls.add(source_url)
                rows.append(row)

        raise SourceSchemaError("ericsson exhausted its maximum page safeguard")

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
            f"ericsson requires the official global careers listing for {company.name}"
        )


def _page(payload: object) -> tuple[tuple[Mapping[str, object], ...], int]:
    if not isinstance(payload, Mapping):
        raise SourceSchemaError("ericsson expected an object response")
    if payload.get("status") != 200:
        raise SourceSchemaError("ericsson listing API reported failure")
    if payload.get("error") != {"message": "", "body": ""}:
        raise SourceSchemaError("ericsson successful listing response contained an error")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise SourceSchemaError("ericsson listing response is missing data")
    if data.get("sortBy") != "hot":
        raise SourceSchemaError("ericsson listing changed its default sort mode")
    if data.get("appliedFilters") != {}:
        raise SourceSchemaError("ericsson listing applied unexpected filters")
    results_metadata = data.get("resultsMetaData")
    if (
        not isinstance(results_metadata, Mapping)
        or results_metadata.get("usedFuzzSearch") is not False
    ):
        raise SourceSchemaError("ericsson unfiltered listing enabled fuzzy search")

    total = data.get("count")
    records = data.get("positions")
    if type(total) is not int or total < 0:
        raise SourceSchemaError("ericsson count must be a nonnegative integer")
    if not isinstance(records, list):
        raise SourceSchemaError("ericsson positions must be a list")
    if len(records) > PAGE_SIZE:
        raise SourceSchemaError("ericsson page exceeded the fixed page size")
    if any(not isinstance(record, Mapping) for record in records):
        raise SourceSchemaError("ericsson listing contained a malformed posting")
    return tuple(records), total


def _row(record: Mapping[str, object], company: CompanyCfg) -> dict:
    raw_id = record.get("id")
    if type(raw_id) is not int or not _PCSX_ID.fullmatch(str(raw_id)):
        raise SourceSchemaError("ericsson posting ID was invalid")
    identity = str(raw_id)
    ats_id = _required_id(record.get("atsJobId"), label="ATS job ID")
    display_id = _required_id(record.get("displayJobId"), label="display job ID")
    if display_id != ats_id:
        raise SourceSchemaError("ericsson ATS and display job IDs disagreed")
    title = _required_text(record.get("name"), label="posting title")
    department = _required_text(record.get("department"), label="posting department")
    locations = _text_list(record.get("locations"), label="posting locations")
    posted_timestamp = _positive_timestamp(
        record.get("postedTs"), label="posting timestamp"
    )
    position_path = _required_text(record.get("positionUrl"), label="posting URL")
    expected_path = f"/careers/job/{identity}"
    try:
        parsed_path = urlsplit(position_path)
    except ValueError as exc:
        raise SourceSchemaError("ericsson posting URL was invalid") from exc
    if (
        position_path != expected_path
        or parsed_path.scheme
        or parsed_path.netloc
        or parsed_path.query
        or parsed_path.fragment
    ):
        raise SourceSchemaError("ericsson posting URL did not match its ID")

    job_functions = _optional_text_list(
        record.get("efcustomTextJobfuntionAdvancefilterpcs"),
        label="posting job functions",
    )
    creation = record.get("creationTs")
    if creation is not None:
        creation = _positive_timestamp(creation, label="creation timestamp")
    work_location = _optional_text(
        record.get("workLocationOption"), label="work location option"
    )

    return make_row(
        source="direct",
        source_adapter="ericsson",
        company=company.name,
        title=title,
        location=" | ".join(locations),
        source_url=f"{SOURCE_URL}/job/{identity}",
        date_posted=iso_date(posted_timestamp),
        remote_status=work_location,
        extra={
            "source_id": identity,
            "source_requisition_id": f"ericsson:{ats_id}",
            "source_system": "ericsson_eightfold_pcsx",
            "ericsson_pcsx_id": identity,
            "ericsson_ats_job_id": ats_id,
            "department": department,
            "job_functions": list(job_functions),
            "source_created_date": iso_date(creation),
            "active": True,
        },
    )


def _required_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _ATS_ID.fullmatch(value.strip()):
        raise SourceSchemaError(f"ericsson {label} was invalid")
    return value.strip()


def _required_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceSchemaError(f"ericsson {label} was invalid")
    return value.strip()


def _optional_text(value: object, *, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError(f"ericsson {label} was invalid")
    return value.strip()


def _text_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise SourceSchemaError(f"ericsson {label} were invalid")
    items = tuple(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    )
    if len(items) != len(value) or len(items) != len(set(items)):
        raise SourceSchemaError(f"ericsson {label} were invalid")
    return items


def _optional_text_list(value: object, *, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return _text_list(value, label=label)


def _positive_timestamp(value: object, *, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise SourceSchemaError(f"ericsson {label} was invalid")
    return value
