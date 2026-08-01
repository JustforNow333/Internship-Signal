from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.hosted.catalog import CompanyCatalog, company_slug
from app.hosted.database import HostedDatabase
from app.hosted.schemas import CompanyRequestInput, PreferencesUpdate, WatchlistUpdate
from app.hosted.security import (
    hash_password,
    normalized_email,
    token_hash,
    verify_password,
)
from app.hosted.settings import HostedSettings


def test_argon2_password_hash_and_opaque_token_hash() -> None:
    password = "correct horse battery staple"
    password_hash = hash_password(password)

    assert password not in password_hash
    assert password_hash.startswith("$argon2id$")
    assert verify_password(password_hash, password)
    assert not verify_password(password_hash, "incorrect password")
    assert token_hash("raw secret token") != "raw secret token"
    assert len(token_hash("raw secret token")) == 64


def test_email_normalization_is_consistent_and_case_insensitive() -> None:
    display, key = normalized_email("  Student@Example.COM ")

    assert display == "Student@example.com"
    assert key == "student@example.com"


def test_hosted_settings_reject_wildcard_cors_and_invalid_lifetimes(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HOSTED_ALLOWED_FRONTEND_ORIGINS", "*")
    with pytest.raises(ValueError, match="wildcard"):
        HostedSettings.from_env()

    monkeypatch.setenv("HOSTED_ALLOWED_FRONTEND_ORIGINS", "https://app.example.com")
    monkeypatch.setenv("HOSTED_SESSION_LIFETIME_SECONDS", "0")
    with pytest.raises(ValueError, match="positive integer"):
        HostedSettings.from_env()

    monkeypatch.delenv("HOSTED_ALLOWED_FRONTEND_ORIGINS")
    monkeypatch.setenv("CORS_ORIGINS", "https://legacy-ui.example.com")
    monkeypatch.setenv("HOSTED_SESSION_LIFETIME_SECONDS", "3600")
    assert HostedSettings.from_env().allowed_frontend_origins == (
        "https://legacy-ui.example.com",
    )

    with pytest.raises(ValueError, match="must use PostgreSQL"):
        HostedDatabase("sqlite+pysqlite:///:memory:")
    with pytest.raises(ValueError, match="valid PostgreSQL URL"):
        HostedDatabase("not a database URL")

    monkeypatch.setenv("HOSTED_SESSION_COOKIE_NAME", "unsafe;cookie")
    with pytest.raises(ValueError, match="valid cookie name"):
        HostedSettings.from_env()

    monkeypatch.setenv("HOSTED_SESSION_COOKIE_NAME", "hosted_session")
    monkeypatch.setenv(
        "HOSTED_ALLOWED_FRONTEND_ORIGINS", "https://user:secret@example.com"
    )
    with pytest.raises(ValueError, match=r"HTTP\(S\) origins"):
        HostedSettings.from_env()

    monkeypatch.setenv("HOSTED_ALLOWED_FRONTEND_ORIGINS", "https://app.example.com")
    monkeypatch.setenv(
        "HOSTED_DATABASE_URL",
        "postgresql+psycopg://user:database-secret@example.com/hosted",
    )
    monkeypatch.setenv("HOSTED_SMTP_USERNAME", "smtp-private-user")
    monkeypatch.setenv("HOSTED_SMTP_PASSWORD", "smtp-secret")
    settings_repr = repr(HostedSettings.from_env())
    assert "database-secret" not in settings_repr
    assert "smtp-private-user" not in settings_repr
    assert "smtp-secret" not in settings_repr

    monkeypatch.setenv(
        "HOSTED_PUBLIC_FRONTEND_URL", "https://user:secret@app.example.com"
    )
    with pytest.raises(ValueError, match=r"safe HTTP\(S\) URL"):
        HostedSettings.from_env()

    monkeypatch.setenv("HOSTED_PUBLIC_FRONTEND_URL", "https://app.example.com")
    monkeypatch.setenv("HOSTED_SMTP_FROM_EMAIL", "not an email")
    with pytest.raises(ValueError, match="valid email"):
        HostedSettings.from_env()


def test_preferences_reject_unsupported_duplicates_and_oversized_values() -> None:
    valid = {
        "role_ids": ["software_engineering"],
        "preferred_locations": ["Boston, MA"],
        "include_remote": True,
        "internship_season": "Summer 2027",
        "alert_frequency": "as_detected",
        "globally_paused": False,
    }
    assert PreferencesUpdate(**valid).role_ids == ["software_engineering"]

    with pytest.raises(ValidationError):
        PreferencesUpdate(**{**valid, "role_ids": ["not_supported"]})
    with pytest.raises(ValidationError):
        PreferencesUpdate(
            **{**valid, "preferred_locations": ["Boston, MA", "boston, ma"]}
        )
    with pytest.raises(ValidationError):
        PreferencesUpdate(**{**valid, "alert_frequency": "instant"})


def test_watchlist_and_company_request_input_validation() -> None:
    with pytest.raises(ValidationError):
        WatchlistUpdate(
            companies=[
                {"company_id": "stripe", "paused": False},
                {"company_id": "stripe", "paused": True},
            ]
        )
    assert (
        CompanyRequestInput(
            company_name="Example", career_url="https://example.com/careers"
        ).career_url
        is not None
    )
    for unsafe_url in (
        "ftp://example.com/jobs",
        "https://user:password@example.com/jobs",
        "not a URL",
    ):
        with pytest.raises(ValidationError):
            CompanyRequestInput(company_name="Example", career_url=unsafe_url)


def test_catalog_is_derived_and_sanitized() -> None:
    catalog = CompanyCatalog.from_watcher_config()
    payloads = [company.as_dict() for company in catalog.companies]

    assert payloads
    assert company_slug("Capital One") == "capital-one"
    assert {"direct", "backstop"}.issubset(
        {company["coverage"] for company in payloads}
    )
    assert all(
        set(company) == {"id", "name", "aliases", "coverage", "selectable"}
        for company in payloads
    )
    serialized = repr(payloads).casefold()
    for internal_key in (
        "token",
        "workday_site",
        "workday_shard",
        "github_listing",
        "alumni_match",
        "source_url",
    ):
        assert internal_key not in serialized
