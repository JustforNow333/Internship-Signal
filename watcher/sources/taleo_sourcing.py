"""Oracle Taleo Enterprise Sourcing (SelectMinds) direct source adapter.

This adapter targets the Taleo Enterprise Sourcing / SelectMinds product only:
an anonymous portal that bootstraps a session on its home page, creates a
server-side job search, and renders paginated listing HTML through JSON AJAX
responses. It is deliberately not a generic Taleo adapter and shares nothing
with Taleo Career Section products.
"""

from __future__ import annotations

import math
import random
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, build_opener

from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceError,
    SourceSchemaError,
    TextHttpResponse,
)
from watcher.sources.direct import DirectRecordAdapter
from watcher.sources.parsing import page_fingerprint
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import make_row
from watcher.sources.transport import (
    get_text_response,
    post_form_response,
)

# A fully enumerated portal can need many sequential page requests, so a
# per-request attempt limit alone does not bound how long one crawl may run.
DEFAULT_MAX_PAGES = 500
DEFAULT_MAX_CRAWL_RETRIES = 5
# Conservative spacing between sequential listing pages of one crawl.
DEFAULT_PAGE_DELAY_SECONDS = 0.25
MAX_PAGE_DELAY_SECONDS = 5.0

_SEARCH_PATH = "/ajax/jobs/search/create"
_RESULTS_PATH = "/ajax/content/job_results"
_POSTING_PATH_PREFIX = "/jobs/"
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]{0,17}")
_SITE_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?")
_ROW_ID = re.compile(r"job_list_([1-9][0-9]{0,17})\Z")
# The portal publishes its site identity in an inline configuration block.
_SHORT_NAME = re.compile(r"short_name\s*:\s*\"([A-Za-z0-9._-]{1,64})\"")
_MAX_TOKEN_LENGTH = 512
_MAX_FIELD_LENGTH = 2000


@dataclass(frozen=True)
class _AnonymousSession:
    opener: Callable[..., Any]
    cookies: Iterable[object]


@dataclass(frozen=True)
class _Bootstrap:
    home_url: str
    request_token: str


@dataclass(frozen=True)
class _ResultsPage:
    records: tuple[Mapping[str, str], ...]
    total_results: int
    current_page: int
    total_pages: int

    @property
    def fingerprint(self) -> str:
        return page_fingerprint([dict(record) for record in self.records])


class _HiddenTokenParser(HTMLParser):
    """Collect only the hidden request-verification token input values."""

    def __init__(self) -> None:
        super().__init__()
        self.tokens: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "input":
            return
        fields = {str(key).casefold(): value for key, value in attrs}
        name = str(fields.get("name") or fields.get("id") or "").casefold()
        if name == "tsstoken":
            self.tokens.append(str(fields.get("value") or ""))


