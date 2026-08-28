"""Source collection: adapter resolution, fetch planning, outcomes, and attempts."""

from __future__ import annotations

import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from watcher.collection_concurrency import (
    PROVIDER_GITHUB_FEED,
    CollectionConcurrencyMetrics,
    CollectionScheduler,
    CollectionTask,
    TaskResult,
    direct_origin_key,
    log_collection_concurrency,
    origin_key_for_url,
)
from watcher.collection_snapshot import CollectionBatch, collection_config_fingerprint
from watcher.config import (
    COLLECTION_MODE_SERIAL,
    CollectionConcurrencyCfg,
    CompanyCfg,
    GitHubListingSourceCfg,
    WatcherConfig,
    workday_min_interval_seconds,
)
from watcher.run_logging import LOGGER, _timed_stage, _timing_log_value
from watcher.source_health import (
    ERROR_FETCH,
    ERROR_MISSING_ADAPTER,
    ERROR_SCHEMA,
    ERROR_SOURCE,
    ERROR_UNEXPECTED,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    SourceAttempt,
    direct_health_key,
    github_feed_health_key,
    new_run_id,
    sanitize_error,
    sanitize_feed_label,
    safe_error_kind,
    utc_datetime,
)
from watcher.sources import (
    DirectSourceDiagnostics,
    GitHubListingsSource,
    GitHubMarkdownTableSource,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
)
from watcher.sources.registry import DIRECT_ATS, build_direct_sources
from watcher.sources.workday import WorkdayPacer, WorkdayStartTelemetry
from watcher.text_safety import exception_text, safe_text


@dataclass
class CollectionStats:
    github_feeds_configured: int = 0
    github_feeds_succeeded: int = 0
    source_attempts: list[SourceAttempt] = field(default_factory=list)
    workday_attempted: int = 0
    workday_succeeded: int = 0
    workday_failed: int = 0
    workday_request_attempts: int = 0
    workday_retry_attempts: int = 0
    workday_failure_codes: Counter[str] = field(default_factory=Counter)
    # Collection-mode diagnostics stay in memory: they are canary evidence, not
    # part of the persisted collection snapshot schema.
    collection_mode: str = COLLECTION_MODE_SERIAL
    collection_concurrency: CollectionConcurrencyMetrics | None = None
    workday_start_telemetry: WorkdayStartTelemetry | None = None
    http_status_counts: Counter[int] = field(default_factory=Counter)
    challenge_responses: int = 0
    unexpected_task_exceptions: int = 0


@dataclass(frozen=True)
class WorkdayTransportSummary:
    attempted_tenants: int = 0
    successful_tenants: int = 0
    failed_tenants: int = 0
    retry_attempts: int = 0
    dominant_error: str = "none"
    dominant_error_count: int = 0
    likely_shared_incident: bool = False


WORKDAY_TRANSPORT_ERROR_CODES = frozenset(
    {
        "compressed_decode_failure",
        "connection_reset",
        "dns_failure",
        "empty_response",
        "html_challenge",
        "html_response",
        "json_decode_failure",
        "network_failure",
        "rate_limited",
        "redirected_to_html",
        "timeout",
        "transient_http_error",
        "unsupported_content_type",
    }
)


def _log_source_timing(
    *,
    company: str,
    adapter: str,
    success: bool,
    elapsed_seconds: float,
    rows_returned: int,
    source: object,
    source_name: str | None = None,
    error: Exception | None = None,
    request_count: int | None = None,
    retry_count: int | None = None,
) -> None:
    fields = [
        "SOURCE-TIMING",
        f"company={_timing_log_value(company)}",
        f"adapter={_timing_log_value(adapter)}",
    ]
    if source_name is not None:
        fields.append(f"source={_timing_log_value(source_name)}")
    fields.extend(
        (
            f"success={'true' if success else 'false'}",
            f"seconds={elapsed_seconds:.3f}",
            f"rows={max(0, int(rows_returned))}",
        )
    )
    if request_count is None and retry_count is None:
        request_count, retry_count = _source_request_counts(source, error=error)
    if request_count is not None:
        fields.append(f"requests={request_count}")
    if retry_count is not None:
        fields.append(f"retries={retry_count}")
    LOGGER.info(" ".join(fields))


