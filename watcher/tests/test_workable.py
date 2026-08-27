"""Strict Workable cursor pagination tests."""

from __future__ import annotations

from typing import Any

import pytest

from watcher.config import CompanyCfg
from watcher.sources.base import SourceFetchError, SourceSchemaError
from watcher.sources.retry import DEFAULT_MAX_ATTEMPTS
from watcher.sources.workable import WorkableSource


COMPANY = CompanyCfg(name="Example", ats="workable", token="example")


def _job(shortcode: str, title: str | None = None) -> dict[str, Any]:
    return {
        "id": f"job-{shortcode}",
        "shortcode": shortcode,
        "title": title or f"Role {shortcode}",
        "location": {"city": "Boston", "region": "MA", "country": "US"},
    }


def _source(
    payloads: list[Any],
    *,
    max_pages: int = 100,
    **source_kwargs: Any,
):
    calls: list[dict[str, Any]] = []

    def request_json(url: str, payload: dict, source_name: str):
        assert url == WorkableSource.endpoint("example")
        assert source_name == "workable"
        calls.append(payload)
        if not payloads:
            raise AssertionError("unexpected Workable request")
        outcome = payloads.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return (
        WorkableSource(
            request_json=request_json,
            max_pages=max_pages,
            **source_kwargs,
        ),
        calls,
    )


def _rate_limited() -> SourceFetchError:
    return SourceFetchError(
        "workable POST failed: code=rate_limited endpoint=https://apply.workable.com/",
        error_code="rate_limited",
        status_code=429,
        retryable=True,
    )


def test_fetch_preserves_legacy_single_page_contract():
    source, calls = _source([{"total": 1, "results": [_job("ONE")] }])

    rows = source.fetch(COMPANY)

    assert calls == [{}]
    assert [row["extra"]["source_requisition_id"] for row in rows] == ["ONE"]
    assert rows[0]["source_url"] == "https://apply.workable.com/example/j/ONE/"
    assert source.last_health_diagnostics.complete is True
    assert source.request_attempts == 1
    assert source.retry_attempts == 0
    assert source.last_health_diagnostics.failed_request_count == 0
    assert source.last_health_diagnostics.reason_codes == ()
    assert source.last_health_diagnostics.degraded is False


def test_fetch_follows_cursor_until_stable_total_is_complete():
    source, calls = _source(
        [
            {
                "total": 3,
                "results": [_job("ONE"), _job("TWO")],
                "nextPage": "cursor-1",
            },
            {"total": 3, "results": [_job("THREE")]},
        ]
    )

    rows = source.fetch(COMPANY)

    assert calls == [{}, {"query": "", "token": "cursor-1"}]
    assert [row["extra"]["source_requisition_id"] for row in rows] == [
        "ONE",
        "TWO",
        "THREE",
    ]
    assert source.pages_requested == 2
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.incomplete is False


def test_fetch_accepts_exact_explicit_empty_board():
    source, calls = _source([{"total": 0, "results": []}])

    assert source.fetch(COMPANY) == []
    assert calls == [{}]
    assert source.last_health_diagnostics.complete is True


def test_mixed_malformed_records_across_pages_retain_valid_rows_and_degrade():
    source, _ = _source(
        [
            {
                "total": 3,
                "results": [_job("ONE"), {"title": "missing shortcode"}],
                "nextPage": "cursor-1",
            },
            {"total": 3, "results": [_job("THREE")]},
        ]
    )

    rows = source.fetch(COMPANY)

    assert [row["extra"]["source_requisition_id"] for row in rows] == [
        "ONE",
        "THREE",
    ]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.schema_error_row_count == 1
    assert diagnostics.degraded is True
    assert diagnostics.complete is False
    assert diagnostics.reason_codes == ("schema_invalid_records_skipped",)


