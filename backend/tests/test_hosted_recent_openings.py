"""Stage 1 recent-opening admission, watch-start stability, and the
``include_recent_openings`` preference.

These run against in-memory SQLite so the admission rules are covered on every
run; the PostgreSQL-gated suites exercise the same endpoints against the real
schema. The admission gate is pure Python over already-loaded rows, so the
backend makes no difference to the decisions asserted here.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import DateTime, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.hosted.catalog import CompanyCatalog, PublicCompany
from app.hosted.database import Base
from app.hosted.mailer import InMemoryMailer
from app.hosted.match_service import RECENT_OPENING_WINDOW_DAYS
from app.hosted.models import (
    HostedJob,
    HostedNotificationBatch,
    HostedNotificationItem,
    User,
    UserCompanyWatch,
    UserJobMatch,
    UserPreference,
)
from app.hosted.services import HostedServices
from app.hosted.settings import HostedSettings
from app.main import app

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
WINDOW = timedelta(days=RECENT_OPENING_WINDOW_DAYS)


class AwareDateTime(TypeDecorator):
    """Give SQLite the aware timestamps PostgreSQL `timestamptz` returns."""

    impl = DateTime
    cache_ok = True

    def process_result_value(self, value, dialect):
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def _catalog() -> CompanyCatalog:
    return CompanyCatalog(
        tuple(
            PublicCompany(
                id=company_id,
                name=name,
                aliases=(),
                coverage="direct",
                selectable=True,
            )
            for company_id, name in (
                ("google", "Google"),
                ("goldman-sachs", "Goldman Sachs"),
                ("stripe", "Stripe"),
            )
        )
    )


@pytest.fixture
def hosted(monkeypatch):
    for name in ("HOSTED_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine.dialect.colspecs = {**engine.dialect.colspecs, DateTime: AwareDateTime}
    Base.metadata.create_all(engine)
    clock = MutableClock()
    services = HostedServices(
        # These tests move the clock by months; the session must outlive that so
        # an expired cookie never masquerades as a matching result.
        settings=replace(
            HostedSettings.from_env(),
            session_lifetime_seconds=10 * 365 * 24 * 60 * 60,
        ),
        database=SimpleNamespace(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False)
        ),
        mailer=InMemoryMailer(),
        clock=clock,
        catalog=_catalog(),
    )
    previous = app.state.hosted_services
    app.state.hosted_services = services
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(client=client, services=services, clock=clock)
    finally:
        app.state.hosted_services = previous
        Base.metadata.drop_all(engine)
        engine.dispose()


def signup(hosted, email: str = "student@example.com") -> None:
    response = hosted.client.post(
        "/api/auth/signup", json={"email": email, "password": "secure password"}
    )
    assert response.status_code == 201, response.text


def preferences_payload(**overrides) -> dict:
    payload = {
        "role_ids": ["software_engineering", "data_science"],
        "preferred_locations": ["New York, NY"],
        "include_remote": True,
        "internship_season": "Any season",
        "alert_frequency": "as_detected",
        "globally_paused": False,
        "include_recent_openings": True,
    }
    payload.update(overrides)
    return payload


def put_preferences(hosted, **overrides):
    response = hosted.client.put(
        "/api/preferences", json=preferences_payload(**overrides)
    )
    assert response.status_code == 200, response.text
    return response.json()


def put_watchlist(hosted, companies):
    response = hosted.client.put(
        "/api/watchlist",
        json={
            "companies": [
                {"company_id": company_id, "paused": paused}
                for company_id, paused in companies
            ]
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def add_job(
    hosted,
    *,
    company_id: str = "google",
    watcher_job_id: str | None = None,
    role_id: str = "software_engineering",
    title: str = "Software Engineer Intern",
    location: str = "New York, NY",
    posting_date: date | None = None,
    first_seen_at: datetime | None = None,
    is_open: bool = True,
) -> uuid.UUID:
    job_id = uuid.uuid4()
    seen = first_seen_at or hosted.clock()
    with hosted.services.database.session_factory() as db:
        db.add(
            HostedJob(
                id=job_id,
                watcher_job_id=watcher_job_id or f"watcher-{job_id}",
                company_id=company_id,
                company_name=company_id.replace("-", " ").title(),
                title=title,
                location=location,
                remote_status="",
                role_id=role_id,
                description="",
                requirements="",
                application_url="https://example.com/job",
                posting_date=posting_date,
                deadline=None,
                is_open=is_open,
                first_seen_at=seen,
                last_seen_at=seen,
                closed_at=None if is_open else seen,
                source_metadata={},
                created_at=seen,
                updated_at=seen,
            )
        )
        db.commit()
    return job_id


def active_match_job_ids(hosted) -> set[uuid.UUID]:
    with hosted.services.database.session_factory() as db:
        return {
            match.job_id
            for match in db.scalars(
                select(UserJobMatch).where(UserJobMatch.no_longer_matches_at.is_(None))
            )
        }


def match_for(hosted, job_id: uuid.UUID) -> UserJobMatch | None:
    with hosted.services.database.session_factory() as db:
        return db.scalar(select(UserJobMatch).where(UserJobMatch.job_id == job_id))


def notification_counts(hosted) -> tuple[int, int]:
    with hosted.services.database.session_factory() as db:
        batches = len(db.scalars(select(HostedNotificationBatch)).all())
        items = len(db.scalars(select(HostedNotificationItem)).all())
    return batches, items


def watch_rows(hosted) -> dict[str, UserCompanyWatch]:
    with hosted.services.database.session_factory() as db:
        return {
            watch.company_id: watch
            for watch in db.scalars(select(UserCompanyWatch))
        }


# --- preference surface --------------------------------------------------


def test_new_accounts_default_to_including_recent_openings(hosted) -> None:
    signup(hosted)

    response = hosted.client.get("/api/preferences")

    assert response.status_code == 200
    assert response.json()["include_recent_openings"] is True
    with hosted.services.database.session_factory() as db:
        assert db.scalar(select(UserPreference)).include_recent_openings is True


def test_preferences_round_trip_include_recent_openings(hosted) -> None:
    signup(hosted)

    assert put_preferences(hosted, include_recent_openings=False)[
        "include_recent_openings"
    ] is False
    assert (
        hosted.client.get("/api/preferences").json()["include_recent_openings"]
        is False
    )
    assert put_preferences(hosted, include_recent_openings=True)[
        "include_recent_openings"
    ] is True
    assert (
        hosted.client.get("/api/preferences").json()["include_recent_openings"] is True
    )


def test_a_client_that_omits_the_field_keeps_the_product_default(hosted) -> None:
    signup(hosted)
    payload = preferences_payload()
    payload.pop("include_recent_openings")

    response = hosted.client.put("/api/preferences", json=payload)

    assert response.status_code == 200
    assert response.json()["include_recent_openings"] is True


# --- admission: include_recent_openings ON -------------------------------


def test_adding_a_company_backfills_a_recent_open_posting(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    recent = add_job(hosted, posting_date=(NOW - timedelta(days=30)).date())

    put_watchlist(hosted, [("google", False)])

    assert recent in active_match_job_ids(hosted)


def test_the_ninety_day_posting_boundary_is_inclusive(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    boundary = add_job(hosted, posting_date=(NOW - WINDOW).date())
    just_outside = add_job(
        hosted,
        watcher_job_id="watcher-outside",
        posting_date=(NOW - WINDOW - timedelta(days=1)).date(),
    )

    put_watchlist(hosted, [("google", False)])

    admitted = active_match_job_ids(hosted)
    assert boundary in admitted
    assert just_outside not in admitted


def test_a_known_old_posting_date_is_not_rescued_by_a_recent_first_seen_at(
    hosted,
) -> None:
    signup(hosted)
    put_preferences(hosted)
    stale = add_job(
        hosted,
        posting_date=(NOW - timedelta(days=200)).date(),
        first_seen_at=NOW - timedelta(days=1),
    )

    put_watchlist(hosted, [("google", False)])

    assert stale not in active_match_job_ids(hosted)


def test_an_unknown_posting_date_falls_back_to_first_seen_at(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    recent = add_job(
        hosted, posting_date=None, first_seen_at=NOW - timedelta(days=45)
    )

    put_watchlist(hosted, [("google", False)])

    assert recent in active_match_job_ids(hosted)


def test_an_unknown_posting_date_first_seen_long_ago_is_not_backfilled(
    hosted,
) -> None:
    signup(hosted)
    put_preferences(hosted)
    stale = add_job(
        hosted, posting_date=None, first_seen_at=NOW - timedelta(days=120)
    )

    put_watchlist(hosted, [("google", False)])

    assert stale not in active_match_job_ids(hosted)


def test_a_closed_recent_posting_is_never_backfilled(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    closed = add_job(
        hosted,
        posting_date=(NOW - timedelta(days=5)).date(),
        first_seen_at=NOW - timedelta(days=5),
        is_open=False,
    )

    put_watchlist(hosted, [("google", False)])

    assert closed not in active_match_job_ids(hosted)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"role_id": "product_management"}, "role not selected"),
        ({"location": "London, United Kingdom"}, "location not preferred"),
        ({"title": "Winter 2020 Software Engineer Intern"}, "season conflict"),
        ({"company_id": "stripe"}, "company not watched"),
    ],
)
def test_ordinary_matching_filters_still_apply_to_catch_up_candidates(
    hosted, overrides: dict, reason: str
) -> None:
    signup(hosted)
    put_preferences(hosted, internship_season="Summer 2027")
    rejected = add_job(
        hosted, posting_date=(NOW - timedelta(days=10)).date(), **overrides
    )

    put_watchlist(hosted, [("google", False)])

    assert rejected not in active_match_job_ids(hosted), reason


def test_remote_exclusion_still_applies_to_catch_up_candidates(hosted) -> None:
    signup(hosted)
    put_preferences(hosted, include_remote=False)
    remote = add_job(
        hosted,
        posting_date=(NOW - timedelta(days=10)).date(),
        location="Remote - United States",
    )

    put_watchlist(hosted, [("google", False)])

    assert remote not in active_match_job_ids(hosted)


# --- admission: include_recent_openings OFF ------------------------------


def test_disabled_catch_up_blocks_pre_watch_history_on_add(hosted) -> None:
    signup(hosted)
    put_preferences(hosted, include_recent_openings=False)
    historical = add_job(
        hosted,
        posting_date=(NOW - timedelta(days=10)).date(),
        first_seen_at=NOW - timedelta(days=10),
    )

    put_watchlist(hosted, [("google", False)])

    assert historical not in active_match_job_ids(hosted)


def test_disabled_catch_up_still_admits_genuine_post_watch_openings(hosted) -> None:
    signup(hosted)
    put_preferences(hosted, include_recent_openings=False)
    put_watchlist(hosted, [("google", False)])
    hosted.clock.advance(days=3)

    dated = add_job(hosted, posting_date=hosted.clock().date())
    undated = add_job(
        hosted,
        watcher_job_id="watcher-undated",
        posting_date=None,
        first_seen_at=hosted.clock(),
    )
    put_preferences(hosted, include_recent_openings=False, preferred_locations=[])

    admitted = active_match_job_ids(hosted)
    assert dated in admitted
    assert undated in admitted


def test_a_later_import_cannot_smuggle_in_a_pre_watch_job(hosted) -> None:
    """The June posting must not match because a September import touched it."""

    signup(hosted)
    put_preferences(hosted, include_recent_openings=False)
    old_job = add_job(
        hosted,
        posting_date=(NOW - timedelta(days=110)).date(),
        first_seen_at=NOW - timedelta(days=110),
    )
    put_watchlist(hosted, [("google", False)])

    # The employer edits the posting; reconciliation runs over the changed job.
    hosted.clock.advance(days=5)
    from app.hosted.match_service import reconcile_jobs
    from app.hosted.notification_enqueue import enqueue_import_notifications

    with hosted.services.database.session_factory() as db:
        job = db.get(HostedJob, old_job)
        job.description = "Updated description"
        job.last_seen_at = hosted.clock()
        outcome = reconcile_jobs(db, [old_job], now=hosted.clock())
        enqueue_import_notifications(
            db,
            outcome.created_match_ids,
            import_run_id=uuid.uuid4(),
            now=hosted.clock(),
        )
        db.commit()

    assert outcome.created == 0
    assert match_for(hosted, old_job) is None
    assert notification_counts(hosted) == (0, 0)


# --- notifications --------------------------------------------------------


def test_watchlist_backfill_creates_no_notification_work(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    add_job(hosted, posting_date=(NOW - timedelta(days=20)).date())

    put_watchlist(hosted, [("google", False)])

    assert active_match_job_ids(hosted)
    assert notification_counts(hosted) == (0, 0)


def test_enabling_the_setting_creates_no_notification_work(hosted) -> None:
    signup(hosted)
    put_preferences(hosted, include_recent_openings=False)
    historical = add_job(
        hosted,
        posting_date=(NOW - timedelta(days=20)).date(),
        first_seen_at=NOW - timedelta(days=20),
    )
    put_watchlist(hosted, [("google", False)])
    assert historical not in active_match_job_ids(hosted)

    put_preferences(hosted, include_recent_openings=True)

    assert historical in active_match_job_ids(hosted)
    assert notification_counts(hosted) == (0, 0)


# --- preference transitions ----------------------------------------------


def test_disabling_the_setting_keeps_matches_that_were_already_admitted(
    hosted,
) -> None:
    signup(hosted)
    put_preferences(hosted)
    historical = add_job(hosted, posting_date=(NOW - timedelta(days=20)).date())
    put_watchlist(hosted, [("google", False)])
    assert historical in active_match_job_ids(hosted)

    put_preferences(hosted, include_recent_openings=False)

    assert historical in active_match_job_ids(hosted)
    assert match_for(hosted, historical).no_longer_matches_at is None


def test_an_admitted_match_does_not_expire_by_ageing_past_the_window(
    hosted,
) -> None:
    signup(hosted)
    put_preferences(hosted)
    admitted = add_job(hosted, posting_date=(NOW - WINDOW).date())
    put_watchlist(hosted, [("google", False)])
    assert admitted in active_match_job_ids(hosted)

    # Day 90 becomes day 91 and beyond; admission is not an expiry rule.
    hosted.clock.advance(days=40)
    put_preferences(hosted, preferred_locations=[])

    assert admitted in active_match_job_ids(hosted)
    assert match_for(hosted, admitted).no_longer_matches_at is None


def test_adding_a_role_backfills_recent_openings_for_watched_companies(
    hosted,
) -> None:
    signup(hosted)
    put_preferences(hosted, role_ids=["software_engineering"])
    put_watchlist(hosted, [("google", False)])
    other_role = add_job(
        hosted,
        role_id="data_science",
        posting_date=(NOW - timedelta(days=30)).date(),
        first_seen_at=NOW - timedelta(days=30),
    )
    assert other_role not in active_match_job_ids(hosted)

    put_preferences(hosted, role_ids=["software_engineering", "data_science"])

    assert other_role in active_match_job_ids(hosted)


# --- ordinary deactivation still works -----------------------------------


@pytest.mark.parametrize(
    "change", ["removed", "paused", "role", "season", "closed"]
)
def test_existing_matches_still_deactivate_for_ordinary_reasons(
    hosted, change: str
) -> None:
    signup(hosted)
    put_preferences(hosted)
    job_id = add_job(
        hosted,
        title="Summer 2027 Software Engineer Intern",
        posting_date=(NOW - timedelta(days=10)).date(),
    )
    put_watchlist(hosted, [("google", False)])
    assert job_id in active_match_job_ids(hosted)

    if change == "removed":
        put_watchlist(hosted, [])
    elif change == "paused":
        put_watchlist(hosted, [("google", True)])
    elif change == "role":
        put_preferences(hosted, role_ids=["product_management"])
    elif change == "season":
        put_preferences(hosted, internship_season="Winter 2030")
    else:
        from app.hosted.match_service import reconcile_jobs

        with hosted.services.database.session_factory() as db:
            db.get(HostedJob, job_id).is_open = False
            reconcile_jobs(db, [job_id], now=hosted.clock())
            db.commit()

    assert job_id not in active_match_job_ids(hosted)
    assert match_for(hosted, job_id).no_longer_matches_at is not None


# --- watch-start stability -----------------------------------------------


def test_unchanged_watchlist_saves_preserve_created_at(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    put_watchlist(hosted, [("google", False), ("goldman-sachs", False)])
    started = {
        company_id: watch.created_at
        for company_id, watch in watch_rows(hosted).items()
    }

    hosted.clock.advance(days=7)
    result = put_watchlist(hosted, [("google", False), ("goldman-sachs", False)])

    assert {
        company_id: watch.created_at
        for company_id, watch in watch_rows(hosted).items()
    } == started
    assert {entry["company_id"] for entry in result} == set(started)


def test_pause_and_resume_preserve_created_at(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    put_watchlist(hosted, [("google", False)])
    started = watch_rows(hosted)["google"].created_at

    hosted.clock.advance(days=2)
    put_watchlist(hosted, [("google", True)])
    paused = watch_rows(hosted)["google"]
    assert paused.paused is True
    assert paused.created_at == started
    assert paused.updated_at == hosted.clock()

    hosted.clock.advance(days=2)
    put_watchlist(hosted, [("google", False)])
    resumed = watch_rows(hosted)["google"]
    assert resumed.paused is False
    assert resumed.created_at == started


def test_removing_and_re_adding_a_company_starts_a_new_watch(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    put_watchlist(hosted, [("google", False)])
    started = watch_rows(hosted)["google"].created_at

    put_watchlist(hosted, [])
    assert watch_rows(hosted) == {}
    hosted.clock.advance(days=10)
    put_watchlist(hosted, [("google", False)])

    restarted = watch_rows(hosted)["google"].created_at
    assert restarted == hosted.clock()
    assert restarted > started


def test_the_re_added_watch_start_is_the_boundary_the_gate_reads(hosted) -> None:
    signup(hosted)
    put_preferences(hosted, include_recent_openings=False)
    put_watchlist(hosted, [("google", False)])

    hosted.clock.advance(days=30)
    put_watchlist(hosted, [])
    hosted.clock.advance(days=10)
    # Posted after the original watch began but before the company was re-added.
    between = add_job(
        hosted,
        posting_date=(hosted.clock() - timedelta(days=5)).date(),
        first_seen_at=hosted.clock() - timedelta(days=5),
    )
    put_watchlist(hosted, [("google", False)])

    assert between not in active_match_job_ids(hosted)


def test_no_op_watchlist_updates_are_idempotent(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    add_job(hosted, posting_date=(NOW - timedelta(days=10)).date())
    first = put_watchlist(hosted, [("google", False)])
    matched = match_for(hosted, next(iter(active_match_job_ids(hosted))))
    matched_at, last_matched_at = matched.matched_at, matched.last_matched_at

    hosted.clock.advance(days=1)
    second = put_watchlist(hosted, [("google", False)])

    assert first == second
    refreshed = match_for(hosted, matched.job_id)
    assert refreshed.matched_at == matched_at
    assert refreshed.last_matched_at == last_matched_at


def test_backfilled_matches_keep_the_time_they_were_matched(hosted) -> None:
    signup(hosted)
    put_preferences(hosted)
    old = add_job(hosted, posting_date=(NOW - timedelta(days=60)).date())
    hosted.clock.advance(days=1)

    put_watchlist(hosted, [("google", False)])

    assert match_for(hosted, old).matched_at == hosted.clock()
