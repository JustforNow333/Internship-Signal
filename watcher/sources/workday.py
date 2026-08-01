"""Workday source adapter."""

from __future__ import annotations

import hashlib
import logging
import random
import re
import statistics
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from watcher.config import CompanyCfg, workday_min_interval_seconds
from watcher.sources.base import JsonHttpResponse, SourceError, SourceFetchError, SourceSchemaError, ensure_list, html_to_text, make_row, page_fingerprint, post_json, require_token

LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 10.0


@dataclass(frozen=True)
class WorkdayParseDiagnostics:
    raw_postings_seen: int = 0
    valid_rows_retained: int = 0
    malformed_postings_skipped: int = 0
    skip_reasons: tuple[tuple[str, int], ...] = ()
    request_attempts: int = 0
    retry_attempts: int = 0
    last_transport_error: str = ""


@dataclass(frozen=True)
class WorkdayStartRecord:
    """One in-memory monotonic tenant start with a safe company identifier."""

    company_identifier: str
    started_at: float


@dataclass(frozen=True)
class WorkdayStartOffset:
    task_identifier: str
    company_identifier: str
    offset_seconds: float


@dataclass(frozen=True)
class WorkdayStartTelemetry:
    """Private canary telemetry derived only from monotonic start records."""

    configured_start_interval_seconds: float
    start_count: int
    minimum_spacing_seconds: float | None
    median_spacing_seconds: float | None
    maximum_spacing_seconds: float | None
    pacing_violation_count: int
    start_offsets: tuple[WorkdayStartOffset, ...] = ()

    def as_dict(self) -> dict[str, object]:
        def rounded(value: float | None) -> float | None:
            return None if value is None else round(float(value), 6)

        return {
            "configured_start_interval_seconds": round(
                float(self.configured_start_interval_seconds), 6
            ),
            "start_count": int(self.start_count),
            "minimum_spacing_seconds": rounded(self.minimum_spacing_seconds),
            "median_spacing_seconds": rounded(self.median_spacing_seconds),
            "maximum_spacing_seconds": rounded(self.maximum_spacing_seconds),
            "pacing_violation_count": int(self.pacing_violation_count),
            "start_offsets": [
                {
                    "task_identifier": item.task_identifier,
                    "company_identifier": item.company_identifier,
                    "offset_seconds": rounded(item.offset_seconds),
                }
                for item in self.start_offsets
            ],
        }


def _safe_workday_company_identifier(value: object) -> str:
    """Return a bounded identifier that can never expose a URL or credential."""

    raw = str(value or "").strip()
    if not raw:
        return "unknown-company"
    if any(marker in raw for marker in ("://", "@", "/", "\\", "?", "#", "=")):
        digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"company-{digest}"
    safe = re.sub(r"[^A-Za-z0-9 .&'()+_-]+", "_", raw)
    safe = re.sub(r"\s+", " ", safe).strip(" ._-")[:80]
    return safe or "unknown-company"


def summarize_workday_starts(
    configured_interval_seconds: float,
    records: Iterable[WorkdayStartRecord],
) -> WorkdayStartTelemetry:
    """Calculate deterministic spacing and sanitized relative start offsets."""

    interval = max(0.0, float(configured_interval_seconds))
    ordered = sorted(
        (
            WorkdayStartRecord(
                company_identifier=_safe_workday_company_identifier(
                    record.company_identifier
                ),
                started_at=float(record.started_at),
            )
            for record in records
        ),
        key=lambda record: (record.started_at, record.company_identifier),
    )
    if not ordered:
        return WorkdayStartTelemetry(
            configured_start_interval_seconds=interval,
            start_count=0,
            minimum_spacing_seconds=None,
            median_spacing_seconds=None,
            maximum_spacing_seconds=None,
            pacing_violation_count=0,
        )

    first_started_at = ordered[0].started_at
    spacings = [
        current.started_at - previous.started_at
        for previous, current in zip(ordered, ordered[1:])
    ]
    offsets = tuple(
        WorkdayStartOffset(
            task_identifier=f"workday-start-{index:03d}",
            company_identifier=record.company_identifier,
            offset_seconds=max(0.0, record.started_at - first_started_at),
        )
        for index, record in enumerate(ordered, start=1)
    )
    return WorkdayStartTelemetry(
        configured_start_interval_seconds=interval,
        start_count=len(ordered),
        minimum_spacing_seconds=min(spacings) if spacings else None,
        median_spacing_seconds=(float(statistics.median(spacings)) if spacings else None),
        maximum_spacing_seconds=max(spacings) if spacings else None,
        pacing_violation_count=sum(1 for spacing in spacings if spacing < interval),
        start_offsets=offsets,
    )


