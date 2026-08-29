"""Transitional home for configuration validation not extracted in stage three.

Configuration models live in ``models.py``, process-environment behavior lives
in ``env.py``, and watchlist loading lives in ``loader.py``. Per-ATS,
registry-aware, hostname, feed URL, and uniqueness validation remains here
unchanged until the validation extraction stage.
"""

from __future__ import annotations

import re
from typing import Sequence
from urllib.parse import urlsplit

from watcher.company_matching import company_matching_key
from watcher.config.env import ConfigError
from watcher.config.models import CompanyCfg, GitHubListingSourceCfg

# Configuration-only modes: no direct adapter is attempted for these entries.
NON_DIRECT_ATS = frozenset({"bespoke", "github_only"})


def supported_ats() -> frozenset[str]:
    """Return every accepted watchlist ``ats`` value.

    The registry import is deferred because source adapters import this module;
    importing it here at module scope would create a cycle in the current MVP
    architecture.
    """

    from watcher.sources.registry import DIRECT_ATS

    return DIRECT_ATS | NON_DIRECT_ATS


SUPPORTED_GITHUB_LISTING_FORMATS = {
    "simplify_json",
    "github_markdown_table",
}
_HOSTNAME_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


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
