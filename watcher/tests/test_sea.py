"""Sea fixtures come from its public listing API and careers-page metadata."""
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.config.models import CompanyCfg
from watcher.sources.contracts import SourceFetchError, SourceSchemaError
from watcher.sources.sea import SeaSource

FIXTURES = Path(__file__).parent / 'fixtures'


def records(count=2):
    payload = json.loads((FIXTURES / 'sea_listing.json').read_text(encoding='utf-8'))
    return payload['data']['job_list'][:count]


def metadata():
    return json.loads((FIXTURES / 'sea_locations.json').read_text(encoding='utf-8'))


def html(meta=None):
    payload = '1:' + json.dumps({'meta': meta if meta is not None else metadata()}) + '\n'
    return '<script>self.__next_f.push(' + json.dumps([1, payload]) + ')</script>'


def page(rows, total):
    return {'code': 0, 'message': 'success', 'data': {'job_list': rows, 'total_count': total}}


def source(pages, **kw):
    calls = []
    def request(url, name):
        calls.append(parse_qs(urlsplit(url).query))
        return copy.deepcopy(pages[len(calls) - 1])
    src = SeaSource(request_json=request, request_text=lambda *_: html(), **kw)
    return src, calls


def company():
    return CompanyCfg(name='Sea', ats='sea')


def test_atomic_inventory_maps_location_and_identity():
    src, calls = source([page(records(), 2)])
    rows = src.fetch(company())
    assert len(rows) == 2
    assert rows[0]['location'] == 'Singapore, Singapore'
    assert rows[0]['extra']['source_requisition_id'] == records()[0]['job_id']
    assert rows[0]['source_url'].endswith('/position/' + records()[0]['job_id'])
    assert rows[0]['date_posted'] == ''
    assert rows[0]['extra']['source_adapter'] == 'sea'
    assert src.last_health_diagnostics.complete
    assert src.request_count == 2
    assert calls[0]['external_entity_id'] == ['3']
    assert calls[0]['post_type'] == ['1']


def test_pagination_proves_exact_total():
    a, b = records()
    src, calls = source([page([a], 2), page([b], 2)], page_size=1)
    assert len(src.fetch(company())) == 2
    assert [c['offset'] for c in calls] == [['0'], ['1']]


@pytest.mark.parametrize('payload', [page([], 0), page(None, 0)])
def test_only_successful_explicit_zero_is_empty(payload):
    src, _ = source([payload])
    assert src.fetch(company()) == []
    assert src.last_health_diagnostics.complete


@pytest.mark.parametrize('payload', [[], {}, {'code': 0, 'data': {}},
    {'code': 1, 'data': {'job_list': [], 'total_count': 0}},
    {'code': False, 'data': {'job_list': [], 'total_count': 0}},
    page(None, 2), page([], 2), page(records(), 0), page(records(), True),
    page(records(), 10001), page([None], 1)])
def test_missing_bounds_and_inconsistent_responses_fail(payload):
    src, _ = source([payload])
    with pytest.raises(SourceSchemaError):
        src.fetch(company())
    assert not src.last_health_diagnostics.complete


@pytest.mark.parametrize('kind', ['duplicate', 'drift', 'omission', 'foreign_board',
                                  'bad_entity', 'wrong_city', 'id'])
def test_inventory_failures_publish_no_partial_result(kind):
    a, b = records()
    pages = [page([a], 2), page([b], 2)]
    if kind == 'duplicate': pages[1] = page([a], 2)
    elif kind == 'drift': pages[1]['data']['total_count'] = 3
    elif kind == 'omission': pages[1]['data']['job_list'] = None
    elif kind == 'foreign_board': a['post_type'] = 2
    elif kind == 'bad_entity': a['external_entity_id'] = 0
    elif kind == 'wrong_city': a['city_id'] = 999999
    else: a['job_id'] = '../bad'
    src, _ = source(pages, page_size=1)
    with pytest.raises(SourceSchemaError):
        src.fetch(company())
    assert not src.last_health_diagnostics.complete


def test_corporate_board_retains_every_entity_on_the_ignored_filter():
    # The server ignores external_entity_id, so one post_type=1 response spans
    # several entity values. All are Sea corporate postings and none may be
    # dropped; the entity is recorded as metadata only.
    rows = records(3)
    assert {r['external_entity_id'] for r in rows} == {1, 3, 4}
    assert {r['post_type'] for r in rows} == {1}
    src, _ = source([page(rows, 3)])
    collected = src.fetch(company())
    assert len(collected) == 3
    assert [r['extra']['sea_external_entity_id'] for r in collected] == [3, 4, 1]
    assert {r['extra']['source_requisition_id'] for r in collected} == {
        r['job_id'] for r in rows}
    assert src.last_health_diagnostics.complete
    assert src.last_health_diagnostics.retained_row_count == 3


@pytest.mark.parametrize('foreign', [2, 4])
def test_separate_shopee_and_monee_boards_are_never_collected(foreign):
    # post_type=2 is Shopee/SPX and post_type=4 is Monee/MariBank.
    rows = records(1)
    rows[0]['post_type'] = foreign
    src, _ = source([page(rows, 1)])
    with pytest.raises(SourceSchemaError):
        src.fetch(company())
    assert not src.last_health_diagnostics.complete


def test_bad_metadata_fails_without_guessing_locations():
    src = SeaSource(request_text=lambda *_: '<html>unavailable</html>')
    with pytest.raises(SourceSchemaError):
        src.fetch(company())


def test_transport_failure_is_bounded():
    def fail(*_):
        raise SourceFetchError('unavailable', status_code=403)
    src = SeaSource(request_text=fail)
    with pytest.raises(SourceFetchError): src.fetch(company())
    assert src.request_count == 1
    assert not src.last_health_diagnostics.complete


def test_registry_catalog_and_replay():
    from dataclasses import replace
    from backend.app.hosted.catalog import CompanyCatalog
    from watcher.sources.registry import build_direct_sources
    from watcher.collection_concurrency import direct_origin_key
    from watcher.collection_snapshot import collection_config_fingerprint
    from watcher.config.loader import load_watchlist
    cfg = load_watchlist()
    assert isinstance(build_direct_sources()['sea'], SeaSource)
    assert direct_origin_key('sea') == 'https://career.sea.com'
    assert CompanyCatalog.from_watcher_config(cfg).resolve('Sea Limited').coverage == 'direct'
    without = replace(cfg, companies=tuple(c for c in cfg.companies if c.name != 'Sea'))
    assert collection_config_fingerprint(cfg) != collection_config_fingerprint(without)
