"""SimplifyJobs GitHub listings backstop adapter."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from watcher.company_matching import company_matches
from watcher.config import CompanyCfg
from watcher.season_terms import terms_match
from watcher.sources.base import SourceError, SourceSchemaError, ensure_list, fetch_json, iso_date, make_row

LOGGER = logging.getLogger(__name__)


class GitHubListingsSource:
    name = "github_listings"
    format = "simplify_json"
    priority = 10
    required_keys = {"company_name", "title", "locations", "url", "date_posted", "active", "terms"}

    def __init__(self, url: str, *, source_name: str = "simplify"):
        self.url = str(url).strip()
        if not self.url:
            raise ValueError("GitHub listings source requires a URL")
        self.source_name = str(source_name).strip() or "simplify"
        self.feed_label = _safe_feed_url(self.url)

    def fetch_payload(self):
        try:
            return fetch_json(self.url, self.name)
        except SourceError as exc:
            message = str(exc).replace(self.url, self.feed_label)
            raise type(exc)(message) from exc

    def fetch(self, company: CompanyCfg) -> list[dict]:
        return self.parse(self.fetch_payload(), company)

    def fetch_many(self, companies: list[CompanyCfg] | tuple[CompanyCfg, ...]) -> list[dict]:
        payload = self.fetch_payload()
        rows = []
        for company in companies:
            rows.extend(self.parse(payload, company))
        return rows

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        listings = ensure_list(payload, self.name, "payload")
        if not listings:
            self._schema_problem("github listings payload contained no entries")

        valid_entries = []
        skipped = 0
        first_problem = ""
        for entry in listings:
            try:
                self._validate_entry(entry)
            except SourceSchemaError as exc:
                skipped += 1
                if not first_problem:
                    first_problem = str(exc)
            else:
                valid_entries.append(entry)
        if skipped:
            safe_company = re.sub(
                r"[\x00-\x1f\x7f]+",
                " ",
                str(company.name or "unknown"),
            ).strip()[:120]
            LOGGER.warning(
                "GitHub listings schema problem: skipped %d malformed entry(s) "
                "for %s; %d valid entry(s) retained.",
                skipped,
                safe_company or "unknown",
                len(valid_entries),
            )
        if listings and not valid_entries:
            raise SourceSchemaError(
                f"{first_problem}; github listings received {len(listings)} "
                "entry(s) but none were valid"
            )

        rows = []
        for entry in valid_entries:
            if not entry["active"]:
                continue
            if not company_matches(entry["company_name"], company):
                continue
            if not terms_match(entry["terms"], company.terms):
                continue
            rows.append(self._parse_entry(entry))
        return rows

    def _validate_entry(self, entry: Any) -> None:
        if not isinstance(entry, dict):
            raise SourceSchemaError("github listing entry must be an object")
        missing = sorted(self.required_keys - set(entry))
        if missing:
            raise SourceSchemaError(
                f"github listing entry missing keys: {', '.join(missing)}"
            )
        if not isinstance(entry["locations"], list):
            raise SourceSchemaError("github listing locations must be a list")
        if not isinstance(entry["terms"], list):
            raise SourceSchemaError("github listing terms must be a list")
        if not isinstance(entry["active"], bool):
            raise SourceSchemaError("github listing active must be a boolean")
        for field in ("company_name", "title", "url"):
            if not isinstance(entry[field], str) or not entry[field].strip():
                raise SourceSchemaError(
                    f"github listing {field} must be a nonblank string"
                )

    def _parse_entry(self, entry: dict) -> dict:
        locations = ", ".join(str(location).strip() for location in entry["locations"] if str(location).strip())
        terms = ", ".join(str(term).strip() for term in entry["terms"] if str(term).strip())
        return make_row(
            source="github",
            source_adapter=self.name,
            company=entry["company_name"],
            title=entry["title"],
            location=locations,
            source_url=entry["url"],
            date_posted=iso_date(entry["date_posted"]),
            internship_type=terms,
            extra={
                "source_id": str(entry.get("id") or ""),
                "listing_id": str(entry.get("id") or ""),
                "category": str(entry.get("category") or ""),
                "listing_source": str(entry.get("source") or ""),
                "terms": entry["terms"],
                "feed_url": self.feed_label,
                "source_name": self.source_name,
                "source_format": self.format,
                "source_priority": self.priority,
                "active": True,
            },
        )

    def _schema_problem(self, message: str) -> None:
        LOGGER.warning("GitHub listings schema problem: %s", message)
        raise SourceSchemaError(message)


def _safe_feed_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
