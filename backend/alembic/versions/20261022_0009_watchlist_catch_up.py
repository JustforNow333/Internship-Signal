"""Add durable watchlist catch-up requests and notification provenance.

Revision ID: 20261022_0009
Revises: 20261011_0008

``hosted_catch_up_requests`` records a user's one-off recent-openings
catch-up for a newly watched company until it can be resolved (after email
verification and, if the company has no stored jobs yet, after the first
import that stores some).

Notification batches gain ``kind`` (``import`` or ``catch_up``) and items gain
``source_catch_up_id``, so a catch-up is a user-action notification with its
own provenance rather than a fake import run. Every existing batch becomes an
``import`` batch through the server default and every existing item keeps its
import run, so existing notification history is unchanged. No request rows
are created here: applying the migration never queues an email for anyone.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261022_0009"
down_revision: str | None = "20261011_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OPEN_STATUSES = "'awaiting_verification', 'awaiting_jobs'"
STATUSES = (
    "'awaiting_verification', 'awaiting_jobs', 'enqueued', 'empty', "
    "'suppressed', 'cancelled', 'expired'"
)
IMPORT_SOURCE_FREQUENCY = (
    "(frequency = 'as_detected' AND source_import_run_id IS NOT NULL) OR "
    "(frequency IN ('three_hour', 'daily') AND source_import_run_id IS NULL)"
)
KIND_SOURCE_FREQUENCY = (
    f"(kind = 'import' AND ({IMPORT_SOURCE_FREQUENCY})) OR "
    "(kind = 'catch_up' AND source_import_run_id IS NULL)"
)
ITEM_PROVENANCE = (
    "(source_import_run_id IS NOT NULL AND source_catch_up_id IS NULL) OR "
    "(source_import_run_id IS NULL AND source_catch_up_id IS NOT NULL)"
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5min'")

    op.create_table(
        "hosted_catch_up_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wait_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="status"),
        sa.CheckConstraint(
            f"(status IN ({OPEN_STATUSES}) AND completed_at IS NULL) OR "
            f"(status NOT IN ({OPEN_STATUSES}) AND completed_at IS NOT NULL)",
            name="lifecycle",
        ),
        sa.CheckConstraint(
            "status <> 'awaiting_jobs' OR wait_expires_at IS NOT NULL",
            name="jobs_wait_bounded",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["hosted_users.id"],
            name="fk_hosted_catch_up_requests_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_catch_up_requests"),
    )
    op.create_index(
        "uq_hosted_catch_up_requests_open_company",
        "hosted_catch_up_requests",
        ["user_id", "company_id"],
        unique=True,
        postgresql_where=sa.text(f"status IN ({OPEN_STATUSES})"),
    )
    op.create_index(
        "ix_hosted_catch_up_requests_awaiting_jobs",
        "hosted_catch_up_requests",
        ["company_id"],
        postgresql_where=sa.text("status = 'awaiting_jobs'"),
    )

    # Existing batches all came from imports; the server default records that
    # without rewriting any row's delivery state.
    op.add_column(
        "hosted_notification_batches",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'import'"),
        ),
    )
    # Raw SQL: these names are already final, and the ORM naming convention
    # would otherwise prefix the check-constraint names a second time.
    op.execute(
        "ALTER TABLE hosted_notification_batches ADD CONSTRAINT "
        "ck_hosted_notification_batches_kind CHECK (kind IN ('import', 'catch_up'))"
    )
    op.execute(
        "ALTER TABLE hosted_notification_batches DROP CONSTRAINT "
        "ck_hosted_notification_batches_source_import_frequency"
    )
    op.execute(
        "ALTER TABLE hosted_notification_batches ADD CONSTRAINT "
        "ck_hosted_notification_batches_source_import_frequency "
        f"CHECK ({KIND_SOURCE_FREQUENCY})"
    )

    op.add_column(
        "hosted_notification_items",
        sa.Column("source_catch_up_id", sa.Uuid(), nullable=True),
    )
    op.alter_column(
        "hosted_notification_items", "source_import_run_id", nullable=True
    )
    op.create_foreign_key(
        "fk_hosted_notif_items_catch_up",
        "hosted_notification_items",
        "hosted_catch_up_requests",
        ["source_catch_up_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.execute(
        "ALTER TABLE hosted_notification_items ADD CONSTRAINT "
        f"ck_hosted_notification_items_provenance CHECK ({ITEM_PROVENANCE})"
    )
    op.create_index(
        "ix_hosted_notification_items_catch_up",
        "hosted_notification_items",
        ["source_catch_up_id"],
        postgresql_where=sa.text("source_catch_up_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    bind = op.get_bind()
    catch_up_rows = bind.scalar(
        sa.text(
            "SELECT count(*) FROM hosted_notification_batches "
            "WHERE kind = 'catch_up'"
        )
    ) + bind.scalar(
        sa.text(
            "SELECT count(*) FROM hosted_notification_items "
            "WHERE source_catch_up_id IS NOT NULL"
        )
    )
    if catch_up_rows:
        # The previous schema cannot represent a catch-up notification, and
        # notification history is never deleted to make a downgrade fit.
        raise RuntimeError(
            "Cannot downgrade below 20261022_0009 while catch-up notification "
            "history exists."
        )
    op.drop_index(
        "ix_hosted_notification_items_catch_up",
        table_name="hosted_notification_items",
    )
    op.execute(
        "ALTER TABLE hosted_notification_items DROP CONSTRAINT "
        "ck_hosted_notification_items_provenance"
    )
    op.drop_constraint(
        "fk_hosted_notif_items_catch_up",
        "hosted_notification_items",
        type_="foreignkey",
    )
    op.alter_column(
        "hosted_notification_items", "source_import_run_id", nullable=False
    )
    op.drop_column("hosted_notification_items", "source_catch_up_id")
    op.execute(
        "ALTER TABLE hosted_notification_batches DROP CONSTRAINT "
        "ck_hosted_notification_batches_source_import_frequency"
    )
    op.execute(
        "ALTER TABLE hosted_notification_batches ADD CONSTRAINT "
        "ck_hosted_notification_batches_source_import_frequency "
        f"CHECK ({IMPORT_SOURCE_FREQUENCY})"
    )
    op.execute(
        "ALTER TABLE hosted_notification_batches DROP CONSTRAINT "
        "ck_hosted_notification_batches_kind"
    )
    op.drop_column("hosted_notification_batches", "kind")
    op.drop_index(
        "ix_hosted_catch_up_requests_awaiting_jobs",
        table_name="hosted_catch_up_requests",
    )
    op.drop_index(
        "uq_hosted_catch_up_requests_open_company",
        table_name="hosted_catch_up_requests",
    )
    op.drop_table("hosted_catch_up_requests")
