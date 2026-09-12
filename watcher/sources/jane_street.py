"""Complete first-party Jane Street careers inventory.

Jane Street's official open-roles page is rendered entirely from one anonymous
first-party document, ``/jobs/main.json``: a flat array carrying every posting
with a stable ten-digit integer id. A second first-party document,
``/static/position-directories.json``, independently lists the ids that have a
rendered posting page, and the two agree exactly, which is what establishes
``main.json`` as the authoritative inventory rather than a filtered view.

``/jobs/internships.json`` is not an inventory. It carries no ids and a
different schema, and every entry is a closed internship programme; the page
uses it purely to mark matching rows as no longer accepting applications. It is
used here the same way -- as a first-party closed-state overlay keyed on
position, city, and duration -- and never as a source of postings.

There is no pagination, so the inventory arrives atomically and the adapter
reuses the shared single-payload lifecycle. Everything else stays Jane
Street-specific: the closed overlay, the identity rules, and the fail-closed
completeness checks.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceError, SourceSchemaError
from watcher.sources.direct import SinglePayloadDirectAdapter
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import fetch_json


HOST = "www.janestreet.com"
SOURCE_URL = f"https://{HOST}/join-jane-street/open-roles/"
INVENTORY_URL = f"https://{HOST}/jobs/main.json"
CLOSED_INTERNSHIPS_URL = f"https://{HOST}/jobs/internships.json"
POSITION_URL_PREFIX = f"https://{HOST}/join-jane-street/position/"

DEFAULT_MAX_SNAPSHOT_PASSES = 3
DEFAULT_SNAPSHOT_DELAY_SECONDS = 0.55
# The published inventory is a few hundred postings; this only bounds a
# runaway response, it is never an expected truncation point.
MAX_RECORDS = 20_000

_POSTING_ID = re.compile(r"[1-9][0-9]{0,19}")

_REQUIRED_TEXT_FIELDS = ("position", "category", "availability", "city", "team", "duration")


class _SnapshotUnstable(SourceSchemaError):
    """A complete Jane Street inventory could not be captured in one pass."""


@dataclass(frozen=True)
class _Snapshot:
    rows: tuple[dict, ...]
    identities: frozenset[str]
    total: int


class JaneStreetSource(SinglePayloadDirectAdapter):
    """Enumerate Jane Street's complete anonymous first-party inventory."""

    name = "jane_street"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_snapshot_passes: int = DEFAULT_MAX_SNAPSHOT_PASSES,
        snapshot_delay_seconds: float = DEFAULT_SNAPSHOT_DELAY_SECONDS,
    ) -> None:
        if (
            type(max_snapshot_passes) is not int
            or not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES
        ):
            raise ValueError(
                "jane_street max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        if (
            isinstance(snapshot_delay_seconds, bool)
            or not isinstance(snapshot_delay_seconds, (int, float))
            or not 0 <= snapshot_delay_seconds <= 5
        ):
            raise ValueError(
                "jane_street snapshot_delay_seconds must be between 0 and 5"
            )
        self._request_json = request_json
        self._sleeper = sleeper
        self.max_snapshot_passes = max_snapshot_passes
        self.snapshot_delay_seconds = float(snapshot_delay_seconds)
        self.requests_made = 0
        self.snapshot_passes_requested = 0
        self._closed_programmes: frozenset[tuple[str, str, str]] = frozenset()
        self._begin_direct_diagnostics()

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self.requests_made = 0
        self.snapshot_passes_requested = 0
        _require_config(company)

        previous: _Snapshot | None = None
        last_instability: _SnapshotUnstable | None = None
        for pass_number in range(1, self.max_snapshot_passes + 1):
            if pass_number > 1 and self.snapshot_delay_seconds:
                self._sleeper(self.snapshot_delay_seconds)
            self.snapshot_passes_requested += 1
            try:
                snapshot = self._snapshot(company)
            except _SnapshotUnstable as exc:
                last_instability = exc
                previous = None
                continue
            if (
                previous is not None
                and snapshot.total == previous.total
                and snapshot.identities == previous.identities
            ):
                return list(snapshot.rows)
            previous = snapshot

        detail = f": {last_instability}" if last_instability is not None else ""
        raise SourceSchemaError(
            "jane_street snapshot did not stabilize within the bounded pass limit"
            f"{detail}"
        )

    def _snapshot(self, company: CompanyCfg) -> _Snapshot:
        # The closed overlay is loaded first so every parsed row in this pass
        # is marked against one consistent view of closed programmes.
        self._closed_programmes = _closed_programmes(
            self._fetch(CLOSED_INTERNSHIPS_URL)
        )
        payload = self._fetch(INVENTORY_URL)
        rows = self.parse(payload, company)

        # The shared lifecycle records skipped records rather than raising.
        # Completeness cannot be claimed over a partial parse, so any loss
        # fails the source closed.
        diagnostics = self.last_health_diagnostics
        if diagnostics.malformed_row_count or diagnostics.schema_error_row_count:
            raise SourceSchemaError(
                "jane_street inventory contained malformed or schema-invalid "
                "postings"
            )
        if len(rows) != len(payload):
            raise SourceSchemaError(
                "jane_street retained a different number of rows than the "
                "inventory published"
            )

        identities = {str(row["extra"]["source_id"]) for row in rows}
        urls = {str(row["source_url"]) for row in rows}
        if len(identities) != len(rows):
            raise SourceSchemaError("jane_street returned a duplicate posting id")
        if len(urls) != len(rows):
            raise SourceSchemaError("jane_street returned a duplicate posting URL")

        return _Snapshot(tuple(rows), frozenset(identities), len(rows))

    def _fetch(self, url: str) -> Any:
        request = self._request_json or fetch_json
        self.requests_made += 1
        return request(url, self.name)

    def _records_from_payload(self, payload: Any, company: CompanyCfg) -> list:
        if not isinstance(payload, list):
            raise SourceSchemaError("jane_street inventory must be a JSON array")
        if len(payload) > MAX_RECORDS:
            raise SourceSchemaError(
                "jane_street inventory exceeded the maximum record safeguard"
            )
        return payload

    def _parse_record(self, record: Any, company: CompanyCfg) -> dict:
        if not isinstance(record, Mapping):
            raise SourceSchemaError("jane_street expected each posting to be an object")

        raw_id = record.get("id")
        if type(raw_id) is not int or not _POSTING_ID.fullmatch(str(raw_id)):
            raise SourceSchemaError("jane_street posting id was invalid")
        identity = str(raw_id)

        values = {
            field: _required_text(record.get(field), label=field)
            for field in _REQUIRED_TEXT_FIELDS
        }
        overview = _required_text(record.get("overview"), label="overview")

        closed = (
            values["position"],
            values["city"],
            values["duration"],
        ) in self._closed_programmes

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=values["position"],
            location=values["city"],
            description=html_to_text(overview),
            compensation=_compensation(record),
            # The site's own canonical form keeps the trailing slash; without
            # it the posting route answers with a 301 to the slashed URL.
            source_url=f"{POSITION_URL_PREFIX}{identity}/",
            internship_type=values["availability"],
            extra={
                "source_id": identity,
                "source_requisition_id": f"jane_street:{identity}",
                "source_system": "jane_street_careers_json",
                "jane_street_posting_id": identity,
                "category": values["category"],
                "team": values["team"],
                "duration": values["duration"],
                "availability": values["availability"],
                # The inventory publishes no posting date, so none is claimed.
                "active": not closed,
                "closed_programme": closed,
            },
        )


