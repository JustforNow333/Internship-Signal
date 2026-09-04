"""Source adapters for external job posting systems.

The package facade keeps its historical exports without importing every
adapter when a caller needs only one source-layer module.
"""

from importlib import import_module


_EXPORT_MODULES = {
    "AshbySource": "watcher.sources.ashby",
    "BainSource": "watcher.sources.bain",
    "BloombergSource": "watcher.sources.bloomberg",
    "BrassRingSource": "watcher.sources.brassring",
    "DirectSourceDiagnostics": "watcher.sources.diagnostics",
    "EpicSource": "watcher.sources.epic",
    "EightfoldSource": "watcher.sources.eightfold",
    "GitHubListingsSource": "watcher.sources.github_listings",
    "GitHubMarkdownTableSource": "watcher.sources.github_markdown_table",
    "GreenhouseSource": "watcher.sources.greenhouse",
    "IcimsSource": "watcher.sources.icims",
    "IbmSource": "watcher.sources.ibm",
    "LeverSource": "watcher.sources.lever",
    "OracleHcmSource": "watcher.sources.oracle_hcm",
    "PaylocitySource": "watcher.sources.paylocity",
    "ShopifySource": "watcher.sources.shopify",
    "SmartRecruitersSource": "watcher.sources.smartrecruiters",
    "SuccessFactorsSource": "watcher.sources.successfactors",
    "TalentBrewSource": "watcher.sources.talentbrew",
    "TaleoSourcingSource": "watcher.sources.taleo_sourcing",
    "UkgSource": "watcher.sources.ukg",
    "Source": "watcher.sources.contracts",
    "SourceError": "watcher.sources.contracts",
    "SourceFetchError": "watcher.sources.contracts",
    "SourceSchemaError": "watcher.sources.contracts",
    "WorkableSource": "watcher.sources.workable",
    "WorkdaySource": "watcher.sources.workday",
    "make_row": "watcher.sources.rows",
}

__all__ = [
    "AshbySource",
    "BainSource",
    "BloombergSource",
    "BrassRingSource",
    "DirectSourceDiagnostics",
    "EpicSource",
    "EightfoldSource",
    "GitHubListingsSource",
    "GitHubMarkdownTableSource",
    "GreenhouseSource",
    "IcimsSource",
    "IbmSource",
    "LeverSource",
    "OracleHcmSource",
    "PaylocitySource",
    "ShopifySource",
    "SmartRecruitersSource",
    "SuccessFactorsSource",
    "TalentBrewSource",
    "TaleoSourcingSource",
    "UkgSource",
    "Source",
    "SourceError",
    "SourceFetchError",
    "SourceSchemaError",
    "WorkableSource",
    "WorkdaySource",
    "make_row",
]


def __getattr__(name: str) -> object:
    """Resolve and cache one documented package export on first access."""

    try:
        module_name = _EXPORT_MODULES[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include unresolved public exports in interactive package discovery."""

    return sorted(set(globals()) | set(__all__))
