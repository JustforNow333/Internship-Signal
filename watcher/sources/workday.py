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
from html import unescape
from typing import Any, Callable, Iterable

from watcher.config import (
    WORKDAY_DETAIL_EARLY_CAREER,
    WORKDAY_DETAIL_INTERNSHIP,
    WORKDAY_DETAIL_NONE,
    CompanyCfg,
    workday_min_interval_seconds,
)
from watcher.sources.base import (
    JsonHttpResponse,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    ensure_list,
    get_json_response,
    html_to_text,
    make_row,
    page_fingerprint,
    post_json,
    require_token,
)

LOGGER = logging.getLogger(__name__)
DEFAULT_MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 10.0
DEFAULT_MAX_DETAIL_CANDIDATES = 100
MAX_CONFIGURABLE_DETAIL_CANDIDATES = 500
# Pagination now continues while a tenant keeps returning full pages, so it
# needs its own terminal bound for a board that never signals an end. At the
# fixed page size this allows 10,000 postings, far above the largest configured
# Workday board, and reaching it is reported as an incomplete listing.
MAX_LISTING_PAGES = 500
COOP_SEPARATOR_PATTERN = r"[-\u2010\u2011\u2012\u2013\u2014 ]?"
_INTERNSHIP_SIGNAL_RE = re.compile(r"\bintern(?:ship)?s?\b", re.IGNORECASE)
_COOP_SIGNAL_RE = re.compile(
    rf"\bco{COOP_SEPARATOR_PATTERN}op\b",
    re.IGNORECASE,
)
_SEASON_YEAR_SIGNAL_RE = re.compile(
    r"(?:\b(?:spring|summer|fall|autumn|winter)[\s-]+"
    r"(?:20\d{2}|['\u2019]\d{2})\b|"
    r"\b20\d{2}[\s-]+(?:spring|summer|fall|autumn|winter)\b)",
    re.IGNORECASE,
)
_EARLY_CAREER_PROGRAM_RE = re.compile(
    r"\b(?:technology|engineering|software\s+development|analyst|technical)\s+"
    r"program(?:me)?\b",
    re.IGNORECASE,
)
_SUMMARIZED_LOCATIONS_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.IGNORECASE)
_LISTING_SIGNAL_FIELDS = (
    "businessTitle",
    "employmentType",
    "jobCategory",
    "jobFamily",
    "jobProfile",
    "studentProgram",
    "timeType",
    "workerSubType",
    "workerType",
)
_REQUIREMENT_HEADINGS = (
    "requirements",
    "basic qualifications",
    "minimum qualifications",
    "required qualifications",
    "what you need",
)
_REQUIREMENT_STOP_HEADINGS = (
    "benefits",
    "compensation",
    "preferred qualifications",
    "responsibilities",
    "what you will do",
    "about us",
    "about the role",
    "application",
)
_DETAIL_TITLE_FIELDS = ("title",)
_DETAIL_DESCRIPTION_FIELDS = ("jobDescription",)
_DETAIL_REQUIREMENT_FIELDS = (
    "requirements",
    "qualifications",
    "minimumQualifications",
)
_DETAIL_REQUISITION_ID_FIELDS = (
    "jobReqId",
    "requisitionId",
    "jobRequisitionId",
)
# Listing requisition IDs are single tokens: letters, digits, and the joiners
# tenants use between segments. Anything else is display metadata.
_REQUISITION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_MIN_REQUISITION_ID_LENGTH = 2
_MAX_REQUISITION_ID_LENGTH = 64
_DETAIL_LOCATION_FIELDS = (
    "location",
    "jobRequisitionLocation",
    "additionalLocations",
    "locations",
)
_DETAIL_POSTING_DATE_FIELDS = ("startDate", "postedOn")
_DETAIL_DEADLINE_FIELDS = (
    "endDate",
    "jobPostingEndDate",
    "jobPostingEndDateAsText",
    "applicationDeadline",
)
_DETAIL_TYPE_FIELDS = (
    "workerSubType",
    "jobType",
    "employmentType",
    "timeType",
)
_DETAIL_REMOTE_FIELDS = ("remoteType", "remoteStatus", "workplaceType")
_DETAIL_COMPENSATION_FIELDS = (
    "payRange",
    "salaryRange",
    "compensation",
    "compensationText",
)
_DETAIL_METADATA_FIELDS = (
    ("studentProgram", "student_program"),
    ("studentPrograms", "student_programs"),
    ("degreeRequirements", "degree_requirements"),
    ("educationRequirements", "education_requirements"),
    ("classYear", "class_year"),
    ("classYears", "class_years"),
    ("graduationYear", "graduation_year"),
    ("graduationYears", "graduation_years"),
    ("eligibility", "eligibility"),
    ("eligibilityRequirements", "eligibility_requirements"),
    ("country", "country"),
    ("jobRequisitionLocation", "job_requisition_location"),
    ("workerSubType", "worker_subtype"),
    ("workerType", "worker_type"),
    ("employmentType", "employment_type"),
    ("timeType", "time_type"),
    ("remoteType", "remote_type"),
    ("canApply", "can_apply"),
    ("posted", "posted"),
)
# Workday search remains authoritative for the title, so title is a valid
# contract marker without being merged back over the listing value. Every
# other schema field below is consumed by a merger helper or metadata mapping.
_DETAIL_SCHEMA_FIELD_GROUPS = (
    _DETAIL_TITLE_FIELDS,
    _DETAIL_DESCRIPTION_FIELDS,
    _DETAIL_REQUIREMENT_FIELDS,
    _DETAIL_REQUISITION_ID_FIELDS,
    _DETAIL_LOCATION_FIELDS,
    _DETAIL_POSTING_DATE_FIELDS,
    _DETAIL_DEADLINE_FIELDS,
    _DETAIL_TYPE_FIELDS,
    _DETAIL_REMOTE_FIELDS,
    _DETAIL_COMPENSATION_FIELDS,
)
_DETAIL_SCHEMA_FIELDS = frozenset(
    field
    for group in _DETAIL_SCHEMA_FIELD_GROUPS
    for field in group
) | frozenset(
    source_field for source_field, _destination_field in _DETAIL_METADATA_FIELDS
)