def _source_request_counts(
    source: object,
    *,
    error: Exception | None,
) -> tuple[int | None, int | None]:
    try:
        diagnostics = getattr(source, "last_diagnostics", None)
    except Exception:
        diagnostics = None
    request_count = _nonnegative_optional_int(
        _safe_attribute(diagnostics, "request_attempts")
    )
    retry_count = _nonnegative_optional_int(
        _safe_attribute(diagnostics, "retry_attempts")
    )
    if request_count is None:
        attempt_count = _nonnegative_optional_int(
            _safe_attribute(error, "attempt_count")
        )
        if attempt_count is not None:
            request_count = max(1, attempt_count)
            if retry_count is None:
                retry_count = max(0, request_count - 1)
    return request_count, retry_count


def _safe_attribute(value: object, name: str) -> object | None:
    if value is None:
        return None
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _nonnegative_optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except Exception:
        return None


def collect_rows(
    config: WatcherConfig,
    *,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    stats: CollectionStats | None = None,
    run_id: str | None = None,
    observed_at: datetime | None = None,
) -> tuple[list[dict], list[str]]:
    batch = collect_batch(
        config,
        direct_sources=direct_sources,
        github_source=github_source,
        stats=stats,
        run_id=run_id,
        observed_at=observed_at,
    )
    return batch.mutable_rows(), list(batch.errors)


def collect_batch(
    config: WatcherConfig,
    *,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    stats: CollectionStats | None = None,
    run_id: str | None = None,
    observed_at: datetime | None = None,
    captured_at: datetime | None = None,
) -> CollectionBatch:
    """Run normal live collection and return its complete replayable result."""

    active_stats = stats if stats is not None else CollectionStats()
    with _timed_stage("collection"):
        rows, errors = _collect_rows(
            config,
            direct_sources=direct_sources,
            github_source=github_source,
            stats=active_stats,
            run_id=run_id,
            observed_at=observed_at,
        )
    return CollectionBatch.create(
        captured_at=captured_at or datetime.now(timezone.utc),
        collection_config_fingerprint=collection_config_fingerprint(config),
        rows=rows,
        errors=errors,
        source_attempts=active_stats.source_attempts,
        github_feeds_configured=active_stats.github_feeds_configured,
        github_feeds_succeeded=active_stats.github_feeds_succeeded,
        workday_attempted=active_stats.workday_attempted,
        workday_succeeded=active_stats.workday_succeeded,
        workday_failed=active_stats.workday_failed,
        workday_request_attempts=active_stats.workday_request_attempts,
        workday_retry_attempts=active_stats.workday_retry_attempts,
        workday_failure_codes=active_stats.workday_failure_codes,
    )


def _collection_stats_from_batch(batch: CollectionBatch) -> CollectionStats:
    return CollectionStats(
        github_feeds_configured=batch.github_feeds_configured,
        github_feeds_succeeded=batch.github_feeds_succeeded,
        source_attempts=list(batch.source_attempts),
        workday_attempted=batch.workday_attempted,
        workday_succeeded=batch.workday_succeeded,
        workday_failed=batch.workday_failed,
        workday_request_attempts=batch.workday_request_attempts,
        workday_retry_attempts=batch.workday_retry_attempts,
        workday_failure_codes=Counter(dict(batch.workday_failure_codes)),
    )


@dataclass
class _DirectFetchOutcome:
    """Everything one direct fetch produced, captured inside its worker."""

    rows: list[dict] = field(default_factory=list)
    succeeded: bool = False
    error: Exception | None = None
    error_kind: str = ""
    workday_failure_code: str = ""
    request_count: int = 1
    retry_count: int = 0
    status_code: int | None = None
    challenge_response: bool = False
    diagnostics: DirectSourceDiagnostics | None = None


@dataclass
class _GithubFetchOutcome:
    rows: list[dict] = field(default_factory=list)
    succeeded: bool = False
    error: Exception | None = None
    error_kind: str = ""
    status_code: int | None = None
    challenge_response: bool = False


