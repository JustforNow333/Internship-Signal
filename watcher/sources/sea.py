"""Sea corporate careers: bounded first-party listing API plus location metadata.

The official /jobs bundle builds one listing request, /api/user/job/list with
externalEntityId and postType taken from its own enums (``{Sea: 3}`` and
``{Sea: 1}``). Its mobile view uses limit=1000; desktop uses offset pagination.

``post_type`` is the honored scope gate and the only one this adapter trusts.
``post_type=1`` is the corporate board; ``post_type=2`` is Shopee/SPX and
``post_type=4`` is Monee/MariBank, both separate boards this adapter must never
collect. ``external_entity_id`` is sent to mirror the official request but the
server ignores it: filtered and unfiltered responses are byte-identical, and one
``post_type=1`` response carries entity 1, 3, and 4 together. Entity also does
not partition the board - ``Sea Labs Indonesia`` appears under entity 1 and 3,
and ``Sea Corporate`` under entity 3 and 4 - so it is retained as metadata only
and never used to include or exclude a posting. Nothing is discarded by entity.

Success requires code=0, an explicit stable total_count, exact raw/unique counts,
and full pages until that total. A null list is empty only with an explicit zero
total. Missing/error envelopes, early empty pages, duplicates, and cap exhaustion
fail closed. Locations come from the same page's public Next.js Flight metadata.
This covers Sea's corporate board, not the separate Shopee/Garena/Monee boards.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from watcher.config.models import CompanyCfg
from watcher.sources.contracts import SourceSchemaError
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import get_json_response, get_text_response

HOST = 'career.sea.com'
PAGE_SIZE = 1000
MAX_POSTINGS = 10_000
MAX_METADATA_BYTES = 4 * 1024 * 1024


class SeaSource(DirectDiagnosticsMixin):
    name = 'sea'

    def __init__(self, *, request_json: Callable | None = None,
                 request_text: Callable | None = None, page_size: int = PAGE_SIZE):
        if type(page_size) is not int or not 1 <= page_size <= PAGE_SIZE:
            raise ValueError('sea invalid page size')
        self.page_size = page_size
        self._request_json = request_json
        self._request_text = request_text
        self.request_count = 0
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint(*, offset: int = 0, limit: int = PAGE_SIZE) -> str:
        query = urlencode({'external_entity_id': 3, 'post_type': 1,
                           'offset': offset, 'limit': limit})
        return f'https://{HOST}/api/user/job/list?{query}'

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        self.request_count += 1
        if self._request_text:
            response = self._request_text(f'https://{HOST}/jobs', self.name)
        else:
            response = get_text_response(f'https://{HOST}/jobs', self.name,
                                         max_response_bytes=MAX_METADATA_BYTES)
        locations = _locations(getattr(response, 'text', response))
        rows: list[dict] = []
        ids, native_ids = set(), set()
        total = None
        # Single-attempt bounded transport: failures discard the entire crawl.
        # No retries or partial healthy results, and no per-posting requests.
        for offset in range(0, MAX_POSTINGS, self.page_size):
            self.request_count += 1
            request = self._request_json or get_json_response
            response = request(self.endpoint(offset=offset, limit=self.page_size), self.name)
            records, count = _page(getattr(response, 'payload', response))
            if total is None:
                total = count
            if count != total:
                raise SourceSchemaError('sea inventory total changed')
            expected = min(self.page_size, total - offset)
            if len(records) != expected:
                raise SourceSchemaError('sea page omitted or exceeded inventory records')
            for record in records:
                row, native_id = _posting(record, company, locations)
                identity = row['extra']['source_requisition_id']
                if identity in ids or native_id in native_ids:
                    raise SourceSchemaError('sea duplicate posting identity')
                ids.add(identity)
                native_ids.add(native_id)
                rows.append(row)
            if len(rows) == total:
                self._finish_direct_diagnostics(rows)
                return rows
        raise SourceSchemaError('sea inventory exceeded page safeguard')


def _page(payload: Any) -> tuple[list, int]:
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
        raise SourceSchemaError('sea missing successful listing envelope')
    data = payload.get('data')
    if not isinstance(data, dict) or 'job_list' not in data:
        raise SourceSchemaError('sea missing inventory')
    count = data.get('total_count')
    if type(count) is not int or not 0 <= count <= MAX_POSTINGS:
        raise SourceSchemaError('sea missing or invalid inventory total')
    records = data['job_list']
    if records is None and count == 0:
        records = []
    if not isinstance(records, list):
        raise SourceSchemaError('sea missing listing records')
    return records, count


def _locations(html: Any) -> dict[tuple[int, int], tuple[str, str]]:
    if not isinstance(html, str) or len(html.encode('utf-8')) > MAX_METADATA_BYTES:
        raise SourceSchemaError('sea invalid metadata page')
    chunks = []
    try:
        for match in re.finditer(r'self\.__next_f\.push\((.*?)\)</script>', html, re.DOTALL):
            value = json.loads(match[1])
            if isinstance(value, list) and len(value) == 2 and value[0] == 1 and isinstance(value[1], str):
                chunks.append(value[1])
        flight = ''.join(chunks)
        # Read JSON only; never execute page scripts or evaluate Flight code.
        markers = list(re.finditer(r'"meta":\s*\{', flight))
        if len(markers) != 1:
            raise SourceSchemaError('sea missing or ambiguous location metadata')
        start = flight.index('{', markers[0].start())
        meta, _ = json.JSONDecoder().raw_decode(flight, start)
        records = meta['flatLocations']
    except (ValueError, KeyError, TypeError, RecursionError) as exc:
        raise SourceSchemaError('sea malformed location metadata') from exc
    if not isinstance(records, list) or not records or len(records) > MAX_POSTINGS:
        raise SourceSchemaError('sea invalid location inventory')
    locations = {}
    for record in records:
        if not isinstance(record, dict):
            raise SourceSchemaError('sea invalid location record')
        region, city = record.get('regionId'), record.get('cityId')
        names = record.get('cityName'), record.get('regionName')
        if (type(region) is not int or type(city) is not int or region <= 0 or city <= 0
                or any(not isinstance(v, str) or not v.strip() for v in names)):
            raise SourceSchemaError('sea invalid location identity')
        key = region, city
        if key in locations:
            raise SourceSchemaError('sea duplicate location identity')
        locations[key] = names
    return locations


def _posting(record: Any, company: CompanyCfg, locations: dict) -> tuple[dict, int]:
    if not isinstance(record, dict):
        raise SourceSchemaError('sea invalid posting')
    # post_type is the server-honored corporate-board scope gate. Entity is
    # recorded, never trusted for scope: the server ignores the entity filter and
    # one corporate response legitimately spans several entity values.
    if type(record.get('post_type')) is not int or record['post_type'] != 1:
        raise SourceSchemaError('sea posting outside configured corporate board')
    entity = record.get('external_entity_id')
    if type(entity) is not int or entity <= 0:
        raise SourceSchemaError('sea invalid posting entity')
    identity, native_id = record.get('job_id'), record.get('id')
    if (not isinstance(identity, str) or not re.fullmatch(r'J[0-9]{1,30}', identity)
            or type(native_id) is not int or native_id <= 0):
        raise SourceSchemaError('sea invalid posting identity')
    region, city = record.get('region_id'), record.get('city_id')
    if type(region) is not int or type(city) is not int or (region, city) not in locations:
        raise SourceSchemaError('sea unresolved posting location')
    city_name, country = locations[region, city]
    for field in ['job_name', 'job_description', 'requirements', 'sub_team_description']:
        if not isinstance(record.get(field), str):
            raise SourceSchemaError('sea invalid posting text')
    if not record['job_name'].strip():
        raise SourceSchemaError('sea missing title')
    row = make_row(
        company=company.name, title=record['job_name'].strip(),
        location=f'{city_name}, {country}',
        description=html_to_text(record['job_description'] + '\n' + record['sub_team_description']),
        requirements=html_to_text(record['requirements']),
        source_url=f'https://{HOST}/position/{identity}',
        source='direct', source_adapter='sea',
        extra={'source_id': identity, 'source_requisition_id': identity,
               'source_system': 'sea', 'country': country, 'active': True,
               'sea_external_entity_id': entity},
    )
    return row, native_id
