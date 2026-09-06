"""Offline Bechtel contract tests; no live HTTP or production state.

The saved fixture is reduced from the reconciled 2026-09-05 public responses.
Large boards below repeat its shape with synthetic requisitions. In particular,
a duplicate replacing an omitted ID must fail despite raw rows == totalHits.
"""

import ast
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from watcher.config import CompanyCfg, WatcherConfig, load_watchlist
from watcher.sources.bechtel import BechtelSource, MAX_PAGES, MAX_REQUESTS, PAGE_SIZE
from watcher.sources.contracts import JsonHttpResponse, SourceFetchError, SourceSchemaError

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/bechtel_refine_search.json").read_text(encoding="utf-8"))


def company():
    return CompanyCfg(name="Bechtel Corporation", ats="bechtel", aliases=("Bechtel",))


def jobs(count):
    result = []
    for index in range(count):
        job = deepcopy(FIXTURE["populated"]["refineSearch"]["data"]["jobs"][index % 2])
        req = str(100000 + index)
        job.update(reqId=req, jobId=req, jobSeqNo=f"BCFBCKUS{req}EXTERNALENUS")
        job["applyUrl"] = (
            "https://career4.successfactors.com/career?company=Bechtel"
            f"&career_ns=job_application&career_job_req_id={req}"
        )
        result.append(job)
    return result


def page(records, total=None):
    return {"refineSearch": {"status": 200, "hits": len(records),
            "totalHits": len(records) if total is None else total,
            "data": {"jobs": deepcopy(records)}}}


def snapshot(records):
    if not records:
        return [deepcopy(FIXTURE["empty"])]
    return [page(records[i:i + PAGE_SIZE], len(records))
            for i in range(0, len(records), PAGE_SIZE)] + [page([], len(records))]


def source_for(responses, **kwargs):
    pending = deque(responses)
    calls = []

    def request(url, body, name):
        calls.append((url, deepcopy(body), name))
        assert pending, "adapter exceeded the planned offline request budget"
        response = pending.popleft()
        if isinstance(response, Exception):
            raise response
        return JsonHttpResponse(deepcopy(response), {"status": 200})

    return BechtelSource(request_json=request, **kwargs), calls


def assert_failed(source, *, match=None):
    with pytest.raises(SourceSchemaError, match=match):
        source.fetch(company())
    assert source.last_health_diagnostics.complete is False
    assert source.last_health_diagnostics.succeeded is False
    assert source.last_health_diagnostics.retained_row_count == 0


def test_actual_fixture_fields_and_listing_only_requests():
    records = FIXTURE["populated"]["refineSearch"]["data"]["jobs"]
    source, calls = source_for(snapshot(records) * 2)
    rows = source.fetch(company())
    assert len(rows) == 2
    assert rows[0]["title"] == "AI Solutions Engineer"
    assert rows[0]["description"] == "Requisition ID: 297512"
    assert rows[0]["date_posted"] == ""  # Index timestamp is not an employer date.
    assert rows[0]["source_url"] == "https://jobs.bechtel.com/us/en/job/BCFBCKUS297512EXTERNALENUS/AI-Solutions-Engineer"
    assert rows[0]["extra"]["source_requisition_id"] == "297512"
    assert rows[0]["extra"]["source_adapter"] == "bechtel"
    assert rows[0]["extra"]["source"] == "direct"
    assert rows[1]["description"] == records[1]["descriptionTeaser"]
    assert rows[1]["extra"]["locations"] == records[1]["multi_location"]
    assert "Washington, District of Columbia" in rows[1]["location"]
    assert rows[1]["source_url"].endswith("/Manager-State-Government-Affairs")
    assert {url for url, _, _ in calls} == {BechtelSource.endpoint()}
    assert {name for _, _, name in calls} == {"bechtel"}
    assert all(body == BechtelSource.request_body(body["from"]) for _, body, _ in calls)
    assert source.request_attempts == 4 and source.retry_attempts == 0
    assert source.last_health_diagnostics.complete
    assert not source.last_health_diagnostics.degraded


def test_500_plus_final_short_page_requires_two_clean_snapshots_and_terminal():
    records = jobs(938)
    source, calls = source_for(snapshot(records) * 2)
    rows = source.fetch(company())
    assert [body["from"] for _, body, _ in calls] == [0, 500, 938] * 2
    assert all(body["size"] == 500 for _, body, _ in calls)
    assert len(rows) == len({r["extra"]["source_requisition_id"] for r in rows}) == 938
    assert len({r["source_url"] for r in rows}) == 938
    assert [(r.total, r.raw_rows, r.unique_requisitions, r.requests, r.clean)
            for r in source.pass_reports] == [(938, 938, 938, 3, True)] * 2
    assert source.pass_reports[-1].matched_previous
    assert source.last_health_diagnostics.reason_codes == ()
    assert source.last_health_diagnostics.failed_request_count == 0