class _DirectSourceProvider:
    """Resolve direct adapters safely for serial and concurrent collection.

    Injected registries are reused as-is so existing callers keep their exact
    behavior. Production collection (no injection) builds one adapter set per
    worker thread, because adapters such as Workday keep per-fetch diagnostics
    on the instance. The Workday tenant pacer is deliberately shared across
    those per-thread adapters so concurrency cannot weaken tenant pacing.
    """

    def __init__(
        self,
        direct_sources: dict[str, object] | None,
        *,
        concurrent: bool,
    ) -> None:
        self._injected = direct_sources
        self._local = threading.local()
        self._shared: dict[str, object] | None = None
        self._workday_pacer: WorkdayPacer | None = None
        if direct_sources is None:
            if concurrent:
                self._workday_pacer = WorkdayPacer(workday_min_interval_seconds())
                self._supported = DIRECT_ATS
            else:
                self._shared = _default_direct_sources()
                self._supported = frozenset(self._shared)
        else:
            self._supported = frozenset(direct_sources)

    def supports(self, ats: str) -> bool:
        return ats in self._supported

    def get(self, ats: str) -> object | None:
        if self._injected is not None:
            return self._injected.get(ats)
        if self._shared is not None:
            return self._shared.get(ats)
        mapping = getattr(self._local, "mapping", None)
        if mapping is None:
            mapping = _default_direct_sources(workday_pacer=self._workday_pacer)
            self._local.mapping = mapping
        return mapping.get(ats)

    def workday_start_telemetry(self) -> WorkdayStartTelemetry | None:
        pacer = self._workday_pacer
        if pacer is None:
            registry = self._injected if self._injected is not None else self._shared
            source = registry.get("workday") if registry is not None else None
            candidate = getattr(source, "_pacer", None)
            pacer = candidate if isinstance(candidate, WorkdayPacer) else None
        return pacer.start_telemetry() if pacer is not None else None


def _collect_rows(
    config: WatcherConfig,
    *,
    direct_sources: dict[str, object] | None = None,
    github_source: object | None = None,
    stats: CollectionStats | None = None,
    run_id: str | None = None,
    observed_at: datetime | None = None,
) -> tuple[list[dict], list[str]]:
    active_run_id = run_id or new_run_id(observed_at)
    active_observed_at = utc_datetime(observed_at or datetime.now(timezone.utc))
    if stats is None:
        stats = CollectionStats()
    concurrency = (
        getattr(config, "collection_concurrency", None) or CollectionConcurrencyCfg()
    )
    scheduler = CollectionScheduler(concurrency)
    stats.collection_mode = concurrency.mode
    source_provider = _DirectSourceProvider(
        direct_sources,
        concurrent=concurrency.concurrent,
    )
    configured_sources = config.effective_github_listing_sources()
    configured_count = len(configured_sources)
    if github_source is None:
        github_sources = [
            (source_config, _build_github_source(source_config))
            for source_config in configured_sources
        ]
    elif isinstance(github_source, (list, tuple)):
        github_sources = [
            (_config_for_injected_source(source, configured_sources), source)
            for source in github_source
        ]
    else:
        github_sources = [
            (_config_for_injected_source(github_source, configured_sources), github_source)
        ]
    github_sources.sort(key=lambda item: _github_runtime_source_sort_key(*item))
    stats.github_feeds_configured = configured_count
    direct_rows: list[dict] = []
    github_rows: list[dict] = []
    errors: list[str] = []

    with _timed_stage("direct_source_collection"):
        # Plan in configuration order, execute under the active mode, then apply
        # every outcome in that same order. Serial and concurrent collection
        # therefore produce identical rows, errors, attempts, and counters.
        planned: list[tuple[str, CompanyCfg, int | None]] = []
        direct_tasks: list[CollectionTask] = []
        for company in config.companies:
            if company.ats in {"bespoke", "github_only"}:
                planned.append(("unsupported", company, None))
                continue
            if not source_provider.supports(company.ats):
                planned.append(("missing_adapter", company, None))
                continue
            planned.append(("fetch", company, len(direct_tasks)))
            direct_tasks.append(_direct_collection_task(company, source_provider))
        direct_results = scheduler.run(direct_tasks)
        for kind, company, index in planned:
            if kind == "unsupported":
                LOGGER.info(
                    "Skipping direct fetch for %s (%s).", company.name, company.ats
                )
                stats.source_attempts.append(
                    SourceAttempt(
                        health_key=direct_health_key(company.name, company.ats),
                        run_id=active_run_id,
                        observed_at=active_observed_at,
                        source_kind=SOURCE_KIND_DIRECT,
                        company=company.name,
                        adapter=company.ats,
                        attempted=False,
                        succeeded=None,
                        rows_returned=None,
                        unsupported_reason=company.ats,
                    )
                )
                continue
            if kind == "missing_adapter":
                _record_error(
                    errors,
                    f"{company.name}: no source registered for ats '{company.ats}'",
                )
                stats.source_attempts.append(
                    _failed_attempt(
                        health_key=direct_health_key(company.name, company.ats),
                        run_id=active_run_id,
                        observed_at=active_observed_at,
                        source_kind=SOURCE_KIND_DIRECT,
                        company=company.name,
                        adapter=company.ats,
                        error_kind=ERROR_MISSING_ADAPTER,
                        error=RuntimeError(
                            f"no source registered for ats '{company.ats}'"
                        ),
                    )
                )
                continue
            assert index is not None
            _apply_direct_outcome(
                company,
                _direct_outcome_from_result(company, direct_results[index], stats),
                stats=stats,
                errors=errors,
                direct_rows=direct_rows,
                run_id=active_run_id,
                observed_at=active_observed_at,
            )
        stats.workday_start_telemetry = source_provider.workday_start_telemetry()

    with _timed_stage("github_backstop_collection"):
        github_plan = [
            _github_feed_plan(source_config, source)
            for source_config, source in github_sources
        ]
        github_results = scheduler.run(
            [
                _github_collection_task(plan, config)
                for plan in github_plan
            ]
        )
        for plan, result in zip(github_plan, github_results):
            _apply_github_outcome(
                plan,
                _github_outcome_from_result(plan, result, stats),
                stats=stats,
                errors=errors,
                github_rows=github_rows,
                run_id=active_run_id,
                observed_at=active_observed_at,
            )

    stats.collection_concurrency = scheduler.metrics()
    log_collection_concurrency(stats.collection_concurrency)

    # Direct rows first: backend dedupe keeps the first row's extra metadata,
    # so this implements the direct-over-GitHub source-priority rule.
    return [*direct_rows, *github_rows], errors


