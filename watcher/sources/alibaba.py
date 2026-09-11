"""Official Alibaba campus-internship practical-partial source.

Alibaba's current first-party campus frontend publishes an explicit
``internship`` batch category and lists the active batches through its own
anonymous JSON contract.  Those batches are useful and exactly enumerable,
but they are only one campus slice of a careers system that also exposes
separate graduate, social, domestic, and overseas channels.  This adapter is
therefore permanently incomplete and can never establish direct-complete
Alibaba coverage.

The anonymous frontend session supplies a short-lived CSRF value and cookies.
They stay in memory, are never logged or persisted, and are used exactly as the
public browser client uses them; no authenticated or access-controlled surface
is involved.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlencode, urlunsplit
from urllib.request import HTTPCookieProcessor, build_opener

from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceError,
    SourceSchemaError,
    TextHttpResponse,
)
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.rows import iso_date, make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import get_text_response, post_json_response


HOST = "campus-talent.alibaba.com"
HOME_URL = f"https://{HOST}/campus/index"
SOURCE_URL = f"https://{HOST}/campus/position"
BATCH_ENDPOINT = "/searchCondition/listBatch"
SEARCH_ENDPOINT = "/position/search"
CHANNEL = "new_campus_group_official_site"
LANGUAGE = "zh"
PAGE_SIZE = 100
MAX_PAGE_REQUESTS = 101
MAX_SNAPSHOT_PASSES = 3

_SCOPE = f"{HOST}:official_internship_batches"
_PARTIAL_REASON = "scope_not_completeness_proven"
_POSITIVE_ID = re.compile(r"[1-9][0-9]{0,17}")
_TOKEN = re.compile(r'__token__\s*:\s*"([^"]+)"')
_LOCALE = re.compile(r'\blocale\s*:\s*"([^"]+)"')
_CIRCLE = re.compile(r"\bcircle\s*:\s*(\{[^\n;]+\})")


@dataclass(frozen=True)
class _AnonymousSession:
    opener: Callable[..., Any]
    cookies: Iterable[object]


@dataclass(frozen=True)
class _Bootstrap:
    token: str


@dataclass(frozen=True)
class _Batch:
    batch_id: int
    name: str
    name_en: str
    batch_type: str

    @property
    def signature(self) -> tuple[int, str, str, str]:
        return (self.batch_id, self.name, self.name_en, self.batch_type)


@dataclass(frozen=True)
class _ListingPage:
    records: tuple[Mapping[str, object], ...]
    total: int
    page_size: int
    current_page: int
    terminal: bool = False


@dataclass(frozen=True)
class _Snapshot:
    rows: tuple[dict, ...]
    identities: frozenset[str]
    totals: tuple[tuple[int, int], ...]
    batches: tuple[tuple[int, str, str, str], ...]


class AlibabaSource(DirectDiagnosticsMixin):
    """Enumerate every current official internship batch without overclaiming."""

    name = "alibaba"

    def __init__(
        self,
        *,
        request_text: Callable[[str, str], Any] | None = None,
        request_json: Callable[[str, dict, str, dict[str, str]], Any] | None = None,
        session_factory: Callable[[], _AnonymousSession] | None = None,
        max_page_requests: int = MAX_PAGE_REQUESTS,
        max_snapshot_passes: int = MAX_SNAPSHOT_PASSES,
    ) -> None:
        if (
            type(max_page_requests) is not int
            or not 2 <= max_page_requests <= MAX_PAGE_REQUESTS
        ):
            raise ValueError(
                f"alibaba max_page_requests must be between 2 and {MAX_PAGE_REQUESTS}"
            )
        if (
            type(max_snapshot_passes) is not int
            or not 2 <= max_snapshot_passes <= MAX_SNAPSHOT_PASSES
        ):
            raise ValueError(
                "alibaba max_snapshot_passes must be between 2 and "
                f"{MAX_SNAPSHOT_PASSES}"
            )
        self._request_text = request_text
        self._request_json = request_json
        self._session_factory = session_factory or _new_anonymous_session
        self.max_page_requests = max_page_requests
        self.max_snapshot_passes = max_snapshot_passes
        self.request_count = 0
        self.bootstrap_requests = 0
        self.batch_requests = 0
        self.listing_requests = 0
        self.snapshot_passes_requested = 0
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint() -> str:
        return SOURCE_URL

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        self.bootstrap_requests = 0
        self.batch_requests = 0
        self.listing_requests = 0
        self.snapshot_passes_requested = 0
        _require_config(company)

        session = self._session_factory()
        bootstrap = self._bootstrap(session)
        previous: _Snapshot | None = None

        for _pass_number in range(1, self.max_snapshot_passes + 1):
            self.snapshot_passes_requested += 1
            batches = self._batches(session, bootstrap)
            snapshot = self._snapshot(
                company,
                session=session,
                bootstrap=bootstrap,
                batches=batches,
            )
            if (
                previous is not None
                and snapshot.batches == previous.batches
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
            "alibaba internship snapshot did not stabilize within the bounded pass limit"
        )

    def _bootstrap(self, session: _AnonymousSession) -> _Bootstrap:
        self.bootstrap_requests += 1
        self.request_count += 1
        if self._request_text is not None:
            response = self._request_text(HOME_URL, self.name)
        else:
            response = get_text_response(
                HOME_URL,
                self.name,
                max_response_bytes=512 * 1024,
                opener=session.opener,
            )
            if not tuple(session.cookies):
                raise SourceSchemaError(
                    "alibaba bootstrap did not establish an anonymous session"
                )
        html = response.text if isinstance(response, TextHttpResponse) else response
        return _parse_bootstrap(html)

    def _batches(
        self,
        session: _AnonymousSession,
        bootstrap: _Bootstrap,
    ) -> tuple[_Batch, ...]:
        self.batch_requests += 1
        payload = self._post(session, bootstrap, BATCH_ENDPOINT, {})
        return _batch_inventory(payload)

    def _snapshot(
        self,
        company: CompanyCfg,
        *,
        session: _AnonymousSession,
        bootstrap: _Bootstrap,
        batches: tuple[_Batch, ...],
    ) -> _Snapshot:
        rows: list[dict] = []
        identities: set[str] = set()
        urls: set[str] = set()
        totals: list[tuple[int, int]] = []

        for batch in batches:
            batch_rows, total = self._batch_rows(
                company,
                session=session,
                bootstrap=bootstrap,
                batch=batch,
            )
            totals.append((batch.batch_id, total))
            for row in batch_rows:
                source_id = str(row["extra"]["source_requisition_id"])
                source_url = str(row["source_url"])
                if source_id in identities:
                    raise SourceSchemaError(
                        "alibaba returned a duplicate posting ID across internship batches"
                    )
                if source_url in urls:
                    raise SourceSchemaError(
                        "alibaba returned a duplicate posting URL across internship batches"
                    )
                identities.add(source_id)
                urls.add(source_url)
                rows.append(row)

        return _Snapshot(
            rows=tuple(rows),
            identities=frozenset(identities),
            totals=tuple(totals),
            batches=tuple(batch.signature for batch in batches),
        )

    def _batch_rows(
        self,
        company: CompanyCfg,
        *,
        session: _AnonymousSession,
        bootstrap: _Bootstrap,
        batch: _Batch,
    ) -> tuple[list[dict], int]:
        rows: list[dict] = []
        identities: set[str] = set()
        urls: set[str] = set()
        expected_total: int | None = None
        raw_count = 0

        for page_number in range(1, self.max_page_requests + 1):
            self.listing_requests += 1
            payload = {
                "batchId": batch.batch_id,
                "pageIndex": page_number,
                "pageSize": PAGE_SIZE,
                "channel": CHANNEL,
                "language": LANGUAGE,
            }
            page = _listing_page(
                self._post(session, bootstrap, SEARCH_ENDPOINT, payload),
                expected_page=page_number,
            )

            if page.terminal:
                if expected_total is None:
                    if page_number != 1:
                        raise SourceSchemaError(
                            "alibaba internship pagination ended without a total"
                        )
                    return [], 0
                if raw_count != expected_total:
                    raise SourceSchemaError(
                        "alibaba internship pagination ended before the reported total"
                    )
                return rows, expected_total

            if expected_total is None:
                expected_total = page.total
            elif page.total != expected_total:
                raise SourceSchemaError(
                    "alibaba internship total changed during pagination"
                )
            raw_count += len(page.records)
            if raw_count > expected_total:
                raise SourceSchemaError(
                    "alibaba returned more internship postings than its total"
                )
            if raw_count < expected_total and len(page.records) != PAGE_SIZE:
                raise SourceSchemaError(
                    "alibaba internship pagination ended before the reported total"
                )

            for record in page.records:
                row = _row(record, company, batch=batch)
                source_id = str(row["extra"]["source_requisition_id"])
                source_url = str(row["source_url"])
                if source_id in identities:
                    raise SourceSchemaError(
                        "alibaba returned a duplicate posting ID within an internship batch"
                    )
                if source_url in urls:
                    raise SourceSchemaError(
                        "alibaba returned a duplicate posting URL within an internship batch"
                    )
                identities.add(source_id)
                urls.add(source_url)
                rows.append(row)

        raise SourceSchemaError(
            "alibaba reached the maximum page safeguard before an explicit terminal page"
        )

    def _post(
        self,
        session: _AnonymousSession,
        bootstrap: _Bootstrap,
        path: str,
        payload: dict,
    ) -> object:
        self.request_count += 1
        url = _api_url(path, bootstrap.token)
        headers = {"Referer": SOURCE_URL, "X-Requested-With": "XMLHttpRequest"}
        if self._request_json is not None:
            response = self._request_json(url, payload, self.name, headers)
        else:
            response = post_json_response(
                url,
                payload,
                self.name,
                request_headers=headers,
                opener=session.opener,
            )
        return response.payload if isinstance(response, JsonHttpResponse) else response


def _new_anonymous_session() -> _AnonymousSession:
    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies))
    return _AnonymousSession(opener.open, cookies)


def _api_url(path: str, token: str) -> str:
    return urlunsplit(("https", HOST, path, urlencode({"_csrf": token}), ""))


def _require_config(company: CompanyCfg) -> None:
    if str(company.source_url or "").strip() != SOURCE_URL:
        raise SourceError(
            f"alibaba requires the official campus internship listing for {company.name}"
        )


def _parse_bootstrap(html: object) -> _Bootstrap:
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError("alibaba bootstrap response was empty")
    tokens = _TOKEN.findall(html)
    locales = _LOCALE.findall(html)
    circles = _CIRCLE.findall(html)
    if len(tokens) != 1 or len(locales) != 1 or len(circles) != 1:
        raise SourceSchemaError("alibaba bootstrap contract was ambiguous")
    token = tokens[0]
    if not re.fullmatch(r"[A-Za-z0-9._~-]{8,256}", token):
        raise SourceSchemaError("alibaba bootstrap request token was invalid")
    if locales[0] != LANGUAGE:
        raise SourceSchemaError("alibaba bootstrap language changed")
    try:
        circle = json.loads(circles[0])
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SourceSchemaError("alibaba bootstrap circle configuration was malformed") from exc
    if not isinstance(circle, dict) or {
        "portalCampusChannel": circle.get("portalCampusChannel"),
        "portalDomain": circle.get("portalDomain"),
        "circleCode": circle.get("circleCode"),
    } != {
        "portalCampusChannel": CHANNEL,
        "portalDomain": f"https://{HOST}",
        "circleCode": "1002",
    }:
        raise SourceSchemaError("alibaba bootstrap changed the campus organization scope")
    return _Bootstrap(token=token)


def _api_content(payload: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise SourceSchemaError(f"alibaba {label} response expected an object")
    if payload.get("success") is not True:
        raise SourceSchemaError(f"alibaba {label} API reported failure")
    content = payload.get("content")
    if not isinstance(content, Mapping):
        raise SourceSchemaError(f"alibaba {label} response is missing content")
    return content


def _batch_inventory(payload: object) -> tuple[_Batch, ...]:
    content = _api_content(payload, label="batch discovery")
    sequence = content.get("sequence")
    if not isinstance(sequence, list) or sequence.count("internship") != 1:
        raise SourceSchemaError(
            "alibaba batch discovery lacks one internship batch category"
        )
    raw_batches = content.get("internship")
    if not isinstance(raw_batches, list):
        raise SourceSchemaError(
            "alibaba batch discovery lacks an internship batch category"
        )

    batches: list[_Batch] = []
    seen: set[int] = set()
    for raw in raw_batches:
        if not isinstance(raw, Mapping):
            raise SourceSchemaError("alibaba internship batch was malformed")
        batch_id = _positive_id(raw.get("id"), label="internship batch ID")
        name = _text(raw.get("name"), label="internship batch name")
        name_en = _text(raw.get("enName"), label="internship batch English name")
        batch_type = _text(raw.get("type"), label="internship batch type")
        if batch_id in seen:
            raise SourceSchemaError("alibaba returned a duplicate internship batch ID")
        seen.add(batch_id)
        batches.append(_Batch(batch_id, name, name_en, batch_type))
    return tuple(batches)


def _listing_page(payload: object, *, expected_page: int) -> _ListingPage:
    content = _api_content(payload, label="listing")
    records = content.get("datas")
    total = content.get("totalCount")
    page_size = content.get("pageSize")
    current_page = content.get("currentPage")

    if records is None:
        if (total, page_size, current_page) != (0, 0, 0):
            raise SourceSchemaError("alibaba listing terminal response was inconsistent")
        return _ListingPage((), 0, 0, 0, terminal=True)
    if not isinstance(records, list) or not records:
        raise SourceSchemaError("alibaba listing page contained no postings")
    if type(total) is not int or total <= 0:
        raise SourceSchemaError("alibaba listing total was invalid")
    if page_size != PAGE_SIZE:
        raise SourceSchemaError("alibaba listing page size changed")
    if current_page != expected_page:
        raise SourceSchemaError("alibaba listing current page did not match the request")
    if len(records) > PAGE_SIZE or len(records) > total:
        raise SourceSchemaError("alibaba listing page exceeded its reported bounds")
    if any(not isinstance(record, Mapping) for record in records):
        raise SourceSchemaError("alibaba listing contained a malformed posting")
    return _ListingPage(tuple(records), total, page_size, current_page)


def _row(record: Mapping[str, object], company: CompanyCfg, *, batch: _Batch) -> dict:
    posting_id = _positive_id(record.get("id"), label="posting ID")
    if record.get("batchId") != batch.batch_id:
        raise SourceSchemaError("alibaba posting batch ID changed scope")
    if record.get("batchName") != batch.name:
        raise SourceSchemaError("alibaba posting batch name changed scope")
    if record.get("status") != "recruit":
        raise SourceSchemaError("alibaba listing contained a non-recruiting posting")
    if record.get("categoryType") != "project":
        raise SourceSchemaError("alibaba internship posting changed category scope")

    title = _text(record.get("name"), label="posting title")
    locations = _text_list(record.get("workLocations"), label="posting locations")
    description = html_to_text(
        _text(record.get("description"), label="posting description")
    )
    requirements = html_to_text(
        _text(record.get("requirement"), label="posting requirements")
    )
    circles = _text_list(record.get("circleNames"), label="posting organizations")
    modified = record.get("modifyTime")
    if type(modified) is not int or modified <= 0:
        raise SourceSchemaError("alibaba posting modification date was invalid")
    published = record.get("publishTime")
    if published is not None and (type(published) is not int or published <= 0):
        raise SourceSchemaError("alibaba posting publication date was invalid")

    source_id = str(posting_id)
    return make_row(
        source="direct",
        source_adapter="alibaba",
        company=company.name,
        title=title,
        location=", ".join(locations),
        description=description,
        requirements=requirements,
        source_url=f"https://{HOST}/campus/position/{source_id}",
        date_posted=iso_date(published),
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "alibaba_campus",
            "source_scope": _SCOPE,
            "source_completeness": "practical_partial",
            "official_internship_batch_id": str(batch.batch_id),
            "official_internship_batch_name": batch.name,
            "official_internship_batch_name_en": batch.name_en,
            "source_modified_date": iso_date(modified),
            "business_units": list(circles),
            "active": True,
        },
    )


def _positive_id(value: object, *, label: str) -> int:
    if type(value) is not int or not _POSITIVE_ID.fullmatch(str(value)):
        raise SourceSchemaError(f"alibaba {label} was invalid")
    return value


def _text(value: object, *, label: str) -> str:
    if not isinstance(value, str):
        raise SourceSchemaError(f"alibaba {label} was invalid")
    text = " ".join(value.split())
    if not text or len(text) > 20_000:
        raise SourceSchemaError(f"alibaba {label} was invalid")
    return text


def _text_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > 50:
        raise SourceSchemaError(f"alibaba {label} were invalid")
    values: list[str] = []
    for item in value:
        text = _text(item, label=label)
        if text in values:
            raise SourceSchemaError(f"alibaba {label} contained duplicates")
        values.append(text)
    return tuple(values)
