"""Source adapters for external job posting systems."""

from .base import Source, SourceError, SourceFetchError, SourceSchemaError, make_row
from .ashby import AshbySource
from .github_listings import GitHubListingsSource
from .github_markdown_table import GitHubMarkdownTableSource
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .oracle_hcm import OracleHcmSource
from .smartrecruiters import SmartRecruitersSource
from .talentbrew import TalentBrewSource
from .workable import WorkableSource
from .workday import WorkdaySource

__all__ = [
    "AshbySource",
    "GitHubListingsSource",
    "GitHubMarkdownTableSource",
    "GreenhouseSource",
    "LeverSource",
    "OracleHcmSource",
    "SmartRecruitersSource",
    "TalentBrewSource",
    "Source",
    "SourceError",
    "SourceFetchError",
    "SourceSchemaError",
    "WorkableSource",
    "WorkdaySource",
    "make_row",
]
