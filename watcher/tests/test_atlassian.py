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


def test_campus_americas_portal_is_accepted():
    """Atlassian publishes early-career roles on a campus-americas portal.

    The official all-jobs endpoint served a "Software Engineer, 2027 Graduate
    U.S." posting on campus-americas.icims.com, a portal the allowlist did not
    carry. Because one unlisted portal fails the whole atomic inventory, every
    Atlassian posting was lost -- including the only graduate-engineering row.
    """

    data = records()
    data[0]['portalJobPost']['portalUrl'] = (
        'https://campus-americas.icims.com/jobs/25583/'
        'software-engineer%2c-2027-graduate-u.s./job'
    )

    source, rows = fetch(data)

    assert len(rows) == 2
    assert rows[0]['source_url'] == (
        'https://campus-americas.icims.com/jobs/25583/'
        'software-engineer%2c-2027-graduate-u.s./job'
    )
    assert source.last_health_diagnostics.complete


def test_globalmobility_portal_is_accepted():
    """Atlassian publishes relocation roles on a globalmobility portal.

    On 2026-10-29 the official all-jobs endpoint served postings 25398 and
    27067 on globalmobility-careers-atlassian.icims.com (portalId 193343), a
    portal the allowlist did not carry, so the whole inventory was rejected.
    """

    data = records()
    data[0]['portalId'] = data[0]['portalJobPost']['portalId'] = 193343
    data[0]['portalJobPost']['portalUrl'] = (
        'https://globalmobility-careers-atlassian.icims.com/jobs/25583/'
        'sales-development-representative%2c-mid-market-jsm-dach/job'
    )

    source, rows = fetch(data)

    assert len(rows) == 2
    assert rows[0]['source_url'].startswith(
        'https://globalmobility-careers-atlassian.icims.com/jobs/25583/'
    )
    assert source.last_health_diagnostics.complete


def test_one_unlisted_portal_still_fails_the_whole_inventory():
    """The allowlist keeps its meaning: an off-portal URL is still rejected."""

    data = records()
    data[0]['portalJobPost']['portalUrl'] = (
        'https://careers.example.com/jobs/25583/a/job'
    )

    with pytest.raises(SourceSchemaError):
        fetch(data)


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


TEXT_FIELDS = ('overview', 'responsibilities', 'qualifications')


def _textless_posting():
    """The live shape of postings 25274 and 27466 (2026-10-29).

    The official inventory omits all three text fields on these postings, and
    27466 also has no locations. Rejecting them failed the whole atomic
    inventory on every run from 2026-10-24.
    """

    return {
        'portalJobPost': {
            'portalId': 111,
            'portalUrl': 'https://careers-americas.icims.com/jobs/27466/'
                         'senior-principle-machine-learning-engineer/job',
            'id': 27466,
            'updatedDate': '2026-09-29 08:11 PM',
        },
        'id': 27466,
        'portalId': 111,
        'title': 'Senior Principle Machine Learning Engineer',
        'type': 'Full-Time',
        'locations': [],
        'category': 'Engineering',
        'applyUrl': 'https://careers-americas.icims.com/jobs/27466/'
                    'senior-principle-machine-learning-engineer/job?mode=apply',
    }


@pytest.mark.parametrize('form', ['absent', 'null'])
def test_missing_posting_text_is_empty_and_inventory_stays_whole(form):
    data = records()
    textless = _textless_posting()
    if form == 'null':
        textless.update(dict.fromkeys(TEXT_FIELDS))
    data.append(textless)
    data.append(copy.deepcopy(textless))  # the live board repeats some postings

    source, rows = fetch(data)

    assert len(rows) == 3
    assert rows[0]['description'] and rows[0]['requirements']
    row = rows[2]
    assert row['title'] == 'Senior Principle Machine Learning Engineer'
    assert row['extra']['source_requisition_id'] == 'atlassian:27466'
    assert row['source_url'] == textless['portalJobPost']['portalUrl']
    assert row['description'] == '' and row['requirements'] == ''
    assert source.last_health_diagnostics.duplicate_row_count == 1
    assert source.last_health_diagnostics.complete


@pytest.mark.parametrize('field', TEXT_FIELDS)
@pytest.mark.parametrize('value', [0, ['text'], {'html': 'text'}, True])
def test_non_string_posting_text_still_fails(field, value):
    data = records()
    data[0][field] = value
    with pytest.raises(SourceSchemaError, match='invalid posting text'):
        fetch(data)


def test_textless_posting_still_requires_identity_title_and_url():
    for mutate in (
        lambda r: r.pop('id'),
        lambda r: r.update(title=None),
        lambda r: r['portalJobPost'].pop('portalUrl'),
        lambda r: r['portalJobPost'].update(portalId=7),
    ):
        textless = _textless_posting()
        mutate(textless)
        with pytest.raises(SourceSchemaError):
            fetch(records() + [textless])


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
