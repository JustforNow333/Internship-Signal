import json
from email.message import Message
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watcher.config import CompanyCfg, WatcherConfig
from watcher.run import CollectionStats, _default_direct_sources, collect_rows
from watcher.sources import (
    AshbySource,
    GreenhouseSource,
    LeverSource,
    OracleHcmSource,
    SmartRecruitersSource,
    SourceFetchError,
    SourceSchemaError,
    WorkableSource,
    WorkdaySource,
)
from watcher.sources.base import DirectSourceDiagnostics, get_json_response


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def company() -> CompanyCfg:
    return CompanyCfg(
        name="JPMorgan Chase",
        ats="oracle_hcm",
        oracle_hcm_host="jpmc.fa.oraclecloud.com",
        oracle_hcm_site="CX_1001",
        source_url=(
            "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
            "CX_1001/jobs"
        ),
    )


def test_valid_one_page_response_maps_canonical_fields_and_stable_identity():
    rows = OracleHcmSource().parse(fixture("oracle_hcm_one_page.json"), company())

    assert len(rows) == 2
    first = rows[0]
    assert first["company"] == "JPMorgan Chase"
    assert first["title"] == "2027 Software Engineer Program - Summer Internship"
    assert first["location"] == (
        "New York, NY, United States; Chicago, IL, United States; "
        "Dallas, TX, United States"
    )
    assert first["description"] == "Build reliable software."
    assert first["requirements"] == "Currently enrolled in a degree program."
    assert first["date_posted"] == "2026-07-15"
    assert first["deadline"] == "2026-10-01"
    assert first["internship_type"] == "Intern; Full time"
    assert first["remote_status"] == "Hybrid"
    assert first["source_url"] == (
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
        "CX_1001/job/210773759"
    )
    assert first["extra"] == {
        "source": "direct",
        "source_adapter": "oracle_hcm",
        "source_id": "210773759",
        "source_requisition_id": "210773759",
        "source_system": "oracle_hcm",
        "source_scope": "jpmc.fa.oraclecloud.com:CX_1001",
        "oracle_hcm_host": "jpmc.fa.oraclecloud.com",
        "oracle_hcm_site": "CX_1001",
        "active": True,
    }


def test_missing_optional_fields_do_not_reject_a_valid_posting():
    rows = OracleHcmSource().parse(fixture("oracle_hcm_one_page.json"), company())

    assert rows[1]["title"] == "Operations Analyst"
    assert rows[1]["location"] == ""
    assert rows[1]["description"] == ""
    assert rows[1]["requirements"] == ""
    assert rows[1]["date_posted"] == ""
    assert rows[1]["deadline"] == ""


def test_multi_page_fetch_collects_unique_postings_and_advances_offsets():
    payloads = [fixture("oracle_hcm_page_1.json"), fixture("oracle_hcm_page_2.json")]
    urls = []

    def request_json(url: str, source_name: str):
        urls.append(url)
        assert source_name == "oracle_hcm"
        return payloads[len(urls) - 1]

    source = OracleHcmSource(request_json=request_json, page_size=2)
    rows = source.fetch(company())

    assert [row["extra"]["source_requisition_id"] for row in rows] == [
        "ORACLE-1",
        "ORACLE-2",
        "ORACLE-3",
    ]
    finders = [parse_qs(urlsplit(url).query)["finder"][0] for url in urls]
    assert finders == [
        "findReqs;siteNumber=CX_1001,limit=2,offset=0",
        "findReqs;siteNumber=CX_1001,limit=2,offset=2",
    ]
    assert source.last_diagnostics.pages_requested == 2
    assert source.last_diagnostics.raw_postings_seen == 4
    assert source.last_diagnostics.duplicate_postings_skipped == 1
    health = source.last_health_diagnostics
    assert health.duplicate_row_count == 1
    assert health.degraded is False
    assert health.complete is True


def test_non_advancing_pagination_raises_schema_error():
    first = fixture("oracle_hcm_page_1.json")
    repeated_offset = fixture("oracle_hcm_page_2.json")
    repeated_offset["items"][0]["Offset"] = 0
    payloads = iter((first, repeated_offset))
    source = OracleHcmSource(request_json=lambda *_: next(payloads), page_size=2)

    with pytest.raises(SourceSchemaError, match="offset"):
        source.fetch(company())


