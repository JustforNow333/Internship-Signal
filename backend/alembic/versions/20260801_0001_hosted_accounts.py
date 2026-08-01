"""Create hosted accounts, sessions, preferences, and watchlists.

Revision ID: 20260801_0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260801_0001"
down_revision: str | None = None
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
        "hosted_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("normalized_email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_users"),
        sa.UniqueConstraint(
            "normalized_email", name="uq_hosted_users_normalized_email"
        ),
    )
    op.create_table(
        "hosted_authentication_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["hosted_users.id"],
            ondelete="CASCADE",
            name="fk_hosted_authentication_sessions_user_id_hosted_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_authentication_sessions"),
        sa.UniqueConstraint(
            "token_hash", name="uq_hosted_authentication_sessions_token_hash"
        ),
    )
    op.create_index(
        "ix_hosted_authentication_sessions_user_id",
        "hosted_authentication_sessions",
        ["user_id"],
    )
    for table in (
        "hosted_email_verification_tokens",
        "hosted_password_reset_tokens",
    ):
        op.create_table(
            table,
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["hosted_users.id"],
                ondelete="CASCADE",
                name=f"fk_{table}_user_id_hosted_users",
            ),
            sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
            sa.UniqueConstraint("token_hash", name=f"uq_{table}_token_hash"),
        )
        op.create_index(f"ix_{table}_user_id", table, ["user_id"])
    op.create_table(
        "hosted_user_preferences",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "role_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "preferred_locations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "include_remote", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column(
            "internship_season",
            sa.String(length=80),
            server_default="Any season",
            nullable=False,
        ),
        sa.Column(
            "alert_frequency",
            sa.String(length=32),
            server_default="as_detected",
            nullable=False,
        ),
        sa.Column(
            "globally_paused", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "alert_frequency IN ('as_detected', 'three_hour', 'daily', 'paused')",
            name="ck_hosted_user_preferences_alert_frequency",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["hosted_users.id"],
            ondelete="CASCADE",
            name="fk_hosted_user_preferences_user_id_hosted_users",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_hosted_user_preferences"),
    )
    op.create_table(
        "hosted_user_company_watches",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.String(length=120), nullable=False),
        sa.Column("paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["hosted_users.id"],
            ondelete="CASCADE",
            name="fk_hosted_user_company_watches_user_id_hosted_users",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "company_id", name="pk_hosted_user_company_watches"
        ),
    )
    op.create_table(
        "hosted_unsupported_company_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=False),
        sa.Column("career_url", sa.String(length=2048), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default="received", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('received')",
            name="ck_hosted_unsupported_company_requests_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["hosted_users.id"],
            ondelete="CASCADE",
            name="fk_hosted_unsupported_company_requests_user_id_hosted_users",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_unsupported_company_requests"),
    )
    op.create_index(
        "ix_hosted_unsupported_company_requests_user_id",
        "hosted_unsupported_company_requests",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_table("hosted_unsupported_company_requests")
    op.drop_table("hosted_user_company_watches")
    op.drop_table("hosted_user_preferences")
    op.drop_table("hosted_password_reset_tokens")
    op.drop_table("hosted_email_verification_tokens")
    op.drop_table("hosted_authentication_sessions")
    op.drop_table("hosted_users")
