"""Create persistent per-user job matches and allow nonzero match counters.

Revision ID: 20260803_0003
Revises: 20260802_0002
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803_0003"
down_revision: str | None = "20260802_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def upgrade() -> None:
    op.create_table(
        "hosted_user_job_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column(
            "match_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_matched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("no_longer_matches_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "last_matched_at >= matched_at",
            name="ck_hosted_user_job_matches_match_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["hosted_users.id"],
            ondelete="CASCADE",
            name="fk_hosted_user_job_matches_user_id_hosted_users",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["hosted_jobs.id"],
            ondelete="CASCADE",
            name="fk_hosted_user_job_matches_job_id_hosted_jobs",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_user_job_matches"),
        sa.UniqueConstraint(
            "user_id",
            "job_id",
            name="uq_hosted_user_job_matches_user_job",
        ),
    )
    op.create_index(
        "ix_hosted_user_job_matches_user_active",
        "hosted_user_job_matches",
        ["user_id", "matched_at"],
        postgresql_where=sa.text(
            "no_longer_matches_at IS NULL AND dismissed_at IS NULL"
        ),
    )
    op.create_index(
        "ix_hosted_user_job_matches_user_saved",
        "hosted_user_job_matches",
        ["user_id", "saved_at"],
        postgresql_where=sa.text("saved_at IS NOT NULL"),
    )
    op.create_index(
        "ix_hosted_user_job_matches_user_dismissed",
        "hosted_user_job_matches",
        ["user_id", "dismissed_at"],
        postgresql_where=sa.text("dismissed_at IS NOT NULL"),
    )
    op.create_index(
        "ix_hosted_user_job_matches_user_historical",
        "hosted_user_job_matches",
        ["user_id", "no_longer_matches_at"],
        postgresql_where=sa.text("no_longer_matches_at IS NOT NULL"),
    )
    op.create_index(
        "ix_hosted_user_job_matches_job",
        "hosted_user_job_matches",
        ["job_id"],
    )

    # Phase 2A pinned the counter to zero because no matching layer existed.
    # Phase 2B records real insert counts, so only nonnegativity is required.
    op.drop_constraint(
        "ck_hosted_job_import_runs_matches_created_zero",
        "hosted_job_import_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_hosted_job_import_runs_matches_created_nonnegative",
        "hosted_job_import_runs",
        "matches_created >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_hosted_job_import_runs_matches_created_nonnegative",
        "hosted_job_import_runs",
        type_="check",
    )
    op.execute("UPDATE hosted_job_import_runs SET matches_created = 0")
    op.create_check_constraint(
        "ck_hosted_job_import_runs_matches_created_zero",
        "hosted_job_import_runs",
        "matches_created = 0",
    )
    op.drop_table("hosted_user_job_matches")
