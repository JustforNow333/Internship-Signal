"""Database-free validation of the Applied fields on ``MatchUpdate``.

The PostgreSQL API behaviour (including the server-now bound, which needs the
service clock) lives in ``test_hosted_match_applied_api``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.hosted.schemas import MATCH_VIEWS, MatchResponse, MatchUpdate


def test_applied_view_and_response_field_are_declared() -> None:
    assert "applied" in MATCH_VIEWS
    for existing in ("active", "saved", "dismissed", "historical", "all"):
        assert existing in MATCH_VIEWS
    assert "applied_at" in MatchResponse.model_fields


@pytest.mark.parametrize(
    "payload",
    [
        {"applied": True},
        {"applied": False},
        {"applied": True, "saved": True},
        {"applied": True, "applied_at": "2026-08-01T10:00:00Z"},
        {"applied": True, "applied_at": "2026-08-01T10:00:00.123456+05:30"},
    ],
)
def test_valid_applied_payloads(payload) -> None:
    MatchUpdate.model_validate(payload)


def test_supplied_applied_at_is_normalized_to_utc() -> None:
    update = MatchUpdate.model_validate(
        {"applied": True, "applied_at": "2026-08-01T09:15:30.123456+02:00"}
    )
    assert update.applied_at == datetime(2026, 8, 1, 7, 15, 30, 123456, tzinfo=UTC)
    assert update.applied_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"applied": None},
        {"applied": "true"},
        {"applied": "yes"},
        {"applied": 1},
        {"applied": 0},
        {"applied_at": "2026-08-01T10:00:00Z"},
        {"applied": False, "applied_at": "2026-08-01T10:00:00Z"},
        {"saved": True, "applied_at": "2026-08-01T10:00:00Z"},
        {"applied": None, "applied_at": "2026-08-01T10:00:00Z"},
        {"applied": True, "applied_at": None},
        {"applied": True, "applied_at": ""},
        {"applied": True, "applied_at": "not a date"},
        {"applied": True, "applied_at": "2026-08-01"},
        {"applied": True, "applied_at": "2026-08-01T10:00:00"},
        {"applied": True, "applied_at": 1785000000},
        {"applied": True, "applied_at": 1785000000.5},
        {"applied": True, "applied_at": "1785000000"},
        {"applied": True, "applied_at": True},
        {"applied": True, "applied_at": ["2026-08-01T10:00:00Z"]},
        {"applied": True, "applied_at": "0001-01-01T00:00:00+05:00"},
        {"applied": True, "applied_at": "2026-08-01T10:00:00Z", "extra": 1},
    ],
)
def test_invalid_applied_payloads_are_rejected(payload) -> None:
    with pytest.raises(ValidationError):
        MatchUpdate.model_validate(payload)
