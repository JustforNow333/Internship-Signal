"""Add the include_recent_openings hosted preference.

Revision ID: 20260919_0005
Revises: 20260803_0004
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260919_0005"
down_revision: str | None = "20260803_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL with a true server default backfills every existing preference
    # row, so accounts created before this migration keep the product default.
    op.add_column(
        "hosted_user_preferences",
        sa.Column(
            "include_recent_openings",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("hosted_user_preferences", "include_recent_openings")
