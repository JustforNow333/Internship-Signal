"""Create durable hosted notification batches, items, and attempts.

Revision ID: 20260803_0004
Revises: 20260803_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0004"
down_revision: str | None = "20260803_0003"
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
        "hosted_notification_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("frequency", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempt_count", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("processing_token", sa.Uuid(), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("send_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("email_message_id", sa.String(length=255), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("source_import_run_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "frequency IN ('as_detected', 'three_hour', 'daily')",
            name="ck_hosted_notification_batches_frequency",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'sent', 'permanent_failed', "
            "'uncertain', 'cancelled')",
            name="ck_hosted_notification_batches_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_hosted_notification_batches_attempt_count_nonnegative",
        ),
        sa.CheckConstraint(
            "(frequency = 'as_detected' AND source_import_run_id IS NOT NULL) OR "
            "(frequency IN ('three_hour', 'daily') AND source_import_run_id IS NULL)",
            name="ck_hosted_notification_batches_source_import_frequency",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND processing_token IS NULL AND "
            "processing_started_at IS NULL AND lease_expires_at IS NULL AND "
            "send_started_at IS NULL AND sent_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'processing' AND processing_token IS NOT NULL AND "
            "processing_started_at IS NOT NULL AND lease_expires_at IS NOT NULL AND "
            "sent_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'sent' AND processing_token IS NULL AND "
            "processing_started_at IS NULL AND lease_expires_at IS NULL AND "
            "send_started_at IS NOT NULL AND sent_at IS NOT NULL AND "
            "cancelled_at IS NULL) OR "
            "(status IN ('permanent_failed', 'uncertain') AND "
            "processing_token IS NULL AND processing_started_at IS NULL AND "
            "lease_expires_at IS NULL AND send_started_at IS NOT NULL AND "
            "sent_at IS NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND processing_token IS NULL AND "
            "processing_started_at IS NULL AND lease_expires_at IS NULL AND "
            "send_started_at IS NULL AND sent_at IS NULL AND cancelled_at IS NOT NULL)",
            name="ck_hosted_notification_batches_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["hosted_users.id"],
            ondelete="CASCADE",
            name="fk_hosted_notification_batches_user_id_hosted_users",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_run_id"],
            ["hosted_job_import_runs.id"],
            ondelete="RESTRICT",
            name="fk_hosted_notif_batches_import_run",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_notification_batches"),
        sa.UniqueConstraint(
            "user_id",
            "source_import_run_id",
            name="uq_hosted_notification_batches_user_import",
        ),
        sa.UniqueConstraint(
            "email_message_id",
            name="uq_hosted_notification_batches_message_id",
        ),
    )
    op.create_index(
        "ix_hosted_notification_batches_due_pending",
        "hosted_notification_batches",
        ["due_at", "next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_hosted_notification_batches_expired_leases",
        "hosted_notification_batches",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'processing'"),
    )
    op.create_index(
        "ix_hosted_notification_batches_user_history",
        "hosted_notification_batches",
        ["user_id", "created_at"],
    )

    op.create_table(
        "hosted_notification_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("user_job_match_id", sa.Uuid(), nullable=False),
        sa.Column("source_import_run_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("cancellation_reason", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'cancelled')",
            name="ck_hosted_notification_items_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND sent_at IS NULL AND cancelled_at IS NULL AND "
            "cancellation_reason IS NULL) OR "
            "(status = 'sent' AND sent_at IS NOT NULL AND cancelled_at IS NULL AND "
            "cancellation_reason IS NULL) OR "
            "(status = 'cancelled' AND sent_at IS NULL AND cancelled_at IS NOT NULL "
            "AND cancellation_reason IS NOT NULL)",
            name="ck_hosted_notification_items_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["hosted_notification_batches.id"],
            ondelete="CASCADE",
            name="fk_hosted_notif_items_batch",
        ),
        sa.ForeignKeyConstraint(
            ["user_job_match_id"],
            ["hosted_user_job_matches.id"],
            ondelete="CASCADE",
            name="fk_hosted_notif_items_match",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_run_id"],
            ["hosted_job_import_runs.id"],
            ondelete="RESTRICT",
            name="fk_hosted_notif_items_import_run",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_notification_items"),
        sa.UniqueConstraint(
            "user_job_match_id",
            name="uq_hosted_notification_items_user_job_match",
        ),
    )
    op.create_index(
        "ix_hosted_notification_items_batch",
        "hosted_notification_items",
        ["batch_id"],
    )

    op.create_table(
        "hosted_notification_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "attempt_number > 0",
            name="ck_hosted_notification_attempts_attempt_number_positive",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('sent', 'retryable_failure', "
            "'permanent_failure', 'uncertain')",
            name="ck_hosted_notification_attempts_outcome",
        ),
        sa.CheckConstraint(
            "(completed_at IS NULL AND outcome IS NULL AND error_code IS NULL) OR "
            "(completed_at IS NOT NULL AND outcome = 'sent' AND error_code IS NULL) OR "
            "(completed_at IS NOT NULL AND outcome IN ('retryable_failure', "
            "'permanent_failure', 'uncertain') AND error_code IS NOT NULL)",
            name="ck_hosted_notification_attempts_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["hosted_notification_batches.id"],
            ondelete="CASCADE",
            name="fk_hosted_notif_attempts_batch",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_notification_attempts"),
        sa.UniqueConstraint(
            "batch_id",
            "attempt_number",
            name="uq_hosted_notification_attempts_batch_number",
        ),
    )
    op.create_index(
        "ix_hosted_notification_attempts_batch",
        "hosted_notification_attempts",
        ["batch_id"],
    )


def downgrade() -> None:
    op.drop_table("hosted_notification_attempts")
    op.drop_table("hosted_notification_items")
    op.drop_table("hosted_notification_batches")
