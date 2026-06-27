"""Source adapters for external job posting systems."""

from .base import Source, SourceError, SourceFetchError, SourceSchemaError, make_row
from .ashby import AshbySource
from .github_listings import GitHubListingsSource
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .smartrecruiters import SmartRecruitersSource
from .workable import WorkableSource
from .workday import WorkdaySource

__all__ = [
    "AshbySource",
    "GitHubListingsSource",
    "GreenhouseSource",
    "LeverSource",
    "SmartRecruitersSource",
    "Source",
    "SourceError",
    "SourceFetchError",
    "SourceSchemaError",
    "WorkableSource",
    "WorkdaySource",
    "make_row",
]