class _ResultsParser(HTMLParser):
    """Extract only the bounded listing contract from one results payload."""

    _TEXT_CLASSES = {
        "jlr_company": "region",
        "location": "location",
        "category": "category",
        "jlr_description": "description",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[dict[str, str]] = []
        self.total_results: str | None = None
        self.total_pages: str | None = None
        self.current_page: str | None = None
        self.structural_error = False
        self._depth = 0
        self._row: dict[str, str] | None = None
        self._row_depth = 0
        self._capture_field: str | None = None
        self._capture_depth = 0
        self._capture_text: list[str] = []
        self._link_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._depth += 1
        fields = {str(key).casefold(): (value or "") for key, value in attrs}
        classes = set(str(fields.get("class", "")).split())
        element_id = str(fields.get("id", ""))

        if "job_list_row" in classes:
            if self._row is not None:
                self.structural_error = True
                return
            match = _ROW_ID.search(element_id)
            self._row = {"id": match.group(1) if match else "", "title": "", "url": ""}
            self._row_depth = self._depth
            return

        if tag.casefold() == "a" and "job_link" in classes and self._row is not None:
            if self._row["url"]:
                self.structural_error = True
                return
            self._row["url"] = str(fields.get("href", "")).strip()
            self._begin_capture("title")
            self._link_depth = self._depth
            return

        for css_class, field in self._TEXT_CLASSES.items():
            if css_class in classes and self._row is not None:
                self._begin_capture(field)
                return

        if element_id == "jPaginateNumPages":
            self._begin_capture("__total_pages")
        elif element_id == "jPaginateCurrPage":
            self._begin_capture("__current_page")
        elif "total_results" in classes:
            self._begin_capture("__total_results")

    def handle_endtag(self, tag: str) -> None:
        if self._capture_field is not None and self._depth <= self._capture_depth:
            self._end_capture()
        if self._row is not None and self._depth <= self._row_depth:
            self.records.append(self._row)
            self._row = None
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data: str) -> None:
        if self._capture_field is not None:
            self._capture_text.append(data)

    def close(self) -> None:  # pragma: no cover - defensive completion
        super().close()
        if self._capture_field is not None:
            self._end_capture()
        if self._row is not None:
            self.records.append(self._row)
            self._row = None

    def _begin_capture(self, field: str) -> None:
        if self._capture_field is not None:
            # Nested captures would silently merge unrelated listing text.
            self.structural_error = True
            return
        self._capture_field = field
        self._capture_depth = self._depth
        self._capture_text = []

    def _end_capture(self) -> None:
        field = self._capture_field
        value = " ".join("".join(self._capture_text).split())[:_MAX_FIELD_LENGTH]
        self._capture_field = None
        self._capture_text = []
        if field is None:
            return
        if field == "__total_results":
            self.total_results = value
        elif field == "__total_pages":
            self.total_pages = value
        elif field == "__current_page":
            self.current_page = value
        elif self._row is not None:
            if field == "title":
                self._row["title"] = value
            else:
                self._row.setdefault(field, value)


