"""Epic's official complete careers-site direct source."""

from __future__ import annotations

import json
import random
import re
import time
from html.parser import HTMLParser
from json import JSONDecodeError
from typing import Any, Callable, Mapping
from urllib.parse import quote, urlsplit

from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceSchemaError,
    TextHttpResponse,
)
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.parsing import parse_records
from watcher.sources.retry import (
    DEFAULT_MAX_ATTEMPTS,
    RequestRetrier,
    RetryPolicy,
)
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import get_json_response, get_text_response

CAREERS_HOST = "careers.epic.com"
DETAIL_HOST = "epic.avature.net"
JOBS_URL = f"https://{CAREERS_HOST}/jobs/"
SEARCH_URL = f"https://{CAREERS_HOST}/cached-api/jobs/search/"
DEFAULT_MAX_PAGE_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_SEARCH_BYTES = 1024 * 1024
_NATIVE_ID = re.compile(r"[1-9][0-9]*")
_NEXT_PUSH = re.compile(r"\s*self\.__next_f\.push\((.*)\)\s*", re.DOTALL)
_GENERIC_TITLES = frozenset(
    {
        "available positions",
        "careers",
        "jobs",
        "search",
        "search jobs",
        "sign in",
    }
)


class _FlightScriptParser(HTMLParser):
    """Retain only inline Next.js Flight script text from the jobs page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._active = False
        self._text: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag == "script":
            self._active = True
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._active:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._active:
            self.scripts.append("".join(self._text))
            self._active = False
            self._text = []


class EpicSource(DirectDiagnosticsMixin):
    """Collect Epic's complete official Next.js-published job set."""

    name = "epic"

    def __init__(
        self,
        *,
        request_text: Callable[[str, str], Any] | None = None,
        request_json: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        self._request_text = request_text
        self._request_json = request_json
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.pages_requested = 0
        self.job_set_checks = 0
        self.page_response_metadata: dict[str, object] = {}
        self.search_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def endpoint() -> str:
        return JOBS_URL

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self.pages_requested = 1
        self.job_set_checks = 1
        self.page_response_metadata = {}
        self.search_response_metadata = {}

        html = self._request_page()
        open_records, positions = _next_jobs_contract(html)
        page_ids, page_duplicates = _posting_ids(
            open_records,
            contract="Next.js open-job",
        )
        search_ids, search_duplicates = _posting_ids(
            self._request_search(),
            contract="search API",
        )
        if set(page_ids) != set(search_ids):
            raise SourceSchemaError(
                "epic official page and search API job sets did not match"
            )

        records: list[object] = []
        for posting_id in page_ids:
            metadata = positions.get(posting_id)
            if not isinstance(metadata, Mapping):
                records.append(metadata)
                continue
            record = dict(metadata)
            record["_posting_id"] = posting_id
            records.append(record)

        rows = parse_records(
            records,
            lambda record: _parse_posting(record, company),
            source_name=self.name,
            company_name=company.name,
            diagnostics=self._record_parse_diagnostics,
        )
        parse_loss = bool(
            self._diagnostic_malformed_rows or self._diagnostic_schema_rows
        )
        recovered = bool(self.retry_attempts) and not parse_loss
        reasons = ("request_retry_recovered",) if self.retry_attempts else ()
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=page_duplicates + search_duplicates,
            failed_request_count=self.retry_attempts,
            degraded=True if recovered else None,
            complete=True if recovered else None,
            reason_codes=reasons,
        )
        return rows

    def _request_page(self) -> str:
        request = self._request_text or _get_page
        response = self._request_with_retry(request, JOBS_URL)
        if isinstance(response, TextHttpResponse):
            self.page_response_metadata = dict(response.metadata)
            return response.text
        if not isinstance(response, str):
            raise SourceSchemaError("epic expected an HTML jobs response")
        return response

    def _request_search(self) -> object:
        request = self._request_json or _get_search
        response = self._request_with_retry(request, SEARCH_URL)
        if isinstance(response, JsonHttpResponse):
            self.search_response_metadata = dict(response.metadata)
            return response.payload
        return response

    def _request_with_retry(
        self,
        request: Callable[[str, str], Any],
        url: str,
    ) -> Any:
        return self._retrier.run(lambda: request(url, self.name))


def _get_page(url: str, source_name: str) -> TextHttpResponse:
    return get_text_response(
        url,
        source_name,
        max_response_bytes=DEFAULT_MAX_PAGE_BYTES,
    )


def _get_search(url: str, source_name: str) -> JsonHttpResponse:
    return get_json_response(
        url,
        source_name,
        max_response_bytes=DEFAULT_MAX_SEARCH_BYTES,
    )


