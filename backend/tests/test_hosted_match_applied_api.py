"""PostgreSQL API tests for the persistent Applied match status.

``PATCH /api/matches/{id}`` accepts a strict-boolean ``applied`` and, only with
``applied=true``, an optional timezone-aware ISO ``applied_at`` no later than
server now (used by Undo). Applied is independent of saved/dismissed and of the
matching metadata, and ``GET /api/matches?view=applied`` lists every applied
match the caller owns regardless of its other states.

These need ``HOSTED_TEST_DATABASE_URL`` and skip without it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.hosted.match_service import reconcile_jobs
from app.hosted.models import HostedJob, UserCompanyWatch, UserJobMatch
from tests.test_hosted_match_reconciliation import (  # noqa: F401 - pytest fixtures
    catalog,
    clean_match_database,
    create_job,
    create_user,
    hosted,
    make_client,
    match_postgres_url,
    signup,
)

SNAPSHOT_FIELDS = (
    "saved_at",
    "dismissed_at",
    "applied_at",
    "matched_at",
    "last_matched_at",
    "no_longer_matches_at",
    "match_reasons",
)


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _watch_stripe(db, user_id, now) -> None:
    db.add(
        UserCompanyWatch(
            user_id=user_id,
            company_id="stripe",
            paused=False,
            created_at=now,
            updated_at=now,
        )
    )


def _signed_in_match(services, clock, client, email: str = "owner@example.com"):
    user_id = uuid.UUID(signup(client, email))
    with services.database.session_factory.begin() as db:
        _watch_stripe(db, user_id, clock())
        job_id = create_job(db, clock)
        db.flush()
        reconcile_jobs(db, [job_id], now=clock())
    match_id = client.get("/api/matches").json()["items"][0]["id"]
    return user_id, job_id, match_id


def _snapshot(services, match_id) -> dict:
    with services.database.session_factory() as db:
        row = db.get(UserJobMatch, uuid.UUID(str(match_id)))
        return {field: getattr(row, field) for field in SNAPSHOT_FIELDS}


def _patch(client, match_id, payload):
    return client.patch(f"/api/matches/{match_id}", json=payload)


# --- response ---------------------------------------------------------------


def test_match_responses_include_applied_at(hosted, make_client) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, client)

    listed = client.get("/api/matches").json()["items"][0]
    assert "applied_at" in listed and listed["applied_at"] is None
    detail = client.get(f"/api/matches/{match_id}").json()
    assert "applied_at" in detail and detail["applied_at"] is None


# --- marking, idempotence, unmarking, Undo -----------------------------------


def test_mark_applied_sets_server_now_and_repeat_keeps_the_original(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, client)
    clock.advance(hours=1)
    marked_at = clock()

    first = _patch(client, match_id, {"applied": True})
    assert first.status_code == 200, first.text
    assert _parse(first.json()["applied_at"]) == marked_at
    assert _snapshot(services, match_id)["applied_at"] == marked_at

    clock.advance(days=2)
    again = _patch(client, match_id, {"applied": True})
    assert again.status_code == 200, again.text
    assert _parse(again.json()["applied_at"]) == marked_at
    assert _snapshot(services, match_id)["applied_at"] == marked_at
    detail = client.get(f"/api/matches/{match_id}").json()
    assert _parse(detail["applied_at"]) == marked_at


def test_unmark_clears_and_undo_restores_the_exact_original_timestamp(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, client)
    clock.advance(minutes=5)
    original = _patch(client, match_id, {"applied": True}).json()["applied_at"]
    original_at = _parse(original)

    clock.advance(days=3)
    cleared = _patch(client, match_id, {"applied": False})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["applied_at"] is None
    assert _snapshot(services, match_id)["applied_at"] is None

    # Unmarking an unapplied match is a harmless no-op.
    assert _patch(client, match_id, {"applied": False}).json()["applied_at"] is None

    restored = _patch(client, match_id, {"applied": True, "applied_at": original})
    assert restored.status_code == 200, restored.text
    assert _parse(restored.json()["applied_at"]) == original_at
    assert _snapshot(services, match_id)["applied_at"] == original_at


def test_supplied_applied_at_replaces_and_preserves_microseconds_and_offsets(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, client)
    _patch(client, match_id, {"applied": True})

    supplied = "2026-08-01T09:15:30.123456+02:00"
    expected = datetime(2026, 8, 1, 7, 15, 30, 123456, tzinfo=UTC)
    response = _patch(client, match_id, {"applied": True, "applied_at": supplied})
    assert response.status_code == 200, response.text
    assert _parse(response.json()["applied_at"]) == expected
    assert _snapshot(services, match_id)["applied_at"] == expected

    # "Z" suffix and exactly server-now are both accepted.
    now_iso = clock().isoformat().replace("+00:00", "Z")
    at_now = _patch(client, match_id, {"applied": True, "applied_at": now_iso})
    assert at_now.status_code == 200, at_now.text
    assert _parse(at_now.json()["applied_at"]) == clock()


# --- validation ---------------------------------------------------------------


def test_applied_payload_validation_rejects_invalid_combinations_and_values(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, client)
    clock.advance(hours=1)
    _patch(client, match_id, {"applied": True, "applied_at": "2026-08-02T10:00:00Z"})
    before = _snapshot(services, match_id)

    past = "2026-08-01T10:00:00Z"
    future = (clock() + timedelta(seconds=1)).isoformat()
    invalid_payloads = (
        {},
        {"applied": "true"},
        {"applied": "yes"},
        {"applied": 1},
        {"applied": 0},
        {"applied": None},
        {"applied_at": past},
        {"applied": False, "applied_at": past},
        {"saved": True, "applied_at": past},
        {"dismissed": False, "applied_at": past},
        {"applied": True, "applied_at": None},
        {"applied": True, "applied_at": ""},
        {"applied": True, "applied_at": "not a date"},
        {"applied": True, "applied_at": "2026-08-01"},
        {"applied": True, "applied_at": "2026-08-01T10:00:00"},
        {"applied": True, "applied_at": 1785000000},
        {"applied": True, "applied_at": 1785000000.5},
        {"applied": True, "applied_at": "1785000000"},
        {"applied": True, "applied_at": True},
        {"applied": True, "applied_at": future},
        {"applied": True, "applied_at": "2999-01-01T00:00:00Z"},
        {"applied": True, "applied_at": {"date": past}},
        {"saved": True, "applied": True, "applied_at": future},
    )
    for payload in invalid_payloads:
        response = _patch(client, match_id, payload)
        assert response.status_code == 422, (payload, response.text)
    assert _snapshot(services, match_id) == before


def test_applied_alone_is_a_valid_action_and_combines_with_save(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, client)

    both = _patch(client, match_id, {"saved": True, "applied": True})
    assert both.status_code == 200, both.text
    assert both.json()["saved_at"] is not None
    assert both.json()["applied_at"] is not None


# --- authorization --------------------------------------------------------------


def test_applied_patch_requires_authentication(hosted, make_client) -> None:
    services, _mailer, clock = hosted
    owner = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, owner)
    anonymous = make_client()

    assert _patch(anonymous, match_id, {"applied": True}).status_code == 401
    assert (
        _patch(
            anonymous, match_id, {"applied": True, "applied_at": "2026-08-01T00:00:00Z"}
        ).status_code
        == 401
    )
    assert anonymous.get("/api/matches?view=applied").status_code == 401
    assert _snapshot(services, match_id)["applied_at"] is None


def test_applied_patch_on_another_users_match_is_404_and_changes_nothing(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    owner = make_client()
    other = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, owner)
    signup(other, "other@example.com")
    _patch(owner, match_id, {"applied": True})
    before = _snapshot(services, match_id)

    for payload in (
        {"applied": True},
        {"applied": False},
        {"applied": True, "applied_at": "2026-08-01T00:00:00Z"},
    ):
        assert _patch(other, match_id, payload).status_code == 404, payload
    assert _snapshot(services, match_id) == before
    assert other.get("/api/matches?view=applied").json()["total"] == 0


# --- independence ---------------------------------------------------------------


def test_applied_changes_never_touch_saved_dismissed_or_matching_metadata(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, client)
    clock.advance(minutes=1)
    _patch(client, match_id, {"saved": True})
    clock.advance(minutes=1)
    _patch(client, match_id, {"dismissed": True})
    before = _snapshot(services, match_id)
    assert before["saved_at"] is not None and before["dismissed_at"] is not None

    for payload in (
        {"applied": True},
        {"applied": True},
        {"applied": False},
        {"applied": True, "applied_at": "2026-08-01T00:00:00Z"},
    ):
        clock.advance(minutes=1)
        assert _patch(client, match_id, payload).status_code == 200, payload
        after = _snapshot(services, match_id)
        for field in SNAPSHOT_FIELDS:
            if field != "applied_at":
                assert after[field] == before[field], (payload, field)

    # The same holds for a match with neither save nor dismiss.
    with services.database.session_factory.begin() as db:
        row = db.get(UserJobMatch, uuid.UUID(match_id))
        row.saved_at = None
        row.dismissed_at = None
    before = _snapshot(services, match_id)
    clock.advance(minutes=1)
    _patch(client, match_id, {"applied": False})
    _patch(client, match_id, {"applied": True})
    after = _snapshot(services, match_id)
    assert after["saved_at"] is None and after["dismissed_at"] is None
    for field in ("matched_at", "last_matched_at", "no_longer_matches_at"):
        assert after[field] == before[field]
    assert after["match_reasons"] == before["match_reasons"]


def test_save_and_dismiss_changes_never_touch_applied_at(hosted, make_client) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _user_id, _job_id, match_id = _signed_in_match(services, clock, client)
    applied = _patch(
        client, match_id, {"applied": True, "applied_at": "2026-08-01T08:00:00Z"}
    ).json()["applied_at"]
    applied_at = _parse(applied)

    for payload in (
        {"saved": True},
        {"dismissed": True},
        {"saved": False},
        {"dismissed": False},
        {"saved": True, "dismissed": True},
        {"saved": False, "dismissed": False},
    ):
        clock.advance(minutes=1)
        response = _patch(client, match_id, payload)
        assert response.status_code == 200, payload
        assert _parse(response.json()["applied_at"]) == applied_at, payload
        assert _snapshot(services, match_id)["applied_at"] == applied_at, payload

    # And an unapplied match stays unapplied through save/dismiss.
    _patch(client, match_id, {"applied": False})
    for payload in ({"saved": True}, {"dismissed": True}, {"dismissed": False}):
        response = _patch(client, match_id, payload)
        assert response.json()["applied_at"] is None, payload
        assert _snapshot(services, match_id)["applied_at"] is None, payload


# --- view=applied ---------------------------------------------------------------


def _seed_listing(services, clock, client):
    """Seed the caller's matches across states plus another user's applied match.

    Returns the caller's match ids keyed by label.
    """
    user_id = uuid.UUID(signup(client, "owner@example.com"))
    labels = ("active", "dismissed", "closed", "unapplied", "saved")
    job_ids: dict[str, uuid.UUID] = {}
    with services.database.session_factory.begin() as db:
        _watch_stripe(db, user_id, clock())
        other_id = create_user(db, clock, "other@example.com")
        db.flush()
        for index, label in enumerate(labels):
            clock.advance(minutes=1)
            job_ids[label] = create_job(db, clock, watcher_job_id=f"watcher-{index}")
            reconcile_jobs(db, [job_ids[label]], now=clock())
    with services.database.session_factory() as db:
        rows = db.execute(
            select(UserJobMatch.id, UserJobMatch.job_id, UserJobMatch.user_id)
        ).all()
    owned = {r.job_id: r.id for r in rows if r.user_id == user_id}
    ids = {label: str(owned[job_id]) for label, job_id in job_ids.items()}
    other_ids = [r.id for r in rows if r.user_id == other_id]
    assert len(other_ids) == len(labels)

    for label in ("active", "dismissed", "closed", "saved"):
        assert _patch(client, ids[label], {"applied": True}).status_code == 200
    _patch(client, ids["dismissed"], {"dismissed": True})
    _patch(client, ids["saved"], {"saved": True})
    _patch(client, ids["unapplied"], {"saved": True})
    with services.database.session_factory.begin() as db:
        db.get(HostedJob, job_ids["closed"]).is_open = False
        clock.advance(minutes=1)
        reconcile_jobs(db, [job_ids["closed"]], now=clock())
        for other_match_id in other_ids:
            db.get(UserJobMatch, other_match_id).applied_at = clock()
    return ids


def test_applied_view_lists_every_applied_match_the_caller_owns(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    ids = _seed_listing(services, clock, client)
    assert _snapshot(services, ids["closed"])["no_longer_matches_at"] is not None
    assert _snapshot(services, ids["closed"])["applied_at"] is not None

    applied = client.get("/api/matches?view=applied").json()
    assert set(applied) == {"items", "limit", "offset", "total", "has_more"}
    assert applied["total"] == 4
    assert applied["limit"] == 50 and applied["offset"] == 0
    assert applied["has_more"] is False
    listed = [item["id"] for item in applied["items"]]
    assert set(listed) == {ids["active"], ids["dismissed"], ids["closed"], ids["saved"]}
    assert all(item["applied_at"] is not None for item in applied["items"])

    # Same ordering as the other views (newest matched first).
    every = [item["id"] for item in client.get("/api/matches?view=all").json()["items"]]
    assert listed == [match_id for match_id in every if match_id in set(listed)]
    assert listed == [ids["saved"], ids["closed"], ids["dismissed"], ids["active"]]


def test_applied_view_paginates_with_the_existing_envelope(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    _seed_listing(services, clock, client)
    full = [
        item["id"]
        for item in client.get("/api/matches?view=applied").json()["items"]
    ]

    first = client.get("/api/matches?view=applied&limit=3&offset=0").json()
    assert first["total"] == 4 and first["limit"] == 3 and first["offset"] == 0
    assert first["has_more"] is True
    second = client.get("/api/matches?view=applied&limit=3&offset=3").json()
    assert second["total"] == 4 and second["offset"] == 3
    assert second["has_more"] is False
    paged = [item["id"] for item in first["items"] + second["items"]]
    assert paged == full
    beyond = client.get("/api/matches?view=applied&limit=3&offset=6").json()
    assert beyond["items"] == [] and beyond["total"] == 4


def test_other_views_are_unchanged_by_applied_state(hosted, make_client) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    ids = _seed_listing(services, clock, client)

    def view(name: str) -> set[str]:
        return {
            item["id"]
            for item in client.get(f"/api/matches?view={name}").json()["items"]
        }

    # Applied matches stay in Active while active and undismissed.
    assert view("active") == {ids["active"], ids["unapplied"], ids["saved"]}
    assert client.get("/api/matches").json()["total"] == 3
    assert view("saved") == {ids["saved"], ids["unapplied"]}
    assert view("dismissed") == {ids["dismissed"]}
    assert view("historical") == {ids["closed"]}
    assert view("all") == set(ids.values())

    # Unmarking removes a match only from the Applied view.
    _patch(client, ids["active"], {"applied": False})
    assert ids["active"] not in view("applied")
    assert ids["active"] in view("active")
