"""Environment-backed settings for the hosted multi-user API."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _origins() -> tuple[str, ...]:
    raw = os.getenv(
        "HOSTED_ALLOWED_FRONTEND_ORIGINS",
        os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ),
    )
    origins = tuple(
        origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()
    )
    if not origins:
        raise ValueError("HOSTED_ALLOWED_FRONTEND_ORIGINS must contain an origin")
    for origin in origins:
        try:
            parsed = urlsplit(origin)
            _ = parsed.port
        except ValueError:
            raise ValueError(
                "HOSTED_ALLOWED_FRONTEND_ORIGINS contains an invalid origin"
            ) from None
        if "*" in origin:
            raise ValueError("credentialed hosted CORS cannot use a wildcard origin")
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "HOSTED_ALLOWED_FRONTEND_ORIGINS must contain HTTP(S) origins only"
            )
    return origins


def _public_frontend_url() -> str:
    value = (
        os.getenv("HOSTED_PUBLIC_FRONTEND_URL", "http://localhost:5173")
        .strip()
        .rstrip("/")
    )
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ValueError("HOSTED_PUBLIC_FRONTEND_URL must be a valid URL") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("HOSTED_PUBLIC_FRONTEND_URL must be a safe HTTP(S) URL")
    return value


def _smtp_from_email() -> str:
    value = os.getenv("HOSTED_SMTP_FROM_EMAIL", "").strip()
    if not value:
        return ""
    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError:
        raise ValueError("HOSTED_SMTP_FROM_EMAIL must be a valid email") from None


@dataclass(frozen=True)
class HostedSettings:
    database_url: str | None = field(repr=False)
    session_lifetime_seconds: int
    session_cookie_name: str
    secure_cookies: bool
    allowed_frontend_origins: tuple[str, ...]
    verification_token_lifetime_seconds: int
    password_reset_token_lifetime_seconds: int
    public_frontend_url: str
    smtp_host: str
    smtp_port: int
    smtp_username: str = field(repr=False)
    smtp_password: str = field(repr=False)
    smtp_from_email: str = field(repr=False)
    smtp_starttls: bool
    smtp_timeout_seconds: int

    @classmethod
    def from_env(cls) -> HostedSettings:
        cookie_name = os.getenv(
            "HOSTED_SESSION_COOKIE_NAME", "internship_signal_session"
        ).strip()
        if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", cookie_name):
            raise ValueError("HOSTED_SESSION_COOKIE_NAME must be a valid cookie name")
        return cls(
            database_url=os.getenv("HOSTED_DATABASE_URL") or None,
            session_lifetime_seconds=_positive_int(
                "HOSTED_SESSION_LIFETIME_SECONDS", 14 * 24 * 60 * 60
            ),
            session_cookie_name=cookie_name,
            secure_cookies=_bool("HOSTED_SECURE_COOKIES", False),
            allowed_frontend_origins=_origins(),
            verification_token_lifetime_seconds=_positive_int(
                "HOSTED_VERIFICATION_TOKEN_LIFETIME_SECONDS", 24 * 60 * 60
            ),
            password_reset_token_lifetime_seconds=_positive_int(
                "HOSTED_PASSWORD_RESET_TOKEN_LIFETIME_SECONDS", 60 * 60
            ),
            public_frontend_url=_public_frontend_url(),
            smtp_host=os.getenv("HOSTED_SMTP_HOST", "").strip(),
            smtp_port=_positive_int("HOSTED_SMTP_PORT", 587),
            smtp_username=os.getenv("HOSTED_SMTP_USERNAME", "").strip(),
            smtp_password=os.getenv("HOSTED_SMTP_PASSWORD", ""),
            smtp_from_email=_smtp_from_email(),
            smtp_starttls=_bool("HOSTED_SMTP_STARTTLS", True),
            smtp_timeout_seconds=_positive_int("HOSTED_SMTP_TIMEOUT_SECONDS", 10),
        )

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from_email)
