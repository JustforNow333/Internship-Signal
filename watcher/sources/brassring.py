"""IBM/Kenexa BrassRing TGNewUI direct source adapter."""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, build_opener

from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceError,
    SourceSchemaError,
    TextHttpResponse,
)
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.parsing import page_fingerprint, parse_records
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import get_text_response, post_json_response

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_SNAPSHOT_PASSES = 3
_SAFE_ID = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,127})?")
_POSITIVE_INTEGER = re.compile(r"[1-9][0-9]{0,19}")
_HOME_PATH = "/tgnewui/search/home/home"
_LISTING_PATH = "/TgNewUI/Search/Ajax/ProcessSortAndShowMoreJobs"
_DETAIL_PATH = "/tgnewui/search/home/homewithpreload"
_POSTING_URL_QUERY_KEYS = (
    {"partnerid", "siteid", "PageType", "jobid"},
    {"partnerid", "siteid", "PageType", "jobid", "frmSiteId"},
)
_QUESTION_FIELDS = frozenset(
    {
        "reqid",
        "jobtitle",
        "formtext23",
        "jobdescription",
        "formtext21",
        "department",
    }
)


@dataclass(frozen=True)
class _AnonymousSession:
    opener: Callable[..., Any]
    cookies: Iterable[object]


@dataclass(frozen=True)
class _Bootstrap:
    home_url: str
    request_token: str
    encrypted_session_value: str


@dataclass(frozen=True)
class _Snapshot:
    rows: tuple[dict, ...]
    identities: frozenset[str]
    total: int
    malformed_rows: int
    schema_error_rows: int
    reason_codes: tuple[str, ...]


class _HiddenInputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, list[str]] = {}

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "input":
            return
        fields = {str(key).casefold(): value for key, value in attrs}
        name = str(fields.get("id") or fields.get("name") or "").casefold()
        if name in {"partnerid", "siteid", "cookievalue", "rftoken"}:
            self.values.setdefault(name, []).append(str(fields.get("value") or ""))


