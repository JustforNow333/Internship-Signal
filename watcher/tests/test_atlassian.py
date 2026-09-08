"""Offline checks for the atomic inventory used by Atlassian's careers UI."""
import copy
import json
from pathlib import Path

import pytest

from watcher.config.models import CompanyCfg
from watcher.sources.atlassian import AtlassianSource
from watcher.sources.contracts import SourceSchemaError

FIXTURE = Path(__file__).parent / 'fixtures' / 'atlassian.json'


def records():
    return json.loads(FIXTURE.read_text(encoding='utf-8'))


def fetch(payload):
    source = AtlassianSource(request_json=lambda *_: payload)
    return source, source.fetch(CompanyCfg(name='Atlassian', ats='atlassian'))


def test_saved_official_inventory():
    source, rows = fetch(records())
    assert len(rows) == 2
    assert rows[0]['title'] == records()[0]['title']
    assert rows[0]['extra']['source_adapter'] == 'atlassian'
    assert rows[0]['extra']['source_requisition_id'] == 'atlassian:25583'
    assert rows[0]['date_posted'] == ''  # updatedDate is not a posting date
    assert source.request_count == 1
    assert source.last_health_diagnostics.complete


@pytest.mark.parametrize('payload', [None, {}, {'jobs': []}, '', [None]])
def test_invalid_inventory_fails(payload):
    with pytest.raises(SourceSchemaError):
        fetch(payload)


def test_explicit_empty():
    source, rows = fetch([])
    assert rows == []
    assert source.last_health_diagnostics.complete


@pytest.mark.parametrize('mutation', ['conflict', 'id', 'portal', 'url', 'title', 'locations'])
def test_incomplete_or_conflicting_inventory_fails(mutation):
    data = records()
    if mutation == 'conflict':
        data.append(copy.deepcopy(data[0]))
        data[-1]['title'] += ' changed'
    elif mutation == 'id':
        data[0]['portalJobPost']['id'] += 1
    elif mutation == 'portal':
        data[0]['portalJobPost']['portalId'] += 1
    elif mutation == 'url':
        data[0]['portalJobPost']['portalUrl'] = 'https://example.com/jobs/25583/a/job'
    elif mutation == 'title':
        data[0]['title'] = ''
    else:
        data[0]['locations'] = [None]
    with pytest.raises(SourceSchemaError):
        fetch(data)


def test_registry_watchlist_origin_and_catalog():
    from watcher.sources.registry import build_direct_sources, DIRECT_ATS
    from watcher.config.loader import load_watchlist
    from watcher.collection_concurrency import direct_origin_key
    assert 'atlassian' in DIRECT_ATS
    assert isinstance(build_direct_sources()['atlassian'], AtlassianSource)
    assert direct_origin_key('atlassian') == 'https://www.atlassian.com'
    company = next(c for c in load_watchlist().companies if c.name == 'Atlassian')
    assert company.ats == 'atlassian'


def test_identical_copies_follow_frontend_identity_map():
    data = records()
    data.append(copy.deepcopy(data[0]))
    source, rows = fetch(data)
    assert len(rows) == 2
    assert source.last_health_diagnostics.duplicate_row_count == 1
    assert source.last_health_diagnostics.complete


def test_native_identity_survives_title_and_url_slug_changes():
    from backend.app.dedupe import posting_identity_key
    data = records()
    _, before = fetch(data)
    data[0]['title'] = 'Updated title'
    data[0]['portalJobPost']['portalUrl'] = (
        'https://globalcareers-atlassian.icims.com/jobs/25583/updated-title/job'
    )
    _, after = fetch(data)
    assert posting_identity_key(before[0]) == posting_identity_key(after[0])


def test_catalog_coverage_and_replay_use_canonical_registry():
    from dataclasses import replace
    from backend.app.hosted.catalog import CompanyCatalog
    from watcher.config.loader import load_watchlist
    from watcher.collection_snapshot import collection_config_fingerprint
    from watcher.health.coverage import build_coverage_audit
    from watcher.health.models import COVERAGE_AUDIT_DIRECT_UNVERIFIED
    config = load_watchlist()
    public = CompanyCatalog.from_watcher_config(config).resolve('Atlassian Corporation')
    assert public.name == 'Atlassian'
    assert public.coverage == 'direct' and public.selectable
    report = build_coverage_audit(config, {}, state_database_present=False)
    entry = next(c for c in report.companies if c.company == 'Atlassian')
    assert entry.state == COVERAGE_AUDIT_DIRECT_UNVERIFIED
    without = replace(config, companies=tuple(c for c in config.companies if c.name != 'Atlassian'))
    assert collection_config_fingerprint(config) != collection_config_fingerprint(without)


def test_transport_failure_is_one_attempt_and_never_complete():
    from watcher.sources.contracts import SourceFetchError
    calls = []
    def request(*args):
        calls.append(args)
        raise SourceFetchError('unavailable', status_code=429)
    source = AtlassianSource(request_json=request)
    with pytest.raises(SourceFetchError):
        source.fetch(CompanyCfg(name='Atlassian', ats='atlassian'))
    assert len(calls) == 1
    assert not source.last_health_diagnostics.complete


def test_inventory_cap_fails_before_parsing(monkeypatch):
    monkeypatch.setattr('watcher.sources.atlassian.MAX_POSTINGS', 1)
    with pytest.raises(SourceSchemaError):
        fetch(records())
