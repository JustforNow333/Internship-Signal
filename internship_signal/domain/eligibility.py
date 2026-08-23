"""Shared student-eligibility reason codes.

The categorical exclusion reason codes and the set of them are shared: the
backend decides them, and the watcher gate checks membership. The eligibility
engine that produces those decisions stays in `backend.app.eligibility`, which
imports these and re-exports them for existing callers.
"""

from __future__ import annotations

PHD_ONLY = "phd_only"
GRADUATE_ONLY = "graduate_only"
FRESHMAN_ONLY = "freshman_only"
RETURNING_INTERN_ONLY = "returning_intern_only"
CATEGORICAL_EXCLUSION_REASONS = frozenset(
    {PHD_ONLY, GRADUATE_ONLY, FRESHMAN_ONLY, RETURNING_INTERN_ONLY}
)
