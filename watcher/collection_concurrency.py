"""Opt-in bounded concurrency for watcher source collection.

Serial collection remains the production default and the permanent rollback and
diagnostic path. Concurrent mode only changes *when* the existing per-source
fetch callables run; it never changes what they do. Adapters keep their own
timeouts, pacing, retries, backoff, and safety rules, and this module adds no
proxy rotation, cookie handling, header/identity rotation, browser automation,
challenge or CAPTCHA handling, alternate infrastructure, or undocumented
endpoints. Challenge, forbidden, unauthorized, and rate-limited responses stay
ordinary source failures produced by the adapters themselves.

Every task must satisfy *all* applicable limits at once:

* the global worker pool (``WATCHER_COLLECTION_MAX_WORKERS``),
* its origin limit (``WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY``),
* its provider limit (the per-origin limit, so a provider spread across many
  hosts cannot exceed the per-origin bound just by using different companies),
* the Workday limit (``WATCHER_WORKDAY_MAX_CONCURRENCY``) for Workday tasks.

The tightest applicable bound wins. Results are always returned in submission
order so downstream row ordering, dedupe precedence, and diagnostics stay
byte-identical to serial collection.
"""

from __future__ import annotations

import logging
import re
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from watcher.config import (
    COLLECTION_MODE_CONCURRENT,
    COLLECTION_MODE_SERIAL,
    CollectionConcurrencyCfg,
)

LOGGER = logging.getLogger(__name__)

PROVIDER_WORKDAY = "workday"
PROVIDER_GITHUB_FEED = "github_feed"
UNKNOWN_ORIGIN = "unknown"

# Host per direct adapter, mirroring each adapter's documented endpoint. A
# regression test derives the same hosts from the adapters so the two cannot
# drift. Workday is per tenant because each tenant is its own host.
DIRECT_ORIGIN_HOSTS: Mapping[str, str] = {
    "bain": "www.bain.com",
    "epic": "careers.epic.com",
    "greenhouse": "boards-api.greenhouse.io",
    "ibm": "www-api.ibm.com",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
    "smartrecruiters": "api.smartrecruiters.com",
    "paylocity": "recruiting.paylocity.com",
    "workable": "apply.workable.com",
}


class CollectionConcurrencyError(RuntimeError):
    """Raised when the bounded collection scheduler cannot run safely."""


def _safe_key(value: object, *, limit: int = 120) -> str:
    """Return a bounded identifier free of credentials, paths, and queries."""

    # Scheme separators stay readable; every key is assembled from a scheme,
    # host, and port only, so no path, query, or credential can reach it.
    text = str(value or "").strip()
    text = re.sub(r"[\x00-\x20\x7f]+", "_", text)
    text = re.sub(r"[^A-Za-z0-9_.:/-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_.")
    return text[:limit].casefold() or UNKNOWN_ORIGIN


