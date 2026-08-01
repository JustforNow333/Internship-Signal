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

from watcher.config import CompanyCfg, load_watchlist  # noqa: E402

DIRECT_ATS = {
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
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

    @classmethod
    def from_watcher_config(cls) -> CompanyCatalog:
        watcher = load_watchlist()
        has_backstop = bool(watcher.effective_github_listing_sources())
        return cls(
            tuple(
                _public_company(company, has_backstop) for company in watcher.companies
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
