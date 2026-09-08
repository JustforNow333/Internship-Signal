"""Atlassian's atomic first-party careers inventory.

The official all-jobs Careers component GETs /endpoint/careers/listings once;
all search, filters and result counts are computed locally from that array.
There is no offset, cursor, or server-side result limit in this contract. The
complete array is the inventory bound, including [] for an empty board. The sampled native
iCIMS listing portal redirects to this frontend. Fail on malformed or conflicting records, never silently
shrink this inventory. No detail requests or updatedDate-as-posted-date mapping.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from watcher.config.models import CompanyCfg
from watcher.sources.contracts import SourceSchemaError
from watcher.sources.diagnostics import DirectDiagnosticsMixin
from watcher.sources.rows import make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import get_json_response

MAX_POSTINGS = 10_000
PORTALS = frozenset({
    'globalcareers-atlassian.icims.com',
    'campus-globalcareers-atlassian.icims.com',
    'careers-americas.icims.com',
    'careers-apac-atlassian.icims.com',
})


class AtlassianSource(DirectDiagnosticsMixin):
    name = 'atlassian'

    def __init__(self, *, request_json=None):
        self._request_json = request_json
        self.request_count = 0
        self._begin_direct_diagnostics()

    @staticmethod
    def endpoint():
        return 'https://www.atlassian.com/endpoint/careers/listings'

    def fetch(self, company: CompanyCfg) -> list[dict]:
        self._begin_direct_diagnostics()
        self.request_count = 0
        # One bounded attempt. Transport/schema failures propagate; no partial
        # inventory is published and no retry can mix two inventories.
        request = self._request_json or get_json_response
        self.request_count += 1
        response = request(self.endpoint(), self.name)
        records = getattr(response, 'payload', response)
        if not isinstance(records, list) or len(records) > MAX_POSTINGS:
            raise SourceSchemaError('atlassian invalid inventory array')
        rows, ids, urls = [], {}, set()
        duplicates = 0
        for record in records:
            row = _posting(record, company)
            identity = row['extra']['source_requisition_id']
            if identity in ids:
                if ids[identity] != record:
                    raise SourceSchemaError('atlassian conflicting posting identity')
                duplicates += 1
                continue
            if row['source_url'] in urls:
                raise SourceSchemaError('atlassian conflicting posting URL')
            ids[identity] = record
            urls.add(row['source_url'])
            rows.append(row)
        self._finish_direct_diagnostics(rows, duplicate_row_count=duplicates)
        return rows


def _posting(record, company):
    if not isinstance(record, dict):
        raise SourceSchemaError('atlassian invalid posting')
    posting_id = record.get('id')
    portal_id = record.get('portalId')
    post = record.get('portalJobPost')
    if (type(posting_id) is not int or posting_id <= 0
            or type(portal_id) is not int or portal_id <= 0
            or not isinstance(post, dict)
            or type(post.get('id')) is not int or post['id'] != posting_id
            or type(post.get('portalId')) is not int or post['portalId'] != portal_id):
        raise SourceSchemaError('atlassian conflicting posting or portal identity')
    title = record.get('title')
    locations = record.get('locations')
    if not isinstance(title, str) or not title.strip():
        raise SourceSchemaError('atlassian missing title')
    if not isinstance(locations, list) or any(
        not isinstance(x, str) or not x.strip() for x in locations
    ):
        raise SourceSchemaError('atlassian invalid locations')
    url = post.get('portalUrl')
    if not isinstance(url, str):
        raise SourceSchemaError('atlassian missing posting URL')
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.netloc in PORTALS
                 and not parsed.query and not parsed.fragment
                 and re.fullmatch(r'/jobs/' + str(posting_id) + r'/[^/]+/job', parsed.path))
    except ValueError:
        valid = False
    if not valid:
        raise SourceSchemaError('atlassian invalid posting URL')
    texts = []
    for field in ('overview', 'responsibilities', 'qualifications'):
        value = record.get(field)
        if not isinstance(value, str):
            raise SourceSchemaError('atlassian invalid posting text')
        texts.append(html_to_text(value))
    return make_row(
        company=company.name, title=title.strip(), location='; '.join(locations),
        description='\n\n'.join(texts[:2]), requirements=texts[2], source_url=url,
        source='direct', source_adapter='atlassian',
        extra={'source_requisition_id': f'atlassian:{posting_id}'},
    )