@dataclass(frozen=True)
class _GithubFeedPlan:
    """Stable identity for one configured GitHub backstop feed."""

    source_config: GitHubListingSourceCfg | None
    source: object
    source_name: str
    adapter: str
    label: str
    health_key: str
    url: str


def _direct_collection_task(
    company: CompanyCfg,
    source_provider: "_DirectSourceProvider",
) -> CollectionTask:
    """Describe one direct fetch and the scopes that bound it."""

    def run() -> _DirectFetchOutcome:
        # Resolved inside the worker so concurrent mode uses that thread's
        # adapter instance rather than sharing per-fetch adapter state.
        source = source_provider.get(company.ats)
        return _fetch_direct_source(company, source)

    return CollectionTask(
        key=f"direct:{company.ats}:{company.name}",
        origin=direct_origin_key(
            company.ats,
            token=company.token,
            workday_shard=company.workday_shard,
            oracle_hcm_host=company.oracle_hcm_host,
            talentbrew_host=company.talentbrew_host,
            icims_host=company.icims_host,
            successfactors_host=company.successfactors_host,
        ),
        provider=company.ats,
        run=run,
        workday=company.ats == "workday",
    )


def _fetch_direct_source(
    company: CompanyCfg,
    source: object,
) -> _DirectFetchOutcome:
    """Fetch one direct source, classifying every failure exactly as before."""

    rows: list[dict] = []
    succeeded = False
    error: Exception | None = None
    error_kind = ""
    workday_failure_code = ""
    fetch_started = time.perf_counter()
    try:
        LOGGER.info("Fetching %s via %s...", company.name, company.ats)
        rows = list(source.fetch(company))
        succeeded = True
        LOGGER.info("Fetched %d direct row(s) for %s.", len(rows), company.name)
    except SourceSchemaError as exc:
        error, error_kind, workday_failure_code = exc, ERROR_SCHEMA, "schema_failure"
    except SourceFetchError as exc:
        error, error_kind, workday_failure_code = exc, _fetch_error_kind(exc), exc.error_code
    except SourceError as exc:
        error, error_kind, workday_failure_code = exc, ERROR_SOURCE, "source_failure"
    except Exception as exc:  # defensive run-loop boundary
        error, error_kind, workday_failure_code = exc, ERROR_UNEXPECTED, "unexpected_exception"
    finally:
        request_count, retry_count = _source_request_counts(source, error=error)
        _log_source_timing(
            company=company.name,
            adapter=company.ats,
            success=succeeded,
            elapsed_seconds=time.perf_counter() - fetch_started,
            rows_returned=len(rows) if succeeded else 0,
            source=source,
            error=error,
            request_count=request_count,
            retry_count=retry_count,
        )
    return _DirectFetchOutcome(
        rows=rows if succeeded else [],
        succeeded=succeeded,
        error=error,
        error_kind=error_kind,
        workday_failure_code=workday_failure_code,
        request_count=request_count if request_count is not None else 1,
        retry_count=retry_count or 0,
        status_code=_http_status_from_error(error),
        challenge_response=_challenge_response(error),
        diagnostics=_direct_diagnostics_from_source(
            source,
            succeeded=succeeded,
            error_kind=error_kind,
        ),
    )


