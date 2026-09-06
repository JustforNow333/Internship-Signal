"""Google Careers' authoritative listing source.

Google publishes its job inventory through the careers application's own
``batchexecute`` RPC. This module models Google's verified ``r06xKb`` contract
rather than ``batchexecute`` as a transport: the request shape, the envelope
handling, the payload layout, and the record fields below are all specific to
this one RPC, and nothing here generalizes to other Google or Alphabet
properties.

The RPC answers with an anti-XSSI prefix followed by a batchexecute envelope
whose ``wrb.fr`` row for this rpcid carries a JSON string decoding to
``[jobs, null, total, page_size]``. The total and page size are advertised on
every page, so a bounded pass is provably complete.

Two Google-specific policies live here. First, the RPC exposes an official
organization filter, so this source requests Google's own inventory rather than
enumerating every Alphabet organization and discarding the rest; the retained
records are still checked to carry Google's organization name, and a record from
another organization fails the collection closed. Second, a small number of
listings are published without an application link - future openings and general
career-opportunity entries. They are real inventory and count toward raw
completeness, but they are not open roles, so they are retained with
``active=False`` rather than given an invented application URL. That reuses the
watcher's existing open-job rule instead of adding a Google-only exception to it.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlencode

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceSchemaError, TextHttpResponse
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.parsing import page_fingerprint
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import post_form_text_response


HOST = "www.google.com"
RPC_PATH = "/about/careers/applications/_/HiringCportalFrontendUi/data/batchexecute"
RPC_ID = "r06xKb"
RESULTS_PATH = "/about/careers/applications/jobs/results"
# The organization this source is configured to publish. The RPC filters on it
# and every retained record is verified against it.
ORGANIZATION = "Google"
REQUEST_LOCALE = "en-US"
DEFAULT_MAX_PAGES = 1000
DEFAULT_MAX_SNAPSHOT_PASSES = 3
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
# Defensive ceiling well above the several thousand roles Google publishes.
MAX_TOTAL_RESULTS = 100_000
_MAX_FIELD_LENGTH = 20_000

# Google prefixes the RPC response with an anti-XSSI guard.
_XSSI_PREFIX = ")]}'"
_POSTING_ID = re.compile(r"[0-9]+")

# Record layout inside one job entry of the RPC payload.
_IDX_ID = 0
_IDX_TITLE = 1
_IDX_APPLY_URL = 2
_IDX_RESPONSIBILITIES = 3
_IDX_QUALIFICATIONS = 4
_IDX_ORGANIZATION = 7
_IDX_LOCATIONS = 9
_IDX_ABOUT = 10
_MIN_RECORD_LENGTH = 11


class _GoogleSnapshotUnstable(SourceSchemaError):
    """One pass observed the board changing under it and must be discarded."""


@dataclass(frozen=True)
class GoogleDiagnostics:
    listing_pages_requested: int = 0
    snapshot_passes_requested: int = 0
    advertised_total: int = 0
    raw_records_seen: int = 0
    retained_rows: int = 0
    non_actionable_rows: int = 0
    request_attempts: int = 0
    retry_attempts: int = 0


@dataclass(frozen=True)
class _Posting:
    posting_id: str
    title: str
    organization: str
    locations: tuple[str, ...]
    description: str
    requirements: str
    apply_url: str

    @property
    def actionable(self) -> bool:
        """Whether this listing can actually be applied to today."""

        return bool(self.apply_url)


@dataclass(frozen=True)
class _Page:
    postings: tuple[_Posting, ...]
    total: int
    page_size: int

    @property
    def membership_fingerprint(self) -> str:
        return page_fingerprint([{"id": p.posting_id} for p in self.postings])


@dataclass(frozen=True)
class _Snapshot:
    postings: tuple[_Posting, ...]
    total: int
    page_size: int

    @property
    def identity(self) -> tuple:
        """Total plus id-keyed records, so ordering differences do not matter."""

        return (
            self.total,
            self.page_size,
            tuple(sorted((p.posting_id, p.title, p.apply_url) for p in self.postings)),
        )


class GoogleSource(DirectDiagnosticsMixin):
    """Enumerate one internally consistent, complete Google listing snapshot."""

    name = "google"

    def __init__(
        self,
        *,
        request_form: Callable[[str, str, dict], Any] | None = None,
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
        self._request_form = request_form
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self.max_pages = max_pages
        self.max_snapshot_passes = max_snapshot_passes
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()
        self._reset_counters()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @property
    def last_diagnostics(self) -> GoogleDiagnostics:
        return GoogleDiagnostics(
            listing_pages_requested=self._listing_pages_requested,
            snapshot_passes_requested=self._snapshot_passes_requested,
            advertised_total=self._advertised_total,
            raw_records_seen=self._raw_records_seen,
            retained_rows=self._retained_rows,
            non_actionable_rows=self._non_actionable_rows,
            request_attempts=self.request_attempts,
            retry_attempts=self.retry_attempts,
        )

    @staticmethod
    def endpoint() -> str:
        return f"https://{HOST}{RPC_PATH}?{urlencode({'rpcids': RPC_ID})}"

    @staticmethod
    def posting_url(posting_id: str) -> str:
        return f"https://{HOST}{RESULTS_PATH}/{posting_id}"

    @staticmethod
    def request_fields(page: int) -> dict[str, str]:
        """Return the form body for one page of the verified RPC.

        The inner request is the shape Google's own careers page issues, with a
        one-based page number and the organization filter this source scopes to.
        """

        inner = json.dumps(
            [[None, [ORGANIZATION], None, None, REQUEST_LOCALE, None, None, page]],
            separators=(",", ":"),
        )
        envelope = json.dumps(
            [[[RPC_ID, inner, None, "generic"]]], separators=(",", ":")
        )
        return {"f.req": envelope}

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self._reset_counters()
        previous: _Snapshot | None = None
        stable: _Snapshot | None = None

        for _pass_number in range(1, self.max_snapshot_passes + 1):
            self._snapshot_passes_requested += 1
            try:
                snapshot = self._fetch_snapshot()
            except _GoogleSnapshotUnstable:
                previous = None
                continue
            if previous is not None and snapshot.identity == previous.identity:
                stable = snapshot
                break
            previous = snapshot

        if stable is None:
            raise SourceSchemaError(
                "google snapshot did not stabilize within the bounded pass limit"
            )

        self._advertised_total = stable.total
        self._raw_records_seen = len(stable.postings)
        rows = [self._row(posting, company) for posting in stable.postings]
        self._retained_rows = len(rows)
        self._non_actionable_rows = sum(
            1 for posting in stable.postings if not posting.actionable
        )
        recovered = bool(self.retry_attempts)
        reasons = ("request_retry_recovered",) if recovered else ()
        self._finish_direct_diagnostics(
            rows,
            failed_request_count=self.retry_attempts,
            degraded=True if recovered else None,
            complete=True if recovered else None,
            reason_codes=reasons,
        )
        return rows

    def _fetch_snapshot(self) -> _Snapshot:
        """Return one whole-board pass, or discard it if the board moved."""

        expected_total: int | None = None
        expected_page_size: int | None = None
        postings: list[_Posting] = []
        seen_pages: set[str] = set()
        seen_ids: dict[str, str] = {}
        page_number = 1

        while page_number <= self.max_pages:
            self._listing_pages_requested += 1
            page = _rpc_page(self._fetch_text(page_number))

            if expected_total is None:
                expected_total, expected_page_size = page.total, page.page_size
                if expected_total > MAX_TOTAL_RESULTS:
                    raise SourceSchemaError(
                        "google advertised total exceeds the pagination safeguard"
                    )
                if expected_total == 0:
                    if page.postings:
                        raise SourceSchemaError(
                            "google reported an empty board while returning records"
                        )
                    return _Snapshot((), 0, page.page_size)
            elif page.total != expected_total or page.page_size != expected_page_size:
                raise _GoogleSnapshotUnstable(
                    "google advertised total or page size changed during pagination"
                )

            last_page = math.ceil(expected_total / expected_page_size)
            expected_count = min(expected_page_size, expected_total - len(postings))
            if len(page.postings) != expected_count:
                if page_number < last_page:
                    raise _GoogleSnapshotUnstable(
                        "google returned a short page before the final page"
                    )
                raise SourceSchemaError(
                    "google final page did not match its advertised-total arithmetic"
                )

            fingerprint = page.membership_fingerprint
            if fingerprint in seen_pages:
                raise _GoogleSnapshotUnstable("google repeated a listing page")
            seen_pages.add(fingerprint)

            for posting in page.postings:
                if posting.organization != ORGANIZATION:
                    raise SourceSchemaError(
                        "google returned a record outside the requested organization"
                    )
                if posting.posting_id in seen_ids:
                    raise SourceSchemaError("google returned a duplicate posting id")
                seen_ids[posting.posting_id] = posting.title
                postings.append(posting)

            if len(postings) == expected_total:
                if page_number != last_page:
                    raise SourceSchemaError(
                        "google completed its advertised total on an unexpected page"
                    )
                self._verify_terminal_page(
                    last_page + 1, expected_total, expected_page_size
                )
                return _Snapshot(tuple(postings), expected_total, expected_page_size)
            if len(postings) > expected_total:
                raise SourceSchemaError(
                    "google returned more records than its advertised total"
                )
            page_number += 1

        raise SourceSchemaError(
            "google reached the maximum page safeguard before completion"
        )

    def _verify_terminal_page(self, page: int, total: int, page_size: int) -> None:
        """A page past the end must be empty while still advertising the total.

        That is the board's truncation signal. Confirming it keeps an empty
        response from ever being mistaken for a healthy empty board.
        """

        self._listing_pages_requested += 1
        terminal = _rpc_page(self._fetch_text(page))
        if terminal.total != total or terminal.page_size != page_size:
            raise _GoogleSnapshotUnstable(
                "google advertised total changed at the terminal page"
            )
        if terminal.postings:
            raise SourceSchemaError(
                "google returned records past its advertised final page"
            )

    def _row(self, posting: _Posting, company: CompanyCfg) -> dict:
        source_id = f"google:{posting.posting_id}"
        extra: dict[str, Any] = {
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "google",
            "google_posting_id": posting.posting_id,
            "google_organization": posting.organization,
            # Listings without an application link are real inventory but are not
            # open roles, so the watcher's existing open-job rule excludes them.
            "active": posting.actionable,
        }
        if posting.apply_url:
            extra["application_url"] = posting.apply_url
        else:
            extra["google_non_actionable_reason"] = "no_application_link"
        return make_row(
            source="direct",
            source_adapter="google",
            company=company.name,
            title=posting.title,
            location="; ".join(posting.locations),
            description=posting.description,
            requirements=posting.requirements,
            source_url=self.posting_url(posting.posting_id),
            extra=extra,
        )

    def _fetch_text(self, page: int) -> Any:
        url = self.endpoint()
        fields = self.request_fields(page)

        def attempt() -> Any:
            if self._request_form is not None:
                response = self._request_form(url, self.name, fields)
            else:
                response = post_form_text_response(
                    url,
                    fields,
                    self.name,
                    max_response_bytes=MAX_RESPONSE_BYTES,
                )
            if isinstance(response, TextHttpResponse):
                self.last_response_metadata = dict(response.metadata)
                return response.text
            self.last_response_metadata = {}
            return response

        return self._retrier.run(attempt)

    def _reset_counters(self) -> None:
        self._listing_pages_requested = 0
        self._snapshot_passes_requested = 0
        self._advertised_total = 0
        self._raw_records_seen = 0
        self._retained_rows = 0
        self._non_actionable_rows = 0
        self.last_response_metadata = {}


# --- provider-local RPC decoding ------------------------------------------


def _rpc_page(payload: Any) -> _Page:
    """Return one page of the RPC, failing closed on any contract change."""

    if not isinstance(payload, str) or not payload.strip():
        raise SourceSchemaError("google rpc response was empty")
    body = payload.lstrip()
    if body.startswith(_XSSI_PREFIX):
        _, separator, body = body.partition("\n")
        if not separator:
            raise SourceSchemaError("google rpc response carried only its xssi guard")
    try:
        envelope = json.loads(body)
    except ValueError as exc:
        raise SourceSchemaError(
            "google rpc response was not a decodable batchexecute envelope"
        ) from exc
    if not isinstance(envelope, list):
        raise SourceSchemaError("google rpc envelope was not a list of messages")

    inner = _rpc_payload(envelope)
    if not isinstance(inner, list) or len(inner) < 4:
        raise SourceSchemaError("google rpc payload did not carry the expected shape")

    total = _count(inner[2], "total")
    page_size = _count(inner[3], "page size")
    if page_size == 0:
        raise SourceSchemaError("google rpc reported a zero page size")

    records = inner[0]
    if records is None:
        records = []
    if not isinstance(records, list):
        raise SourceSchemaError("google rpc records were not a list")
    return _Page(tuple(_posting(record) for record in records), total, page_size)


def _rpc_payload(envelope: list) -> Any:
    """Return the decoded payload of this rpcid's successful response.

    A batchexecute envelope carries unrelated bookkeeping messages, and reports
    a failed call as an ``er`` message rather than an HTTP error. Only a
    ``wrb.fr`` message for this rpcid with a non-null payload is a result.
    """

    for message in envelope:
        if not isinstance(message, list) or len(message) < 2:
            continue
        if message[0] == "er" and message[1] == RPC_ID:
            raise SourceSchemaError("google rpc reported an error for this request")

    matches = [
        message
        for message in envelope
        if isinstance(message, list)
        and len(message) > 2
        and message[0] == "wrb.fr"
        and message[1] == RPC_ID
    ]
    if len(matches) != 1:
        raise SourceSchemaError(
            "google rpc envelope did not contain exactly one response for this request"
        )
    raw = matches[0][2]
    if not isinstance(raw, str) or not raw.strip():
        raise SourceSchemaError("google rpc returned an empty payload for this request")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SourceSchemaError("google rpc payload was not decodable JSON") from exc


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceSchemaError(f"google rpc {field} was not an integer")
    if value < 0:
        raise SourceSchemaError(f"google rpc {field} was negative")
    return value


def _posting(record: Any) -> _Posting:
    if not isinstance(record, list) or len(record) < _MIN_RECORD_LENGTH:
        raise SourceSchemaError("google rpc record did not carry the expected shape")

    posting_id = record[_IDX_ID]
    if not isinstance(posting_id, str) or not _POSTING_ID.fullmatch(posting_id):
        raise SourceSchemaError("google rpc record lacked a valid posting id")
    title = _clean(record[_IDX_TITLE], "title")
    organization = _clean(record[_IDX_ORGANIZATION], "organization")
    if not title or not organization:
        raise SourceSchemaError("google rpc record lacked a title or organization")

    return _Posting(
        posting_id=posting_id,
        title=title,
        organization=organization,
        locations=_locations(record[_IDX_LOCATIONS]),
        description=_rich_text(record[_IDX_ABOUT], record[_IDX_RESPONSIBILITIES]),
        requirements=_rich_text(record[_IDX_QUALIFICATIONS]),
        apply_url=_apply_url(record[_IDX_APPLY_URL]),
    )


def _apply_url(value: Any) -> str:
    """Return the posting's application link, or blank when it has none.

    Future openings and general career-opportunity entries are published without
    one. That absence is meaningful, so it is preserved rather than replaced.
    """

    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError("google rpc application url was not a string or null")
    value = value.strip()
    if value and not value.startswith("https://"):
        raise SourceSchemaError("google rpc application url was not an https url")
    return value


def _locations(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise SourceSchemaError("google rpc locations were not a list")
    out: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            label = entry
        elif isinstance(entry, list) and entry and isinstance(entry[0], str):
            label = entry[0]
        else:
            raise SourceSchemaError("google rpc location entry was malformed")
        label = " ".join(label.split())
        if label and label not in out:
            out.append(label)
    return tuple(out)


def _rich_text(*values: Any) -> str:
    """Return bounded plain text from the RPC's ``[null, html]`` text fields."""

    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            html = value
        elif isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
            html = value[1]
        elif isinstance(value, list):
            continue
        else:
            raise SourceSchemaError("google rpc text field was malformed")
        text = html_to_text(html)
        if text:
            parts.append(text)
    return " ".join(" ".join(parts).split())[:_MAX_FIELD_LENGTH]


def _clean(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError(f"google rpc {field} was not a string")
    return " ".join(value.split())[:_MAX_FIELD_LENGTH]
