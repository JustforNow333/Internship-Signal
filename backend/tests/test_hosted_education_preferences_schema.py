"""Stage 3 education preferences schema validation (AC2, AC3). Database-free."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.hosted import schemas
from pydantic import ValidationError

LEVELS = ("not_specified", "undergraduate", "masters", "doctoral")
STANDINGS = ("not_specified", "first_year", "sophomore", "junior", "senior")
NON_UNDERGRADUATE = ("not_specified", "masters", "doctoral")
BAD_VALUES = (None, 3, True, ["undergraduate"], {"id": "masters"}, "", "phd", "Masters")


def payload(**overrides) -> dict:
    value = {
        "role_ids": ["software_engineering"],
        "preferred_locations": ["New York, NY"],
        "include_remote": True,
        "internship_season": "Any season",
        "alert_frequency": "as_detected",
        "globally_paused": False,
    }
    value.update(overrides)
    return value


def test_update_schema_accepts_every_allowed_level() -> None:
    for level in LEVELS:
        parsed = schemas.PreferencesUpdate(**payload(education_level=level))
        assert parsed.education_level == level


def test_update_schema_accepts_every_standing_with_undergraduate() -> None:
    for standing in STANDINGS:
        parsed = schemas.PreferencesUpdate(
            **payload(education_level="undergraduate", class_standing=standing)
        )
        assert parsed.class_standing == standing


@pytest.mark.parametrize("field", ("education_level", "class_standing"))
@pytest.mark.parametrize("value", BAD_VALUES)
def test_update_schema_rejects_bad_values(field, value) -> None:
    extra = {"education_level": "undergraduate"} if field == "class_standing" else {}
    with pytest.raises(ValidationError):
        schemas.PreferencesUpdate(**payload(**extra, **{field: value}))


@pytest.mark.parametrize("level", NON_UNDERGRADUATE)
@pytest.mark.parametrize("standing", STANDINGS[1:])
def test_update_schema_rejects_standing_without_undergraduate(level, standing) -> None:
    with pytest.raises(ValidationError):
        schemas.PreferencesUpdate(
            **payload(education_level=level, class_standing=standing)
        )


def test_update_schema_tracks_omission() -> None:
    parsed = schemas.PreferencesUpdate(**payload())
    assert "education_level" not in parsed.model_fields_set
    assert "class_standing" not in parsed.model_fields_set
    explicit = schemas.PreferencesUpdate(
        **payload(education_level="not_specified", class_standing="not_specified")
    )
    assert {"education_level", "class_standing"} <= explicit.model_fields_set


def test_standing_only_update_parses_for_later_merge() -> None:
    # The stored level decides compatibility, so the schema alone accepts it.
    parsed = schemas.PreferencesUpdate(**payload(class_standing="junior"))
    assert parsed.class_standing == "junior"
    assert "education_level" not in parsed.model_fields_set


def test_response_schema_always_serializes_education_fields() -> None:
    now = datetime(2026, 11, 22, tzinfo=UTC)
    response = schemas.PreferencesResponse(**payload(), created_at=now, updated_at=now)
    dumped = response.model_dump()
    assert dumped["education_level"] == "not_specified"
    assert dumped["class_standing"] == "not_specified"
