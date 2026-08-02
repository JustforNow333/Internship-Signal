"""Create hosted jobs and idempotent import tracking.

Revision ID: 20260802_0002
Revises: 20260801_0001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260802_0002"
down_revision: str | None = "20260801_0001"
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
        "hosted_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("watcher_job_id", sa.String(length=128), nullable=False),
        sa.Column("company_id", sa.String(length=120), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=False),
        sa.Column("remote_status", sa.String(length=120), nullable=False),
        sa.Column("role_id", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("requirements", sa.Text(), nullable=False),
        sa.Column("application_url", sa.String(length=2048), nullable=True),
        sa.Column("posting_date", sa.Date(), nullable=True),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("is_open", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "role_id IN ('software_engineering', 'machine_learning_ai', "
            "'data_science', 'data_engineering', 'quantitative_development', "
            "'product_management', 'hardware_embedded', 'other_engineering')",
            name="ck_hosted_jobs_role_id",
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at",
            name="ck_hosted_jobs_seen_timestamps",
        ),
        sa.CheckConstraint(
            "closed_at IS NULL OR closed_at >= first_seen_at",
            name="ck_hosted_jobs_closed_timestamp",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_jobs"),
        sa.UniqueConstraint(
            "watcher_job_id",
            name="uq_hosted_jobs_watcher_job_id",
        ),
    )
    op.create_index("ix_hosted_jobs_company_id", "hosted_jobs", ["company_id"])

    op.create_table(
        "hosted_job_import_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_identifier", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("jobs_received", sa.Integer(), nullable=False),
        sa.Column("jobs_inserted", sa.Integer(), nullable=False),
        sa.Column("jobs_updated", sa.Integer(), nullable=False),
        sa.Column("jobs_unchanged", sa.Integer(), nullable=False),
        sa.Column("jobs_skipped", sa.Integer(), nullable=False),
        sa.Column("matches_created", sa.Integer(), nullable=False),
        sa.Column("failure_summary", sa.String(length=400), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_hosted_job_import_runs_status",
        ),
        sa.CheckConstraint(
            "jobs_received >= 0",
            name="ck_hosted_job_import_runs_jobs_received_nonnegative",
        ),
        sa.CheckConstraint(
            "jobs_inserted >= 0",
            name="ck_hosted_job_import_runs_jobs_inserted_nonnegative",
        ),
        sa.CheckConstraint(
            "jobs_updated >= 0",
            name="ck_hosted_job_import_runs_jobs_updated_nonnegative",
        ),
        sa.CheckConstraint(
            "jobs_unchanged >= 0",
            name="ck_hosted_job_import_runs_jobs_unchanged_nonnegative",
        ),
        sa.CheckConstraint(
            "jobs_skipped >= 0",
            name="ck_hosted_job_import_runs_jobs_skipped_nonnegative",
        ),
        sa.CheckConstraint(
            "matches_created = 0",
            name="ck_hosted_job_import_runs_matches_created_zero",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="ck_hosted_job_import_runs_completion_state",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_job_import_runs"),
        sa.UniqueConstraint(
            "source_fingerprint",
            name="uq_hosted_job_import_runs_source_fingerprint",
        ),
    )

    op.create_table(
        "hosted_job_import_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_run_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failure_summary", sa.String(length=400), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_hosted_job_import_attempts_attempt_number_positive",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_hosted_job_import_attempts_status",
        ),
        sa.CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="ck_hosted_job_import_attempts_completion_state",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["hosted_job_import_runs.id"],
            ondelete="CASCADE",
            name="fk_hosted_import_attempt_run",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_job_import_attempts"),
        sa.UniqueConstraint(
            "import_run_id",
            "attempt_number",
            name="uq_hosted_job_import_attempts_run_number",
        ),
    )
    op.create_index(
        "ix_hosted_job_import_attempts_import_run_id",
        "hosted_job_import_attempts",
        ["import_run_id"],
    )


def downgrade() -> None:
    op.drop_table("hosted_job_import_attempts")
    op.drop_table("hosted_job_import_runs")
    op.drop_table("hosted_jobs")
