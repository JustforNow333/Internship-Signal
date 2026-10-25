"""The split-out role tracks score and filter exactly like the tracks they left.

``data_science`` was classified as ``data_engineering`` and ``quant_research``
as ``quant_dev``. Only their hosted role routing changes, so every watcher and
scoring table must treat each new track exactly like its parent.
"""

import pytest

from app import ask, profile, scoring, signals
from watcher import eligibility, notify

SPLITS = [("data_science", "data_engineering"), ("quant_research", "quant_dev")]

TRACK_SETS = {
    "ask.BACKEND_ADJACENT_TRACKS": ask.BACKEND_ADJACENT_TRACKS,
    "scoring.SOFTWARE_FIT_TRACKS": scoring.SOFTWARE_FIT_TRACKS,
    "scoring.NEAR_PERFECT_TRACKS": scoring.NEAR_PERFECT_TRACKS,
    "signals.SOFTWARE_SIGNAL_TRACKS": signals.SOFTWARE_SIGNAL_TRACKS,
    "signals.BACKEND_FOCUS_TRACKS": signals.BACKEND_FOCUS_TRACKS,
    "eligibility.SWE_TARGET_TRACKS": eligibility.SWE_TARGET_TRACKS,
}
TRACK_WEIGHTS = {
    "scoring.ROLE_TRACK_PRIORITY": scoring.ROLE_TRACK_PRIORITY,
    "notify.ROLE_TRACK_SORT_PRIORITY": notify.ROLE_TRACK_SORT_PRIORITY,
    "profile.role_track_affinity": profile.DEFAULT_PROFILE["role_track_affinity"],
}


@pytest.mark.parametrize(("track", "parent"), SPLITS)
@pytest.mark.parametrize("name", sorted(TRACK_SETS))
def test_split_track_shares_parent_set_membership(name, track, parent):
    members = TRACK_SETS[name]

    assert (track in members) == (parent in members)


@pytest.mark.parametrize(("track", "parent"), SPLITS)
@pytest.mark.parametrize("name", sorted(TRACK_WEIGHTS))
def test_split_track_shares_parent_weight(name, track, parent):
    weights = TRACK_WEIGHTS[name]

    assert weights.get(track) == weights.get(parent)
