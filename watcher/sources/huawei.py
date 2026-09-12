"""Official Huawei campus-internship practical-partial source.

Huawei's current first-party careers frontend exposes an explicit ``INTERN``
filter on its campus listing contract. The English and Chinese portals are
bounded, anonymous, and independently enumerable, but Huawei also publishes a
separate social-recruiting board and links additional regional career stations.
This adapter therefore covers only the two current campus internship locales
and can never establish organization-wide direct-complete coverage.

The production frontend does not require an anonymous bootstrap token or
session cookie. It publishes the application, tenant, language, environment,
origin, and referrer values sent here; no credential or access-controlled state
is used.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.contracts import JsonHttpResponse, SourceError, SourceSchemaError
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import post_json_response


CAREERS_HOST = "career.huawei.com"
API_HOST = "apigw-dgg-b0.huawei.com"
APP_ID = "app_000000035886"
TENANT = "hcm"
ENVIRONMENT = "prod"
PAGE_SIZE = 100
MAX_PAGE_REQUESTS = 101
MAX_SNAPSHOT_PASSES = 3
SOURCE_URL = (
    f"https://{CAREERS_HOST}/en/campus-recruitment-job-list"
    "?recruitmentType=INTERN"
)
API_URL = (
    f"https://{API_HOST}/api/apig/channelhw/recruitmentPosition/pub/getJobPage"
    f"?X-HW-ID={APP_ID}"
)

_SCOPE = f"{CAREERS_HOST}:campus:intern:en+cn"
_PARTIAL_REASON = "scope_not_completeness_proven"
_POSITIVE_ID = re.compile(r"[1-9][0-9]{0,17}")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class _Locale:
    route: str
    language: str
    scenario_name: str

    @property
    def referer(self) -> str:
        return (
            f"https://{CAREERS_HOST}/{self.route}/campus-recruitment-job-list"
            "?recruitmentType=INTERN"
        )

    @property
    def custom_referer(self) -> str:
        return f"https://{CAREERS_HOST}/{self.route}"


_LOCALES = (
    _Locale("en", "en_US", "Interns"),
    _Locale("cn", "zh_CN", "实习生"),
)


@dataclass(frozen=True)
class _ListingPage:
    records: tuple[Mapping[str, object], ...]
    total: int
    total_pages: int
    terminal: bool = False


@dataclass(frozen=True)
class _Snapshot:
    rows: tuple[dict, ...]
    identities: frozenset[str]
    totals: tuple[tuple[str, int], ...]


class HuaweiSource(DirectDiagnosticsMixin):
    """Enumerate the official English and Chinese campus internship slices."""

    name = "huawei"

    def __init__(
        self,
        *,
        request_json: Callable[[str, dict, str, dict[str, str]], Any] | None = None,
        max_page_requests: int = MAX_PAGE_REQUESTS,
        max_snapshot_passes: int = MAX_SNAPSHOT_PASSES,
    ) -> None:
        if (
            type(max_page_requests) is not int
            or not 1 <= max_page_requests <= MAX_PAGE_REQUESTS
        ):
            raise ValueError(
                f"huawei max_page_requests must be between 1 and {MAX_PAGE_REQUESTS}"
            )
        if (
            type(max_snapshot_passes) is not int
            or not 2 <= max_snapshot_passes <= MAX_SNAPSHOT_PASSES
        ):
            raise ValueError(
                "huawei max_snapshot_passes must be between 2 and "
                f"{MAX_SNAPSHOT_PASSES}"
            )
        self._request_json = request_json
        self.max_page_requests = max_page_requests
        self.max_snapshot_passes = max_snapshot_passes
        self.request_count = 0
        self.listing_requests = 0
        self.snapshot_passes_requested = 0
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint() -> str:
        return API_URL

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        self.listing_requests = 0
        self.snapshot_passes_requested = 0
        _require_config(company)

        previous: _Snapshot | None = None
        for _pass_number in range(1, self.max_snapshot_passes + 1):
            self.snapshot_passes_requested += 1
            snapshot = self._snapshot(company)
            if (
                previous is not None
                and snapshot.totals == previous.totals
                and snapshot.identities == previous.identities
            ):
                rows = list(snapshot.rows)
                self._finish_direct_diagnostics(
                    rows,
                    incomplete=True,
                    degraded=True,
                    complete=False,
                    reason_codes=(_PARTIAL_REASON,),
                )
                return rows
            previous = snapshot

        raise SourceSchemaError(
            "huawei internship snapshot did not stabilize within the bounded pass limit"
        )

    def _snapshot(self, company: CompanyCfg) -> _Snapshot:
        rows: list[dict] = []
        identities: set[str] = set()
        urls: set[str] = set()
        totals: list[tuple[str, int]] = []

        for locale in _LOCALES:
            locale_rows, total = self._locale_rows(company, locale=locale)
            totals.append((locale.route, total))
            for row in locale_rows:
                source_id = str(row["extra"]["source_requisition_id"])
                source_url = str(row["source_url"])
                if source_id in identities:
                    raise SourceSchemaError(
                        "huawei returned a duplicate posting ID across locales"
                    )
                if source_url in urls:
                    raise SourceSchemaError(
                        "huawei returned a duplicate posting URL across locales"
                    )
                identities.add(source_id)
                urls.add(source_url)
                rows.append(row)

        return _Snapshot(
            rows=tuple(rows),
            identities=frozenset(identities),
            totals=tuple(totals),
        )

    def _locale_rows(
        self,
        company: CompanyCfg,
        *,
        locale: _Locale,
    ) -> tuple[list[dict], int]:
        rows: list[dict] = []
        identities: set[str] = set()
        urls: set[str] = set()
        expected_total: int | None = None
        expected_pages: int | None = None

        for page_number in range(1, self.max_page_requests + 1):
            payload = {
                "curPage": page_number,
                "pageSize": PAGE_SIZE,
                "jobType": "CR",
                "recruitmentType": ["INTERN"],
            }
            page = _listing_page(
                self._post(locale, payload),
                expected_page=page_number,
            )

            if page.terminal:
                if expected_total is None:
                    if page_number != 1:
                        raise SourceSchemaError(
                            "huawei internship pagination ended without a total"
                        )
                    return [], 0
                if page_number != expected_pages + 1:
                    raise SourceSchemaError(
                        "huawei internship pagination terminated at the wrong boundary"
                    )
                if len(rows) != expected_total:
                    raise SourceSchemaError(
                        "huawei internship pagination ended before the reported total"
                    )
                return rows, expected_total

            if expected_total is None:
                expected_total = page.total
                expected_pages = page.total_pages
            elif page.total != expected_total or page.total_pages != expected_pages:
                raise SourceSchemaError(
                    "huawei internship total changed during pagination"
                )
            if page_number > expected_pages:
                raise SourceSchemaError(
                    "huawei returned postings past the reported terminal page"
                )

            expected_count = (
                PAGE_SIZE
                if page_number < expected_pages
                else expected_total - PAGE_SIZE * (expected_pages - 1)
            )
            if len(page.records) != expected_count:
                raise SourceSchemaError(
                    "huawei listing page count disagreed with the reported total"
                )

            for record in page.records:
                row = _row(record, company, locale=locale)
                source_id = str(row["extra"]["source_requisition_id"])
                source_url = str(row["source_url"])
                if source_id in identities:
                    raise SourceSchemaError(
                        "huawei returned a duplicate posting ID within one locale"
                    )
                if source_url in urls:
                    raise SourceSchemaError(
                        "huawei returned a duplicate posting URL within one locale"
                    )
                identities.add(source_id)
                urls.add(source_url)
                rows.append(row)

        raise SourceSchemaError(
            "huawei reached the maximum page safeguard before an explicit terminal page"
        )

    def _post(self, locale: _Locale, payload: dict) -> object:
        self.request_count += 1
        self.listing_requests += 1
        headers = {
            "X-HW-ID": APP_ID,
            "x-jalor-tenantAlias": TENANT,
            "x-language": locale.language,
            "x-Referer": locale.custom_referer,
            "x-alb-gray": ENVIRONMENT,
            "Origin": f"https://{CAREERS_HOST}",
            "Referer": locale.referer,
        }
        if self._request_json is not None:
            response = self._request_json(API_URL, payload, self.name, headers)
        else:
            response = post_json_response(
                API_URL,
                payload,
                self.name,
                request_headers=headers,
            )
        return response.payload if isinstance(response, JsonHttpResponse) else response


def _require_config(company: CompanyCfg) -> None:
    if str(company.source_url or "").strip() != SOURCE_URL:
        raise SourceError(
            f"huawei requires the official campus internship listing for {company.name}"
        )


def _listing_page(payload: object, *, expected_page: int) -> _ListingPage:
    if not isinstance(payload, Mapping):
        raise SourceSchemaError("huawei listing response expected an object")
    if payload.get("status") != "SUCCESS":
        raise SourceSchemaError("huawei listing API reported failure")
    if payload.get("errors") not in (None, []):
        raise SourceSchemaError("huawei successful listing response contained errors")
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise SourceSchemaError("huawei listing response is missing data")

    page_vo = data.get("pageVO")
    records = data.get("result")
    if page_vo is None or records is None:
        if page_vo is not None or records is not None:
            raise SourceSchemaError("huawei listing terminal response was inconsistent")
        return _ListingPage((), 0, 0, terminal=True)
    if not isinstance(page_vo, Mapping):
        raise SourceSchemaError("huawei listing pagination was malformed")
    if not isinstance(records, list) or not records:
        raise SourceSchemaError("huawei listing page contained no postings")
    if any(not isinstance(record, Mapping) for record in records):
        raise SourceSchemaError("huawei listing contained a malformed posting")

    total = _positive_int(page_vo.get("totalRows"), label="listing total")
    total_pages = _positive_int(page_vo.get("totalPages"), label="listing page count")
    current_page = _positive_int(page_vo.get("curPage"), label="listing current page")
    page_size = _positive_int(page_vo.get("pageSize"), label="listing page size")
    start_index = _positive_int(page_vo.get("startIndex"), label="listing start index")
    end_index = _positive_int(page_vo.get("endIndex"), label="listing end index")
    if current_page != expected_page:
        raise SourceSchemaError("huawei listing current page did not match the request")
    if page_size != PAGE_SIZE:
        raise SourceSchemaError("huawei listing page size changed")
    if total_pages != (total + PAGE_SIZE - 1) // PAGE_SIZE:
        raise SourceSchemaError("huawei listing page count disagreed with its total")
    expected_start = (expected_page - 1) * PAGE_SIZE + 1
    expected_end = expected_start + len(records) - 1
    if start_index != expected_start:
        raise SourceSchemaError("huawei listing start index did not match the request")
    if end_index != expected_end or expected_end > total:
        raise SourceSchemaError("huawei listing end index was invalid")
    if len(records) > PAGE_SIZE:
        raise SourceSchemaError("huawei listing exceeded its page size")
    return _ListingPage(tuple(records), total, total_pages)


def _row(
    record: Mapping[str, object],
    company: CompanyCfg,
    *,
    locale: _Locale,
) -> dict:
    posting_id = _positive_int(record.get("advertisementId"), label="posting ID")
    job_id = _positive_int(record.get("jobId"), label="job ID")
    if record.get("scenarioCode") != "1":
        raise SourceSchemaError("huawei internship posting changed scenario scope")
    if record.get("scenarioName") != locale.scenario_name:
        raise SourceSchemaError("huawei internship posting changed localized scenario")

    title = _text(record.get("jobName"), label="posting title")
    location = _text(record.get("workPlace"), label="posting location")
    modified = _date(record.get("lastUpdateDate"), label="posting update date")
    description = html_to_text(_optional_text(record.get("mainBusiness")))
    requirements = html_to_text(_optional_text(record.get("jobRequire")))
    source_id = str(posting_id)

    return make_row(
        source="direct",
        source_adapter="huawei",
        company=company.name,
        title=title,
        location=location,
        description=description,
        requirements=requirements,
        source_url=(
            f"https://{CAREERS_HOST}/{locale.route}/job-details"
            f"?advertisementId={source_id}"
        ),
        date_posted="",
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_job_id": str(job_id),
            "source_system": "huawei_recruitment",
            "source_scope": _SCOPE,
            "source_completeness": "practical_partial",
            "source_locale": locale.route,
            "source_modified_date": modified,
            "category": _optional_text(record.get("categoryName")),
            "job_family": _optional_text(record.get("jobFamilyName")),
            "business_unit": _optional_text(record.get("deptName")),
            "source_location": _optional_text(record.get("jobAddress")),
            "active": True,
        },
    )


def _positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or not _POSITIVE_ID.fullmatch(str(value)):
        raise SourceSchemaError(f"huawei {label} was invalid")
    return value


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceSchemaError(f"huawei {label} was invalid")
    return value.strip()


def _optional_text(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError("huawei optional posting text was invalid")
    return value.strip()


def _date(value: object, *, label: str) -> str:
    text = _text(value, label=label)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise SourceSchemaError(f"huawei {label} was invalid") from exc
    if not _ISO_DATE.fullmatch(text) or parsed.isoformat() != text:
        raise SourceSchemaError(f"huawei {label} was invalid")
    return text