@pytest.mark.parametrize("count", [1, 499, 500, 501, 1000])
def test_page_boundaries_including_exact_multiples(count):
    source, calls = source_for(snapshot(jobs(count)) * 2)
    assert len(source.fetch(company())) == count
    expected = list(range(0, count, 500)) + [count]
    assert [body["from"] for _, body, _ in calls] == expected * 2


def test_failed_duplicate_omission_pass_then_two_clean_passes():
    records = jobs(938)
    bad = deepcopy(records)
    bad[500] = deepcopy(bad[499])
    failed = snapshot(bad)[:2]  # Reject before making an unnecessary terminal call.
    source, calls = source_for(failed + snapshot(records) * 2)
    rows = source.fetch(company())
    assert len(rows) == 938 and len(calls) == 8
    first = source.pass_reports[0]
    assert first.raw_rows == first.total == 938
    assert first.unique_requisitions == 937
    assert first.rejection_reason == "duplicate_requisition"
    assert not first.clean
    assert source.pass_reports[2].matched_previous
    assert source.last_health_diagnostics.complete
    assert not source.last_health_diagnostics.degraded
    assert source.last_health_diagnostics.duplicate_row_count == 0


def test_three_duplicate_passes_fail_without_union_or_partial_results():
    records = jobs(501)
    records[-1] = deepcopy(records[-2])
    source, calls = source_for(snapshot(records)[:2] * 3)
    assert_failed(source, match="three passes")
    assert len(calls) == 6
    assert len(source.pass_reports) == 3
    assert all(r.rejection_reason == "duplicate_requisition" for r in source.pass_reports)


def test_clean_failure_clean_are_not_consecutive():
    records = jobs(2)
    source, _ = source_for(snapshot(records) + [page([records[0]] * 2)] + snapshot(records))
    assert_failed(source, match="three passes")
    assert [r.clean for r in source.pass_reports] == [True, False, True]


def test_three_different_clean_sets_do_not_stabilize():
    a, b, c = jobs(3)
    source, _ = source_for(snapshot([a]) + snapshot([b]) + snapshot([c]))
    assert_failed(source, match="three passes")
    assert all(r.clean and not r.matched_previous for r in source.pass_reports)


def test_changed_set_then_two_matching_sets_returns_latest_rows_not_union():
    a, b = jobs(2)
    updated = deepcopy(b)
    updated["title"] = "Updated engineer title"
    source, _ = source_for(snapshot([a]) + snapshot([b]) + snapshot([updated]))
    rows = source.fetch(company())
    assert [r["extra"]["source_requisition_id"] for r in rows] == [b["reqId"]]
    assert rows[0]["title"] == "Updated engineer title"


def test_equal_sets_can_have_different_order():
    records = jobs(3)
    source, _ = source_for(snapshot(records) + snapshot(records[::-1]))
    assert len(source.fetch(company())) == 3
    assert source.pass_reports[-1].matched_previous


def test_total_drift_discards_pass_and_restarts_at_zero():
    records = jobs(501)
    bad = [page(records[:500], 501), page(records[500:], 502)]
    source, calls = source_for(bad + snapshot(records) * 2)
    assert len(source.fetch(company())) == 501
    assert source.pass_reports[0].rejection_reason == "total_changed"
    assert [body["from"] for _, body, _ in calls] == [0, 500, 0, 500, 501, 0, 500, 501]


@pytest.mark.parametrize("returned", [0, 1, 499])
def test_premature_empty_or_short_page_is_never_completion(returned):
    source, _ = source_for([page(jobs(returned), 938)] * 3)
    assert_failed(source)
    assert all(r.rejection_reason == "page_arithmetic" for r in source.pass_reports)


def test_repeated_page_is_rejected():
    first = page(jobs(500), 1000)
    source, _ = source_for([first, first] * 3)
    assert_failed(source)
    assert all(r.rejection_reason == "repeated_page" for r in source.pass_reports)


@pytest.mark.parametrize("terminal", [page([], 0), page(jobs(1), 2), page([], 3)])
def test_invalid_terminal_total_or_membership_rejects_the_whole_pass(terminal):
    source, _ = source_for([page(jobs(2)), terminal] * 3)
    assert_failed(source)
    assert len(source.pass_reports) == 3


def test_explicit_zero_requires_two_complete_valid_empty_responses():
    source, calls = source_for([FIXTURE["empty"]] * 2)
    assert source.fetch(company()) == []
    assert len(calls) == 2
    assert source.pass_reports[-1].matched_previous
    assert source.last_health_diagnostics.complete
    assert not source.last_health_diagnostics.degraded


@pytest.mark.parametrize("payload", [None, [], "{", "<html>no jobs</html>", {},
    {"refineSearch": {}}, {"refineSearch": {"status": 200, "hits": 0, "totalHits": 0, "data": {}}}])