@dataclass
class WorkdayPacer:
    """Pacing between tenant fetch starts, never between pages.

    One pacer instance paces the tenants that share it. Serial collection keeps
    the existing one-instance-per-adapter behavior; bounded concurrent
    collection shares a single pacer across worker threads so tenant pacing is
    never weakened. Threads sleep outside the lock, then recheck under the lock
    so delayed wakeups cannot bunch actual starts together.
    """

    min_interval_seconds: float
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    last_started_at: float | None = None

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._start_records: list[WorkdayStartRecord] = []

    def wait_for_tenant(self, company_identifier: object = "") -> float:
        total_delay = 0.0
        while True:
            with self._lock:
                now = float(self.clock())
                delay = 0.0
                if self.last_started_at is not None:
                    delay = max(
                        0.0,
                        self.min_interval_seconds - (now - self.last_started_at),
                    )
                if delay <= 0:
                    # This timestamp is the actual tenant start: pacing is done,
                    # and returning transfers control directly to adapter fetch.
                    self.last_started_at = now
                    self._start_records.append(
                        WorkdayStartRecord(
                            company_identifier=_safe_workday_company_identifier(
                                company_identifier
                            ),
                            started_at=now,
                        )
                    )
                    return total_delay
            # Never hold the scheduling/telemetry lock while sleeping. On wake,
            # recheck the latest actual start because another waiter may have
            # started first.
            self.sleeper(delay)
            total_delay += delay

    def start_records(self) -> tuple[WorkdayStartRecord, ...]:
        with self._lock:
            return tuple(self._start_records)

    def start_telemetry(self) -> WorkdayStartTelemetry:
        return summarize_workday_starts(
            self.min_interval_seconds,
            self.start_records(),
        )