def _next_jobs_contract(html: object) -> tuple[list[object], Mapping[str, object]]:
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError("epic Next.js jobs contract response was empty")
    parser = _FlightScriptParser()
    try:
        parser.feed(html)
        parser.close()
    except (TypeError, ValueError) as exc:
        raise SourceSchemaError(
            "epic Next.js jobs contract contained malformed HTML"
        ) from exc

    chunks: list[str] = []
    malformed_push = False
    for script in parser.scripts:
        match = _NEXT_PUSH.fullmatch(script)
        if not match:
            continue
        try:
            value = json.loads(match.group(1))
        except (JSONDecodeError, TypeError, ValueError):
            malformed_push = True
            continue
        if (
            isinstance(value, list)
            and len(value) > 1
            and isinstance(value[1], str)
        ):
            chunks.append(value[1])
    flight = "".join(chunks)
    try:
        open_jobs = _flight_value(flight, "allOpenJobs")
        positions = _flight_value(flight, "avaturePositions")
    except SourceSchemaError:
        if malformed_push:
            raise SourceSchemaError(
                "epic Next.js jobs contract contained malformed Flight data"
            ) from None
        raise
    if not isinstance(open_jobs, list):
        raise SourceSchemaError(
            "epic Next.js jobs contract expected allOpenJobs to be a list"
        )
    if not isinstance(positions, Mapping):
        raise SourceSchemaError(
            "epic Next.js jobs contract expected avaturePositions to be an object"
        )
    return open_jobs, positions


def _flight_value(flight: str, key: str) -> object:
    marker = f'"{key}":'
    if flight.count(marker) != 1:
        raise SourceSchemaError(
            f"epic Next.js jobs contract lacks one unambiguous {key} field"
        )
    start = flight.index(marker) + len(marker)
    try:
        value, _ = json.JSONDecoder().raw_decode(flight[start:])
    except (JSONDecodeError, TypeError, ValueError) as exc:
        raise SourceSchemaError(
            f"epic Next.js jobs contract has malformed {key} data"
        ) from exc
    return value


def _posting_ids(
    records: object,
    *,
    contract: str,
) -> tuple[list[str], int]:
    if not isinstance(records, list):
        raise SourceSchemaError(f"epic {contract} contract expected a list")
    ordered: list[str] = []
    seen: dict[str, Mapping[str, object]] = {}
    duplicates = 0
    for record in records:
        if not isinstance(record, Mapping):
            raise SourceSchemaError(f"epic {contract} posting ID entry is malformed")
        value = record.get("id")
        if isinstance(value, bool):
            raise SourceSchemaError(f"epic {contract} posting ID is invalid")
        posting_id = str(value or "").strip()
        if not _NATIVE_ID.fullmatch(posting_id):
            raise SourceSchemaError(f"epic {contract} posting ID is invalid")
        previous = seen.get(posting_id)
        if previous is not None:
            if dict(previous) != dict(record):
                raise SourceSchemaError(
                    f"epic {contract} returned a conflicting posting ID"
                )
            duplicates += 1
            continue
        seen[posting_id] = record
        ordered.append(posting_id)
    return ordered, duplicates


def _parse_posting(record: object, company: CompanyCfg) -> dict:
    if not isinstance(record, Mapping):
        raise SourceSchemaError("epic posting metadata is malformed")
    posting_id = str(record.get("_posting_id") or "").strip()
    if not _NATIVE_ID.fullmatch(posting_id):
        raise SourceSchemaError("epic posting ID is invalid")
    title_value = record.get("externalName")
    if not isinstance(title_value, str):
        raise SourceSchemaError("epic posting title is blank or generic")
    title = html_to_text(title_value)
    if not title or title.casefold() in _GENERIC_TITLES:
        raise SourceSchemaError("epic posting title is blank or generic")
    if record.get("isOpen") is not True or record.get("isPublished") is not True:
        raise SourceSchemaError("epic posting is not open and published")
    description = _optional_text(record.get("shortSummary"), "shortSummary")
    requirements = _optional_text(record.get("background"), "background")
    source_url = _posting_url(title, posting_id)
    source_id = f"epic:{posting_id}"
    extra = {
        "source_id": source_id,
        "source_requisition_id": source_id,
        "epic_native_id": posting_id,
    }
    reference_number = record.get("referenceNumber")
    if (
        not isinstance(reference_number, bool)
        and isinstance(reference_number, int)
        and reference_number > 0
    ):
        extra["epic_reference_number"] = str(reference_number)
    return make_row(
        source="direct",
        source_adapter="epic",
        company=company.name,
        title=title,
        location="",
        description=description,
        requirements=requirements,
        source_url=source_url,
        extra=extra,
    )


def _optional_text(value: object, field: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError(f"epic posting {field} is malformed")
    return html_to_text(value)


def _posting_url(title: str, posting_id: str) -> str:
    slug = re.sub(r"\s+", "-", title)
    slug = re.sub(r"[ /]", "-", slug)
    slug = re.sub(r"[,()]", "", slug)
    slug = quote(slug, safe="-._~")
    url = f"https://{DETAIL_HOST}/Careers/FolderDetail/{slug}/{posting_id}"
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise SourceSchemaError("epic posting detail URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != DETAIL_HOST
        or port is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(
            rf"/Careers/FolderDetail/[^/]+/{re.escape(posting_id)}",
            parsed.path,
        )
    ):
        raise SourceSchemaError("epic posting detail URL is invalid or generic")
    return url
