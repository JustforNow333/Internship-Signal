"""Alembic environment for the hosted PostgreSQL schema."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.hosted import models  # noqa: F401 - registers model metadata
from app.hosted.database import Base, alembic_config_url, database_url_from_env

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# An explicit sqlalchemy.url (used by the PostgreSQL test fixtures) wins, then
# the shared resolver the FastAPI runtime uses. The URL itself is never printed.
database_url = config.get_main_option("sqlalchemy.url") or database_url_from_env()
if not database_url:
    raise RuntimeError(
        "DATABASE_URL or HOSTED_DATABASE_URL is required for Alembic migrations"
    )
config.set_main_option("sqlalchemy.url", alembic_config_url(database_url))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
