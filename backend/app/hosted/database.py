"""SQLAlchemy engine and session construction for hosted PostgreSQL state."""

from __future__ import annotations

import os

from sqlalchemy import MetaData, create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# HOSTED_DATABASE_URL is this repository's original convention and keeps
# precedence. DATABASE_URL is what Railway and most managed PostgreSQL add-ons
# export, so both are accepted and resolved in exactly one place: the FastAPI
# runtime and Alembic must never disagree about where the URL came from.
DATABASE_URL_ENV_VARS = ("HOSTED_DATABASE_URL", "DATABASE_URL")


def database_url_from_env() -> str | None:
    """Return the configured hosted PostgreSQL URL, or None when unset.

    The value is never logged or echoed; callers receive it directly.
    """

    for name in DATABASE_URL_ENV_VARS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value
    return None


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def alembic_config_url(url: str) -> str:
    """Normalize a URL for Alembic's ConfigParser-backed configuration.

    Alembic stores ``sqlalchemy.url`` in a ConfigParser that applies ``%``
    interpolation, so percent characters in URL-encoded credentials must be
    escaped or they are read as interpolation syntax.
    """

    return normalize_database_url(url).replace("%", "%%")


class HostedDatabase:
    def __init__(self, url: str) -> None:
        self.url = normalize_database_url(url)
        try:
            backend_name = make_url(self.url).get_backend_name()
        except ArgumentError:
            raise ValueError(
                "DATABASE_URL or HOSTED_DATABASE_URL must be a "
                "valid PostgreSQL URL"
            ) from None
        if backend_name != "postgresql":
            raise ValueError(
                "DATABASE_URL or HOSTED_DATABASE_URL must use PostgreSQL"
            )
        self.engine: Engine = create_engine(self.url, pool_pre_ping=True)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    def dispose(self) -> None:
        self.engine.dispose()
