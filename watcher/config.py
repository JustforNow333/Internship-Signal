"""Configuration loading for the watcher."""

from __future__ import annotations

import ast
import hashlib
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from watcher.company_matching import company_matching_key

WATCHER_DIR = Path(__file__).resolve().parent
REPO_ROOT = WATCHER_DIR.parent
DEFAULT_DOTENV_PATH = REPO_ROOT / ".env"


def load_dotenv(path: str | Path = DEFAULT_DOTENV_PATH) -> None:
    """Load simple .env assignments without adding a dependency.

    Supports normal dotenv lines (`KEY=value`) and the PowerShell form currently
    documented in `.env.example` (`$env:KEY = "value"`). Existing process env
    values are left alone so explicit shell settings win.
    """

    path = Path(path)
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        parsed = _parse_env_assignment(raw_line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def _parse_env_assignment(line: str) -> tuple[str, str] | None:
    line = _strip_comment(line).strip()
    if not line:
        return None

    match = re.fullmatch(r"\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
    if not match:
        match = re.fullmatch(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
    if not match:
        return None
    return match.group(1), _parse_env_value(match.group(2).strip())


def _parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
        return str(parsed)
    return value


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for index, char in enumerate(line):
        if char == "\\" and (in_single or in_double):
            escaped = not escaped
            continue
        if char == "'" and not in_double and not escaped:
            in_single = not in_single
        elif char == '"' and not in_single and not escaped:
            in_double = not in_double
        elif (
            char == "#"
            and not in_single
            and not in_double
            and (index == 0 or line[index - 1].isspace())
        ):
            return line[:index]
        escaped = False
    return line


load_dotenv()

DEFAULT_WATCHLIST_PATH = WATCHER_DIR / "watchlist.yml"
DEFAULT_SEEN_DB_PATH = Path(os.getenv("WATCHER_SEEN_DB", WATCHER_DIR / "seen.sqlite"))
DEFAULT_ANALYSIS_CACHE_FILENAME = "analysis-cache.sqlite"
DEFAULT_WORKDAY_MIN_INTERVAL_SECONDS = 0.5
MAX_WORKDAY_MIN_INTERVAL_SECONDS = 10.0
DEFAULT_ANALYSIS_CACHE_ENABLED = True
COLLECTION_MODE_SERIAL = "serial"
COLLECTION_MODE_CONCURRENT = "concurrent"
SUPPORTED_COLLECTION_MODES = (COLLECTION_MODE_SERIAL, COLLECTION_MODE_CONCURRENT)
# Production stays serial. Concurrent mode is opt-in for controlled canaries and
# is promoted only by a separate change after reviewed canary evidence.
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
SUPPORTED_ATS = {
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "workday",
    "oracle_hcm",
    "talentbrew",
    "icims",
    "bespoke",
    "github_only",
}
SUPPORTED_GITHUB_LISTING_FORMATS = {
    "simplify_json",
    "github_markdown_table",
}
COVERAGE_STATUS_NO_SOURCE_FOUND = "no_source_found"
SUPPORTED_COVERAGE_STATUSES = {COVERAGE_STATUS_NO_SOURCE_FOUND}
MAX_PLATFORM_FAMILY_LENGTH = 80


class ConfigError(ValueError):
    """Raised when watcher config is missing or invalid."""


def resolve_analysis_cache_path(
    seen_db_path: str | Path,
    value: str | Path | None = None,
) -> Path:
    """Return the dedicated cache path, defaulting beside the seen database."""

    raw = (
        os.getenv("WATCHER_ANALYSIS_CACHE_PATH")
        if value is None
        else value
    )
    if raw is None or not str(raw).strip():
        return Path(seen_db_path).parent / DEFAULT_ANALYSIS_CACHE_FILENAME
    return Path(str(raw).strip())


def analysis_cache_enabled(value: str | bool | None = None) -> bool:
    """Return the validated watcher static-analysis cache switch."""

    raw = (
        os.getenv("WATCHER_ANALYSIS_CACHE_ENABLED")
        if value is None
        else value
    )
    if raw is None:
        return DEFAULT_ANALYSIS_CACHE_ENABLED
    if isinstance(raw, bool):
        return raw
    normalized = str(raw).strip().casefold()
    if not normalized:
        return DEFAULT_ANALYSIS_CACHE_ENABLED
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(
        "WATCHER_ANALYSIS_CACHE_ENABLED must be true/false, yes/no, on/off, or 1/0"
    )


def workday_min_interval_seconds(value: str | float | int | None = None) -> float:
    """Return the validated delay between starting Workday tenant fetches."""

    raw = os.getenv("WATCHER_WORKDAY_MIN_INTERVAL_SECONDS") if value is None else value
    if raw in (None, ""):
        return DEFAULT_WORKDAY_MIN_INTERVAL_SECONDS
    try:
        interval = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            "WATCHER_WORKDAY_MIN_INTERVAL_SECONDS must be a numeric value between 0 and 10"
        ) from exc
    if not math.isfinite(interval) or not 0 <= interval <= MAX_WORKDAY_MIN_INTERVAL_SECONDS:
        raise ConfigError(
            "WATCHER_WORKDAY_MIN_INTERVAL_SECONDS must be between 0 and 10 seconds"
        )
    return interval


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


def _collection_mode_value(value: object) -> str:
    if value is None:
        return DEFAULT_COLLECTION_MODE
    normalized = str(value).strip().casefold()
    if not normalized:
        return DEFAULT_COLLECTION_MODE
    if normalized not in SUPPORTED_COLLECTION_MODES:
        raise ConfigError(
            "WATCHER_COLLECTION_MODE must be one of: "
            + ", ".join(SUPPORTED_COLLECTION_MODES)
        )
    return normalized


def _bounded_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer between {minimum} and {maximum}")
    if isinstance(value, float) and not float(value).is_integer():
        raise ConfigError(f"{label} must be an integer between {minimum} and {maximum}")
    try:
        parsed = int(str(value).strip() if isinstance(value, str) else value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"{label} must be an integer between {minimum} and {maximum}"
        ) from exc
    if parsed < minimum or parsed > maximum:
        raise ConfigError(
            f"{label} must be an integer between {minimum} and {maximum}"
        )
    return parsed


def load_collection_concurrency(
    *,
    mode: str | None = None,
    max_workers: str | int | None = None,
    workday_max_concurrency: str | int | None = None,
    per_origin_max_concurrency: str | int | None = None,
) -> CollectionConcurrencyCfg:
    """Return validated collection concurrency settings from the environment."""

    return CollectionConcurrencyCfg(
        mode=_env_or_default(
            "WATCHER_COLLECTION_MODE", mode, DEFAULT_COLLECTION_MODE
        ),
        max_workers=_env_or_default(
            "WATCHER_COLLECTION_MAX_WORKERS",
            max_workers,
            DEFAULT_COLLECTION_MAX_WORKERS,
        ),
        workday_max_concurrency=_env_or_default(
            "WATCHER_WORKDAY_MAX_CONCURRENCY",
            workday_max_concurrency,
            DEFAULT_WORKDAY_MAX_CONCURRENCY,
        ),
        per_origin_max_concurrency=_env_or_default(
            "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
            per_origin_max_concurrency,
            DEFAULT_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY,
        ),
    )


def _env_or_default(name: str, value: object, default: object) -> object:
    if value is not None:
        return value
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return raw


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
    source_url: str = ""
    module: str = ""
    aliases: Sequence[str] = field(default_factory=tuple)
    alumni_match: Sequence[str] = field(default_factory=tuple)
    terms: Sequence[str] = field(default_factory=tuple)
    coverage_status: str = ""
    platform_family: str = ""

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

        sources = list(self.github_listing_sources)
        sources.extend(_legacy_github_source(url) for url in self.github_listing_urls)
        return tuple(sorted(sources, key=_github_source_sort_key))


def load_watchlist(path: str | Path = DEFAULT_WATCHLIST_PATH) -> WatcherConfig:
    """Load the small watcher YAML file using the supported config subset.

    The project does not depend on PyYAML, so this parser intentionally supports
    the simple watchlist shape used here: top-level `defaults` and `companies`,
    scalar values, booleans, integers, and inline lists.
    """

    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Watchlist not found: {path}")

    data = _parse_watchlist_yaml(path.read_text(encoding="utf-8"))
    defaults = data.get("defaults", {})
    companies_data = data.get("companies", [])
    if not isinstance(defaults, dict):
        raise ConfigError("watchlist defaults must be a mapping")
    if not isinstance(companies_data, list) or not companies_data:
        raise ConfigError("watchlist must define at least one company")

    if "terms" not in defaults:
        raise ConfigError("watchlist defaults.terms must explicitly define at least one nonblank term")
    terms = _terms_tuple(defaults["terms"], "defaults.terms")
    github_listing_sources = _github_listing_sources(defaults.get("github_listing_sources", ()))
    github_listing_urls = _github_listing_urls(defaults.get("github_listing_urls", ()))
    _validate_github_source_uniqueness(
        (*github_listing_sources, *(_legacy_github_source(url) for url in github_listing_urls))
    )
    target_roles = frozenset(_string_tuple(defaults.get("target_roles", ("swe",))))
    min_score = defaults.get("min_score")
    if min_score in ("", None):
        min_score = None
    elif not isinstance(min_score, int):
        raise ConfigError("defaults.min_score must be an integer when set")

    companies = tuple(_build_company(entry, terms) for entry in companies_data)
    _validate_unique_company_names(companies)
    return WatcherConfig(
        companies=companies,
        terms=terms,
        github_listing_sources=github_listing_sources,
        github_listing_urls=github_listing_urls,
        target_roles=target_roles,
        min_score=min_score,
        seen_db_path=DEFAULT_SEEN_DB_PATH,
        analysis_cache_enabled=analysis_cache_enabled(),
        analysis_cache_path=resolve_analysis_cache_path(DEFAULT_SEEN_DB_PATH),
        collection_concurrency=load_collection_concurrency(),
    )


def _build_company(entry: dict, default_terms: tuple[str, ...]) -> CompanyCfg:
    if not isinstance(entry, dict):
        raise ConfigError("each company entry must be a mapping")
    name = str(entry.get("name") or "").strip()
    ats = str(entry.get("ats") or "").strip()
    token = str(entry.get("token") or "").strip()
    if not name:
        raise ConfigError("company entry missing name")
    if ats not in SUPPORTED_ATS:
        raise ConfigError(f"{name}: unsupported ats '{ats}'")
    coverage_status = str(entry.get("coverage_status") or "").strip().casefold()
    if coverage_status and coverage_status not in SUPPORTED_COVERAGE_STATUSES:
        raise ConfigError(
            f"{name}: coverage_status must be one of: "
            + ", ".join(sorted(SUPPORTED_COVERAGE_STATUSES))
        )
    if coverage_status and ats not in {"bespoke", "github_only"}:
        raise ConfigError(
            f"{name}: coverage_status '{coverage_status}' requires bespoke or github_only ats"
        )
    platform_family = _platform_family(entry.get("platform_family"), name)
    if platform_family and ats not in {"bespoke", "github_only"}:
        raise ConfigError(
            f"{name}: platform_family requires bespoke or github_only ats"
        )
    if ats in {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"} and not token:
        raise ConfigError(f"{name}: {ats} entries require token")
    workday_site = str(entry.get("workday_site") or "").strip()
    workday_shard = str(entry.get("workday_shard") or "").strip()
    if "workday_detail_policy" in entry:
        raw_detail_policy = entry.get("workday_detail_policy")
        # YAML commonly decodes an unquoted ``none`` scalar as null. Treat an
        # explicitly present null as the documented disabled policy while a
        # missing setting retains the normal internship-candidate default.
        workday_detail_policy = (
            WORKDAY_DETAIL_NONE
            if raw_detail_policy is None
            else str(raw_detail_policy).strip()
        )
    else:
        workday_detail_policy = WORKDAY_DETAIL_INTERNSHIP
    if ats == "workday":
        if not token:
            raise ConfigError(f"{name}: workday entries require token")
        if not workday_shard:
            raise ConfigError(f"{name}: workday entries require workday_shard")
        if not workday_site:
            raise ConfigError(f"{name}: workday entries require workday_site")
        if workday_detail_policy not in SUPPORTED_WORKDAY_DETAIL_POLICIES:
            supported = ", ".join(sorted(SUPPORTED_WORKDAY_DETAIL_POLICIES))
            raise ConfigError(
                f"{name}: workday_detail_policy must be one of: {supported}"
            )
    oracle_hcm_host = str(entry.get("oracle_hcm_host") or "").strip().casefold()
    oracle_hcm_site = str(entry.get("oracle_hcm_site") or "").strip()
    source_url = str(entry.get("source_url") or "").strip()
    if ats == "oracle_hcm":
        _validate_oracle_hcm_config(
            name,
            host=oracle_hcm_host,
            site=oracle_hcm_site,
            source_url=source_url,
        )
    talentbrew_host = str(entry.get("talentbrew_host") or "").strip().casefold()
    talentbrew_site_id = str(entry.get("talentbrew_site_id") or "").strip()
    talentbrew_category_id = str(entry.get("talentbrew_category_id") or "").strip()
    talentbrew_category_name = str(entry.get("talentbrew_category_name") or "").strip()
    if ats == "talentbrew":
        _validate_talentbrew_config(
            name,
            host=talentbrew_host,
            site_id=talentbrew_site_id,
            category_id=talentbrew_category_id,
            category_name=talentbrew_category_name,
            source_url=source_url,
        )
    icims_variant = str(entry.get("icims_variant") or "").strip().casefold()
    icims_host = str(entry.get("icims_host") or "").strip().casefold()
    icims_portals = _string_tuple(entry.get("icims_portals", ()))
    icims_portals = tuple(portal.strip().casefold() for portal in icims_portals)
    if ats == "icims":
        _validate_icims_config(
            name,
            variant=icims_variant,
            host=icims_host,
            portals=icims_portals,
            source_url=source_url,
        )
    if "terms" in entry:
        company_terms = _terms_tuple(entry["terms"], f"{name}.terms")
    else:
        company_terms = default_terms
    aliases = _aliases_tuple(entry["aliases"], name) if "aliases" in entry else ()
    return CompanyCfg(
        name=name,
        ats=ats,
        token=token,
        workday_shard=workday_shard,
        workday_site=workday_site,
        workday_detail_policy=workday_detail_policy,
        oracle_hcm_host=oracle_hcm_host,
        oracle_hcm_site=oracle_hcm_site,
        talentbrew_host=talentbrew_host,
        talentbrew_site_id=talentbrew_site_id,
        talentbrew_category_id=talentbrew_category_id,
        talentbrew_category_name=talentbrew_category_name,
        icims_variant=icims_variant,
        icims_host=icims_host,
        icims_portals=icims_portals,
        source_url=source_url,
        module=str(entry.get("module") or "").strip(),
        aliases=aliases,
        alumni_match=_string_tuple(entry.get("alumni_match", ())),
        terms=company_terms,
        coverage_status=coverage_status,
        platform_family=platform_family,
    )


def _platform_family(value: object, company: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"{company}: platform_family must be a string")
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized or len(normalized) > MAX_PLATFORM_FAMILY_LENGTH:
        raise ConfigError(
            f"{company}: platform_family must be 1-{MAX_PLATFORM_FAMILY_LENGTH} characters"
        )
    if re.search(r"[\x00-\x1f\x7f]", normalized):
        raise ConfigError(f"{company}: platform_family contains invalid control characters")
    return normalized


def _validate_oracle_hcm_config(
    name: str,
    *,
    host: str,
    site: str,
    source_url: str,
) -> None:
    if not host:
        raise ConfigError(f"{name}: oracle_hcm entries require oracle_hcm_host")
    if (
        not re.fullmatch(r"[a-z0-9.-]+", host)
        or host.startswith(".")
        or ".." in host
    ):
        raise ConfigError(f"{name}: oracle_hcm_host must be a hostname")
    try:
        parsed_host = urlsplit(f"https://{host}")
    except ValueError as exc:
        raise ConfigError(f"{name}: oracle_hcm_host must be a hostname") from exc
    if (
        parsed_host.hostname != host
        or parsed_host.username
        or parsed_host.password
        or parsed_host.path not in {"", "/"}
        or parsed_host.query
        or parsed_host.fragment
        or not host.endswith(".oraclecloud.com")
    ):
        raise ConfigError(f"{name}: oracle_hcm_host must be an Oracle Cloud hostname")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", site):
        raise ConfigError(f"{name}: oracle_hcm entries require a valid oracle_hcm_site")
    try:
        parsed_source = urlsplit(source_url)
    except ValueError as exc:
        raise ConfigError(f"{name}: oracle_hcm entries require a valid source_url") from exc
    if (
        parsed_source.scheme.casefold() != "https"
        or (parsed_source.hostname or "").casefold() != host
        or parsed_source.netloc.casefold() != host
        or parsed_source.username
        or parsed_source.password
        or parsed_source.query
        or parsed_source.fragment
        or f"/sites/{site}/" not in parsed_source.path
    ):
        raise ConfigError(
            f"{name}: oracle_hcm source_url must be a credential-free HTTPS URL on oracle_hcm_host"
        )


def _validate_talentbrew_config(
    name: str,
    *,
    host: str,
    site_id: str,
    category_id: str,
    category_name: str,
    source_url: str,
) -> None:
    if (
        not re.fullmatch(r"[a-z0-9.-]+", host)
        or host.startswith(".")
        or ".." in host
    ):
        raise ConfigError(f"{name}: talentbrew entries require a valid talentbrew_host")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", site_id):
        raise ConfigError(f"{name}: talentbrew entries require talentbrew_site_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", category_id):
        raise ConfigError(f"{name}: talentbrew entries require talentbrew_category_id")
    if not category_name:
        raise ConfigError(f"{name}: talentbrew entries require talentbrew_category_name")
    try:
        parsed = urlsplit(source_url)
    except ValueError as exc:
        raise ConfigError(f"{name}: talentbrew entries require a valid source_url") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{name}: talentbrew source_url must be a credential-free HTTPS URL on talentbrew_host"
        )


def _validate_icims_config(
    name: str,
    *,
    variant: str,
    host: str,
    portals: tuple[str, ...],
    source_url: str,
) -> None:
    if variant not in {"jibe_json", "classic"}:
        raise ConfigError(
            f"{name}: icims_variant must be one of: classic, jibe_json"
        )
    if not _valid_config_hostname(host):
        raise ConfigError(f"{name}: icims_host must be a hostname")
    if portals:
        if len(portals) != len(set(portals)):
            raise ConfigError(f"{name}: icims_portals must contain unique hostnames")
        if host not in portals:
            raise ConfigError(f"{name}: icims_portals must include icims_host")
        if any(not _valid_config_hostname(portal) for portal in portals):
            raise ConfigError(f"{name}: icims_portals must contain only hostnames")
    try:
        parsed = urlsplit(source_url)
    except ValueError as exc:
        raise ConfigError(f"{name}: icims entries require a valid source_url") from exc
    allowed_hosts = set(portals or (host,))
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() not in allowed_hosts
        or parsed.netloc.casefold() not in allowed_hosts
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{name}: icims source_url must be a credential-free HTTPS URL on a configured portal"
        )


def _valid_config_hostname(value: str) -> bool:
    if (
        not value
        or len(value) > 253
        or not re.fullmatch(r"[a-z0-9.-]+", value)
        or value.startswith(".")
        or value.endswith(".")
        or ".." in value
    ):
        return False
    try:
        parsed = urlsplit(f"https://{value}")
    except ValueError:
        return False
    return bool(
        parsed.hostname == value
        and parsed.netloc == value
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _validate_unique_company_names(companies: Sequence[CompanyCfg]) -> None:
    owners: dict[str, tuple[int, str]] = {}
    for index, company in enumerate(companies):
        labels = (company.name, *company.aliases)
        for label in labels:
            key = company_matching_key(label)
            if not key:
                raise ConfigError(
                    f"{company.name}: company names and aliases must normalize to a nonblank value"
                )
            owner = owners.get(key)
            if owner is not None and owner[0] != index:
                raise ConfigError(
                    f"watchlist company/alias {label!r} is ambiguous between {owner[1]!r} and {company.name!r}"
                )
            owners[key] = (index, company.name)


def _parse_watchlist_yaml(text: str) -> dict:
    data: dict[str, object] = {}
    defaults: dict[str, object] | None = None
    companies: list[dict[str, object]] | None = None
    current_company: dict[str, object] | None = None
    current_default_list: list[dict[str, object]] | None = None
    current_default_item: dict[str, object] | None = None
    section = ""

    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).rstrip()
        if not line.strip():
            continue
        if line == "defaults:":
            defaults = {}
            data["defaults"] = defaults
            section = "defaults"
            current_company = None
            current_default_list = None
            current_default_item = None
            continue
        if line == "companies:":
            companies = []
            data["companies"] = companies
            section = "companies"
            current_company = None
            current_default_list = None
            current_default_item = None
            continue

        if section == "defaults":
            if defaults is None:
                raise ConfigError(f"Invalid defaults line: {raw_line}")
            if line.startswith("      ") and current_default_item is not None:
                key, value = _split_key_value(line.strip())
                current_default_item[key] = _parse_value(value)
                continue
            if line.startswith("    - ") and current_default_list is not None:
                current_default_item = {}
                current_default_list.append(current_default_item)
                rest = line.strip()[2:].strip()
                if rest:
                    key, value = _split_key_value(rest)
                    current_default_item[key] = _parse_value(value)
                continue
            if not line.startswith("  ") or line.startswith("    "):
                raise ConfigError(f"Invalid defaults line: {raw_line}")
            key, value = _split_key_value(line.strip())
            if key == "github_listing_sources" and value == "":
                current_default_list = []
                current_default_item = None
                defaults[key] = current_default_list
            else:
                current_default_list = None
                current_default_item = None
                defaults[key] = _parse_value(value)
            continue

        if section == "companies":
            if companies is None:
                raise ConfigError("companies section not initialized")
            stripped = line.strip()
            if line.startswith("  - "):
                current_company = {}
                companies.append(current_company)
                rest = stripped[2:].strip()
                if rest:
                    key, value = _split_key_value(rest)
                    current_company[key] = _parse_value(value)
                continue
            if current_company is None or not line.startswith("    "):
                raise ConfigError(f"Invalid company line: {raw_line}")
            key, value = _split_key_value(stripped)
            current_company[key] = _parse_value(value)
            continue

        raise ConfigError(f"Unknown watchlist line: {raw_line}")

    return data


def _split_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ConfigError(f"Expected key/value pair: {text}")
    key, value = text.split(":", 1)
    key = key.strip()
    if not key:
        raise ConfigError(f"Empty config key in: {text}")
    return key, value.strip()


def _parse_value(value: str):
    if value == "":
        return ""
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    if value.startswith("["):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ConfigError(f"Invalid inline list: {value}") from exc
        if not isinstance(parsed, list):
            raise ConfigError(f"Expected inline list: {value}")
        return parsed
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise ConfigError(f"Invalid quoted value: {value}") from exc
    try:
        return int(value)
    except ValueError:
        return value


def _string_tuple(value) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        return (stripped,) if stripped else ()
    if isinstance(value, Sequence):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)


