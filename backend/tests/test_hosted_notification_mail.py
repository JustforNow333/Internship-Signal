from __future__ import annotations

import json
import logging
from datetime import date
from dataclasses import replace
import smtplib

import httpx
import pytest

from app.hosted.notification_mail import (
    RESEND_ENDPOINT,
    DigestJob,
    ResendNotificationTransport,
    SMTPNotificationTransport,
    UnavailableNotificationTransport,
    build_digest_email,
    configured_notification_transport,
)
from app.hosted.settings import HostedSettings

API_KEY = "re_notification_secret_key"
MESSAGE_ID = "<notification-abc@internship-signal.invalid>"


def job(index: int = 1, **overrides) -> DigestJob:
    values = {
        "company_name": f"Company {index}",
        "title": f"Software Intern {index}",
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "posting_date": date(2026, 8, 1),
        "deadline": date(2026, 9, 1),
        "application_url": f"https://example.com/apply/{index}",
        "match_reasons": [
            {"code": "company_watched", "value": "private-internal-id"},
            {"code": "role_selected", "value": "software_engineering"},
            {"code": "unknown", "value": "must-not-render"},
        ],
    }
    values.update(overrides)
    return DigestJob(**values)


def test_digest_has_plain_html_safe_fields_and_required_links() -> None:
    message = build_digest_email(
        recipient="verified@example.com",
        frequency="as_detected",
        jobs=[
            job(
                company_name="A <Company>",
                title='Intern <script>alert("x")</script>',
                location="R&D <Remote>",
                application_url='https://example.com/apply?q=<unsafe>&x="quoted"',
            )
        ],
        message_id="<stable@example.invalid>",
        public_frontend_url="https://internships.example",
    )

    assert message.subject == "New internship matches (1)"
    assert message.message_id == "<stable@example.invalid>"
    assert "A <Company>" in message.text
    assert "<script>" not in message.html
    assert "&lt;script&gt;" in message.html
    assert "Company is on your watchlist" in message.text
    assert "private-internal-id" not in message.text + message.html
    assert "must-not-render" not in message.text + message.html
    assert "https://internships.example/app/matches" in message.text
    assert "https://internships.example/app/settings" in message.text
    assert "description" not in message.text.casefold()


def test_digest_displays_twenty_five_jobs_and_reports_remaining_count() -> None:
    message = build_digest_email(
        recipient="verified@example.com",
        frequency="daily",
        jobs=[job(index) for index in range(1, 28)],
        message_id="<stable@example.invalid>",
        public_frontend_url="https://internships.example",
    )

    assert message.subject == "Your Internship Signal digest (27)"
    assert "Software Intern 25" in message.text
    assert "Software Intern 26" not in message.text
    assert "Software Intern 27" not in message.html
    assert "2 additional matches" in message.text


def test_smtp_transport_classifies_explicit_rejections_and_post_submit_loss(
    monkeypatch,
) -> None:
    settings = replace(
        HostedSettings.from_env(),
        smtp_host="smtp.example.com",
        smtp_from_email="sender@example.com",
        smtp_starttls=False,
    )
    message = build_digest_email(
        recipient="verified@example.com",
        frequency="as_detected",
        jobs=[job()],
        message_id="<stable@example.invalid>",
        public_frontend_url="https://internships.example",
    )

    class FakeSMTP:
        error: Exception | None = None

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send_message(self, email):
            assert email["Message-ID"] == "<stable@example.invalid>"
            raise self.error

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    transport = SMTPNotificationTransport(settings)

    FakeSMTP.error = smtplib.SMTPDataError(451, b"try later")
    assert transport.send(message).outcome == "retryable_failure"
    FakeSMTP.error = smtplib.SMTPDataError(550, b"rejected")
    assert transport.send(message).outcome == "permanent_failure"
    FakeSMTP.error = smtplib.SMTPServerDisconnected("lost")
    result = transport.send(message)
    assert (result.outcome, result.error_code) == (
        "uncertain",
        "connection_lost_after_submission",
    )