def _direct_outcome_from_result(
    company: CompanyCfg,
    result: TaskResult,
    stats: CollectionStats,
) -> _DirectFetchOutcome:
    """Convert an escaped worker error into an ordinary failed outcome."""

    if isinstance(result.value, _DirectFetchOutcome) and result.error is None:
        return result.value
    error = (
        result.error
        if result.error is not None
        else RuntimeError(f"collection task returned no outcome for {company.name}")
    )
    stats.unexpected_task_exceptions += 1
    LOGGER.error(
        "Collection task for %s failed unexpectedly: %s",
        company.name,
        sanitize_error(exception_text(error)),
    )
    return _DirectFetchOutcome(
        succeeded=False,
        error=error if isinstance(error, Exception) else RuntimeError(safe_text(error)),
        error_kind=ERROR_UNEXPECTED,
        workday_failure_code="unexpected_exception",
    )


def _apply_direct_outcome(
    company: CompanyCfg,
    outcome: _DirectFetchOutcome,
    *,
    stats: CollectionStats,
    errors: list[str],
    direct_rows: list[dict],
    run_id: str,
    observed_at: datetime,
) -> None:
    is_workday = company.ats == "workday"
    if is_workday:
        stats.workday_attempted += 1
    if outcome.status_code is not None:
        stats.http_status_counts[outcome.status_code] += 1
    if outcome.challenge_response:
        stats.challenge_responses += 1
    if outcome.succeeded:
        direct_rows.extend(outcome.rows)
        stats.source_attempts.append(
            _successful_attempt(
                health_key=direct_health_key(company.name, company.ats),
                run_id=run_id,
                observed_at=observed_at,
                source_kind=SOURCE_KIND_DIRECT,
                company=company.name,
                adapter=company.ats,
                rows_returned=len(outcome.rows),
                diagnostics=outcome.diagnostics,
            )
        )
        if is_workday:
            stats.workday_succeeded += 1
            stats.workday_request_attempts += outcome.request_count
            stats.workday_retry_attempts += outcome.retry_count
        return
    error = (
        outcome.error
        if outcome.error is not None
        else RuntimeError("unknown direct source failure")
    )
    if is_workday:
        _record_workday_failure(
            stats,
            outcome.workday_failure_code,
            request_count=outcome.request_count,
            retry_count=outcome.retry_count,
        )
    if outcome.error_kind == ERROR_UNEXPECTED:
        _record_error(
            errors,
            f"{company.name}: unexpected {exception_text(error)}",
        )
    else:
        _record_error(errors, f"{company.name}: {safe_text(error)}")
    stats.source_attempts.append(
        _failed_direct_attempt(
            company,
            run_id,
            observed_at,
            outcome.error_kind or ERROR_SOURCE,
            error,
        )
    )


def _github_feed_plan(
    source_config: GitHubListingSourceCfg | None,
    source: object,
) -> _GithubFeedPlan:
    configured_url = source_config.url if source_config else _github_source_url(source)
    source_name = source_config.name if source_config else _github_source_name(source)
    adapter = (
        source_config.format if source_config else _github_source_adapter(source)
    )
    safe_url = sanitize_feed_label(configured_url or _github_source_label(source))
    label = (
        safe_url
        if source_config and source_config.name.startswith("legacy_simplify_")
        else sanitize_feed_label(f"{source_name} [{safe_url}]")
    )
    return _GithubFeedPlan(
        source_config=source_config,
        source=source,
        source_name=source_name,
        adapter=adapter,
        label=label,
        health_key=github_feed_health_key(configured_url or safe_url),
        url=configured_url or "",
    )


def _github_collection_task(
    plan: _GithubFeedPlan,
    config: WatcherConfig,
) -> CollectionTask:
    return CollectionTask(
        key=f"github:{plan.source_name}",
        origin=origin_key_for_url(plan.url),
        provider=PROVIDER_GITHUB_FEED,
        run=lambda: _fetch_github_source(plan, config),
    )


