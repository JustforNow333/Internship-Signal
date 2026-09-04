"""KPMG US's authoritative careers-board direct source.

KPMG publishes its US jobs through a first-party WordPress careers site whose
search results are served by one theme-local endpoint. That endpoint answers
with JSON carrying an exact result count and an HTML fragment of job cards, so
this module models KPMG's verified contract rather than WordPress or the Google
Cloud Talent Solution layer visible underneath it. Neither is a platform this
adapter generalizes: the contract, the card markup, and the identity rules below
are all specific to this board.

The source is listing-only. Every card carries the concrete locations of its
posting, not just a count, so the listing already holds everything the pipeline
gates on: a stable requisition id, a title, real ``City, ST`` locations, and a
posting-specific URL. Opening each posting would add one request per job for a
description the watcher treats as quality-enhancing rather than required.

Identity needs one KPMG-specific rule. A card is identified by its Google CTS
``data-id`` and every card is distinct, but KPMG splits a requisition whose
location list is long across two cards that share one ``jobId``, one title, one
practice area, and one canonical URL while carrying disjoint halves of the
location list. Those cards are two presentations of one requisition, so they are
reconciled into a single row whose locations are the union of both. Raw card
completeness is proven against the board's exact total *before* that
reconciliation, and any repeated ``jobId`` whose cards disagree on an invariant
requisition field fails the collection closed rather than being collapsed.
"""

from __future__ import annotations

import json
import math
import random
import re
import time
from dataclasses import dataclass
from html import unescape
from typing import Any, Callable
from urllib.parse import urlencode

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceSchemaError, TextHttpResponse
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.parsing import page_fingerprint
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import make_row
from watcher.sources.transport import get_text_response


HOST = "www.kpmguscareers.com"
SEARCH_PATH = (
    "/wp-content/themes/understrap-child-main/page-templates/google/get-jobs.php"
)
DETAIL_PATH = "/jobdetail/"
PAGE_SIZE = 12
DEFAULT_MAX_PAGES = 400
DEFAULT_MAX_SNAPSHOT_PASSES = 3
MAX_LISTING_BYTES = 8 * 1024 * 1024
# A defensive ceiling on a board that currently publishes several hundred jobs.
MAX_TOTAL_RESULTS = PAGE_SIZE * DEFAULT_MAX_PAGES

_CARD = re.compile(
    r'<div class="search--item[^"]*">(?P<body>.*?)'
    r'(?=<div class="search--item|\Z)',
    re.S,
)
_CARD_LINK = re.compile(
    r'<a\s+href="(?P<url>/jobdetail/\?jobId=(?P<job_id>\d+))"\s+'
    r'data-id="(?P<card_id>\d+)"',
)
_LIST_VIEW_TITLE = re.compile(
    r'<div class="list-view.*?<div class="h5[^"]*">(?P<title>[^<]*)</div>', re.S
)
_LIST_VIEW_META = re.compile(
    r'<div class="list-view.*?<div class="h5[^"]*">[^<]*</div>\s*'
    r'<div class="text-xs[^"]*">(?P<meta>[^<]*)</div>',
    re.S,
)
# The board reports its exact count both as a structured integer and inside the
# rendered "N Results" label. Both are required and must agree.
_SHOWING_COUNT = re.compile(
    r'<span[^>]*data-action="count"[^>]*>\s*([\d,]+)\s*</span>\s*Results',
    re.I,
)
_PAGE_LINK = re.compile(r'data-href="(\d+)"')
_NUMERIC = re.compile(r"^\d+$")


class _KpmgSnapshotUnstable(SourceSchemaError):
    """One pass observed the board changing under it and must be discarded."""


@dataclass(frozen=True)
class KpmgDiagnostics:
    listing_pages_requested: int = 0
    snapshot_passes_requested: int = 0
    raw_cards_seen: int = 0
    authoritative_total: int = 0
    unique_card_ids: int = 0
    retained_requisitions: int = 0
    reconciled_requisitions: int = 0
    request_attempts: int = 0
    retry_attempts: int = 0


@dataclass(frozen=True)
class _Card:
    """One rendered search result, identified by its Google CTS card id."""

    card_id: str
    job_id: str
    url: str
    title: str
    practice_area: str
    locations: tuple[str, ...]


@dataclass(frozen=True)
class _Page:
    cards: tuple[_Card, ...]
    total_results: int

    @property
    def membership_fingerprint(self) -> str:
        return page_fingerprint(
            [{"card_id": card.card_id, "job_id": card.job_id} for card in self.cards]
        )


@dataclass(frozen=True)
class _Snapshot:
    cards: tuple[_Card, ...]
    page_membership: tuple[str, ...]
    total_results: int


