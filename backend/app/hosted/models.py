"""SQLAlchemy models for hosted users, sessions, and preferences."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from .database import Base

JsonList = JSON().with_variant(JSONB(), "postgresql")


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
