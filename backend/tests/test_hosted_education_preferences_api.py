"""Stage 3 education preferences API (AC2, AC3, AC12).

PostgreSQL API cases using the reconciliation suite's fixtures; they skip
without HOSTED_TEST_DATABASE_URL. Database-free schema cases live in
test_hosted_education_preferences_schema.py. Education is only ever read from
the two explicit fields; no other user field is consulted.
"""

from __future__ import annotations

import pytest
from app.hosted import router as hosted_router
from app.hosted.match_service import reconcile_jobs
from app.hosted.models import HostedJob, UserJobMatch, UserPreference
from sqlalchemy import select
from tests.test_hosted_match_reconciliation import (  # noqa: F401 - pytest fixtures
    catalog,
    clean_match_database,
    create_job,
    hosted,
    make_client,
    match_postgres_url,
    signup,
)

PERSISTED_FIELDS = (
    "role_ids",
    "career_levels",
    "preferred_locations",
    "include_remote",
    "internship_season",
    "alert_frequency",
    "globally_paused",
    "include_recent_openings",
    "education_level",
    "class_standing",
    "updated_at",
)


def payload(**overrides) -> dict:
    """An older-client payload: no education fields unless overridden."""

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


def stored(services) -> dict:
    with services.database.session_factory() as db:
        row = db.scalars(select(UserPreference)).one()
        return {field: getattr(row, field) for field in PERSISTED_FIELDS}


def put(client, **overrides):
    return client.put("/api/preferences", json=payload(**overrides))


@pytest.fixture
def reconcile_calls(monkeypatch) -> list:
    calls: list = []
    original = hosted_router.reconcile_user

    def recording(db, user_id, **kwargs):
        calls.append(user_id)
        return original(db, user_id, **kwargs)

    monkeypatch.setattr(hosted_router, "reconcile_user", recording)
    return calls


def test_default_account_reports_not_specified(hosted, make_client) -> None:
    services, _mailer, _clock = hosted
    client = make_client()
    signup(client, "owner@example.com")

    body = client.get("/api/preferences").json()
    assert body["education_level"] == "not_specified"
    assert body["class_standing"] == "not_specified"
    assert stored(services)["education_level"] == "not_specified"

    saved = put(client)
    assert saved.status_code == 200, saved.text
    assert saved.json()["education_level"] == "not_specified"
    assert saved.json()["class_standing"] == "not_specified"


def test_explicit_values_round_trip_in_every_response(hosted, make_client) -> None:
    client = make_client()
    signup(client, "owner@example.com")

    saved = put(client, education_level="undergraduate", class_standing="junior")
    assert saved.status_code == 200, saved.text
    assert saved.json()["education_level"] == "undergraduate"
    assert saved.json()["class_standing"] == "junior"
    fetched = client.get("/api/preferences").json()
    assert (fetched["education_level"], fetched["class_standing"]) == (
        "undergraduate",
        "junior",
    )


def test_older_client_omitting_both_fields_keeps_stored_values(
    hosted, make_client
) -> None:
    services, _mailer, _clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    put(client, education_level="undergraduate", class_standing="senior")

    saved = put(client, role_ids=["data_science"])
    assert saved.status_code == 200, saved.text
    assert saved.json()["education_level"] == "undergraduate"
    assert saved.json()["class_standing"] == "senior"
    assert stored(services)["role_ids"] == ["data_science"]
    assert stored(services)["class_standing"] == "senior"


def test_standing_only_update_uses_stored_undergraduate_level(
    hosted, make_client
) -> None:
    client = make_client()
    signup(client, "owner@example.com")
    put(client, education_level="undergraduate", class_standing="first_year")

    saved = put(client, class_standing="sophomore")
    assert saved.status_code == 200, saved.text
    assert saved.json()["education_level"] == "undergraduate"
    assert saved.json()["class_standing"] == "sophomore"


def test_standing_only_update_rejected_when_stored_level_is_not_undergraduate(
    hosted, make_client
) -> None:
    services, _mailer, _clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    put(client, education_level="masters")
    before = stored(services)

    rejected = put(client, class_standing="junior", role_ids=["data_science"])
    assert rejected.status_code == 422, rejected.text
    assert stored(services) == before


def test_level_only_transition_away_from_undergraduate_resets_standing(
    hosted, make_client
) -> None:
    services, _mailer, _clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    put(client, education_level="undergraduate", class_standing="junior")

    for level in ("masters", "doctoral", "not_specified"):
        put(client, education_level="undergraduate", class_standing="junior")
        saved = put(client, education_level=level)
        assert saved.status_code == 200, saved.text
        assert saved.json()["education_level"] == level
        assert saved.json()["class_standing"] == "not_specified"
        assert stored(services)["class_standing"] == "not_specified"


def test_level_only_undergraduate_update_keeps_stored_standing(
    hosted, make_client
) -> None:
    client = make_client()
    signup(client, "owner@example.com")
    put(client, education_level="undergraduate", class_standing="junior")

    saved = put(client, education_level="undergraduate")
    assert saved.status_code == 200, saved.text
    assert saved.json()["class_standing"] == "junior"


