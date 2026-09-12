"""Complete Wolverine Trading inventory from its first-party Pinpoint board.

Wolverine's corporate open-positions page embeds the Pinpoint tenant ``wolve``.
That tenant's custom first-party host publishes one unfiltered
``/postings.json`` payload. The rendered-board metadata explicitly disables
pagination, has no page size, exposes only the ``en`` locale, and carries no
active exclusion values. The public sitemap independently enumerates the same
posting UUIDs as the JSON canonical URLs.

Each collection pass validates those three representations together. Two
consecutive complete passes must match before rows are published, so a posting
opening or closing between requests cannot produce a partial inventory. The
implementation stays Wolverine-specific: another configured Pinpoint tenant is
currently empty and therefore cannot prove that its posting-record contract is
the same reusable provider invariant.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

from internship_signal.domain.identity import norm_url
from watcher.config import CompanyCfg
from watcher.sources.contracts import (
    JsonHttpResponse,
    SourceError,
    SourceSchemaError,
    TextHttpResponse,
)
from watcher.sources.direct import SinglePayloadDirectAdapter
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import get_json_response, get_text_response


HOST = "careers.wolve.com"
BOARD_URL = f"https://{HOST}"
SOURCE_URL = "https://www.wolve.com/open-positions"
CAREERS_METADATA_URL = f"{BOARD_URL}/"
INVENTORY_URL = f"{BOARD_URL}/postings.json"
SITEMAP_URL = f"{BOARD_URL}/sitemap.xml"

MAX_CAREERS_BYTES = 2 * 1024 * 1024
MAX_INVENTORY_BYTES = 8 * 1024 * 1024
MAX_SITEMAP_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 5_000
DEFAULT_MAX_SNAPSHOT_PASSES = 3
DEFAULT_SNAPSHOT_DELAY_SECONDS = 0.55

_POSTING_ID = re.compile(r"[1-9][0-9]{0,19}")
_POSTING_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_POSTING_PATH = re.compile(
    rf"^/en/postings/(?P<identity>{_POSTING_UUID.pattern})$"
)
_SITEMAP_POSTING_PATH = re.compile(
    rf"^/postings/(?P<identity>{_POSTING_UUID.pattern})$"
)
_RECORD_FIELDS = frozenset(
    {
        "id",
        "benefits",
        "benefits_header",
        "compensation",
        "compensation_currency",
        "compensation_frequency",
        "compensation_maximum",
        "compensation_minimum",
        "compensation_visible",
        "deadline_at",
        "description",
        "employment_type",
        "employment_type_text",
        "job",
        "key_responsibilities",
        "key_responsibilities_header",
        "location",
        "path",
        "reporting_to",
        "skills_knowledge_expertise",
        "skills_knowledge_expertise_header",
        "title",
        "url",
        "workplace_type",
        "workplace_type_text",
    }
)
_JOB_FIELDS = frozenset(
    {
        "id",
        "requisition_id",
        "department",
        "division",
        "structure_custom_group_one",
    }
)
_LOCATION_FIELDS = frozenset(
    {"id", "city", "name", "postal_code", "province", "street_address"}
)
_EXPECTED_EXCLUSION_FIELDS = frozenset(
    {
        "exclude_department_id",
        "exclude_division_id",
        "exclude_location_id",
        "exclude_structure_custom_group_one_id",
    }
)


class _SnapshotUnstable(SourceSchemaError):
    """The independently published first-party representations disagreed."""


@dataclass(frozen=True)
class _Snapshot:
    rows: tuple[dict, ...]
    identities: frozenset[str]
    canonical_urls: frozenset[str]
    sitemap_identities: frozenset[str]
    duplicate_count: int


class WolverineSource(SinglePayloadDirectAdapter):
    """Enumerate Wolverine's bounded, stable, complete public job inventory."""

    name = "wolverine"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        request_text: Callable[[str, str], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_snapshot_passes: int = DEFAULT_MAX_SNAPSHOT_PASSES,
        snapshot_delay_seconds: float = DEFAULT_SNAPSHOT_DELAY_SECONDS,
    ) -> None:
        if (
            type(max_snapshot_passes) is not int
            or not 2 <= max_snapshot_passes <= DEFAULT_MAX_SNAPSHOT_PASSES
        ):
            raise ValueError(
                "wolverine max_snapshot_passes must be between 2 and "
                f"{DEFAULT_MAX_SNAPSHOT_PASSES}"
            )
        if (
            isinstance(snapshot_delay_seconds, bool)
            or not isinstance(snapshot_delay_seconds, (int, float))
            or not 0 <= snapshot_delay_seconds <= 5
        ):
            raise ValueError(
                "wolverine snapshot_delay_seconds must be between 0 and 5"
            )
        self._request_json = request_json
        self._request_text = request_text
        self._sleeper = sleeper
        self.max_snapshot_passes = max_snapshot_passes
        self.snapshot_delay_seconds = float(snapshot_delay_seconds)
        self.requests_made = 0
        self.snapshot_passes_requested = 0
        self.last_response_metadata: dict[str, Mapping[str, object]] = {}
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint() -> str:
        return INVENTORY_URL

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.requests_made = 0
        self.snapshot_passes_requested = 0
        self.last_response_metadata = {}
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
                previous = None
                last_instability = exc
                continue
            if snapshot == previous:
                rows = list(snapshot.rows)
                self._finish_direct_diagnostics(
                    rows,
                    duplicate_row_count=snapshot.duplicate_count,
                )
                return rows
            previous = snapshot

        detail = f": {last_instability}" if last_instability is not None else ""
        raise SourceSchemaError(
            "wolverine snapshot did not stabilize within the bounded pass limit"
            f"{detail}"
        )

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        """Parse one inventory payload with the source's strict row semantics."""

        self._begin_direct_diagnostics()
        rows, duplicate_count = self._parse_inventory(payload, company)
        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=duplicate_count,
        )
        return rows

    def _snapshot(self, company: CompanyCfg) -> _Snapshot:
        self._begin_direct_diagnostics()
        _validate_listing_contract(self._fetch_text(CAREERS_METADATA_URL))
        rows, duplicate_count = self._parse_inventory(
            self._fetch_json(INVENTORY_URL),
            company,
        )
        sitemap_identities = _sitemap_identities(self._fetch_text(SITEMAP_URL))
        identities = frozenset(
            str(row["extra"]["source_id"]) for row in rows
        )
        canonical_urls = frozenset(norm_url(str(row["source_url"])) for row in rows)
        posting_uuids = frozenset(
            str(row["extra"]["pinpoint_posting_uuid"]) for row in rows
        )
        if posting_uuids != sitemap_identities:
            raise _SnapshotUnstable(
                "wolverine postings JSON and sitemap identities disagreed"
            )
        return _Snapshot(
            rows=tuple(rows),
            identities=identities,
            canonical_urls=canonical_urls,
            sitemap_identities=sitemap_identities,
            duplicate_count=duplicate_count,
        )

    def _parse_inventory(
        self,
        payload: Any,
        company: CompanyCfg,
    ) -> tuple[list[dict], int]:
        records = self._records_from_payload(payload, company)
        rows = self._parse_direct_records(
            records,
            company,
            lambda record: self._parse_record(record, company),
        )
        if self._diagnostic_malformed_rows or self._diagnostic_schema_rows:
            raise SourceSchemaError(
                "wolverine inventory contained malformed or schema-invalid postings"
            )
        rows, duplicate_count = _deduplicate_rows(rows)
        rows.sort(key=lambda row: int(row["extra"]["pinpoint_posting_id"]))
        return rows, duplicate_count

    def _records_from_payload(self, payload: Any, company: CompanyCfg) -> list:
        if not isinstance(payload, dict) or set(payload) != {"data"}:
            raise SourceSchemaError("wolverine inventory envelope changed")
        records = payload.get("data")
        if not isinstance(records, list):
            raise SourceSchemaError("wolverine inventory data must be a list")
        if len(records) > MAX_RECORDS:
            raise SourceSchemaError(
                "wolverine inventory exceeded the maximum record safeguard"
            )
        return records

    def _parse_record(self, record: Any, company: CompanyCfg) -> dict:
        if not isinstance(record, Mapping) or set(record) != _RECORD_FIELDS:
            raise SourceSchemaError("wolverine posting schema changed")

        posting_id = _numeric_text(record.get("id"), "posting id")
        title = _required_text(record.get("title"), "title")
        posting_uuid, source_url = _posting_identity(
            record.get("path"),
            record.get("url"),
        )
        location = _location(record.get("location"))
        job = _job(record.get("job"))

        description = _joined_html(
            record.get("description"),
            record.get("key_responsibilities"),
        )
        requirements = html_to_text(
            _required_text(
                record.get("skills_knowledge_expertise"),
                "skills_knowledge_expertise",
            )
        )
        _required_text(record.get("benefits"), "benefits")
        for header in (
            "benefits_header",
            "key_responsibilities_header",
            "skills_knowledge_expertise_header",
        ):
            _required_text(record.get(header), header)
        _optional_text(record.get("reporting_to"), "reporting_to")
        compensation = _compensation(record)

        source_id = f"wolverine:{posting_id}"
        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=location["name"],
            description=description,
            requirements=requirements,
            compensation=compensation,
            source_url=source_url,
            deadline=_deadline(record.get("deadline_at")),
            remote_status=_required_text(
                record.get("workplace_type_text"),
                "workplace_type_text",
            ),
            internship_type=_required_text(
                record.get("employment_type_text"),
                "employment_type_text",
            ),
            extra={
                "source_id": source_id,
                "source_requisition_id": source_id,
                "source_system": "wolverine_pinpoint_postings_json",
                "pinpoint_posting_id": posting_id,
                "pinpoint_posting_uuid": posting_uuid,
                "pinpoint_job_id": job["id"],
                "pinpoint_requisition_id": job["requisition_id"],
                "department": job["department"],
                "division": job["division"],
                "sub_department": job["sub_department"],
                "employment_type": _required_text(
                    record.get("employment_type"),
                    "employment_type",
                ),
                "workplace_type": _required_text(
                    record.get("workplace_type"),
                    "workplace_type",
                ),
                "location": location,
                # ``postings.json`` is the board's Current Opportunities set;
                # closed routes are removed from both it and the sitemap.
                "active": True,
            },
        )

    def _fetch_json(self, url: str) -> Any:
        self.requests_made += 1
        response = (
            self._request_json(url, self.name)
            if self._request_json is not None
            else get_json_response(
                url,
                self.name,
                max_response_bytes=MAX_INVENTORY_BYTES,
            )
        )
        if isinstance(response, JsonHttpResponse):
            self.last_response_metadata[url] = dict(response.metadata)
            return response.payload
        return response

    def _fetch_text(self, url: str) -> str:
        self.requests_made += 1
        response = (
            self._request_text(url, self.name)
            if self._request_text is not None
            else get_text_response(
                url,
                self.name,
                max_response_bytes=(
                    MAX_CAREERS_BYTES
                    if url == CAREERS_METADATA_URL
                    else MAX_SITEMAP_BYTES
                ),
            )
        )
        if isinstance(response, TextHttpResponse):
            self.last_response_metadata[url] = dict(response.metadata)
            return response.text
        if not isinstance(response, str):
            raise SourceSchemaError("wolverine expected a text response")
        return response


