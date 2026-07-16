"""SimplifyJobs GitHub listings backstop adapter."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from backend.app.dedupe import norm_company
from watcher.config import CompanyCfg
from watcher.sources.base import SourceError, SourceSchemaError, ensure_list, fetch_json, iso_date, make_row

LOGGER = logging.getLogger(__name__)


class GitHubListingsSource:
    name = "github_listings"
    required_keys = {"company_name", "title", "locations", "url", "date_posted", "active", "terms"}

    def __init__(self, url: str):
        self.url = str(url).strip()
        if not self.url:
            raise ValueError("GitHub listings source requires a URL")
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
        rows = []
        for entry in listings:
            self._validate_entry(entry)
            if not entry["active"]:
                continue
            if not _company_matches(entry["company_name"], company):
                continue
            if not _terms_match(entry["terms"], company.terms):
                continue
            rows.append(self._parse_entry(entry))
        return rows

    def _validate_entry(self, entry: Any) -> None:
        if not isinstance(entry, dict):
            self._schema_problem("github listing entry must be an object")
        missing = sorted(self.required_keys - set(entry))
        if missing:
            self._schema_problem(f"github listing entry missing keys: {', '.join(missing)}")
        if not isinstance(entry["locations"], list):
            self._schema_problem("github listing locations must be a list")
        if not isinstance(entry["terms"], list):
            self._schema_problem("github listing terms must be a list")

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
                "category": str(entry.get("category") or ""),
                "listing_source": str(entry.get("source") or ""),
                "terms": entry["terms"],
                "feed_url": self.feed_label,
            },
        )

    def _schema_problem(self, message: str) -> None:
        LOGGER.warning("GitHub listings schema problem: %s", message)
        raise SourceSchemaError(message)


def _company_matches(source_company: Any, company: CompanyCfg) -> bool:
    source_norm = norm_company(str(source_company or ""))
    return any(source_norm == norm_company(name) for name in company.match_names())


def _terms_match(source_terms: list, configured_terms: Any) -> bool:
    terms = {_normalize_term(term) for term in source_terms if str(term).strip()}
    wanted = {_normalize_term(term) for term in configured_terms if str(term).strip()}
    return not wanted or bool(terms & wanted)


def _normalize_term(value: Any) -> str:
    return " ".join(str(value).split()).casefold()


def _safe_feed_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
