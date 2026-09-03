"""Shopify first-party careers inventory source adapter.

Shopify publishes its authoritative job inventory from its own site rather than
from a public ATS board. Application data behind the posting is Ashby-backed and
Ashby field names surface inside Shopify's payload, but no public Ashby board or
token exists for it, so Shopify's own route data is the only enumerable source of
record. This adapter is deliberately not an Ashby variant.

The listing route answers with a React Router single-fetch payload: one flattened
JSON array in which every object is ``{"_<key index>": <value index>}`` and every
string is deduplicated and referenced by index. Negative indices are the format's
null sentinels. That decoding is specific to this provider and stays here.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceSchemaError, TextHttpResponse
from watcher.sources.direct import DirectRecordAdapter
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS, RequestRetrier, RetryPolicy
from watcher.sources.rows import iso_date, make_row
from watcher.sources.transport import get_text_response

HOST = "www.shopify.com"
LISTING_PATH = "/careers.data"
# The inventory is a few hundred KB; this bounds a structurally changed or
# runaway response well below the transport default.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# Depth guard for the flattened payload: real records nest only a few levels,
# and index references could otherwise describe a cycle.
MAX_HYDRATION_DEPTH = 32
MAX_POSTINGS = 5000
_MAX_FIELD_LENGTH = 500

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_ROUTE_KEY = "($locale)/careers"
_DATA_KEY = "data"
_INVENTORY_KEY = "jobPostingsWithJobs"


class ShopifySource(DirectRecordAdapter):
    """Enumerate Shopify's complete first-party careers inventory."""

    name = "shopify"

    def __init__(
        self,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        request_text: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._retrier = RequestRetrier(
            policy=RetryPolicy(max_attempts=max_attempts),
            sleeper=sleeper,
            jitter=jitter,
        )
        self._request_text = request_text
        self.request_count = 0
        self.last_response_metadata: dict[str, object] = {}
        self._begin_direct_diagnostics()

    @property
    def request_attempts(self) -> int:
        return self._retrier.request_attempts

    @property
    def retry_attempts(self) -> int:
        return self._retrier.retry_attempts

    @staticmethod
    def endpoint() -> str:
        return f"https://{HOST}{LISTING_PATH}"

    @staticmethod
    def posting_url(title: str, posting_id: str) -> str:
        """Return the canonical Shopify posting route.

        The payload carries only the legacy ``?ashby_jid=`` link, which redirects
        to ``/careers/<title slug>_<posting uuid>``. Building that route directly
        keeps the stored URL posting-specific and free of the legacy query.
        """

        slug = _SLUG_STRIP.sub("-", str(title or "").casefold()).strip("-")
        return f"https://{HOST}/careers/{slug}_{posting_id}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self._retrier.reset()
        self.request_count = 0
        self.last_response_metadata = {}

        records = self._listing_records()
        parsed = self._parse_direct_records(
            list(records),
            company,
            lambda record: _parse_posting(record, company),
        )

        rows: list[dict] = []
        by_id: dict[str, str] = {}
        by_url: dict[str, str] = {}
        for row in parsed:
            posting_id = str(row["extra"]["shopify_posting_id"])
            source_url = row["source_url"]
            if posting_id in by_id:
                raise SourceSchemaError("shopify returned a duplicate posting id")
            other = by_url.get(source_url)
            if other is not None and other != posting_id:
                raise SourceSchemaError(
                    "shopify returned one canonical route for conflicting posting ids"
                )
            by_id[posting_id] = source_url
            by_url[source_url] = posting_id
            rows.append(row)

        return self._finish(rows)

    def _listing_records(self) -> list[Any]:
        url = self.endpoint()
        self.request_count += 1

        def attempt() -> TextHttpResponse:
            if self._request_text is not None:
                return self._request_text(url, self.name)
            return get_text_response(
                url, self.name, max_response_bytes=MAX_RESPONSE_BYTES
            )

        response = self._retrier.run(attempt)
        self.last_response_metadata = dict(getattr(response, "metadata", {}) or {})
        return _inventory_records(getattr(response, "text", response))

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


# --- React Router single-fetch decoding (provider-local) -------------------


def _flat_payload(text: Any) -> list:
    """Return the flattened value array from one single-fetch response."""

    if not isinstance(text, str) or not text.strip():
        raise SourceSchemaError("shopify listing response was empty")
    first_line = text.split("\n", 1)[0].strip()
    try:
        flat = json.loads(first_line)
    except ValueError as exc:
        raise SourceSchemaError(
            "shopify listing response was not a decodable route payload"
        ) from exc
    if not isinstance(flat, list) or not flat:
        raise SourceSchemaError(
            "shopify listing payload was not the expected flattened array"
        )
    return flat


def _hydrate(flat: list, index: Any, depth: int = 0) -> Any:
    """Resolve one index in the flattened array into a plain Python value.

    A negative index is the format's null sentinel. Depth is bounded because
    index references could otherwise describe a cycle.
    """

    if not isinstance(index, int) or isinstance(index, bool):
        return index
    if index < 0:
        return None
    if index >= len(flat):
        raise SourceSchemaError("shopify payload referenced an out-of-range index")
    if depth > MAX_HYDRATION_DEPTH:
        raise SourceSchemaError("shopify payload nested beyond the supported depth")

    value = flat[index]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if isinstance(raw_key, str) and raw_key.startswith("_"):
                key_index = raw_key[1:]
                if not key_index.lstrip("-").isdigit():
                    raise SourceSchemaError("shopify payload had a malformed key index")
                key = _hydrate(flat, int(key_index), depth + 1)
            else:
                key = raw_key
            if not isinstance(key, str):
                raise SourceSchemaError("shopify payload key was not a string")
            out[key] = _hydrate(flat, raw_value, depth + 1)
        return out
    if isinstance(value, list):
        return [_hydrate(flat, item, depth + 1) for item in value]
    return value


def _inventory_records(text: Any) -> list[Any]:
    """Return the posting records, failing closed on any structural change.

    A payload that does not carry the careers route's inventory key is treated
    as a changed or unusable contract, never as an empty board. Only an explicit
    empty ``jobPostingsWithJobs`` list means the board is genuinely empty.
    """

    flat = _flat_payload(text)
    try:
        inventory_key_index = flat.index(_INVENTORY_KEY)
    except ValueError as exc:
        raise SourceSchemaError(
            "shopify payload did not contain the expected job inventory key"
        ) from exc

    owner_key = f"_{inventory_key_index}"
    owners = [
        value
        for value in flat
        if isinstance(value, dict) and owner_key in value
    ]
    if len(owners) != 1:
        raise SourceSchemaError(
            "shopify payload did not contain exactly one job inventory object"
        )
    if not _has_careers_route(flat):
        raise SourceSchemaError(
            "shopify payload did not contain the expected careers route"
        )

    records = _hydrate(flat, owners[0][owner_key])
    if not isinstance(records, list):
        raise SourceSchemaError("shopify job inventory was not a list")
    if len(records) > MAX_POSTINGS:
        raise SourceSchemaError("shopify job inventory exceeded the supported size")
    return records


def _has_careers_route(flat: list) -> bool:
    """Return whether the payload still routes the careers page to route data."""

    try:
        route_index = flat.index(_ROUTE_KEY)
        data_index = flat.index(_DATA_KEY)
    except ValueError:
        return False
    route_key = f"_{route_index}"
    for value in flat:
        if not isinstance(value, dict) or route_key not in value:
            continue
        target = value[route_key]
        if not isinstance(target, int) or not 0 <= target < len(flat):
            continue
        holder = flat[target]
        if isinstance(holder, dict) and f"_{data_index}" in holder:
            return True
    return False


# --- canonical row construction -------------------------------------------


def _parse_posting(record: Any, company: CompanyCfg) -> dict:
    if not isinstance(record, Mapping):
        raise SourceSchemaError("shopify expected each inventory entry to be an object")
    posting = record.get("jobPosting")
    if not isinstance(posting, Mapping):
        raise SourceSchemaError("shopify entry was missing its jobPosting object")

    posting_id = str(posting.get("id") or "").strip().casefold()
    title = _text(posting.get("title"), "title")
    if not _UUID.fullmatch(posting_id) or not title:
        raise SourceSchemaError("shopify posting missing a valid id or title")
    if posting.get("isListed") is not True:
        raise SourceSchemaError("shopify posting is not listed on the public board")
    if str(posting.get("status") or "").strip() != "Published":
        raise SourceSchemaError("shopify posting is not published")

    job = record.get("job") if isinstance(record.get("job"), Mapping) else {}
    source_id = f"shopify:{posting_id}"
    extra = {
        "source_id": source_id,
        "source_requisition_id": source_id,
        "source_system": "shopify",
        "shopify_posting_id": posting_id,
        "active": True,
    }
    for key, field in (
        ("shopify_job_id", "jobId"),
        ("department", "departmentName"),
        ("team", "teamName"),
        ("workplace_type", "workplaceType"),
        ("employment_type", "employmentType"),
    ):
        value = _text(posting.get(field), field)
        if value:
            extra[key] = value
    requisition = _text(job.get("customRequisitionId"), "customRequisitionId")
    if requisition:
        extra["shopify_requisition_number"] = requisition

    return make_row(
        source="direct",
        source_adapter="shopify",
        company=company.name,
        title=title,
        location=_text(posting.get("locationName"), "locationName"),
        date_posted=_posted_date(posting.get("publishedDate")),
        source_url=ShopifySource.posting_url(title, posting_id),
        extra=extra,
    )


def _text(value: Any, field: str, limit: int = _MAX_FIELD_LENGTH) -> str:
    """Return bounded text, treating the payload's null sentinel as absent."""

    if value is None:
        return ""
    if isinstance(value, bool) or isinstance(value, (int, float)):
        # Hydration already resolved real sentinels to None; a surviving number
        # is a contract change rather than a value this adapter should coerce.
        raise SourceSchemaError(f"shopify {field} must be a string or null")
    if not isinstance(value, str):
        raise SourceSchemaError(f"shopify {field} must be a string or null")
    return " ".join(value.split())[:limit]


def _posted_date(value: Any) -> str:
    """Return the normalized publication date, never inventing one."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError("shopify publishedDate must be a string or null")
    return iso_date(value.strip())