class WorkdaySource:
    name = "workday"
    page_size = 20

    def __init__(
        self,
        *,
        min_interval_seconds: float | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] = random.uniform,
        request_json: Callable[[str, dict, str], Any] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        pacer: WorkdayPacer | None = None,
    ) -> None:
        interval = workday_min_interval_seconds(min_interval_seconds)
        if max_attempts < 1 or max_attempts > DEFAULT_MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {DEFAULT_MAX_ATTEMPTS}")
        self.last_diagnostics = WorkdayParseDiagnostics()
        self._request_attempts = 0
        self._retry_attempts = 0
        self._last_transport_error = ""
        self.last_response_metadata: dict[str, object] = {}
        self._sleeper = sleeper
        self._jitter = jitter
        self._request_json = request_json
        self._max_attempts = max_attempts
        # An injected pacer lets bounded concurrent collection share one tenant
        # pacer across worker threads without changing serial behavior.
        self._pacer = pacer or WorkdayPacer(interval, sleeper=sleeper, clock=clock)

    @staticmethod
    def endpoint(token: str, shard: str, site: str) -> str:
        return f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/jobs"

    @staticmethod
    def posting_url(token: str, shard: str, site: str, external_path: str) -> str:
        return f"https://{token}.{shard}.myworkdayjobs.com/{site}{external_path}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._reset_diagnostics()
        token = require_token(company, self.name)
        shard = _required(company.workday_shard, "workday_shard", company)
        site = _required(company.workday_site, "workday_site", company)
        self._pacer.wait_for_tenant(company.name)
        rows: list[dict] = []
        raw_postings_seen = 0
        skip_reasons: Counter[str] = Counter()
        offset = 0
        total = None
        seen_pages: set[str] = set()
        while True:
            payload = self._fetch_page(
                self.endpoint(token, shard, site),
                {"appliedFacets": {}, "limit": self.page_size, "offset": offset, "searchText": ""},
            )
            postings, total_found = self._page(payload)
            if postings:
                fingerprint = page_fingerprint(postings)
                if fingerprint in seen_pages:
                    raise SourceSchemaError("workday returned a repeated pagination page")
                seen_pages.add(fingerprint)
            raw_postings_seen += len(postings)
            page_rows, page_reasons = self._parse_postings(postings, company, token, shard, site)
            rows.extend(page_rows)
            skip_reasons.update(page_reasons)
            offset += len(postings)
            total = total_found if total_found is not None else total
            if not postings or (total is not None and offset >= total):
                break
        return self._finalize(rows, raw_postings_seen, skip_reasons, company)

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        self._reset_diagnostics()
        token = require_token(company, self.name)
        shard = _required(company.workday_shard, "workday_shard", company)
        site = _required(company.workday_site, "workday_site", company)
        postings, _ = self._page(payload)
        rows, skip_reasons = self._parse_postings(postings, company, token, shard, site)
        return self._finalize(rows, len(postings), skip_reasons, company)

    def probe_transport(self, company: CompanyCfg) -> tuple[Any, dict[str, object]]:
        """Fetch only the first page and return payload plus safe metadata."""

        self._reset_diagnostics()
        token = require_token(company, self.name)
        shard = _required(company.workday_shard, "workday_shard", company)
        site = _required(company.workday_site, "workday_site", company)
        self._pacer.wait_for_tenant(company.name)
        payload = self._fetch_page(
            self.endpoint(token, shard, site),
            {"appliedFacets": {}, "limit": self.page_size, "offset": 0, "searchText": ""},
        )
        self.last_diagnostics = self._diagnostics_snapshot()
        return payload, dict(self.last_response_metadata)

    def _fetch_page(self, url: str, payload: dict) -> Any:
        request_json = self._request_json or post_json
        for attempt in range(1, self._max_attempts + 1):
            self._request_attempts += 1
            try:
                response = request_json(url, payload, self.name)
                if isinstance(response, JsonHttpResponse):
                    self.last_response_metadata = dict(response.metadata)
                    self.last_diagnostics = self._diagnostics_snapshot()
                    return response.payload
                self.last_response_metadata = {}
                self.last_diagnostics = self._diagnostics_snapshot()
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
                    self.last_diagnostics = self._diagnostics_snapshot()
                    raise
                self._retry_attempts += 1
                retry_after = exc.response_metadata.get("retry_after_seconds")
                delay = workday_retry_delay(
                    attempt,
                    jitter=self._jitter,
                    retry_after=retry_after if isinstance(retry_after, (int, float)) else None,
                )
                self._sleeper(delay)

        raise AssertionError("unreachable Workday retry state")

    def _page(self, payload: Any) -> tuple[list, int | None]:
        if not isinstance(payload, dict):
            raise SourceSchemaError("workday expected a JSON object")
        postings = ensure_list(payload.get("jobPostings"), self.name, "jobPostings")
        total = payload.get("total")
        if total is not None and not isinstance(total, int):
            raise SourceSchemaError("workday expected total to be an integer")
        return postings, total

    def _parse_postings(
        self,
        postings: list,
        company: CompanyCfg,
        token: str,
        shard: str,
        site: str,
    ) -> tuple[list[dict], Counter[str]]:
        rows = []
        reasons: Counter[str] = Counter()
        for posting in postings:
            reason = _posting_skip_reason(posting)
            if reason:
                reasons[reason] += 1
                continue
            try:
                rows.append(self._parse_posting(posting, company, token, shard, site))
            except SourceSchemaError:
                reasons["posting_schema_error"] += 1
        return rows, reasons

    def _finalize(
        self,
        rows: list[dict],
        raw_postings_seen: int,
        skip_reasons: Counter[str],
        company: CompanyCfg,
    ) -> list[dict]:
        skipped = sum(skip_reasons.values())
        self.last_diagnostics = WorkdayParseDiagnostics(
            raw_postings_seen=raw_postings_seen,
            valid_rows_retained=len(rows),
            malformed_postings_skipped=skipped,
            skip_reasons=tuple(sorted(skip_reasons.items())),
            request_attempts=self._request_attempts,
            retry_attempts=self._retry_attempts,
            last_transport_error=self._last_transport_error,
        )
        if skipped:
            posting_word = "posting" if skipped == 1 else "postings"
            valid_word = "posting" if len(rows) == 1 else "postings"
            reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(skip_reasons.items()))
            company_name = _safe_company_name(company.name)
            LOGGER.warning(
                "Skipped %d malformed Workday %s for %s; %d valid %s retained. Reasons: %s",
                skipped,
                posting_word,
                company_name,
                len(rows),
                valid_word,
                reasons,
            )
        if raw_postings_seen > 0 and not rows:
            raise SourceSchemaError(
                f"workday received {raw_postings_seen} posting record(s) for "
                f"{_safe_company_name(company.name)} but none were valid"
            )
        return rows

    def _reset_diagnostics(self) -> None:
        self._request_attempts = 0
        self._retry_attempts = 0
        self._last_transport_error = ""
        self.last_response_metadata = {}
        self.last_diagnostics = WorkdayParseDiagnostics()

    def _diagnostics_snapshot(self) -> WorkdayParseDiagnostics:
        current = self.last_diagnostics
        return WorkdayParseDiagnostics(
            raw_postings_seen=current.raw_postings_seen,
            valid_rows_retained=current.valid_rows_retained,
            malformed_postings_skipped=current.malformed_postings_skipped,
            skip_reasons=current.skip_reasons,
            request_attempts=self._request_attempts,
            retry_attempts=self._retry_attempts,
            last_transport_error=self._last_transport_error,
        )

    def _parse_posting(self, posting: Any, company: CompanyCfg, token: str, shard: str, site: str) -> dict:
        if not isinstance(posting, dict):
            raise SourceSchemaError("workday expected each posting to be an object")

        title = str(posting.get("title") or "").strip()
        external_path = str(posting.get("externalPath") or "").strip()
        if not title or not external_path:
            raise SourceSchemaError("workday posting missing required title or externalPath")
        if not external_path.startswith("/"):
            external_path = f"/{external_path}"

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=str(posting.get("locationsText") or "").strip(),
            description=html_to_text(posting.get("jobDescription")),
            source_url=self.posting_url(token, shard, site, external_path),
            date_posted=str(posting.get("postedOn") or "").strip(),
            remote_status=_remote_status(posting),
            extra={
                "source_id": _source_id(posting),
                "source_requisition_id": _source_id(posting),
                "source_system": self.name,
                "external_path": external_path,
                "time_type": str(posting.get("timeType") or "").strip(),
                "workday_tenant": token,
                "workday_shard": shard,
                "workday_site": site,
            },
        )