def test_valid_rows_survive_a_malformed_record_on_another_page(caplog):
    first = {
        "items": [
            {
                "Offset": 0,
                "Limit": 1,
                "TotalJobsCount": 2,
                "requisitionList": [{"Id": "GOOD-1", "Title": "Valid Internship"}],
            }
        ]
    }
    second = {
        "items": [
            {
                "Offset": 1,
                "Limit": 1,
                "TotalJobsCount": 2,
                "requisitionList": [{"Id": "BROKEN-2"}],
            }
        ]
    }
    payloads = iter((first, second))
    source = OracleHcmSource(
        request_json=lambda *_: next(payloads),
        page_size=1,
    )

    rows = source.fetch(company())

    assert [row["extra"]["source_requisition_id"] for row in rows] == ["GOOD-1"]
    assert "Skipped 1 malformed oracle_hcm record" in caplog.text
    assert source.last_diagnostics.schema_error_postings_skipped == 1
    health = source.last_health_diagnostics
    assert health.schema_error_row_count == 1
    assert health.malformed_row_count == 0
    assert health.reason_codes == ("schema_invalid_records_skipped",)
    assert health.incomplete is True
    assert health.degraded is True
    assert health.complete is False


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"items": []},
        {"items": [{"Offset": 0, "Limit": 200, "TotalJobsCount": 0}]},
        {"items": [{"Offset": 0, "Limit": 200, "TotalJobsCount": "0", "requisitionList": []}]},
    ],
)
def test_malformed_or_changed_schema_raises(payload):
    with pytest.raises(SourceSchemaError):
        OracleHcmSource().parse(payload, company())


def test_valid_zero_result_response_is_empty():
    source = OracleHcmSource(request_json=lambda *_: fixture("oracle_hcm_zero.json"))

    assert source.fetch(company()) == []
    assert source.last_diagnostics.pages_requested == 1


def test_valid_empty_oracle_board_is_recorded_as_successful_source_health():
    source = OracleHcmSource(request_json=lambda *_: fixture("oracle_hcm_zero.json"))
    stats = CollectionStats()

    rows, errors = collect_rows(
        WatcherConfig(companies=(company(),)),
        direct_sources={"oracle_hcm": source},
        stats=stats,
    )

    assert rows == []
    assert errors == []
    assert len(stats.source_attempts) == 1
    assert stats.source_attempts[0].attempted is True
    assert stats.source_attempts[0].succeeded is True
    assert stats.source_attempts[0].rows_returned == 0


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_http_failures_use_bounded_retries(status):
    calls = 0
    delays = []

    def request_json(*_):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise SourceFetchError(
                "transient",
                status_code=status,
                retryable=True,
                response_metadata={"retry_after_seconds": 0},
            )
        return fixture("oracle_hcm_zero.json")

    source = OracleHcmSource(
        request_json=request_json,
        sleeper=delays.append,
        jitter=lambda _low, _high: 0,
    )

    assert source.fetch(company()) == []
    assert calls == 3
    assert delays == [1.0, 3.0]
    assert source.last_diagnostics.request_attempts == 3
    assert source.last_diagnostics.retry_attempts == 2
    # A recovered retry is a degraded but complete collection, and Oracle -- not
    # the collection layer -- is what says so.
    health = source.last_health_diagnostics
    assert isinstance(health, DirectSourceDiagnostics)
    assert health.failed_request_count == 2
    assert health.reason_codes == ("request_retry_recovered",)
    assert health.degraded is True
    assert health.incomplete is False
    assert health.complete is True


def test_permanent_4xx_fails_without_retrying():
    calls = 0

    def request_json(*_):
        nonlocal calls
        calls += 1
        raise SourceFetchError("not found", status_code=404, retryable=False)

    source = OracleHcmSource(request_json=request_json, sleeper=lambda _: None)

    with pytest.raises(SourceFetchError) as raised:
        source.fetch(company())
    assert calls == 1
    assert raised.value.attempt_count == 1


def test_maximum_page_guard_raises_instead_of_truncating():
    payload = fixture("oracle_hcm_page_1.json")
    source = OracleHcmSource(
        request_json=lambda *_: payload,
        page_size=2,
        max_pages=1,
    )

    with pytest.raises(SourceSchemaError, match="maximum page"):
        source.fetch(company())


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, content_type: str = "application/json"):
        self._body = body
        self.status = status
        self.code = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def read(self, size=-1):
        return self._body if size < 0 else self._body[:size]

    def geturl(self):
        return "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/test"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_malformed_json_is_a_fetch_failure_not_an_empty_board():
    response = FakeResponse(b"not-json")

    with pytest.raises(SourceFetchError, match="non-JSON") as raised:
        get_json_response(
            "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/test",
            "oracle_hcm",
            opener=lambda *_args, **_kwargs: response,
        )

    assert raised.value.error_code == "json_decode_failure"


def test_access_challenge_is_a_source_failure():
    response = FakeResponse(
        b"<html><body>Security check: verify you are human</body></html>",
        status=403,
        content_type="text/html",
    )

    with pytest.raises(SourceFetchError) as raised:
        get_json_response(
            "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/test",
            "oracle_hcm",
            opener=lambda *_args, **_kwargs: response,
        )

    assert raised.value.error_code == "html_challenge"
    assert raised.value.status_code == 403


def test_default_dispatch_registers_oracle_without_changing_existing_adapters():
    sources = _default_direct_sources()

    assert isinstance(sources["ashby"], AshbySource)
    assert isinstance(sources["greenhouse"], GreenhouseSource)
    assert isinstance(sources["lever"], LeverSource)
    assert isinstance(sources["oracle_hcm"], OracleHcmSource)
    assert isinstance(sources["smartrecruiters"], SmartRecruitersSource)
    assert isinstance(sources["workable"], WorkableSource)
    assert isinstance(sources["workday"], WorkdaySource)