def _fetch_github_source(
    plan: _GithubFeedPlan,
    config: WatcherConfig,
) -> _GithubFetchOutcome:
    rows: list[dict] = []
    succeeded = False
    error: Exception | None = None
    error_kind = ""
    fetch_started = time.perf_counter()
    source = plan.source
    try:
        LOGGER.info("Fetching GitHub listings backstop source %s...", plan.label)
        if hasattr(source, "fetch_many"):
            rows.extend(source.fetch_many(config.companies))
        else:
            for company in config.companies:
                rows.extend(source.fetch(company))
        succeeded = True
        LOGGER.info(
            "Fetched %d GitHub backstop row(s) from %s.", len(rows), plan.label
        )
    except SourceSchemaError as exc:
        error, error_kind = exc, ERROR_SCHEMA
    except SourceFetchError as exc:
        error, error_kind = exc, ERROR_FETCH
    except SourceError as exc:
        error, error_kind = exc, ERROR_SOURCE
    except Exception as exc:  # defensive run-loop boundary
        error, error_kind = exc, ERROR_UNEXPECTED
    finally:
        _log_source_timing(
            company="all",
            adapter=plan.adapter,
            source_name=plan.source_name,
            success=succeeded,
            elapsed_seconds=time.perf_counter() - fetch_started,
            rows_returned=len(rows) if succeeded else 0,
            source=source,
            error=error,
        )
    return _GithubFetchOutcome(
        rows=rows if succeeded else [],
        succeeded=succeeded,
        error=error,
        error_kind=error_kind,
        status_code=_http_status_from_error(error),
        challenge_response=_challenge_response(error),
    )


def _github_outcome_from_result(
    plan: _GithubFeedPlan,
    result: TaskResult,
    stats: CollectionStats,
) -> _GithubFetchOutcome:
    if isinstance(result.value, _GithubFetchOutcome) and result.error is None:
        return result.value
    error = (
        result.error
        if result.error is not None
        else RuntimeError(
            f"collection task returned no outcome for {plan.source_name}"
        )
    )
    stats.unexpected_task_exceptions += 1
    LOGGER.error(
        "GitHub backstop task %s failed unexpectedly: %s",
        plan.source_name,
        sanitize_error(exception_text(error)),
    )
    return _GithubFetchOutcome(
        succeeded=False,
        error=error if isinstance(error, Exception) else RuntimeError(safe_text(error)),
        error_kind=ERROR_UNEXPECTED,
    )


def _apply_github_outcome(
    plan: _GithubFeedPlan,
    outcome: _GithubFetchOutcome,
    *,
    stats: CollectionStats,
    errors: list[str],
    github_rows: list[dict],
    run_id: str,
    observed_at: datetime,
) -> None:
    if outcome.status_code is not None:
        stats.http_status_counts[outcome.status_code] += 1
    if outcome.challenge_response:
        stats.challenge_responses += 1
    if outcome.succeeded:
        github_rows.extend(outcome.rows)
        stats.github_feeds_succeeded += 1
        stats.source_attempts.append(
            _successful_attempt(
                health_key=plan.health_key,
                run_id=run_id,
                observed_at=observed_at,
                source_kind=SOURCE_KIND_GITHUB_FEED,
                company=None,
                adapter=plan.adapter,
                rows_returned=len(outcome.rows),
                feed_label=plan.label,
            )
        )
        return
    error = (
        outcome.error
        if outcome.error is not None
        else RuntimeError("unknown GitHub backstop failure")
    )
    if outcome.error_kind == ERROR_UNEXPECTED:
        _record_error(
            errors,
            f"github listings ({plan.label}): unexpected "
            + exception_text(error),
        )
    else:
        _record_error(
            errors,
            f"github listings ({plan.label}): {safe_text(error)}",
        )
    stats.source_attempts.append(
        _failed_attempt(
            health_key=plan.health_key,
            run_id=run_id,
            observed_at=observed_at,
            source_kind=SOURCE_KIND_GITHUB_FEED,
            company=None,
            adapter=plan.adapter,
            error_kind=outcome.error_kind or ERROR_SOURCE,
            error=error,
            feed_label=plan.label,
        )
    )


