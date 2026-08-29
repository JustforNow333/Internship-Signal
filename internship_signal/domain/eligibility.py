"""Shared categorical student-eligibility reason codes."""

from __future__ import annotations

PHD_ONLY = "phd_only"
GRADUATE_ONLY = "graduate_only"
FRESHMAN_ONLY = "freshman_only"
RETURNING_INTERN_ONLY = "returning_intern_only"
CATEGORICAL_EXCLUSION_REASONS = frozenset(
    {PHD_ONLY, GRADUATE_ONLY, FRESHMAN_ONLY, RETURNING_INTERN_ONLY}
)
