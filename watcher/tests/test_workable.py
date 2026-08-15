"""Strict Workable cursor pagination tests."""

from __future__ import annotations

from typing import Any

import pytest

from watcher.config import CompanyCfg
from watcher.sources.base import SourceSchemaError
from watcher.sources.workable import WorkableSource


COMPANY = CompanyCfg(name="Example", ats="workable", token="example")


def _job(shortcode: str, title: str | None = None) -> dict[str, Any]:
    return {
        "id": f"job-{shortcode}",
        "shortcode": shortcode,
        "title": title or f"Role {shortcode}",
        "location": {"city": "Boston", "region": "MA", "country": "US"},
    }


def _source(payloads: list[dict[str, Any]], *, max_pages: int = 100):
    calls: list[dict[str, Any]] = []

    def request_json(url: str, payload: dict, source_name: str):
        assert url == WorkableSource.endpoint("example")
        assert source_name == "workable"
        calls.append(payload)
        if not payloads:
            raise AssertionError("unexpected Workable request")
        return payloads.pop(0)

    return (
        WorkableSource(request_json=request_json, max_pages=max_pages),
        calls,
    )


def test_fetch_preserves_legacy_single_page_contract():
    source, calls = _source([{"total": 1, "results": [_job("ONE")] }])

    rows = source.fetch(COMPANY)

    assert calls == [{}]
    assert [row["extra"]["source_requisition_id"] for row in rows] == ["ONE"]
    assert rows[0]["source_url"] == "https://apply.workable.com/example/j/ONE/"
    assert source.last_health_diagnostics.complete is True


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