# --- job-alert provider selection and Resend transport ---------------------


def _settings(**overrides) -> HostedSettings:
    base = replace(
        HostedSettings.from_env(),
        resend_api_key="",
        resend_from_email="",
        smtp_host="",
        smtp_from_email="",
        smtp_timeout_seconds=3,
    )
    return replace(base, **overrides)


def _resend_settings(**overrides) -> HostedSettings:
    return _settings(
        resend_api_key=API_KEY, resend_from_email="alerts@example.com", **overrides
    )


def notification() -> "object":
    return build_digest_email(
        recipient="verified@example.com",
        frequency="as_detected",
        jobs=[job()],
        message_id=MESSAGE_ID,
        public_frontend_url="https://internships.example",
    )


def _transport(handler) -> ResendNotificationTransport:
    return ResendNotificationTransport(
        _resend_settings(), transport=httpx.MockTransport(handler)
    )


def _responder(status_code: int, payload: object = None):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, json={"id": "provider-id"} if payload is None else payload
        )

    return handler


def test_job_alerts_choose_resend_when_it_is_configured() -> None:
    settings = _resend_settings(
        smtp_host="smtp.example.com", smtp_from_email="smtp@example.com"
    )
    assert isinstance(
        configured_notification_transport(settings), ResendNotificationTransport
    )


def test_job_alerts_fall_back_to_smtp_when_resend_is_absent() -> None:
    settings = _settings(
        smtp_host="smtp.example.com", smtp_from_email="smtp@example.com"
    )
    assert isinstance(
        configured_notification_transport(settings), SMTPNotificationTransport
    )


def test_job_alerts_report_a_safe_failure_when_no_provider_is_configured() -> None:
    transport = configured_notification_transport(_settings())
    assert isinstance(transport, UnavailableNotificationTransport)
    result = transport.send(notification())
    assert (result.outcome, result.error_code) == (
        "permanent_failure",
        "mail_not_configured",
    )


def test_a_resend_transport_without_credentials_never_claims_delivery() -> None:
    result = ResendNotificationTransport(_settings()).send(notification())
    assert (result.outcome, result.error_code) == (
        "permanent_failure",
        "mail_not_configured",
    )


def test_resend_request_carries_sender_recipient_subject_text_and_html() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = json.loads(request.read().decode())
        return httpx.Response(200, json={"id": "provider-id"})

    message = notification()
    assert _transport(handler).send(message).outcome == "sent"

    body = seen["body"]
    assert seen["url"] == RESEND_ENDPOINT
    assert seen["auth"] == f"Bearer {API_KEY}"
    assert seen["content_type"] == "application/json"
    assert body["from"] == "alerts@example.com"
    assert body["to"] == [message.recipient]
    assert body["subject"] == message.subject
    assert body["text"] == message.text
    assert body["html"] == message.html
    # The batch's deterministic Message-ID is forwarded as a custom header, so
    # every attempt for a batch reuses it exactly as the SMTP transport does.
    assert body["headers"]["Message-ID"] == MESSAGE_ID
    # Privacy-sensitive digest rules still hold over HTTPS.
    assert "must-not-render" not in json.dumps(body)


@pytest.mark.parametrize("status_code", [200, 201, 202])
def test_resend_accepted_responses_are_sent(status_code: int) -> None:
    result = _transport(_responder(status_code)).send(notification())
    assert (result.outcome, result.error_code) == ("sent", None)


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (401, "resend_authentication_failed"),
        (403, "resend_authentication_failed"),
        (400, "resend_request_rejected"),
        (422, "resend_request_rejected"),
    ],
)
def test_resend_rejections_are_permanent(status_code: int, code: str) -> None:
    result = _transport(_responder(status_code)).send(notification())
    assert (result.outcome, result.error_code) == ("permanent_failure", code)


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (408, "resend_request_timeout"),
        (429, "resend_rate_limited"),
        (500, "resend_server_error"),
        (503, "resend_server_error"),
    ],
)
def test_resend_transient_responses_retry(status_code: int, code: str) -> None:
    result = _transport(_responder(status_code)).send(notification())
    assert (result.outcome, result.error_code) == ("retryable_failure", code)