class BrassRingSource(DirectDiagnosticsMixin):
    """Fully enumerate an anonymous BrassRing TGNewUI public board."""

    name = "brassring"

    def __init__(
        self,
        *,
        session_factory: Callable[[], _AnonymousSession] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_snapshot_passes: int = DEFAULT_MAX_SNAPSHOT_PASSES,
    ) -> None:
        if not 1 <= max_pages <= DEFAULT_MAX_PAGES:
            raise ValueError(f"max_pages must be between 1 and {DEFAULT_MAX_PAGES}")
        if not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES:
            raise ValueError(
                "max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        self._session_factory = session_factory or _new_anonymous_session
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.max_pages = max_pages
        self.max_snapshot_passes = max_snapshot_passes
        self.bootstrap_requests = 0
        self.pages_requested = 0
        self.snapshot_passes_requested = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self.bootstrap_requests = 0
        self.pages_requested = 0
        self.snapshot_passes_requested = 0
        self.last_response_metadata = {}
        host, partner_id, site_id = _required_config(company)
        session = self._session_factory()
        bootstrap = self._bootstrap(
            session,
            host=host,
            partner_id=partner_id,
            site_id=site_id,
        )

        previous: _Snapshot | None = None
        expected_total: int | None = None
        for _pass_number in range(1, self.max_snapshot_passes + 1):
            self.snapshot_passes_requested += 1
            snapshot = self._fetch_snapshot(
                company,
                session=session,
                bootstrap=bootstrap,
                host=host,
                partner_id=partner_id,
                site_id=site_id,
            )
            if expected_total is None:
                expected_total = snapshot.total
            elif snapshot.total != expected_total:
                raise SourceSchemaError(
                    "brassring total changed between complete snapshots"
                )
            if previous is not None and snapshot.identities == previous.identities:
                self._record_parse_diagnostics(
                    snapshot.malformed_rows,
                    snapshot.schema_error_rows,
                    snapshot.reason_codes,
                )
                return self._finish(list(snapshot.rows))
            previous = snapshot

        raise SourceSchemaError(
            "brassring snapshot did not stabilize within the bounded pass limit"
        )

    def _bootstrap(
        self,
        session: _AnonymousSession,
        *,
        host: str,
        partner_id: str,
        site_id: str,
    ) -> _Bootstrap:
        home_url = _home_url(host, partner_id, site_id)
        self.bootstrap_requests += 1

        def attempt() -> TextHttpResponse:
            return get_text_response(home_url, self.name, opener=session.opener)

        response = self._retrier.run(attempt)
        if not tuple(session.cookies):
            raise SourceSchemaError(
                "brassring bootstrap did not establish an anonymous session cookie"
            )
        return _parse_bootstrap(
            response.text,
            home_url=home_url,
            partner_id=partner_id,
            site_id=site_id,
        )

    def _fetch_snapshot(
        self,
        company: CompanyCfg,
        *,
        session: _AnonymousSession,
        bootstrap: _Bootstrap,
        host: str,
        partner_id: str,
        site_id: str,
    ) -> _Snapshot:
        expected_total: int | None = None
        raw_seen = 0
        seen_pages: set[str] = set()
        rows: list[dict] = []
        rows_by_id: dict[str, dict] = {}
        ids_by_url: dict[str, str] = {}
        malformed_rows = 0
        schema_error_rows = 0
        reason_codes: list[str] = []

        def record_diagnostics(
            malformed: int,
            schema_errors: int,
            reasons: Iterable[str],
        ) -> None:
            nonlocal malformed_rows, schema_error_rows
            malformed_rows += max(0, int(malformed))
            schema_error_rows += max(0, int(schema_errors))
            for reason in reasons:
                if reason not in reason_codes:
                    reason_codes.append(reason)

        for page_number in range(1, self.max_pages + 1):
            self.pages_requested += 1
            payload = self._fetch_page(
                session,
                bootstrap=bootstrap,
                host=host,
                partner_id=partner_id,
                site_id=site_id,
                page_number=page_number,
            )
            records, total = _listing_page(payload)
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise SourceSchemaError(
                    "brassring total changed during pagination"
                )

            if total == 0:
                if records or page_number != 1:
                    raise SourceSchemaError(
                        "brassring zero-result response was inconsistent"
                    )
                return _Snapshot((), frozenset(), 0, 0, 0, ())
            if not records:
                raise SourceSchemaError(
                    "brassring pagination ended before the reported total"
                )
            fingerprint = page_fingerprint(records)
            if fingerprint in seen_pages:
                raise SourceSchemaError(
                    "brassring returned a repeated pagination page"
                )
            seen_pages.add(fingerprint)
            raw_seen += len(records)
            if raw_seen > total:
                raise SourceSchemaError(
                    "brassring returned more records than the reported total"
                )
            if raw_seen < total and len(records) != DEFAULT_PAGE_SIZE:
                raise SourceSchemaError("brassring pagination ended prematurely")

            parsed = parse_records(
                records,
                lambda record: _parse_posting(
                    record,
                    company,
                    host=host,
                    partner_id=partner_id,
                    site_id=site_id,
                ),
                source_name=self.name,
                company_name=company.name,
                diagnostics=record_diagnostics,
            )
            for row in parsed:
                source_id = str(row["extra"]["source_requisition_id"])
                source_url = row["source_url"]
                if source_id in rows_by_id:
                    raise SourceSchemaError(
                        "brassring returned a duplicate requisition ID"
                    )
                other_id = ids_by_url.get(source_url)
                if other_id is not None and other_id != source_id:
                    raise SourceSchemaError(
                        "brassring returned one posting URL for conflicting IDs"
                    )
                rows_by_id[source_id] = row
                ids_by_url[source_url] = source_id
                rows.append(row)

            if raw_seen == total:
                return _Snapshot(
                    tuple(rows),
                    frozenset(rows_by_id),
                    total,
                    malformed_rows,
                    schema_error_rows,
                    tuple(reason_codes),
                )
            if page_number == self.max_pages:
                raise SourceSchemaError(
                    "brassring reached the maximum page safeguard before completion"
                )

        raise AssertionError("unreachable BrassRing pagination state")

    def _fetch_page(
        self,
        session: _AnonymousSession,
        *,
        bootstrap: _Bootstrap,
        host: str,
        partner_id: str,
        site_id: str,
        page_number: int,
    ) -> Any:
        endpoint = f"https://{host}{_LISTING_PATH}"
        payload = _search_payload(
            partner_id,
            site_id,
            page_number=page_number,
            encrypted_session_value=bootstrap.encrypted_session_value,
        )
        headers = {
            "Referer": bootstrap.home_url,
            "RFT": bootstrap.request_token,
        }

        def attempt() -> JsonHttpResponse:
            return post_json_response(
                endpoint,
                payload,
                self.name,
                request_headers=headers,
                opener=session.opener,
            )

        response = self._retrier.run(attempt)
        self.last_response_metadata = dict(response.metadata)
        return response.payload

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


def _required_config(company: CompanyCfg) -> tuple[str, str, str]:
    host = str(company.brassring_host or "").strip().casefold()
    partner_id = str(company.brassring_partner_id or "").strip()
    site_id = str(company.brassring_site_id or "").strip()
    if not host:
        raise SourceError(f"brassring requires brassring_host for {company.name}")
    if not _POSITIVE_INTEGER.fullmatch(partner_id):
        raise SourceError(
            f"brassring requires a valid brassring_partner_id for {company.name}"
        )
    if not _POSITIVE_INTEGER.fullmatch(site_id):
        raise SourceError(
            f"brassring requires a valid brassring_site_id for {company.name}"
        )
    return host, partner_id, site_id


def _home_url(host: str, partner_id: str, site_id: str) -> str:
    return urlunsplit(
        (
            "https",
            host,
            "/TGnewUI/Search/Home/Home",
            urlencode((("partnerid", partner_id), ("siteid", site_id))),
            "",
        )
    )


def _parse_bootstrap(
    html: Any,
    *,
    home_url: str,
    partner_id: str,
    site_id: str,
) -> _Bootstrap:
    if not isinstance(html, str) or not html.strip():
        raise SourceSchemaError("brassring bootstrap response was empty")
    parser = _HiddenInputParser()
    parser.feed(html)

    def required(name: str, *, maximum: int = 4096) -> str:
        values = parser.values.get(name, [])
        if len(values) != 1:
            raise SourceSchemaError(
                f"brassring bootstrap expected exactly one {name} value"
            )
        value = values[0].strip()
        if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
            raise SourceSchemaError(
                f"brassring bootstrap contained an invalid {name} value"
            )
        return value

    if required("partnerid", maximum=20) != partner_id:
        raise SourceSchemaError(
            "brassring bootstrap partner ID did not match configuration"
        )
    if required("siteid", maximum=20) != site_id:
        raise SourceSchemaError(
            "brassring bootstrap site ID did not match configuration"
        )
    return _Bootstrap(
        home_url=home_url,
        request_token=required("rftoken"),
        encrypted_session_value=required("cookievalue"),
    )


def _search_payload(
    partner_id: str,
    site_id: str,
    *,
    page_number: int,
    encrypted_session_value: str,
) -> dict[str, object]:
    return {
        "partnerId": int(partner_id),
        "siteId": int(site_id),
        "keyword": "",
        "location": "",
        "keywordCustomSolrFields": "FORMTEXT21,AutoReq,Department,JobTitle",
        "locationCustomSolrFields": "FORMTEXT2,FORMTEXT23,Location",
        "facetfilterfields": {"Facet": []},
        "linkId": "",
        "Latitude": 0,
        "Longitude": 0,
        "powersearchoptions": {"PowerSearchOption": []},
        "SortType": "JobTitle",
        "pageNumber": page_number,
        "encryptedSessionValue": encrypted_session_value,
    }


def _listing_page(payload: Any) -> tuple[list, int]:
    if not isinstance(payload, dict):
        raise SourceSchemaError("brassring expected a JSON object")
    total = payload.get("JobsCount")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise SourceSchemaError(
            "brassring expected JobsCount to be a nonnegative integer"
        )
    # ``TotalJobsCount`` is not a second total: the live board reports 0 for it
    # on every page, so ``JobsCount`` is the only trustworthy total metadata.
    jobs = payload.get("Jobs")
    if not isinstance(jobs, dict) or not isinstance(jobs.get("Job"), list):
        raise SourceSchemaError("brassring expected Jobs.Job to be a list")
    records = jobs["Job"]
    if len(records) > DEFAULT_PAGE_SIZE:
        raise SourceSchemaError(
            "brassring returned more than the platform page-size limit"
        )
    return records, total


def _parse_posting(
    posting: Any,
    company: CompanyCfg,
    *,
    host: str,
    partner_id: str,
    site_id: str,
) -> dict:
    if not isinstance(posting, dict):
        raise SourceSchemaError("brassring expected each posting to be an object")
    questions = posting.get("Questions")
    if not isinstance(questions, list):
        raise SourceSchemaError("brassring posting Questions must be a list")
    values: dict[str, str] = {}
    for question in questions:
        if not isinstance(question, dict):
            raise SourceSchemaError("brassring Questions entries must be objects")
        raw_name = question.get("QuestionName")
        if not isinstance(raw_name, str):
            raise SourceSchemaError("brassring question name must be a string")
        name = raw_name.strip().casefold()
        if name not in _QUESTION_FIELDS:
            continue
        raw_value = question.get("Value")
        if raw_value is None:
            value = ""
        elif isinstance(raw_value, str):
            value = raw_value.strip()
        else:
            raise SourceSchemaError(
                f"brassring {name} value must be a string or null"
            )
        if name in values and values[name] != value:
            raise SourceSchemaError(
                f"brassring posting contained conflicting {name} values"
            )
        values[name] = value

    native_id = values.get("reqid", "")
    title = values.get("jobtitle", "")
    if not _SAFE_ID.fullmatch(native_id) or not title:
        raise SourceSchemaError(
            "brassring posting missing a valid requisition ID or title"
        )
    source_url, posting_site_id = _posting_url(
        posting.get("Link"),
        host=host,
        partner_id=partner_id,
        site_id=site_id,
        native_id=native_id,
    )
    source_id = f"brassring:{host}:{partner_id}:{site_id}:{native_id}"
    return make_row(
        source="direct",
        source_adapter="brassring",
        company=company.name,
        title=title,
        location=values.get("formtext23", ""),
        description=html_to_text(values.get("jobdescription", "")),
        source_url=source_url,
        extra={
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "brassring",
            "source_scope": f"{host}:{partner_id}:{site_id}",
            "brassring_native_id": native_id,
            "brassring_partner_id": partner_id,
            "brassring_site_id": site_id,
            "brassring_posting_site_id": posting_site_id,
            "business_area": values.get("formtext21", "")[:500],
            "department": values.get("department", "")[:500],
            "active": True,
        },
    )


def _posting_url(
    value: Any,
    *,
    host: str,
    partner_id: str,
    site_id: str,
    native_id: str,
) -> tuple[str, str]:
    """Return the canonical posting URL and the site the posting lives on.

    The configured board also lists localized siblings of the same partner.
    Those postings publish their own ``siteid`` plus a ``frmSiteId`` naming the
    board they were reached from, so both shapes are accepted while every other
    query shape, host, partner, or requisition is rejected.
    """

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise SourceSchemaError("brassring posting URL is invalid") from exc
    query = parse_qs(parsed.query, keep_blank_values=True)
    referring_site = query.get("frmSiteId")
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.path.casefold() != _DETAIL_PATH
        or parsed.fragment
        or set(query) not in _POSTING_URL_QUERY_KEYS
        or query.get("partnerid") != [partner_id]
        or query.get("PageType") != ["JobDetails"]
        or query.get("jobid") != [native_id]
    ):
        raise SourceSchemaError(
            "brassring URL is not a posting-specific official job URL"
        )
    posting_site_id = query["siteid"][0]
    if not _POSITIVE_INTEGER.fullmatch(posting_site_id):
        raise SourceSchemaError("brassring posting site ID is not a positive integer")
    if referring_site is None:
        if posting_site_id != site_id:
            raise SourceSchemaError(
                "brassring posting is not reachable from the configured board"
            )
    elif referring_site != [site_id]:
        raise SourceSchemaError(
            "brassring posting is not reachable from the configured board"
        )
    params = [
        ("partnerid", partner_id),
        ("siteid", posting_site_id),
        ("PageType", "JobDetails"),
        ("jobid", native_id),
    ]
    if referring_site is not None:
        params.append(("frmSiteId", site_id))
    canonical = urlunsplit(
        (
            "https",
            host,
            "/TGnewUI/Search/home/HomeWithPreLoad",
            urlencode(tuple(params)),
            "",
        )
    )
    return canonical, posting_site_id