def origin_key_for_url(url: str) -> str:
    """Return ``scheme://host[:port]`` with no credentials, path, or query."""

    raw = str(url or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return UNKNOWN_ORIGIN
    host = (parsed.hostname or "").casefold()
    if not host:
        return UNKNOWN_ORIGIN
    scheme = (parsed.scheme or "https").casefold()
    try:
        port = parsed.port
    except ValueError:
        port = None
    label = f"{scheme}://{host}" + (f":{port}" if port else "")
    return _safe_key(label)


def direct_origin_key(
    ats: str,
    token: str = "",
    workday_shard: str = "",
    oracle_hcm_host: str = "",
    talentbrew_host: str = "",
    icims_host: str = "",
    successfactors_host: str = "",
    brassring_host: str = "",
    taleo_sourcing_host: str = "",
    ukg_host: str = "",
) -> str:
    """Return the shared origin key for one direct adapter fetch.

    Companies that share an ATS host share this key, so two different companies
    on the same host can never bypass the per-origin limit.
    """

    adapter = str(ats or "").strip().casefold()
    if adapter == PROVIDER_WORKDAY:
        tenant = _safe_key(token, limit=63)
        shard = _safe_key(workday_shard, limit=63)
        if tenant == UNKNOWN_ORIGIN or shard == UNKNOWN_ORIGIN:
            return _safe_key("https://myworkdayjobs.com")
        return _safe_key(f"https://{tenant}.{shard}.myworkdayjobs.com")
    if adapter == "oracle_hcm":
        host = _safe_key(oracle_hcm_host, limit=253)
        return _safe_key(
            f"https://{host}" if host != UNKNOWN_ORIGIN else "https://oraclecloud.com"
        )
    if adapter == "talentbrew":
        host = _safe_key(talentbrew_host, limit=253)
        return _safe_key(
            f"https://{host}" if host != UNKNOWN_ORIGIN else "adapter:talentbrew"
        )
    if adapter == "icims":
        host = _safe_key(icims_host, limit=253)
        return _safe_key(
            f"https://{host}" if host != UNKNOWN_ORIGIN else "adapter:icims"
        )
    if adapter == "successfactors":
        host = _safe_key(successfactors_host, limit=253)
        return _safe_key(
            f"https://{host}"
            if host != UNKNOWN_ORIGIN
            else "adapter:successfactors"
        )
    if adapter == "brassring":
        host = _safe_key(brassring_host, limit=253)
        return _safe_key(
            f"https://{host}" if host != UNKNOWN_ORIGIN else "adapter:brassring"
        )
    if adapter == "taleo_sourcing":
        host = _safe_key(taleo_sourcing_host, limit=253)
        return _safe_key(
            f"https://{host}" if host != UNKNOWN_ORIGIN else "adapter:taleo_sourcing"
        )
    if adapter == "ukg":
        # Every tenant on one UltiPro recruiting host shares that host's limit.
        host = _safe_key(ukg_host, limit=253)
        return _safe_key(
            f"https://{host}" if host != UNKNOWN_ORIGIN else "adapter:ukg"
        )
    host = DIRECT_ORIGIN_HOSTS.get(adapter)
    if host:
        return _safe_key(f"https://{host}")
    return _safe_key(f"adapter:{adapter}")


@dataclass(frozen=True)
class CollectionTask:
    """One independent source fetch plus the scopes that bound it."""

    key: str
    origin: str
    provider: str
    run: Callable[[], object]
    workday: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "key", _safe_key(self.key))
        object.__setattr__(self, "origin", _safe_key(self.origin))
        object.__setattr__(self, "provider", _safe_key(self.provider))
        if not callable(self.run):
            raise CollectionConcurrencyError("collection task run must be callable")


@dataclass(frozen=True)
class TaskResult:
    """Outcome of one scheduled task, in submission order."""

    index: int
    task: CollectionTask
    value: object = None
    error: BaseException | None = None

    @property
    def failed(self) -> bool:
        return self.error is not None

    def error_label(self) -> str:
        if self.error is None:
            return ""
        return _safe_key(type(self.error).__name__, limit=80)


@dataclass(frozen=True)
class CollectionConcurrencyMetrics:
    """Observed concurrency evidence for one collection run."""

    mode: str = COLLECTION_MODE_SERIAL
    max_workers: int = 1
    per_origin_limit: int = 1
    workday_limit: int = 1
    tasks_total: int = 0
    # Collection tasks classify their own source failures, so any exception that
    # escapes a task is an unexpected worker/programming error, never ordinary
    # network variability.
    tasks_failed: int = 0
    unexpected_exceptions: int = 0
    max_observed_global: int = 0
    max_observed_per_origin: int = 0
    max_observed_workday: int = 0
    max_observed_provider: int = 0
    busiest_origin: str = ""
    executor_shutdown_clean: bool = True
    per_origin_observed: tuple[tuple[str, int], ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "max_workers": self.max_workers,
            "per_origin_limit": self.per_origin_limit,
            "workday_limit": self.workday_limit,
            "tasks_total": self.tasks_total,
            "tasks_failed": self.tasks_failed,
            "unexpected_exceptions": self.unexpected_exceptions,
            "max_observed_global": self.max_observed_global,
            "max_observed_per_origin": self.max_observed_per_origin,
            "max_observed_workday": self.max_observed_workday,
            "max_observed_provider": self.max_observed_provider,
            "busiest_origin": self.busiest_origin,
            "executor_shutdown_clean": self.executor_shutdown_clean,
            "per_origin_observed": [
                {"origin": origin, "max_observed": value}
                for origin, value in self.per_origin_observed
            ],
        }

    def limits_within_bounds(self) -> bool:
        """Return whether every observed maximum respected its configured cap."""

        return (
            self.max_observed_global <= self.max_workers
            and self.max_observed_per_origin <= self.per_origin_limit
            and self.max_observed_provider <= self.per_origin_limit
            and self.max_observed_workday <= self.workday_limit
        )


