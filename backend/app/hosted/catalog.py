"""Sanitized public company catalog derived from watcher configuration."""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# FastAPI is documented to start from backend/, while the authoritative watcher
# package is a sibling. Add only the repository root needed to reuse its loader.
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.dedupe import norm_company
from watcher.config import CompanyCfg, WatcherConfig, load_watchlist

DIRECT_ATS = {
    "bain",
    "epic",
    "greenhouse",
    "ibm",
    "icims",
    "lever",
    "oracle_hcm",
    "paylocity",
    "ashby",
    "smartrecruiters",
    "successfactors",
    "workable",
    "workday",
}


def company_slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name.casefold()).strip("-")


@dataclass(frozen=True)
class PublicCompany:
    id: str
    name: str
    aliases: tuple[str, ...]
    coverage: str
    selectable: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "aliases": list(self.aliases),
            "coverage": self.coverage,
            "selectable": self.selectable,
        }


class CompanyCatalog:
    def __init__(self, companies: tuple[PublicCompany, ...]) -> None:
        self.companies = tuple(sorted(companies, key=lambda item: item.name.casefold()))
        self.by_id = {company.id: company for company in self.companies}
        if len(self.by_id) != len(self.companies):
            raise ValueError("watcher company names produce duplicate public IDs")
        name_index: dict[str, PublicCompany | None] = {}
        for company in self.companies:
            for value in (company.id, company.name, *company.aliases):
                for key in _company_lookup_keys(value):
                    existing = name_index.get(key)
                    if existing is None and key in name_index:
                        continue
                    if existing is not None and existing.id != company.id:
                        name_index[key] = None
                    else:
                        name_index[key] = company
        self._by_name = name_index

    @classmethod
    def from_watcher_config(
        cls,
        watcher: WatcherConfig | None = None,
    ) -> CompanyCatalog:
        watcher = watcher or load_watchlist()
        has_backstop = bool(watcher.effective_github_listing_sources())
        return cls(
            tuple(
                _public_company(company, has_backstop) for company in watcher.companies
            )
        )

    def resolve(self, name_or_id: str) -> PublicCompany | None:
        """Resolve a catalog ID, canonical name, or unambiguous public alias."""

        value = str(name_or_id or "").strip()
        if not value:
            return None
        direct = self.by_id.get(value)
        if direct is not None:
            return direct
        matches: dict[str, PublicCompany] = {}
        for key in _company_lookup_keys(value):
            if key not in self._by_name:
                continue
            company = self._by_name[key]
            if company is None:
                return None
            matches[company.id] = company
        return next(iter(matches.values())) if len(matches) == 1 else None


def _company_lookup_keys(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            key for key in (company_slug(value), norm_company(value)) if key
        )
    )


def _public_company(company: CompanyCfg, has_backstop: bool) -> PublicCompany:
    direct = company.ats in DIRECT_ATS
    coverage = "direct" if direct else "backstop"
    selectable = direct or has_backstop
    aliases = tuple(
        dict.fromkeys(alias.strip() for alias in company.aliases if alias.strip())
    )
    return PublicCompany(
        id=company_slug(company.name),
        name=company.name,
        aliases=aliases,
        coverage=coverage,
        selectable=selectable,
    )
