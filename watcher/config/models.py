"""Dependency-light configuration data models for the watcher.

This module owns only the four configuration dataclasses and the constants
intrinsic to their fields. Environment parsing, dotenv handling, and coercion
helpers live in ``env.py``; YAML loading lives in ``loader.py`` and watchlist
validation lives in ``validation.py``.

``DEFAULT_SEEN_DB_PATH`` is environment-derived and owned by ``env.py``, which
guarantees that dotenv initialization runs before the field default is read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from watcher.config.env import ConfigError, DEFAULT_SEEN_DB_PATH

DEFAULT_ANALYSIS_CACHE_ENABLED = True
COLLECTION_MODE_SERIAL = "serial"
COLLECTION_MODE_CONCURRENT = "concurrent"
SUPPORTED_COLLECTION_MODES = (COLLECTION_MODE_SERIAL, COLLECTION_MODE_CONCURRENT)
DEFAULT_COLLECTION_MODE = COLLECTION_MODE_SERIAL
DEFAULT_COLLECTION_MAX_WORKERS = 4
MIN_COLLECTION_MAX_WORKERS = 1
MAX_COLLECTION_MAX_WORKERS = 16
DEFAULT_WORKDAY_MAX_CONCURRENCY = 1
MIN_WORKDAY_MAX_CONCURRENCY = 1
MAX_WORKDAY_MAX_CONCURRENCY = 5
DEFAULT_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY = 2
MIN_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY = 1
MAX_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY = 4
WORKDAY_DETAIL_NONE = "none"
WORKDAY_DETAIL_INTERNSHIP = "internship_candidates"
WORKDAY_DETAIL_EARLY_CAREER = "early_career_candidates"
SUPPORTED_WORKDAY_DETAIL_POLICIES = frozenset(
    {
        WORKDAY_DETAIL_NONE,
        WORKDAY_DETAIL_INTERNSHIP,
        WORKDAY_DETAIL_EARLY_CAREER,
    }
)


@dataclass(frozen=True)
class CollectionConcurrencyCfg:
    """Validated opt-in collection concurrency limits.

    Every limit is an upper bound: a task may run only when the global worker
    pool, its origin limit, its provider limit, and (for Workday) the Workday
    limit all allow it. Serial mode ignores the limits and remains the
    permanent rollback and diagnostic path.
    """

    mode: str = DEFAULT_COLLECTION_MODE
    max_workers: int = DEFAULT_COLLECTION_MAX_WORKERS
    workday_max_concurrency: int = DEFAULT_WORKDAY_MAX_CONCURRENCY
    per_origin_max_concurrency: int = DEFAULT_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY

    def __post_init__(self) -> None:
        from watcher.config.env import _bounded_int, _collection_mode_value

        object.__setattr__(self, "mode", _collection_mode_value(self.mode))
        object.__setattr__(
            self,
            "max_workers",
            _bounded_int(
                self.max_workers,
                "WATCHER_COLLECTION_MAX_WORKERS",
                MIN_COLLECTION_MAX_WORKERS,
                MAX_COLLECTION_MAX_WORKERS,
            ),
        )
        object.__setattr__(
            self,
            "workday_max_concurrency",
            _bounded_int(
                self.workday_max_concurrency,
                "WATCHER_WORKDAY_MAX_CONCURRENCY",
                MIN_WORKDAY_MAX_CONCURRENCY,
                MAX_WORKDAY_MAX_CONCURRENCY,
            ),
        )
        object.__setattr__(
            self,
            "per_origin_max_concurrency",
            _bounded_int(
                self.per_origin_max_concurrency,
                "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
                MIN_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY,
                MAX_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY,
            ),
        )
        if self.workday_max_concurrency > self.max_workers:
            raise ConfigError(
                "WATCHER_WORKDAY_MAX_CONCURRENCY cannot exceed "
                "WATCHER_COLLECTION_MAX_WORKERS"
            )
        if self.per_origin_max_concurrency > self.max_workers:
            raise ConfigError(
                "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY cannot exceed "
                "WATCHER_COLLECTION_MAX_WORKERS"
            )

    @property
    def concurrent(self) -> bool:
        return self.mode == COLLECTION_MODE_CONCURRENT

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "max_workers": self.max_workers,
            "workday_max_concurrency": self.workday_max_concurrency,
            "per_origin_max_concurrency": self.per_origin_max_concurrency,
        }


@dataclass(frozen=True)
class CompanyCfg:
    """Per-company source configuration used by adapters."""

    name: str
    ats: str = ""
    token: str = ""
    workday_shard: str = ""
    workday_site: str = ""
    workday_detail_policy: str = WORKDAY_DETAIL_INTERNSHIP
    oracle_hcm_host: str = ""
    oracle_hcm_site: str = ""
    talentbrew_host: str = ""
    talentbrew_site_id: str = ""
    talentbrew_category_id: str = ""
    talentbrew_category_name: str = ""
    icims_variant: str = ""
    icims_host: str = ""
    icims_portals: Sequence[str] = field(default_factory=tuple)
    successfactors_host: str = ""
    successfactors_site_prefix: str = ""
    successfactors_locale: str = ""
    paylocity_company_id: str = ""
    paylocity_module_id: str = ""
    paylocity_slug: str = ""
    source_url: str = ""
    module: str = ""
    aliases: Sequence[str] = field(default_factory=tuple)
    alumni_match: Sequence[str] = field(default_factory=tuple)
    terms: Sequence[str] = field(default_factory=tuple)

    def match_names(self) -> tuple[str, ...]:
        return (self.name, *tuple(self.aliases))


@dataclass(frozen=True)
class GitHubListingSourceCfg:
    """One typed GitHub backstop feed from watchlist configuration."""

    name: str
    format: str
    url: str
    default_term: str = ""

    @property
    def priority(self) -> int:
        return {
            "simplify_json": 10,
            "github_markdown_table": 20,
        }[self.format]


@dataclass(frozen=True)
class WatcherConfig:
    companies: tuple[CompanyCfg, ...]
    terms: tuple[str, ...] = ()
    github_listing_sources: tuple[GitHubListingSourceCfg, ...] = ()
    github_listing_urls: tuple[str, ...] = ()
    target_roles: frozenset[str] = frozenset({"swe"})
    min_score: int | None = None
    seen_db_path: Path = DEFAULT_SEEN_DB_PATH
    analysis_cache_enabled: bool = DEFAULT_ANALYSIS_CACHE_ENABLED
    analysis_cache_path: Path | None = None
    collection_concurrency: CollectionConcurrencyCfg = field(
        default_factory=CollectionConcurrencyCfg
    )

    def __post_init__(self) -> None:
        from watcher.config.env import resolve_analysis_cache_path

        seen_db_path = Path(self.seen_db_path)
        cache_path = (
            resolve_analysis_cache_path(seen_db_path, "")
            if self.analysis_cache_path is None
            else Path(self.analysis_cache_path)
        )
        object.__setattr__(self, "seen_db_path", seen_db_path)
        object.__setattr__(self, "analysis_cache_path", cache_path)

    def effective_github_listing_sources(self) -> tuple[GitHubListingSourceCfg, ...]:
        """Return typed sources plus deterministic adapters for legacy URLs."""

        from watcher.config.loader import (
            _github_source_sort_key,
            _legacy_github_source,
        )

        sources = list(self.github_listing_sources)
        sources.extend(_legacy_github_source(url) for url in self.github_listing_urls)
        return tuple(sorted(sources, key=_github_source_sort_key))