def _require_config(company: CompanyCfg) -> None:
    if str(company.source_url or "").strip() != SOURCE_URL:
        raise SourceError(
            "wolverine requires the official open-positions listing for "
            f"{company.name}"
        )


class _ExternalJobsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.components: list[str] = []
        self._parts: list[str] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag.casefold() != "script":
            return
        if values.get("data-component-name") != "External::Jobs":
            return
        if self._parts is not None:
            self.components.append("")
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "script" and self._parts is not None:
            self.components.append("".join(self._parts))
            self._parts = None

    def close(self) -> None:
        super().close()
        if self._parts is not None:
            self.components.append("")
            self._parts = None


def _validate_listing_contract(html: Any) -> None:
    if not isinstance(html, str):
        raise SourceSchemaError("wolverine careers metadata must be HTML text")
    parser = _ExternalJobsParser()
    parser.feed(html)
    parser.close()
    if len(parser.components) != 1 or not parser.components[0].strip():
        raise SourceSchemaError(
            "wolverine careers page must publish one jobs component"
        )
    try:
        props = json.loads(parser.components[0])
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SourceSchemaError(
            "wolverine careers jobs metadata was malformed"
        ) from exc
    if not isinstance(props, Mapping):
        raise SourceSchemaError("wolverine careers jobs metadata changed")
    if props.get("url") != "/postings.json":
        raise SourceSchemaError("wolverine careers inventory endpoint changed")
    if props.get("showPagination") is not False:
        raise SourceSchemaError("wolverine careers pagination was enabled")
    if props.get("pageSize") is not None:
        raise SourceSchemaError("wolverine careers page size became bounded")
    if props.get("target") != "external:jobs:index:":
        raise SourceSchemaError("wolverine careers listing target changed")
    if props.get("enabledLocaleKeys") != ["en"]:
        raise SourceSchemaError("wolverine careers locale scope changed")

    exclusions = props.get("excludeFilters")
    if not isinstance(exclusions, list):
        raise SourceSchemaError("wolverine careers filter metadata changed")
    fields: set[str] = set()
    for exclusion in exclusions:
        if not isinstance(exclusion, Mapping):
            raise SourceSchemaError("wolverine careers filter metadata changed")
        field = str(exclusion.get("attribute") or "").strip()
        if not field or exclusion.get("values") != [] or field in fields:
            raise SourceSchemaError("wolverine careers filter scope is not empty")
        fields.add(field)
    if fields != _EXPECTED_EXCLUSION_FIELDS:
        raise SourceSchemaError("wolverine careers filter metadata changed")


