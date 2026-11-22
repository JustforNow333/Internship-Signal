"""PostgreSQL schema foundation for Stage 3 education eligibility.

Revision ``20261121_0012`` (after ``20261119_0011``) adds
``hosted_user_preferences.education_level`` and ``class_standing`` (String,
NOT NULL, server default ``not_specified``, allowed-id CHECK constraints) and
the nullable JSON ``hosted_jobs.student_eligibility``. Upgrading must supply
only the defaults and leave every existing row otherwise untouched, the
downgrade drops all three columns, and the models stay in sync with the
migrated schema.

These need ``HOSTED_TEST_DATABASE_URL`` and skip without it, like every
PostgreSQL test; the database-free model and revision-chain checks live in
``test_hosted_education_model``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.hosted.database import alembic_config_url
from app.hosted.models import HostedJob, UserPreference
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
PREVIOUS_REVISION = "20261119_0011"
EDUCATION_REVISION = "20261121_0012"
PREFERENCE_COLUMNS = ("education_level", "class_standing")
JOB_COLUMN = "student_eligibility"
# Constraint name -> (column, allowed ids). Enforcement itself is exercised by
# test_constraints_enforce_allowed_ids_and_not_null.
CHECK_NAMES = {
    "ck_hosted_user_preferences_education_level": (
        "education_level",
        ("not_specified", "undergraduate", "masters", "doctoral"),
    ),
    "ck_hosted_user_preferences_class_standing": (
        "class_standing",
        ("not_specified", "first_year", "sophomore", "junior", "senior"),
    ),
}
EXPECTED_PREFERENCE_COLUMN = (
    "character varying",
    32,
    "NO",
    "'not_specified'::character varying",
)
EXPECTED_JOB_COLUMN = ("jsonb", None, "YES", None)


def _alembic(url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", alembic_config_url(url))
    return config


def _revision(connection) -> str:
    return connection.scalar(text("SELECT version_num FROM alembic_version"))


def _columns(connection) -> dict[tuple[str, str], tuple]:
    rows = connection.execute(
        text(
            "SELECT table_name, column_name, data_type, character_maximum_length, "
            "is_nullable, column_default FROM information_schema.columns WHERE "
            "(table_name = 'hosted_user_preferences' AND column_name IN "
            "('education_level', 'class_standing')) OR "
            "(table_name = 'hosted_jobs' AND column_name = 'student_eligibility')"
        )
    )
    return {
        (row.table_name, row.column_name): (
            row.data_type,
            row.character_maximum_length,
            row.is_nullable,
            row.column_default,
        )
        for row in rows
    }


def _checks(connection) -> dict[str, str]:
    rows = connection.execute(
        text(
            "SELECT conname, pg_get_constraintdef(oid) AS definition "
            "FROM pg_constraint WHERE conrelid = "
            "'public.hosted_user_preferences'::regclass AND contype = 'c' "
            "AND conname = ANY(:names)"
        ),
        {"names": list(CHECK_NAMES)},
    )
    return {row.conname: row.definition for row in rows}


def _assert_head_shape(connection) -> None:
    assert _revision(connection) == EDUCATION_REVISION
    assert _columns(connection) == {
        ("hosted_user_preferences", "education_level"): EXPECTED_PREFERENCE_COLUMN,
        ("hosted_user_preferences", "class_standing"): EXPECTED_PREFERENCE_COLUMN,
        ("hosted_jobs", JOB_COLUMN): EXPECTED_JOB_COLUMN,
    }
    checks = _checks(connection)
    assert set(checks) == set(CHECK_NAMES)
    for name, (column, allowed) in CHECK_NAMES.items():
        definition = checks[name]
        assert column in definition
        assert set(re.findall(r"'([^']*)'", definition)) == set(allowed)


def _assert_previous_shape(connection) -> None:
    assert _revision(connection) == PREVIOUS_REVISION
    assert _columns(connection) == {}
    assert _checks(connection) == {}
    assert not set(PREFERENCE_COLUMNS) & {
        column["name"]
        for column in inspect(connection).get_columns("hosted_user_preferences")
    }
    assert JOB_COLUMN not in {
        column["name"] for column in inspect(connection).get_columns("hosted_jobs")
    }


def _stored_rows(connection, table: str, key: str, ignore: tuple[str, ...]) -> dict:
    """Every stored column except ``ignore``, plus the tuple version.

    PostgreSQL bumps ``xmin`` whenever a row is rewritten, so an unchanged
    ``xmin`` proves the migration issued no UPDATE and no table rewrite.
    """

    removed = "ARRAY[" + ", ".join(f"'{name}'" for name in ignore) + "]::text[]"
    rows = connection.execute(
        text(
            f"SELECT {key}::text AS key, xmin::text AS tuple_version, "
            f"(to_jsonb(t) - {removed}) AS data FROM {table} t ORDER BY key"
        )
    ).mappings()
    return {row["key"]: (row["tuple_version"], row["data"]) for row in rows}


def _populate(database, catalog, clock) -> tuple:
    with database.session_factory.begin() as db:
        first = create_user(db, clock(), "first@example.com")
        second = create_user(db, clock(), "second@example.com", frequency="daily")
    eligibility = {
        "exclusion_reason": "graduate_only",
        "degree_level": "masters",
        "evidence_source": "requirements",
        "evidence": "Currently pursuing a Master's degree",
    }
    import_jobs(database, catalog, clock, [final_job(1), final_job(2)], "a")
    return first, second, eligibility


def test_head_schema_has_education_columns_constraints_and_json_eligibility(
    database,
) -> None:
    with database.engine.connect() as connection:
        _assert_head_shape(connection)


def test_upgrade_supplies_defaults_and_rewrites_no_existing_row(
    database, catalog, clock, notification_postgres_url
) -> None:
    alembic = _alembic(notification_postgres_url)
    _populate(database, catalog, clock)
    database.engine.dispose()
    try:
        command.downgrade(alembic, PREVIOUS_REVISION)
        with database.engine.connect() as connection:
            _assert_previous_shape(connection)
            preferences_before = _stored_rows(
                connection, "hosted_user_preferences", "user_id", ()
            )
            jobs_before = _stored_rows(connection, "hosted_jobs", "id", ())
            matches_before = _stored_rows(
                connection, "hosted_user_job_matches", "id", ()
            )
        assert len(preferences_before) == 2
        assert len(jobs_before) == 2
        assert len(matches_before) == 4
        database.engine.dispose()

        command.upgrade(alembic, "head")
        with database.engine.connect() as connection:
            _assert_head_shape(connection)
            # Byte-for-byte equal apart from the new columns, same tuple version.
            assert _stored_rows(
                connection, "hosted_user_preferences", "user_id", PREFERENCE_COLUMNS
            ) == preferences_before
            assert _stored_rows(
                connection, "hosted_jobs", "id", (JOB_COLUMN,)
            ) == jobs_before
            assert _stored_rows(
                connection, "hosted_user_job_matches", "id", ()
            ) == matches_before
            assert connection.execute(
                text(
                    "SELECT DISTINCT education_level, class_standing "
                    "FROM hosted_user_preferences"
                )
            ).all() == [("not_specified", "not_specified")]
            # Existing jobs stay unknown: SQL NULL, never JSON 'null'.
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM hosted_jobs "
                    "WHERE student_eligibility IS NOT NULL"
                )
            ) == 0
    finally:
        database.engine.dispose()
        command.upgrade(alembic, "head")


def test_constraints_enforce_allowed_ids_and_not_null(
    database, catalog, clock
) -> None:
    first, _, eligibility = _populate(database, catalog, clock)
    invalid = (
        "UPDATE hosted_user_preferences SET education_level = 'bachelors'",
        "UPDATE hosted_user_preferences SET education_level = 'Undergraduate'",
        "UPDATE hosted_user_preferences SET education_level = ''",
        "UPDATE hosted_user_preferences SET education_level = NULL",
        "UPDATE hosted_user_preferences SET class_standing = 'graduate'",
        "UPDATE hosted_user_preferences SET class_standing = 'first-year'",
        "UPDATE hosted_user_preferences SET class_standing = ''",
        "UPDATE hosted_user_preferences SET class_standing = NULL",
    )
    for statement in invalid:
        with pytest.raises(IntegrityError):
            with database.engine.begin() as connection:
                connection.execute(text(statement))
    with database.engine.connect() as connection:
        assert connection.execute(
            text(
                "SELECT DISTINCT education_level, class_standing "
                "FROM hosted_user_preferences"
            )
        ).all() == [("not_specified", "not_specified")]

    for level in ("not_specified", "undergraduate", "masters", "doctoral"):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE hosted_user_preferences SET education_level = :level "
                    "WHERE user_id = :user"
                ),
                {"level": level, "user": first},
            )
    for standing in ("not_specified", "first_year", "sophomore", "junior", "senior"):
        with database.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE hosted_user_preferences SET class_standing = :standing "
                    "WHERE user_id = :user"
                ),
                {"standing": standing, "user": first},
            )
    with database.session_factory() as db:
        stored = db.get(UserPreference, first)
        assert (stored.education_level, stored.class_standing) == (
            "doctoral",
            "senior",
        )

    # The ORM writes and reads a JSON object, and None is SQL NULL.
    with database.session_factory.begin() as db:
        job = db.scalars(select(HostedJob).order_by(HostedJob.watcher_job_id)).first()
        job.student_eligibility = dict(eligibility)
        job_id = job.id
    with database.session_factory() as db:
        assert db.get(HostedJob, job_id).student_eligibility == eligibility
    with database.session_factory.begin() as db:
        db.get(HostedJob, job_id).student_eligibility = None
    with database.engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM hosted_jobs WHERE student_eligibility IS NULL"
            )
        ) == 2


def test_upgrade_downgrade_upgrade_round_trip_has_no_drift(
    database, catalog, clock, notification_postgres_url
) -> None:
    alembic = _alembic(notification_postgres_url)
    first, _, eligibility = _populate(database, catalog, clock)
    with database.session_factory.begin() as db:
        preference = db.get(UserPreference, first)
        preference.education_level = "undergraduate"
        preference.class_standing = "junior"
        job = db.scalars(select(HostedJob).order_by(HostedJob.watcher_job_id)).first()
        job.student_eligibility = dict(eligibility)
    database.engine.dispose()
    try:
        command.downgrade(alembic, PREVIOUS_REVISION)
        with database.engine.connect() as connection:
            _assert_previous_shape(connection)
            # The downgrade drops only the new columns; no row is lost.
            assert connection.scalar(
                text("SELECT count(*) FROM hosted_user_preferences")
            ) == 2
            assert connection.scalar(text("SELECT count(*) FROM hosted_jobs")) == 2
            assert connection.scalar(
                text("SELECT count(*) FROM hosted_user_job_matches")
            ) == 4
        database.engine.dispose()

        command.upgrade(alembic, "head")
        with database.engine.connect() as connection:
            _assert_head_shape(connection)
            # A downgrade discards the values; the upgrade supplies only defaults.
            assert connection.execute(
                text(
                    "SELECT DISTINCT education_level, class_standing "
                    "FROM hosted_user_preferences"
                )
            ).all() == [("not_specified", "not_specified")]
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM hosted_jobs "
                    "WHERE student_eligibility IS NOT NULL"
                )
            ) == 0
        database.engine.dispose()
        # Models and the migrated schema agree (raises on any drift).
        command.check(alembic)
    finally:
        database.engine.dispose()
        command.upgrade(alembic, "head")
