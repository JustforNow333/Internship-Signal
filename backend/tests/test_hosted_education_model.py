"""Database-free checks for the Stage 3 education schema foundation.

Revision ``20261121_0012`` follows ``20261119_0011`` and adds
``hosted_user_preferences.education_level`` and ``class_standing`` (NOT NULL
strings defaulting to ``not_specified`` with allowed-id CHECK constraints) and
the nullable JSON ``hosted_jobs.student_eligibility``. The PostgreSQL
migration checks live in ``test_hosted_education_schema``; these run without a
database so the models and the revision chain are always verified.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint, String
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import AddConstraint

from app.hosted import models
from app.hosted.models import HostedJob, UserPreference

BACKEND_DIR = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "20261119_0011"
EDUCATION_REVISION = "20261121_0012"
EXPECTED_EDUCATION_LEVELS = ("not_specified", "undergraduate", "masters", "doctoral")
EXPECTED_CLASS_STANDINGS = (
    "not_specified",
    "first_year",
    "sophomore",
    "junior",
    "senior",
)


def _scripts() -> ScriptDirectory:
    return ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))


def _check(table, name: str) -> CheckConstraint:
    [constraint] = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == f"ck_{table.name}_{name}"
    ]
    return constraint


def _check_ddl(constraint: CheckConstraint) -> str:
    ddl = str(AddConstraint(constraint).compile(dialect=postgresql.dialect()))
    return " ".join(ddl.split())


def test_education_revision_is_the_single_head_directly_after_0011() -> None:
    scripts = _scripts()
    assert scripts.get_heads() == [EDUCATION_REVISION]
    revision = scripts.get_revision(EDUCATION_REVISION)
    assert revision.down_revision == PREVIOUS_REVISION
    # Exactly one new revision follows 0011, and 0011 still follows 0010.
    assert [
        script.revision
        for script in scripts.walk_revisions()
        if script.down_revision == PREVIOUS_REVISION
    ] == [EDUCATION_REVISION]
    assert scripts.get_revision(PREVIOUS_REVISION).down_revision == "20261025_0010"


def test_allowed_ids_are_exactly_the_owner_specified_values() -> None:
    assert getattr(models, "EDUCATION_LEVELS", None) == EXPECTED_EDUCATION_LEVELS
    assert getattr(models, "CLASS_STANDINGS", None) == EXPECTED_CLASS_STANDINGS


def test_preference_columns_are_not_null_strings_defaulting_to_not_specified() -> None:
    for name in ("education_level", "class_standing"):
        column = UserPreference.__table__.c[name]
        assert column.nullable is False
        assert isinstance(column.type, String)
        assert column.type.length == 32
        assert column.server_default is not None
        assert column.server_default.arg == "not_specified"
        assert column.default is not None
        assert column.default.arg == "not_specified"


def test_preference_columns_carry_allowed_id_check_constraints() -> None:
    table = UserPreference.__table__
    assert _check_ddl(_check(table, "education_level")) == (
        "ALTER TABLE hosted_user_preferences ADD CONSTRAINT "
        "ck_hosted_user_preferences_education_level CHECK (education_level IN "
        "('not_specified', 'undergraduate', 'masters', 'doctoral'))"
    )
    assert _check_ddl(_check(table, "class_standing")) == (
        "ALTER TABLE hosted_user_preferences ADD CONSTRAINT "
        "ck_hosted_user_preferences_class_standing CHECK (class_standing IN "
        "('not_specified', 'first_year', 'sophomore', 'junior', 'senior'))"
    )


def test_job_student_eligibility_is_nullable_json_stored_as_sql_null() -> None:
    column = HostedJob.__table__.c.student_eligibility
    assert column.nullable is True
    assert column.default is None
    assert column.server_default is None
    # PostgreSQL stores it as JSONB like the other hosted JSON columns.
    assert isinstance(
        column.type.dialect_impl(postgresql.dialect()), postgresql.JSONB
    )
    # Python None must persist as SQL NULL (unknown), never as JSON 'null'.
    assert column.type.none_as_null is True
    assert column.type.dialect_impl(postgresql.dialect()).none_as_null is True


def test_student_eligibility_is_loaded_with_the_job_row() -> None:
    # Reconciliation must be able to read the column without an extra query.
    assert HostedJob.__mapper__.attrs["student_eligibility"].deferred is False
