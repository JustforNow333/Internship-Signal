"""Add the user-owned Applied status to hosted matches.

Revision ID: 20261119_0011
Revises: 20261025_0010

``hosted_user_job_matches.applied_at`` is a nullable timestamptz with no
default, so PostgreSQL adds it without rewriting the table and every existing
match reads as not applied. No row is updated. The partial index serves the
per-user Applied listing. The downgrade drops the index and the column.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261119_0011"
down_revision: str | None = "20261025_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5min'")
    op.add_column(
        "hosted_user_job_matches",
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_hosted_user_job_matches_user_applied",
        "hosted_user_job_matches",
        ["user_id", "applied_at"],
        postgresql_where=sa.text("applied_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index(
        "ix_hosted_user_job_matches_user_applied",
        table_name="hosted_user_job_matches",
    )
    op.drop_column("hosted_user_job_matches", "applied_at")