def _posting_skip_reason(posting: Any) -> str | None:
    if not isinstance(posting, dict):
        return "posting_not_object"
    title = str(posting.get("title") or "").strip()
    external_path = str(posting.get("externalPath") or "").strip()
    if not title and not external_path:
        return "missing_title_and_external_path"
    if not title:
        return "missing_title"
    if not external_path:
        return "missing_external_path"
    return None


def _safe_company_name(value: Any) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "unknown")).strip()[:120] or "unknown"


def _required(value: str, field: str, company: CompanyCfg) -> str:
    value = str(value or "").strip()
    if not value:
        raise SourceError(f"workday requires {field} for {company.name}")
    return value


def _source_id(posting: dict) -> str:
    bullet_fields = posting.get("bulletFields")
    if isinstance(bullet_fields, list) and bullet_fields:
        return str(bullet_fields[0] or "").strip()
    return ""


def _remote_status(posting: dict) -> str:
    text = " ".join(
        str(value or "").lower()
        for value in (posting.get("title"), posting.get("locationsText"))
    )
    if "remote" in text:
        return "Remote"
    if "hybrid" in text:
        return "Hybrid"
    return ""


def workday_retry_delay(
    failed_attempt: int,
    *,
    jitter: Callable[[float, float], float] = random.uniform,
    retry_after: float | None = None,
) -> float:
    """Return the bounded delay before the next attempt.

    Failed attempt one yields roughly 1-2 seconds; failed attempt two yields
    roughly 3-5 seconds. A Retry-After value can raise the delay up to ten
    seconds but can never create an unbounded sleep.
    """

    if failed_attempt <= 1:
        backoff = 1.0 + max(0.0, min(1.0, float(jitter(0.0, 1.0))))
    else:
        backoff = 3.0 + max(0.0, min(2.0, float(jitter(0.0, 2.0))))
    if retry_after is not None:
        backoff = max(backoff, min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(retry_after))))
    return min(MAX_RETRY_AFTER_SECONDS, backoff)


def _log_transport_failure(
    error: SourceFetchError,
    attempt: int,
    max_attempts: int,
    will_retry: bool,
) -> None:
    metadata = error.response_metadata
    digest = str(metadata.get("body_sha256") or "")[:12] or "none"
    LOGGER.warning(
        "Workday transport %s: code=%s status=%s content_type=%s content_encoding=%s "
        "body_bytes=%s body_kind=%s body_sha256=%s attempt=%d/%d transient=%s",
        "retry" if will_retry else "failure",
        error.error_code,
        error.status_code if error.status_code is not None else "none",
        str(metadata.get("content_type") or "none")[:80],
        str(metadata.get("content_encoding") or "none")[:40],
        metadata.get("body_bytes", "unknown"),
        str(metadata.get("body_kind") or "unknown")[:40],
        digest,
        attempt,
        max_attempts,
        "yes" if error.retryable else "no",
    )
