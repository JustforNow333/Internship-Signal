"""Compatibility imports for the former configuration implementation module.

Validation now lives in :mod:`watcher.config.validation`. This module remains
for one final facade-cleanup commit because existing tests and any transitional
callers may still import its historical private seams.
"""

from watcher.config.validation import (
    COVERAGE_STATUS_NO_SOURCE_FOUND,
    MAX_PLATFORM_FAMILY_LENGTH,
    NON_DIRECT_ATS,
    SUPPORTED_COVERAGE_STATUSES,
    SUPPORTED_GITHUB_LISTING_FORMATS,
    _HOSTNAME_LABEL,
    _platform_family,
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
    "COVERAGE_STATUS_NO_SOURCE_FOUND",
    "MAX_PLATFORM_FAMILY_LENGTH",
    "NON_DIRECT_ATS",
    "SUPPORTED_COVERAGE_STATUSES",
    "SUPPORTED_GITHUB_LISTING_FORMATS",
    "is_valid_hostname",
    "supported_ats",
]
