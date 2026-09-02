"""Pure watchlist configuration validation rules.

This module owns accepted-value vocabularies, section and field validation,
per-ATS entry rules, hostname and feed-URL checks, and cross-entry uniqueness.
Environment coercion belongs to ``env.py``; YAML parsing and configuration
construction belong to ``loader.py``.
"""

from __future__ import annotations

import re
from typing import Sequence
from urllib.parse import parse_qs, urlsplit

from watcher.company_matching import company_matching_key
from .env import ConfigError
from .models import (
    SUPPORTED_WORKDAY_DETAIL_POLICIES,
    SUPPORTED_WORKDAY_HOST_VARIANTS,
    WORKDAY_HOST_JOBS,
    CompanyCfg,
    GitHubListingSourceCfg,
)

# Configuration-only modes: no direct adapter is attempted for these entries.
NON_DIRECT_ATS = frozenset({"bespoke", "github_only"})


def supported_ats() -> frozenset[str]:
    """Return registered direct ATS values plus non-direct config modes.

    The registry import remains deferred because source adapters import watcher
    configuration; importing it at module scope would create a cycle.
    """

    from watcher.sources.registry import DIRECT_ATS

    return DIRECT_ATS | NON_DIRECT_ATS


SUPPORTED_GITHUB_LISTING_FORMATS = {
    "simplify_json",
    "github_markdown_table",
}
_HOSTNAME_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _validate_watchlist_sections(defaults: object, companies: object) -> None:
    if not isinstance(defaults, dict):
        raise ConfigError("watchlist defaults must be a mapping")
    if not isinstance(companies, list) or not companies:
        raise ConfigError("watchlist must define at least one company")


def _validate_default_terms_present(defaults: dict) -> None:
    if "terms" not in defaults:
        raise ConfigError(
            "watchlist defaults.terms must explicitly define at least one nonblank term"
        )


def _validated_min_score(value: object) -> int | None:
    if value in ("", None):
        return None
    if not isinstance(value, int):
        raise ConfigError("defaults.min_score must be an integer when set")
    return value


def _validate_company_entry(entry: object) -> None:
    if not isinstance(entry, dict):
        raise ConfigError("each company entry must be a mapping")


def _validate_company_identity(name: str, ats: str) -> None:
    if not name:
        raise ConfigError("company entry missing name")
    if ats not in supported_ats():
        raise ConfigError(f"{name}: unsupported ats '{ats}'")


def _validate_token_config(name: str, ats: str, token: str) -> None:
    if ats in {"greenhouse", "lever", "ashby", "smartrecruiters", "workable"} and not token:
        raise ConfigError(f"{name}: {ats} entries require token")


def _validate_workday_config(
    name: str,
    *,
    ats: str,
    token: str,
    shard: str,
    site: str,
    detail_policy: str,
    host_variant: str = WORKDAY_HOST_JOBS,
) -> None:
    if ats != "workday":
        return
    if not token:
        raise ConfigError(f"{name}: workday entries require token")
    if not shard:
        raise ConfigError(f"{name}: workday entries require workday_shard")
    if not site:
        raise ConfigError(f"{name}: workday entries require workday_site")
    if detail_policy not in SUPPORTED_WORKDAY_DETAIL_POLICIES:
        supported = ", ".join(sorted(SUPPORTED_WORKDAY_DETAIL_POLICIES))
        raise ConfigError(
            f"{name}: workday_detail_policy must be one of: {supported}"
        )
    if host_variant not in SUPPORTED_WORKDAY_HOST_VARIANTS:
        supported = ", ".join(sorted(SUPPORTED_WORKDAY_HOST_VARIANTS))
        raise ConfigError(
            f"{name}: workday_host_variant must be one of: {supported}"
        )


def _validate_terms_tuple(terms: tuple[str, ...], label: str) -> None:
    if not terms:
        raise ConfigError(f"{label} must define at least one nonblank term")


def _validate_aliases(aliases: Sequence[str], company_name: str) -> None:
    normalized: dict[str, str] = {}
    for alias in aliases:
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


def _validated_github_listing_urls(values: Sequence[object]) -> tuple[str, ...]:
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


def _validate_github_listing_sources_value(value: object) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ConfigError("defaults.github_listing_sources must be a list of mappings")


def _validated_github_source_fields(
    entry: object,
    index: int,
) -> tuple[str, str, str, str]:
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
        raise ConfigError(
            f"{label}.default_term is required for github_markdown_table"
        )
    return name, source_format, url, default_term


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
    if not is_valid_hostname(host):
        raise ConfigError(f"{name}: icims_host must be a hostname")
    if portals:
        if len(portals) != len(set(portals)):
            raise ConfigError(f"{name}: icims_portals must contain unique hostnames")
        if host not in portals:
            raise ConfigError(f"{name}: icims_portals must include icims_host")
        if any(not is_valid_hostname(portal) for portal in portals):
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


