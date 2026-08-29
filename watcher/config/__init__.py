"""Stable public facade for watcher configuration.

Environment handling lives in :mod:`.env`, dataclasses in :mod:`.models`,
watchlist validation in :mod:`.validation`, and loading in :mod:`.loader`.
Importing the environment layer first preserves the load-bearing dotenv and
environment-derived default evaluation order.
"""

from __future__ import annotations

from .env import (
    DEFAULT_ANALYSIS_CACHE_FILENAME,
    DEFAULT_DOTENV_PATH,
    DEFAULT_SEEN_DB_PATH,
    DEFAULT_WORKDAY_MIN_INTERVAL_SECONDS,
    MAX_WORKDAY_MIN_INTERVAL_SECONDS,
    REPO_ROOT,
    WATCHER_DIR,
    ConfigError,
    _parse_env_assignment,
    analysis_cache_enabled,
    load_collection_concurrency,
    load_dotenv,
    resolve_analysis_cache_path,
    workday_min_interval_seconds,
)
from .models import (
    COLLECTION_MODE_CONCURRENT,
    COLLECTION_MODE_SERIAL,
    DEFAULT_ANALYSIS_CACHE_ENABLED,
    DEFAULT_COLLECTION_MAX_WORKERS,
    DEFAULT_COLLECTION_MODE,
    DEFAULT_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY,
    DEFAULT_WORKDAY_MAX_CONCURRENCY,
    MAX_COLLECTION_MAX_WORKERS,
    MAX_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY,
    MAX_WORKDAY_MAX_CONCURRENCY,
    MIN_COLLECTION_MAX_WORKERS,
    MIN_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY,
    MIN_WORKDAY_MAX_CONCURRENCY,
    SUPPORTED_COLLECTION_MODES,
    SUPPORTED_WORKDAY_DETAIL_POLICIES,
    WORKDAY_DETAIL_EARLY_CAREER,
    WORKDAY_DETAIL_INTERNSHIP,
    WORKDAY_DETAIL_NONE,
    CollectionConcurrencyCfg,
    CompanyCfg,
    GitHubListingSourceCfg,
    WatcherConfig,
)
from .validation import (
    NON_DIRECT_ATS,
    SUPPORTED_GITHUB_LISTING_FORMATS,
    is_valid_hostname,
    supported_ats,
)
from .loader import (
    DEFAULT_WATCHLIST_PATH,
    _parse_watchlist_yaml,
    load_watchlist,
)


__all__ = [
    "COLLECTION_MODE_CONCURRENT",
    "COLLECTION_MODE_SERIAL",
    "CollectionConcurrencyCfg",
    "CompanyCfg",
    "ConfigError",
    "DEFAULT_ANALYSIS_CACHE_ENABLED",
    "DEFAULT_ANALYSIS_CACHE_FILENAME",
    "DEFAULT_COLLECTION_MAX_WORKERS",
    "DEFAULT_COLLECTION_MODE",
    "DEFAULT_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
    "DEFAULT_DOTENV_PATH",
    "DEFAULT_SEEN_DB_PATH",
    "DEFAULT_WATCHLIST_PATH",
    "DEFAULT_WORKDAY_MAX_CONCURRENCY",
    "DEFAULT_WORKDAY_MIN_INTERVAL_SECONDS",
    "GitHubListingSourceCfg",
    "MAX_COLLECTION_MAX_WORKERS",
    "MAX_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
    "MAX_WORKDAY_MAX_CONCURRENCY",
    "MAX_WORKDAY_MIN_INTERVAL_SECONDS",
    "MIN_COLLECTION_MAX_WORKERS",
    "MIN_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
    "MIN_WORKDAY_MAX_CONCURRENCY",
    "NON_DIRECT_ATS",
    "REPO_ROOT",
    "SUPPORTED_COLLECTION_MODES",
    "SUPPORTED_GITHUB_LISTING_FORMATS",
    "SUPPORTED_WORKDAY_DETAIL_POLICIES",
    "WATCHER_DIR",
    "WORKDAY_DETAIL_EARLY_CAREER",
    "WORKDAY_DETAIL_INTERNSHIP",
    "WORKDAY_DETAIL_NONE",
    "WatcherConfig",
    "analysis_cache_enabled",
    "is_valid_hostname",
    "load_collection_concurrency",
    "load_dotenv",
    "load_watchlist",
    "resolve_analysis_cache_path",
    "supported_ats",
    "workday_min_interval_seconds",
]
