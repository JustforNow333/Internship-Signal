"""SQLAlchemy engine and session construction for hosted PostgreSQL state."""

from __future__ import annotations

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


def normalize_database_url(url: str) -> str:
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


class HostedDatabase:
    def __init__(self, url: str) -> None:
        self.url = normalize_database_url(url)
        try:
            backend_name = make_url(self.url).get_backend_name()
        except ArgumentError:
            raise ValueError(
                "HOSTED_DATABASE_URL must be a valid PostgreSQL URL"
            ) from None
        if backend_name != "postgresql":
            raise ValueError("HOSTED_DATABASE_URL must use PostgreSQL")
        self.engine: Engine = create_engine(self.url, pool_pre_ping=True)
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    def dispose(self) -> None:
        self.engine.dispose()