@dataclass(frozen=True)
class WorkdayParseDiagnostics:
    raw_postings_seen: int = 0
    valid_rows_retained: int = 0
    malformed_postings_skipped: int = 0
    skip_reasons: tuple[tuple[str, int], ...] = ()
    request_attempts: int = 0
    retry_attempts: int = 0
    last_transport_error: str = ""
    listing_pages: int = 0
    listing_rows: int = 0
    detail_candidates: int = 0
    detail_requests: int = 0
    detail_successes: int = 0
    detail_retries: int = 0
    detail_failures: int = 0
    disappeared_postings: int = 0
    rows_enriched: int = 0
    canonical_fields_filled: int = 0
    canonical_field_counts: tuple[tuple[str, int], ...] = ()
    descriptions_filled: int = 0
    locations_expanded: int = 0
    candidates_skipped_by_policy: int = 0
    detail_failure_reasons: tuple[tuple[str, int], ...] = ()
    detail_enrichment_degraded: bool = False
    detail_degraded_reason: str = ""
    listing_incomplete: bool = False
    listing_incomplete_reasons: tuple[str, ...] = ()
    pagination_total_anomalies: tuple[tuple[str, int], ...] = ()


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


class _WorkdayDetailSchemaError(SourceSchemaError):
    def __init__(self, message: str, *, error_code: str = "schema_error") -> None:
        super().__init__(message)
        self.error_code = error_code


