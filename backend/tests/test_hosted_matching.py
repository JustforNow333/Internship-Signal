"""Offline tests for the pure hosted match decision."""

from __future__ import annotations

import pytest
from app.hosted.matching import (
    MAX_REASONS,
    REASON_CODES,
    MatchJob,
    MatchPreferences,
    bounded_reasons,
    evaluate_match,
    is_remote,
    job_from_model,
    preferences_from_model,
)


def job(**overrides) -> MatchJob:
    values = {
        "company_id": "stripe",
        "role_id": "software_engineering",
        "title": "Software Engineering Intern",
        "location": "New York, NY",
        "remote_status": "",
        "description": "",
        "is_open": True,
    }
    values.update(overrides)
    return MatchJob(**values)


def preferences(**overrides) -> MatchPreferences:
    values = {
        "role_ids": frozenset({"software_engineering"}),
        "preferred_locations": ("New York, NY",),
        "include_remote": True,
        "internship_season": "Any season",
    }
    values.update(overrides)
    return MatchPreferences(**values)


def decide(job_value=None, preference_value=None, *, watching=True, paused=False):
    return evaluate_match(
        job_value or job(),
        preference_value or preferences(),
        watching=watching,
        watch_paused=paused,
    )


def codes(decision) -> list[str]:
    return [reason["code"] for reason in decision.reasons]


def test_watched_unpaused_company_matches() -> None:
    decision = decide()
    assert decision.matches is True
    assert "company_watched" in codes(decision)


def test_unwatched_company_does_not_match() -> None:
    assert decide(watching=False).matches is False


def test_paused_company_watch_does_not_match() -> None:
    assert decide(paused=True).matches is False


def test_closed_job_does_not_match() -> None:
    assert decide(job(is_open=False)).matches is False


def test_unselected_role_does_not_match() -> None:
    assert decide(job(role_id="data_science")).matches is False


def test_selected_role_matches() -> None:
    decision = decide(
        job(role_id="data_science"),
        preferences(role_ids=frozenset({"data_science"})),
    )
    assert decision.matches is True
    assert {"code": "role_selected", "value": "data_science"} in decision.reasons


def test_no_preferred_locations_accepts_any_location() -> None:
    decision = decide(
        job(location="Austin, TX"),
        preferences(preferred_locations=()),
    )
    assert decision.matches is True
    assert "location_any" in codes(decision)


def test_unmatched_physical_location_does_not_match() -> None:
    assert decide(job(location="Austin, TX")).matches is False


def test_remote_job_matches_when_remote_is_included() -> None:
    decision = decide(job(location="Austin, TX", remote_status="Remote"))
    assert decision.matches is True
    assert "remote_included" in codes(decision)


def test_remote_job_does_not_match_when_remote_is_excluded() -> None:
    decision = decide(
        job(location="Austin, TX", remote_status="Remote"),
        preferences(include_remote=False),
    )
    assert decision.matches is False


def test_united_states_preference_uses_the_shared_location_gate() -> None:
    decision = decide(
        job(location="Austin, TX"),
        preferences(preferred_locations=("United States",)),
    )
    assert decision.matches is True
    assert "location_united_states" in codes(decision)


def test_united_states_preference_rejects_explicitly_foreign_locations() -> None:
    decision = decide(
        job(location="Berlin, Germany"),
        preferences(preferred_locations=("United States",)),
    )
    assert decision.matches is False


def test_country_abbreviation_does_not_substring_match_a_city() -> None:
    """``us`` must not match inside ``Austin`` via naive substring logic."""

    decision = decide(
        job(location="Austin, TX"),
        preferences(preferred_locations=("US",)),
    )
    # Reached only through the country gate, never through token containment.
    assert "location_preferred" not in codes(decision)


def test_matching_season_matches() -> None:
    decision = decide(
        job(title="Software Engineering Intern, Summer 2027"),
        preferences(internship_season="Summer 2027"),
    )
    assert decision.matches is True
    assert "season_match" in codes(decision)


def test_conflicting_season_term_does_not_match() -> None:
    decision = decide(
        job(title="Fall 2027 Software Engineering Co-op"),
        preferences(internship_season="Summer 2027"),
    )
    assert decision.matches is False


def test_conflicting_season_year_does_not_match() -> None:
    decision = decide(
        job(title="Summer 2026 Software Engineering Intern"),
        preferences(internship_season="Summer 2027"),
    )
    assert decision.matches is False


def test_missing_season_evidence_stays_compatible() -> None:
    decision = decide(
        job(title="Software Engineering Intern"),
        preferences(internship_season="Summer 2027"),
    )
    assert decision.matches is True
    assert "season_unspecified" in codes(decision)


def test_any_season_preference_accepts_every_posting() -> None:
    decision = decide(
        job(title="Fall 2026 Software Engineering Co-op"),
        preferences(internship_season="Any season"),
    )
    assert decision.matches is True
    assert "season_any" in codes(decision)


def test_reasons_are_deterministic_bounded_and_allowlisted() -> None:
    first = decide()
    second = decide()
    assert first.reasons == second.reasons
    assert len(first.reasons) <= MAX_REASONS
    assert all(reason["code"] in REASON_CODES for reason in first.reasons)
    # Reasons carry only catalog identifiers, never descriptions or preferences.
    assert all(set(reason) <= {"code", "value"} for reason in first.reasons)


def test_reason_order_is_company_role_location_then_season() -> None:
    assert codes(decide()) == [
        "company_watched",
        "role_selected",
        "location_preferred",
        "season_any",
    ]


@pytest.mark.parametrize("frequency", ["as_detected", "three_hour", "daily", "paused"])
def test_alert_frequency_never_changes_match_existence(frequency: str) -> None:
    """Alert frequency is Phase 3 delivery state and is not a match input."""

    assert not hasattr(preferences(), "alert_frequency")
    assert decide().matches is True


def test_global_notification_pause_never_changes_match_existence() -> None:
    class PausedPreferenceRow:
        role_ids = ["software_engineering"]
        preferred_locations = ["New York, NY"]
        include_remote = True
        internship_season = "Any season"
        alert_frequency = "paused"
        globally_paused = True

    pure = preferences_from_model(PausedPreferenceRow())
    assert not hasattr(pure, "globally_paused")
    assert evaluate_match(job(), pure, watching=True, watch_paused=False).matches


def test_bounded_reasons_drops_unknown_codes_and_oversized_payloads() -> None:
    stored = [
        {"code": "company_watched", "value": "stripe"},
        {"code": "not_a_real_code", "value": "x"},
        {"code": "role_selected", "value": "y" * 500},
        "not-an-object",
    ]
    result = bounded_reasons(stored)
    assert [reason["code"] for reason in result] == [
        "company_watched",
        "role_selected",
    ]
    assert len(result[1]["value"]) == 120


def test_bounded_reasons_tolerates_malformed_storage() -> None:
    assert bounded_reasons(None) == ()
    assert bounded_reasons("string") == ()
    assert bounded_reasons({"code": "company_watched"}) == ()


def test_model_adapters_read_only_normalized_fields() -> None:
    class JobRow:
        company_id = "figma"
        role_id = "data_science"
        title = "Data Science Intern"
        location = "Remote - United States"
        remote_status = "Remote"
        description = ""
        is_open = True

    mapped = job_from_model(JobRow())
    assert mapped.company_id == "figma"
    assert is_remote(mapped) is True
