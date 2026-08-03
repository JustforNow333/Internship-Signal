"""Hosted account, preference, watchlist, and catalog API routes."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Response,
    status,
)
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .dependencies import CurrentIdentity, get_current_identity, get_db, get_services
from .mailer import (
    MailerDeliveryError,
    password_reset_message,
    verification_message,
)
from .match_service import reconcile_user
from .matching import bounded_reasons, is_remote, job_from_model
from .models import (
    AuthenticationSession,
    EmailVerificationToken,
    HostedJob,
    PasswordResetToken,
    UnsupportedCompanyRequest,
    User,
    UserCompanyWatch,
    UserJobMatch,
    UserPreference,
)
from .schemas import (
    MATCH_MAX_OFFSET,
    MATCH_PAGE_LIMIT,
    MATCH_PAGE_MAX_LIMIT,
    MATCH_VIEWS,
    AcceptedResponse,
    AuthCredentials,
    AuthResponse,
    CompanyRequestInput,
    CompanyRequestResponse,
    CompanyResponse,
    CompanyWatchResponse,
    EmailRequest,
    MatchListResponse,
    MatchReason,
    MatchResponse,
    MatchUpdate,
    PreferencesResponse,
    PreferencesUpdate,
    ResetPasswordRequest,
    TokenRequest,
    UserResponse,
    WatchlistUpdate,
)
from .security import (
    hash_password,
    new_token,
    normalized_email,
    token_hash,
    verify_dummy_password,
    verify_password,
)
from .services import HostedServices

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api")


def _user_response(user: User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified_at is not None,
        created_at=user.created_at,
        last_successful_scan_at=None,
    )


def _set_session_cookie(
    response: Response,
    raw_token: str,
    expires_at,
    services: HostedServices,
) -> None:
    response.set_cookie(
        key=services.settings.session_cookie_name,
        value=raw_token,
        max_age=services.settings.session_lifetime_seconds,
        expires=expires_at,
        path="/",
        secure=services.settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def _clear_session_cookie(response: Response, services: HostedServices) -> None:
    response.delete_cookie(
        key=services.settings.session_cookie_name,
        path="/",
        secure=services.settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )


def _create_session(
    db: Session, user: User, services: HostedServices
) -> tuple[str, AuthenticationSession]:
    now = services.clock()
    raw_token = new_token()
    authentication_session = AuthenticationSession(
        user_id=user.id,
        token_hash=token_hash(raw_token),
        created_at=now,
        expires_at=now + timedelta(seconds=services.settings.session_lifetime_seconds),
        last_used_at=now,
    )
    db.add(authentication_session)
    return raw_token, authentication_session


def _create_verification_token(
    db: Session, user: User, services: HostedServices
) -> str:
    now = services.clock()
    raw_token = new_token()
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=token_hash(raw_token),
            created_at=now,
            expires_at=now
            + timedelta(seconds=services.settings.verification_token_lifetime_seconds),
        )
    )
    return raw_token


def _create_reset_token(db: Session, user: User, services: HostedServices) -> str:
    now = services.clock()
    raw_token = new_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash(raw_token),
            created_at=now,
            expires_at=now
            + timedelta(
                seconds=services.settings.password_reset_token_lifetime_seconds
            ),
        )
    )
    return raw_token


def _deliver_verification(user: User, raw_token: str, services: HostedServices) -> bool:
    query = urlencode({"token": raw_token})
    message = verification_message(
        user.email,
        f"{services.settings.public_frontend_url}/verify-email?{query}",
    )
    try:
        return services.mailer.send(message)
    except MailerDeliveryError:
        logger.warning("Hosted verification mail delivery failed")
        return False


def _deliver_reset(user: User, raw_token: str, services: HostedServices) -> bool:
    query = urlencode({"token": raw_token})
    message = password_reset_message(
        user.email,
        f"{services.settings.public_frontend_url}/reset-password?{query}",
    )
    try:
        return services.mailer.send(message)
    except MailerDeliveryError:
        logger.warning("Hosted password-reset mail delivery failed")
        return False


@router.post(
    "/auth/signup",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
)
def signup(
    payload: AuthCredentials,
    response: Response,
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> AuthResponse:
    email, email_key = normalized_email(payload.email)
    if db.scalar(select(User.id).where(User.normalized_email == email_key)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )
    now = services.clock()
    user = User(
        email=email,
        normalized_email=email_key,
        password_hash=hash_password(payload.password),
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    try:
        db.flush()
        db.add(
            UserPreference(
                user_id=user.id,
                role_ids=["software_engineering"],
                preferred_locations=[],
                include_remote=True,
                internship_season="Any season",
                alert_frequency="as_detected",
                globally_paused=False,
                created_at=now,
                updated_at=now,
            )
        )
        verification_token = _create_verification_token(db, user, services)
        session_token, authentication_session = _create_session(db, user, services)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from exc
    delivered = _deliver_verification(user, verification_token, services)
    _set_session_cookie(
        response, session_token, authentication_session.expires_at, services
    )
    return AuthResponse(
        user=_user_response(user),
        verification_email_sent=delivered,
    )


@router.post("/auth/login", response_model=AuthResponse)
def login(
    payload: AuthCredentials,
    response: Response,
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> AuthResponse:
    _, email_key = normalized_email(payload.email)
    user = db.scalar(select(User).where(User.normalized_email == email_key))
    if user is None:
        verify_dummy_password(payload.password)
        password_valid = False
    else:
        password_valid = verify_password(user.password_hash, payload.password)
    if user is None or not user.is_active or not password_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password was not recognized.",
        )
    raw_token, authentication_session = _create_session(db, user, services)
    db.commit()
    _set_session_cookie(
        response, raw_token, authentication_session.expires_at, services
    )
    return AuthResponse(user=_user_response(user))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> None:
    identity.authentication_session.revoked_at = services.clock()
    db.commit()
    _clear_session_cookie(response, services)


@router.post("/auth/forgot-password", response_model=AcceptedResponse)
def forgot_password(
    payload: EmailRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> AcceptedResponse:
    _, email_key = normalized_email(payload.email)
    user = db.scalar(select(User).where(User.normalized_email == email_key))
    raw_token = None
    if user is not None and user.is_active:
        raw_token = _create_reset_token(db, user, services)
        db.commit()
        background_tasks.add_task(_deliver_reset, user, raw_token, services)
    return AcceptedResponse()


@router.post("/auth/reset-password", response_model=AcceptedResponse)
def reset_password(
    payload: ResetPasswordRequest,
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> AcceptedResponse:
    reset_candidate = db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == token_hash(payload.token))
    )
    if reset_candidate is None:
        raise HTTPException(
            status_code=400, detail="This reset link is invalid or expired."
        )
    user = db.scalar(
        select(User)
        .where(User.id == reset_candidate.user_id)
        .with_for_update()
    )
    reset_token = db.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.id == reset_candidate.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = services.clock()
    if (
        reset_token is None
        or reset_token.used_at is not None
        or reset_token.expires_at <= now
        or user is None
        or not user.is_active
    ):
        db.rollback()
        raise HTTPException(
            status_code=400, detail="This reset link is invalid or expired."
        )
    user.password_hash = hash_password(payload.password)
    user.updated_at = now
    reset_token.used_at = now
    db.execute(
        update(PasswordResetToken)
        .where(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.execute(
        update(AuthenticationSession)
        .where(
            AuthenticationSession.user_id == user.id,
            AuthenticationSession.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    db.commit()
    return AcceptedResponse()


@router.post("/auth/resend-verification", response_model=AcceptedResponse)
def resend_verification(
    payload: EmailRequest,
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> AcceptedResponse:
    _, email_key = normalized_email(payload.email)
    user = db.scalar(select(User).where(User.normalized_email == email_key))
    if user is not None and user.is_active and user.email_verified_at is None:
        raw_token = _create_verification_token(db, user, services)
        db.commit()
        _deliver_verification(user, raw_token, services)
    return AcceptedResponse()


@router.post("/auth/verify-email", response_model=AcceptedResponse)
def verify_email(
    payload: TokenRequest,
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> AcceptedResponse:
    now = services.clock()
    verification_token = db.scalar(
        select(EmailVerificationToken)
        .where(EmailVerificationToken.token_hash == token_hash(payload.token))
        .with_for_update()
    )
    if (
        verification_token is None
        or verification_token.used_at is not None
        or verification_token.expires_at <= now
    ):
        raise HTTPException(
            status_code=400, detail="This verification link is invalid or expired."
        )
    user = db.get(User, verification_token.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=400, detail="This verification link is invalid or expired."
        )
    verification_token.used_at = now
    user.email_verified_at = now
    user.updated_at = now
    db.commit()
    return AcceptedResponse()


@router.get("/me", response_model=UserResponse)
def me(identity: CurrentIdentity = Depends(get_current_identity)) -> UserResponse:
    return _user_response(identity.user)


@router.get("/companies", response_model=list[CompanyResponse])
def companies(
    services: HostedServices = Depends(get_services),
) -> list[CompanyResponse]:
    return [
        CompanyResponse(**company.as_dict()) for company in services.catalog.companies
    ]


def _preferences_response(preferences: UserPreference) -> PreferencesResponse:
    return PreferencesResponse(
        role_ids=list(preferences.role_ids),
        preferred_locations=list(preferences.preferred_locations),
        include_remote=preferences.include_remote,
        internship_season=preferences.internship_season,
        alert_frequency=preferences.alert_frequency,
        globally_paused=preferences.globally_paused,
        created_at=preferences.created_at,
        updated_at=preferences.updated_at,
    )


@router.get("/preferences", response_model=PreferencesResponse)
def get_preferences(
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
) -> PreferencesResponse:
    preferences = db.get(UserPreference, identity.user.id)
    if preferences is None:
        raise HTTPException(
            status_code=500, detail="Account preferences are unavailable."
        )
    return _preferences_response(preferences)


@router.put("/preferences", response_model=PreferencesResponse)
def put_preferences(
    payload: PreferencesUpdate,
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> PreferencesResponse:
    preferences = db.get(UserPreference, identity.user.id)
    if preferences is None:
        raise HTTPException(
            status_code=500, detail="Account preferences are unavailable."
        )
    # Alert frequency and the global pause govern Phase 3 delivery only, so
    # they deliberately do not trigger match reconciliation.
    matching_changed = (
        list(preferences.role_ids) != list(payload.role_ids)
        or list(preferences.preferred_locations) != list(payload.preferred_locations)
        or preferences.include_remote != payload.include_remote
        or preferences.internship_season != payload.internship_season
    )
    preferences.role_ids = list(payload.role_ids)
    preferences.preferred_locations = list(payload.preferred_locations)
    preferences.include_remote = payload.include_remote
    preferences.internship_season = payload.internship_season
    preferences.alert_frequency = payload.alert_frequency
    preferences.globally_paused = payload.globally_paused
    now = services.clock()
    preferences.updated_at = now
    if matching_changed:
        reconcile_user(db, identity.user.id, now=now)
    db.commit()
    return _preferences_response(preferences)


def _watch_response(watch: UserCompanyWatch) -> CompanyWatchResponse:
    return CompanyWatchResponse(
        company_id=watch.company_id,
        paused=watch.paused,
        created_at=watch.created_at,
        updated_at=watch.updated_at,
    )


@router.get("/watchlist", response_model=list[CompanyWatchResponse])
def get_watchlist(
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
) -> list[CompanyWatchResponse]:
    watches = db.scalars(
        select(UserCompanyWatch)
        .where(UserCompanyWatch.user_id == identity.user.id)
        .order_by(UserCompanyWatch.company_id)
    ).all()
    return [_watch_response(watch) for watch in watches]


@router.put("/watchlist", response_model=list[CompanyWatchResponse])
def put_watchlist(
    payload: WatchlistUpdate,
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> list[CompanyWatchResponse]:
    invalid = [
        entry.company_id
        for entry in payload.companies
        if entry.company_id not in services.catalog.by_id
        or not services.catalog.by_id[entry.company_id].selectable
    ]
    if invalid:
        raise HTTPException(
            status_code=400, detail="Watchlist contains an unsupported company."
        )
    now = services.clock()
    previous = db.scalars(
        select(UserCompanyWatch).where(UserCompanyWatch.user_id == identity.user.id)
    ).all()
    # Additions, removals, pauses, and resumes all reconcile, but only for the
    # companies whose watch state actually changed.
    previous_state = {watch.company_id: watch.paused for watch in previous}
    next_state = {entry.company_id: entry.paused for entry in payload.companies}
    affected = sorted(
        company_id
        for company_id in set(previous_state) | set(next_state)
        if previous_state.get(company_id) != next_state.get(company_id)
    )
    db.execute(
        delete(UserCompanyWatch).where(UserCompanyWatch.user_id == identity.user.id)
    )
    watches = [
        UserCompanyWatch(
            user_id=identity.user.id,
            company_id=entry.company_id,
            paused=entry.paused,
            created_at=now,
            updated_at=now,
        )
        for entry in payload.companies
    ]
    db.add_all(watches)
    if affected:
        db.flush()
        reconcile_user(db, identity.user.id, now=now, company_ids=affected)
    db.commit()
    return [_watch_response(watch) for watch in watches]


def _match_response(match: UserJobMatch, job: HostedJob) -> MatchResponse:
    return MatchResponse(
        id=match.id,
        job_id=job.id,
        company_id=job.company_id,
        company=job.company_name,
        title=job.title,
        location=job.location,
        remote=is_remote(job_from_model(job)),
        remote_status=job.remote_status,
        role_id=job.role_id,
        application_url=job.application_url,
        posting_date=job.posting_date,
        deadline=job.deadline,
        is_open=job.is_open,
        match_reasons=[
            MatchReason(code=reason["code"], value=reason.get("value"))
            for reason in bounded_reasons(match.match_reasons)
        ],
        matched_at=match.matched_at,
        last_matched_at=match.last_matched_at,
        no_longer_matches_at=match.no_longer_matches_at,
        saved_at=match.saved_at,
        dismissed_at=match.dismissed_at,
    )


def _match_view_filters(view: str):
    if view == "saved":
        return (UserJobMatch.saved_at.is_not(None),)
    if view == "dismissed":
        return (UserJobMatch.dismissed_at.is_not(None),)
    if view == "historical":
        return (UserJobMatch.no_longer_matches_at.is_not(None),)
    if view == "all":
        return ()
    # The default view is the user's current, undismissed matches.
    return (
        UserJobMatch.no_longer_matches_at.is_(None),
        UserJobMatch.dismissed_at.is_(None),
    )


@router.get("/matches", response_model=MatchListResponse)
def list_matches(
    view: str = Query(default="active"),
    limit: int = Query(default=MATCH_PAGE_LIMIT, ge=1, le=MATCH_PAGE_MAX_LIMIT),
    offset: int = Query(default=0, ge=0, le=MATCH_MAX_OFFSET),
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
) -> MatchListResponse:
    if view not in MATCH_VIEWS:
        raise HTTPException(status_code=400, detail="Unsupported match view.")
    filters = _match_view_filters(view)
    total = db.scalar(
        select(func.count())
        .select_from(UserJobMatch)
        .where(UserJobMatch.user_id == identity.user.id, *filters)
    )
    rows = db.execute(
        select(UserJobMatch, HostedJob)
        .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
        .where(UserJobMatch.user_id == identity.user.id, *filters)
        .order_by(UserJobMatch.matched_at.desc(), UserJobMatch.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [_match_response(match, job) for match, job in rows]
    return MatchListResponse(
        items=items,
        limit=limit,
        offset=offset,
        total=int(total or 0),
        has_more=offset + len(items) < int(total or 0),
    )


def _owned_match(db: Session, user_id, match_id):
    """Ownership is part of the lookup, so another user's row is a 404."""

    row = db.execute(
        select(UserJobMatch, HostedJob)
        .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
        .where(UserJobMatch.id == match_id, UserJobMatch.user_id == user_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Match not found.")
    return row


@router.get("/matches/{match_id}", response_model=MatchResponse)
def get_match(
    match_id: uuid.UUID,
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
) -> MatchResponse:
    match, job = _owned_match(db, identity.user.id, match_id)
    return _match_response(match, job)


@router.patch("/matches/{match_id}", response_model=MatchResponse)
def update_match(
    match_id: uuid.UUID,
    payload: MatchUpdate,
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> MatchResponse:
    match, job = _owned_match(db, identity.user.id, match_id)
    now = services.clock()
    # Save and dismiss are independent states: the current frontend exposes a
    # save toggle only, so dismissing never silently clears a save.
    if payload.saved is not None:
        match.saved_at = now if payload.saved else None
    if payload.dismissed is not None:
        match.dismissed_at = now if payload.dismissed else None
    match.updated_at = now
    db.commit()
    return _match_response(match, job)


@router.post(
    "/company-requests",
    response_model=CompanyRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
def request_company(
    payload: CompanyRequestInput,
    identity: CurrentIdentity = Depends(get_current_identity),
    db: Session = Depends(get_db),
    services: HostedServices = Depends(get_services),
) -> CompanyRequestResponse:
    request = UnsupportedCompanyRequest(
        user_id=identity.user.id,
        company_name=payload.company_name,
        career_url=str(payload.career_url) if payload.career_url else None,
        status="received",
        created_at=services.clock(),
    )
    db.add(request)
    db.commit()
    return CompanyRequestResponse(
        id=request.id,
        company_name=request.company_name,
        career_url=request.career_url,
        status="received",
        created_at=request.created_at,
    )
