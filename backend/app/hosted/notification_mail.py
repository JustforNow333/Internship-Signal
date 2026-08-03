"""Typed hosted notification transport and privacy-safe digest rendering."""

from __future__ import annotations

import html
import smtplib
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from typing import Literal, Protocol

from .matching import bounded_reasons
from .settings import HostedSettings

DeliveryOutcome = Literal[
    "sent", "retryable_failure", "permanent_failure", "uncertain"
]


@dataclass(frozen=True)
class DeliveryResult:
    outcome: DeliveryOutcome
    error_code: str | None = None


@dataclass(frozen=True)
class NotificationEmail:
    recipient: str
    subject: str
    text: str
    html: str
    message_id: str


class NotificationTransport(Protocol):
    def send(self, message: NotificationEmail) -> DeliveryResult: ...


@dataclass(frozen=True)
class DigestJob:
    company_name: str
    title: str
    location: str
    remote_status: str
    posting_date: date | None
    deadline: date | None
    application_url: str | None
    match_reasons: object


REASON_LABELS = {
    "company_watched": "Company is on your watchlist",
    "role_selected": "Role matches your selected categories",
    "location_any": "Location matches your open location preference",
    "location_preferred": "Location matches your preferred locations",
    "location_united_states": "Location is compatible with your U.S. preference",
    "remote_included": "Remote work is included in your preferences",
    "season_any": "Any internship season is included",
    "season_match": "Internship season matches your preference",
    "season_unspecified": "No conflicting internship season was listed",
}
DISPLAY_LIMIT = 25


class SMTPNotificationTransport:
    """Hosted SMTP transport with conservative, phase-aware outcomes."""

    def __init__(self, settings: HostedSettings) -> None:
        self.settings = settings

    def send(self, message: NotificationEmail) -> DeliveryResult:
        if not self.settings.smtp_configured:
            return DeliveryResult("permanent_failure", "smtp_not_configured")

        email = EmailMessage()
        email["From"] = self.settings.smtp_from_email
        email["To"] = message.recipient
        email["Subject"] = message.subject
        email["Message-ID"] = message.message_id
        email.set_content(message.text)
        email.add_alternative(message.html, subtype="html")
        submission_started = False
        try:
            with smtplib.SMTP(
                self.settings.smtp_host,
                self.settings.smtp_port,
                timeout=self.settings.smtp_timeout_seconds,
            ) as smtp:
                if self.settings.smtp_starttls:
                    smtp.starttls()
                if self.settings.smtp_username:
                    smtp.login(
                        self.settings.smtp_username,
                        self.settings.smtp_password,
                    )
                submission_started = True
                rejected = smtp.send_message(email)
                if rejected:
                    return DeliveryResult("permanent_failure", "recipient_rejected")
            return DeliveryResult("sent")
        except smtplib.SMTPAuthenticationError:
            return DeliveryResult("permanent_failure", "smtp_authentication_failed")
        except smtplib.SMTPSenderRefused:
            return DeliveryResult("permanent_failure", "sender_rejected")
        except smtplib.SMTPRecipientsRefused:
            return DeliveryResult("permanent_failure", "recipient_rejected")
        except smtplib.SMTPConnectError:
            return DeliveryResult(
                "retryable_failure", "connection_failed_before_submission"
            )
        except smtplib.SMTPDataError as exc:
            if 400 <= exc.smtp_code < 500:
                return DeliveryResult("retryable_failure", "smtp_data_4xx")
            if 500 <= exc.smtp_code < 600:
                return DeliveryResult("permanent_failure", "smtp_data_5xx")
            return DeliveryResult("uncertain", "smtp_data_unknown")
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code < 500:
                return DeliveryResult("retryable_failure", "smtp_4xx")
            if 500 <= exc.smtp_code < 600:
                return DeliveryResult("permanent_failure", "smtp_5xx")
            return DeliveryResult(
                "uncertain" if submission_started else "retryable_failure",
                "smtp_response_unknown",
            )
        except (smtplib.SMTPServerDisconnected, TimeoutError, OSError):
            return DeliveryResult(
                "uncertain" if submission_started else "retryable_failure",
                "connection_lost_after_submission"
                if submission_started
                else "connection_failed_before_submission",
            )
        except smtplib.SMTPException:
            return DeliveryResult(
                "uncertain" if submission_started else "retryable_failure",
                "smtp_unexpected_after_submission"
                if submission_started
                else "smtp_setup_failed",
            )
        except Exception:  # noqa: BLE001 - transport boundary must classify safely
            return DeliveryResult(
                "uncertain" if submission_started else "retryable_failure",
                "unexpected_after_submission"
                if submission_started
                else "unexpected_before_submission",
            )