class TaleoSourcingSource(DirectRecordAdapter):
    """Fully enumerate one anonymous Taleo Enterprise Sourcing portal."""

    name = "taleo_sourcing"

    def __init__(
        self,
        *,
        session_factory: Callable[[], _AnonymousSession] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_crawl_retries: int = DEFAULT_MAX_CRAWL_RETRIES,
        max_pages: int = DEFAULT_MAX_PAGES,
        page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS,
    ) -> None:
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        if not 0.0 <= page_delay_seconds <= MAX_PAGE_DELAY_SECONDS:
            raise ValueError(
                f"page_delay_seconds must be between 0 and {MAX_PAGE_DELAY_SECONDS}"
            )
        self._session_factory = session_factory or _new_anonymous_session
        self._sleeper = sleeper
        self._retrier = RequestRetrier(
            policy=RetryPolicy(
                max_attempts=max_attempts,
                max_crawl_retries=max_crawl_retries,
            ),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.max_pages = max_pages
        self.page_delay_seconds = float(page_delay_seconds)
        self.bootstrap_requests = 0
        self.search_requests = 0
        self.pages_requested = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def results_endpoint(
        host: str,
        *,
        search_id: int,
        site: str,
        page_index: int,
    ) -> str:
        query = urlencode(
            (
                ("JobSearch.id", search_id),
                ("page_index", page_index),
                ("site-name", site),
                ("include_site", "true"),
            )
        )
        return f"https://{host}{_RESULTS_PATH}?{query}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self.bootstrap_requests = 0
        self.search_requests = 0
        self.pages_requested = 0
        self.last_response_metadata = {}
        host, site = _required_config(company)
        session = self._session_factory()
        bootstrap = self._bootstrap(session, host=host, site=site)
        search_id = self._create_search(session, bootstrap=bootstrap, host=host)
        rows = self._enumerate(
            company,
            session=session,
            bootstrap=bootstrap,
            host=host,
            site=site,
            search_id=search_id,
        )
        return self._finish(rows)

    def _bootstrap(
        self,
        session: _AnonymousSession,
        *,
        host: str,
        site: str,
    ) -> _Bootstrap:
        home_url = f"https://{host}/"
        self.bootstrap_requests += 1

        def attempt() -> TextHttpResponse:
            return get_text_response(home_url, self.name, opener=session.opener)

        response = self._retrier.run(attempt)
        if not tuple(session.cookies):
            raise SourceSchemaError(
                "taleo_sourcing bootstrap did not establish an anonymous session cookie"
            )
        return _parse_bootstrap(response.text, home_url=home_url, site=site)

    def _create_search(
        self,
        session: _AnonymousSession,
        *,
        bootstrap: _Bootstrap,
        host: str,
    ) -> int:
        endpoint = f"https://{host}{_SEARCH_PATH}"
        self.search_requests += 1

        def attempt() -> JsonHttpResponse:
            return post_form_response(
                endpoint,
                {"keywords": ""},
                self.name,
                request_headers=_ajax_headers(bootstrap),
                opener=session.opener,
            )

        response = self._retrier.run(attempt)
        self.last_response_metadata = dict(response.metadata)
        return _search_id(response.payload)

    def _enumerate(
        self,
        company: CompanyCfg,
        *,
        session: _AnonymousSession,
        bootstrap: _Bootstrap,
        host: str,
        site: str,
        search_id: int,
    ) -> list[dict]:
        expected_total: int | None = None
        expected_pages: int | None = None
        page_size: int | None = None
        raw_seen = 0
        seen_pages: set[str] = set()
        rows: list[dict] = []
        rows_by_id: dict[str, dict] = {}
        ids_by_url: dict[str, str] = {}

        for page_index in range(1, self.max_pages + 1):
            if page_index > 1 and self.page_delay_seconds:
                self._sleeper(self.page_delay_seconds)
            page = self._fetch_page(
                session,
                bootstrap=bootstrap,
                host=host,
                site=site,
                search_id=search_id,
                page_index=page_index,
            )
            if expected_total is None:
                expected_total = page.total_results
                expected_pages = page.total_pages
            elif page.total_results != expected_total:
                raise SourceSchemaError(
                    "taleo_sourcing total changed during pagination"
                )
            elif page.total_pages != expected_pages:
                raise SourceSchemaError(
                    "taleo_sourcing page count changed during pagination"
                )
            if page.current_page != page_index:
                raise SourceSchemaError(
                    f"taleo_sourcing returned page {page.current_page}; "
                    f"expected {page_index}"
                )

            if expected_total == 0:
                if page.records or page_index != 1 or page.total_pages != 0:
                    raise SourceSchemaError(
                        "taleo_sourcing zero-result response was inconsistent"
                    )
                return []
            if expected_pages is not None and expected_pages < 1:
                raise SourceSchemaError(
                    "taleo_sourcing reported results without any result page"
                )
            if not page.records:
                raise SourceSchemaError(
                    "taleo_sourcing pagination ended before the reported total"
                )

            fingerprint = page.fingerprint
            if fingerprint in seen_pages:
                raise SourceSchemaError(
                    "taleo_sourcing returned a repeated pagination page"
                )
            seen_pages.add(fingerprint)

            if page_size is None:
                page_size = len(page.records)
                if expected_pages != math.ceil(expected_total / page_size):
                    raise SourceSchemaError(
                        "taleo_sourcing page metadata disagrees with its reported total"
                    )
            elif page_index < expected_pages and len(page.records) != page_size:
                raise SourceSchemaError(
                    "taleo_sourcing pagination ended prematurely"
                )

            raw_seen += len(page.records)
            if raw_seen > expected_total:
                raise SourceSchemaError(
                    "taleo_sourcing returned more records than the reported total"
                )

            parsed = self._parse_direct_records(
                list(page.records),
                company,
                lambda record: _parse_posting(
                    record,
                    company,
                    host=host,
                    site=site,
                ),
            )
            for row in parsed:
                source_id = str(row["extra"]["source_requisition_id"])
                source_url = row["source_url"]
                if source_id in rows_by_id:
                    raise SourceSchemaError(
                        "taleo_sourcing returned a duplicate posting ID"
                    )
                other_id = ids_by_url.get(source_url)
                if other_id is not None and other_id != source_id:
                    raise SourceSchemaError(
                        "taleo_sourcing returned one posting URL for conflicting IDs"
                    )
                rows_by_id[source_id] = row
                ids_by_url[source_url] = source_id
                rows.append(row)

            if raw_seen == expected_total:
                if page_index != expected_pages:
                    raise SourceSchemaError(
                        "taleo_sourcing finished before its reported final page"
                    )
                return rows
            if page_index == expected_pages:
                raise SourceSchemaError(
                    "taleo_sourcing final page did not complete the reported total"
                )
            if page_index == self.max_pages:
                raise SourceSchemaError(
                    "taleo_sourcing reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable Taleo Sourcing pagination state")

    def _fetch_page(
        self,
        session: _AnonymousSession,
        *,
        bootstrap: _Bootstrap,
        host: str,
        site: str,
        search_id: int,
        page_index: int,
    ) -> _ResultsPage:
        endpoint = self.results_endpoint(
            host,
            search_id=search_id,
            site=site,
            page_index=page_index,
        )
        self.pages_requested += 1

        def attempt() -> JsonHttpResponse:
            return post_form_response(
                endpoint,
                {},
                self.name,
                request_headers=_ajax_headers(bootstrap),
                opener=session.opener,
            )

        response = self._retrier.run(attempt)
        self.last_response_metadata = dict(response.metadata)
        return _results_page(response.payload)

    def _finish(self, rows: list[dict]) -> list[dict]:
        parse_loss = bool(
            self._diagnostic_malformed_rows or self._diagnostic_schema_rows
        )
        recovered = bool(self.retry_attempts)
        reasons = ("request_retry_recovered",) if recovered else ()
        self._finish_direct_diagnostics(
            rows,
            failed_request_count=self.retry_attempts,
            incomplete=parse_loss,
            degraded=True if recovered else None,
            complete=True if recovered and not parse_loss else None,
            reason_codes=reasons,
        )
        return rows


def _new_anonymous_session() -> _AnonymousSession:
    cookies = CookieJar()
    opener = build_opener(HTTPCookieProcessor(cookies))
    return _AnonymousSession(opener.open, cookies)


def _ajax_headers(bootstrap: _Bootstrap) -> dict[str, str]:
    return {
        "Referer": bootstrap.home_url,
        "X-Requested-With": "XMLHttpRequest",
        "tss-token": bootstrap.request_token,
    }


def _required_config(company: CompanyCfg) -> tuple[str, str]:
    host = str(company.taleo_sourcing_host or "").strip().casefold()
    site = str(company.taleo_sourcing_site or "").strip()
    if not host:
        raise SourceError(
            f"taleo_sourcing requires taleo_sourcing_host for {company.name}"
        )
    if not _SITE_NAME.fullmatch(site):
        raise SourceError(
            f"taleo_sourcing requires a valid taleo_sourcing_site for {company.name}"
        )
    return host, site


def _parse_bootstrap(html: Any, *, home_url: str, site: str) -> _Bootstrap:
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError("taleo_sourcing bootstrap response was empty")
    parser = _HiddenTokenParser()
    parser.feed(html)
    if len(parser.tokens) != 1:
        raise SourceSchemaError(
            "taleo_sourcing bootstrap expected exactly one request token"
        )
    token = parser.tokens[0].strip()
    if (
        not token
        or len(token) > _MAX_TOKEN_LENGTH
        or any(ord(char) < 32 or ord(char) == 127 for char in token)
    ):
        raise SourceSchemaError(
            "taleo_sourcing bootstrap contained an invalid request token"
        )
    published = {match.group(1) for match in _SHORT_NAME.finditer(html)}
    if len(published) != 1:
        raise SourceSchemaError(
            "taleo_sourcing bootstrap expected exactly one site identifier"
        )
    if published != {site}:
        raise SourceSchemaError(
            "taleo_sourcing bootstrap site identifier did not match configuration"
        )
    return _Bootstrap(home_url=home_url, request_token=token)


def _search_id(payload: Any) -> int:
    if not isinstance(payload, dict):
        raise SourceSchemaError("taleo_sourcing expected a JSON object")
    result = payload.get("Result")
    if not isinstance(result, dict):
        raise SourceSchemaError("taleo_sourcing search response is missing Result")
    search_id = result.get("JobSearch.id")
    if isinstance(search_id, bool) or not isinstance(search_id, int) or search_id <= 0:
        raise SourceSchemaError(
            "taleo_sourcing search response did not return a positive JobSearch.id"
        )
    return search_id


def _results_page(payload: Any) -> _ResultsPage:
    if not isinstance(payload, dict):
        raise SourceSchemaError("taleo_sourcing expected a JSON object")
    html = payload.get("Result")
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError(
            "taleo_sourcing results response did not contain listing HTML"
        )
    parser = _ResultsParser()
    parser.feed(html)
    parser.close()
    if parser.structural_error:
        raise SourceSchemaError("taleo_sourcing listing structure was ambiguous")
    total_results = _count(parser.total_results, "total result count")
    total_pages = _page_count(parser.total_pages)
    current_page = _count(parser.current_page, "current page index")
    if current_page < 1:
        raise SourceSchemaError("taleo_sourcing current page index must be positive")
    if total_pages and current_page > total_pages:
        raise SourceSchemaError("taleo_sourcing pagination metadata is invalid")
    return _ResultsPage(
        records=tuple(parser.records),
        total_results=total_results,
        current_page=current_page,
        total_pages=total_pages,
    )


def _count(value: Any, label: str) -> int:
    raw = str(value or "").strip()
    if not raw.isdigit() or len(raw) > 18:
        raise SourceSchemaError(
            f"taleo_sourcing listing is missing an explicit {label}"
        )
    return int(raw)


def _page_count(value: Any) -> int:
    """Return the page count the portal publishes as a decimal string."""

    raw = str(value or "").strip()
    if not re.fullmatch(r"[0-9]{1,18}(?:\.0+)?", raw):
        raise SourceSchemaError(
            "taleo_sourcing listing is missing an explicit page count"
        )
    return int(raw.split(".", 1)[0])


def _parse_posting(
    record: Any,
    company: CompanyCfg,
    *,
    host: str,
    site: str,
) -> dict:
    if not isinstance(record, Mapping):
        raise SourceSchemaError("taleo_sourcing expected each posting to be a record")
    native_id = str(record.get("id") or "").strip()
    title = str(record.get("title") or "").strip()
    if not _POSITIVE_INTEGER.fullmatch(native_id) or not title:
        raise SourceSchemaError(
            "taleo_sourcing posting missing a numeric posting ID or title"
        )
    source_url = _posting_url(record.get("url"), host=host, native_id=native_id)
    source_id = f"taleo_sourcing:{host}:{site}:{native_id}"
    return make_row(
        source="direct",
        source_adapter="taleo_sourcing",
        company=company.name,
        title=title,
        location=str(record.get("location") or "").strip(),
        description=str(record.get("description") or "").strip(),
        source_url=source_url,
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "taleo_sourcing",
            "source_scope": f"{host}:{site}",
            "taleo_sourcing_native_id": native_id,
            "taleo_sourcing_site": site,
            "category": str(record.get("category") or "").strip()[:500],
            "region": str(record.get("region") or "").strip()[:500],
            "active": True,
        },
    )


def _posting_url(value: Any, *, host: str, native_id: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SourceSchemaError("taleo_sourcing posting URL is invalid") from exc
    path = parsed.path
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.query
        or parsed.fragment
        or not path.startswith(_POSTING_PATH_PREFIX)
        or not path.endswith(f"-{native_id}")
        or "/" in path[len(_POSTING_PATH_PREFIX) :]
    ):
        raise SourceSchemaError(
            "taleo_sourcing URL is not a posting-specific official job URL"
        )
    return f"https://{host}{path}"
