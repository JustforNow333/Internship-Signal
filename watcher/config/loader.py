"""Watchlist file loading: YAML text in, configuration objects out.

This module owns locating and reading the watchlist, the small hand-written
YAML subset parser, and the construction of `CompanyCfg`,
`GitHubListingSourceCfg`, and the final `WatcherConfig` from what it parses.

Validation is deliberately separate. The per-ATS rules, hostname and feed-URL
checks, uniqueness checks, and accepted-value vocabularies live in
``validation.py``; this module only parses and constructs configuration.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Sequence

from .env import (
    DEFAULT_SEEN_DB_PATH,
    WATCHER_DIR,
    ConfigError,
    _strip_comment,
    analysis_cache_enabled,
    load_collection_concurrency,
    resolve_analysis_cache_path,
)
from .models import (
    WORKDAY_DETAIL_INTERNSHIP,
    WORKDAY_DETAIL_NONE,
    CompanyCfg,
    GitHubListingSourceCfg,
    WatcherConfig,
)

from .validation import (
    _platform_family,
    _validate_aliases,
    _validate_company_entry,
    _validate_company_identity,
    _validate_coverage_status,
    _validate_default_terms_present,
    _validate_github_source_uniqueness,
    _validate_github_listing_sources_value,
    _validate_icims_config,
    _validate_oracle_hcm_config,
    _validate_paylocity_config,
    _validate_platform_family_mode,
    _validate_successfactors_config,
    _validate_talentbrew_config,
    _validate_terms_tuple,
    _validate_token_config,
    _validate_unique_company_names,
    _validate_watchlist_sections,
    _validate_workday_config,
    _validated_github_listing_urls,
    _validated_github_source_fields,
    _validated_min_score,
)

DEFAULT_WATCHLIST_PATH = WATCHER_DIR / "watchlist.yml"


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
    _validate_watchlist_sections(defaults, companies_data)

    _validate_default_terms_present(defaults)
    terms = _terms_tuple(defaults["terms"], "defaults.terms")
    github_listing_sources = _github_listing_sources(defaults.get("github_listing_sources", ()))
    github_listing_urls = _github_listing_urls(defaults.get("github_listing_urls", ()))
    _validate_github_source_uniqueness(
        (*github_listing_sources, *(_legacy_github_source(url) for url in github_listing_urls))
    )
    target_roles = frozenset(_string_tuple(defaults.get("target_roles", ("swe",))))
    min_score = _validated_min_score(defaults.get("min_score"))

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
    _validate_company_entry(entry)
    name = str(entry.get("name") or "").strip()
    ats = str(entry.get("ats") or "").strip()
    token = str(entry.get("token") or "").strip()
    _validate_company_identity(name, ats)
    coverage_status = str(entry.get("coverage_status") or "").strip().casefold()
    _validate_coverage_status(name, ats, coverage_status)
    platform_family = _platform_family(entry.get("platform_family"), name)
    _validate_platform_family_mode(name, ats, platform_family)
    _validate_token_config(name, ats, token)
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
    _validate_workday_config(
        name,
        ats=ats,
        token=token,
        shard=workday_shard,
        site=workday_site,
        detail_policy=workday_detail_policy,
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
    successfactors_host = str(
        entry.get("successfactors_host") or ""
    ).strip().casefold()
    successfactors_site_prefix = str(
        entry.get("successfactors_site_prefix") or ""
    ).strip()
    successfactors_locale = str(
        entry.get("successfactors_locale") or ""
    ).strip()
    if ats == "successfactors":
        _validate_successfactors_config(
            name,
            host=successfactors_host,
            site_prefix=successfactors_site_prefix,
            locale=successfactors_locale,
            source_url=source_url,
        )
    paylocity_company_id = str(
        entry.get("paylocity_company_id") or ""
    ).strip().casefold()
    paylocity_module_id = str(entry.get("paylocity_module_id") or "").strip()
    paylocity_slug = str(entry.get("paylocity_slug") or "").strip()
    if ats == "paylocity":
        _validate_paylocity_config(
            name,
            company_id=paylocity_company_id,
            module_id=paylocity_module_id,
            slug=paylocity_slug,
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
        successfactors_host=successfactors_host,
        successfactors_site_prefix=successfactors_site_prefix,
        successfactors_locale=successfactors_locale,
        paylocity_company_id=paylocity_company_id,
        paylocity_module_id=paylocity_module_id,
        paylocity_slug=paylocity_slug,
        source_url=source_url,
        module=str(entry.get("module") or "").strip(),
        aliases=aliases,
        alumni_match=_string_tuple(entry.get("alumni_match", ())),
        terms=company_terms,
        coverage_status=coverage_status,
        platform_family=platform_family,
    )


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
    aliases = tuple(str(raw_alias).strip() for raw_alias in values)
    _validate_aliases(aliases, company_name)
    return aliases


def _terms_tuple(value, label: str) -> tuple[str, ...]:
    terms = _string_tuple(value)
    _validate_terms_tuple(terms, label)
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

    return _validated_github_listing_urls(values)


def _github_listing_sources(value) -> tuple[GitHubListingSourceCfg, ...]:
    if value in (None, ""):
        return ()
    _validate_github_listing_sources_value(value)

    sources = []
    for index, entry in enumerate(value, start=1):
        name, source_format, url, default_term = _validated_github_source_fields(
            entry,
            index,
        )
        sources.append(
            GitHubListingSourceCfg(
                name=name,
                format=source_format,
                url=url,
                default_term=default_term,
            )
        )
    return tuple(sources)


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
