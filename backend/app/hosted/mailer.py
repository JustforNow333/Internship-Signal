"""Verification/reset mail delivery with injectable test support."""

from __future__ import annotations

import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import httpx

from .settings import HostedSettings

RESEND_ENDPOINT = "https://api.resend.com/emails"


@dataclass(frozen=True)
class OutboundMessage:
    recipient: str
    subject: str
    text: str
    kind: str


class Mailer(Protocol):
    def send(self, message: OutboundMessage) -> bool: ...


class MailerDeliveryError(RuntimeError):
    """A bounded delivery failure that callers may safely handle."""


class DisabledMailer:
    def send(self, message: OutboundMessage) -> bool:
        return False


class InMemoryMailer:
    def __init__(self, *, accept: bool = True) -> None:
        self.accept = accept
        self.messages: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> bool:
        if not self.accept:
            return False
        self.messages.append(message)
        return True


class SMTPMailer:
    def __init__(self, settings: HostedSettings) -> None:
        self.settings = settings

    def send(self, message: OutboundMessage) -> bool:
        email = EmailMessage()
        email["From"] = self.settings.smtp_from_email
        email["To"] = message.recipient
        email["Subject"] = message.subject
        email.set_content(message.text)
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
                rejected = smtp.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            raise MailerDeliveryError("SMTP delivery failed") from exc
        return not rejected


class ResendMailer:
    """HTTPS transactional delivery for hosts that block outbound SMTP.

    Provider responses are never returned or logged: a caller only learns that
    delivery was accepted, or receives a bounded MailerDeliveryError.
    """

    def __init__(
        self,
        settings: HostedSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self._transport = transport

    def send(self, message: OutboundMessage) -> bool:
        payload = {
            "from": self.settings.resend_from_email,
            "to": [message.recipient],
            "subject": message.subject,
            "text": message.text,
        }
        try:
            with httpx.Client(
                timeout=self.settings.smtp_timeout_seconds,
                transport=self._transport,
            ) as client:
                response = client.post(
                    RESEND_ENDPOINT,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.settings.resend_api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            # Only the exception type crosses this boundary; httpx messages can
            # quote the request, never the Authorization header.
            raise MailerDeliveryError(
                f"Resend delivery failed ({type(exc).__name__})"
            ) from None
        if response.status_code // 100 != 2:
            raise MailerDeliveryError(
                f"Resend delivery was rejected (HTTP {response.status_code})"
            )
        try:
            accepted = response.json()
        except ValueError:
            raise MailerDeliveryError(
                "Resend returned a malformed delivery response"
            ) from None
        if not isinstance(accepted, dict):
            raise MailerDeliveryError("Resend returned a malformed delivery response")
        return True


def configured_mailer(settings: HostedSettings) -> Mailer:
    """Explicit precedence: Resend HTTPS, then SMTP, then no delivery."""

    if settings.resend_configured:
        return ResendMailer(settings)
    if settings.smtp_configured:
        return SMTPMailer(settings)
    return DisabledMailer()


def verification_message(recipient: str, verification_url: str) -> OutboundMessage:
    return OutboundMessage(
        recipient=recipient,
        subject="Verify your Internship Signal email",
        text=(
            "Verify your email to finish setting up Internship Signal:\n\n"
            f"{verification_url}\n\n"
            "This link expires and can be used only once."
        ),
        kind="verification",
    )


def password_reset_message(recipient: str, reset_url: str) -> OutboundMessage:
    return OutboundMessage(
        recipient=recipient,
        subject="Reset your Internship Signal password",
        text=(
            "Use this link to reset your Internship Signal password:\n\n"
            f"{reset_url}\n\n"
            "This link expires and can be used only once. If you did not request "
            "a reset, you can ignore this message."
        ),
        kind="password_reset",
    )