def build_digest_email(
    *,
    recipient: str,
    frequency: str,
    jobs: list[DigestJob],
    message_id: str,
    public_frontend_url: str,
) -> NotificationEmail:
    count = len(jobs)
    subject = (
        f"New internship matches ({count})"
        if frequency == "as_detected"
        else f"Your Internship Signal digest ({count})"
    )
    dashboard_url = f"{public_frontend_url.rstrip('/')}/app/matches"
    settings_url = f"{public_frontend_url.rstrip('/')}/app/settings"
    displayed = jobs[:DISPLAY_LIMIT]
    remaining = max(0, count - len(displayed))

    text_parts = [subject, ""]
    html_parts = [
        "<!doctype html><html><body>",
        f"<h1>{html.escape(subject)}</h1>",
    ]
    for job in displayed:
        reasons = _reason_labels(job.match_reasons)
        text_parts.extend(_plain_job(job, reasons))
        html_parts.extend(_html_job(job, reasons))
    if remaining:
        notice = (
            f"{remaining} additional match{'es' if remaining != 1 else ''} "
            "is available on your dashboard."
        )
        text_parts.extend([notice, ""])
        html_parts.append(f"<p>{html.escape(notice)}</p>")
    text_parts.extend(
        [f"View all matches: {dashboard_url}", f"Notification settings: {settings_url}"]
    )
    html_parts.extend(
        [
            f'<p><a href="{html.escape(dashboard_url, quote=True)}">View all matches</a></p>',
            f'<p><a href="{html.escape(settings_url, quote=True)}">Notification settings</a></p>',
            "</body></html>",
        ]
    )
    return NotificationEmail(
        recipient=recipient,
        subject=subject,
        text="\n".join(text_parts),
        html="".join(html_parts),
        message_id=message_id,
    )


def _reason_labels(value: object) -> list[str]:
    return [
        REASON_LABELS[reason["code"]]
        for reason in bounded_reasons(value)
        if reason["code"] in REASON_LABELS
    ]


def _plain_job(job: DigestJob, reasons: list[str]) -> list[str]:
    lines = [f"{job.company_name} — {job.title}"]
    lines.append(f"Location: {job.location or 'Not specified'}")
    lines.append(f"Remote status: {job.remote_status or 'Not specified'}")
    if job.posting_date:
        lines.append(f"Posted: {job.posting_date.isoformat()}")
    if job.deadline:
        lines.append(f"Deadline: {job.deadline.isoformat()}")
    if reasons:
        lines.append("Why it matched: " + "; ".join(reasons))
    if job.application_url:
        lines.append(f"Apply: {job.application_url}")
    lines.append("")
    return lines


def _html_job(job: DigestJob, reasons: list[str]) -> list[str]:
    parts = [
        "<section>",
        f"<h2>{html.escape(job.company_name)} — {html.escape(job.title)}</h2>",
        f"<p><strong>Location:</strong> {html.escape(job.location or 'Not specified')}<br>",
        f"<strong>Remote status:</strong> {html.escape(job.remote_status or 'Not specified')}",
    ]
    if job.posting_date:
        parts.append(f"<br><strong>Posted:</strong> {job.posting_date.isoformat()}")
    if job.deadline:
        parts.append(f"<br><strong>Deadline:</strong> {job.deadline.isoformat()}")
    parts.append("</p>")
    if reasons:
        parts.append(
            f"<p><strong>Why it matched:</strong> {html.escape('; '.join(reasons))}</p>"
        )
    if job.application_url:
        parts.append(
            f'<p><a href="{html.escape(job.application_url, quote=True)}">Apply</a></p>'
        )
    parts.append("</section>")
    return parts
