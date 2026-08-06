"""Greenhouse source adapter."""

from __future__ import annotations

from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.base import (
    DirectDiagnosticsMixin,
    SourceSchemaError,
    ensure_list,
    fetch_json,
    html_to_text,
    iso_date,
    make_row,
    parse_records,
    require_token,
)


class GreenhouseSource(DirectDiagnosticsMixin):
    name = "greenhouse"

    @staticmethod
    def endpoint(token: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        return self.parse(fetch_json(self.endpoint(token), self.name), company)

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        if not isinstance(payload, dict):
            raise SourceSchemaError("greenhouse expected a JSON object")
        jobs = ensure_list(payload.get("jobs"), self.name, "jobs")
        rows = parse_records(
            jobs,
            lambda job: self._parse_job(job, company),
            source_name=self.name,
            company_name=company.name,
            diagnostics=self._record_parse_diagnostics,
        )
        self._finish_direct_diagnostics(rows)
        return rows

    def _parse_job(self, job: Any, company: CompanyCfg) -> dict:
        if not isinstance(job, dict):
            raise SourceSchemaError("greenhouse expected each job to be an object")

        title = str(job.get("title") or "").strip()
        source_url = str(job.get("absolute_url") or "").strip()
        if not title or not source_url:
            raise SourceSchemaError("greenhouse job missing required title or absolute_url")

        location = job.get("location") or {}
        if location is not None and not isinstance(location, dict):
            raise SourceSchemaError("greenhouse job location must be an object")

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=str((location or {}).get("name") or "").strip(),
            description=html_to_text(job.get("content")),
            source_url=source_url,
            date_posted=iso_date(job.get("first_published") or job.get("updated_at")),
            deadline=iso_date(job.get("application_deadline")),
            internship_type=_metadata_value(job.get("metadata"), "Role Type"),
            extra={
                "source_id": str(job.get("id") or ""),
                "source_requisition_id": str(job.get("id") or ""),
                "source_system": self.name,
                "requisition_id": str(job.get("requisition_id") or ""),
                "greenhouse_company_name": str(job.get("company_name") or ""),
                "location": location or {},
            },
        )


def _metadata_value(metadata: Any, name: str) -> str:
    if not isinstance(metadata, list):
        return ""
    for item in metadata:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip().lower() == name.lower():
            return str(item.get("value") or "").strip()
    return ""