def _http_status_from_error(error: Exception | None) -> int | None:
    """Return the observed HTTP status without reading raw response bodies."""

    if error is None:
        return None
    status = getattr(error, "status_code", None)
    if isinstance(status, int) and 100 <= status <= 599:
        return status
    match = re.search(r"\bHTTP (\d{3})\b", safe_text(error))
    if match:
        value = int(match.group(1))
        if 100 <= value <= 599:
            return value
    return None


def _challenge_response(error: Exception | None) -> bool:
    if error is None:
        return False
    if str(getattr(error, "error_code", "")) == "html_challenge":
        return True
    metadata = getattr(error, "response_metadata", None)
    if isinstance(metadata, dict):
        return str(metadata.get("body_kind") or "") == "html_challenge"
    return False


def _default_direct_sources(
    *,
    workday_pacer: WorkdayPacer | None = None,
) -> dict[str, object]:
    return build_direct_sources(workday_pacer=workday_pacer)


def _build_github_source(config: GitHubListingSourceCfg) -> object:
    if config.format == "simplify_json":
        return GitHubListingsSource(config.url, source_name=config.name)
    if config.format == "github_markdown_table":
        return GitHubMarkdownTableSource(
            config.url,
            source_name=config.name,
            default_term=config.default_term,
        )
    raise ValueError(f"Unsupported GitHub listing source format: {config.format}")


def _config_for_injected_source(
    source: object,
    configured_sources: tuple[GitHubListingSourceCfg, ...],
) -> GitHubListingSourceCfg | None:
    source_url = sanitize_feed_label(_github_source_url(source))
    for configured in configured_sources:
        if sanitize_feed_label(configured.url) == source_url:
            return configured
    return None


def _github_runtime_source_sort_key(
    config: GitHubListingSourceCfg | None,
    source: object,
) -> tuple[int, str, str]:
    if config is not None:
        return config.priority, config.name.casefold(), config.url
    try:
        priority = int(getattr(source, "priority", 50))
    except (TypeError, ValueError):
        priority = 50
    return priority, _github_source_name(source).casefold(), _github_source_url(source)


def _record_error(errors: list[str], message: str) -> None:
    safe_message = sanitize_error(message)
    LOGGER.warning(safe_message)
    errors.append(safe_message)


def _successful_attempt(
    *,
    health_key: str,
    run_id: str,
    observed_at: datetime,
    source_kind: str,
    company: str | None,
    adapter: str,
    rows_returned: int,
    feed_label: str | None = None,
    diagnostics: DirectSourceDiagnostics | None = None,
) -> SourceAttempt:
    return SourceAttempt(
        health_key=health_key,
        run_id=run_id,
        observed_at=observed_at,
        source_kind=source_kind,
        company=company,
        adapter=adapter,
        attempted=True,
        succeeded=True,
        rows_returned=rows_returned,
        feed_label=feed_label,
        malformed_row_count=(
            diagnostics.malformed_row_count if diagnostics is not None else None
        ),
        schema_error_row_count=(
            diagnostics.schema_error_row_count if diagnostics is not None else None
        ),
        duplicate_row_count=(
            diagnostics.duplicate_row_count if diagnostics is not None else None
        ),
        failed_request_count=(
            diagnostics.failed_request_count if diagnostics is not None else None
        ),
        incomplete=diagnostics.incomplete if diagnostics is not None else None,
        truncated=diagnostics.truncated if diagnostics is not None else None,
        reason_codes=diagnostics.reason_codes if diagnostics is not None else (),
        degraded=diagnostics.degraded if diagnostics is not None else None,
        complete=diagnostics.complete if diagnostics is not None else None,
    )


def _failed_attempt(
    *,
    health_key: str,
    run_id: str,
    observed_at: datetime,
    source_kind: str,
    company: str | None,
    adapter: str,
    error_kind: str,
    error: Exception,
    feed_label: str | None = None,
) -> SourceAttempt:
    direct = source_kind == SOURCE_KIND_DIRECT
    reason_code = safe_error_kind(error_kind) or ERROR_SOURCE
    return SourceAttempt(
        health_key=health_key,
        run_id=run_id,
        observed_at=observed_at,
        source_kind=source_kind,
        company=company,
        adapter=adapter,
        attempted=True,
        succeeded=False,
        rows_returned=None,
        error_kind=error_kind,
        error_message=sanitize_error(exception_text(error)),
        feed_label=feed_label,
        malformed_row_count=0 if direct else None,
        schema_error_row_count=0 if direct else None,
        duplicate_row_count=0 if direct else None,
        failed_request_count=1 if direct else None,
        incomplete=True if direct else None,
        truncated=False if direct else None,
        reason_codes=(reason_code,) if direct else (),
        degraded=False if direct else None,
        complete=False if direct else None,
    )