def _validate_successfactors_config(
    name: str,
    *,
    host: str,
    site_prefix: str,
    locale: str,
    source_url: str,
) -> None:
    if not is_valid_hostname(host):
        raise ConfigError(f"{name}: successfactors_host must be a hostname")
    if site_prefix and not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,78}[A-Za-z0-9])?",
        site_prefix,
    ):
        raise ConfigError(
            f"{name}: successfactors_site_prefix must be one safe path segment"
        )
    if locale and not re.fullmatch(r"[a-z]{2}_[A-Z]{2}", locale):
        raise ConfigError(
            f"{name}: successfactors_locale must use language_COUNTRY format"
        )
    try:
        parsed = urlsplit(source_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError(
            f"{name}: successfactors entries require a valid source_url"
        ) from exc
    root_path = f"/{site_prefix}/" if site_prefix else "/"
    allowed_paths = {root_path, f"{root_path}search/"}
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or parsed_port is not None
        or parsed.path not in allowed_paths
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{name}: successfactors source_url must be a credential-free HTTPS URL at the configured site root"
        )


def _validate_paylocity_config(
    name: str,
    *,
    company_id: str,
    module_id: str,
    slug: str,
    source_url: str,
) -> None:
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        company_id,
    ):
        raise ConfigError(f"{name}: paylocity_company_id must be a lower-case UUID")
    if not re.fullmatch(r"[1-9][0-9]*", module_id):
        raise ConfigError(f"{name}: paylocity_module_id must be a positive integer")
    if not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,126}[A-Za-z0-9])?", slug
    ):
        raise ConfigError(f"{name}: paylocity_slug must be one safe path segment")
    try:
        parsed = urlsplit(source_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError(
            f"{name}: paylocity entries require a valid source_url"
        ) from exc
    expected_path = f"/recruiting/jobs/All/{company_id}/{slug}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "recruiting.paylocity.com"
        or parsed.netloc != "recruiting.paylocity.com"
        or parsed.username
        or parsed.password
        or parsed_port is not None
        or parsed.path != expected_path
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{name}: paylocity source_url must exactly match its configured public board"
        )


def _validate_brassring_config(
    name: str,
    *,
    host: str,
    partner_id: str,
    site_id: str,
    source_url: str,
) -> None:
    if not is_valid_hostname(host):
        raise ConfigError(f"{name}: brassring_host must be a hostname")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", partner_id):
        raise ConfigError(f"{name}: brassring_partner_id must be a positive integer")
    if not re.fullmatch(r"[1-9][0-9]{0,19}", site_id):
        raise ConfigError(f"{name}: brassring_site_id must be a positive integer")
    try:
        parsed = urlsplit(source_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError(
            f"{name}: brassring entries require a valid source_url"
        ) from exc
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or parsed_port is not None
        or parsed.path.casefold() != "/tgnewui/search/home/home"
        or query != {"partnerid": [partner_id], "siteid": [site_id]}
        or parsed.fragment
    ):
        raise ConfigError(
            f"{name}: brassring source_url must exactly match its configured public board"
        )


def _validate_taleo_sourcing_config(
    name: str,
    *,
    host: str,
    site: str,
    source_url: str,
) -> None:
    if not is_valid_hostname(host):
        raise ConfigError(f"{name}: taleo_sourcing_host must be a hostname")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62}[A-Za-z0-9])?", site):
        raise ConfigError(
            f"{name}: taleo_sourcing_site must be a bounded site identifier"
        )
    try:
        parsed = urlsplit(source_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError(
            f"{name}: taleo_sourcing entries require a valid source_url"
        ) from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or parsed_port is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{name}: taleo_sourcing source_url must be its credential-free portal root"
        )


def _validate_ukg_config(
    name: str,
    *,
    host: str,
    tenant: str,
    board_id: str,
    source_url: str,
) -> None:
    if not is_valid_hostname(host):
        raise ConfigError(f"{name}: ukg_host must be a hostname")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?", tenant):
        raise ConfigError(f"{name}: ukg_tenant must be a bounded safe identifier")
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", board_id
    ):
        raise ConfigError(f"{name}: ukg_board_id must be a UUID")
    try:
        parsed = urlsplit(source_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{name}: ukg entries require a valid source_url") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or parsed_port is not None
        or parsed.path != f"/{tenant}/JobBoard/{board_id}/"
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{name}: ukg source_url must be its credential-free public board root"
        )


def _validate_eightfold_config(
    name: str,
    *,
    host: str,
    domain: str,
    variant: str,
    source_url: str,
) -> None:
    if variant != "legacy":
        raise ConfigError(f"{name}: eightfold_variant must be legacy")
    if not is_valid_hostname(host):
        raise ConfigError(f"{name}: eightfold_host must be a hostname")
    if not is_valid_hostname(domain):
        raise ConfigError(f"{name}: eightfold_domain must be a hostname")
    try:
        parsed = urlsplit(source_url)
        parsed_port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"{name}: eightfold entries require a valid source_url") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != host
        or parsed.netloc.casefold() != host
        or parsed.username
        or parsed.password
        or parsed_port is not None
        or parsed.path not in {"/careers", "/careers/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{name}: eightfold source_url must be its credential-free HTTPS careers root"
        )


def is_valid_hostname(value: str) -> bool:
    """Return whether value is a lower-case DNS hostname without URL syntax."""

    if (
        not value
        or len(value) > 253
        or not re.fullmatch(r"[a-z0-9.-]+", value)
        or value.startswith(".")
        or value.endswith(".")
        or ".." in value
        or any(not _HOSTNAME_LABEL.fullmatch(label) for label in value.split("."))
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
