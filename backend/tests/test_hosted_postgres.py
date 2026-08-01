from __future__ import annotations

import logging
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.hosted.database import HostedDatabase, normalize_database_url
from app.hosted.mailer import InMemoryMailer
from app.hosted.models import (
    AuthenticationSession,
    UnsupportedCompanyRequest,
    User,
    UserCompanyWatch,
)
from app.hosted.security import hash_password, token_hash
from app.hosted.services import HostedServices
from app.hosted.settings import HostedSettings
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def _psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@pytest.fixture(scope="session")
def postgres_url() -> str:
    base_url = os.getenv("HOSTED_TEST_DATABASE_URL", "").strip()
    if not base_url:
        pytest.skip("HOSTED_TEST_DATABASE_URL is required for hosted PostgreSQL tests")
    parsed = make_url(normalize_database_url(base_url))
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("HOSTED_TEST_DATABASE_URL must use PostgreSQL")
    database_name = f"internship_signal_test_{uuid.uuid4().hex}"
    admin_url = parsed.set(database="postgres")
    with psycopg.connect(
        _psycopg_url(admin_url.render_as_string(hide_password=False)), autocommit=True
    ) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    isolated_url = parsed.set(database=database_name).render_as_string(
        hide_password=False
    )
    alembic = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic.set_main_option("sqlalchemy.url", isolated_url.replace("%", "%%"))
    existing_logger = logging.getLogger("hosted.migration.existing")
    existing_logger.disabled = False
    command.upgrade(alembic, "head")
    if existing_logger.disabled:
        pytest.fail("Alembic migration disabled an existing application logger")
    command.check(alembic)
    yield isolated_url
    with psycopg.connect(
        _psycopg_url(admin_url.render_as_string(hide_password=False)), autocommit=True
    ) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        connection.execute(
            sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
        )


@pytest.fixture(autouse=True)
def clean_database(postgres_url: str):
    database = HostedDatabase(postgres_url)
    table_names = inspect(database.engine).get_table_names()
    with database.engine.begin() as connection:
        if table_names:
            quoted = ", ".join(
                f'"{name}"' for name in table_names if name != "alembic_version"
            )
            if quoted:
                connection.execute(
                    text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
                )
    database.dispose()


@pytest.fixture
def hosted(postgres_url: str):
    settings = replace(HostedSettings.from_env(), database_url=postgres_url)
    mailer = InMemoryMailer()
    clock = MutableClock()
    services = HostedServices.build(settings=settings, mailer=mailer, clock=clock)
    previous = app.state.hosted_services
    app.state.hosted_services = services
    try:
        yield services, mailer, clock
    finally:
        app.state.hosted_services = previous
        services.database.dispose()


@pytest.fixture
def client(hosted):
    with TestClient(app) as test_client:
        yield test_client


def signup(
    client: TestClient,
    email: str = "student@example.com",
    password: str = "secure password",
):
    response = client.post(
        "/api/auth/signup", json={"email": email, "password": password}
    )
    assert response.status_code == 201, response.text
    return response


def message_token(text_value: str) -> str:
    url = next(line for line in text_value.splitlines() if line.startswith("http"))
    return parse_qs(urlsplit(url).query)["token"][0]


def preferences_payload(**overrides):
    payload = {
        "role_ids": ["software_engineering", "data_engineering"],
        "preferred_locations": ["New York, NY", "Boston, MA"],
        "include_remote": True,
        "internship_season": "Summer 2027",
        "alert_frequency": "as_detected",
        "globally_paused": False,
    }
    payload.update(overrides)
    return payload


def test_empty_database_migrates_to_expected_postgresql_schema(
    postgres_url: str,
) -> None:
    database = HostedDatabase(postgres_url)
    inspector = inspect(database.engine)
    tables = set(inspector.get_table_names())
    assert {
        "hosted_users",
        "hosted_authentication_sessions",
        "hosted_email_verification_tokens",
        "hosted_password_reset_tokens",
        "hosted_user_preferences",
        "hosted_user_company_watches",
        "hosted_unsupported_company_requests",
    }.issubset(tables)
    with database.engine.connect() as connection:
        data_type = connection.scalar(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='hosted_user_preferences' AND column_name='role_ids'"
            )
        )
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert data_type == "jsonb"
    assert revision == "20260801_0001"
    database.dispose()


