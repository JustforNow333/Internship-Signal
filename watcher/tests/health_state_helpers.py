"""Shared attempt and state builders for the source-health test modules."""

from datetime import datetime, timezone

from watcher.source_health import (
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    SourceAttempt,
    calculate_next_state,
    direct_health_key,
    github_feed_health_key,
)


NOW = datetime(2026, 7, 16, 14, 30, tzinfo=timezone.utc)


def attempt(
    *,
    run_id="run-1",
    rows=1,
    succeeded=True,
    source_kind=SOURCE_KIND_DIRECT,
    company="Example Co",
    adapter="greenhouse",
    observed_at=NOW,
    attempted=True,
    error_kind=None,
    error_message=None,
    feed_label=None,
    unsupported_reason=None,
    **diagnostics,
):
    if source_kind == SOURCE_KIND_GITHUB_FEED:
        company = None
        adapter = "github_listings"
        feed_label = feed_label or "https://example.test/listings.json"
        key = github_feed_health_key(feed_label)
    else:
        key = direct_health_key(company, adapter)
    return SourceAttempt(
        health_key=key,
        run_id=run_id,
        observed_at=observed_at,
        source_kind=source_kind,
        company=company,
        adapter=adapter,
        attempted=attempted,
        succeeded=succeeded,
        rows_returned=rows,
        error_kind=error_kind,
        error_message=error_message,
        feed_label=feed_label,
        unsupported_reason=unsupported_reason,
        **diagnostics,
    )


def next_state(previous, **kwargs):
    return calculate_next_state(previous, attempt(**kwargs))


def diagnostic_attempt(*, rows=1, succeeded=True, **overrides):
    values = {
        "run_id": "diagnostic-run",
        "rows": rows,
        "succeeded": succeeded,
        "malformed_row_count": 0,
        "schema_error_row_count": 0,
        "duplicate_row_count": 0,
        "failed_request_count": 0,
        "incomplete": False,
        "truncated": False,
        "reason_codes": (),
        "degraded": False,
        "complete": True,
    }
    values.update(overrides)
    return attempt(**values)
