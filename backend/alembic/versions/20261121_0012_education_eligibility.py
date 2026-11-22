"""Add the optional education profile and stored job student eligibility.

Revision ID: 20261121_0012
Revises: 20261119_0011

``hosted_user_preferences.education_level`` and ``class_standing`` are NOT
NULL strings whose server default ``not_specified`` stays in place, so every
existing preference row reads as not specified (which excludes nothing) and
any insert path that omits them gets the same value. PostgreSQL 11+ stores a
constant default in the catalog, so adding them rewrites no row. The CHECK
constraints allow only the published ids; they validate existing rows, which
all hold the default, without updating any.

``hosted_jobs.student_eligibility`` is nullable JSONB with no default. Existing
jobs stay NULL (unknown, always permissive); there is no backfill, and the next
ordinary import repopulates postings it still sees.

The downgrade drops both constraints and all three columns, discarding any
stored values.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261121_0012"
down_revision: str | None = "20261119_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EDUCATION_LEVELS = ("not_specified", "undergraduate", "masters", "doctoral")
CLASS_STANDINGS = ("not_specified", "first_year", "sophomore", "junior", "senior")


def _in_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5min'")
    for name in ("education_level", "class_standing"):
        op.add_column(
            "hosted_user_preferences",
            sa.Column(
                name,
                sa.String(length=32),
                nullable=False,
                server_default="not_specified",
            ),
        )
    # Short logical names; NAMING_CONVENTION adds the ck_<table>_ prefix once.
    op.create_check_constraint(
        "education_level",
        "hosted_user_preferences",
        f"education_level IN ({_in_list(EDUCATION_LEVELS)})",
    )
    op.create_check_constraint(
        "class_standing",
        "hosted_user_preferences",
        f"class_standing IN ({_in_list(CLASS_STANDINGS)})",
    )
    op.add_column(
        "hosted_jobs",
        sa.Column(
            "student_eligibility",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("hosted_jobs", "student_eligibility")
    op.drop_constraint("class_standing", "hosted_user_preferences", type_="check")
    op.drop_constraint("education_level", "hosted_user_preferences", type_="check")
    op.drop_column("hosted_user_preferences", "class_standing")
    op.drop_column("hosted_user_preferences", "education_level")
