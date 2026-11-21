"""PostgreSQL lifecycle and notification invariants for the Applied match status.

Reconciliation (deactivation, reactivation, reason refresh) must preserve the
exact ``applied_at``; marking/unmarking Applied must never create, cancel or
change notification rows; reactivating an applied match through an import must
enqueue nothing; and account deletion still removes the user's matches.

These need ``HOSTED_TEST_DATABASE_URL`` and skip without it.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from app.hosted.dependencies import CSRF_HEADER
from app.hosted.job_import import JobImportService
from app.hosted.models import HostedJob, HostedNotificationItem, User, UserJobMatch
from app.hosted.notification_mail import DeliveryResult, NotificationEmail
from app.hosted.notification_worker import NotificationDeliveryWorker
from sqlalchemy import func, select, text
from tests.test_hosted_match_reconciliation import (  # noqa: F401 - pytest fixtures
    catalog,
    clean_match_database,
    hosted,
    make_client,
    match_postgres_url,
)

PASSWORD = "secure password"
NOTIFICATION_TABLES = (
    "hosted_notification_batches",
    "hosted_notification_items",
    "hosted_notification_attempts",
    "hosted_catch_up_requests",
)
MATCH_FIELDS = (
    "applied_at",
    "saved_at",
    "dismissed_at",
    "matched_at",
    "last_matched_at",
    "no_longer_matches_at",
    "match_reasons",
)


class RecordingTransport:
    def __init__(self) -> None:
        self.messages: list[NotificationEmail] = []

    def send(self, message: NotificationEmail) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult("sent")


def _preferences(**overrides) -> dict:
    value = {
        "role_ids": ["software_engineering"],
        "career_levels": ["internship"],
        "preferred_locations": ["New York, NY"],
        "include_remote": True,
        "internship_season": "Any season",
        "alert_frequency": "as_detected",
        "globally_paused": False,
        # Off, so no catch-up requests are created: every notification row in
        # these tests comes from a genuinely new opening.
        "include_recent_openings": False,
    }
    value.update(overrides)
    return value


def _account(services, mailer, make_client, email: str, **prefs):
    """A verified, signed-in account watching Stripe; returns (client, user_id)."""

    client = make_client()
    response = client.post("/api/auth/signup", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    user_id = uuid.UUID(response.json()["user"]["id"])
    message = next(m for m in reversed(mailer.messages) if m.recipient == email)
    url = next(line for line in message.text.splitlines() if line.startswith("http"))
    token = parse_qs(urlsplit(url).query)["token"][0]
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    _put_preferences(client, **prefs)
    _put_watch(client, paused=False)
    return client, user_id


def _put_preferences(client, **prefs) -> None:
    response = client.put("/api/preferences", json=_preferences(**prefs))
    assert response.status_code == 200, response.text


def _put_watch(client, *, paused: bool | None) -> None:
    companies = [] if paused is None else [{"company_id": "stripe", "paused": paused}]
    response = client.put("/api/watchlist", json={"companies": companies})
    assert response.status_code == 200, response.text


def _job(index: int = 1, **overrides) -> dict:
    value = {
        "id": f"watcher-job-{index}",
        "company": "Stripe",
        "title": (
            "Backend Software Engineer Intern"
            if index == 1
            else "Platform Software Engineer Intern"
        ),
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "description": "Build production APIs.",
        "requirements": "Python and SQL",
        "source_url": f"https://example.com/jobs/{index}",
        # Posted on the watch's first day, so the first import is a genuinely
        # new opening and enqueues a real notification item.
        "date_posted": "2026-08-03",
        "deadline": "2026-09-01",
        "deadline_days_left": 30,
        "internship_type": "Summer 2027 Internship",
        "role_classification": {"role": "swe", "role_track": "backend"},
        "extra": {"source": "direct", "source_adapter": "workday"},
    }
    value.update(overrides)
    return value


def _import(services, catalog, clock, jobs: list[dict], marker: str):
    return JobImportService(services.database, catalog, clock=clock).import_jobs(
        jobs,
        source_fingerprint=marker * 64,
        source_identifier=f"snapshot-{marker}.json.gz",
        source_type="collection_snapshot",
    )


def _match_id(services, user_id, index: int = 1) -> str:
    with services.database.session_factory() as db:
        return str(
            db.scalar(
                select(UserJobMatch.id)
                .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
                .where(
                    UserJobMatch.user_id == user_id,
                    HostedJob.watcher_job_id == f"watcher-job-{index}",
                )
            )
        )


def _state(services, match_id) -> dict:
    with services.database.session_factory() as db:
        row = db.get(UserJobMatch, uuid.UUID(match_id))
        return {field: getattr(row, field) for field in MATCH_FIELDS}


def _instant(value: str) -> datetime:
    # The API may render the same instant with a different UTC offset (the
    # database session's time zone), so compare instants, not text.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _reason_codes(state: dict) -> list[str]:
    return [reason["code"] for reason in state["match_reasons"]]


def _notification_state(services) -> dict[str, list[str]]:
    """Every column of every notification-owned row, keyed by table."""

    snapshot: dict[str, list[str]] = {}
    with services.database.engine.connect() as connection:
        for table in NOTIFICATION_TABLES:
            rows = connection.execute(
                text(f'SELECT row_to_json(t)::text FROM "{table}" t')
            ).scalars()
            snapshot[table] = sorted(rows)
    return snapshot


def _rows_by_id(snapshot: dict[str, list[str]], table: str) -> dict[str, dict]:
    return {row["id"]: row for row in map(json.loads, snapshot[table])}


def _view_ids(client, view: str) -> set[str]:
    response = client.get(f"/api/matches?view={view}")
    assert response.status_code == 200, response.text
    return {item["id"] for item in response.json()["items"]}


def _patch(client, match_id, payload) -> dict:
    response = client.patch(f"/api/matches/{match_id}", json=payload)
    assert response.status_code == 200, (payload, response.text)
    return response.json()


def _mark_applied(client, clock, match_id) -> datetime:
    clock.advance(minutes=7)
    body = _patch(client, match_id, {"applied": True})
    applied_at = datetime.fromisoformat(body["applied_at"])
    assert applied_at == clock()
    return applied_at


# --- reconciliation through preferences and watches ---------------------------


def test_preference_deactivation_reactivation_and_reason_refresh_keep_applied_at(
    hosted, make_client, catalog
) -> None:
    services, mailer, clock = hosted
    client, user_id = _account(services, mailer, make_client, "owner@example.com")
    _import(services, catalog, clock, [_job()], "a")
    match_id = _match_id(services, user_id)
    applied_at = _mark_applied(client, clock, match_id)
    before = _state(services, match_id)
    assert _reason_codes(before)[-1] == "season_any"

    # Deactivation: the job stays open but the role no longer matches.
    clock.advance(hours=1)
    _put_preferences(client, role_ids=["data_science"])
    deactivated = _state(services, match_id)
    assert deactivated["no_longer_matches_at"] == clock()
    assert deactivated["applied_at"] == applied_at
    assert match_id in _view_ids(client, "applied")
    assert match_id in _view_ids(client, "historical")
    assert match_id not in _view_ids(client, "active")

    # Reactivation restores the match without touching applied_at.
    clock.advance(hours=1)
    _put_preferences(client)
    reactivated = _state(services, match_id)
    assert reactivated["no_longer_matches_at"] is None
    assert reactivated["last_matched_at"] == clock()
    assert reactivated["matched_at"] == before["matched_at"]
    assert reactivated["applied_at"] == applied_at
    assert match_id in _view_ids(client, "active") & _view_ids(client, "applied")

    # Reason refresh on a still-active match: season_any -> season_unspecified.
    clock.advance(hours=1)
    _put_preferences(client, internship_season="Summer 2027")
    refreshed = _state(services, match_id)
    assert _reason_codes(refreshed)[-1] == "season_unspecified"
    assert refreshed["match_reasons"] != reactivated["match_reasons"]
    assert refreshed["last_matched_at"] == clock()
    assert refreshed["no_longer_matches_at"] is None
    assert refreshed["applied_at"] == applied_at


def test_open_job_that_stops_matching_through_watch_changes_stays_applied(
    hosted, make_client, catalog
) -> None:
    services, mailer, clock = hosted
    client, user_id = _account(services, mailer, make_client, "owner@example.com")
    _import(services, catalog, clock, [_job()], "a")
    match_id = _match_id(services, user_id)
    applied_at = _mark_applied(client, clock, match_id)

    for step, watch in enumerate((True, False, None), start=1):
        clock.advance(hours=step)
        _put_watch(client, paused=watch)
        state = _state(services, match_id)
        stopped = watch is not False
        assert (state["no_longer_matches_at"] == clock()) is stopped, watch
        assert (state["no_longer_matches_at"] is None) is not stopped, watch
        assert state["applied_at"] == applied_at, watch
        assert match_id in _view_ids(client, "applied"), watch
        assert (match_id in _view_ids(client, "active")) is not stopped, watch

    with services.database.session_factory() as db:
        assert db.scalar(select(HostedJob.is_open)) is True
    listed = client.get("/api/matches?view=applied").json()["items"]
    assert [item["id"] for item in listed] == [match_id]
    assert datetime.fromisoformat(listed[0]["applied_at"]) == applied_at
    assert listed[0]["is_open"] is True and listed[0]["no_longer_matches_at"]


# --- reconciliation through the import path ---------------------------------------


def test_import_refresh_deactivation_and_reactivation_keep_applied_and_enqueue_nothing(
    hosted, make_client, catalog
) -> None:
    services, mailer, clock = hosted
    client, user_id = _account(
        services, mailer, make_client, "owner@example.com", internship_season="Summer 2027"
    )
    first = _import(services, catalog, clock, [_job()], "a")
    assert first.counters.matches_created == 1
    assert len(first.new_opening_match_ids) == 1
    match_id = _match_id(services, user_id)
    assert _reason_codes(_state(services, match_id))[-1] == "season_unspecified"
    applied_at = _mark_applied(client, clock, match_id)
    baseline = _notification_state(services)
    assert len(baseline["hosted_notification_items"]) == 1

    # Refresh while active: the title gains matching season evidence.
    clock.advance(hours=1)
    refresh = _import(
        services, catalog, clock, [_job(title="Summer 2027 Backend Software Engineer Intern")], "b"
    )
    assert refresh.counters.matches_created == 0 and not refresh.new_opening_match_ids
    refreshed = _state(services, match_id)
    assert _reason_codes(refreshed)[-1] == "season_match"
    assert refreshed["last_matched_at"] == clock()
    assert refreshed["no_longer_matches_at"] is None
    assert refreshed["applied_at"] == applied_at
    assert _notification_state(services) == baseline

    # Deactivation: the still-open job's title moves to a conflicting season.
    clock.advance(hours=1)
    _import(
        services, catalog, clock, [_job(title="Fall 2027 Backend Software Engineer Intern")], "c"
    )
    deactivated = _state(services, match_id)
    assert deactivated["no_longer_matches_at"] == clock()
    assert deactivated["applied_at"] == applied_at
    assert match_id in _view_ids(client, "applied") - _view_ids(client, "active")
    assert _notification_state(services) == baseline

    # Reactivation in the same import as a genuinely new opening: only the new
    # opening is enqueued; the applied match and every existing row are untouched.
    clock.advance(hours=1)
    reactivation = _import(services, catalog, clock, [_job(), _job(2)], "d")
    assert reactivation.counters.matches_created == 1
    new_match_id = _match_id(services, user_id, 2)
    assert [str(i) for i in reactivation.new_opening_match_ids] == [new_match_id]
    reactivated = _state(services, match_id)
    assert reactivated["no_longer_matches_at"] is None
    assert reactivated["last_matched_at"] == clock()
    assert _reason_codes(reactivated)[-1] == "season_unspecified"
    assert reactivated["applied_at"] == applied_at
    after = _notification_state(services)
    for table in NOTIFICATION_TABLES:
        old, new = _rows_by_id(baseline, table), _rows_by_id(after, table)
        assert {key: new[key] for key in old} == old, table
    old_items = _rows_by_id(baseline, "hosted_notification_items")
    added = [
        row
        for key, row in _rows_by_id(after, "hosted_notification_items").items()
        if key not in old_items
    ]
    assert [row["user_job_match_id"] for row in added] == [new_match_id]
    with services.database.session_factory() as db:
        assert db.scalar(
            select(func.count())
            .select_from(HostedNotificationItem)
            .where(HostedNotificationItem.user_job_match_id == uuid.UUID(match_id))
        ) == 1


# --- PATCH with existing notification rows -----------------------------------------


def test_applied_patches_never_create_cancel_or_change_notification_rows(
    hosted, make_client, catalog
) -> None:
    services, mailer, clock = hosted
    client, user_id = _account(services, mailer, make_client, "owner@example.com")
    transport = RecordingTransport()
    worker = NotificationDeliveryWorker(
        services.database, transport, "https://internships.example", clock=clock
    )
    _import(services, catalog, clock, [_job()], "a")
    assert worker.run().sent == 1
    clock.advance(minutes=1)
    _import(services, catalog, clock, [_job(2)], "b")
    sent_match, pending_match = _match_id(services, user_id), _match_id(services, user_id, 2)
    baseline = _notification_state(services)
    statuses = sorted(
        row["status"] for row in _rows_by_id(baseline, "hosted_notification_items").values()
    )
    assert statuses == ["pending", "sent"]
    assert len(baseline["hosted_notification_attempts"]) == 1
    sent_mail, account_mail = len(transport.messages), len(mailer.messages)

    original: dict[str, str] = {}
    for match_id in (sent_match, pending_match):
        clock.advance(minutes=3)
        original[match_id] = _patch(client, match_id, {"applied": True})["applied_at"]
        assert _notification_state(services) == baseline
        clock.advance(minutes=3)
        repeat = _patch(client, match_id, {"applied": True})
        assert _instant(repeat["applied_at"]) == _instant(original[match_id])
        assert _notification_state(services) == baseline
        clock.advance(minutes=3)
        assert _patch(client, match_id, {"applied": False})["applied_at"] is None
        assert _notification_state(services) == baseline
        restore = {"applied": True, "applied_at": original[match_id]}
        restored = _patch(client, match_id, restore)["applied_at"]
        assert _instant(restored) == _instant(original[match_id])
        assert _notification_state(services) == baseline
    assert (len(transport.messages), len(mailer.messages)) == (sent_mail, account_mail)

    # The pending alert for the applied match is still delivered normally.
    assert _state(services, pending_match)["applied_at"] is not None
    assert worker.run().sent == 1
    assert len(transport.messages) == sent_mail + 1


# --- account deletion -----------------------------------------------------------------


def test_account_deletion_removes_applied_matches_and_spares_other_users(
    hosted, make_client, catalog
) -> None:
    services, mailer, clock = hosted
    leaving, leaving_id = _account(services, mailer, make_client, "leaving@example.com")
    staying, staying_id = _account(services, mailer, make_client, "staying@example.com")
    _import(services, catalog, clock, [_job(), _job(2)], "a")
    leaving_matches = [_match_id(services, leaving_id, i) for i in (1, 2)]
    staying_matches = [_match_id(services, staying_id, i) for i in (1, 2)]
    for match_id in leaving_matches:
        _mark_applied(leaving, clock, match_id)
    staying_applied = _mark_applied(staying, clock, staying_matches[0])
    staying_before = {m: _state(services, m) for m in staying_matches}

    response = leaving.request(
        "DELETE",
        "/api/me",
        json={"password": PASSWORD},
        headers={
            "Origin": services.settings.allowed_frontend_origins[0],
            CSRF_HEADER: "1",
        },
    )
    assert response.status_code in (200, 204), response.text

    with services.database.session_factory() as db:
        assert db.get(User, leaving_id) is None
        assert db.scalars(
            select(UserJobMatch.id).where(UserJobMatch.user_id == leaving_id)
        ).all() == []
        assert db.scalar(select(func.count()).select_from(HostedJob)) == 2
    assert {m: _state(services, m) for m in staying_matches} == staying_before
    assert _view_ids(staying, "applied") == {staying_matches[0]}
    assert staying_before[staying_matches[0]]["applied_at"] == staying_applied