def workday_detail_candidate_reason(
    title: object,
    metadata: object,
    policy: str,
) -> str | None:
    """Return the first conservative reason a listing merits one detail GET."""

    if policy == WORKDAY_DETAIL_NONE:
        return None
    text = " ".join(
        part for part in (str(title or "").strip(), _listing_signal_text(metadata)) if part
    )
    if _INTERNSHIP_SIGNAL_RE.search(text):
        return "internship"
    if _COOP_SIGNAL_RE.search(text):
        return "co_op"
    if _SEASON_YEAR_SIGNAL_RE.search(text):
        return "season_year"
    for reason, pattern in (
        ("student", r"\bstudents?\b"),
        ("university", r"\buniversity\b"),
        ("campus", r"\bcampus\b"),
        ("early_career", r"\bearly[- ]career\b"),
        ("apprenticeship", r"\bapprentic(?:e|eship)\b"),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            return reason
    if policy == WORKDAY_DETAIL_EARLY_CAREER and _EARLY_CAREER_PROGRAM_RE.search(text):
        return "early_career_program"
    return None


def _listing_signal_text(metadata: object) -> str:
    if not isinstance(metadata, dict):
        return ""
    values = []
    for key in _LISTING_SIGNAL_FIELDS:
        values.extend(_text_values(metadata.get(key)))
    bullet_fields = metadata.get("bulletFields")
    if isinstance(bullet_fields, list):
        values.extend(str(value).strip() for value in bullet_fields[1:] if str(value).strip())
    return " ".join(values)


def _text_values(value: object, *, depth: int = 0) -> list[str]:
    """Return bounded display text from common Workday scalar/container shapes."""

    if depth > 3 or value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, (int, float, bool)):
        return [str(value)]
    if isinstance(value, list):
        values: list[str] = []
        for item in value[:50]:
            values.extend(_text_values(item, depth=depth + 1))
        return values
    if isinstance(value, dict):
        for key in ("descriptor", "name", "label", "value"):
            if key in value:
                values = _text_values(value.get(key), depth=depth + 1)
                if values:
                    return values
    return []


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
        request_detail_json: Callable[[str, str], Any] | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_detail_candidates: int = DEFAULT_MAX_DETAIL_CANDIDATES,
        pacer: WorkdayPacer | None = None,
    ) -> None:
        interval = workday_min_interval_seconds(min_interval_seconds)
        if max_attempts < 1 or max_attempts > DEFAULT_MAX_ATTEMPTS:
            raise ValueError(f"max_attempts must be between 1 and {DEFAULT_MAX_ATTEMPTS}")
        if not 1 <= int(max_detail_candidates) <= MAX_CONFIGURABLE_DETAIL_CANDIDATES:
            raise ValueError(
                "max_detail_candidates must be between 1 and "
                f"{MAX_CONFIGURABLE_DETAIL_CANDIDATES}"
            )
        self.last_diagnostics = WorkdayParseDiagnostics()
        self._request_attempts = 0
        self._retry_attempts = 0
        self._last_transport_error = ""
        self.last_response_metadata: dict[str, object] = {}
        self._sleeper = sleeper
        self._jitter = jitter
        self._request_json = request_json
        self._request_detail_json = request_detail_json
        self._max_attempts = max_attempts
        self._max_detail_candidates = int(max_detail_candidates)
        # An injected pacer lets bounded concurrent collection share one tenant
        # pacer across worker threads without changing serial behavior.
        self._pacer = pacer or WorkdayPacer(interval, sleeper=sleeper, clock=clock)

    @staticmethod
    def endpoint(token: str, shard: str, site: str) -> str:
        return f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/jobs"

    @staticmethod
    def posting_url(token: str, shard: str, site: str, external_path: str) -> str:
        return f"https://{token}.{shard}.myworkdayjobs.com/{site}{external_path}"

    @staticmethod
    def detail_endpoint(token: str, shard: str, site: str, external_path: str) -> str:
        """Return the public CXS detail endpoint linked by ``externalPath``."""

        path = str(external_path or "").strip()
        if not path.startswith("/"):
            path = f"/{path}"
        return (
            f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/"
            f"{token}/{site}{path}"
        )

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
        total: int | None = None
        pages_fetched = 0
        incomplete_reasons: set[str] = set()
        seen_pages: set[str] = set()
        while True:
            payload = self._fetch_page(
                self.endpoint(token, shard, site),
                {"appliedFacets": {}, "limit": self.page_size, "offset": offset, "searchText": ""},
            )
            postings, total_found = self._page(payload)
            pages_fetched += 1
            self._listing_pages += 1
            self._listing_rows += len(postings)
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
            total = self._reconcile_listing_total(total, total_found, offset)
            # A short page is the tenant's own end-of-board signal, so a full
            # page is the only reason to ask for another one.
            if len(postings) < self.page_size:
                if total is not None and offset < total:
                    incomplete_reasons.add("pagination_ended_early")
                break
            if total is not None and offset >= total:
                break
            if pages_fetched >= MAX_LISTING_PAGES:
                incomplete_reasons.add("pagination_safety_limit_reached")
                break
        self._listing_incomplete_reasons = incomplete_reasons
        self._log_listing_summary(company, pages_fetched, offset, total, incomplete_reasons)
        rows = self._finalize(rows, raw_postings_seen, skip_reasons, company)
        return self._enrich_details(rows, company, token, shard, site)

    def _reconcile_listing_total(
        self,
        current: int | None,
        reported: object,
        offset: int,
    ) -> int | None:
        """Return the pagination reference total, keeping the trustworthy value.

        Many Workday tenants report a real total only on the first page and then
        send ``total: 0`` (or omit it) on every continuation page. Adopting those
        values ended pagination after two pages, so a later value replaces the
        reference only when it is credible: a non-negative integer that is not
        behind the rows already retrieved. A credible change is adopted upward
        only, so a shrinking board can never shorten pagination. Every ignored or
        changed value is counted in bounded, payload-free diagnostics.
        """

        anomalies = self._pagination_total_anomalies
        if reported is None:
            if current is not None:
                anomalies["total_missing"] += 1
            return current
        reported_total = int(reported)
        if reported_total < 0:
            anomalies["total_negative"] += 1
            return current
        if current is not None and reported_total == current:
            return current
        # A total behind the rows already in hand contradicts the postings this
        # page delivered, so it can never become the reference - not even as the
        # first value seen.
        if reported_total == 0 and current is not None:
            anomalies["total_zero"] += 1
            return current
        if reported_total < offset:
            anomalies["total_below_offset"] += 1
            return current
        if current is None:
            return reported_total
        anomalies[
            "total_increased" if reported_total > current else "total_decreased"
        ] += 1
        return max(current, reported_total)

    def _log_listing_summary(
        self,
        company: CompanyCfg,
        pages: int,
        rows_seen: int,
        total: int | None,
        incomplete_reasons: set[str],
    ) -> None:
        log = LOGGER.warning if incomplete_reasons else LOGGER.info
        log(
            "Workday listing summary for %s: pages=%d postings=%d total_reference=%s "
            "anomalies=%s incomplete=%s",
            _safe_company_name(company.name),
            pages,
            rows_seen,
            "unknown" if total is None else total,
            ",".join(
                f"{reason}={count}"
                for reason, count in sorted(self._pagination_total_anomalies.items())
            )
            or "none",
            ",".join(sorted(incomplete_reasons)) or "none",
        )

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        self._reset_diagnostics()
        token = require_token(company, self.name)
        shard = _required(company.workday_shard, "workday_shard", company)
        site = _required(company.workday_site, "workday_site", company)
        postings, _ = self._page(payload)
        self._listing_pages = 1
        self._listing_rows = len(postings)
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
        return self._request_with_retry(
            lambda: request_json(url, payload, self.name),
            detail=False,
        )

    def _fetch_detail(self, url: str) -> Any:
        request_json = self._request_detail_json or get_json_response
        return self._request_with_retry(
            lambda: request_json(url, self.name),
            detail=True,
        )

    def _request_with_retry(
        self,
        request: Callable[[], Any],
        *,
        detail: bool,
    ) -> Any:
        for attempt in range(1, self._max_attempts + 1):
            self._request_attempts += 1
            if detail:
                self._detail_requests += 1
            try:
                response = request()
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
                if detail:
                    self._detail_retries += 1
                retry_after = exc.response_metadata.get("retry_after_seconds")
                delay = workday_retry_delay(
                    attempt,
                    jitter=self._jitter,
                    retry_after=retry_after if isinstance(retry_after, (int, float)) else None,
                )
                self._sleeper(delay)

        raise AssertionError("unreachable Workday retry state")

    def _enrich_details(
        self,
        rows: list[dict],
        company: CompanyCfg,
        token: str,
        shard: str,
        site: str,
    ) -> list[dict]:
        policy = company.workday_detail_policy
        grouped: dict[str, list[dict]] = {}
        candidate_reasons: dict[str, str] = {}
        for row in rows:
            extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
            extra["workday_detail_policy"] = policy
            row["extra"] = extra
            path = str(extra.get("external_path") or "").strip()
            if not path:
                extra["workday_detail_status"] = "not_selected"
                continue
            grouped.setdefault(path, []).append(row)
            reason = workday_detail_candidate_reason(
                row.get("title"),
                extra.get("workday_listing_metadata"),
                policy,
            )
            if reason:
                candidate_reasons[path] = reason

        self._detail_candidates = len(candidate_reasons)
        self._candidates_skipped_by_policy = max(
            0, len(grouped) - self._detail_candidates
        )
        for path, path_rows in grouped.items():
            if path not in candidate_reasons:
                status = "disabled" if policy == WORKDAY_DETAIL_NONE else "not_selected"
                for row in path_rows:
                    row["extra"]["workday_detail_status"] = status

        if self._detail_candidates > self._max_detail_candidates:
            self.last_diagnostics = self._diagnostics_snapshot()
            raise SourceSchemaError(
                "workday detail candidate limit exceeded for "
                f"{_safe_company_name(company.name)}: "
                f"{self._detail_candidates}>{self._max_detail_candidates}"
            )

        for path, reason in candidate_reasons.items():
            path_rows = grouped[path]
            for row in path_rows:
                row["extra"]["workday_detail_candidate_reason"] = reason
            try:
                payload = self._fetch_detail(
                    self.detail_endpoint(token, shard, site, path)
                )
                info = _detail_posting_info(payload)
                listing_id = str(
                    path_rows[0]["extra"].get("source_requisition_id") or ""
                ).strip()
                detail_id = _detail_requisition_id(info)
                if listing_id and detail_id and listing_id != detail_id:
                    raise _WorkdayDetailSchemaError(
                        "workday detail requisition ID conflicts with listing",
                        error_code="requisition_id_conflict",
                    )
                self._detail_successes += 1
                for row in path_rows:
                    filled = _merge_detail_into_row(row, info)
                    if filled:
                        self._rows_enriched += 1
                        self._canonical_field_counts.update(filled)
                        self._descriptions_filled += int("description" in filled)
                        self._locations_expanded += int("location" in filled)
                        row["extra"]["workday_detail_status"] = "enriched"
                    else:
                        row["extra"]["workday_detail_status"] = "success"
            except SourceFetchError as exc:
                if exc.status_code in {404, 410}:
                    # The search result remains authoritative for discovery.
                    # Retain the row for diagnostics, but mark the posting
                    # inactive because it disappeared before detail retrieval.
                    self._disappeared_postings += 1
                    for row in path_rows:
                        row["extra"].update(
                            {
                                "active": False,
                                "workday_detail_status": "disappeared",
                                "workday_detail_error": "posting_disappeared",
                            }
                        )
                    self._detail_failure_reasons["posting_disappeared"] += 1
                else:
                    self._record_detail_failure(path_rows, exc.error_code)
            except _WorkdayDetailSchemaError as exc:
                self._record_detail_failure(path_rows, exc.error_code)
            except SourceSchemaError:
                self._record_detail_failure(path_rows, "schema_error")

        failed_or_gone = self._detail_failures + self._disappeared_postings
        if self._detail_candidates and failed_or_gone == self._detail_candidates:
            reasons = list(self._detail_failure_reasons)
            if len(reasons) == 1:
                self._detail_enrichment_degraded = True
                self._detail_degraded_reason = reasons[0]

        self.last_diagnostics = self._diagnostics_snapshot()
        self._log_detail_summary(company)
        return rows

    def _record_detail_failure(self, rows: list[dict], reason: object) -> None:
        safe_reason = _safe_detail_error_code(reason)
        self._detail_failures += 1
        self._detail_failure_reasons[safe_reason] += 1
        for row in rows:
            row["extra"].update(
                {
                    "workday_detail_status": "failed",
                    "workday_detail_error": safe_reason,
                }
            )

    def _log_detail_summary(self, company: CompanyCfg) -> None:
        log = LOGGER.warning if self._detail_enrichment_degraded else LOGGER.info
        log(
            "Workday detail summary for %s: listing_pages=%d listing_rows=%d "
            "detail_candidates=%d detail_requests=%d successes=%d retries=%d "
            "failures=%d disappeared=%d rows_enriched=%d fields_filled=%d "
            "skipped_by_policy=%d degraded=%s reason=%s",
            _safe_company_name(company.name),
            self._listing_pages,
            self._listing_rows,
            self._detail_candidates,
            self._detail_requests,
            self._detail_successes,
            self._detail_retries,
            self._detail_failures,
            self._disappeared_postings,
            self._rows_enriched,
            sum(self._canonical_field_counts.values()),
            self._candidates_skipped_by_policy,
            "yes" if self._detail_enrichment_degraded else "no",
            self._detail_degraded_reason or "none",
        )

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
            listing_pages=self._listing_pages,
            listing_rows=self._listing_rows,
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
        self._listing_pages = 0
        self._listing_rows = 0
        self._detail_candidates = 0
        self._detail_requests = 0
        self._detail_successes = 0
        self._detail_retries = 0
        self._detail_failures = 0
        self._disappeared_postings = 0
        self._rows_enriched = 0
        self._canonical_field_counts: Counter[str] = Counter()
        self._descriptions_filled = 0
        self._locations_expanded = 0
        self._candidates_skipped_by_policy = 0
        self._detail_failure_reasons: Counter[str] = Counter()
        self._detail_enrichment_degraded = False
        self._detail_degraded_reason = ""
        self._listing_incomplete_reasons: set[str] = set()
        self._pagination_total_anomalies: Counter[str] = Counter()
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
            listing_pages=self._listing_pages,
            listing_rows=self._listing_rows,
            detail_candidates=self._detail_candidates,
            detail_requests=self._detail_requests,
            detail_successes=self._detail_successes,
            detail_retries=self._detail_retries,
            detail_failures=self._detail_failures,
            disappeared_postings=self._disappeared_postings,
            rows_enriched=self._rows_enriched,
            canonical_fields_filled=sum(self._canonical_field_counts.values()),
            canonical_field_counts=tuple(sorted(self._canonical_field_counts.items())),
            descriptions_filled=self._descriptions_filled,
            locations_expanded=self._locations_expanded,
            candidates_skipped_by_policy=self._candidates_skipped_by_policy,
            detail_failure_reasons=tuple(sorted(self._detail_failure_reasons.items())),
            detail_enrichment_degraded=self._detail_enrichment_degraded,
            detail_degraded_reason=self._detail_degraded_reason,
            listing_incomplete=bool(self._listing_incomplete_reasons),
            listing_incomplete_reasons=tuple(sorted(self._listing_incomplete_reasons)),
            pagination_total_anomalies=tuple(
                sorted(self._pagination_total_anomalies.items())
            ),
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
                "workday_listing_metadata": _listing_metadata(posting),
                "workday_tenant": token,
                "workday_shard": shard,
                "workday_site": site,
            },
        )


