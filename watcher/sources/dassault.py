"""Dassault Systèmes' bounded first-party careers search contract.

The official 3ds.com jobs frontend configures its WOC search card with the
public ``/apisearch/card_search_api`` endpoint. Offset pages are not safe: the
date sort has ties and repeated live passes produced duplicate/omitted records
whose counts cancelled out. This adapter therefore never paginates.

Each accepted inventory has one atomic response containing every result. A
one-record request at its reported terminal boundary must then return an exact,
non-estimated empty result with the same total. Two consecutive complete
inventories must agree after sorting by stable ``card_id``. The endpoint's
current inventory fits below its tested 1,000-result request limit; growth past
that bound fails closed instead of silently truncating. The board is already
scoped to Dassault Systèmes group careers by the official host and
``content_type=career`` query, including its product brands.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import re
import unicodedata
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

from watcher.config.models import CompanyCfg
from watcher.sources.contracts import SourceSchemaError
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import get_text_response


HOST = "www.3ds.com"
SEARCH_PATH = "/apisearch/card_search_api"
SEARCH_QUERY = '#all card_content_lang:en  (card_content_type="career") '
SEARCH_SORT = "desc(card_content_start_datetime)"
MAX_POSTINGS = 1_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TERMINAL_PROBES = 6
DEFAULT_SNAPSHOT_PASSES = 3
REQUIRED_META_FIELDS = frozenset(
    {
        "card_id",
        "content_funnel",
        "content_title",
        "content_info_1_value",
        "content_info_2_value",
        "content_start_datetime",
        "content_summary",
        "content_cta_1_url",
        "content_type",
        "content_lang",
        "visibility",
        "content_categories",
    }
)


@dataclass(frozen=True)
class _Posting:
    identity: str
    title: str
    location: str
    job_type: str
    date_posted: str
    description: str
    source_url: str
    categories: str


@dataclass(frozen=True)
class _Snapshot:
    postings: tuple[_Posting, ...]


class DassaultSource(DirectDiagnosticsMixin):
    """Collect the official group-wide Dassault careers inventory."""

    name = "dassault"

    def __init__(
        self,
        *,
        request_json: Callable[[str, str], Any] | None = None,
        max_terminal_probes: int = DEFAULT_TERMINAL_PROBES,
        max_snapshot_passes: int = DEFAULT_SNAPSHOT_PASSES,
    ) -> None:
        if type(max_terminal_probes) is not int or not 1 <= max_terminal_probes <= 6:
            raise ValueError("dassault invalid terminal-probe limit")
        if type(max_snapshot_passes) is not int or not 2 <= max_snapshot_passes <= 3:
            raise ValueError("dassault invalid snapshot-pass limit")
        self._request_json = request_json
        self.max_terminal_probes = max_terminal_probes
        self.max_snapshot_passes = max_snapshot_passes
        self.request_count = 0
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint(*, start: int = 0, results: int = MAX_POSTINGS) -> str:
        if type(start) is not int or not 0 <= start <= MAX_POSTINGS:
            raise ValueError("dassault invalid result offset")
        if type(results) is not int or not 1 <= results <= MAX_POSTINGS:
            raise ValueError("dassault invalid result limit")
        query = urlencode(
            {
                "q": SEARCH_QUERY,
                "s": SEARCH_SORT,
                "b": start,
                "hf": results,
                "output_format": "json",
            }
        )
        return f"https://{HOST}{SEARCH_PATH}?{query}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        previous: _Snapshot | None = None
        for _ in range(self.max_snapshot_passes):
            current = self._fetch_snapshot()
            if current == previous:
                rows = [_row(posting, company) for posting in current.postings]
                self._finish_direct_diagnostics(rows)
                return rows
            previous = current
        raise SourceSchemaError("dassault could not prove a stable complete inventory")

    def _fetch_snapshot(self) -> _Snapshot:
        hits, total, estimated = _envelope(
            self._request(start=0, results=MAX_POSTINGS),
            expected_start=0,
            result_limit=MAX_POSTINGS,
        )
        if total == 0:
            if estimated:
                raise SourceSchemaError("dassault empty inventory total is approximate")
            return _Snapshot(())
        if len(hits) != total:
            raise SourceSchemaError("dassault atomic response omitted inventory records")

        for _ in range(self.max_terminal_probes):
            terminal_hits, terminal_total, terminal_estimated = _envelope(
                self._request(start=total, results=1),
                expected_start=total,
                result_limit=1,
            )
            if terminal_total != total:
                raise SourceSchemaError("dassault inventory total changed during snapshot")
            if terminal_hits:
                raise SourceSchemaError("dassault terminal probe found omitted records")
            if not terminal_estimated:
                break
        else:
            raise SourceSchemaError("dassault could not obtain an exact terminal total")

        postings = tuple(sorted((_posting(hit) for hit in hits), key=lambda job: int(job.identity)))
        identities = [posting.identity for posting in postings]
        urls = [posting.source_url for posting in postings]
        if len(identities) != len(set(identities)):
            raise SourceSchemaError("dassault duplicate posting identity")
        if len(urls) != len(set(urls)):
            raise SourceSchemaError("dassault duplicate canonical posting URL")
        return _Snapshot(postings)

    def _request(self, *, start: int, results: int) -> Any:
        self.request_count += 1
        endpoint = self.endpoint(start=start, results=results)
        if self._request_json is not None:
            response = self._request_json(endpoint, self.name)
            return getattr(response, "payload", response)
        response = get_text_response(
            endpoint,
            self.name,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        try:
            return json.loads(response.text)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise SourceSchemaError("dassault returned malformed listing JSON") from exc


def _envelope(
    payload: Any,
    *,
    expected_start: int,
    result_limit: int,
) -> tuple[list[Any], int, bool]:
    if not isinstance(payload, dict):
        raise SourceSchemaError("dassault missing listing envelope")
    if payload.get("autocorrected") is not False:
        raise SourceSchemaError("dassault listing query was autocorrected")
    estimated = payload.get("estimated")
    if type(estimated) is not bool:
        raise SourceSchemaError("dassault missing total precision")
    total = payload.get("nhits")
    matches = payload.get("nmatches")
    start = payload.get("start")
    if (
        type(total) is not int
        or type(matches) is not int
        or type(start) is not int
        or total != matches
        or start != expected_start
        or not 0 <= total <= MAX_POSTINGS
    ):
        raise SourceSchemaError("dassault invalid or unsafe inventory bounds")
    hits = payload.get("hits")
    if not isinstance(hits, list) or len(hits) > result_limit:
        raise SourceSchemaError("dassault invalid listing records")
    if len(hits) != min(result_limit, max(0, total - expected_start)):
        raise SourceSchemaError("dassault response count does not match inventory total")
    return hits, total, estimated


def _posting(hit: Any) -> _Posting:
    if not isinstance(hit, dict) or type(hit.get("did")) is not int or hit["did"] <= 0:
        raise SourceSchemaError("dassault invalid posting")
    metas = hit.get("metas")
    if not isinstance(metas, list) or not metas or len(metas) > 100:
        raise SourceSchemaError("dassault invalid posting metadata")
    values: dict[str, str] = {}
    for meta in metas:
        if not isinstance(meta, dict) or not isinstance(meta.get("name"), str):
            raise SourceSchemaError("dassault malformed posting metadata")
        name = meta["name"]
        if name not in REQUIRED_META_FIELDS:
            continue
        if name in values:
            raise SourceSchemaError("dassault duplicate required posting metadata")
        value = meta.get("value")
        if isinstance(value, str):
            values[name] = value

    identity = _required_text(values, "card_id", 30)
    if not re.fullmatch(r"[1-9][0-9]{0,29}", identity):
        raise SourceSchemaError("dassault invalid posting identity")
    if _required_text(values, "content_funnel", 30) != identity:
        raise SourceSchemaError("dassault inconsistent posting identity")
    if hit.get("url") != f"CARD_ID={identity}&CONTENT_LANG=en&":
        raise SourceSchemaError("dassault invalid posting lookup identity")
    if _required_text(values, "content_type", 30) != "career":
        raise SourceSchemaError("dassault posting outside careers scope")
    if _required_text(values, "content_lang", 10) != "en":
        raise SourceSchemaError("dassault posting outside configured language")
    if _required_text(values, "visibility", 10) != "true":
        raise SourceSchemaError("dassault posting is not visible")

    raw_date = _required_text(values, "content_start_datetime", 40)
    try:
        date_posted = datetime.strptime(raw_date, "%Y/%m/%d %H:%M:%S").date().isoformat()
    except ValueError as exc:
        raise SourceSchemaError("dassault invalid posting date") from exc

    return _Posting(
        identity=identity,
        title=_required_text(values, "content_title", 2_000),
        location=_required_text(values, "content_info_2_value", 2_000),
        job_type=_required_text(values, "content_info_1_value", 200),
        date_posted=date_posted,
        description=_required_text(values, "content_summary", 250_000),
        source_url=_canonical_url(
            _required_text(values, "content_cta_1_url", 4_000), identity
        ),
        categories=_required_text(values, "content_categories", 10_000),
    )


def _required_text(values: dict[str, str], name: str, limit: int) -> str:
    value = values.get(name)
    if not isinstance(value, str):
        raise SourceSchemaError(f"dassault missing posting field {name}")
    value = value.strip()
    if not value or len(value) > limit or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise SourceSchemaError(f"dassault invalid posting field {name}")
    return value


def _canonical_url(raw: str, identity: str) -> str:
    try:
        parsed = urlsplit(unicodedata.normalize("NFC", raw))
        port = parsed.port
    except ValueError as exc:
        raise SourceSchemaError("dassault invalid canonical posting URL") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or re.search(r"%(?![0-9A-Fa-f]{2})", parsed.path)
    ):
        raise SourceSchemaError("dassault invalid canonical posting URL")
    path = unicodedata.normalize("NFC", unquote(parsed.path))
    match = re.fullmatch(r"/careers/jobs/[^/]+-([1-9][0-9]{0,29})", path)
    if match is None or match.group(1) != identity:
        raise SourceSchemaError("dassault canonical URL does not match posting identity")
    return urlunsplit(("https", HOST, quote(path, safe="/-._~"), "", ""))


def _row(posting: _Posting, company: CompanyCfg) -> dict:
    return make_row(
        company=company.name,
        title=posting.title,
        location=posting.location,
        description=html_to_text(posting.description),
        date_posted=posting.date_posted,
        internship_type=posting.job_type,
        source_url=posting.source_url,
        source="direct",
        source_adapter="dassault",
        extra={
            "source_id": posting.identity,
            "source_requisition_id": posting.identity,
            "source_system": "dassault_woc",
            "active": True,
            "dassault_categories": posting.categories,
        },
    )