def _direct_diagnostics_from_source(
    source: object,
    *,
    succeeded: bool,
    error_kind: str,
) -> DirectSourceDiagnostics | None:
    """Read the shared health contract one direct adapter published.

    Every registered direct adapter publishes `last_health_diagnostics` itself,
    because only the adapter knows what its provider-specific counters mean.
    This layer therefore stays free of adapter names: it records the failure
    contract for a failed fetch, returns whatever the adapter published for a
    successful one, and reports nothing for an injected or legacy source that
    publishes no shared diagnostics.
    """

    if not succeeded:
        return DirectSourceDiagnostics(
            succeeded=False,
            failed_request_count=1,
            incomplete=True,
            reason_codes=(safe_error_kind(error_kind) or ERROR_SOURCE,),
            complete=False,
        )
    shared = getattr(source, "last_health_diagnostics", None)
    return shared if isinstance(shared, DirectSourceDiagnostics) else None


def _fetch_error_kind(error: SourceFetchError) -> str:
    code = re.sub(r"[^a-z0-9_.-]+", "_", str(error.error_code or "").casefold()).strip("_")
    return ERROR_FETCH if not code or code == ERROR_FETCH else f"{ERROR_FETCH}/{code}"


def _record_workday_failure(
    stats: CollectionStats,
    code: str,
    *,
    request_count: int,
    retry_count: int,
) -> None:
    stable_code = re.sub(r"[^a-z0-9_.-]+", "_", str(code or "unknown").casefold()).strip("_")
    stats.workday_failed += 1
    stats.workday_request_attempts += max(1, int(request_count))
    stats.workday_retry_attempts += max(0, int(retry_count))
    stats.workday_failure_codes[stable_code or "unknown"] += 1


def summarize_workday_transport(stats: CollectionStats) -> WorkdayTransportSummary:
    dominant_error = "none"
    dominant_count = 0
    if stats.workday_failure_codes:
        dominant_error, dominant_count = sorted(
            stats.workday_failure_codes.items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
    shared = (
        stats.workday_failed >= 5
        and dominant_error in WORKDAY_TRANSPORT_ERROR_CODES
        and dominant_count * 100 >= stats.workday_failed * 60
    )
    return WorkdayTransportSummary(
        attempted_tenants=stats.workday_attempted,
        successful_tenants=stats.workday_succeeded,
        failed_tenants=stats.workday_failed,
        retry_attempts=stats.workday_retry_attempts,
        dominant_error=dominant_error,
        dominant_error_count=dominant_count,
        likely_shared_incident=shared,
    )


def _log_workday_transport(summary: WorkdayTransportSummary) -> None:
    LOGGER.warning(
        "WORKDAY TRANSPORT SUMMARY: attempted_tenants=%d successful_tenants=%d "
        "failed_tenants=%d retry_attempts=%d dominant_error=%s dominant_error_count=%d "
        "likely_shared_incident=%s",
        summary.attempted_tenants,
        summary.successful_tenants,
        summary.failed_tenants,
        summary.retry_attempts,
        summary.dominant_error,
        summary.dominant_error_count,
        "yes" if summary.likely_shared_incident else "no",
    )


def _failed_direct_attempt(
    company,
    run_id: str,
    observed_at: datetime,
    error_kind: str,
    error: Exception,
) -> SourceAttempt:
    return _failed_attempt(
        health_key=direct_health_key(company.name, company.ats),
        run_id=run_id,
        observed_at=observed_at,
        source_kind=SOURCE_KIND_DIRECT,
        company=company.name,
        adapter=company.ats,
        error_kind=error_kind,
        error=error,
    )


def _github_source_label(source: object) -> str:
    return str(
        getattr(source, "feed_label", "")
        or getattr(source, "name", "")
        or "injected"
    )


def _github_source_url(source: object) -> str:
    return str(getattr(source, "url", "") or getattr(source, "feed_label", "") or "")


def _github_source_name(source: object) -> str:
    return str(
        getattr(source, "source_name", "")
        or getattr(source, "name", "")
        or "injected"
    )


def _github_source_adapter(source: object) -> str:
    return str(
        getattr(source, "format", "")
        or getattr(source, "name", "")
        or "github_listings"
    )
