"""Watcher configuration: watchlist, environment, and validated limits.

`watcher.config` is the stable public path and re-exports the whole surface it
exposed before it became a package. The implementation is being decomposed:

| Module | Owns |
|---|---|
| `env.py` | dotenv loading, every `WATCHER_*` setting, and the coercion helpers |
| `models.py` | the configuration dataclasses and the constants intrinsic to them |
| `loader.py` | locating and reading the watchlist, YAML parsing, building the config objects |
| `validation.py` | pure watchlist and per-source configuration validation |
| `_legacy.py` | **transitional** — compatibility imports only |

`env.py` is imported first below because importing it runs `load_dotenv()`, and
`models.py` reads its environment-derived default from it.

Import from `watcher.config`; the split behind it is not stable yet.
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
from watcher.config.validation import (
    COVERAGE_STATUS_NO_SOURCE_FOUND,
    MAX_PLATFORM_FAMILY_LENGTH,
    NON_DIRECT_ATS,
    SUPPORTED_COVERAGE_STATUSES,
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
    "COVERAGE_STATUS_NO_SOURCE_FOUND",
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
    "MAX_PLATFORM_FAMILY_LENGTH",
    "MAX_WORKDAY_MAX_CONCURRENCY",
    "MAX_WORKDAY_MIN_INTERVAL_SECONDS",
    "MIN_COLLECTION_MAX_WORKERS",
    "MIN_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
    "MIN_WORKDAY_MAX_CONCURRENCY",
    "NON_DIRECT_ATS",
    "REPO_ROOT",
    "SUPPORTED_COLLECTION_MODES",
    "SUPPORTED_COVERAGE_STATUSES",
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
