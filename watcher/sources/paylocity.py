"""Paylocity public recruiting-board source adapter."""

from __future__ import annotations

import json
import re
from datetime import datetime
from json import JSONDecodeError
from typing import Any, Callable

from watcher.config import CompanyCfg
from watcher.sources.base import (
    DirectDiagnosticsMixin,
    SourceError,
    SourceSchemaError,
    TextHttpResponse,
    get_text_response,
    html_to_text,
    make_row,
    parse_records,
)

HOST = "recruiting.paylocity.com"
_COMPANY_ID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_MODULE_ID = re.compile(r"[1-9][0-9]*")
_SLUG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,126}[A-Za-z0-9])?")
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*")
_DETAIL_BASE = re.compile(
    r"window\.ATSJobDetailsBaseUrl\s*=\s*"
    r"(['\"])/Recruiting/Jobs/Details/\1\s*;",
    re.IGNORECASE,
)


class PaylocitySource(DirectDiagnosticsMixin):
    """Collect the full job array used by Paylocity's official public UI."""

    name = "paylocity"

    def __init__(
        self,
        *,
        request_text: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._request_text = request_text
        self.request_count = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint(company: CompanyCfg) -> str:
        company_id, _module_id, slug = _required_config(company)
        return f"https://{HOST}/recruiting/jobs/All/{company_id}/{slug}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        self.last_response_metadata = {}
        self.request_count += 1
        response = (self._request_text or _get_text)(
            self.endpoint(company), self.name
        )
        if isinstance(response, TextHttpResponse):
            self.last_response_metadata = dict(response.metadata)
            html = response.text
        elif isinstance(response, str):
            html = response
        else:
            raise SourceSchemaError("paylocity expected an HTML text response")
        return self._parse_and_finish(html, company)

    def parse(self, html: Any, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        self.last_response_metadata = {}
        return self._parse_and_finish(html, company)

    def _parse_and_finish(self, html: Any, company: CompanyCfg) -> list[dict]:
        company_id, module_id, _slug = _required_config(company)
        page_data = _parse_page_data(html)
        _validate_board_contract(html, page_data, company_id, module_id)
        jobs = page_data.get("Jobs")
        if not isinstance(jobs, list):
            raise SourceSchemaError("paylocity expected pageData.Jobs to be a list")

        parsed = parse_records(
            jobs,
            lambda job: _parse_posting(job, company, company_id, module_id),
            source_name=self.name,
            company_name=company.name,
            diagnostics=self._record_parse_diagnostics,
        )
        rows: list[dict] = []
        rows_by_id: dict[str, dict] = {}
        ids_by_url: dict[str, str] = {}
        duplicates = 0
        for row in parsed:
            source_id = str(row["extra"]["source_requisition_id"])
            source_url = row["source_url"]
            existing = rows_by_id.get(source_id)
            if existing is not None:
                if existing != row:
                    raise SourceSchemaError(
                        "paylocity returned a conflicting posting ID"
                    )
                duplicates += 1
                continue
            other_id = ids_by_url.get(source_url)
            if other_id is not None and other_id != source_id:
                raise SourceSchemaError(
                    "paylocity returned one posting URL for conflicting IDs"
                )
            rows_by_id[source_id] = row
            ids_by_url[source_url] = source_id
            rows.append(row)

        self._finish_direct_diagnostics(rows, duplicate_row_count=duplicates)
        return rows


def _get_text(url: str, source_name: str) -> TextHttpResponse:
    return get_text_response(url, source_name)


def _required_config(company: CompanyCfg) -> tuple[str, str, str]:
    company_id = str(company.paylocity_company_id or "").strip()
    module_id = str(company.paylocity_module_id or "").strip()
    slug = str(company.paylocity_slug or "").strip()
    if not _COMPANY_ID.fullmatch(company_id):
        raise SourceError(
            f"paylocity requires a valid paylocity_company_id for {company.name}"
        )
    if not _MODULE_ID.fullmatch(module_id):
        raise SourceError(
            f"paylocity requires a valid paylocity_module_id for {company.name}"
        )
    if not _SLUG.fullmatch(slug):
        raise SourceError(
            f"paylocity requires a valid paylocity_slug for {company.name}"
        )
    return company_id, module_id, slug


def _parse_page_data(html: Any) -> dict[str, Any]:
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError("paylocity board response was empty")
    matches = list(_PAGE_DATA.finditer(html))
    if len(matches) != 1:
        raise SourceSchemaError(
            "paylocity response must contain exactly one window.pageData contract"
        )
    try:
        page_data, _end = json.JSONDecoder().raw_decode(html[matches[0].end() :])
    except (JSONDecodeError, RecursionError, ValueError) as exc:
        raise SourceSchemaError("paylocity pageData was malformed") from exc
    if not isinstance(page_data, dict):
        raise SourceSchemaError("paylocity pageData must be an object")
    return page_data


def _validate_board_contract(
    html: Any,
    page_data: dict[str, Any],
    company_id: str,
    module_id: str,
) -> None:
    if not isinstance(html, str) or not _DETAIL_BASE.search(html):
        raise SourceSchemaError("paylocity detail URL contract was missing or invalid")
    if page_data.get("LeadJoinUrl") != (
        f"/Recruiting/PublicLeads/New/{company_id}"
    ):
        raise SourceSchemaError("paylocity company identity did not match configuration")
    if str(page_data.get("ModuleId") or "").strip() != module_id:
        raise SourceSchemaError("paylocity module identity did not match configuration")
    if not isinstance(page_data.get("ModuleTitle"), str) or not str(
        page_data["ModuleTitle"]
    ).strip():
        raise SourceSchemaError("paylocity module title was missing")
    if not isinstance(page_data.get("Departments"), list) or not isinstance(
        page_data.get("Locations"), list
    ):
        raise SourceSchemaError("paylocity board filter metadata was malformed")
    if not isinstance(page_data.get("ShowInternal"), bool):
        raise SourceSchemaError("paylocity board visibility metadata was malformed")


def _parse_posting(
    posting: Any,
    company: CompanyCfg,
    company_id: str,
    module_id: str,
) -> dict:
    if not isinstance(posting, dict):
        raise SourceSchemaError("paylocity expected each posting to be an object")
    native_id = posting.get("JobId")
    title = posting.get("JobTitle")
    if (
        isinstance(native_id, bool)
        or not isinstance(native_id, int)
        or native_id <= 0
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise SourceSchemaError(
            "paylocity posting missing a numeric JobId or title"
        )

    location_data = posting.get("JobLocation")
    if location_data is None:
        location_data = {}
    if not isinstance(location_data, dict):
        raise SourceSchemaError("paylocity JobLocation must be an object or null")
    location_module = location_data.get("ModuleId")
    if location_module not in (None, "") and str(location_module) != module_id:
        raise SourceSchemaError(
            "paylocity posting location identified a conflicting module"
        )
    for field in ("LocationName", "Description", "HiringDepartment"):
        if posting.get(field) is not None and not isinstance(posting.get(field), str):
            raise SourceSchemaError(f"paylocity {field} must be a string or null")
    for field in ("IsRemote", "IsInternal", "ShouldDisplayLocation"):
        if posting.get(field) is not None and not isinstance(posting.get(field), bool):
            raise SourceSchemaError(f"paylocity {field} must be a boolean or null")
    remote_type = posting.get("IndeedRemoteType")
    if remote_type is not None and (
        isinstance(remote_type, bool) or not isinstance(remote_type, int)
    ):
        raise SourceSchemaError(
            "paylocity IndeedRemoteType must be an integer or null"
        )

    native_id_text = str(native_id)
    source_id = f"paylocity:{company_id}:{native_id_text}"
    source_url = f"https://{HOST}/Recruiting/Jobs/Details/{native_id_text}"
    return make_row(
        source="direct",
        source_adapter="paylocity",
        company=company.name,
        title=title.strip(),
        location=_location(posting, location_data),
        description=html_to_text(posting.get("Description")),
        source_url=source_url,
        date_posted=_posting_date(posting.get("PublishedDate")),
        remote_status="Remote" if posting.get("IsRemote") is True else "",
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "paylocity",
            "source_scope": company_id,
            "paylocity_native_id": native_id_text,
            "paylocity_company_id": company_id,
            "paylocity_module_id": module_id,
            "department": str(posting.get("HiringDepartment") or "").strip(),
            "location": location_data,
            "is_internal": posting.get("IsInternal") is True,
            "indeed_remote_type": remote_type,
        },
    )


def _location(posting: dict[str, Any], location_data: dict[str, Any]) -> str:
    name = str(posting.get("LocationName") or location_data.get("Name") or "").strip()
    country = str(location_data.get("Country") or "").strip()
    if name:
        if country.casefold() not in {"", "us", "usa", "united states"} and (
            country.casefold() not in name.casefold()
        ):
            return f"{name}, {country}"
        return name
    parts = (
        location_data.get("City"),
        location_data.get("State"),
        location_data.get("Country"),
    )
    return ", ".join(str(part).strip() for part in parts if str(part or "").strip())


def _posting_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError("paylocity PublishedDate must be an ISO date string")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        raise SourceSchemaError(
            "paylocity PublishedDate must be an ISO date string"
        ) from exc
