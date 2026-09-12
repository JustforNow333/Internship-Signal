"""Source adapters for external job posting systems.

The package facade keeps its historical exports without importing every
adapter when a caller needs only one source-layer module.
"""

from importlib import import_module


_EXPORT_MODULES = {
    "AlibabaSource": "watcher.sources.alibaba",
    "AtlassianSource": "watcher.sources.atlassian",
    "AppleSource": "watcher.sources.apple",
    "AnsysSource": "watcher.sources.ansys",
    "AshbySource": "watcher.sources.ashby",
    "BainSource": "watcher.sources.bain",
    "BechtelSource": "watcher.sources.bechtel",
    "BloombergSource": "watcher.sources.bloomberg",
    "BrassRingSource": "watcher.sources.brassring",
    "ByteDanceCareersSource": "watcher.sources.bytedance_careers",
    "DassaultSource": "watcher.sources.dassault",
    "DirectSourceDiagnostics": "watcher.sources.diagnostics",
    "EpicSource": "watcher.sources.epic",
    "EightfoldSource": "watcher.sources.eightfold",
    "EricssonSource": "watcher.sources.ericsson",
    "GitHubListingsSource": "watcher.sources.github_listings",
    "GitHubMarkdownTableSource": "watcher.sources.github_markdown_table",
    "GoogleSource": "watcher.sources.google",
    "GreenhouseSource": "watcher.sources.greenhouse",
    "HuaweiSource": "watcher.sources.huawei",
    "IcimsSource": "watcher.sources.icims",
    "KpmgSource": "watcher.sources.kpmg",
    "LamResearchSource": "watcher.sources.lam_research",
    "IbmSource": "watcher.sources.ibm",
    "LeverSource": "watcher.sources.lever",
    "OracleHcmSource": "watcher.sources.oracle_hcm",
    "PaylocitySource": "watcher.sources.paylocity",
    "SeaSource": "watcher.sources.sea",
    "SiemensSource": "watcher.sources.siemens",
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
    "AlibabaSource",
    "AtlassianSource",
    "AppleSource",
    "AnsysSource",
    "AshbySource",
    "BainSource",
    "BechtelSource",
    "BloombergSource",
    "BrassRingSource",
    "ByteDanceCareersSource",
    "DassaultSource",
    "DirectSourceDiagnostics",
    "EpicSource",
    "EightfoldSource",
    "EricssonSource",
    "GitHubListingsSource",
    "GitHubMarkdownTableSource",
    "GoogleSource",
    "GreenhouseSource",
    "HuaweiSource",
    "IcimsSource",
    "KpmgSource",
    "LamResearchSource",
    "IbmSource",
    "LeverSource",
    "OracleHcmSource",
    "PaylocitySource",
    "SeaSource",
    "ShopifySource",
    "SiemensSource",
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
