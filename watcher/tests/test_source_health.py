"""Per-attempt health state, transitions, and the total sanitizers.

Owned by :mod:`watcher.health.state` and :mod:`watcher.health.sanitize`, and
imported here through the :mod:`watcher.source_health` compatibility facade.
"""

import pytest

from watcher.source_health import (
    ERROR_FETCH,
    SOURCE_KIND_GITHUB_FEED,
    STATUS_DEGRADED,
    STATUS_FAILING,
    STATUS_HEALTHY,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_NOT_CONFIGURED,
    DIRECT_STATUS_UNKNOWN,
    calculate_next_state,
    github_feed_health_key,
    sanitize_error,
    sanitize_feed_label,
    transition_for,
)
from watcher.tests.health_state_helpers import (
    attempt,
    diagnostic_attempt,
    next_state,
)


def test_direct_diagnostic_states_are_per_attempt_and_listing_count_is_separate():
    listed = calculate_next_state(None, diagnostic_attempt(rows=2))
    empty = calculate_next_state(None, diagnostic_attempt(rows=0))
    degraded = calculate_next_state(
        None,
        diagnostic_attempt(
            rows=1,
            malformed_row_count=1,
            reason_codes=("malformed_records_skipped",),
            degraded=True,
            complete=False,
        ),
    )
    failed = calculate_next_state(
        None,
        diagnostic_attempt(
            rows=None,
            succeeded=False,
            failed_request_count=1,
            reason_codes=("fetch_failure",),
            complete=False,
        ),
    )
    not_configured = calculate_next_state(
        None,
        attempt(
            attempted=False,
            succeeded=None,
            rows=None,
            adapter="github_only",
            unsupported_reason="github_only",
        ),
    )

    assert listed.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert empty.status == DIRECT_STATUS_HEALTHY_EMPTY
    assert degraded.status == DIRECT_STATUS_DEGRADED
    assert failed.status == DIRECT_STATUS_FAILED
    assert not_configured.status == DIRECT_STATUS_NOT_CONFIGURED


def test_direct_success_without_sufficient_diagnostics_is_unknown():
    state = calculate_next_state(None, attempt(rows=4))
    assert state.status == DIRECT_STATUS_UNKNOWN


def test_duplicates_and_optional_enrichment_failure_do_not_force_degradation():
    duplicates = calculate_next_state(
        None,
        diagnostic_attempt(rows=2, duplicate_row_count=3),
    )
    optional_enrichment = calculate_next_state(
        None,
        diagnostic_attempt(
            rows=2,
            failed_request_count=1,
            reason_codes=("optional_enrichment_failed",),
        ),
    )
    material_enrichment = calculate_next_state(
        None,
        diagnostic_attempt(
            rows=2,
            failed_request_count=1,
            reason_codes=("material_enrichment_failed",),
            degraded=True,
            complete=False,
        ),
    )

    assert duplicates.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert optional_enrichment.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    assert material_enrichment.status == DIRECT_STATUS_DEGRADED


def test_feed_keys_and_labels_do_not_expose_query_strings():
    raw = "https://user:secret@example.test/listings.json?temporary_token=private#fragment"
    label = sanitize_feed_label(raw)
    key = github_feed_health_key(raw)
    assert label == "https://example.test/listings.json"
    assert "private" not in key
    assert "temporary_token" not in key
    assert "?" not in key


@pytest.mark.parametrize(
    "raw, expected",
    (
        ("https://example.test:99999/listings.json", "https://example.test/listings.json"),
        ("https://example.test:port/listings.json", "https://example.test/listings.json"),
        ("https://user:secret@example.test:99999/x?t=1", "https://example.test/x"),
        ("https://[::1]:70000/listings.json", "https://::1/listings.json"),
        ("http://[unterminated/listings.json?t=1", "http://[unterminated/listings.json"),
    ),
)
def test_feed_labels_sanitize_malformed_authorities_without_raising(raw, expected):
    assert sanitize_feed_label(raw) == expected
    assert "secret" not in sanitize_feed_label(raw)


def test_sanitize_error_survives_a_malformed_url_in_failure_text():
    message = sanitize_error("workday POST failed: https://tenant.test:99999/wday/cxs/jobs?q=1")
    assert message == "workday POST failed: https://tenant.test/wday/cxs/jobs"