@pytest.mark.parametrize(
    ("payloads", "message"),
    [
        (
            [{"total": 2, "results": [_job("ONE")]}],
            "ended before total",
        ),
        (
            [
                {
                    "total": 2,
                    "results": [_job("ONE")],
                    "nextPage": "cursor-1",
                },
                {"total": 3, "results": [_job("TWO")]},
            ],
            "total changed",
        ),
        (
            [
                {
                    "total": 3,
                    "results": [_job("ONE")],
                    "nextPage": "cursor-1",
                },
                {
                    "total": 3,
                    "results": [_job("TWO")],
                    "nextPage": "cursor-1",
                },
            ],
            "repeated cursor",
        ),
        (
            [
                {
                    "total": 2,
                    "results": [_job("ONE")],
                    "nextPage": "cursor-1",
                },
                {"total": 2, "results": [_job("ONE")]},
            ],
            "repeated pagination page",
        ),
        (
            [{"total": 0, "results": [], "nextPage": "cursor-1"}],
            "zero-result response",
        ),
        (
            [{"total": 1, "results": [_job("ONE")], "nextPage": 42}],
            "nextPage",
        ),
        (
            [{"total": 1, "results": [_job("ONE")], "nextPage": "extra"}],
            "cursor after total",
        ),
    ],
)
def test_incomplete_or_inconsistent_cursor_pagination_fails(payloads, message):
    source, _ = _source(payloads)

    with pytest.raises(SourceSchemaError, match=message):
        source.fetch(COMPANY)


def test_cursor_pagination_has_a_bounded_page_safeguard():
    source, _ = _source(
        [
            {
                "total": 2,
                "results": [_job("ONE")],
                "nextPage": "cursor-1",
            }
        ],
        max_pages=1,
    )

    with pytest.raises(SourceSchemaError, match="maximum page safeguard"):
        source.fetch(COMPANY)


def test_rate_limit_then_success_retries_the_same_page_and_reports_recovery():
    delays: list[float] = []
    source, calls = _source(
        [_rate_limited(), {"total": 1, "results": [_job("ONE")]}],
        sleeper=delays.append,
        jitter=lambda _low, _high: 0.0,
    )

    rows = source.fetch(COMPANY)

    assert calls == [{}, {}]
    assert [row["extra"]["source_requisition_id"] for row in rows] == ["ONE"]
    assert source.pages_requested == 1
    assert source.request_attempts == 2
    assert source.retry_attempts == 1
    assert delays == [1.0]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.failed_request_count == 1
    assert diagnostics.reason_codes == ("request_retry_recovered",)
    assert diagnostics.succeeded is True
    assert diagnostics.degraded is True
    assert diagnostics.complete is True
    assert diagnostics.incomplete is False
    assert diagnostics.truncated is False


def test_rate_limit_exhaustion_fails_without_partial_success_diagnostics():
    delays: list[float] = []
    source, calls = _source(
        [_rate_limited(), _rate_limited(), _rate_limited()],
        sleeper=delays.append,
        jitter=lambda _low, _high: 0.0,
    )

    with pytest.raises(SourceFetchError, match="rate_limited") as raised:
        source.fetch(COMPANY)

    assert calls == [{}, {}, {}]
    assert source.pages_requested == 1
    assert source.request_attempts == DEFAULT_MAX_ATTEMPTS
    assert source.retry_attempts == DEFAULT_MAX_ATTEMPTS - 1
    assert delays == [1.0, 3.0]
    assert raised.value.error_code == "rate_limited"
    assert raised.value.status_code == 429
    assert raised.value.attempt_count == DEFAULT_MAX_ATTEMPTS
    diagnostics = source.last_health_diagnostics
    assert diagnostics.succeeded is None
    assert diagnostics.retained_row_count == 0
    assert "request_retry_recovered" not in diagnostics.reason_codes
    assert diagnostics.complete is False


def test_later_page_rate_limit_retries_identical_cursor_without_row_loss():
    source, calls = _source(
        [
            {
                "total": 3,
                "results": [_job("ONE"), _job("TWO")],
                "nextPage": "cursor-1",
            },
            _rate_limited(),
            {"total": 3, "results": [_job("THREE")]},
        ],
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0.0,
    )

    rows = source.fetch(COMPANY)

    next_page = {"query": "", "token": "cursor-1"}
    assert calls == [{}, next_page, next_page]
    assert [row["extra"]["source_requisition_id"] for row in rows] == [
        "ONE",
        "TWO",
        "THREE",
    ]
    assert source.pages_requested == 2
    assert source.request_attempts == 3
    assert source.retry_attempts == 1
    diagnostics = source.last_health_diagnostics
    assert diagnostics.retained_row_count == 3
    assert diagnostics.failed_request_count == 1
    assert diagnostics.reason_codes == ("request_retry_recovered",)
    assert diagnostics.degraded is True
    assert diagnostics.complete is True
    assert diagnostics.incomplete is False
    assert diagnostics.truncated is False


