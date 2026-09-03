"""Bounded transport retry mechanics shared by direct source adapters.

Several direct adapters issue plain bounded-retry HTTP requests with the same
contract: at most `DEFAULT_MAX_ATTEMPTS` attempts per request, retry only a
transient `SourceFetchError`, sleep roughly one second before the first retry
and three before later ones, and never sleep longer than five seconds. This
module owns that mechanism and nothing else — pagination, completeness,
parsing, dedupe, request construction, and reason codes stay in the adapter.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from watcher.sources.contracts import SourceFetchError

DEFAULT_MAX_ATTEMPTS = 3
FIRST_RETRY_DELAY_SECONDS = 1.0
LATER_RETRY_DELAY_SECONDS = 3.0
MAX_RETRY_DELAY_SECONDS = 5.0
# Ceiling for the Retry-After-aware delay below. A server-supplied
# Retry-After may raise the wait up to this bound but never past it.
MAX_RETRY_AFTER_SECONDS = 10.0

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded attempts per request plus an optional crawl-wide retry budget.

    `max_crawl_retries` is `None` for adapters that bound only a single
    request. Adapters that crawl many pages under one budget pass their own
    bound; the budget is spent across every request the retrier runs until it
    is reset.
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_crawl_retries: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= DEFAULT_MAX_ATTEMPTS:
            raise ValueError(
                f"max_attempts must be between 1 and {DEFAULT_MAX_ATTEMPTS}"
            )
        if self.max_crawl_retries is not None and self.max_crawl_retries < 0:
            raise ValueError("max_crawl_retries must not be negative")


def retry_delay(
    attempt: int,
    jitter: Callable[[float, float], float],
) -> float:
    """Return the bounded delay to sleep after `attempt` failed."""

    base = (
        FIRST_RETRY_DELAY_SECONDS
        if attempt == 1
        else LATER_RETRY_DELAY_SECONDS
    )
    return min(MAX_RETRY_DELAY_SECONDS, base + max(0.0, float(jitter(0.0, 1.0))))


def http_retry_delay(
    failed_attempt: int,
    *,
    jitter: Callable[[float, float], float] = random.uniform,
    retry_after: float | None = None,
) -> float:
    """Return the bounded delay before the next attempt, honoring Retry-After.

    Failed attempt one yields roughly 1-2 seconds; failed attempt two yields
    roughly 3-5 seconds. A Retry-After value can raise the delay up to ten
    seconds but can never create an unbounded sleep.

    This differs from `retry_delay` above, which the `RequestRetrier` uses and
    which has no Retry-After input. Adapters that read a server-supplied
    Retry-After header use this one; the formula is transport-level and carries
    nothing provider-specific.
    """

    if failed_attempt <= 1:
        backoff = 1.0 + max(0.0, min(1.0, float(jitter(0.0, 1.0))))
    else:
        backoff = 3.0 + max(0.0, min(2.0, float(jitter(0.0, 2.0))))
    if retry_after is not None:
        backoff = max(backoff, min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(retry_after))))
    return min(MAX_RETRY_AFTER_SECONDS, backoff)


class RequestRetrier:
    """Run one adapter's HTTP requests under a bounded retry policy.

    The retrier is per-adapter-instance state, never global: adapters are
    constructed per collection worker, and `reset()` clears the counters (and
    any crawl-wide budget) at the start of each fetch.
    """

    def __init__(
        self,
        *,
        policy: RetryPolicy,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._policy = policy
        self._sleeper = sleeper
        self._jitter = jitter
        self._request_attempts = 0
        self._retry_attempts = 0

    @property
    def policy(self) -> RetryPolicy:
        return self._policy

    @property
    def request_attempts(self) -> int:
        """Total HTTP attempts made, including retries."""

        return self._request_attempts

    @property
    def retry_attempts(self) -> int:
        """Retries performed; a request that exhausts its bound is not one."""

        return self._retry_attempts

    def reset(self) -> None:
        self._request_attempts = 0
        self._retry_attempts = 0

    def run(self, request: Callable[[], T]) -> T:
        """Return `request()`, retrying transient fetch failures in bounds.

        The exception raised when the bound is exhausted is the last
        `SourceFetchError` itself, carrying its attempt count and the
        `attempt`/`max_attempts` metadata the adapters publish.
        """

        max_attempts = self._policy.max_attempts
        budget = self._policy.max_crawl_retries
        for attempt in range(1, max_attempts + 1):
            self._request_attempts += 1
            try:
                return request()
            except SourceFetchError as exc:
                exc.attempt_count = attempt
                exc.response_metadata.update(
                    {"attempt": attempt, "max_attempts": max_attempts}
                )
                if (
                    not exc.retryable
                    or attempt == max_attempts
                    or (budget is not None and self._retry_attempts >= budget)
                ):
                    raise
                self._retry_attempts += 1
                self._sleeper(retry_delay(attempt, self._jitter))
        raise AssertionError("unreachable bounded retry state")