def _listing_metadata(posting: dict) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for key in _LISTING_SIGNAL_FIELDS:
        value = _bounded_metadata_value(posting.get(key))
        if value not in (None, "", [], {}):
            metadata[key] = value
    bullet_fields = posting.get("bulletFields")
    if isinstance(bullet_fields, list) and len(bullet_fields) > 1:
        metadata["bulletFields"] = [
            str(value).strip()[:200]
            for value in bullet_fields[:20]
            if str(value).strip()
        ]
    return metadata


def _detail_posting_info(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise _WorkdayDetailSchemaError("workday detail expected a JSON object")
    info = payload.get("jobPostingInfo")
    if not isinstance(info, dict):
        raise _WorkdayDetailSchemaError(
            "workday detail missing jobPostingInfo object"
        )
    if not _DETAIL_SCHEMA_FIELDS.intersection(info):
        raise _WorkdayDetailSchemaError(
            "workday detail jobPostingInfo has no recognized fields"
        )
    return info


def _detail_requisition_id(info: dict) -> str:
    for key in _DETAIL_REQUISITION_ID_FIELDS:
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return ""


def _merge_detail_into_row(row: dict, info: dict) -> Counter[str]:
    filled: Counter[str] = Counter()
    extra = row["extra"]

    detail_id = _detail_requisition_id(info)
    if detail_id and not str(extra.get("source_requisition_id") or "").strip():
        extra["source_requisition_id"] = detail_id
        extra["source_id"] = detail_id

    description = html_to_text(info.get(_DETAIL_DESCRIPTION_FIELDS[0]))
    _fill_blank(row, "description", description, filled)
    requirements = _detail_requirements(info)
    _fill_blank(row, "requirements", requirements, filled)

    locations = _detail_locations(info)
    if locations:
        current_location = str(row.get("location") or "").strip()
        if not current_location or _SUMMARIZED_LOCATIONS_RE.fullmatch(current_location):
            new_location = "; ".join(locations)
            if new_location != current_location:
                row["location"] = new_location
                filled["location"] += 1

    posted = _first_text(info, *_DETAIL_POSTING_DATE_FIELDS)
    current_posted = str(row.get("date_posted") or "").strip()
    if posted and (
        not current_posted
        or re.match(r"^posted\b", current_posted, re.IGNORECASE)
    ):
        if posted != current_posted:
            row["date_posted"] = posted
            filled["date_posted"] += 1

    _fill_blank(
        row,
        "deadline",
        _first_text(info, *_DETAIL_DEADLINE_FIELDS),
        filled,
    )
    _fill_blank(row, "internship_type", _detail_type(info), filled)
    _fill_blank(row, "remote_status", _detail_remote_status(info), filled)
    _fill_blank(row, "compensation", _detail_compensation(info), filled)

    for source_key, destination_key in _DETAIL_METADATA_FIELDS:
        value = _bounded_metadata_value(info.get(source_key))
        if value not in (None, "", [], {}):
            extra[destination_key] = value
    if "canApply" in info and info.get("canApply") is False:
        extra["active"] = False
    elif info.get("canApply") is True:
        extra["active"] = True
    return filled


def _fill_blank(
    row: dict,
    field: str,
    value: object,
    filled: Counter[str],
) -> None:
    text = str(value or "").strip()
    if text and not str(row.get(field) or "").strip():
        row[field] = text
        filled[field] += 1


def _first_text(info: dict, *keys: str) -> str:
    for key in keys:
        values = _text_values(info.get(key))
        if values:
            return values[0]
    return ""


def _detail_locations(info: dict) -> list[str]:
    values: list[str] = []
    for key in _DETAIL_LOCATION_FIELDS:
        value = info.get(key)
        if key == "jobRequisitionLocation" and isinstance(value, dict):
            value = value.get("descriptor")
        values.extend(_text_values(value))
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value)).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            unique.append(text[:300])
    return unique[:50]


