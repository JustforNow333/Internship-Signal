"""Schema foundation for the persistent Applied match status.

``hosted_user_job_matches.applied_at`` is a nullable timestamptz added by
exactly one migration after ``20261025_0010`` together with the partial index
``ix_hosted_user_job_matches_user_applied``. Adding it must not rewrite any
existing match row, the downgrade drops both, and the models stay in sync with
the migrated schema.

These need ``HOSTED_TEST_DATABASE_URL`` and skip without it, like every
PostgreSQL test; the database-free model and revision-chain checks live in
``test_hosted_match_applied_model``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect, text

from alembic import command
from app.hosted.database import alembic_config_url
from tests.test_hosted_notifications import (  # noqa: F401 - pytest fixtures
    MutableClock,
    catalog,
    clean_database,
    clock,
    create_user,
    database,
    final_job,
    import_jobs,
    notification_postgres_url,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "20261025_0010"
APPLIED_REVISION = "20261119_0011"
# 20261121_0012 (education eligibility) now follows 0011 as the single head; it
# touches neither hosted_user_job_matches nor the applied index.
HEAD_REVISION = "20261121_0012"
INDEX_NAME = "ix_hosted_user_job_matches_user_applied"
EXPECTED_INDEXDEF = (
    f"CREATE INDEX {INDEX_NAME} ON public.hosted_user_job_matches "
    "USING btree (user_id, applied_at) WHERE (applied_at IS NOT NULL)"
)


def _alembic(url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", alembic_config_url(url))
    return config


def _applied_column(connection):
    return connection.execute(
        text(
            "SELECT data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE "
            "table_name = 'hosted_user_job_matches' AND column_name = 'applied_at'"
        )
    ).one_or_none()


def _applied_indexdef(connection):
    return connection.scalar(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
        {"name": INDEX_NAME},
    )


def _revision(connection) -> str:
    return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _match_rows(connection) -> dict:
    """Every stored match column except applied_at, plus the tuple version."""

    rows = connection.execute(
        text(
            "SELECT id, xmin::text AS tuple_version, user_id, job_id, "
            "match_reasons, matched_at, last_matched_at, no_longer_matches_at, "
            "saved_at, dismissed_at, created_at, updated_at "
            "FROM hosted_user_job_matches ORDER BY id"
        )
    ).mappings()
    return {row["id"]: dict(row) for row in rows}


def test_head_schema_has_nullable_timestamptz_applied_at_and_partial_index(
    database,
) -> None:
    with database.engine.connect() as connection:
        assert _revision(connection) == HEAD_REVISION
        assert _applied_column(connection) == (
            "timestamp with time zone",
            "YES",
            None,
        )
        assert _applied_indexdef(connection) == EXPECTED_INDEXDEF


def test_upgrade_keeps_existing_match_rows_untouched(
    database, catalog, clock, notification_postgres_url
) -> None:
    """Rows stored before the revision come back byte-for-byte, applied_at NULL.

    PostgreSQL bumps ``xmin`` whenever a row is rewritten, so an unchanged
    ``xmin`` proves the upgrade issued no UPDATE and no table rewrite.
    """

    alembic = _alembic(notification_postgres_url)
    with database.session_factory.begin() as db:
        first = create_user(db, clock(), "first@example.com")
        second = create_user(db, clock(), "second@example.com")
    import_jobs(database, catalog, clock, [final_job(1), final_job(2)], "a")
    stamp = datetime(2026, 8, 5, 9, 30, tzinfo=UTC)
    with database.engine.begin() as connection:
        # Varied saved/dismissed/historical state on pre-existing rows.
        connection.execute(
            text(
                "UPDATE hosted_user_job_matches SET saved_at = :at "
                "WHERE user_id = :user"
            ),
            {"at": stamp, "user": first},
        )
        connection.execute(
            text(
                "UPDATE hosted_user_job_matches SET dismissed_at = :at, "
                "no_longer_matches_at = :at WHERE user_id = :user"
            ),
            {"at": stamp, "user": second},
        )
    database.engine.dispose()
    try:
        command.downgrade(alembic, PREVIOUS_REVISION)
        with database.engine.connect() as connection:
            assert _revision(connection) == PREVIOUS_REVISION
            assert _applied_column(connection) is None
            before = _match_rows(connection)
        assert len(before) == 4
        database.engine.dispose()

        command.upgrade(alembic, APPLIED_REVISION)
        with database.engine.connect() as connection:
            assert _revision(connection) == APPLIED_REVISION
            after = _match_rows(connection)
            applied = connection.execute(
                text("SELECT applied_at FROM hosted_user_job_matches")
            ).scalars().all()
        assert after == before
        assert applied == [None] * 4
        database.engine.dispose()

        # Continuing to head (education eligibility) leaves match rows alone too.
        command.upgrade(alembic, "head")
        with database.engine.connect() as connection:
            assert _revision(connection) == HEAD_REVISION
            assert _match_rows(connection) == before
            assert connection.execute(
                text("SELECT applied_at FROM hosted_user_job_matches")
            ).scalars().all() == [None] * 4
    finally:
        database.engine.dispose()
        command.upgrade(alembic, "head")


def test_upgrade_downgrade_upgrade_round_trip_has_no_drift(
    database, catalog, clock, notification_postgres_url
) -> None:
    alembic = _alembic(notification_postgres_url)
    with database.session_factory.begin() as db:
        user = create_user(db, clock(), "applied@example.com")
    import_jobs(database, catalog, clock, [final_job(1)], "a")
    applied_at = datetime(2026, 8, 6, 15, 0, tzinfo=UTC)
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE hosted_user_job_matches SET applied_at = :at "
                "WHERE user_id = :user"
            ),
            {"at": applied_at, "user": user},
        )
        stored = connection.scalar(
            text("SELECT applied_at FROM hosted_user_job_matches")
        )
    assert stored == applied_at
    database.engine.dispose()
    try:
        command.downgrade(alembic, PREVIOUS_REVISION)
        with database.engine.connect() as connection:
            assert _revision(connection) == PREVIOUS_REVISION
            assert _applied_column(connection) is None
            assert _applied_indexdef(connection) is None
            assert "applied_at" not in {
                column["name"]
                for column in inspect(connection).get_columns(
                    "hosted_user_job_matches"
                )
            }
            # The downgrade drops only the new column and index.
            assert connection.scalar(
                text("SELECT count(*) FROM hosted_user_job_matches")
            ) == 1
        database.engine.dispose()

        command.upgrade(alembic, "head")
        with database.engine.connect() as connection:
            assert _revision(connection) == HEAD_REVISION
            assert _applied_column(connection) == (
                "timestamp with time zone",
                "YES",
                None,
            )
            assert _applied_indexdef(connection) == EXPECTED_INDEXDEF
            # A downgrade discards the value; nothing invents a new one.
            assert connection.scalar(
                text("SELECT applied_at FROM hosted_user_job_matches")
            ) is None
        database.engine.dispose()
        # Models and the migrated schema agree (raises on any drift).
        command.check(alembic)
    finally:
        database.engine.dispose()
        command.upgrade(alembic, "head")
