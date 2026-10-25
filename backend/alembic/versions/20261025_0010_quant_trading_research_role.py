"""Allow the quantitative trading and research hosted role.

Revision ID: 20261025_0010
Revises: 20261022_0009

``quantitative_trading_research`` becomes a stored ``hosted_jobs.role_id``
alongside ``quantitative_development``. Only the role check constraint widens:
no row is rewritten, so applying the migration changes no job, match, or
notification. Preference ``role_ids`` are validated by the API and have no
database constraint, so they need no change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261025_0010"
down_revision: str | None = "20261022_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_ROLE = "quantitative_trading_research"
PREVIOUS_ROLES = (
    "'software_engineering', 'machine_learning_ai', 'data_science', "
    "'data_engineering', 'quantitative_development', 'product_management', "
    "'hardware_embedded', 'other_engineering'"
)
ROLES = (
    "'software_engineering', 'machine_learning_ai', 'data_science', "
    "'data_engineering', 'quantitative_development', "
    "'quantitative_trading_research', 'product_management', "
    "'hardware_embedded', 'other_engineering'"
)


def _replace_role_constraint(values: str) -> None:
    op.execute("ALTER TABLE hosted_jobs DROP CONSTRAINT ck_hosted_jobs_role_id")
    op.execute(
        "ALTER TABLE hosted_jobs ADD CONSTRAINT ck_hosted_jobs_role_id "
        f"CHECK (role_id IN ({values}))"
    )


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5min'")
    _replace_role_constraint(ROLES)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    stored = bind.scalar(
        sa.text("SELECT count(*) FROM hosted_jobs WHERE role_id = :role"),
        {"role": NEW_ROLE},
    ) + bind.scalar(
        sa.text(
            "SELECT count(*) FROM hosted_user_preferences "
            "WHERE role_ids @> jsonb_build_array(CAST(:role AS text))"
        ),
        {"role": NEW_ROLE},
    )
    if stored:
        # The previous schema cannot store the role and the previous API
        # rejects it when reading preferences; jobs and preferences are never
        # rewritten or deleted to make a downgrade fit.
        raise RuntimeError(
            "Cannot downgrade below 20261025_0010 while jobs or preferences "
            f"use {NEW_ROLE}."
        )
    _replace_role_constraint(PREVIOUS_ROLES)
