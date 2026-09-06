"""Bechtel-only anonymous, listing-only careers inventory.

The 2026-09-05 reconciliation found 964 SAP publication pages but 938 distinct
requisitions, exactly matching Bechtel's refineSearch inventory. The 26 extra
SAP pages were English/Spanish publication pairs, not additional requisitions.
This is evidence for this tenant only, not a reusable Phenom contract.

Offset ordering is unstable even at constant totals. Accept only two consecutive
independently clean passes with equal totals and requisition sets, within three
passes. Never union passes. Pagination failures discard the entire pass; schema,
transport, access, and safety-budget failures stop the fetch. No per-request
retries, cookies, bootstrap, detail enrichment, or watcher state are used.

At 938 jobs each pass is 500 + 438 + terminal zero (three requests); stabilization
normally costs six requests, at most nine at that total. The hard bounds are 21
requests per pass (including terminal), 10,000 jobs, and 63 requests per fetch.
Provider-local pass reports preserve rejection evidence without contaminating
the shared diagnostics of a subsequently validated pair of clean snapshots.

Phenom postedDate can describe index refresh, not SAP externalPostingStartDate;
date_posted is deliberately omitted. Prefer the listing's employer-authored
descriptionTeaser_ats over its generated teaser. Neither requires detail GETs.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit

from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
)
from watcher.sources.diagnostics import DirectDiagnosticsMixin, DirectSourceDiagnostics
from watcher.sources.parsing import page_fingerprint
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import post_json_response

HOST = "jobs.bechtel.com"
PAGE_SIZE = 500
MAX_PASSES = 3
MAX_PAGES = 21  # Includes terminal verification, not just populated pages.
MAX_REQUESTS = MAX_PASSES * MAX_PAGES
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
_ID = re.compile(r"[1-9][0-9]{0,11}")
# Bechtel's frontend route-title transformation, followed by path escaping.
_SLUG_SEPARATORS = re.compile(r"[\$_|`\-+:,/#&\[\]@{}*%.()?– ]")


class _PassRejected(SourceSchemaError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"bechtel pass rejected: {reason}")


@dataclass
class BechtelPassReport:
    """At most three payload/URL-free records, for manual verification only."""

    total: int | None = None
    raw_rows: int = 0
    unique_requisitions: int = 0
    requests: int = 0
    clean: bool = False
    matched_previous: bool = False
    rejection_reason: str = ""


class BechtelSource(DirectDiagnosticsMixin):
    name = "bechtel"

    def __init__(
        self,
        *,
        request_json: Callable[[str, dict, str], Any] | None = None,
        max_pages: int = MAX_PAGES,
        max_requests: int = MAX_REQUESTS,
    ) -> None:
        for name, value, maximum in (
            ("max_pages", max_pages, MAX_PAGES),
            ("max_requests", max_requests, MAX_REQUESTS),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        self._request_json = request_json
        self.max_pages = max_pages
        self.max_requests = max_requests
        self.request_count = 0
        self.pass_reports: list[BechtelPassReport] = []
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self.request_count

    @property
    def retry_attempts(self) -> int:
        return 0  # Stabilization is whole-pass validation, not HTTP retry.

    @staticmethod
    def endpoint() -> str:
        return f"https://{HOST}/widgets"

    @staticmethod
    def request_body(offset: int) -> dict:
        # Live-verified minimal context: tenant/site is resolved by this fixed
        # host. Empty filters are implicit; no guessed refNum/page/session keys.
        return {
            "ddoKey": "refineSearch",
            "from": offset,
            "size": PAGE_SIZE,
            "jobs": True,
            "counts": True,
            "sortBy": "Most recent",
            "sort": {"field": "postedDate", "order": "desc"},
        }

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        self.pass_reports = []
        self.last_response_metadata = {}
        previous: tuple[int, frozenset[str]] | None = None
        try:
            for _ in range(MAX_PASSES):
                report = BechtelPassReport()
                self.pass_reports.append(report)
                try:
                    rows = self._fetch_pass(company, report)
                except _PassRejected as exc:
                    report.rejection_reason = exc.reason
                    previous = None
                    continue
                report.clean = True
                signature = (
                    len(rows),
                    frozenset(row["extra"]["source_requisition_id"] for row in rows),
                )
                if signature == previous:
                    report.matched_previous = True
                    self._finish_direct_diagnostics(rows)
                    return rows
                previous = signature
            raise SourceSchemaError(
                "bechtel snapshots did not stabilize within three passes"
            )
        except SourceError as exc:
            reason = (
                "request_failure" if isinstance(exc, SourceFetchError)
                else "schema_failure"
            )
            if self.pass_reports and not self.pass_reports[-1].clean:
                self.pass_reports[-1].rejection_reason = (
                    self.pass_reports[-1].rejection_reason or reason
                )
            self.last_health_diagnostics = DirectSourceDiagnostics(
                succeeded=False,
                incomplete=True,
                failed_request_count=int(isinstance(exc, SourceFetchError)),
                reason_codes=(reason,),
            )
            raise

    def _fetch_pass(self, company: CompanyCfg, report: BechtelPassReport) -> list[dict]:
        rows: list[dict] = []
        ids: set[str] = set()
        urls: set[str] = set()
        fingerprints: set[str] = set()
        offset = 0
        for _ in range(self.max_pages):
            if self.request_count >= self.max_requests:
                raise SourceSchemaError("bechtel exceeded the request safety limit")
            self.request_count += 1
            report.requests += 1
            body = self.request_body(offset)
            response = (
                self._request_json(self.endpoint(), body, self.name)
                if self._request_json is not None
                else post_json_response(
                    self.endpoint(),
                    body,
                    self.name,
                    max_response_bytes=MAX_RESPONSE_BYTES,
                )
            )
            if isinstance(response, JsonHttpResponse):
                self.last_response_metadata = dict(response.metadata)
                response = response.payload
            jobs, total = _page(response)
            report.raw_rows += len(jobs)
            if report.total is None:
                report.total = total
                needed = (total + PAGE_SIZE - 1) // PAGE_SIZE + 1
                if needed > self.max_pages:
                    raise SourceSchemaError("bechtel total exceeds the page safety limit")
            elif total != report.total:
                raise _PassRejected("total_changed")
            if len(jobs) != min(PAGE_SIZE, total - offset):
                raise _PassRejected(
                    "terminal_not_empty" if offset == total else "page_arithmetic"
                )
            if offset == total:
                if len(rows) != total or len(ids) != total or report.raw_rows != total:
                    raise _PassRejected("unique_count_mismatch")
                return rows

            parsed = [_posting(job, company) for job in jobs]
            page_ids = [row["extra"]["source_requisition_id"] for row in parsed]
            fingerprint = page_fingerprint(page_ids)
            report.unique_requisitions = len(ids | set(page_ids))
            if fingerprint in fingerprints:
                raise _PassRejected("repeated_page")
            fingerprints.add(fingerprint)
            if len(set(page_ids)) != len(page_ids) or ids.intersection(page_ids):
                raise _PassRejected("duplicate_requisition")
            for row in parsed:
                if row["source_url"] in urls:
                    raise SourceSchemaError("bechtel conflicting canonical posting URLs")
                urls.add(row["source_url"])
            ids.update(page_ids)
            rows.extend(parsed)
            offset += len(jobs)
        raise SourceSchemaError("bechtel exceeded the page safety limit")


def _page(payload: Any) -> tuple[list, int]:
    if not isinstance(payload, dict):
        raise SourceSchemaError("bechtel response must be an object")
    result = payload.get("refineSearch")
    if (
        not isinstance(result, dict)
        or type(result.get("status")) is not int
        or result["status"] != 200
    ):
        raise SourceSchemaError("bechtel response lacks successful refineSearch status")
    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SourceSchemaError(
            "bechtel response requires data.jobs list, even for zero results"
        )
    total, hits = result.get("totalHits"), result.get("hits")
    if type(total) is not int or total < 0:
        raise SourceSchemaError("bechtel totalHits must be a nonnegative integer")
    if type(hits) is not int or hits != len(data["jobs"]):
        raise SourceSchemaError("bechtel hits must equal the jobs count")
    return data["jobs"], total


def _text(value: Any, field: str, *, limit: int = 1000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise SourceSchemaError(f"bechtel {field} must be bounded text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SourceSchemaError(f"bechtel {field} contains invalid Unicode") from exc
    return value.strip()


def _identity(value: Any) -> str:
    if type(value) not in (str, int) or not _ID.fullmatch(str(value)):
        raise SourceSchemaError("bechtel requires a numeric requisition ID")
    return str(value)


def _posting(job: Any, company: CompanyCfg) -> dict:
    if not isinstance(job, dict):
        raise SourceSchemaError("bechtel job must be an object")
    req = _identity(job.get("reqId"))
    if "jobId" in job and _identity(job["jobId"]) != req:
        raise SourceSchemaError("bechtel reqId and jobId disagree")
    if "applyUrl" in job:
        apply = _text(job["applyUrl"], "applyUrl", limit=4000)
        try:
            url = urlsplit(apply)
            query = parse_qs(url.query, keep_blank_values=True)
            valid = (
                url.scheme == "https"
                and url.hostname == "career4.successfactors.com"
                and url.port in (None, 443)
                and not url.username and not url.password
                and not url.fragment
                and url.path == "/career"
                and query.get("company") == ["Bechtel"]
                and query.get("career_ns") == ["job_application"]
                and query.get("career_job_req_id") == [req]
            )
        except ValueError:
            valid = False
        if not valid:
            raise SourceSchemaError(
                "bechtel application URL disagrees with requisition identity"
            )
    seq = _text(job.get("jobSeqNo"), "jobSeqNo")
    if seq != f"BCFBCKUS{req}EXTERNALENUS":
        raise SourceSchemaError(
            "bechtel jobSeqNo disagrees with requisition or public locale"
        )
    if "locale" in job and job["locale"] != "en_US":
        raise SourceSchemaError("bechtel listing locale is not en_US")
    title = _text(job.get("title"), "title", limit=500)
    if not title:
        raise SourceSchemaError("bechtel job requires a title")
    locations = []
    primary = _text(job.get("location"), "location")
    if primary:
        locations.append(primary)
    for key in ("multi_location", "multi_location_array"):
        values = job.get(key, [])
        if not isinstance(values, list):
            raise SourceSchemaError(f"bechtel {key} must be a list")
        for value in values:
            if key == "multi_location_array":
                if not isinstance(value, dict):
                    raise SourceSchemaError(
                        "bechtel multi_location_array entry must be an object"
                    )
                value = value.get("location")
            location = _text(value, key)
            if not location:
                raise SourceSchemaError("bechtel location entry must not be empty")
            if location not in locations:
                locations.append(location)
    if not locations:
        raise SourceSchemaError("bechtel job requires a concrete location")
    parser = job.get("ml_job_parser", {})
    if not isinstance(parser, dict):
        raise SourceSchemaError("bechtel ml_job_parser must be an object")
    description = _text(
        parser.get("descriptionTeaser_ats"), "descriptionTeaser_ats", limit=500000
    )
    if not description:
        description = _text(job.get("descriptionTeaser"), "descriptionTeaser", limit=500000)
    slug = re.sub(r"-+", "-", _SLUG_SEPARATORS.sub("-", title)).removesuffix("-")
    return make_row(
        source="direct",
        source_adapter="bechtel",
        company=company.name,
        title=title,
        location="; ".join(locations),
        description=html_to_text(description),
        source_url=f"https://{HOST}/us/en/job/{seq}/{quote(slug, safe='')}",
        extra={
            "source_id": req,
            "source_requisition_id": req,
            "source_system": "bechtel",
            "locations": locations,
            "country": _text(job.get("country"), "country"),
        },
    )