def _require_config(company: CompanyCfg) -> None:
    if str(company.source_url or "").strip() != SOURCE_URL:
        raise SourceError(
            "jane_street requires the official open-roles listing for "
            f"{company.name}"
        )


def _closed_programmes(payload: Any) -> frozenset[tuple[str, str, str]]:
    """Return the closed (position, location, duration) triples the site marks.

    This document is a programme catalogue, not an inventory: it carries no
    posting ids, so it can only ever narrow the state of a row that the
    inventory already published.
    """

    if not isinstance(payload, list):
        raise SourceSchemaError(
            "jane_street closed-internship overlay must be a JSON array"
        )
    if len(payload) > MAX_RECORDS:
        raise SourceSchemaError(
            "jane_street closed-internship overlay exceeded the maximum record "
            "safeguard"
        )
    closed: set[tuple[str, str, str]] = set()
    for entry in payload:
        if not isinstance(entry, Mapping):
            raise SourceSchemaError(
                "jane_street closed-internship entry must be an object"
            )
        status = _required_text(entry.get("status"), label="closed status")
        position = _required_text(entry.get("position"), label="closed position")
        location = _required_text(entry.get("location"), label="closed location")
        duration = _required_text(entry.get("duration"), label="closed duration")
        if status != "closed":
            raise SourceSchemaError(
                "jane_street closed-internship overlay carried a non-closed entry"
            )
        closed.add((position, location, duration))
    return frozenset(closed)


def _compensation(record: Mapping[str, object]) -> str:
    """Return the published salary range verbatim, inventing no currency."""

    minimum = record.get("min_salary")
    maximum = record.get("max_salary")
    if minimum is None and maximum is None:
        return ""
    if not isinstance(minimum, str) or not isinstance(maximum, str):
        raise SourceSchemaError("jane_street salary bounds were invalid")
    low, high = minimum.strip(), maximum.strip()
    if not low or not high:
        raise SourceSchemaError("jane_street published a one-sided salary range")
    return f"{low} - {high}"


def _required_text(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceSchemaError(f"jane_street {label} was invalid")
    return value.strip()