class KpmgSource(DirectDiagnosticsMixin):
    """Enumerate one internally consistent, complete KPMG listing snapshot."""

    name = "kpmg"

    def __init__(
        self,
        *,
        request_text: Callable[[str, str], Any] | None = None,
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
        self._request_text = request_text
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
    def last_diagnostics(self) -> KpmgDiagnostics:
        return KpmgDiagnostics(
            listing_pages_requested=self._listing_pages_requested,
            snapshot_passes_requested=self._snapshot_passes_requested,
            raw_cards_seen=self._raw_cards_seen,
            authoritative_total=self._authoritative_total,
            unique_card_ids=self._unique_card_ids,
            retained_requisitions=self._retained_requisitions,
            reconciled_requisitions=self._reconciled_requisitions,
            request_attempts=self.request_attempts,
            retry_attempts=self.retry_attempts,
        )

    @staticmethod
    def endpoint(spage: int = 1) -> str:
        query = urlencode(
            {"ajax": "1", "page_type": "search", "spage": str(spage)}
        )
        return f"https://{HOST}{SEARCH_PATH}?{query}"

    @staticmethod
    def posting_url(job_id: str) -> str:
        return f"https://{HOST}{DETAIL_PATH}?jobId={job_id}"

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
            except _KpmgSnapshotUnstable:
                previous = None
                continue
            if previous is not None and snapshot == previous:
                stable = snapshot
                break
            previous = snapshot

        if stable is None:
            raise SourceSchemaError(
                "kpmg snapshot did not stabilize within the bounded pass limit"
            )

        self._authoritative_total = stable.total_results
        self._raw_cards_seen = len(stable.cards)
        self._unique_card_ids = len({card.card_id for card in stable.cards})
        requisitions = _reconcile(stable.cards)
        self._retained_requisitions = len(requisitions)
        self._reconciled_requisitions = len(stable.cards) - len(requisitions)

        rows = [self._row(req, company) for req in requisitions]
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
        cards: list[_Card] = []
        page_membership: list[str] = []
        seen_pages: set[str] = set()
        seen_card_ids: dict[str, str] = {}
        spage = 1

        while spage <= self.max_pages:
            self._listing_pages_requested += 1
            page = _search_page(self._fetch_text(self.endpoint(spage)))

            if expected_total is None:
                expected_total = page.total_results
                if expected_total > MAX_TOTAL_RESULTS:
                    raise SourceSchemaError(
                        "kpmg exact total exceeds the pagination safeguard"
                    )
                if expected_total == 0:
                    if page.cards:
                        raise SourceSchemaError(
                            "kpmg reported an empty board while returning cards"
                        )
                    return _Snapshot((), (), 0)
            elif page.total_results != expected_total:
                raise _KpmgSnapshotUnstable(
                    "kpmg exact total changed during pagination"
                )

            last_page = math.ceil(expected_total / PAGE_SIZE)
            expected_count = min(PAGE_SIZE, expected_total - len(cards))
            if len(page.cards) != expected_count:
                # A short page before the arithmetic says so means the board
                # moved under this pass, or the contract changed.
                if spage < last_page:
                    raise _KpmgSnapshotUnstable(
                        "kpmg returned a short page before the final page"
                    )
                raise SourceSchemaError(
                    "kpmg final page did not match its exact-total arithmetic"
                )

            fingerprint = page.membership_fingerprint
            if fingerprint in seen_pages:
                raise _KpmgSnapshotUnstable("kpmg repeated a listing page")
            seen_pages.add(fingerprint)
            page_membership.append(fingerprint)

            for card in page.cards:
                if card.card_id in seen_card_ids:
                    raise SourceSchemaError(
                        "kpmg returned a duplicate card id"
                    )
                seen_card_ids[card.card_id] = card.job_id
                cards.append(card)

            if len(cards) == expected_total:
                if spage != last_page:
                    raise SourceSchemaError(
                        "kpmg completed its exact total on an unexpected page"
                    )
                return _Snapshot(
                    tuple(cards), tuple(page_membership), expected_total
                )
            if len(cards) > expected_total:
                raise SourceSchemaError(
                    "kpmg returned more cards than its exact total"
                )
            spage += 1

        raise SourceSchemaError(
            "kpmg reached the maximum page safeguard before completion"
        )

    def _row(self, req: _Card, company: CompanyCfg) -> dict:
        """Build one canonical row from a reconciled requisition."""

        source_id = f"kpmg:{req.job_id}"
        extra = {
            "source_id": source_id,
            "source_requisition_id": source_id,
            "source_system": "kpmg",
            "kpmg_job_id": req.job_id,
            "kpmg_card_ids": list(req.card_id.split(",")),
            "active": True,
        }
        if req.practice_area:
            extra["practice_area"] = req.practice_area
        return make_row(
            source="direct",
            source_adapter="kpmg",
            company=company.name,
            title=req.title,
            location="; ".join(req.locations),
            source_url=req.url,
            extra=extra,
        )

    def _fetch_text(self, url: str) -> Any:
        def attempt() -> Any:
            if self._request_text is not None:
                response = self._request_text(url, self.name)
            else:
                response = get_text_response(
                    url, self.name, max_response_bytes=MAX_LISTING_BYTES
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
        self._raw_cards_seen = 0
        self._authoritative_total = 0
        self._unique_card_ids = 0
        self._retained_requisitions = 0
        self._reconciled_requisitions = 0
        self.last_response_metadata = {}


# --- provider-local parsing -----------------------------------------------


def _search_page(payload: Any) -> _Page:
    """Return one page's exact total and parsed cards, failing closed."""

    if not isinstance(payload, str) or not payload.strip():
        raise SourceSchemaError("kpmg listing response was empty")
    try:
        document = json.loads(payload)
    except ValueError as exc:
        raise SourceSchemaError(
            "kpmg listing response was not decodable JSON"
        ) from exc
    if not isinstance(document, dict):
        raise SourceSchemaError("kpmg listing payload was not an object")

    postings = document.get("postings")
    if not isinstance(postings, dict):
        raise SourceSchemaError("kpmg listing payload lacked its postings object")

    total = _exact_total(postings.get("size"), document.get("showing"))
    jobs_html = postings.get("jobs")
    if not isinstance(jobs_html, str):
        raise SourceSchemaError("kpmg listing payload lacked its job card fragment")

    cards = tuple(_card(match.group("body")) for match in _CARD.finditer(jobs_html))
    return _Page(cards, total)


def _exact_total(size: Any, showing: Any) -> int:
    """Return the board's exact total from both required representations.

    The endpoint reports its count as a structured integer and again inside the
    rendered label. Both are required, and a disagreement is a changed contract
    rather than a total this adapter may choose between.
    """

    if isinstance(size, bool) or not isinstance(size, int):
        if not (isinstance(size, str) and _NUMERIC.fullmatch(size.strip())):
            raise SourceSchemaError("kpmg listing payload lacked a valid exact total")
        size = int(size.strip())
    if size < 0:
        raise SourceSchemaError("kpmg exact total was negative")

    if not isinstance(showing, str):
        raise SourceSchemaError("kpmg listing payload lacked its result label")
    match = _SHOWING_COUNT.search(showing)
    if not match:
        raise SourceSchemaError("kpmg result label did not carry an exact total")
    if int(match.group(1).replace(",", "")) != size:
        raise SourceSchemaError("kpmg exact totals disagreed with each other")
    return size


def _card(body: str) -> _Card:
    link = _CARD_LINK.search(body)
    if not link:
        raise SourceSchemaError("kpmg job card lacked its posting link and card id")
    title_match = _LIST_VIEW_TITLE.search(body)
    meta_match = _LIST_VIEW_META.search(body)
    if not title_match or not meta_match:
        raise SourceSchemaError("kpmg job card lacked its title or location line")

    title = _clean(title_match.group("title"))
    practice_area, locations = _meta(meta_match.group("meta"))
    job_id = link.group("job_id")
    if not title or not locations:
        raise SourceSchemaError("kpmg job card lacked a usable title or location")
    return _Card(
        card_id=link.group("card_id"),
        job_id=job_id,
        url=KpmgSource.posting_url(job_id),
        title=title,
        practice_area=practice_area,
        locations=locations,
    )


def _meta(raw: str) -> tuple[str, tuple[str, ...]]:
    """Split a card's ``Practice Area | City, ST; City, ST`` line.

    The grid view renders only a ``N Locations`` summary; the list view carries
    the concrete locations, and those are the only location evidence this
    adapter accepts. A summary standing in for the list is a contract change.
    """

    text = _clean(raw)
    area, separator, locations = text.partition("|")
    if not separator:
        area, locations = "", text
    parsed = tuple(
        part for part in (piece.strip() for piece in locations.split(";")) if part
    )
    if len(parsed) == 1 and re.fullmatch(r"\d+\s+Locations?", parsed[0], re.I):
        raise SourceSchemaError(
            "kpmg job card carried a location count instead of its locations"
        )
    return area.strip(), parsed


def _clean(value: str) -> str:
    return " ".join(unescape(value).split())


def _reconcile(cards: tuple[_Card, ...]) -> list[_Card]:
    """Collapse KPMG's split-location card pairs into single requisitions.

    Cards are grouped by ``jobId``. A group of one is returned unchanged. A
    larger group is accepted only when every card agrees on the invariant
    requisition fields - title, canonical URL, and practice area - in which case
    the requisition's locations are the deterministic union of every card's
    locations. Any disagreement fails closed rather than discarding a card.
    """

    grouped: dict[str, list[_Card]] = {}
    for card in cards:
        grouped.setdefault(card.job_id, []).append(card)

    reconciled: list[_Card] = []
    for job_id, group in grouped.items():
        if len(group) == 1:
            reconciled.append(group[0])
            continue
        first = group[0]
        for other in group[1:]:
            if (
                other.title != first.title
                or other.url != first.url
                or other.practice_area != first.practice_area
            ):
                raise SourceSchemaError(
                    "kpmg repeated a requisition id with conflicting posting fields"
                )
        locations = sorted({loc for card in group for loc in card.locations})
        reconciled.append(
            _Card(
                card_id=",".join(card.card_id for card in group),
                job_id=job_id,
                url=first.url,
                title=first.title,
                practice_area=first.practice_area,
                locations=tuple(locations),
            )
        )
    return reconciled
