"""Configuration loading for the watcher."""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

WATCHER_DIR = Path(__file__).resolve().parent
DEFAULT_WATCHLIST_PATH = WATCHER_DIR / "watchlist.yml"
DEFAULT_SEEN_DB_PATH = Path(os.getenv("WATCHER_SEEN_DB", WATCHER_DIR / "seen.sqlite"))
SUPPORTED_ATS = {
    "greenhouse",
    "lever",
    "ashby",
    "smartrecruiters",
    "workable",
    "workday",
    "bespoke",
    "github_only",
}


class ConfigError(ValueError):
    """Raised when watcher config is missing or invalid."""


@dataclass(frozen=True)
class CompanyCfg:
    """Per-company source configuration used by adapters."""

    name: str
    ats: str = ""
    token: str = ""
    workday_shard: str = ""
    workday_site: str = ""
    module: str = ""
    aliases: Sequence[str] = field(default_factory=tuple)
    alumni_match: Sequence[str] = field(default_factory=tuple)
    terms: Sequence[str] = field(default_factory=lambda: ("Summer 2026",))

    def match_names(self) -> tuple[str, ...]:
        return (self.name, *tuple(self.aliases))


@dataclass(frozen=True)
class WatcherConfig:
    companies: tuple[CompanyCfg, ...]
    terms: tuple[str, ...] = ("Summer 2026",)
    target_roles: frozenset[str] = frozenset({"swe"})
    min_score: int | None = None
    seen_db_path: Path = DEFAULT_SEEN_DB_PATH


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

    terms = _string_tuple(defaults.get("terms", ("Summer 2026",)))
    target_roles = frozenset(_string_tuple(defaults.get("target_roles", ("swe",))))
    min_score = defaults.get("min_score")
    if min_score in ("", None):
        min_score = None
    elif not isinstance(min_score, int):
        raise ConfigError("defaults.min_score must be an integer when set")

    companies = tuple(_build_company(entry, terms) for entry in companies_data)
    return WatcherConfig(
        companies=companies,
        terms=terms,
        target_roles=target_roles,
        min_score=min_score,
        seen_db_path=DEFAULT_SEEN_DB_PATH,
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
    if ats in {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"} and not token:
        raise ConfigError(f"{name}: {ats} entries require token")
    workday_site = str(entry.get("workday_site") or "").strip()
    workday_shard = str(entry.get("workday_shard") or "").strip()
    if ats == "workday":
        if not token:
            raise ConfigError(f"{name}: workday entries require token")
        if not workday_shard:
            raise ConfigError(f"{name}: workday entries require workday_shard")
        if not workday_site:
            raise ConfigError(f"{name}: workday entries require workday_site")
    return CompanyCfg(
        name=name,
        ats=ats,
        token=token,
        workday_shard=workday_shard,
        workday_site=workday_site,
        module=str(entry.get("module") or "").strip(),
        aliases=_string_tuple(entry.get("aliases", ())),
        alumni_match=_string_tuple(entry.get("alumni_match", ())),
        terms=_string_tuple(entry.get("terms", default_terms)),
    )


def _parse_watchlist_yaml(text: str) -> dict:
    data: dict[str, object] = {}
    defaults: dict[str, object] | None = None
    companies: list[dict[str, object]] | None = None
    current_company: dict[str, object] | None = None
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
            continue
        if line == "companies:":
            companies = []
            data["companies"] = companies
            section = "companies"
            current_company = None
            continue

        if section == "defaults":
            if defaults is None or not line.startswith("  "):
                raise ConfigError(f"Invalid defaults line: {raw_line}")
            key, value = _split_key_value(line.strip())
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


def _strip_comment(line: str) -> str:
    in_single = False
    in_double = False
    for i, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            return line[:i]
    return line


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
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),)