def test_later_page_rate_limit_exhaustion_never_returns_earlier_rows():
    source, calls = _source(
        [
            {
                "total": 2,
                "results": [_job("ONE")],
                "nextPage": "cursor-1",
            },
            _rate_limited(),
            _rate_limited(),
            _rate_limited(),
        ],
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0.0,
    )

    with pytest.raises(SourceFetchError, match="rate_limited"):
        source.fetch(COMPANY)

    next_page = {"query": "", "token": "cursor-1"}
    assert calls == [{}, next_page, next_page, next_page]
    assert source.pages_requested == 2
    assert source.request_attempts == 4
    assert source.retry_attempts == 2
    diagnostics = source.last_health_diagnostics
    assert diagnostics.succeeded is None
    assert diagnostics.retained_row_count == 0
    assert "request_retry_recovered" not in diagnostics.reason_codes
    assert diagnostics.complete is False


def test_permanent_http_failure_is_not_retried():
    delays: list[float] = []
    source, calls = _source(
        [
            SourceFetchError(
                "workable POST failed: code=permanent_http_error",
                error_code="permanent_http_error",
                status_code=404,
                retryable=False,
            ),
            {"total": 1, "results": [_job("ONE")]},
        ],
        sleeper=delays.append,
        jitter=lambda _low, _high: 0.0,
    )

    with pytest.raises(SourceFetchError, match="permanent_http_error"):
        source.fetch(COMPANY)

    assert calls == [{}]
    assert source.request_attempts == 1
    assert source.retry_attempts == 0
    assert delays == []
    assert "request_retry_recovered" not in source.last_health_diagnostics.reason_codes


def test_schema_failure_does_not_enter_the_transport_retry_path():
    delays: list[float] = []
    source, calls = _source(
        [
            {"total": 1, "results": "not-a-list"},
            {"total": 1, "results": [_job("ONE")]},
        ],
        sleeper=delays.append,
        jitter=lambda _low, _high: 0.0,
    )

    with pytest.raises(SourceSchemaError, match="results to be a list"):
        source.fetch(COMPANY)

    assert calls == [{}]
    assert source.request_attempts == 1
    assert source.retry_attempts == 0
    assert delays == []
    assert "request_retry_recovered" not in source.last_health_diagnostics.reason_codes


def test_retry_budget_is_bounded_across_the_whole_cursor_crawl():
    source, calls = _source(
        [
            _rate_limited(),
            {
                "total": 4,
                "results": [_job("ONE")],
                "nextPage": "cursor-1",
            },
            _rate_limited(),
            {
                "total": 4,
                "results": [_job("TWO")],
                "nextPage": "cursor-2",
            },
            _rate_limited(),
        ],
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0.0,
        max_crawl_retries=2,
    )

    with pytest.raises(SourceFetchError, match="rate_limited"):
        source.fetch(COMPANY)

    assert calls == [
        {},
        {},
        {"query": "", "token": "cursor-1"},
        {"query": "", "token": "cursor-1"},
        {"query": "", "token": "cursor-2"},
    ]
    assert source.pages_requested == 3
    assert source.request_attempts == 5
    assert source.retry_attempts == 2
    assert source.last_health_diagnostics.succeeded is None


def test_retry_diagnostics_reset_before_the_next_fetch():
    source, calls = _source(
        [
            _rate_limited(),
            {"total": 1, "results": [_job("ONE")]},
            {"total": 1, "results": [_job("TWO")]},
        ],
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0.0,
    )

    source.fetch(COMPANY)
    rows = source.fetch(COMPANY)

    assert calls == [{}, {}, {}]
    assert [row["extra"]["source_requisition_id"] for row in rows] == ["TWO"]
    assert source.request_attempts == 1
    assert source.retry_attempts == 0
    diagnostics = source.last_health_diagnostics
    assert diagnostics.failed_request_count == 0
    assert diagnostics.reason_codes == ()
    assert diagnostics.degraded is False
    assert diagnostics.complete is True


def test_recovered_retry_is_not_reported_when_record_loss_remains():
    source, _ = _source(
        [
            _rate_limited(),
            {
                "total": 2,
                "results": [_job("ONE"), {"title": "missing shortcode"}],
            },
        ],
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0.0,
    )

    rows = source.fetch(COMPANY)

    assert [row["extra"]["source_requisition_id"] for row in rows] == ["ONE"]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.failed_request_count == 1
    assert diagnostics.reason_codes == ("schema_invalid_records_skipped",)
    assert diagnostics.degraded is True
    assert diagnostics.complete is False
    assert diagnostics.incomplete is False
    assert diagnostics.truncated is False
