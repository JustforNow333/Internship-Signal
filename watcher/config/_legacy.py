"""Compatibility imports for the former configuration implementation module.

Validation now lives in :mod:`watcher.config.validation`. This module remains
through the final facade-cleanup stage so transitional callers can keep using
the historical validation seams without creating a second implementation.
"""

from watcher.config.env import ConfigError
from watcher.config.validation import (
    NON_DIRECT_ATS,
    SUPPORTED_GITHUB_LISTING_FORMATS,
    _HOSTNAME_LABEL,
    _validate_github_source_uniqueness,
    _validate_icims_config,
    _validate_oracle_hcm_config,
    _validate_paylocity_config,
    _validate_successfactors_config,
    _validate_talentbrew_config,
    _validate_unique_company_names,
    _validated_feed_url,
    is_valid_hostname,
    supported_ats,
)

__all__ = [
    "NON_DIRECT_ATS",
    "SUPPORTED_GITHUB_LISTING_FORMATS",
    "is_valid_hostname",
    "supported_ats",
]