def test_malformed_or_zero_looking_missing_structure_fails_immediately(payload):
    source, calls = source_for([payload])
    assert_failed(source)
    assert len(calls) == 1


@pytest.mark.parametrize("field,value", [
    ("totalHits", None), ("totalHits", -1), ("totalHits", True), ("totalHits", "0"),
    ("totalHits", 0.0), ("hits", True), ("hits", None), ("hits", 1),
    ("status", "200"), ("status", 500), ("status", True), ("data", []),
])
def test_invalid_counts_status_and_data_never_mean_empty(field, value):
    payload = deepcopy(FIXTURE["empty"])
    payload["refineSearch"][field] = value
    source, _ = source_for([payload])
    assert_failed(source)


@pytest.mark.parametrize("value", [None, {}, "", 0])
def test_data_jobs_must_be_present_and_a_list(value):
    payload = deepcopy(FIXTURE["empty"])
    payload["refineSearch"]["data"]["jobs"] = value
    source, _ = source_for([payload])
    assert_failed(source, match="data.jobs")


@pytest.mark.parametrize("update", [
    {"reqId": None}, {"reqId": True}, {"reqId": "001"}, {"reqId": 1.0},
    {"jobId": "222222"}, {"jobSeqNo": "BCFBCKUS222222EXTERNALENUS"},
    {"jobSeqNo": "BCFBCKUS100000EXTERNAL ESES"}, {"locale": "es_ES"},
    {"title": ""}, {"title": "\ud800"}, {"title": 4}, {"multi_location": [None]},
    {"multi_location_array": ["City"]}, {"ml_job_parser": []},
])
def test_bad_record_or_conflicting_identity_is_a_schema_failure(update):
    record = jobs(1)[0]
    record.update(update)
    source, _ = source_for([page([record])])
    assert_failed(source)


@pytest.mark.parametrize("url", [
    "https://career4.successfactors.com/career?company=Bechtel&career_ns=job_application&career_job_req_id=999999",
    "https://career4.successfactors.com/career?company=Other&career_ns=job_application&career_job_req_id=100000",
    "https://career4.successfactors.com/career?company=Bechtel&career_ns=job_application&career_job_req_id=100000&career_job_req_id=100000",
    "https://career4.successfactors.com:bad/career", "https://[invalid",
    "https://untrusted.example/career", "http://career4.successfactors.com/career", None,
])
def test_application_identity_and_safe_host_are_required_when_present(url):
    record = jobs(1)[0]
    record["applyUrl"] = url
    source, _ = source_for([page([record])])
    assert_failed(source, match="application URL")


def test_optional_identity_fields_can_be_absent_but_req_id_is_required():
    record = jobs(1)[0]
    del record["jobId"], record["applyUrl"]
    source, _ = source_for(snapshot([record]) * 2)
    assert len(source.fetch(company())) == 1


@pytest.mark.parametrize("record", [None, "job", 1, {}])
def test_mixed_malformed_records_are_not_silently_dropped(record):
    source, _ = source_for([page([jobs(1)[0], record])])
    assert_failed(source)


def test_duplicate_identity_with_a_changed_title_is_still_rejected():
    a = jobs(1)[0]
    b = deepcopy(a)
    b["title"] = "Different route title"
    source, _ = source_for([page([a, b])] * 3)
    assert_failed(source)


def test_total_cannot_force_an_unbounded_number_of_requests():
    source, calls = source_for([page(jobs(500), 10001)])
    assert_failed(source, match="page safety limit")
    assert len(calls) == 1


def test_terminal_request_counts_toward_page_budget():
    source, calls = source_for([page(jobs(501)[:500], 501)], max_pages=2)
    assert_failed(source, match="page safety limit")
    assert len(calls) == 1


def test_overall_request_budget_is_enforced_before_http():
    source, calls = source_for(snapshot(jobs(501)) * 2, max_requests=5)
    assert_failed(source, match="request safety limit")
    assert len(calls) == 5


@pytest.mark.parametrize("kwargs", [
    {"max_pages": 0}, {"max_pages": True}, {"max_pages": MAX_PAGES + 1},
    {"max_requests": 0}, {"max_requests": 1.0}, {"max_requests": MAX_REQUESTS + 1},
])
def test_constructor_bounds_cannot_be_disabled(kwargs):
    with pytest.raises(ValueError):
        BechtelSource(**kwargs)


@pytest.mark.parametrize("code", ["invalid_json", "timeout", "http_403", "http_429"])
def test_request_errors_abort_without_retries_or_partial_success(code):
    error = SourceFetchError("request failed", error_code=code, retryable=True)
    source, calls = source_for([page(jobs(501)[:500], 501), error])
    with pytest.raises(SourceFetchError):
        source.fetch(company())
    assert len(calls) == 2
    assert source.pass_reports[0].rejection_reason == "request_failure"
    assert source.last_health_diagnostics.failed_request_count == 1
    assert not source.last_health_diagnostics.complete