def _aliases_tuple(value: object, company_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, Sequence):
        values = tuple(value)
    else:
        values = (value,)
    aliases: list[str] = []
    normalized: dict[str, str] = {}
    for raw_alias in values:
        alias = str(raw_alias).strip()
        if not alias:
            raise ConfigError(f"{company_name}: aliases may not contain blank values")
        key = company_matching_key(alias)
        if not key:
            raise ConfigError(f"{company_name}: alias {alias!r} normalizes to blank")
        previous = normalized.get(key)
        if previous is not None:
            raise ConfigError(
                f"{company_name}: aliases {previous!r} and {alias!r} normalize to the same value"
            )
        normalized[key] = alias
        aliases.append(alias)
    return tuple(aliases)


def _terms_tuple(value, label: str) -> tuple[str, ...]:
    terms = _string_tuple(value)
    if not terms:
        raise ConfigError(f"{label} must define at least one nonblank term")
    return terms


def _github_listing_urls(value) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, Sequence):
        values = tuple(value)
    else:
        values = (value,)

    urls: list[str] = []
    identities: dict[tuple[str, str, int | None, str], str] = {}
    for raw_url in values:
        url, identity = _validated_feed_url(raw_url, "defaults.github_listing_urls")
        previous = identities.get(identity)
        if previous is not None and previous != url:
            raise ConfigError(
                "defaults.github_listing_urls contains duplicate feed identities that differ only by query or fragment"
            )
        identities[identity] = url
        if url not in urls:
            urls.append(url)
    return tuple(urls)


