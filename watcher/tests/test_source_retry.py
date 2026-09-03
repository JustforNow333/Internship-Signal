from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from watcher.sources.base import SourceFetchError
from watcher.sources.retry import (
    DEFAULT_MAX_ATTEMPTS,
    MAX_RETRY_AFTER_SECONDS,
    RequestRetrier,
    RetryPolicy,
    http_retry_delay,
    retry_delay,
)

ROOT = Path(__file__).resolve().parents[2]


class Recorder:
    """A request whose per-attempt outcome is scripted."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        outcome = self._outcomes[min(self.calls, len(self._outcomes)) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def transient(message: str = "temporary") -> SourceFetchError:
    return SourceFetchError(message, retryable=True)


def permanent(message: str = "forbidden") -> SourceFetchError:
    return SourceFetchError(message, retryable=False)


def retrier(
    *,
    delays: list[float] | None = None,
    jitter_calls: list[tuple[float, float]] | None = None,
    jitter_value: float = 0.0,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_crawl_retries: int | None = None,
) -> RequestRetrier:
    def jitter(low: float, high: float) -> float:
        if jitter_calls is not None:
            jitter_calls.append((low, high))
        return jitter_value

    return RequestRetrier(
        policy=RetryPolicy(
            max_attempts=max_attempts,
            max_crawl_retries=max_crawl_retries,
        ),
        sleeper=(delays if delays is not None else []).append,
        jitter=jitter,
    )


def test_immediate_success_makes_one_attempt_and_never_sleeps():
    delays: list[float] = []
    request = Recorder("payload")
    runner = retrier(delays=delays)

    assert runner.run(request) == "payload"
    assert request.calls == 1
    assert runner.request_attempts == 1
    assert runner.retry_attempts == 0
    assert delays == []


def test_a_retryable_failure_is_retried_and_the_recovered_value_returned():
    delays: list[float] = []
    request = Recorder(transient(), "payload")
    runner = retrier(delays=delays)

    assert runner.run(request) == "payload"
    assert request.calls == 2
    assert runner.request_attempts == 2
    assert runner.retry_attempts == 1
    assert delays == [1.0]


def test_exhausted_retries_raise_the_last_failure_with_attempt_metadata():
    delays: list[float] = []
    last = transient("third")
    request = Recorder(transient("first"), transient("second"), last)
    runner = retrier(delays=delays)

    with pytest.raises(SourceFetchError) as raised:
        runner.run(request)

    assert raised.value is last
    assert request.calls == DEFAULT_MAX_ATTEMPTS
    assert runner.request_attempts == DEFAULT_MAX_ATTEMPTS
    # The attempt that exhausts the bound is not counted as a retry.
    assert runner.retry_attempts == DEFAULT_MAX_ATTEMPTS - 1
    assert delays == [1.0, 3.0]
    assert raised.value.attempt_count == DEFAULT_MAX_ATTEMPTS
    assert raised.value.response_metadata["attempt"] == DEFAULT_MAX_ATTEMPTS
    assert raised.value.response_metadata["max_attempts"] == DEFAULT_MAX_ATTEMPTS


def test_a_non_retryable_failure_is_raised_without_a_retry():
    delays: list[float] = []
    failure = permanent()
    request = Recorder(failure, "payload")
    runner = retrier(delays=delays)

    with pytest.raises(SourceFetchError) as raised:
        runner.run(request)

    assert raised.value is failure
    assert request.calls == 1
    assert runner.request_attempts == 1
    assert runner.retry_attempts == 0
    assert delays == []
    assert raised.value.attempt_count == 1
    assert raised.value.response_metadata == {
        "attempt": 1,
        "max_attempts": DEFAULT_MAX_ATTEMPTS,
    }


def test_only_source_fetch_errors_are_retried():
    delays: list[float] = []
    request = Recorder(ValueError("not a fetch failure"), "payload")
    runner = retrier(delays=delays)

    with pytest.raises(ValueError):
        runner.run(request)

    assert request.calls == 1
    assert runner.request_attempts == 1
    assert runner.retry_attempts == 0
    assert delays == []


def test_backoff_asks_the_injected_jitter_for_one_bounded_second_each_retry():
    delays: list[float] = []
    jitter_calls: list[tuple[float, float]] = []
    request = Recorder(transient(), transient(), "payload")
    runner = retrier(delays=delays, jitter_calls=jitter_calls, jitter_value=0.5)

    assert runner.run(request) == "payload"
    assert jitter_calls == [(0.0, 1.0), (0.0, 1.0)]
    assert delays == [1.5, 3.5]


@pytest.mark.parametrize(
    ("attempt", "jitter_value", "expected"),
    [
        (1, 0.0, 1.0),
        (1, 1.0, 2.0),
        (2, 0.0, 3.0),
        (2, 1.0, 4.0),
        (3, 1.0, 4.0),
        # Negative jitter never shortens a delay, and no jitter can exceed
        # the five-second cap.
        (1, -5.0, 1.0),
        (2, 99.0, 5.0),
    ],
)
def test_retry_delay_is_bounded_in_both_directions(attempt, jitter_value, expected):
    assert retry_delay(attempt, lambda _low, _high: jitter_value) == expected


def test_a_single_attempt_policy_never_retries():
    delays: list[float] = []
    failure = transient()
    request = Recorder(failure, "payload")
    runner = retrier(delays=delays, max_attempts=1)

    with pytest.raises(SourceFetchError) as raised:
        runner.run(request)

    assert raised.value is failure
    assert request.calls == 1
    assert runner.request_attempts == 1
    assert runner.retry_attempts == 0
    assert delays == []


def test_a_crawl_budget_is_spent_across_requests_and_then_stops_retrying():
    delays: list[float] = []
    runner = retrier(delays=delays, max_crawl_retries=2)

    for _ in range(2):
        assert runner.run(Recorder(transient(), "payload")) == "payload"

    assert runner.retry_attempts == 2
    assert runner.request_attempts == 4
    assert delays == [1.0, 1.0]

    exhausting = Recorder(transient(), "payload")
    with pytest.raises(SourceFetchError):
        runner.run(exhausting)

    # The budget is gone, so the third request fails on its first attempt.
    assert exhausting.calls == 1
    assert runner.retry_attempts == 2
    assert runner.request_attempts == 5
    assert delays == [1.0, 1.0]


def test_a_zero_crawl_budget_disables_retries_entirely():
    delays: list[float] = []
    request = Recorder(transient(), "payload")
    runner = retrier(delays=delays, max_crawl_retries=0)

    with pytest.raises(SourceFetchError):
        runner.run(request)

    assert request.calls == 1
    assert runner.retry_attempts == 0
    assert delays == []


def test_reset_clears_the_counters_and_refunds_the_crawl_budget():
    delays: list[float] = []
    runner = retrier(delays=delays, max_crawl_retries=1)

    assert runner.run(Recorder(transient(), "payload")) == "payload"
    assert (runner.request_attempts, runner.retry_attempts) == (2, 1)

    runner.reset()

    assert (runner.request_attempts, runner.retry_attempts) == (0, 0)
    assert runner.run(Recorder(transient(), "payload")) == "payload"
    assert (runner.request_attempts, runner.retry_attempts) == (2, 1)


def test_retriers_hold_no_shared_state():
    first = retrier()
    second = retrier()

    assert first.run(Recorder(transient(), "payload")) == "payload"

    assert first.retry_attempts == 1
    assert second.request_attempts == 0
    assert second.retry_attempts == 0


@pytest.mark.parametrize("max_attempts", [0, -1, DEFAULT_MAX_ATTEMPTS + 1])
def test_out_of_range_attempt_bounds_are_rejected(max_attempts):
    with pytest.raises(ValueError, match="max_attempts must be between"):
        RetryPolicy(max_attempts=max_attempts)


def test_a_negative_crawl_budget_is_rejected():
    with pytest.raises(ValueError, match="max_crawl_retries"):
        RetryPolicy(max_crawl_retries=-1)


def test_the_policy_is_immutable():
    policy = RetryPolicy()

    with pytest.raises(FrozenInstanceError):
        policy.max_attempts = 1


# --- Retry-After-aware delay: the neutral owner of a formula three adapters
# --- share (Workday, Oracle HCM, TalentBrew). Values pinned here are the ones
# --- those adapters produced when the helper lived in workday.py.


@pytest.mark.parametrize(
    ("failed_attempt", "jitter_value", "retry_after", "expected"),
    [
        # First failure: 1s base plus jitter clamped to [0, 1].
        (0, 0.0, None, 1.0),
        (1, 0.0, None, 1.0),
        (1, 0.25, None, 1.25),
        (1, 1.0, None, 2.0),
        (1, 3.0, None, 2.0),
        (1, -1.0, None, 1.0),
        # Later failures: 3s base plus jitter clamped to [0, 2].
        (2, 0.0, None, 3.0),
        (2, 1.5, None, 4.5),
        (2, 2.0, None, 5.0),
        (2, 3.0, None, 5.0),
        (5, 0.0, None, 3.0),
        # Retry-After raises the wait but never past the ceiling.
        (1, 0.0, 2.0, 2.0),
        (1, 0.0, 7.5, 7.5),
        (1, 0.0, 10.0, 10.0),
        (1, 0.0, 99.0, 10.0),
        (1, 0.0, -5.0, 1.0),
        (2, 2.0, 1.0, 5.0),
    ],
)
def test_http_retry_delay_is_bounded_and_honors_retry_after(
    failed_attempt, jitter_value, retry_after, expected
):
    assert (
        http_retry_delay(
            failed_attempt,
            jitter=lambda _low, _high: jitter_value,
            retry_after=retry_after,
        )
        == expected
    )


def test_http_retry_delay_never_exceeds_the_retry_after_ceiling():
    for failed_attempt in range(0, 6):
        for retry_after in (None, 0.0, 5.0, 1_000_000.0):
            delay = http_retry_delay(
                failed_attempt,
                jitter=lambda _low, _high: 99.0,
                retry_after=retry_after,
            )
            assert 0.0 <= delay <= MAX_RETRY_AFTER_SECONDS


def test_adapters_take_retry_behavior_from_the_neutral_owner_not_workday():
    """Oracle HCM and TalentBrew must not import retry behavior from Workday."""

    import ast

    for module in ("oracle_hcm", "talentbrew", "workday"):
        tree = ast.parse(
            (ROOT / f"watcher/sources/{module}.py").read_text(encoding="utf-8")
        )
        workday_imports = {
            name.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "watcher.sources.workday"
            for name in node.names
        }
        assert not {"DEFAULT_MAX_ATTEMPTS", "workday_retry_delay"} & workday_imports, (
            f"{module} still imports retry behavior from workday"
        )

    from watcher.sources import oracle_hcm, talentbrew, workday

    assert oracle_hcm.http_retry_delay is http_retry_delay
    assert talentbrew.http_retry_delay is http_retry_delay
    assert workday.http_retry_delay is http_retry_delay
    assert oracle_hcm.DEFAULT_MAX_ATTEMPTS == talentbrew.DEFAULT_MAX_ATTEMPTS == 3
    assert not hasattr(workday, "workday_retry_delay")
