"""Lever source adapter."""

from __future__ import annotations

from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.base import (
    SourceSchemaError,
    ensure_list,
    fetch_json,
    html_to_text,
    iso_date,
    make_row,
    parse_records,
    require_token,
)


class LeverSource:
    name = "lever"

    @staticmethod
    def endpoint(token: str) -> str:
        return f"https://api.lever.co/v0/postings/{token}?mode=json"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        return self.parse(fetch_json(self.endpoint(token), self.name), company)

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        postings = ensure_list(payload, self.name, "payload")
        return parse_records(
            postings,
            lambda posting: self._parse_posting(posting, company),
            source_name=self.name,
            company_name=company.name,
        )

    def _parse_posting(self, posting: Any, company: CompanyCfg) -> dict:
        if not isinstance(posting, dict):
            raise SourceSchemaError("lever expected each posting to be an object")

        title = str(posting.get("text") or "").strip()
        source_url = str(posting.get("applyUrl") or posting.get("hostedUrl") or "").strip()
        if not title or not source_url:
            raise SourceSchemaError("lever posting missing required text or URL")

        categories = posting.get("categories") or {}
        if categories is not None and not isinstance(categories, dict):
            raise SourceSchemaError("lever posting categories must be an object")

        location = _location(categories)
        requirements = _requirements(posting.get("lists"))

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=location,
            compensation=_salary_range(posting.get("salaryRange")),
            description=_description(posting),
            requirements=requirements,
            source_url=source_url,
            date_posted=iso_date(posting.get("createdAt")),
            remote_status=_remote_status(posting.get("workplaceType")),
            extra={
                "source_id": str(posting.get("id") or ""),
                "posting_url": str(posting.get("hostedUrl") or ""),
            },
        )


def _location(categories: dict) -> str:
    all_locations = categories.get("allLocations")
    if isinstance(all_locations, list) and all_locations:
        return ", ".join(str(location).strip() for location in all_locations if str(location).strip())
    return str(categories.get("location") or "").strip()


def _description(posting: dict) -> str:
    parts = [
        posting.get("descriptionPlain"),
        posting.get("descriptionBodyPlain"),
        posting.get("additionalPlain"),
    ]
    return "\n\n".join(str(part).strip() for part in parts if str(part or "").strip())


def _requirements(lists: Any) -> str:
    if not isinstance(lists, list):
        return ""

    selected = []
    for section in lists:
        if not isinstance(section, dict):
            continue
        heading = str(section.get("text") or "").strip()
        content = html_to_text(section.get("content"))
        if not content:
            continue
        if any(term in heading.lower() for term in ("requirement", "qualification", "experience")):
            selected.append(f"{heading}: {content}" if heading else content)
    return "\n\n".join(selected)


def _remote_status(workplace_type: Any) -> str:
    value = str(workplace_type or "").strip().lower()
    return {
        "remote": "Remote",
        "hybrid": "Hybrid",
        "onsite": "On-site",
        "on-site": "On-site",
    }.get(value, "")


def _salary_range(value: Any) -> str:
    if not isinstance(value, dict):
        return ""

    minimum = value.get("min")
    maximum = value.get("max")
    currency = str(value.get("currency") or "").upper()
    interval = str(value.get("interval") or "").lower()
    if minimum is None and maximum is None:
        return ""

    prefix = "$" if currency == "USD" else f"{currency} " if currency else ""
    suffix = {
        "per-year-salary": " per year",
        "per-month-salary": " per month",
        "per-week-salary": " per week",
        "per-day-wage": " per day",
        "per-hour-wage": " per hour",
    }.get(interval, "")

    def money(amount: Any) -> str:
        try:
            return f"{prefix}{float(amount):,.0f}"
        except (TypeError, ValueError):
            return f"{prefix}{amount}"

    if minimum is not None and maximum is not None:
        return f"{money(minimum)} - {money(maximum)}{suffix}"
    return f"{money(minimum if minimum is not None else maximum)}{suffix}"
