"""Workable source adapter."""

from __future__ import annotations

from typing import Any, Callable

from watcher.config import CompanyCfg
from watcher.sources.base import (
    SourceSchemaError,
    ensure_list,
    html_to_text,
    iso_date,
    make_row,
    page_fingerprint,
    post_json,
    require_token,
)
from watcher.sources.direct import DirectRecordAdapter

DEFAULT_MAX_PAGES = 1_000


class WorkableSource(DirectRecordAdapter):
    name = "workable"

    def __init__(
        self,
        *,
        request_json: Callable[[str, dict, str], Any] | None = None,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        self._request_json = request_json
        self.max_pages = max_pages
        self.pages_requested = 0
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint(token: str) -> str:
        return f"https://apply.workable.com/api/v3/accounts/{token}/jobs"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        self._begin_direct_diagnostics()
        self.pages_requested = 0
        endpoint = self.endpoint(token)
        request_json = self._request_json or post_json
        expected_total: int | None = None
        raw_seen = 0
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_pages: set[str] = set()
        rows: list[dict] = []

        for page_number in range(1, self.max_pages + 1):
            payload = {} if cursor is None else {"query": "", "token": cursor}
            self.pages_requested += 1
            response = request_json(endpoint, payload, self.name)
            jobs, total, next_cursor = _strict_page(response)

            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise SourceSchemaError("workable total changed during pagination")

            if total == 0:
                if page_number != 1 or jobs or next_cursor is not None:
                    raise SourceSchemaError(
                        "workable zero-result response was inconsistent"
                    )
                self._finish_direct_diagnostics(rows)
                return rows
            if not jobs:
                raise SourceSchemaError("workable pagination ended before total")

            fingerprint = page_fingerprint(jobs)
            if fingerprint in seen_pages:
                raise SourceSchemaError(
                    "workable returned a repeated pagination page"
                )
            seen_pages.add(fingerprint)
            raw_seen += len(jobs)
            if raw_seen > total:
                raise SourceSchemaError(
                    "workable returned more records than the reported total"
                )

            rows.extend(self._parse_records(jobs, company))
            if raw_seen == total:
                if next_cursor is not None:
                    raise SourceSchemaError(
                        "workable returned a cursor after total completion"
                    )
                self._finish_direct_diagnostics(rows)
                return rows
            if next_cursor is None:
                raise SourceSchemaError("workable pagination ended before total")
            if next_cursor in seen_cursors:
                raise SourceSchemaError("workable returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if page_number == self.max_pages:
                raise SourceSchemaError(
                    "workable reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable Workable pagination state")

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        if not isinstance(payload, dict):
            raise SourceSchemaError("workable expected a JSON object")
        total = payload.get("total")
        if total is not None and not isinstance(total, int):
            raise SourceSchemaError("workable expected total to be an integer")
        jobs = ensure_list(payload.get("results"), self.name, "results")
        rows = self._parse_records(jobs, company)
        incomplete = total is not None and total > len(jobs)
        self._finish_direct_diagnostics(
            rows,
            incomplete=incomplete,
            truncated=incomplete,
            reason_codes=("reported_total_exceeds_response",) if incomplete else (),
        )
        return rows

    def _parse_records(self, jobs: list, company: CompanyCfg) -> list[dict]:
        return self._parse_direct_records(
            jobs,
            company,
            lambda job: self._parse_job(job, company),
        )

    def _parse_job(self, job: Any, company: CompanyCfg) -> dict:
        if not isinstance(job, dict):
            raise SourceSchemaError("workable expected each job to be an object")

        title = str(job.get("title") or "").strip()
        shortcode = str(job.get("shortcode") or "").strip()
        source_url = str(job.get("url") or "").strip() or _job_url(company.token, shortcode)
        if not title or not shortcode or not source_url:
            raise SourceSchemaError("workable job missing required title, shortcode, or URL")

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=_location(job),
            description=html_to_text(job.get("description")),
            requirements=html_to_text(job.get("requirements")),
            source_url=source_url,
            date_posted=iso_date(job.get("published")),
            remote_status=_remote_status(job),
            internship_type=str(job.get("type") or "").strip(),
            extra={
                "source_id": str(job.get("id") or ""),
                "source_requisition_id": shortcode,
                "source_system": self.name,
                "shortcode": shortcode,
                "department": _join(job.get("department")),
                "workplace": str(job.get("workplace") or ""),
                "locations": job.get("locations") or [job.get("location") or {}],
            },
        )


def _strict_page(payload: Any) -> tuple[list, int, str | None]:
    if not isinstance(payload, dict):
        raise SourceSchemaError("workable expected a JSON object")
    jobs = payload.get("results")
    total = payload.get("total")
    next_cursor = payload.get("nextPage")
    if not isinstance(jobs, list):
        raise SourceSchemaError("workable expected results to be a list")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SourceSchemaError("workable expected total to be a nonnegative integer")
    if next_cursor is not None:
        if not isinstance(next_cursor, str):
            raise SourceSchemaError("workable expected nextPage to be a string")
        next_cursor = next_cursor.strip()
        if not next_cursor:
            next_cursor = None
    return jobs, total, next_cursor


def _job_url(token: str, shortcode: str) -> str:
    return f"https://apply.workable.com/{token}/j/{shortcode}/" if token and shortcode else ""


def _location(job: dict) -> str:
    locations = job.get("locations")
    if isinstance(locations, list) and locations:
        location_names = [_location_dict(location) for location in locations]
        return "; ".join(name for name in location_names if name)
    return _location_dict(job.get("location"))


def _location_dict(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    parts = [value.get("city"), value.get("region"), value.get("country")]
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def _remote_status(job: dict) -> str:
    workplace = str(job.get("workplace") or "").strip().lower()
    if workplace == "remote" or job.get("remote") is True:
        return "Remote"
    if workplace == "hybrid":
        return "Hybrid"
    if workplace in {"on_site", "onsite", "on-site"}:
        return "On-site"
    return ""


def _join(value: Any) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if item is not None]
        return ", ".join(item for item in items if item)
    return str(value or "").strip()