def test_direct_success_and_zero_status_do_not_depend_on_history():
    healthy = calculate_next_state(None, diagnostic_attempt(rows=2))
    assert healthy.status == DIRECT_STATUS_HEALTHY_WITH_LISTINGS
    first_zero = calculate_next_state(None, diagnostic_attempt(rows=0))
    second_zero = calculate_next_state(
        first_zero, diagnostic_attempt(run_id="run-2", rows=0)
    )
    zero_after_nonzero = calculate_next_state(
        healthy, diagnostic_attempt(run_id="run-3", rows=0)
    )
    assert first_zero.status == DIRECT_STATUS_HEALTHY_EMPTY
    assert second_zero.status == DIRECT_STATUS_HEALTHY_EMPTY
    assert zero_after_nonzero.status == DIRECT_STATUS_HEALTHY_EMPTY


def test_direct_failure_is_immediate_and_nonzero_recovery():
    first = next_state(None, succeeded=False, rows=None, error_kind=ERROR_FETCH)
    second = next_state(first, run_id="run-2", succeeded=False, rows=None, error_kind=ERROR_FETCH)
    third = next_state(second, run_id="run-3", succeeded=False, rows=None, error_kind=ERROR_FETCH)
    recovered = calculate_next_state(
        third, diagnostic_attempt(run_id="run-4", rows=3)
    )
    assert [first.status, second.status, third.status, recovered.status] == [
        DIRECT_STATUS_FAILED,
        DIRECT_STATUS_FAILED,
        DIRECT_STATUS_FAILED,
        DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    ]
    transition = transition_for(third, recovered)
    assert transition.recovery is True


def test_failure_followed_by_zero_is_empty_recovery():
    failed = next_state(None, succeeded=False, rows=None, error_kind=ERROR_FETCH)
    responding = calculate_next_state(
        failed, diagnostic_attempt(run_id="run-2", rows=0)
    )
    transition = transition_for(failed, responding)
    assert responding.status == DIRECT_STATUS_HEALTHY_EMPTY
    assert transition.recovery is True


@pytest.mark.parametrize("adapter", ["bespoke", "github_only"])
def test_unsupported_does_not_accumulate_failures(adapter):
    unsupported_attempt = attempt(
        adapter=adapter,
        attempted=False,
        succeeded=None,
        rows=None,
        unsupported_reason=adapter,
    )
    first = calculate_next_state(None, unsupported_attempt)
    second = calculate_next_state(first, unsupported_attempt)
    assert second.status == DIRECT_STATUS_NOT_CONFIGURED
    assert second.total_attempts == 0
    assert second.consecutive_failures == 0


def test_github_zero_is_healthy_and_failure_threshold_and_recovery():
    healthy_zero = next_state(None, source_kind=SOURCE_KIND_GITHUB_FEED, rows=0)
    first = next_state(
        healthy_zero,
        run_id="run-2",
        source_kind=SOURCE_KIND_GITHUB_FEED,
        succeeded=False,
        rows=None,
        error_kind=ERROR_FETCH,
    )
    second = next_state(first, run_id="run-3", source_kind=SOURCE_KIND_GITHUB_FEED, succeeded=False, rows=None)
    third = next_state(second, run_id="run-4", source_kind=SOURCE_KIND_GITHUB_FEED, succeeded=False, rows=None)
    recovered = next_state(third, run_id="run-5", source_kind=SOURCE_KIND_GITHUB_FEED, rows=0)
    assert [healthy_zero.status, first.status, second.status, third.status, recovered.status] == [
        STATUS_HEALTHY,
        STATUS_DEGRADED,
        STATUS_DEGRADED,
        STATUS_FAILING,
        STATUS_HEALTHY,
    ]
    assert transition_for(third, recovered).recovery is True


def test_transitions_omit_initialization_and_unchanged_states():
    healthy = calculate_next_state(None, diagnostic_attempt(rows=1))
    failed = next_state(healthy, run_id="run-2", succeeded=False, rows=None)
    failed_again = next_state(failed, run_id="run-3", succeeded=False, rows=None)
    failing = next_state(failed_again, run_id="run-4", succeeded=False, rows=None)
    assert transition_for(None, healthy) is None
    assert transition_for(healthy, failed).to_status == DIRECT_STATUS_FAILED
    assert transition_for(failed, failed_again) is None
    assert transition_for(failed_again, failing) is None


def test_sanitizers_are_deterministic():
    assert sanitize_error("HTTP https://example.test/a?x=1") == "HTTP https://example.test/a"
    assert github_feed_health_key("https://example.test/a?x=1") == github_feed_health_key(
        "https://example.test/a?x=2"
    )
