"""Onboarding and settings persist the new role category independently."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_hosted_postgres import (  # noqa: F401 - pytest fixtures
    clean_database,
    hosted,
    postgres_url,
    preferences_payload,
    signup,
)


@pytest.mark.parametrize(
    "role_ids",
    [
        ["quantitative_trading_research"],
        ["data_science"],
        ["data_science", "quantitative_trading_research"],
        ["quantitative_development"],
    ],
)
def test_role_categories_round_trip_through_preferences(hosted, role_ids) -> None:
    with TestClient(app) as client:
        signup(client)
        saved = client.put("/api/preferences", json=preferences_payload(role_ids=role_ids))

        assert saved.status_code == 200, saved.text
        assert client.get("/api/preferences").json()["role_ids"] == role_ids


def test_signup_default_does_not_select_new_categories(hosted) -> None:
    with TestClient(app) as client:
        signup(client)

        role_ids = client.get("/api/preferences").json()["role_ids"]

    assert "quantitative_trading_research" not in role_ids
    assert "data_science" not in role_ids


def test_unknown_role_category_is_rejected_and_preferences_kept(hosted) -> None:
    with TestClient(app) as client:
        signup(client)
        client.put("/api/preferences", json=preferences_payload(role_ids=["data_science"]))

        rejected = client.put(
            "/api/preferences",
            json=preferences_payload(role_ids=["quantitative_trading"]),
        )

        assert rejected.status_code == 422
        assert client.get("/api/preferences").json()["role_ids"] == ["data_science"]
