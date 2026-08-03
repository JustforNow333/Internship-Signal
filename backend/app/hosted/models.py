"""SQLAlchemy models for hosted users, sessions, and preferences."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .database import Base

JsonList = JSON().with_variant(JSONB(), "postgresql")
JsonObject = JSON().with_variant(JSONB(), "postgresql")


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class User(TimestampMixin, Base):
    __tablename__ = "hosted_users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    normalized_email: Mapped[str] = mapped_column(
        String(320), nullable=False, unique=True
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    preferences: Mapped[UserPreference] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )


class AuthenticationSession(Base):
    __tablename__ = "hosted_authentication_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosted_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    user: Mapped[User] = relationship()


class EmailVerificationToken(Base):
    __tablename__ = "hosted_email_verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosted_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


class PasswordResetToken(Base):
    __tablename__ = "hosted_password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosted_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()


class UserPreference(TimestampMixin, Base):
    __tablename__ = "hosted_user_preferences"
    __table_args__ = (
        CheckConstraint(
            "alert_frequency IN ('as_detected', 'three_hour', 'daily', 'paused')",
            name="alert_frequency",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosted_users.id", ondelete="CASCADE"), primary_key=True
    )
    role_ids: Mapped[list[str]] = mapped_column(JsonList, nullable=False, default=list)
    preferred_locations: Mapped[list[str]] = mapped_column(
        JsonList, nullable=False, default=list
    )
    include_remote: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    internship_season: Mapped[str] = mapped_column(
        String(80), nullable=False, default="Any season", server_default="Any season"
    )
    alert_frequency: Mapped[str] = mapped_column(
        String(32), nullable=False, default="as_detected", server_default="as_detected"
    )
    globally_paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    user: Mapped[User] = relationship(back_populates="preferences")


class UserCompanyWatch(TimestampMixin, Base):
    __tablename__ = "hosted_user_company_watches"

    # The composite primary key is the required UNIQUE(user_id, company_id)
    # constraint and its PostgreSQL index also serves user-scoped lookups.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosted_users.id", ondelete="CASCADE"), primary_key=True
    )
    company_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )


class UnsupportedCompanyRequest(Base):
    __tablename__ = "hosted_unsupported_company_requests"
    __table_args__ = (CheckConstraint("status IN ('received')", name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosted_users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    career_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="received", server_default="received"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HostedJob(TimestampMixin, Base):
    __tablename__ = "hosted_jobs"
    __table_args__ = (
        CheckConstraint(
            "role_id IN ('software_engineering', 'machine_learning_ai', "
            "'data_science', 'data_engineering', 'quantitative_development', "
            "'product_management', 'hardware_embedded', 'other_engineering')",
            name="role_id",
        ),
        CheckConstraint(
            "last_seen_at >= first_seen_at",
            name="seen_timestamps",
        ),
        CheckConstraint(
            "closed_at IS NULL OR closed_at >= first_seen_at",
            name="closed_timestamp",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    watcher_job_id: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True
    )
    company_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    location: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    remote_status: Mapped[str] = mapped_column(
        String(120), nullable=False, default=""
    )
    role_id: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requirements: Mapped[str] = mapped_column(Text, nullable=False, default="")
    application_url: Mapped[str | None] = mapped_column(String(2048))
    posting_date: Mapped[date | None] = mapped_column(Date)
    deadline: Mapped[date | None] = mapped_column(Date)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_metadata: Mapped[dict[str, object]] = mapped_column(
        JsonObject, nullable=False, default=dict
    )


class UserJobMatch(TimestampMixin, Base):
    """One durable historical match relationship per (user, job).

    Rows are never deleted when preferences stop matching; reconciliation only
    stamps ``no_longer_matches_at`` so the history stays auditable and a later
    rematch can reactivate the same row.
    """

    __tablename__ = "hosted_user_job_matches"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_hosted_user_job_matches_user_job"),
        CheckConstraint(
            "last_matched_at >= matched_at",
            name="match_timestamps",
        ),
        # Default authenticated listing: active, non-dismissed, newest first.
        Index(
            "ix_hosted_user_job_matches_user_active",
            "user_id",
            "matched_at",
            postgresql_where=text(
                "no_longer_matches_at IS NULL AND dismissed_at IS NULL"
            ),
        ),
        Index(
            "ix_hosted_user_job_matches_user_saved",
            "user_id",
            "saved_at",
            postgresql_where=text("saved_at IS NOT NULL"),
        ),
        Index(
            "ix_hosted_user_job_matches_user_dismissed",
            "user_id",
            "dismissed_at",
            postgresql_where=text("dismissed_at IS NOT NULL"),
        ),
        Index(
            "ix_hosted_user_job_matches_user_historical",
            "user_id",
            "no_longer_matches_at",
            postgresql_where=text("no_longer_matches_at IS NOT NULL"),
        ),
        # Import reconciliation fans out from the changed jobs. Per-user and
        # per-company reconciliation is served by the unique (user_id, job_id)
        # index together with ix_hosted_jobs_company_id on the job side.
        Index("ix_hosted_user_job_matches_job", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosted_users.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("hosted_jobs.id", ondelete="CASCADE"), nullable=False
    )
    match_reasons: Mapped[list[dict[str, str]]] = mapped_column(
        JsonList, nullable=False, default=list
    )
    matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_matched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    no_longer_matches_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    saved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HostedJobImportRun(TimestampMixin, Base):
    __tablename__ = "hosted_job_import_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="status",
        ),
        CheckConstraint("jobs_received >= 0", name="jobs_received_nonnegative"),
        CheckConstraint("jobs_inserted >= 0", name="jobs_inserted_nonnegative"),
        CheckConstraint("jobs_updated >= 0", name="jobs_updated_nonnegative"),
        CheckConstraint("jobs_unchanged >= 0", name="jobs_unchanged_nonnegative"),
        CheckConstraint("jobs_skipped >= 0", name="jobs_skipped_nonnegative"),
        CheckConstraint("matches_created >= 0", name="matches_created_nonnegative"),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="completion_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    source_identifier: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    jobs_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jobs_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jobs_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jobs_unchanged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    jobs_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matches_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_summary: Mapped[str | None] = mapped_column(String(400))

    attempts: Mapped[list[HostedJobImportAttempt]] = relationship(
        back_populates="import_run",
        cascade="all, delete-orphan",
        order_by="HostedJobImportAttempt.attempt_number",
    )


class HostedJobImportAttempt(Base):
    __tablename__ = "hosted_job_import_attempts"
    __table_args__ = (
        UniqueConstraint(
            "import_run_id",
            "attempt_number",
            name="uq_hosted_job_import_attempts_run_number",
        ),
        CheckConstraint("attempt_number > 0", name="attempt_number_positive"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="status",
        ),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="completion_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey(
            "hosted_job_import_runs.id",
            ondelete="CASCADE",
            name="fk_hosted_import_attempt_run",
        ),
        nullable=False,
        index=True,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_summary: Mapped[str | None] = mapped_column(String(400))

    import_run: Mapped[HostedJobImportRun] = relationship(back_populates="attempts")
