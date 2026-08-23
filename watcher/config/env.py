"""The watcher's process-environment layer.

Everything that reads, parses, or coerces a value out of the process
environment lives here: dotenv loading, the `WATCHER_*` settings, the bounded
coercion helpers those settings share, and the defaults they fall back to.
YAML loading lives in `loader.py`; watchlist validation lives in
`validation.py`.

This is the lowest module in the config package. It imports nothing else from
`watcher.config` at module scope, because `models.py` imports
`DEFAULT_SEEN_DB_PATH` from here while its class bodies execute. The three
functions that need a model constant take a deferred import instead, the same
pattern `supported_ats()` uses.

Import-time behaviour is deliberate and load-bearing: `load_dotenv()` runs
while this module is imported, and `DEFAULT_SEEN_DB_PATH` is evaluated
immediately after it, so a `WATCHER_SEEN_DB` written in `.env` reaches the
default. Process environment values still win: `load_dotenv` uses
`os.environ.setdefault`.
"""

from __future__ import annotations

import ast
import math
import os
import re
from pathlib import Path

WATCHER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = WATCHER_DIR.parent
DEFAULT_DOTENV_PATH = REPO_ROOT / ".env"


class ConfigError(ValueError):
    """Raised when watcher config is missing or invalid."""


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

DEFAULT_SEEN_DB_PATH = Path(os.getenv("WATCHER_SEEN_DB", WATCHER_DIR / "seen.sqlite"))
DEFAULT_ANALYSIS_CACHE_FILENAME = "analysis-cache.sqlite"
DEFAULT_WORKDAY_MIN_INTERVAL_SECONDS = 0.5
MAX_WORKDAY_MIN_INTERVAL_SECONDS = 10.0


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

    from .models import DEFAULT_ANALYSIS_CACHE_ENABLED

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


def _collection_mode_value(value: object) -> str:
    from .models import (
        DEFAULT_COLLECTION_MODE,
        SUPPORTED_COLLECTION_MODES,
    )

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
    """Return validated collection concurrency settings from the environment.

    Production stays serial. Concurrent mode is opt-in for controlled canaries
    and is promoted only by a separate change after reviewed canary evidence.
    """

    from .models import (
        DEFAULT_COLLECTION_MAX_WORKERS,
        DEFAULT_COLLECTION_MODE,
        DEFAULT_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY,
        DEFAULT_WORKDAY_MAX_CONCURRENCY,
        CollectionConcurrencyCfg,
    )

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