def _github_listing_sources(value) -> tuple[GitHubListingSourceCfg, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ConfigError("defaults.github_listing_sources must be a list of mappings")

    sources = []
    for index, entry in enumerate(value, start=1):
        label = f"defaults.github_listing_sources[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"{label} must be a mapping")
        name = str(entry.get("name") or "").strip()
        source_format = str(entry.get("format") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ConfigError(
                f"{label}.name must use only letters, digits, periods, underscores, or hyphens"
            )
        if source_format not in SUPPORTED_GITHUB_LISTING_FORMATS:
            raise ConfigError(
                f"{label}.format must be one of: "
                + ", ".join(sorted(SUPPORTED_GITHUB_LISTING_FORMATS))
            )
        url, _identity = _validated_feed_url(entry.get("url"), f"{label}.url")
        default_term = str(entry.get("default_term") or "").strip()
        if source_format == "github_markdown_table" and not default_term:
            raise ConfigError(f"{label}.default_term is required for github_markdown_table")
        sources.append(
            GitHubListingSourceCfg(
                name=name,
                format=source_format,
                url=url,
                default_term=default_term,
            )
        )
    return tuple(sources)


def _validated_feed_url(
    raw_url: object,
    label: str,
) -> tuple[str, tuple[str, str, int | None, str]]:
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ConfigError(f"{label} values must be nonblank HTTP or HTTPS URLs")
    url = raw_url.strip()
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise ConfigError(f"{label} contains an invalid HTTP/HTTPS URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ConfigError(f"{label} contains an invalid HTTP/HTTPS URL")
    if parsed.username or parsed.password:
        raise ConfigError(f"{label} must not contain URL credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{label} contains an invalid HTTP/HTTPS URL") from exc
    return url, (
        parsed.scheme.lower(),
        str(parsed.hostname).lower(),
        port,
        parsed.path or "/",
    )


def _validate_github_source_uniqueness(
    sources: Sequence[GitHubListingSourceCfg],
) -> None:
    names: set[str] = set()
    identities: dict[tuple[str, str, int | None, str], str] = {}
    for source in sources:
        normalized_name = source.name.casefold()
        if normalized_name in names:
            raise ConfigError(
                f"defaults.github_listing_sources contains duplicate source name: {source.name}"
            )
        names.add(normalized_name)
        _url, identity = _validated_feed_url(source.url, "defaults.github_listing_sources.url")
        previous = identities.get(identity)
        if previous is not None:
            raise ConfigError(
                "GitHub listing sources contain duplicate feed identities after removing query or fragment"
            )
        identities[identity] = source.url


def _legacy_github_source(url: str) -> GitHubListingSourceCfg:
    safe_url = str(url).strip()
    digest = hashlib.sha256(safe_url.encode("utf-8")).hexdigest()[:10]
    return GitHubListingSourceCfg(
        name=f"legacy_simplify_{digest}",
        format="simplify_json",
        url=safe_url,
    )


def _github_source_sort_key(source: GitHubListingSourceCfg) -> tuple[int, str, str]:
    return source.priority, source.name.casefold(), source.url
