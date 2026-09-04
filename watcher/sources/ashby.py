"""Ashby source adapter."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote
from uuid import UUID

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceFetchError, SourceSchemaError, require_token
from watcher.sources.direct import DirectRecordAdapter
from watcher.sources.parsing import ensure_list
from watcher.sources.rows import iso_date, make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import fetch_text, get_json_response


_APP_DATA_ASSIGNMENT = re.compile(r"window\.__appData\s*=\s*")


class AshbySource(DirectRecordAdapter):
    name = "ashby"

    @staticmethod
    def endpoint(token: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{token}"

    @staticmethod
    def hosted_endpoint(token: str) -> str:
        return f"https://jobs.ashbyhq.com/{quote(token, safe='')}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        try:
            response = get_json_response(self.endpoint(token), self.name)
        except SourceFetchError as exc:
            if exc.status_code != 404:
                raise
            return self.parse_hosted(
                fetch_text(self.hosted_endpoint(token), self.name),
                company,
            )
        return self.parse(response.payload, company)

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        if not isinstance(payload, dict):
            raise SourceSchemaError("ashby expected a JSON object")
        jobs = ensure_list(payload.get("jobs"), self.name, "jobs")
        rows = self._parse_direct_records(
            jobs,
            company,
            lambda job: self._parse_job(job, company),
            include=_should_parse,
        )
        self._finish_direct_diagnostics(rows)
        return rows

    def parse_hosted(self, html: str, company: CompanyCfg) -> list[dict]:
        """Parse the bounded listing data embedded in an official hosted board."""

        self._begin_direct_diagnostics()
        app_data = _hosted_app_data(html)
        organization = app_data.get("organization")
        if not isinstance(organization, dict):
            raise SourceSchemaError("ashby hosted board missing organization data")

        token = require_token(company, self.name)
        slug = str(organization.get("hostedJobsPageSlug") or "").strip()
        if slug != token:
            raise SourceSchemaError("ashby hosted board slug did not match configuration")

        board = app_data.get("jobBoard")
        if not isinstance(board, dict):
            raise SourceSchemaError("ashby hosted board missing job board data")
        jobs = ensure_list(board.get("jobPostings"), self.name, "jobPostings")
        teams = _hosted_teams(board.get("teams"))
        rows = self._parse_direct_records(
            jobs,
            company,
            lambda job: self._parse_hosted_job(job, company, token, teams),
        )
        rows, duplicate_count = _deduplicate_hosted_rows(rows)
        self._finish_direct_diagnostics(rows, duplicate_row_count=duplicate_count)
        return rows

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
                "source_requisition_id": str(job.get("id") or ""),
                "source_system": self.name,
                "job_url": str(job.get("jobUrl") or ""),
                "team": str(job.get("team") or ""),
                "department": str(job.get("department") or ""),
                "location": {
                    "name": str(job.get("location") or "").strip(),
                    "country": str(job.get("country") or "").strip(),
                },
                "locations": job.get("secondaryLocations") or [],
            },
        )

    def _parse_hosted_job(
        self,
        job: Any,
        company: CompanyCfg,
        token: str,
        teams: dict[str, str],
    ) -> dict:
        if not isinstance(job, dict):
            raise SourceSchemaError("ashby expected each hosted job to be an object")

        source_id = _hosted_source_id(job.get("id"))
        title = str(job.get("title") or "").strip()
        if not title:
            raise SourceSchemaError("ashby hosted job missing required title")

        source_url = (
            f"https://jobs.ashbyhq.com/{quote(token, safe='')}/"
            f"{quote(source_id, safe='')}"
        )
        primary_location = str(job.get("locationName") or "").strip()
        team_id = str(job.get("teamId") or "").strip()
        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=_hosted_location(job),
            description="",
            source_url=source_url,
            date_posted="",
            remote_status=_remote_status(job),
            internship_type=str(job.get("employmentType") or "").strip(),
            extra={
                "source_id": source_id,
                "source_requisition_id": source_id,
                "source_system": self.name,
                "job_url": source_url,
                "team": teams.get(team_id, ""),
                "department": "",
                "location": {"name": primary_location, "country": ""},
                "locations": job.get("secondaryLocations") or [],
            },
        )


def _should_parse(job: Any) -> bool:
    return not isinstance(job, dict) or job.get("isListed", True) is not False


def _location(job: dict) -> str:
    locations = []
    primary = str(job.get("location") or "").strip()
    if primary:
        locations.append(primary)
    secondary = job.get("secondaryLocations")
    if isinstance(secondary, list):
        for location in secondary:
            if isinstance(location, str):
                name = location.strip()
            elif isinstance(location, dict):
                name = str(location.get("location") or "").strip()
            else:
                raise SourceSchemaError("ashby secondary location must be a string or object")
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


def _hosted_app_data(html: Any) -> dict:
    if not isinstance(html, str):
        raise SourceSchemaError("ashby hosted board expected HTML text")
    matches = list(_APP_DATA_ASSIGNMENT.finditer(html))
    if len(matches) != 1:
        raise SourceSchemaError("ashby hosted board missing unique app data")
    try:
        app_data, _ = json.JSONDecoder().raw_decode(html[matches[0].end() :])
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SourceSchemaError("ashby hosted board app data was invalid") from exc
    if not isinstance(app_data, dict):
        raise SourceSchemaError("ashby hosted board app data must be an object")
    return app_data


def _hosted_teams(value: Any) -> dict[str, str]:
    teams = ensure_list(value, "ashby", "teams")
    result: dict[str, str] = {}
    for team in teams:
        if not isinstance(team, dict):
            continue
        team_id = str(team.get("id") or "").strip()
        name = str(team.get("externalName") or team.get("name") or "").strip()
        if team_id and name:
            result[team_id] = name
    return result


def _hosted_source_id(value: Any) -> str:
    source_id = str(value or "").strip()
    try:
        parsed = UUID(source_id)
    except (AttributeError, TypeError, ValueError) as exc:
        raise SourceSchemaError("ashby hosted job missing a valid native ID") from exc
    if str(parsed) != source_id.casefold():
        raise SourceSchemaError("ashby hosted job native ID was not canonical")
    return source_id


def _hosted_location(job: dict) -> str:
    locations = []
    primary = str(job.get("locationName") or "").strip()
    if primary:
        locations.append(primary)
    secondary = job.get("secondaryLocations")
    if not isinstance(secondary, list):
        raise SourceSchemaError("ashby hosted secondary locations must be a list")
    for location in secondary:
        if not isinstance(location, dict):
            raise SourceSchemaError("ashby hosted secondary location must be an object")
        name = str(location.get("locationName") or "").strip()
        if name:
            locations.append(name)
    return ", ".join(dict.fromkeys(locations))


def _deduplicate_hosted_rows(rows: list[dict]) -> tuple[list[dict], int]:
    retained: list[dict] = []
    by_source_id: dict[str, dict] = {}
    duplicate_count = 0
    for row in rows:
        source_id = str(row.get("extra", {}).get("source_id") or "")
        prior = by_source_id.get(source_id)
        if prior is None:
            by_source_id[source_id] = row
            retained.append(row)
        elif prior == row:
            duplicate_count += 1
        else:
            raise SourceSchemaError("ashby hosted board contained a conflicting duplicate ID")
    return retained, duplicate_count
