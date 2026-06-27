"""Ashby source adapter."""

from __future__ import annotations

from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.base import SourceSchemaError, ensure_list, fetch_json, html_to_text, iso_date, make_row, require_token


class AshbySource:
    name = "ashby"

    @staticmethod
    def endpoint(token: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        return self.parse(fetch_json(self.endpoint(token), self.name), company)

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        if not isinstance(payload, dict):
            raise SourceSchemaError("ashby expected a JSON object")
        jobs = ensure_list(payload.get("jobs"), self.name, "jobs")
        return [self._parse_job(job, company) for job in jobs if _is_public(job)]

    def _parse_job(self, job: Any, company: CompanyCfg) -> dict:
        if not isinstance(job, dict):
            raise SourceSchemaError("ashby expected each job to be an object")

        title = str(job.get("title") or "").strip()
        source_url = str(job.get("applyUrl") or job.get("jobUrl") or "").strip()
        if not title or not source_url:
            raise SourceSchemaError("ashby job missing required title or URL")

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=_location(job),
            description=str(job.get("descriptionPlain") or "").strip() or html_to_text(job.get("descriptionHtml")),
            source_url=source_url,
            date_posted=iso_date(job.get("publishedAt")),
            remote_status=_remote_status(job),
            internship_type=str(job.get("employmentType") or "").strip(),
            extra={
                "source_id": str(job.get("id") or ""),
                "job_url": str(job.get("jobUrl") or ""),
                "team": str(job.get("team") or ""),
                "department": str(job.get("department") or ""),
            },
        )


def _is_public(job: Any) -> bool:
    return isinstance(job, dict) and job.get("isListed", True) is not False


def _location(job: dict) -> str:
    locations = []
    primary = str(job.get("location") or "").strip()
    if primary:
        locations.append(primary)
    secondary = job.get("secondaryLocations")
    if isinstance(secondary, list):
        for location in secondary:
            name = str(location if isinstance(location, str) else location.get("location", "")).strip()
            if name:
                locations.append(name)
    return ", ".join(dict.fromkeys(locations))


def _remote_status(job: dict) -> str:
    workplace = str(job.get("workplaceType") or "").strip()
    if workplace:
        return {
            "remote": "Remote",
            "hybrid": "Hybrid",
            "onsite": "On-site",
            "on-site": "On-site",
        }.get(workplace.lower(), workplace)
    return "Remote" if job.get("isRemote") is True else ""
