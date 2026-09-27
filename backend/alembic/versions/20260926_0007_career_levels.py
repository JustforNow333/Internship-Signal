"""Add hosted career levels and the backfill admission marker.

Revision ID: 20260926_0007
Revises: 20260919_0006

Every existing ``hosted_jobs`` row was admitted by the internship-only mapper
gate, and every existing user signed up for the internship-only product, so
both backfill to ``internship``. A read-only production check confirmed that no
hosted job or import run predates that gate.

``hosted_jobs.career_level`` keeps no server default after the backfill: the
importer must classify and set it explicitly on every insert.
``hosted_user_preferences.career_levels`` keeps its default so a preference row
written by any path still means internship-only.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260926_0007"
down_revision: str | None = "20260919_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "hosted_jobs",
        sa.Column(
            "career_level",
            sa.String(length=32),
            nullable=False,
            server_default="internship",
        ),
    )
    op.alter_column("hosted_jobs", "career_level", server_default=None)
    op.create_check_constraint(
        "career_level",
        "hosted_jobs",
        "career_level IN ('internship', 'new_grad_junior', 'mid_level', "
        "'senior_plus', 'unknown')",
    )
    op.add_column(
        "hosted_jobs",
        sa.Column(
            "first_seen_in_backfill",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # The composite index leads with company_id, so it also serves every
    # lookup the single-column index did and replaces it.
    op.create_index(
        "ix_hosted_jobs_company_career_level_role",
        "hosted_jobs",
        ["company_id", "career_level", "role_id"],
    )
    op.drop_index("ix_hosted_jobs_company_id", table_name="hosted_jobs")

    op.add_column(
        "hosted_user_preferences",
        sa.Column(
            "career_levels",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[\"internship\"]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("hosted_user_preferences", "career_levels")
    op.create_index("ix_hosted_jobs_company_id", "hosted_jobs", ["company_id"])
    op.drop_index(
        "ix_hosted_jobs_company_career_level_role", table_name="hosted_jobs"
    )
    op.drop_column("hosted_jobs", "first_seen_in_backfill")
    op.drop_constraint("career_level", "hosted_jobs", type_="check")
    op.drop_column("hosted_jobs", "career_level")