def _detail_type(info: dict) -> str:
    values: list[str] = []
    for key in _DETAIL_TYPE_FIELDS:
        values.extend(_text_values(info.get(key)))
    return _join_unique(values, separator="; ")


def _detail_remote_status(info: dict) -> str:
    text = _first_text(info, *_DETAIL_REMOTE_FIELDS)
    if not text:
        return ""
    lowered = text.casefold()
    if "hybrid" in lowered:
        return "Hybrid"
    if "remote" in lowered:
        return "Remote"
    if "on-site" in lowered or "onsite" in lowered:
        return "On-site"
    return text


def _detail_compensation(info: dict) -> str:
    return _first_text(info, *_DETAIL_COMPENSATION_FIELDS)


def _detail_requirements(info: dict) -> str:
    for key in _DETAIL_REQUIREMENT_FIELDS:
        value = html_to_text(info.get(key))
        if value:
            return value
    raw = str(info.get(_DETAIL_DESCRIPTION_FIELDS[0]) or "")
    if not raw:
        return ""
    text = unescape(raw)
    text = re.sub(
        r"(?i)<\s*(?:br|/p|/div|/li|/h[1-6])\b[^>]*>",
        "\n",
        text,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [re.sub(r"\s+", " ", line).strip(" :-") for line in text.splitlines()]
    collecting = False
    selected: list[str] = []
    for line in lines:
        if not line:
            continue
        normalized = line.casefold().rstrip(":")
        if not collecting and normalized in _REQUIREMENT_HEADINGS:
            collecting = True
            continue
        if collecting and normalized in _REQUIREMENT_STOP_HEADINGS:
            break
        if collecting:
            selected.append(line)
    return " ".join(selected).strip()


def _join_unique(values: Iterable[object], *, separator: str) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return separator.join(result)


def _bounded_metadata_value(value: object, *, depth: int = 0) -> object:
    if value is None or depth > 3:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value.strip()[:500]
    if isinstance(value, list):
        return [
            item
            for item in (
                _bounded_metadata_value(entry, depth=depth + 1)
                for entry in value[:50]
            )
            if item not in (None, "", [], {})
        ]
    if isinstance(value, dict):
        allowed = ("descriptor", "id", "name", "label", "value", "country")
        return {
            key: item
            for key in allowed
            if (item := _bounded_metadata_value(value.get(key), depth=depth + 1))
            not in (None, "", [], {})
        }
    return None


def _safe_detail_error_code(value: object) -> str:
    code = re.sub(r"[^a-z0-9_.-]+", "_", str(value or "detail_failure").casefold())
    return code.strip("_")[:80] or "detail_failure"


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


def _looks_like_requisition_id(value: str) -> bool:
    """Return whether a listing value can be trusted as a requisition ID.

    Real tenant IDs are compact single tokens carrying at least one digit
    (``R244387``, ``JR-2026-21062-5``, ``PT-JR040904``, ``3166958``). Display
    metadata is prose: it carries separators such as spaces, commas, or
    parentheses, or no digit at all.
    """

    if not _MIN_REQUISITION_ID_LENGTH <= len(value) <= _MAX_REQUISITION_ID_LENGTH:
        return False
    if not _REQUISITION_ID_RE.match(value):
        return False
    return any(character.isdigit() for character in value)


def _source_id(posting: dict) -> str:
    """Return the listing requisition ID, or "" for tenant display metadata.

    ``bulletFields`` is a tenant-configured *display* array, not a guaranteed
    requisition ID. Air Products publishes the posting location there, so every
    posting at one site shared a single requisition identity and distinct
    postings were absorbed as duplicates. Only a requisition-shaped value that
    is not simply repeating the listing's own location survives; anything else
    yields "" so identity falls through to the posting-specific URL tier.
    """

    bullet_fields = posting.get("bulletFields")
    if not (isinstance(bullet_fields, list) and bullet_fields):
        return ""
    value = str(bullet_fields[0] or "").strip()
    if not _looks_like_requisition_id(value):
        return ""
    locations_text = str(posting.get("locationsText") or "").strip()
    if locations_text and value.casefold() == locations_text.casefold():
        return ""
    return value


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