def test_signup_hashes_password_normalizes_email_and_sets_secure_session_cookie(
    client, hosted
) -> None:
    response = signup(client, "Student@Example.COM")
    services, mailer, _clock = hosted
    assert response.json()["user"]["email"] == "Student@example.com"
    assert response.json()["verification_email_sent"] is True
    assert len(mailer.messages) == 1
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    with services.database.session_factory() as db:
        user = db.scalar(select(User))
        authentication_session = db.scalar(select(AuthenticationSession))
        assert user.normalized_email == "student@example.com"
        assert user.password_hash.startswith("$argon2id$")
        assert "secure password" not in user.password_hash
        raw_cookie = response.cookies[services.settings.session_cookie_name]
        assert authentication_session.token_hash == token_hash(raw_cookie)
        assert raw_cookie != authentication_session.token_hash

    duplicate = client.post(
        "/api/auth/signup",
        json={"email": "student@example.com", "password": "another password"},
    )
    assert duplicate.status_code == 409


def test_secure_cookie_mode_sets_the_secure_attribute(client, hosted) -> None:
    services, _mailer, _clock = hosted
    services.settings = replace(services.settings, secure_cookies=True)
    response = signup(client, "secure-cookie@example.com")
    assert "Secure" in response.headers["set-cookie"]


def test_signup_reports_when_the_configured_mailer_does_not_accept_delivery(
    client, hosted
) -> None:
    _services, mailer, _clock = hosted
    mailer.accept = False
    response = signup(client, "undelivered@example.com")
    assert response.json()["verification_email_sent"] is False
    assert mailer.messages == []


def test_hosted_request_limits_and_validation_errors_do_not_echo_input(
    client,
) -> None:
    oversized = client.post(
        "/api/auth/signup",
        json={"email": f"{'a' * 70_000}@example.com", "password": "secure password"},
    )
    assert oversized.status_code == 413

    chunked = client.post(
        "/api/auth/signup",
        content=iter([b"x" * 40_000, b"x" * 40_000]),
        headers={"content-type": "application/json"},
    )
    assert chunked.status_code == 413

    secret = "x" * 1_025
    invalid = client.post(
        "/api/auth/signup",
        json={"email": "student@example.com", "password": secret},
    )
    assert invalid.status_code == 422
    assert secret not in invalid.text


