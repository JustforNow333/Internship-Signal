"""FastAPI dependencies for database sessions and current-user authorization."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from .models import AuthenticationSession, User
from .security import token_hash
from .services import HostedServices


@dataclass(frozen=True)
class CurrentIdentity:
    user: User
    authentication_session: AuthenticationSession


def get_services(request: Request) -> HostedServices:
    return request.app.state.hosted_services


def get_db(
    services: HostedServices = Depends(get_services),
) -> Generator[Session, None, None]:
    if services.database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Hosted account storage is not configured.",
        )
    with services.database.session_factory() as session:
        yield session


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required.",
    )


def get_current_identity(
    request: Request,
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> CurrentIdentity:
    raw_token = request.cookies.get(services.settings.session_cookie_name)
    if not raw_token:
        raise unauthorized()
    authentication_session = db.scalar(
        select(AuthenticationSession)
        .options(joinedload(AuthenticationSession.user))
        .where(AuthenticationSession.token_hash == token_hash(raw_token))
    )
    now = services.clock()
    if (
        authentication_session is None
        or authentication_session.revoked_at is not None
        or _aware(authentication_session.expires_at) <= now
        or not authentication_session.user.is_active
    ):
        raise unauthorized()
    authentication_session.last_used_at = now
    db.commit()
    return CurrentIdentity(
        user=authentication_session.user,
        authentication_session=authentication_session,
    )
