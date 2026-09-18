"""Deployment configuration: one resolver for the runtime and for Alembic.

Railway exports ``DATABASE_URL``; this repository's original convention is
``HOSTED_DATABASE_URL``. Both must work, and FastAPI startup and Alembic must
never disagree about which one they picked.
"""

from __future__ import annotations

from configparser import InterpolationSyntaxError
from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command
from app.hosted.database import (
    alembic_config_url,
    database_url_from_env,
    normalize_database_url,
)
from app.hosted.settings import HostedSettings

BACKEND_DIR = Path(__file__).resolve().parents[1]
HOSTED_URL = "postgresql+psycopg://hosted:hosted-secret@hosted.example.com/hosted"
RAILWAY_URL = "postgresql://railway:railway-secret@railway.internal:5432/railway"


@pytest.fixture
def clean_database_env(monkeypatch):
    """Start every case from an environment with neither variable set."""

    monkeypatch.delenv("HOSTED_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return monkeypatch


def test_only_hosted_database_url_is_selected(clean_database_env) -> None:
    clean_database_env.setenv("HOSTED_DATABASE_URL", HOSTED_URL)

    assert database_url_from_env() == HOSTED_URL


def test_only_railway_database_url_is_selected(clean_database_env) -> None:
    clean_database_env.setenv("DATABASE_URL", RAILWAY_URL)

    assert database_url_from_env() == RAILWAY_URL


def test_hosted_database_url_wins_when_both_are_set(clean_database_env) -> None:
    clean_database_env.setenv("HOSTED_DATABASE_URL", HOSTED_URL)
    clean_database_env.setenv("DATABASE_URL", RAILWAY_URL)

    assert database_url_from_env() == HOSTED_URL


def test_blank_hosted_database_url_falls_back_to_database_url(
    clean_database_env,
) -> None:
    clean_database_env.setenv("HOSTED_DATABASE_URL", "   ")
    clean_database_env.setenv("DATABASE_URL", RAILWAY_URL)

    assert database_url_from_env() == RAILWAY_URL


def test_surrounding_whitespace_is_stripped(clean_database_env) -> None:
    clean_database_env.setenv("DATABASE_URL", f"  {RAILWAY_URL}\n")

    assert database_url_from_env() == RAILWAY_URL


@pytest.mark.parametrize(
    ("hosted", "railway"),
    [(None, None), ("", ""), ("  ", "\n")],
)
def test_missing_or_blank_values_resolve_to_none(
    clean_database_env, hosted: str | None, railway: str | None
) -> None:
    if hosted is not None:
        clean_database_env.setenv("HOSTED_DATABASE_URL", hosted)
    if railway is not None:
        clean_database_env.setenv("DATABASE_URL", railway)

    assert database_url_from_env() is None


@pytest.mark.parametrize(
    ("hosted", "railway", "expected"),
    [
        (HOSTED_URL, None, HOSTED_URL),
        (None, RAILWAY_URL, RAILWAY_URL),
        (HOSTED_URL, RAILWAY_URL, HOSTED_URL),
        ("   ", RAILWAY_URL, RAILWAY_URL),
        (None, None, None),
    ],
)
def test_hosted_settings_follow_the_shared_resolver(
    clean_database_env,
    hosted: str | None,
    railway: str | None,
    expected: str | None,
) -> None:
    if hosted is not None:
        clean_database_env.setenv("HOSTED_DATABASE_URL", hosted)
    if railway is not None:
        clean_database_env.setenv("DATABASE_URL", railway)

    assert HostedSettings.from_env().database_url == expected


def test_settings_never_expose_the_database_url(clean_database_env) -> None:
    clean_database_env.setenv("DATABASE_URL", RAILWAY_URL)

    assert "railway-secret" not in repr(HostedSettings.from_env())


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "postgres://user:pass@host:5432/db",
            "postgresql+psycopg://user:pass@host:5432/db",
        ),
        (
            "postgresql://user:pass@host:5432/db",
            "postgresql+psycopg://user:pass@host:5432/db",
        ),
        (
            "postgresql+psycopg://user:pass@host:5432/db",
            "postgresql+psycopg://user:pass@host:5432/db",
        ),
    ],
)
def test_postgres_url_normalization(raw: str, expected: str) -> None:
    assert normalize_database_url(raw) == expected


def test_percent_encoded_credentials_survive_alembic_interpolation() -> None:
    raw = "postgresql://user:p%40ss%25word@host:5432/db"
    config = Config(str(BACKEND_DIR / "alembic.ini"))

    config.set_main_option("sqlalchemy.url", alembic_config_url(raw))

    assert config.get_main_option("sqlalchemy.url") == normalize_database_url(raw)


def test_unescaped_percent_would_break_alembic_configuration() -> None:
    """Guards the escaping above: the raw URL is genuinely unusable."""

    config = Config(str(BACKEND_DIR / "alembic.ini"))

    with pytest.raises((InterpolationSyntaxError, ValueError)):
        config.set_main_option(
            "sqlalchemy.url", "postgresql://user:p%40ss@host:5432/db"
        )


def test_alembic_runs_offline_with_database_url_and_percent_credentials(
    clean_database_env, capsys
) -> None:
    """End-to-end through production env.py, without contacting a database."""

    clean_database_env.setenv(
        "DATABASE_URL", "postgresql://user:p%40ss%25word@host:5432/db"
    )
    config = Config(str(BACKEND_DIR / "alembic.ini"))

    command.upgrade(config, "head", sql=True)

    emitted = capsys.readouterr().out
    assert "CREATE TABLE hosted_users" in emitted
    assert "p%40ss%25word" not in emitted


def test_alembic_error_names_both_supported_variables(clean_database_env) -> None:
    config = Config(str(BACKEND_DIR / "alembic.ini"))

    with pytest.raises(RuntimeError) as failure:
        command.upgrade(config, "head", sql=True)

    message = str(failure.value)
    assert "DATABASE_URL" in message
    assert "HOSTED_DATABASE_URL" in message
