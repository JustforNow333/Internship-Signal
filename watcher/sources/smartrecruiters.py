"""SmartRecruiters source adapter."""

from __future__ import annotations

import re
from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.base import SourceSchemaError, ensure_list, fetch_json, iso_date, make_row, page_fingerprint, parse_records, require_token


class SmartRecruitersSource:
    name = "smartrecruiters"
    page_size = 100

    @staticmethod
    def endpoint(token: str, *, limit: int = 100, offset: int = 0) -> str:
        return f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit={limit}&offset={offset}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        postings: list[dict] = []
        offset = 0
        total = None
        seen_pages: set[str] = set()
        while True:
            payload = fetch_json(self.endpoint(token, limit=self.page_size, offset=offset), self.name)
            page_postings, total_found = self._page(payload)
            if page_postings:
                fingerprint = page_fingerprint(page_postings)
                if fingerprint in seen_pages:
                    raise SourceSchemaError("smartrecruiters returned a repeated pagination page")
                seen_pages.add(fingerprint)
            postings.extend(page_postings)
            offset += len(page_postings)
            total = total_found if total_found is not None else total
            if not page_postings or (total is not None and offset >= total):
                break
        return self._parse_postings(postings, company, token)

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        postings, _ = self._page(payload)
        return self._parse_postings(postings, company, token)

    def _parse_postings(self, postings: list, company: CompanyCfg, token: str) -> list[dict]:
        return parse_records(
            postings,
            lambda posting: self._parse_posting(posting, company, token),
            source_name=self.name,
            company_name=company.name,
        )

    def _page(self, payload: Any) -> tuple[list, int | None]:
        if not isinstance(payload, dict):
            raise SourceSchemaError("smartrecruiters expected a JSON object")
        postings = ensure_list(payload.get("content"), self.name, "content")
        total = payload.get("totalFound")
        if total is not None and not isinstance(total, int):
            raise SourceSchemaError("smartrecruiters expected totalFound to be an integer")
        return postings, total

    def _parse_posting(self, posting: Any, company: CompanyCfg, token: str) -> dict:
        if not isinstance(posting, dict):
            raise SourceSchemaError("smartrecruiters expected each posting to be an object")

        title = str(posting.get("name") or "").strip()
        posting_id = str(posting.get("id") or "").strip()
        source_url = str(posting.get("postingUrl") or "").strip() or _posting_url(token, posting_id, title)
        if not title or not posting_id or not source_url:
            raise SourceSchemaError("smartrecruiters posting missing required title, id, or URL")

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=_location(posting.get("location")),
            source_url=source_url,
            date_posted=iso_date(posting.get("releasedDate")),
            remote_status=_remote_status(posting.get("location")),
            internship_type=_custom_field(posting.get("customField"), "Position type")
            or _label(posting.get("typeOfEmployment")),
            extra={
                "source_id": posting_id,
                "ref_number": str(posting.get("refNumber") or ""),
                "smartrecruiters_company": _company_name(posting.get("company")),
                "function": _label(posting.get("function")),
            },
        )


def _posting_url(token: str, posting_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    suffix = f"-{slug}" if slug else ""
    return f"https://jobs.smartrecruiters.com/{token}/{posting_id}{suffix}"


def _location(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    full = str(value.get("fullLocation") or "").strip()
    if full and full != ",":
        return full
    parts = [value.get("city"), value.get("region"), value.get("country")]
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def _remote_status(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    if value.get("remote") is True:
        return "Remote"
    if value.get("hybrid") is True:
        return "Hybrid"
    return ""


def _label(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("label") or value.get("id") or "").strip()
    return ""


def _custom_field(value: Any, label: str) -> str:
    if not isinstance(value, list):
        return ""
    for item in value:
        if not isinstance(item, dict):
            continue
        if str(item.get("fieldLabel") or "").strip().lower() == label.lower():
            return str(item.get("valueLabel") or item.get("valueId") or "").strip()
    return ""


def _company_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("identifier") or "").strip()
    return ""
