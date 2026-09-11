"""Practical, explicitly incomplete coverage for the official Ansys job search.

Ansys' first-party careers page links directly to the Synopsys TalentBrew
keyword search for ``ansys``. That slice is useful and deterministically
enumerable, but a keyword is not an authoritative business-unit partition.
This adapter therefore reuses TalentBrew's strict pagination, identity, URL,
and detail validation while permanently publishing ``complete=False``.
"""

from __future__ import annotations

from dataclasses import replace
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceSchemaError
from watcher.sources.talentbrew import TalentBrewSource, _SearchPage


HOST = "careers.synopsys.com"
SITE_ID = "44408"
SEARCH_KEYWORD = "ansys"
DEFAULT_PAGE_SIZE = 100
_SCOPE_LABEL = "Ansys-targeted official keyword search"
_PARTIAL_REASON = "scope_not_completeness_proven"


class AnsysSource(TalentBrewSource):
    """Enumerate the official Ansys-targeted search without claiming completeness."""

    name = "ansys"

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("page_size", DEFAULT_PAGE_SIZE)
        super().__init__(**kwargs)

    @staticmethod
    def endpoint(*, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> str:
        if type(page) is not int or page < 1:
            raise ValueError("ansys page must be a positive integer")
        if type(page_size) is not int or not 1 <= page_size <= DEFAULT_PAGE_SIZE:
            raise ValueError("ansys page size must be between 1 and 100")
        params = {
            "ActiveFacetID": "0",
            "CurrentPage": str(page),
            "RecordsPerPage": str(page_size),
            "Distance": "50",
            "RadiusUnitType": "0",
            "Keywords": SEARCH_KEYWORD,
            "Location": "",
            "Latitude": "",
            "Longitude": "",
            "ShowRadius": "False",
            "IsPagination": "True" if page > 1 else "False",
            "CustomFacetName": "",
            "FacetTerm": "",
            "FacetType": "0",
            "SearchResultsModuleName": "Search Results",
            "SearchFiltersModuleName": "Search Filters",
            "SortCriteria": "0",
            "SortDirection": "0",
            "SearchType": "1",
            "CategoryFacetTerm": "",
            "CategoryFacetType": "0",
            "LocationFacetTerm": "",
            "LocationFacetType": "0",
            "KeywordType": "",
            "LocationType": "",
            "LocationPath": "",
            "OrganizationIds": SITE_ID,
            "PostalCode": "",
            "ResultsType": "0",
            "fc": "",
            "fl": "",
            "fcf": "",
            "afc": "",
            "afl": "",
            "afcf": "",
        }
        return f"https://{HOST}/search-jobs/results?{urlencode(params)}"

    @staticmethod
    def search_endpoint(company: CompanyCfg, page: int, page_size: int) -> str:
        if (
            company.talentbrew_host != HOST
            or company.talentbrew_site_id != SITE_ID
        ):
            raise SourceSchemaError("ansys source configuration changed scope")
        return AnsysSource.endpoint(page=page, page_size=page_size)

    def fetch(self, company: CompanyCfg) -> list[dict]:
        scoped_company = replace(
            company,
            talentbrew_category_id=SEARCH_KEYWORD,
            talentbrew_category_name=_SCOPE_LABEL,
        )
        return super().fetch(scoped_company)

    def _search_page(
        self,
        payload: Any,
        company: CompanyCfg,
        expected_page: int | None = None,
    ) -> _SearchPage:
        page = super()._search_page(payload, company, expected_page)
        parser = _SearchScopeParser()
        parser.feed(payload["results"])
        if parser.metadata != {
            "keywords": SEARCH_KEYWORD,
            "organization_ids": SITE_ID,
            "search_type": "1",
        }:
            raise SourceSchemaError(
                "ansys response did not preserve the Ansys-targeted search scope"
            )
        return page

    def _detail(self, listing, company: CompanyCfg) -> dict:
        row = super()._detail(listing, company)
        extra = dict(row["extra"])
        if extra.get("official_category") == _SCOPE_LABEL:
            extra.pop("official_category")
        extra.update(
            {
                "source_scope": f"{HOST}:{SITE_ID}:keyword:{SEARCH_KEYWORD}",
                "official_search_keyword": SEARCH_KEYWORD,
                "source_completeness": "practical_partial",
            }
        )
        return {**row, "extra": extra}

    def _publish_health_diagnostics(self, rows: list[dict]) -> None:
        retry_attempts = self.last_diagnostics.retry_attempts
        reason_codes = [_PARTIAL_REASON]
        if retry_attempts:
            reason_codes.append("request_retry_recovered")
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=self.last_diagnostics.duplicate_postings_skipped,
            failed_request_count=retry_attempts,
            incomplete=True,
            truncated=False,
            degraded=True,
            complete=False,
            reason_codes=reason_codes,
        )


class _SearchScopeParser(HTMLParser):
    """Read only the provider-owned scope echoed on the result container."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: dict[str, str] = {}

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id") != "search-results":
            return
        if self.metadata:
            self.metadata = {"duplicate": "true"}
            return
        self.metadata = {
            "keywords": values.get("data-keywords", "").strip().casefold(),
            "organization_ids": values.get("data-organization-ids", "").strip(),
            "search_type": values.get("data-search-type", "").strip(),
        }