class ConcurrencyObserver:
    """Thread-safe running/peak counters for global and scoped concurrency."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: dict[str, int] = {}
        self._peak: dict[str, int] = {}

    def _enter(self, scope: str) -> None:
        current = self._current.get(scope, 0) + 1
        self._current[scope] = current
        if current > self._peak.get(scope, 0):
            self._peak[scope] = current

    def _exit(self, scope: str) -> None:
        self._current[scope] = max(0, self._current.get(scope, 0) - 1)

    def start(self, task: CollectionTask) -> None:
        with self._lock:
            self._enter("global")
            self._enter(f"origin:{task.origin}")
            self._enter(f"provider:{task.provider}")
            if task.workday:
                self._enter(PROVIDER_WORKDAY)

    def finish(self, task: CollectionTask) -> None:
        with self._lock:
            self._exit("global")
            self._exit(f"origin:{task.origin}")
            self._exit(f"provider:{task.provider}")
            if task.workday:
                self._exit(PROVIDER_WORKDAY)

    def peak(self, scope: str) -> int:
        with self._lock:
            return int(self._peak.get(scope, 0))

    def peaks_by_prefix(self, prefix: str) -> tuple[tuple[str, int], ...]:
        with self._lock:
            items = [
                (scope[len(prefix) :], value)
                for scope, value in self._peak.items()
                if scope.startswith(prefix)
            ]
        return tuple(sorted(items, key=lambda item: (-item[1], item[0])))

    def leaked_scopes(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                sorted(scope for scope, value in self._current.items() if value)
            )


@dataclass
class _ScopeLimiter:
    """Fixed-order semaphore acquisition, which makes deadlock impossible."""

    per_origin_limit: int
    workday_limit: int
    _origins: dict[str, threading.Semaphore] = field(default_factory=dict)
    _providers: dict[str, threading.Semaphore] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _workday: threading.Semaphore | None = None

    def __post_init__(self) -> None:
        self._workday = threading.Semaphore(self.workday_limit)

    def _semaphore(
        self,
        registry: dict[str, threading.Semaphore],
        key: str,
    ) -> threading.Semaphore:
        with self._lock:
            semaphore = registry.get(key)
            if semaphore is None:
                semaphore = threading.Semaphore(self.per_origin_limit)
                registry[key] = semaphore
            return semaphore

    def acquired(self, task: CollectionTask) -> list[threading.Semaphore]:
        # Always provider -> origin -> workday. A single global ordering means
        # no two tasks can hold resources the other needs.
        ordered = [
            self._semaphore(self._providers, task.provider),
            self._semaphore(self._origins, task.origin),
        ]
        if task.workday and self._workday is not None:
            ordered.append(self._workday)
        held: list[threading.Semaphore] = []
        try:
            for semaphore in ordered:
                semaphore.acquire()
                held.append(semaphore)
        except BaseException:
            for semaphore in reversed(held):
                semaphore.release()
            raise
        return held

    @staticmethod
    def release(held: Iterable[threading.Semaphore]) -> None:
        for semaphore in reversed(list(held)):
            semaphore.release()


def dispatch_order(tasks: Sequence[CollectionTask]) -> tuple[int, ...]:
    """Return a deterministic submission order that spreads limited scopes.

    Round-robining across ``(provider, origin)`` groups keeps a run of tenants
    on one bounded origin from filling every worker slot. Result order is
    unaffected: callers always read results by submission index.
    """

    groups: dict[tuple[str, str], list[int]] = {}
    for index, task in enumerate(tasks):
        groups.setdefault((task.provider, task.origin), []).append(index)
    ordered_groups = [groups[key] for key in sorted(groups)]
    order: list[int] = []
    position = 0
    while len(order) < len(tasks):
        added = False
        for group in ordered_groups:
            if position < len(group):
                order.append(group[position])
                added = True
        if not added:  # defensive: every group is exhausted
            break
        position += 1
    return tuple(order)


class CollectionScheduler:
    """Runs one or more task phases under one shared set of limits."""

    def __init__(
        self,
        concurrency: CollectionConcurrencyCfg,
        *,
        observer: ConcurrencyObserver | None = None,
    ) -> None:
        self.concurrency = concurrency
        self.observer = observer or ConcurrencyObserver()
        self._tasks_total = 0
        self._tasks_failed = 0
        self._shutdown_clean = True

    def run(self, tasks: Sequence[CollectionTask]) -> list[TaskResult]:
        results, shutdown_clean = _execute_tasks(
            tasks,
            concurrency=self.concurrency,
            observer=self.observer,
        )
        self._tasks_total += len(results)
        self._tasks_failed += sum(1 for result in results if result.failed)
        self._shutdown_clean = self._shutdown_clean and shutdown_clean
        return results

    def metrics(self) -> CollectionConcurrencyMetrics:
        return _metrics(
            concurrency=self.concurrency,
            observer=self.observer,
            tasks_total=self._tasks_total,
            tasks_failed=self._tasks_failed,
            serial=not self.concurrency.concurrent,
            executor_shutdown_clean=self._shutdown_clean,
        )


def run_collection_tasks(
    tasks: Sequence[CollectionTask],
    *,
    concurrency: CollectionConcurrencyCfg,
    observer: ConcurrencyObserver | None = None,
) -> tuple[list[TaskResult], CollectionConcurrencyMetrics]:
    """Run fetch tasks under the configured mode and return ordered results.

    A worker programming error never propagates out of the scheduler: it is
    converted into a failed :class:`TaskResult` so the remaining tasks still
    finish, and it is counted separately from ordinary source failures.
    """

    scheduler = CollectionScheduler(concurrency, observer=observer)
    results = scheduler.run(tasks)
    return results, scheduler.metrics()


def _execute_tasks(
    tasks: Sequence[CollectionTask],
    *,
    concurrency: CollectionConcurrencyCfg,
    observer: ConcurrencyObserver,
) -> tuple[list[TaskResult], bool]:
    task_list = list(tasks)
    if not concurrency.concurrent:
        return (
            [
                _run_serial(index, task, observer)
                for index, task in enumerate(task_list)
            ],
            True,
        )

    limiter = _ScopeLimiter(
        per_origin_limit=concurrency.per_origin_max_concurrency,
        workday_limit=concurrency.workday_max_concurrency,
    )
    prefix = f"watcher-collect-{uuid.uuid4().hex[:8]}"
    results_by_index: dict[int, TaskResult] = {}
    futures: dict[Future, int] = {}
    with ThreadPoolExecutor(
        max_workers=concurrency.max_workers,
        thread_name_prefix=prefix,
    ) as executor:
        for index in dispatch_order(task_list):
            task = task_list[index]
            futures[executor.submit(_run_limited, task, limiter, observer)] = index
        for future, index in futures.items():
            task = task_list[index]
            try:
                value, error = future.result()
            except Exception as exc:  # scheduler-level defensive boundary
                # Never re-raise a worker programming error here: the remaining
                # futures must still finish and be recorded. Interpreter-level
                # BaseExceptions still unwind through the context manager, which
                # shuts the executor down cleanly.
                value, error = None, exc
            results_by_index[index] = TaskResult(
                index=index,
                task=task,
                value=value,
                error=error,
            )
    results = [results_by_index[index] for index in range(len(task_list))]
    return results, _executor_shutdown_clean(prefix)


def _run_serial(
    index: int,
    task: CollectionTask,
    observer: ConcurrencyObserver,
) -> TaskResult:
    observer.start(task)
    try:
        value, error = _invoke(task)
    finally:
        observer.finish(task)
    return TaskResult(index=index, task=task, value=value, error=error)


def _run_limited(
    task: CollectionTask,
    limiter: _ScopeLimiter,
    observer: ConcurrencyObserver,
) -> tuple[object, BaseException | None]:
    held = limiter.acquired(task)
    try:
        observer.start(task)
        try:
            return _invoke(task)
        finally:
            observer.finish(task)
    finally:
        _ScopeLimiter.release(held)


def _invoke(task: CollectionTask) -> tuple[object, BaseException | None]:
    try:
        return task.run(), None
    except Exception as exc:  # worker programming errors must not abort the run
        LOGGER.error(
            "COLLECTION-TASK-ERROR task=%s origin=%s type=%s",
            task.key,
            task.origin,
            _safe_key(type(exc).__name__, limit=80),
        )
        return None, exc


def _executor_shutdown_clean(prefix: str) -> bool:
    alive = [
        thread.name
        for thread in threading.enumerate()
        if thread.name.startswith(prefix) and thread.is_alive()
    ]
    if alive:
        LOGGER.error(
            "COLLECTION-CONCURRENCY executor shutdown left %d worker thread(s) alive",
            len(alive),
        )
        return False
    return True


def _metrics(
    *,
    concurrency: CollectionConcurrencyCfg,
    observer: ConcurrencyObserver,
    tasks_total: int,
    tasks_failed: int,
    serial: bool,
    executor_shutdown_clean: bool,
) -> CollectionConcurrencyMetrics:
    per_origin = observer.peaks_by_prefix("origin:")
    per_provider = observer.peaks_by_prefix("provider:")
    leaked = observer.leaked_scopes()
    if leaked:
        LOGGER.error(
            "COLLECTION-CONCURRENCY left %d scope counter(s) non-zero", len(leaked)
        )
    return CollectionConcurrencyMetrics(
        mode=COLLECTION_MODE_SERIAL if serial else COLLECTION_MODE_CONCURRENT,
        max_workers=1 if serial else concurrency.max_workers,
        per_origin_limit=1 if serial else concurrency.per_origin_max_concurrency,
        workday_limit=1 if serial else concurrency.workday_max_concurrency,
        tasks_total=tasks_total,
        tasks_failed=tasks_failed,
        unexpected_exceptions=tasks_failed,
        max_observed_global=observer.peak("global"),
        max_observed_per_origin=max((value for _o, value in per_origin), default=0),
        max_observed_provider=max((value for _p, value in per_provider), default=0),
        max_observed_workday=observer.peak(PROVIDER_WORKDAY),
        busiest_origin=per_origin[0][0] if per_origin else "",
        executor_shutdown_clean=executor_shutdown_clean and not leaked,
        per_origin_observed=per_origin,
    )


def log_collection_concurrency(metrics: CollectionConcurrencyMetrics) -> None:
    """Emit one bounded, secret-free concurrency record."""

    LOGGER.info(
        "COLLECTION-CONCURRENCY mode=%s max_workers=%d per_origin_limit=%d "
        "workday_limit=%d tasks=%d max_observed_global=%d "
        "max_observed_per_origin=%d max_observed_provider=%d "
        "max_observed_workday=%d busiest_origin=%s unexpected_exceptions=%d "
        "executor_shutdown_clean=%s",
        metrics.mode,
        metrics.max_workers,
        metrics.per_origin_limit,
        metrics.workday_limit,
        metrics.tasks_total,
        metrics.max_observed_global,
        metrics.max_observed_per_origin,
        metrics.max_observed_provider,
        metrics.max_observed_workday,
        metrics.busiest_origin or "none",
        metrics.unexpected_exceptions,
        "true" if metrics.executor_shutdown_clean else "false",
    )