def test_subsequent_fetch_does_not_reuse_old_success_or_pass_state():
    source, _ = source_for(snapshot(jobs(1)) * 2 + [{}])
    source.fetch(company())
    assert_failed(source)
    assert source.request_count == 1 and len(source.pass_reports) == 1


def test_normal_transport_is_shared_post_json_and_has_a_response_bound(monkeypatch):
    import watcher.sources.bechtel as module
    calls = []

    def post(url, body, name, **kwargs):
        calls.append((url, body, name, kwargs))
        return JsonHttpResponse(FIXTURE["empty"], {"status": 200})

    monkeypatch.setattr(module, "post_json_response", post)
    assert BechtelSource().fetch(company()) == []
    assert len(calls) == 2
    assert all(c[3] == {"max_response_bytes": 16 * 1024 * 1024} for c in calls)


def test_registry_config_alias_catalog_coverage_and_fingerprint_integration():
    from app.hosted.catalog import CompanyCatalog
    from watcher.collection_concurrency import direct_origin_key
    from watcher.collection_snapshot import collection_config_fingerprint
    from watcher.company_matching import company_matches
    from watcher.health.coverage import build_coverage_audit
    from watcher.health.models import COVERAGE_AUDIT_DIRECT_UNVERIFIED
    from watcher.sources.registry import DIRECT_ATS, build_direct_sources
    import watcher.sources as sources

    configured = next(c for c in load_watchlist().companies if c.name == company().name)
    assert configured.ats == "bechtel" and configured.module == ""
    assert "bechtel" in DIRECT_ATS
    assert isinstance(build_direct_sources()["bechtel"], BechtelSource)
    assert sources.BechtelSource is BechtelSource
    assert "BechtelSource" in sources.__all__
    assert direct_origin_key("bechtel") == "https://jobs.bechtel.com"
    assert company_matches("Bechtel", configured)
    assert company_matches("Bechtel Corporation", configured)
    assert not company_matches("Other Corporation", configured)
    catalog = CompanyCatalog.from_watcher_config()
    entry = next(c for c in catalog.companies if c.name == configured.name)
    assert entry.coverage == "direct" and entry.selectable
    cfg = WatcherConfig(companies=(configured,))
    old = WatcherConfig(companies=(replace(configured, ats="bespoke", module="bechtel_corporation"),))
    assert collection_config_fingerprint(cfg) != collection_config_fingerprint(old)
    assert collection_config_fingerprint(cfg) == collection_config_fingerprint(replace(cfg, min_score=50))
    report = build_coverage_audit(cfg, {}, state_database_present=False)
    assert report.companies[0].state == COVERAGE_AUDIT_DIRECT_UNVERIFIED


def test_listing_rows_round_trip_through_existing_snapshot_without_http(tmp_path):
    from watcher.collection import collect_batch
    from watcher.collection_snapshot import (
        load_collection_snapshot, save_collection_snapshot,
    )
    source, calls = source_for(snapshot(jobs(2)) * 2)
    cfg = WatcherConfig(companies=(company(),))
    batch = collect_batch(
        cfg, direct_sources={"bechtel": source},
        captured_at=datetime(2026, 9, 5, tzinfo=timezone.utc),
    )
    rows = batch.mutable_rows()
    assert len(rows) == 2 and not batch.errors
    assert len(batch.source_attempts) == 1
    assert batch.source_attempts[0].complete
    assert not batch.source_attempts[0].degraded
    path = tmp_path / "bechtel.json.gz"
    save_collection_snapshot(batch, path)
    loaded = load_collection_snapshot(path)
    assert loaded.mutable_rows() == rows
    assert loaded.source_attempts[0].complete
    assert len(calls) == 4


def test_failed_source_is_not_promoted_to_complete_by_collection():
    from watcher.collection import collect_batch

    source, _ = source_for([{}])
    batch = collect_batch(WatcherConfig(companies=(company(),)), direct_sources={"bechtel": source})
    assert not batch.rows and batch.errors
    assert len(batch.source_attempts) == 1
    assert not batch.source_attempts[0].succeeded
    assert not batch.source_attempts[0].complete


def test_provider_uses_canonical_owners_not_generic_phenom_or_other_ats():
    tree = ast.parse((ROOT / "watcher/sources/bechtel.py").read_text(encoding="utf-8"))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert {"watcher.sources.contracts", "watcher.sources.diagnostics",
            "watcher.sources.parsing", "watcher.sources.rows", "watcher.sources.transport"} <= imports
    assert not {"watcher.sources.base", "watcher.sources.direct", "watcher.sources.successfactors",
                "watcher.sources.phenom"} & imports
