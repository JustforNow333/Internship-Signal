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
from .epic import EpicSource
from .github_listings import GitHubListingsSource
from .github_markdown_table import GitHubMarkdownTableSource
from .greenhouse import GreenhouseSource
from .icims import IcimsSource
from .ibm import IbmSource
from .lever import LeverSource
from .oracle_hcm import OracleHcmSource
from .paylocity import PaylocitySource
from .smartrecruiters import SmartRecruitersSource
from .successfactors import SuccessFactorsSource
from .talentbrew import TalentBrewSource
from .workable import WorkableSource
from .workday import WorkdaySource

__all__ = [
    "AshbySource",
    "BainSource",
    "DirectSourceDiagnostics",
    "EpicSource",
    "GitHubListingsSource",
    "GitHubMarkdownTableSource",
    "GreenhouseSource",
    "IcimsSource",
    "IbmSource",
    "LeverSource",
    "OracleHcmSource",
    "PaylocitySource",
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
