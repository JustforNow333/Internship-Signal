"""The recognizable-tech coverage milestone, pinned by identity for tests.

The watchlist grows by appending expansion universes (finance employers, and
whatever follows). Those batches must never reach back into the tech universe
that was already complete, so the milestone is pinned here as the exact set of
company names it contained, captured from the last watchlist revision before
any expansion batch existed.

Identity is the durable invariant, not arithmetic: a count of the whole
watchlist has to be restated by every later batch, and a `>=` bound would let
an original company quietly disappear. Membership does neither -- appending a
new batch cannot affect it, and removing or renaming a tech company fails
immediately.

Only membership is pinned. A tech company's adapter may still legitimately
change (a backstop entry earning a direct source, for instance), and pinning
per-company configuration here would fight that.

This module is a test helper, not an importable production surface; it is
deliberately not named ``test_*`` so pytest does not collect it.
"""

from __future__ import annotations

import json
from pathlib import Path


_FIXTURE = Path(__file__).parent / "fixtures" / "tech_universe_companies.json"

TECH_UNIVERSE_COMPANY_NAMES: frozenset[str] = frozenset(
    json.loads(_FIXTURE.read_text(encoding="utf-8"))
)

TECH_UNIVERSE_COMPANY_COUNT = 251


def assert_tech_universe_is_intact(configured_names: set[str]) -> None:
    """Fail unless every company in the milestone is still configured.

    `configured_names` is the full set of watchlist company names. Extra names
    are expected -- they are the expansion batches -- so only the milestone's
    own membership is checked.
    """

    assert len(TECH_UNIVERSE_COMPANY_NAMES) == TECH_UNIVERSE_COMPANY_COUNT
    missing = TECH_UNIVERSE_COMPANY_NAMES - configured_names
    assert not missing, f"tech-universe companies went missing: {sorted(missing)}"


def assert_batch_is_additive(batch_names: tuple[str, ...], configured_names: set[str]) -> None:
    """Fail unless `batch_names` adds to the milestone instead of reaching into it."""

    assert set(batch_names) <= configured_names
    overlap = set(batch_names) & TECH_UNIVERSE_COMPANY_NAMES
    assert not overlap, f"expansion batch claimed tech-universe companies: {sorted(overlap)}"
    assert_tech_universe_is_intact(configured_names)
