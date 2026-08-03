from __future__ import annotations

from datetime import date
from dataclasses import replace
import smtplib

from app.hosted.notification_mail import (
    DigestJob,
    SMTPNotificationTransport,
    build_digest_email,
)
from app.hosted.settings import HostedSettings


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
