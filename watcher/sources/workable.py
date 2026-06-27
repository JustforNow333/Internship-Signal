"""Workable source adapter."""

from __future__ import annotations

from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.base import SourceSchemaError, ensure_list, html_to_text, iso_date, make_row, post_json, require_token


class WorkableSource:
    name = "workable"

    @staticmethod
    def endpoint(token: str) -> str:
        return f"https://apply.workable.com/api/v3/accounts/{token}/jobs"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        return self.parse(post_json(self.endpoint(token), {}, self.name), company)

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        if not isinstance(payload, dict):
            raise SourceSchemaError("workable expected a JSON object")
        total = payload.get("total")
        if total is not None and not isinstance(total, int):
            raise SourceSchemaError("workable expected total to be an integer")
        jobs = ensure_list(payload.get("results"), self.name, "results")
        return [self._parse_job(job, company) for job in jobs]

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
                "shortcode": shortcode,
                "department": _join(job.get("department")),
                "workplace": str(job.get("workplace") or ""),
            },
        )


def _job_url(token: str, shortcode: str) -> str:
    return f"https://apply.workable.com/{token}/j/{shortcode}/" if token and shortcode else ""


def _location(job: dict) -> str:
    locations = job.get("locations")
    if isinstance(locations, list) and locations:
        return "; ".join(_location_dict(location) for location in locations if _location_dict(location))
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
        return ", ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()
