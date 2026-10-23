"""PostgreSQL tests for permanent hosted account deletion.

Every test runs offline against an isolated, migrated PostgreSQL database with
a fake clock, the in-memory account mailer, and a recording notification
transport. Nothing contacts a mail provider or production service.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url

from app.hosted.account_deletion import delete_account
from app.hosted.catalog import CompanyCatalog, PublicCompany
from app.hosted.database import (
    HostedDatabase,
    alembic_config_url,
    normalize_database_url,
)
from app.hosted.dependencies import CSRF_HEADER
from app.hosted.job_import import JobImportService
from app.hosted.mailer import InMemoryMailer
from app.hosted.match_service import reconcile_jobs
from app.hosted.models import (
    EmailVerificationToken,
    HostedCatchUpRequest,
    HostedJob,
    HostedNotificationBatch,
    HostedNotificationItem,
    User,
    UserJobMatch,
)
from app.hosted.notification_mail import DeliveryResult, NotificationEmail
from app.hosted.notification_worker import NotificationDeliveryWorker
from app.hosted.services import HostedServices
from app.hosted.settings import HostedSettings
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
FRONTEND_URL = "https://findsooner.example"
PASSWORD = "secure password"
COMPANIES = (("stripe", "Stripe"), ("google", "Google"))

# Every table holding rows owned by one account, keyed by its user column.
ACCOUNT_TABLES = {
    "hosted_users": "id",
    "hosted_authentication_sessions": "user_id",
    "hosted_email_verification_tokens": "user_id",
    "hosted_password_reset_tokens": "user_id",
    "hosted_user_preferences": "user_id",
    "hosted_user_company_watches": "user_id",
    "hosted_unsupported_company_requests": "user_id",
    "hosted_user_job_matches": "user_id",
    "hosted_catch_up_requests": "user_id",
    "hosted_notification_batches": "user_id",
}
# Account rows reached only through a parent above.
ACCOUNT_CHILD_TABLES = {
    "hosted_notification_items": (
        "SELECT count(*) FROM hosted_notification_items i "
        "JOIN hosted_user_job_matches m ON m.id = i.user_job_match_id "
        "WHERE m.user_id = :user_id"
    ),
    "hosted_notification_attempts": (
        "SELECT count(*) FROM hosted_notification_attempts a "
        "JOIN hosted_notification_batches b ON b.id = a.batch_id "
        "WHERE b.user_id = :user_id"
    ),
}
SHARED_TABLES = (
    "hosted_jobs",
    "hosted_job_identities",
    "hosted_source_observations",
    "hosted_job_import_runs",
    "hosted_job_import_attempts",
)


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class RecordingTransport:
    def __init__(self, on_send=None) -> None:
        self.messages: list[NotificationEmail] = []
        self.on_send = on_send

    def send(self, message: NotificationEmail) -> DeliveryResult:
        self.messages.append(message)
        if self.on_send is not None:
            self.on_send(message)
        return DeliveryResult("sent")


def _psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@pytest.fixture(scope="session")
def deletion_postgres_url() -> Iterator[str]:
    base_url = os.getenv("HOSTED_TEST_DATABASE_URL", "").strip()
    if not base_url:
        pytest.skip("HOSTED_TEST_DATABASE_URL is required for hosted PostgreSQL tests")
    parsed = make_url(normalize_database_url(base_url))
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("HOSTED_TEST_DATABASE_URL must use PostgreSQL")
    database_name = f"internship_signal_deletion_test_{uuid.uuid4().hex}"
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    isolated_url = parsed.set(database=database_name).render_as_string(
        hide_password=False
    )
    try:
        alembic = Config(str(BACKEND_DIR / "alembic.ini"))
        alembic.set_main_option("sqlalchemy.url", alembic_config_url(isolated_url))
        logging.getLogger("hosted.account_deletion.migration").disabled = False
        command.upgrade(alembic, "head")
        command.check(alembic)
        yield isolated_url
    finally:
        with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
            )


@pytest.fixture(autouse=True)
def clean_database(deletion_postgres_url: str) -> None:
    database = HostedDatabase(deletion_postgres_url)
    tables = [
        name
        for name in inspect(database.engine).get_table_names()
        if name != "alembic_version"
    ]
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE "
                + ", ".join(f'"{name}"' for name in tables)
                + " RESTART IDENTITY CASCADE"
            )
        )
    database.dispose()


@pytest.fixture
def hosted(deletion_postgres_url: str):
    settings = replace(
        HostedSettings.from_env(),
        database_url=deletion_postgres_url,
        public_frontend_url=FRONTEND_URL,
        session_lifetime_seconds=365 * 24 * 60 * 60,
    )
    clock = MutableClock()
    mailer = InMemoryMailer()
    catalog = CompanyCatalog(
        tuple(
            PublicCompany(
                id=company_id,
                name=name,
                aliases=(),
                coverage="direct",
                selectable=True,
            )
            for company_id, name in COMPANIES
        )
    )
    services = HostedServices.build(
        settings=settings, mailer=mailer, clock=clock, catalog=catalog
    )
    previous = app.state.hosted_services
    app.state.hosted_services = services
    with ExitStack() as clients:
        try:
            yield SimpleNamespace(
                services=services,
                database=services.database,
                clock=clock,
                mailer=mailer,
                catalog=catalog,
                clients=clients,
                origin=settings.allowed_frontend_origins[0],
            )
        finally:
            app.state.hosted_services = previous
            services.database.dispose()


# --- helpers ---------------------------------------------------------------


def preferences(**overrides) -> dict:
    value = {
        "role_ids": ["software_engineering"],
        "career_levels": ["internship"],
        "preferred_locations": ["New York, NY"],
        "include_remote": True,
        "internship_season": "Any season",
        "alert_frequency": "as_detected",
        "globally_paused": False,
        "include_recent_openings": True,
    }
    value.update(overrides)
    return value


def last_token(hosted, email: str) -> str:
    message = next(m for m in reversed(hosted.mailer.messages) if m.recipient == email)
    url = next(line for line in message.text.splitlines() if line.startswith("http"))
    return parse_qs(urlsplit(url).query)["token"][0]


def new_client(hosted) -> TestClient:
    return hosted.clients.enter_context(TestClient(app))


def account(hosted, email: str, *companies: str, verified: bool = True, **prefs):
    client = new_client(hosted)
    response = client.post(
        "/api/auth/signup", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 201, response.text
    if verified:
        assert (
            client.post(
                "/api/auth/verify-email", json={"token": last_token(hosted, email)}
            ).status_code
            == 200
        )
    assert client.put("/api/preferences", json=preferences(**prefs)).status_code == 200
    if companies:
        response = client.put(
            "/api/watchlist",
            json={"companies": [{"company_id": c, "paused": False} for c in companies]},
        )
        assert response.status_code == 200, response.text
    client.email = email
    return client


def sign_in(hosted, email: str) -> TestClient:
    client = new_client(hosted)
    response = client.post(
        "/api/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return client


def csrf_headers(hosted) -> dict[str, str]:
    return {"Origin": hosted.origin, CSRF_HEADER: "1"}


def delete_me(hosted, client, password: str = PASSWORD, headers=None):
    return client.request(
        "DELETE",
        "/api/me",
        json={"password": password},
        headers=csrf_headers(hosted) if headers is None else headers,
    )


def user_id(hosted, email: str) -> uuid.UUID | None:
    with hosted.database.session_factory() as db:
        return db.scalar(select(User.id).where(User.normalized_email == email))


def account_counts(hosted, owner: uuid.UUID) -> dict[str, int]:
    with hosted.database.engine.connect() as connection:
        counts = {
            table: connection.scalar(
                text(f"SELECT count(*) FROM {table} WHERE {column} = :user_id"),
                {"user_id": owner},
            )
            for table, column in ACCOUNT_TABLES.items()
        }
        counts.update(
            {
                table: connection.scalar(text(query), {"user_id": owner})
                for table, query in ACCOUNT_CHILD_TABLES.items()
            }
        )
    return counts


def snapshot(hosted, tables, *, ignore=()) -> dict[str, list[str]]:
    removed = "ARRAY[" + ", ".join(f"'{name}'" for name in ignore) + "]::text[]"
    with hosted.database.engine.connect() as connection:
        return {
            table: list(
                connection.scalars(
                    text(
                        f"SELECT (to_jsonb(t) - {removed})::text AS row "
                        f"FROM {table} t ORDER BY row"
                    )
                )
            )
            for table in tables
        }


def all_tables(hosted) -> list[str]:
    return sorted(
        name
        for name in inspect(hosted.database.engine).get_table_names()
        if name != "alembic_version"
    )


def final_job(index: int, *, company: str = "Stripe", posted: date) -> dict:
    return {
        "id": f"watcher-deletion-{index}",
        "company": company,
        "title": f"Backend Software Engineer Intern {index}",
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "description": "Build production APIs.",
        "requirements": "Python and SQL",
        "source_url": f"https://example.com/jobs/deletion-{index}",
        "date_posted": posted.isoformat(),
        "deadline": "2026-12-01",
        "deadline_days_left": 40,
        "internship_type": "Summer 2027 Internship",
        "role_classification": {"role": "swe", "role_track": "backend"},
        "extra": {"source": "direct", "source_adapter": "workday"},
    }


def import_jobs(hosted, jobs: list[dict], marker: str):
    return JobImportService(
        hosted.database, hosted.catalog, clock=hosted.clock, identity_shadow=True
    ).import_jobs(
        jobs,
        source_fingerprint=marker * 64,
        source_identifier=f"snapshot-{marker}.json.gz",
        source_type="collection_snapshot",
    )


def worker(hosted, transport) -> NotificationDeliveryWorker:
    return NotificationDeliveryWorker(
        hosted.database, transport, FRONTEND_URL, clock=hosted.clock
    )


def populated_account(hosted, email: str = "leaving@example.com") -> TestClient:
    """An account owning a row in every account table.

    It has two extra sessions, outstanding verification and reset tokens, an
    unsupported-company request, active/saved/dismissed/historical matches, a
    delivered import alert with an attempt, a pending import alert, a pending
    catch-up digest, and a catch-up request still waiting for jobs.
    """

    import_jobs(
        hosted,
        [final_job(index, posted=(NOW - timedelta(days=10)).date()) for index in range(4)],
        "a",
    )
    client = account(hosted, email, "stripe")
    hosted.clock.advance(hours=1)
    import_jobs(hosted, [final_job(10, posted=hosted.clock().date())], "b")
    assert worker(hosted, RecordingTransport()).run().sent == 2
    hosted.clock.advance(hours=1)
    import_jobs(hosted, [final_job(11, posted=hosted.clock().date())], "c")
    # Google has no stored jobs yet, so its catch-up request keeps waiting.
    client.put(
        "/api/watchlist",
        json={
            "companies": [
                {"company_id": "stripe", "paused": False},
                {"company_id": "google", "paused": False},
            ]
        },
    )
    matches = client.get("/api/matches").json()["items"]
    client.patch(f"/api/matches/{matches[0]['id']}", json={"saved": True})
    client.patch(f"/api/matches/{matches[1]['id']}", json={"dismissed": True})
    with hosted.database.session_factory.begin() as db:
        db.get(UserJobMatch, uuid.UUID(matches[2]["id"])).no_longer_matches_at = (
            hosted.clock()
        )
    assert (
        client.post(
            "/api/company-requests",
            json={"company_name": "Example Corp"},
        ).status_code
        == 201
    )
    assert (
        client.post("/api/auth/forgot-password", json={"email": email}).status_code
        == 200
    )
    sign_in(hosted, email)
    sign_in(hosted, email)
    with hosted.database.session_factory.begin() as db:
        db.add(
            EmailVerificationToken(
                user_id=user_id(hosted, email),
                token_hash=uuid.uuid4().hex * 2,
                created_at=hosted.clock(),
                expires_at=hosted.clock() + timedelta(days=1),
            )
        )
    return client


# --- schema ------------------------------------------------------------------


def test_every_account_owned_foreign_key_cascades(hosted) -> None:
    with hosted.database.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT conrelid::regclass::text, confrelid::regclass::text, "
                "confdeltype FROM pg_constraint WHERE contype = 'f'"
            )
        ).all()
    owned = set(ACCOUNT_TABLES) | set(ACCOUNT_CHILD_TABLES)
    referencing_users = {child for child, parent, _ in rows if parent == "hosted_users"}
    assert referencing_users == set(ACCOUNT_TABLES) - {"hosted_users"}
    for child, parent, action in rows:
        if parent in owned:
            assert child in owned, (child, parent)
            assert action == "c", (child, parent, action)
    # Every table is either account-owned or shared.
    assert set(all_tables(hosted)) == owned | set(SHARED_TABLES)


# --- the endpoint --------------------------------------------------------------


def test_deleting_an_account_removes_everything_it_owns(hosted) -> None:
    leaving = populated_account(hosted)
    staying = account(hosted, "staying@example.com", "stripe", "google")
    owner = user_id(hosted, "leaving@example.com")
    before = account_counts(hosted, owner)
    assert all(before.values()), before
    assert before["hosted_authentication_sessions"] == 3
    with hosted.database.session_factory() as db:
        statuses = set(
            db.scalars(
                select(HostedNotificationBatch.status).where(
                    HostedNotificationBatch.user_id == owner
                )
            )
        )
        kinds = set(
            db.scalars(
                select(HostedNotificationBatch.kind).where(
                    HostedNotificationBatch.user_id == owner
                )
            )
        )
    assert {"sent", "pending"} <= statuses and kinds == {"import", "catch_up"}
    other = user_id(hosted, "staying@example.com")
    other_before = account_counts(hosted, other)
    shared_before = snapshot(hosted, SHARED_TABLES)
    other_rows_before = snapshot(
        hosted,
        ["hosted_user_job_matches", "hosted_user_company_watches"],
    )

    response = delete_me(hosted, leaving)

    assert response.status_code == 204
    assert response.content == b""
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{hosted.services.settings.session_cookie_name}=")
    assert "Max-Age=0" in cookie and "HttpOnly" in cookie
    assert set(account_counts(hosted, owner).values()) == {0}
    assert user_id(hosted, "leaving@example.com") is None
    assert account_counts(hosted, other) == other_before
    assert snapshot(hosted, SHARED_TABLES) == shared_before
    after_rows = snapshot(
        hosted, ["hosted_user_job_matches", "hosted_user_company_watches"]
    )
    for table, rows in other_rows_before.items():
        assert [row for row in rows if str(other) in row] == [
            row for row in after_rows[table] if str(other) in row
        ]
    assert staying.get("/api/me").status_code == 200


def test_every_session_and_token_is_unusable_afterwards(hosted) -> None:
    leaving = populated_account(hosted)
    stolen_cookie = leaving.cookies.get(hosted.services.settings.session_cookie_name)
    other_sessions = [sign_in(hosted, "leaving@example.com") for _ in range(2)]
    reset_token = last_token(hosted, "leaving@example.com")

    assert delete_me(hosted, leaving).status_code == 204

    for client in [leaving, *other_sessions]:
        assert client.get("/api/me").status_code == 401
        assert client.get("/api/preferences").status_code == 401
        assert client.get("/api/matches").status_code == 401
    replay = new_client(hosted)
    replay.cookies.set(hosted.services.settings.session_cookie_name, stolen_cookie)
    assert replay.get("/api/me").status_code == 401
    assert delete_me(hosted, replay).status_code == 401
    assert (
        replay.post(
            "/api/auth/reset-password",
            json={"token": reset_token, "password": "another password"},
        ).status_code
        == 400
    )
    assert (
        replay.post(
            "/api/auth/login",
            json={"email": "leaving@example.com", "password": PASSWORD},
        ).status_code
        == 401
    )


def test_a_wrong_password_changes_nothing(hosted) -> None:
    leaving = populated_account(hosted)
    # The authentication touch of last_used_at is the only permitted write.
    before = snapshot(hosted, all_tables(hosted), ignore=("last_used_at",))

    response = delete_me(hosted, leaving, password="not the password")

    assert response.status_code == 403
    assert response.json() == {"detail": "Password was not recognized."}
    assert "set-cookie" not in response.headers
    assert snapshot(hosted, all_tables(hosted), ignore=("last_used_at",)) == before
    assert leaving.get("/api/me").status_code == 200


@pytest.mark.parametrize(
    "payload",
    [{}, {"password": ""}, {"password": PASSWORD, "user_id": str(uuid.uuid4())}],
)
def test_malformed_requests_change_nothing(hosted, payload) -> None:
    leaving = populated_account(hosted)
    before = snapshot(hosted, all_tables(hosted), ignore=("last_used_at",))

    response = leaving.request(
        "DELETE", "/api/me", json=payload, headers=csrf_headers(hosted)
    )

    assert response.status_code == 422
    assert PASSWORD not in response.text
    assert snapshot(hosted, all_tables(hosted), ignore=("last_used_at",)) == before


def test_unauthenticated_requests_are_rejected(hosted) -> None:
    populated_account(hosted)
    signed_out = sign_in(hosted, "leaving@example.com")
    revoked_cookie = signed_out.cookies.get(
        hosted.services.settings.session_cookie_name
    )
    assert signed_out.post("/api/auth/logout").status_code == 204
    before = snapshot(hosted, all_tables(hosted))

    anonymous = new_client(hosted)
    assert delete_me(hosted, anonymous).status_code == 401
    forged = new_client(hosted)
    forged.cookies.set(hosted.services.settings.session_cookie_name, "x" * 64)
    assert delete_me(hosted, forged).status_code == 401
    revoked = new_client(hosted)
    revoked.cookies.set(hosted.services.settings.session_cookie_name, revoked_cookie)
    assert delete_me(hosted, revoked).status_code == 401

    assert snapshot(hosted, all_tables(hosted)) == before
    assert user_id(hosted, "leaving@example.com") is not None


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {CSRF_HEADER: "1"},
        {"Origin": "https://attacker.example", CSRF_HEADER: "1"},
        {"Origin": "null", CSRF_HEADER: "1"},
        "origin-only",
        "wrong-header-value",
    ],
)
def test_requests_failing_csrf_validation_cannot_delete(hosted, headers) -> None:
    leaving = populated_account(hosted)
    if headers == "origin-only":
        headers = {"Origin": hosted.origin}
    elif headers == "wrong-header-value":
        headers = {"Origin": hosted.origin, CSRF_HEADER: "true"}
    before = snapshot(hosted, all_tables(hosted), ignore=("last_used_at",))

    response = delete_me(hosted, leaving, headers=headers)

    assert response.status_code == 403
    assert "set-cookie" not in response.headers
    assert snapshot(hosted, all_tables(hosted), ignore=("last_used_at",)) == before
    assert leaving.get("/api/me").status_code == 200


def test_cors_preflight_admits_only_the_configured_frontend(hosted) -> None:
    client = new_client(hosted)
    request_headers = {
        "Access-Control-Request-Method": "DELETE",
        "Access-Control-Request-Headers": f"content-type,{CSRF_HEADER.lower()}",
    }

    allowed = client.options(
        "/api/me", headers={"Origin": hosted.origin, **request_headers}
    )
    foreign = client.options(
        "/api/me",
        headers={"Origin": "https://attacker.example", **request_headers},
    )

    assert allowed.headers.get("access-control-allow-origin") == hosted.origin
    assert "access-control-allow-origin" not in foreign.headers


def test_the_email_can_sign_up_again_and_starts_fresh(hosted) -> None:
    leaving = populated_account(hosted)
    assert delete_me(hosted, leaving).status_code == 204

    fresh = new_client(hosted)
    response = fresh.post(
        "/api/auth/signup",
        json={"email": "leaving@example.com", "password": "a new password"},
    )

    assert response.status_code == 201
    assert response.json()["user"]["email_verified"] is False
    owner = user_id(hosted, "leaving@example.com")
    counts = account_counts(hosted, owner)
    assert counts["hosted_users"] == 1
    assert counts["hosted_user_preferences"] == 1
    assert counts["hosted_authentication_sessions"] == 1
    assert counts["hosted_email_verification_tokens"] == 1
    assert {
        table: count
        for table, count in counts.items()
        if table
        not in {
            "hosted_users",
            "hosted_user_preferences",
            "hosted_authentication_sessions",
            "hosted_email_verification_tokens",
        }
    } == dict.fromkeys(
        (set(ACCOUNT_TABLES) | set(ACCOUNT_CHILD_TABLES))
        - {
            "hosted_users",
            "hosted_user_preferences",
            "hosted_authentication_sessions",
            "hosted_email_verification_tokens",
        },
        0,
    )
    assert fresh.get("/api/watchlist").json() == []
    assert fresh.get("/api/matches").json()["total"] == 0
    assert fresh.get("/api/preferences").json()["role_ids"] == ["software_engineering"]


# --- notification work ---------------------------------------------------------


def test_a_claimed_batch_is_removed_without_corrupting_the_worker(hosted) -> None:
    leaving = account(hosted, "leaving@example.com", "stripe")
    account(hosted, "staying@example.com", "stripe")
    hosted.clock.advance(hours=1)
    import_jobs(hosted, [final_job(1, posted=hosted.clock().date())], "a")
    transport = RecordingTransport()
    delivery = worker(hosted, transport)
    claims = delivery.claim_due_batches(limit=10)
    assert len(claims) == 2
    owner = user_id(hosted, "leaving@example.com")
    with hosted.database.session_factory() as db:
        owned = {
            batch_id
            for batch_id, _token in claims
            if db.get(HostedNotificationBatch, batch_id).user_id == owner
        }

    assert delete_me(hosted, leaving).status_code == 204

    for batch_id, token in claims:
        prepared = delivery._prepare_delivery(batch_id, token)
        if batch_id in owned:
            assert prepared is None
        else:
            assert prepared is not None
            assert delivery._apply_result(prepared, DeliveryResult("sent")) == "sent"
    assert set(account_counts(hosted, owner).values()) == {0}
    with hosted.database.session_factory() as db:
        assert set(db.scalars(select(HostedNotificationBatch.status))) == {"sent"}
    hosted.clock.advance(minutes=30)
    assert delivery.run().claimed == 0


def test_deletion_during_submission_records_no_rows_and_spares_others(
    hosted,
) -> None:
    leaving = account(hosted, "leaving@example.com", "stripe")
    account(hosted, "staying@example.com", "stripe")
    hosted.clock.advance(hours=1)
    import_jobs(hosted, [final_job(1, posted=hosted.clock().date())], "a")
    owner = user_id(hosted, "leaving@example.com")

    def delete_while_sending(message: NotificationEmail) -> None:
        if message.recipient == "leaving@example.com":
            assert delete_me(hosted, leaving).status_code == 204

    transport = RecordingTransport(on_send=delete_while_sending)
    summary = worker(hosted, transport).run()

    # The provider may already hold the leaving user's message; it cannot be
    # recalled, and the worker records nothing for the deleted account.
    assert summary.sent == 1 and summary.uncertain == 1
    assert sorted(m.recipient for m in transport.messages) == [
        "leaving@example.com",
        "staying@example.com",
    ]
    assert set(account_counts(hosted, owner).values()) == {0}
    with hosted.database.session_factory() as db:
        [batch] = db.scalars(select(HostedNotificationBatch)).all()
        assert batch.status == "sent"
    hosted.clock.advance(days=1)
    assert worker(hosted, RecordingTransport()).run().claimed == 0


def test_no_notification_work_is_created_after_deletion(hosted) -> None:
    leaving = account(hosted, "leaving@example.com", "stripe", "google")
    owner = user_id(hosted, "leaving@example.com")
    with hosted.database.session_factory() as db:
        assert db.scalar(
            select(func.count()).select_from(HostedCatchUpRequest)
        ) == 2
    assert delete_me(hosted, leaving).status_code == 204

    hosted.clock.advance(hours=1)
    imported = import_jobs(
        hosted,
        [
            final_job(1, posted=hosted.clock().date()),
            final_job(2, company="Google", posted=(NOW - timedelta(days=3)).date()),
        ],
        "a",
    )

    assert imported.outcome == "imported"
    assert imported.counters.matches_created == 0
    assert set(account_counts(hosted, owner).values()) == {0}
    with hosted.database.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(HostedCatchUpRequest)) == 0
        assert db.scalar(select(func.count()).select_from(HostedNotificationBatch)) == 0
    assert worker(hosted, RecordingTransport()).run().claimed == 0


# --- concurrency -----------------------------------------------------------------


def _run_in_thread(target) -> tuple[threading.Thread, list]:
    outcome: list = []

    def run() -> None:
        try:
            outcome.append(target())
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_deletion_waits_for_an_in_flight_import(hosted) -> None:
    leaving = account(hosted, "leaving@example.com", "stripe")
    account(hosted, "staying@example.com", "stripe")
    owner = user_id(hosted, "leaving@example.com")
    job_id = uuid.uuid4()
    with hosted.database.session_factory.begin() as db:
        db.add(
            HostedJob(
                id=job_id,
                watcher_job_id="watcher-in-flight",
                company_id="stripe",
                company_name="Stripe",
                title="Software Engineer Intern",
                location="New York, NY",
                remote_status="",
                role_id="software_engineering",
                career_level="internship",
                application_url="https://example.com/in-flight",
                posting_date=NOW.date(),
                is_open=True,
                first_seen_at=NOW,
                last_seen_at=NOW,
                source_metadata={},
            )
        )

    importing = hosted.database.session_factory()
    try:
        outcome = reconcile_jobs(importing, [job_id], now=hosted.clock())
        assert outcome.created == 2
        thread, result = _run_in_thread(lambda: delete_me(hosted, leaving).status_code)
        thread.join(timeout=1.0)
        assert thread.is_alive(), "deletion must wait for the import's locks"
        importing.commit()
    finally:
        importing.close()
    thread.join(timeout=30)

    assert result == [204]
    assert set(account_counts(hosted, owner).values()) == {0}
    with hosted.database.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(UserJobMatch)) == 1


def test_an_import_waits_for_an_in_flight_deletion(hosted) -> None:
    account(hosted, "leaving@example.com", "stripe")
    account(hosted, "staying@example.com", "stripe")
    owner = user_id(hosted, "leaving@example.com")
    with hosted.database.session_factory() as db:
        password_hash = db.get(User, owner).password_hash
    hosted.clock.advance(hours=1)

    deleting = hosted.database.session_factory()
    try:
        assert delete_account(deleting, owner, password_hash=password_hash)
        thread, result = _run_in_thread(
            lambda: import_jobs(
                hosted, [final_job(1, posted=hosted.clock().date())], "a"
            )
        )
        thread.join(timeout=1.0)
        assert thread.is_alive(), "the import must wait for the deletion's locks"
        deleting.commit()
    finally:
        deleting.close()
    thread.join(timeout=30)

    [imported] = result
    assert not isinstance(imported, BaseException), imported
    assert imported.outcome == "imported"
    assert imported.counters.matches_created == 1
    assert set(account_counts(hosted, owner).values()) == {0}
    with hosted.database.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(HostedNotificationItem)) == 1


# --- failure ---------------------------------------------------------------------


def test_a_database_failure_rolls_the_whole_deletion_back(hosted) -> None:
    leaving = populated_account(hosted)
    before = snapshot(hosted, all_tables(hosted), ignore=("last_used_at",))
    with hosted.database.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE FUNCTION fail_attempt_delete() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'simulated storage failure'; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER fail_attempt_delete BEFORE DELETE ON "
                "hosted_notification_attempts FOR EACH ROW "
                "EXECUTE FUNCTION fail_attempt_delete()"
            )
        )
    try:
        response = delete_me(hosted, leaving)
    finally:
        with hosted.database.engine.begin() as connection:
            connection.execute(
                text("DROP TRIGGER fail_attempt_delete ON hosted_notification_attempts")
            )
            connection.execute(text("DROP FUNCTION fail_attempt_delete()"))

    assert response.status_code == 503
    assert "set-cookie" not in response.headers
    assert "simulated" not in response.text
    assert snapshot(hosted, all_tables(hosted), ignore=("last_used_at",)) == before
    assert leaving.get("/api/me").status_code == 200
    assert delete_me(hosted, leaving).status_code == 204
