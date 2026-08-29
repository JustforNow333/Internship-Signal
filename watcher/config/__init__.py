"""Stable watcher configuration facade.

The package is in the third extraction stage: ``env.py`` owns dotenv and
process-environment handling, ``models.py`` owns configuration dataclasses,
``loader.py`` owns watchlist loading, and ``_legacy.py`` retains validation.
``env.py`` is imported first so dotenv initialization precedes
environment-derived model defaults.
"""

from __future__ import annotations

from watcher.config.env import (
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
from watcher.config._legacy import (
    NON_DIRECT_ATS,
    SUPPORTED_GITHUB_LISTING_FORMATS,
    is_valid_hostname,
    supported_ats,
)
from watcher.config.loader import (
    DEFAULT_WATCHLIST_PATH,
    _parse_watchlist_yaml,
    load_watchlist,
)
from watcher.config.models import (
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