def test_an_unexpected_resend_status_is_never_treated_as_delivered() -> None:
    result = _transport(_responder(302)).send(notification())
    assert (result.outcome, result.error_code) == (
        "uncertain",
        "resend_response_unknown",
    )


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("name resolution failed"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.PoolTimeout("pool timed out"),
    ],
)
def test_resend_failures_known_to_precede_submission_retry(failure) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise failure

    result = _transport(handler).send(notification())
    assert (result.outcome, result.error_code) == (
        "retryable_failure",
        "resend_connection_failed_before_submission",
    )


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ReadError("connection reset"),
    ],
)
def test_resend_failures_after_submission_may_have_begun_are_uncertain(
    failure,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise failure

    result = _transport(handler).send(notification())
    assert (result.outcome, result.error_code) == (
        "uncertain",
        "resend_uncertain_after_submission",
    )


def test_resend_failures_never_expose_the_key_body_or_recipient(caplog) -> None:
    message = notification()
    secret_body = "provider said: internal-account-detail"

    def rejecting(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": secret_body})

    def raising(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out sending {request.read().decode()}")

    with caplog.at_level(logging.DEBUG):
        rejected = _transport(rejecting).send(message)
        raised = _transport(raising).send(message)

    leaks = (API_KEY, secret_body, message.recipient, message.text, message.html)
    for result in (rejected, raised):
        rendered = f"{result.outcome} {result.error_code}"
        assert all(leak not in rendered for leak in leaks)
    assert all(leak not in caplog.text for leak in leaks)


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("resend", ResendNotificationTransport),
        ("smtp", SMTPNotificationTransport),
        ("none", UnavailableNotificationTransport),
    ],
)
def test_the_delivery_cli_builds_its_transport_from_the_provider_selector(
    monkeypatch, provider: str, expected: type
) -> None:
    """The one-shot worker must honour the same precedence as account mail."""

    from app.hosted import deliver_notifications
    from app.hosted.notification_worker import WorkerSummary

    for name in (
        "HOSTED_RESEND_API_KEY",
        "HOSTED_RESEND_FROM_EMAIL",
        "HOSTED_SMTP_HOST",
        "HOSTED_SMTP_FROM_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(
        "HOSTED_DATABASE_URL", "postgresql+psycopg://user:pw@localhost:1/db"
    )
    if provider == "resend":
        monkeypatch.setenv("HOSTED_RESEND_API_KEY", API_KEY)
        monkeypatch.setenv("HOSTED_RESEND_FROM_EMAIL", "alerts@example.com")
    elif provider == "smtp":
        monkeypatch.setenv("HOSTED_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("HOSTED_SMTP_FROM_EMAIL", "smtp@example.com")

    captured: dict[str, object] = {}

    class StubDatabase:
        def __init__(self, url: str) -> None:
            captured["url"] = url

        def dispose(self) -> None:
            captured["disposed"] = True

    class StubWorker:
        def __init__(self, _database, transport, _public_frontend_url) -> None:
            captured["transport"] = transport

        def run(self, *, limit: int) -> WorkerSummary:
            captured["limit"] = limit
            return WorkerSummary()

    monkeypatch.setattr(deliver_notifications, "HostedDatabase", StubDatabase)
    monkeypatch.setattr(deliver_notifications, "NotificationDeliveryWorker", StubWorker)

    assert deliver_notifications.main([]) == 0
    assert isinstance(captured["transport"], expected)
    assert captured["limit"] == 25
    assert captured["disposed"] is True
