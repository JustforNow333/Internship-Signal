"""Workday source adapter."""

from __future__ import annotations

from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.base import SourceError, SourceSchemaError, ensure_list, html_to_text, make_row, post_json, require_token


class WorkdaySource:
    name = "workday"
    page_size = 20

    @staticmethod
    def endpoint(token: str, shard: str, site: str) -> str:
        return f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/jobs"

    @staticmethod
    def posting_url(token: str, shard: str, site: str, external_path: str) -> str:
        return f"https://{token}.{shard}.myworkdayjobs.com/{site}{external_path}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        shard = _required(company.workday_shard, "workday_shard", company)
        site = _required(company.workday_site, "workday_site", company)
        rows: list[dict] = []
        offset = 0
        total = None
        while True:
            payload = post_json(
                self.endpoint(token, shard, site),
                {"appliedFacets": {}, "limit": self.page_size, "offset": offset, "searchText": ""},
                self.name,
            )
            postings, total_found = self._page(payload)
            rows.extend(self._parse_posting(posting, company, token, shard, site) for posting in postings)
            offset += len(postings)
            total = total_found if total_found is not None else total
            if not postings or (total is not None and offset >= total):
                break
        return rows

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        shard = _required(company.workday_shard, "workday_shard", company)
        site = _required(company.workday_site, "workday_site", company)
        postings, _ = self._page(payload)
        return [self._parse_posting(posting, company, token, shard, site) for posting in postings]

    def _page(self, payload: Any) -> tuple[list, int | None]:
        if not isinstance(payload, dict):
            raise SourceSchemaError("workday expected a JSON object")
        postings = ensure_list(payload.get("jobPostings"), self.name, "jobPostings")
        total = payload.get("total")
        if total is not None and not isinstance(total, int):
            raise SourceSchemaError("workday expected total to be an integer")
        return postings, total

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
                "external_path": external_path,
                "time_type": str(posting.get("timeType") or "").strip(),
                "workday_tenant": token,
                "workday_shard": shard,
                "workday_site": site,
            },
        )


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
