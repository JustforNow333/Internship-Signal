"""Source adapters for external job posting systems."""

from .base import (
    DirectSourceDiagnostics,
    Source,
    SourceError,
    SourceFetchError,
    SourceSchemaError,
    make_row,
)
from .ashby import AshbySource
from .bain import BainSource
from .github_listings import GitHubListingsSource
from .github_markdown_table import GitHubMarkdownTableSource
from .greenhouse import GreenhouseSource
from .icims import IcimsSource
from .lever import LeverSource
from .oracle_hcm import OracleHcmSource
from .smartrecruiters import SmartRecruitersSource
from .successfactors import SuccessFactorsSource
from .talentbrew import TalentBrewSource
from .workable import WorkableSource
from .workday import WorkdaySource

__all__ = [
    "AshbySource",
    "BainSource",
    "DirectSourceDiagnostics",
    "GitHubListingsSource",
    "GitHubMarkdownTableSource",
    "GreenhouseSource",
    "IcimsSource",
    "LeverSource",
    "OracleHcmSource",
    "SmartRecruitersSource",
    "SuccessFactorsSource",
    "TalentBrewSource",
    "Source",
    "SourceError",
    "SourceFetchError",
    "SourceSchemaError",
    "WorkableSource",
    "WorkdaySource",
    "make_row",
]