def test_level_only_transition_to_undergraduate_from_default(
    hosted, make_client
) -> None:
    client = make_client()
    signup(client, "owner@example.com")

    saved = put(client, education_level="undergraduate")
    assert saved.status_code == 200, saved.text
    assert saved.json()["education_level"] == "undergraduate"
    assert saved.json()["class_standing"] == "not_specified"


def test_explicit_reset_to_not_specified(hosted, make_client) -> None:
    client = make_client()
    signup(client, "owner@example.com")
    put(client, education_level="undergraduate", class_standing="senior")

    saved = put(client, education_level="not_specified", class_standing="not_specified")
    assert saved.status_code == 200, saved.text
    assert saved.json()["education_level"] == "not_specified"
    assert saved.json()["class_standing"] == "not_specified"


@pytest.mark.parametrize(
    "education",
    (
        {"education_level": None},
        {"class_standing": None},
        {"education_level": 2},
        {"education_level": ["masters"]},
        {"education_level": "graduate"},
        {"class_standing": "fifth_year"},
        {"class_standing": True},
        {"education_level": "masters", "class_standing": "senior"},
        {"education_level": "doctoral", "class_standing": "first_year"},
        {"education_level": "not_specified", "class_standing": "junior"},
    ),
)
def test_invalid_education_with_valid_changes_persists_nothing(
    hosted, make_client, reconcile_calls, education
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    put(client, education_level="undergraduate", class_standing="sophomore")
    before = stored(services)
    reconcile_calls.clear()
    clock.advance(hours=1)

    rejected = put(
        client,
        role_ids=["data_science"],
        alert_frequency="daily",
        include_remote=False,
        **education,
    )
    assert rejected.status_code == 422, rejected.text
    assert stored(services) == before
    assert reconcile_calls == []


@pytest.mark.parametrize(
    "change",
    (
        {"education_level": "masters"},
        {"class_standing": "junior"},
        {"education_level": "undergraduate", "class_standing": "senior"},
        {"education_level": "not_specified"},
    ),
)
def test_education_change_triggers_reconcile_user(
    hosted, make_client, reconcile_calls, change
) -> None:
    client = make_client()
    user_id = signup(client, "owner@example.com")
    put(client, education_level="undergraduate", class_standing="sophomore")
    reconcile_calls.clear()

    assert put(client, **change).status_code == 200
    assert [str(value) for value in reconcile_calls] == [user_id]


@pytest.mark.parametrize(
    "same",
    (
        {},
        {"education_level": "undergraduate"},
        {"class_standing": "sophomore"},
        {"education_level": "undergraduate", "class_standing": "sophomore"},
    ),
)
def test_unchanged_education_does_not_trigger_reconcile_user(
    hosted, make_client, reconcile_calls, same
) -> None:
    client = make_client()
    signup(client, "owner@example.com")
    put(client, education_level="undergraduate", class_standing="sophomore")
    reconcile_calls.clear()

    assert put(client, **same).status_code == 200
    assert reconcile_calls == []


def eligibility(reason: str) -> dict:
    return {
        "exclusion_reason": reason,
        "degree_level": None,
        "evidence_source": "title",
        "evidence": "synthetic evidence",
    }


def active_titles(client) -> set[str]:
    body = client.get("/api/matches").json()
    return {item["title"] for item in body["items"]}


def test_education_changes_affect_matching_through_the_api(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    client.put(
        "/api/watchlist",
        json={"companies": [{"company_id": "stripe", "paused": False}]},
    )
    assert put(client).status_code == 200
    with services.database.session_factory.begin() as db:
        ids = [
            create_job(
                db, clock, watcher_job_id="w-fresh", title="Freshman Software Intern"
            ),
            create_job(
                db, clock, watcher_job_id="w-grad", title="Graduate Software Intern"
            ),
            create_job(db, clock, watcher_job_id="w-plain", title="Software Intern"),
        ]
        db.get(HostedJob, ids[0]).student_eligibility = eligibility("freshman_only")
        db.get(HostedJob, ids[1]).student_eligibility = eligibility("graduate_only")
        db.flush()
        reconcile_jobs(db, ids, now=clock())
    everything = {
        "Freshman Software Intern",
        "Graduate Software Intern",
        "Software Intern",
    }
    assert active_titles(client) == everything

    clock.advance(hours=1)
    put(client, education_level="undergraduate", class_standing="first_year")
    assert active_titles(client) == {"Freshman Software Intern", "Software Intern"}

    clock.advance(hours=1)
    assert put(client, class_standing="sophomore").status_code == 200
    assert active_titles(client) == {"Software Intern"}

    clock.advance(hours=1)
    assert put(client, class_standing="first_year").status_code == 200
    assert active_titles(client) == {"Freshman Software Intern", "Software Intern"}

    clock.advance(hours=1)
    assert put(client, education_level="masters").status_code == 200
    assert active_titles(client) == everything

    # Rows are kept and reactivated in place, never duplicated or deleted.
    with services.database.session_factory() as db:
        rows = db.scalars(select(UserJobMatch)).all()
    assert len(rows) == 3
    assert all(row.no_longer_matches_at is None for row in rows)