def test_model_foreign_keys_and_watch_uniqueness_are_enforced(
    postgres_url: str,
) -> None:
    database = HostedDatabase(postgres_url)
    user_id = uuid.uuid4()
    with database.session_factory() as db:
        db.add(
            User(
                id=user_id,
                email="constraint@example.com",
                normalized_email="constraint@example.com",
                password_hash=hash_password("secure password"),
            )
        )
        db.commit()

    with database.session_factory() as db:
        db.add(
            AuthenticationSession(
                user_id=uuid.uuid4(),
                token_hash="a" * 64,
                created_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
                last_used_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    with database.session_factory() as db:
        db.add(
            User(
                email="CONSTRAINT@example.com",
                normalized_email="constraint@example.com",
                password_hash=hash_password("secure password"),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    with database.session_factory() as db:
        with pytest.raises(IntegrityError):
            db.execute(
                insert(UserCompanyWatch),
                [
                    {
                        "user_id": user_id,
                        "company_id": "doordash",
                        "paused": False,
                    },
                    {
                        "user_id": user_id,
                        "company_id": "doordash",
                        "paused": True,
                    },
                ],
            )
            db.commit()
    database.dispose()


def test_login_privacy_logout_and_revocation(client, hosted) -> None:
    signup(client)
    wrong = client.post(
        "/api/auth/login",
        json={"email": "student@example.com", "password": "wrong password"},
    )
    unknown = client.post(
        "/api/auth/login",
        json={"email": "unknown@example.com", "password": "wrong password"},
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()

    login = client.post(
        "/api/auth/login",
        json={"email": "STUDENT@example.com", "password": "secure password"},
    )
    assert login.status_code == 200
    assert client.get("/api/me").status_code == 200
    logout = client.post("/api/auth/logout")
    assert logout.status_code == 204
    assert "Max-Age=0" in logout.headers["set-cookie"]
    assert client.get("/api/me").status_code == 401


@pytest.mark.parametrize("state", ["expired", "revoked", "inactive"])
def test_invalid_session_states_are_rejected(client, hosted, state: str) -> None:
    signup(client)
    services, _mailer, clock = hosted
    with services.database.session_factory() as db:
        authentication_session = db.scalar(select(AuthenticationSession))
        if state == "expired":
            authentication_session.expires_at = clock() - timedelta(seconds=1)
        elif state == "revoked":
            authentication_session.revoked_at = clock()
        else:
            authentication_session.user.is_active = False
        db.commit()
    assert client.get("/api/me").status_code == 401


def test_verification_is_expiring_and_one_time(client, hosted) -> None:
    signup(client)
    _services, mailer, clock = hosted
    token = message_token(mailer.messages[-1].text)
    assert (
        client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    )
    assert client.get("/api/me").json()["email_verified"] is True
    assert (
        client.post("/api/auth/verify-email", json={"token": token}).status_code == 400
    )

    signup(client, "expiring@example.com")
    expired_token = message_token(mailer.messages[-1].text)
    clock.advance(days=2)
    assert (
        client.post("/api/auth/verify-email", json={"token": expired_token}).status_code
        == 400
    )


def test_password_reset_privacy_expiration_and_one_time_use(client, hosted) -> None:
    signup(client)
    _services, mailer, clock = hosted
    known = client.post(
        "/api/auth/forgot-password", json={"email": "student@example.com"}
    )
    message_count = len(mailer.messages)
    unknown = client.post(
        "/api/auth/forgot-password", json={"email": "unknown@example.com"}
    )
    assert known.json() == unknown.json() == {"accepted": True}
    assert len(mailer.messages) == message_count
    token = message_token(mailer.messages[-1].text)

    known_resend = client.post(
        "/api/auth/resend-verification", json={"email": "student@example.com"}
    )
    resend_count = len(mailer.messages)
    unknown_resend = client.post(
        "/api/auth/resend-verification", json={"email": "unknown@example.com"}
    )
    assert known_resend.json() == unknown_resend.json() == {"accepted": True}
    assert len(mailer.messages) == resend_count

    reset = client.post(
        "/api/auth/reset-password",
        json={"token": token, "password": "new secure password"},
    )
    assert reset.status_code == 200
    assert client.get("/api/me").status_code == 401
    assert (
        client.post(
            "/api/auth/reset-password",
            json={"token": token, "password": "another secure password"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "student@example.com", "password": "new secure password"},
        ).status_code
        == 200
    )

    client.post("/api/auth/forgot-password", json={"email": "student@example.com"})
    expired = message_token(mailer.messages[-1].text)
    clock.advance(hours=2)
    assert (
        client.post(
            "/api/auth/reset-password",
            json={"token": expired, "password": "another secure password"},
        ).status_code
        == 400
    )


def test_preferences_watchlists_requests_and_two_user_isolation(hosted) -> None:
    services, _mailer, _clock = hosted
    client_a = TestClient(app)
    client_b = TestClient(app)
    signup(client_a, "a@example.com")
    signup(client_b, "b@example.com")

    prefs_a = preferences_payload()
    prefs_b = preferences_payload(
        role_ids=["machine_learning_ai"],
        preferred_locations=["Seattle, WA"],
        alert_frequency="daily",
    )
    assert client_a.put("/api/preferences", json=prefs_a).status_code == 200
    assert client_b.put("/api/preferences", json=prefs_b).status_code == 200
    stored_a = client_a.get("/api/preferences").json()
    assert stored_a["role_ids"] == prefs_a["role_ids"]
    assert stored_a["preferred_locations"] == prefs_a["preferred_locations"]
    assert client_b.get("/api/preferences").json()["role_ids"] == prefs_b["role_ids"]

    companies = client_a.get("/api/companies").json()
    assert companies and all(
        set(company) == {"id", "name", "aliases", "coverage", "selectable"}
        for company in companies
    )
    selectable = [company["id"] for company in companies if company["selectable"]]
    watch_a = [{"company_id": selectable[0], "paused": False}]
    watch_b = [{"company_id": selectable[1], "paused": True}]
    assert (
        client_a.put("/api/watchlist", json={"companies": watch_a}).status_code == 200
    )
    assert (
        client_b.put("/api/watchlist", json={"companies": watch_b}).status_code == 200
    )
    assert [item["company_id"] for item in client_a.get("/api/watchlist").json()] == [
        selectable[0]
    ]
    assert [item["company_id"] for item in client_b.get("/api/watchlist").json()] == [
        selectable[1]
    ]

    invalid = client_a.put(
        "/api/watchlist",
        json={
            "companies": [*watch_a, {"company_id": "not-supported", "paused": False}]
        },
    )
    assert invalid.status_code == 400
    assert [item["company_id"] for item in client_a.get("/api/watchlist").json()] == [
        selectable[0]
    ]

    request = client_a.post(
        "/api/company-requests",
        json={
            "company_name": "Example Company",
            "career_url": "https://example.com/careers",
        },
    )
    assert request.status_code == 201 and request.json()["status"] == "received"
    assert (
        client_a.post(
            "/api/company-requests",
            json={
                "company_name": "Bad URL",
                "career_url": "https://user:pass@example.com/jobs",
            },
        ).status_code
        == 422
    )
    with services.database.session_factory() as db:
        stored = db.scalar(select(UnsupportedCompanyRequest))
        user_a = db.scalar(select(User).where(User.normalized_email == "a@example.com"))
        assert stored.user_id == user_a.id

    client_a.close()
    client_b.close()


def test_persistence_across_service_and_client_recreation(postgres_url: str) -> None:
    settings = replace(HostedSettings.from_env(), database_url=postgres_url)
    first_services = HostedServices.build(
        settings=settings, mailer=InMemoryMailer(), clock=MutableClock()
    )
    previous = app.state.hosted_services
    app.state.hosted_services = first_services
    first_client = TestClient(app)
    signup(first_client, "persistent@example.com")
    assert (
        first_client.put(
            "/api/preferences", json=preferences_payload(alert_frequency="daily")
        ).status_code
        == 200
    )
    selectable_company = next(
        company["id"]
        for company in first_client.get("/api/companies").json()
        if company["selectable"]
    )
    assert (
        first_client.put(
            "/api/watchlist",
            json={"companies": [{"company_id": selectable_company, "paused": True}]},
        ).status_code
        == 200
    )
    first_client.close()
    first_services.database.dispose()

    second_services = HostedServices.build(
        settings=settings, mailer=InMemoryMailer(), clock=MutableClock()
    )
    app.state.hosted_services = second_services
    second_client = TestClient(app)
    assert (
        second_client.post(
            "/api/auth/login",
            json={"email": "persistent@example.com", "password": "secure password"},
        ).status_code
        == 200
    )
    assert second_client.get("/api/preferences").json()["alert_frequency"] == "daily"
    persisted_watchlist = second_client.get("/api/watchlist").json()
    assert len(persisted_watchlist) == 1
    assert persisted_watchlist[0]["company_id"] == selectable_company
    assert persisted_watchlist[0]["paused"] is True
    second_client.close()
    second_services.database.dispose()
    app.state.hosted_services = previous
