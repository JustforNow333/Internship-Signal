"""Hosted account-mail provider selection, Resend HTTPS delivery, and the
verification flow those messages carry.

The hosted HTTP tests here run on in-memory SQLite so the verification
contract is exercised on every run, not only when a PostgreSQL URL is
available for `test_hosted_postgres.py`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.hosted.catalog import CompanyCatalog
from app.hosted.database import Base
from app.hosted.mailer import (
    RESEND_ENDPOINT,
    DisabledMailer,
    MailerDeliveryError,
    InMemoryMailer,
    OutboundMessage,
    ResendMailer,
    SMTPMailer,
    configured_mailer,
)
from app.hosted.models import EmailVerificationToken, User
from app.hosted.services import HostedServices
from app.hosted.settings import HostedSettings
from app.main import app

API_KEY = "re_test_key_never_logged"
MESSAGE = OutboundMessage(
    recipient="student@example.com",
    subject="Verify your email",
    text="https://app.example.com/verify-email?token=opaque",
    kind="verification",
)


@pytest.fixture
def clean_mail_env(monkeypatch):
    for name in (
        "HOSTED_RESEND_API_KEY",
        "HOSTED_RESEND_FROM_EMAIL",
        "HOSTED_SMTP_HOST",
        "HOSTED_SMTP_FROM_EMAIL",
        "HOSTED_DATABASE_URL",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _settings(**overrides) -> HostedSettings:
    return replace(HostedSettings.from_env(), **overrides)


# --- provider selection -------------------------------------------------


def test_resend_is_selected_when_both_resend_settings_are_present(
    clean_mail_env,
) -> None:
    clean_mail_env.setenv("HOSTED_RESEND_API_KEY", API_KEY)
    clean_mail_env.setenv("HOSTED_RESEND_FROM_EMAIL", "noreply@example.com")
    # SMTP is complete too, so this proves precedence rather than absence.
    clean_mail_env.setenv("HOSTED_SMTP_HOST", "smtp.example.com")
    clean_mail_env.setenv("HOSTED_SMTP_FROM_EMAIL", "smtp-sender@example.com")
    settings = HostedSettings.from_env()

    assert settings.mail_provider == "resend"
    assert isinstance(configured_mailer(settings), ResendMailer)


def test_smtp_remains_the_fallback_when_resend_is_absent(clean_mail_env) -> None:
    clean_mail_env.setenv("HOSTED_SMTP_HOST", "smtp.example.com")
    clean_mail_env.setenv("HOSTED_SMTP_FROM_EMAIL", "smtp-sender@example.com")
    settings = HostedSettings.from_env()

    assert settings.resend_configured is False
    assert settings.mail_provider == "smtp"
    assert isinstance(configured_mailer(settings), SMTPMailer)


def test_delivery_is_disabled_when_no_provider_is_configured(clean_mail_env) -> None:
    settings = HostedSettings.from_env()

    assert settings.mail_provider == "disabled"
    assert isinstance(configured_mailer(settings), DisabledMailer)
    assert DisabledMailer().send(MESSAGE) is False


@pytest.mark.parametrize(
    ("name", "value", "missing"),
    [
        ("HOSTED_RESEND_API_KEY", API_KEY, "HOSTED_RESEND_FROM_EMAIL"),
        ("HOSTED_RESEND_FROM_EMAIL", "noreply@example.com", "HOSTED_RESEND_API_KEY"),
    ],
)
def test_partial_resend_configuration_is_rejected_rather_than_ignored(
    clean_mail_env, name: str, value: str, missing: str
) -> None:
    clean_mail_env.setenv(name, value)

    with pytest.raises(ValueError, match=missing) as failure:
        HostedSettings.from_env()

    assert API_KEY not in str(failure.value)


def test_api_key_stays_out_of_log_safe_configuration_surfaces(clean_mail_env) -> None:
    clean_mail_env.setenv("HOSTED_RESEND_API_KEY", API_KEY)
    clean_mail_env.setenv("HOSTED_RESEND_FROM_EMAIL", "noreply@example.com")
    settings = HostedSettings.from_env()

    record = logging.LogRecord(
        "hosted", logging.INFO, __file__, 0, "hosted settings %r", (settings,), None
    )

    assert settings.resend_api_key == API_KEY
    assert API_KEY not in repr(settings)
    assert API_KEY not in str(settings)
    assert API_KEY not in record.getMessage()
    assert settings.mail_provider == "resend"


# --- Resend HTTPS delivery ----------------------------------------------


def _resend_settings() -> HostedSettings:
    return _settings(
        resend_api_key=API_KEY,
        resend_from_email="noreply@example.com",
        smtp_timeout_seconds=3,
    )


def test_successful_resend_response_reports_accepted_delivery() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"id": "resend-message-id"})

    mailer = ResendMailer(
        _resend_settings(), transport=httpx.MockTransport(handler)
    )

    assert mailer.send(MESSAGE) is True
    assert seen["url"] == RESEND_ENDPOINT
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["content_type"] == "application/json"
    assert MESSAGE.recipient in str(seen["body"])
    assert MESSAGE.subject in str(seen["body"])
    assert MESSAGE.text in str(seen["body"])
    assert "noreply@example.com" in str(seen["body"])


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout("timed out"),
        httpx.ReadTimeout("timed out"),
        httpx.ConnectError("name resolution failed"),
    ],
)
def test_resend_network_failures_become_bounded_delivery_errors(
    failure: httpx.HTTPError, caplog
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    mailer = ResendMailer(
        _resend_settings(), transport=httpx.MockTransport(handler)
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(MailerDeliveryError) as raised:
            mailer.send(MESSAGE)

    assert API_KEY not in str(raised.value)
    assert API_KEY not in caplog.text
    assert MESSAGE.text not in str(raised.value)


@pytest.mark.parametrize("status_code", [400, 401, 403, 422, 429, 500, 503])
def test_resend_rejections_do_not_leak_provider_response_contents(
    status_code: int,
) -> None:
    secret_body = {"message": "invalid api key re_test_key_never_logged"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=secret_body)

    mailer = ResendMailer(
        _resend_settings(), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(MailerDeliveryError) as raised:
        mailer.send(MESSAGE)

    detail = str(raised.value)
    assert str(status_code) in detail
    assert API_KEY not in detail
    assert "invalid api key" not in detail


def test_resend_malformed_success_body_is_a_delivery_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    mailer = ResendMailer(
        _resend_settings(), transport=httpx.MockTransport(handler)
    )

    with pytest.raises(MailerDeliveryError, match="malformed"):
        mailer.send(MESSAGE)


# --- hosted verification flow -------------------------------------------


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class AwareDateTime(TypeDecorator):
    """Give SQLite the aware timestamps PostgreSQL `timestamptz` returns.

    Hosted state runs on PostgreSQL, where every stored timestamp comes back
    timezone-aware; SQLite drops the offset. Without this the harness, not the
    application, would be what fails the token-expiry comparisons.
    """

    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


@pytest.fixture
def hosted_sqlite(clean_mail_env):
    clean_mail_env.setenv("HOSTED_PUBLIC_FRONTEND_URL", "https://app.example.com")
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine.dialect.colspecs = {**engine.dialect.colspecs, DateTime: AwareDateTime}
    Base.metadata.create_all(engine)
    mailer = InMemoryMailer()
    clock = MutableClock()
    services = HostedServices(
        settings=HostedSettings.from_env(),
        database=SimpleNamespace(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False)
        ),
        mailer=mailer,
        clock=clock,
        catalog=CompanyCatalog(()),
    )
    previous = app.state.hosted_services
    app.state.hosted_services = services
    try:
        with TestClient(app) as test_client:
            yield test_client, services, mailer, clock
    finally:
        app.state.hosted_services = previous
        Base.metadata.drop_all(engine)
        engine.dispose()


def _signup(client, email: str = "student@example.com"):
    response = client.post(
        "/api/auth/signup", json={"email": email, "password": "secure password"}
    )
    assert response.status_code == 201, response.text
    return response


def _link(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("http"))


def _token(text: str) -> str:
    return parse_qs(urlsplit(_link(text)).query)["token"][0]


def test_signup_reports_delivery_and_mails_a_public_frontend_verification_link(
    hosted_sqlite,
) -> None:
    client, services, mailer, _clock = hosted_sqlite
    response = _signup(client)

    assert response.json()["verification_email_sent"] is True
    assert response.json()["user"]["email_verified"] is False
    assert len(mailer.messages) == 1
    link = _link(mailer.messages[-1].text)
    assert link.startswith(
        f"{services.settings.public_frontend_url}/verify-email?token="
    )
    raw_token = _token(mailer.messages[-1].text)
    assert raw_token
    with services.database.session_factory() as db:
        stored = db.scalar(select(EmailVerificationToken))
        # Only the hash is persisted; the opaque token exists in the link alone.
        assert stored.token_hash != raw_token
        assert raw_token not in stored.token_hash


def test_signup_reports_when_the_selected_mailer_does_not_accept_delivery(
    hosted_sqlite,
) -> None:
    client, _services, mailer, _clock = hosted_sqlite
    mailer.accept = False

    response = _signup(client, "undelivered@example.com")

    assert response.json()["verification_email_sent"] is False
    assert mailer.messages == []


def test_verification_marks_the_user_and_cannot_be_replayed(hosted_sqlite) -> None:
    client, services, mailer, _clock = hosted_sqlite
    _signup(client)
    raw_token = _token(mailer.messages[-1].text)

    assert client.get("/api/me").json()["email_verified"] is False
    assert (
        client.post("/api/auth/verify-email", json={"token": raw_token}).status_code
        == 200
    )
    assert client.get("/api/me").json()["email_verified"] is True
    with services.database.session_factory() as db:
        assert db.scalar(select(User)).email_verified_at is not None

    replay = client.post("/api/auth/verify-email", json={"token": raw_token})
    assert replay.status_code == 400
    assert "invalid or expired" in replay.json()["detail"]


def test_expired_and_unknown_verification_tokens_fail_safely(hosted_sqlite) -> None:
    client, _services, mailer, clock = hosted_sqlite
    _signup(client, "expiring@example.com")
    expired_token = _token(mailer.messages[-1].text)
    clock.advance(days=2)

    expired = client.post("/api/auth/verify-email", json={"token": expired_token})
    unknown = client.post(
        "/api/auth/verify-email", json={"token": "a" * 43}
    )

    assert expired.status_code == unknown.status_code == 400
    assert expired.json() == unknown.json()
    assert client.get("/api/me").json()["email_verified"] is False


def test_resend_verification_issues_a_fresh_single_use_link(hosted_sqlite) -> None:
    client, _services, mailer, _clock = hosted_sqlite
    _signup(client)
    first_token = _token(mailer.messages[-1].text)

    accepted = client.post(
        "/api/auth/resend-verification", json={"email": "student@example.com"}
    )
    assert accepted.status_code == 200
    second_token = _token(mailer.messages[-1].text)
    assert second_token != first_token
    assert (
        client.post("/api/auth/verify-email", json={"token": second_token}).status_code
        == 200
    )


def test_password_reset_mail_still_flows_through_the_selected_mailer(
    hosted_sqlite,
) -> None:
    client, services, mailer, _clock = hosted_sqlite
    _signup(client)
    mailer.messages.clear()

    accepted = client.post(
        "/api/auth/forgot-password", json={"email": "student@example.com"}
    )

    assert accepted.status_code == 200
    assert len(mailer.messages) == 1
    assert mailer.messages[-1].kind == "password_reset"
    reset_link = _link(mailer.messages[-1].text)
    assert reset_link.startswith(
        f"{services.settings.public_frontend_url}/reset-password?token="
    )
    reset = client.post(
        "/api/auth/reset-password",
        json={"token": _token(mailer.messages[-1].text), "password": "new password"},
    )
    assert reset.status_code == 200