def _sitemap_identities(xml: Any) -> frozenset[str]:
    if not isinstance(xml, str):
        raise SourceSchemaError("wolverine sitemap must be XML text")
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        raise SourceSchemaError("wolverine sitemap was malformed") from exc
    if root.tag.rsplit("}", 1)[-1] != "urlset":
        raise SourceSchemaError("wolverine sitemap root changed")

    identities: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "loc" or not node.text:
            continue
        value = node.text.strip()
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise SourceSchemaError("wolverine sitemap URL was invalid") from exc
        if "/postings/" not in parsed.path:
            continue
        match = _SITEMAP_POSTING_PATH.fullmatch(parsed.path)
        if (
            match is None
            or parsed.scheme.casefold() != "https"
            or (parsed.hostname or "").casefold() != HOST
            or parsed.netloc.casefold() != HOST
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise SourceSchemaError("wolverine sitemap posting URL was invalid")
        identities.append(match.group("identity"))
    if len(identities) != len(set(identities)):
        raise SourceSchemaError("wolverine sitemap contained duplicate postings")
    return frozenset(identities)


def _posting_identity(path_value: Any, url_value: Any) -> tuple[str, str]:
    path = _required_text(path_value, "path")
    match = _POSTING_PATH.fullmatch(path)
    if match is None:
        raise SourceSchemaError("wolverine posting path was invalid")
    source_url = _required_text(url_value, "url")
    try:
        parsed = urlsplit(source_url)
    except ValueError as exc:
        raise SourceSchemaError("wolverine posting URL was invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != HOST
        or parsed.netloc.casefold() != HOST
        or parsed.username
        or parsed.password
        or parsed.path != path
        or parsed.query
        or parsed.fragment
    ):
        raise SourceSchemaError("wolverine posting URL was invalid")
    return match.group("identity"), source_url


def _location(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _LOCATION_FIELDS:
        raise SourceSchemaError("wolverine posting location schema changed")
    return {
        "id": _numeric_text(value.get("id"), "location id"),
        "city": _required_text(value.get("city"), "location city"),
        "name": _required_text(value.get("name"), "location name"),
        "postal_code": _optional_text(
            value.get("postal_code"),
            "location postal_code",
        ),
        "province": _required_text(value.get("province"), "location province"),
        "street_address": _optional_text(
            value.get("street_address"),
            "location street_address",
        ),
    }


def _job(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _JOB_FIELDS:
        raise SourceSchemaError("wolverine posting job schema changed")
    return {
        "id": _numeric_text(value.get("id"), "job id"),
        "requisition_id": _required_text(
            value.get("requisition_id"),
            "requisition_id",
        ),
        "department": _structure(value.get("department"), "department"),
        "division": _structure(value.get("division"), "division"),
        "sub_department": _structure(
            value.get("structure_custom_group_one"),
            "sub_department",
            titled=True,
        ),
    }


def _structure(value: Any, label: str, *, titled: bool = False) -> str:
    expected = {"id", "name", "title"} if titled else {"id", "name"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise SourceSchemaError(f"wolverine {label} schema changed")
    _numeric_text(value.get("id"), f"{label} id")
    if titled:
        _required_text(value.get("title"), f"{label} title")
    return _required_text(value.get("name"), f"{label} name")


def _compensation(record: Mapping[str, Any]) -> str:
    visible = record.get("compensation_visible")
    if type(visible) is not bool:
        raise SourceSchemaError("wolverine compensation visibility was invalid")
    text = _optional_text(record.get("compensation"), "compensation")
    for label in ("compensation_currency", "compensation_frequency"):
        _optional_text(record.get(label), label)
    for label in ("compensation_minimum", "compensation_maximum"):
        value = record.get(label)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, (int, float))
        ):
            raise SourceSchemaError(f"wolverine {label} was invalid")
    return text if visible else ""


def _deadline(value: Any) -> str:
    if value is None:
        return ""
    text = _required_text(value, "deadline_at")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError as exc:
        raise SourceSchemaError("wolverine deadline_at was invalid") from exc


def _numeric_text(value: Any, label: str) -> str:
    text = _required_text(value, label)
    if _POSTING_ID.fullmatch(text) is None:
        raise SourceSchemaError(f"wolverine {label} was invalid")
    return text


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SourceSchemaError(f"wolverine {label} must be nonblank text")
    return value.strip()


def _optional_text(value: Any, label: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SourceSchemaError(f"wolverine {label} must be text or null")
    return value.strip()


def _joined_html(*values: Any) -> str:
    parts = [html_to_text(_required_text(value, "description")) for value in values]
    return "\n\n".join(part for part in parts if part)


def _deduplicate_rows(rows: Iterable[dict]) -> tuple[list[dict], int]:
    unique: list[dict] = []
    id_index: dict[str, int] = {}
    url_index: dict[str, int] = {}
    duplicates = 0
    for row in rows:
        source_id = str(row.get("extra", {}).get("source_id") or "")
        source_url = norm_url(str(row.get("source_url") or ""))
        if not source_id or not source_url:
            raise SourceSchemaError("wolverine canonical row lacks stable identity")
        id_match = id_index.get(source_id)
        url_match = url_index.get(source_url)
        if id_match is None and url_match is None:
            index = len(unique)
            id_index[source_id] = index
            url_index[source_url] = index
            unique.append(row)
            continue
        if (
            id_match is None
            or url_match is None
            or id_match != url_match
            or unique[id_match] != row
        ):
            raise SourceSchemaError(
                "wolverine returned conflicting posting IDs, URLs, or content"
            )
        duplicates += 1
    return unique, duplicates
