"""PostgreSQL tests for the recent-openings catch-up digest.

A newly watched company earns one consolidated email of its recent matches,
queued through the ordinary durable notification batches. Every test runs
offline: an isolated, migrated PostgreSQL database, a fake clock, the
in-memory account mailer, and a recording notification transport.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url

from app.hosted.catalog import CompanyCatalog, PublicCompany
from app.hosted.catch_up import JOB_WAIT, resolve_user_catch_ups
from app.hosted.database import (
    HostedDatabase,
    alembic_config_url,
    normalize_database_url,
)
from app.hosted.import_audit import collect_import_audit
from app.hosted.job_import import JobImportService
from app.hosted.mailer import InMemoryMailer
from app.hosted.match_service import reconcile_user
from app.hosted.models import (
    HostedCatchUpRequest,
    HostedJob,
    HostedJobImportRun,
    HostedNotificationBatch,
    HostedNotificationItem,
    HostedSourceObservation,
    User,
    UserCompanyWatch,
    UserJobMatch,
    UserPreference,
)
from app.hosted.notification_mail import DeliveryResult, NotificationEmail
from app.hosted.notification_worker import NotificationDeliveryWorker
from app.hosted.security import hash_password
from app.hosted.services import HostedServices
from app.hosted.settings import HostedSettings
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
FRONTEND_URL = "https://findsooner.example"
COMPANIES = (
    ("google", "Google"),
    ("stripe", "Stripe"),
    ("goldman-sachs", "Goldman Sachs"),
    ("meta", "Meta"),
)


class MutableClock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class RecordingTransport:
    def __init__(self) -> None:
        self.messages: list[NotificationEmail] = []

    def send(self, message: NotificationEmail) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult("sent")


def _psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@pytest.fixture(scope="session")
def catch_up_postgres_url() -> Iterator[str]:
    base_url = os.getenv("HOSTED_TEST_DATABASE_URL", "").strip()
    if not base_url:
        pytest.skip("HOSTED_TEST_DATABASE_URL is required for hosted PostgreSQL tests")
    parsed = make_url(normalize_database_url(base_url))
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("HOSTED_TEST_DATABASE_URL must use PostgreSQL")
    database_name = f"internship_signal_catch_up_test_{uuid.uuid4().hex}"
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False)
    with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    isolated_url = parsed.set(database=database_name).render_as_string(
        hide_password=False
    )
    try:
        alembic = Config(str(BACKEND_DIR / "alembic.ini"))
        alembic.set_main_option("sqlalchemy.url", alembic_config_url(isolated_url))
        logging.getLogger("hosted.catch_up.migration.existing").disabled = False
        command.upgrade(alembic, "head")
        command.check(alembic)
        yield isolated_url
    finally:
        with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as connection:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
            )


@pytest.fixture(autouse=True)
def clean_database(catch_up_postgres_url: str) -> None:
    database = HostedDatabase(catch_up_postgres_url)
    tables = [
        name
        for name in inspect(database.engine).get_table_names()
        if name != "alembic_version"
    ]
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE TABLE "
                + ", ".join(f'"{name}"' for name in tables)
                + " RESTART IDENTITY CASCADE"
            )
        )
    database.dispose()


@pytest.fixture
def hosted(catch_up_postgres_url: str):
    settings = replace(
        HostedSettings.from_env(),
        database_url=catch_up_postgres_url,
        public_frontend_url=FRONTEND_URL,
        session_lifetime_seconds=365 * 24 * 60 * 60,
    )
    clock = MutableClock()
    mailer = InMemoryMailer()
    services = HostedServices.build(
        settings=settings,
        mailer=mailer,
        clock=clock,
        catalog=CompanyCatalog(
            tuple(
                PublicCompany(
                    id=company_id,
                    name=name,
                    aliases=(),
                    coverage="direct",
                    selectable=True,
                )
                for company_id, name in COMPANIES
            )
        ),
    )
    previous = app.state.hosted_services
    app.state.hosted_services = services
    with ExitStack() as clients:
        try:
            yield SimpleNamespace(
                services=services,
                database=services.database,
                clock=clock,
                mailer=mailer,
                transport=RecordingTransport(),
                clients=clients,
            )
        finally:
            app.state.hosted_services = previous
            services.database.dispose()


# --- helpers ---------------------------------------------------------------


def preferences(**overrides) -> dict:
    value = {
        "role_ids": ["software_engineering"],
        "career_levels": ["internship"],
        "preferred_locations": ["New York, NY"],
        "include_remote": True,
        "internship_season": "Any season",
        "alert_frequency": "as_detected",
        "globally_paused": False,
        "include_recent_openings": True,
    }
    value.update(overrides)
    return value


def verification_token(hosted, email: str) -> str:
    message = next(
        message
        for message in reversed(hosted.mailer.messages)
        if message.recipient == email
    )
    url = next(line for line in message.text.splitlines() if line.startswith("http"))
    return parse_qs(urlsplit(url).query)["token"][0]


def account(hosted, email: str = "student@example.com", *, verified: bool = True):
    client = hosted.clients.enter_context(TestClient(app))
    response = client.post(
        "/api/auth/signup", json={"email": email, "password": "secure password"}
    )
    assert response.status_code == 201, response.text
    client.email = email
    if verified:
        verify(hosted, client)
    return client


def verify(hosted, client, token: str | None = None) -> int:
    response = client.post(
        "/api/auth/verify-email",
        json={"token": token or verification_token(hosted, client.email)},
    )
    return response.status_code


def put_preferences(client, **overrides) -> None:
    response = client.put("/api/preferences", json=preferences(**overrides))
    assert response.status_code == 200, response.text


def put_watchlist(client, *companies) -> None:
    entries = [
        {"company_id": entry, "paused": False}
        if isinstance(entry, str)
        else {"company_id": entry[0], "paused": entry[1]}
        for entry in companies
    ]
    response = client.put("/api/watchlist", json={"companies": entries})
    assert response.status_code == 200, response.text


def onboard(client, *companies, **overrides) -> None:
    """The onboarding order: preferences first, then the watchlist."""

    put_preferences(client, **overrides)
    put_watchlist(client, *companies)


def add_job(
    hosted,
    *,
    company_id: str = "google",
    title: str = "Software Engineer Intern",
    location: str = "New York, NY",
    role_id: str = "software_engineering",
    career_level: str = "internship",
    posting_date: date | None = None,
    first_seen_at: datetime | None = None,
    last_seen_at: datetime | None = None,
    source_key: str | None = None,
    is_open: bool = True,
    backfill: bool = False,
    description: str = "",
) -> uuid.UUID:
    job_id = uuid.uuid4()
    # A stored posting is listed until shown otherwise: by default the last
    # collection saw it, however long ago it was first seen.
    listed = last_seen_at or hosted.clock()
    seen = first_seen_at or listed
    name = dict(COMPANIES)[company_id]
    with hosted.database.session_factory.begin() as db:
        db.add(
            HostedJob(
                id=job_id,
                watcher_job_id=f"watcher-{job_id}",
                company_id=company_id,
                company_name=name,
                title=title,
                location=location,
                remote_status="",
                role_id=role_id,
                career_level=career_level,
                description=description,
                requirements="",
                application_url=f"https://careers.example/{job_id.hex[:8]}",
                posting_date=posting_date,
                deadline=None,
                is_open=is_open,
                first_seen_at=seen,
                last_seen_at=listed,
                closed_at=None if is_open else seen,
                source_key=source_key,
                first_seen_in_backfill=backfill,
                source_metadata={"internal": "never-rendered"},
                created_at=seen,
                updated_at=seen,
            )
        )
    return job_id


def days_ago(days: int) -> date:
    return TODAY - timedelta(days=days)


def run_worker(hosted) -> list[NotificationEmail]:
    before = len(hosted.transport.messages)
    NotificationDeliveryWorker(
        hosted.database, hosted.transport, FRONTEND_URL, clock=hosted.clock
    ).run(limit=100)
    return hosted.transport.messages[before:]


def batches(hosted, kind: str | None = None) -> list[HostedNotificationBatch]:
    query = select(HostedNotificationBatch).order_by(
        HostedNotificationBatch.created_at, HostedNotificationBatch.id
    )
    if kind is not None:
        query = query.where(HostedNotificationBatch.kind == kind)
    with hosted.database.session_factory() as db:
        return list(db.scalars(query))


def items(hosted) -> list[HostedNotificationItem]:
    with hosted.database.session_factory() as db:
        return list(db.scalars(select(HostedNotificationItem)))


def requests(hosted) -> dict[str, str]:
    """The latest request status per company."""

    with hosted.database.session_factory() as db:
        return {
            request.company_id: request.status
            for request in db.scalars(
                select(HostedCatchUpRequest).order_by(
                    HostedCatchUpRequest.created_at
                )
            )
        }


def user_id(hosted, email: str) -> uuid.UUID:
    with hosted.database.session_factory() as db:
        return db.scalar(select(User.id).where(User.email == email))


def notified_titles(hosted, email: str) -> list[str]:
    with hosted.database.session_factory() as db:
        return sorted(
            db.scalars(
                select(HostedJob.title)
                .join(UserJobMatch, UserJobMatch.job_id == HostedJob.id)
                .join(
                    HostedNotificationItem,
                    HostedNotificationItem.user_job_match_id == UserJobMatch.id,
                )
                .join(User, User.id == UserJobMatch.user_id)
                .where(User.email == email)
            )
        )


def seed_history(hosted, companies=("google", "stripe", "goldman-sachs"), count=84):
    """``count`` matching postings, 1..count days old, spread over companies."""

    for index in range(count):
        add_job(
            hosted,
            company_id=companies[index % len(companies)],
            title=f"Software Engineer Intern {index:02d}",
            posting_date=days_ago(index + 1),
            first_seen_at=NOW - timedelta(days=index + 1),
            description="PRIVATE POSTING DESCRIPTION",
        )


def import_final_jobs(hosted, jobs: list[dict], marker: str):
    return JobImportService(
        hosted.database, hosted.services.catalog, clock=hosted.clock
    ).import_jobs(
        jobs,
        source_fingerprint=marker * 64,
        source_identifier=f"snapshot-{marker}.json.gz",
        source_type="collection_snapshot",
    )


def final_job(index: int, *, company: str = "Stripe", posted: date) -> dict:
    return {
        "id": f"watcher-catch-up-{index}",
        "company": company,
        "title": f"Backend Software Engineer Intern {index}",
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "description": "Build production APIs.",
        "requirements": "Python and SQL",
        "source_url": f"https://example.com/jobs/catch-up-{index}",
        "date_posted": posted.isoformat(),
        "deadline": "2026-12-01",
        "deadline_days_left": 60,
        "internship_type": "Summer 2027 Internship",
        "role_classification": {"role": "swe", "role_track": "backend"},
        "extra": {"source": "direct", "source_adapter": "workday"},
    }


# --- initial activation ------------------------------------------------------


def test_activation_queues_one_consolidated_digest_across_companies(hosted) -> None:
    seed_history(hosted)
    client = account(hosted)

    onboard(client, "google", "stripe", "goldman-sachs")

    [batch] = batches(hosted)
    assert (batch.kind, batch.frequency, batch.status) == (
        "catch_up",
        "as_detected",
        "pending",
    )
    assert batch.source_import_run_id is None
    assert batch.due_at == NOW
    queued = items(hosted)
    assert len(queued) == 84
    assert all(item.source_import_run_id is None for item in queued)
    assert all(item.source_catch_up_id is not None for item in queued)
    assert requests(hosted) == dict.fromkeys(
        ("google", "stripe", "goldman-sachs"), "enqueued"
    )

    [message] = run_worker(hosted)

    assert message.recipient == "student@example.com"
    assert message.subject == "Your recent job matches (84)"
    assert "84 open roles from the last 90 days match" in message.text
    assert message.text.count("Apply: ") == 25
    assert message.html.count("<section>") == 25
    assert "59 additional matches" in message.text
    assert f"View all matches: {FRONTEND_URL}/app/matches" in message.text
    # Most recent postings first, across every company.
    shown = [
        line for line in message.text.splitlines() if " — Software Engineer" in line
    ]
    assert shown[:3] == [
        "Google — Software Engineer Intern 00",
        "Stripe — Software Engineer Intern 01",
        "Goldman Sachs — Software Engineer Intern 02",
    ]
    assert f"Posted: {days_ago(1).isoformat()}" in message.text
    assert "Career stage: Internship" in message.text
    assert "Why it matched: Company is on your watchlist" in message.text
    assert "Location: New York, NY" in message.text
    # No posting text, source metadata, or internal identifier is rendered.
    assert "PRIVATE POSTING DESCRIPTION" not in message.text + message.html
    assert "never-rendered" not in message.text + message.html
    for item in queued[:5]:
        assert str(item.user_job_match_id) not in message.text + message.html
    assert {batch.status for batch in batches(hosted)} == {"sent"}
    assert {item.status for item in items(hosted)} == {"sent"}

    # Normal future-opening alerts keep their own path and subject.
    clock = hosted.clock
    clock.advance(hours=1)
    import_final_jobs(
        hosted, [final_job(1, company="Google", posted=clock().date())], "a"
    )
    [alert] = run_worker(hosted)
    assert alert.subject == "New job matches (1)"
    assert "from the last 90 days" not in alert.text


def test_an_activation_with_no_eligible_postings_sends_nothing(hosted) -> None:
    add_job(hosted, company_id="google", posting_date=days_ago(120))
    client = account(hosted)

    onboard(client, "google")

    assert batches(hosted) == []
    assert requests(hosted) == {"google": "empty"}
    assert run_worker(hosted) == []


def test_disabled_recent_openings_records_no_catch_up(hosted) -> None:
    seed_history(hosted, count=6)
    client = account(hosted)

    onboard(client, "google", "stripe", include_recent_openings=False)

    assert requests(hosted) == {}
    assert batches(hosted) == []
    assert run_worker(hosted) == []


def test_ineligible_postings_are_excluded(hosted) -> None:
    add_job(hosted, title="Eligible Intern", posting_date=days_ago(10))
    add_job(hosted, title="Boundary Intern", posting_date=days_ago(90))
    add_job(hosted, title="Too Old Intern", posting_date=days_ago(91))
    add_job(hosted, title="Closed Intern", posting_date=days_ago(5), is_open=False)
    add_job(
        hosted,
        title="Data Science Intern",
        role_id="data_science",
        posting_date=days_ago(5),
    )
    add_job(
        hosted,
        title="Senior Software Engineer",
        career_level="senior_plus",
        posting_date=days_ago(5),
    )
    add_job(
        hosted,
        title="Faraway Intern",
        location="San Francisco, CA",
        posting_date=days_ago(5),
    )
    add_job(
        hosted, title="Software Engineer Intern Fall 2026", posting_date=days_ago(5)
    )
    client = account(hosted)

    onboard(client, "google", internship_season="Summer 2027")

    assert notified_titles(hosted, "student@example.com") == [
        "Boundary Intern",
        "Eligible Intern",
    ]
    [message] = run_worker(hosted)
    assert message.subject == "Your recent job matches (2)"


def test_missing_posting_dates_follow_the_first_seen_rules(hosted) -> None:
    add_job(
        hosted, title="Undated Recent", first_seen_at=NOW - timedelta(days=10)
    )
    add_job(
        hosted, title="Undated Stale", first_seen_at=NOW - timedelta(days=100)
    )
    add_job(
        hosted,
        title="Dated Old Seen Recently",
        posting_date=days_ago(100),
        first_seen_at=NOW - timedelta(days=1),
    )
    add_job(
        hosted,
        title="Undated Backfill Recent",
        first_seen_at=NOW - timedelta(days=10),
        backfill=True,
    )
    client = account(hosted)

    onboard(client, "google")

    assert notified_titles(hosted, "student@example.com") == [
        "Undated Backfill Recent",
        "Undated Recent",
    ]
    [message] = run_worker(hosted)
    # Undated postings are listed without inventing a date.
    assert "Posted:" not in message.text


# --- email verification -----------------------------------------------------


def test_an_unverified_activation_is_held_until_verification(hosted) -> None:
    seed_history(hosted, count=30)
    client = account(hosted, verified=False)
    first_token = verification_token(hosted, client.email)
    assert (
        client.post(
            "/api/auth/resend-verification", json={"email": client.email}
        ).status_code
        == 200
    )
    second_token = verification_token(hosted, client.email)
    assert first_token != second_token

    onboard(client, "google", "stripe", "goldman-sachs")

    assert batches(hosted) == []
    assert set(requests(hosted).values()) == {"awaiting_verification"}
    hosted.clock.advance(hours=6)
    assert run_worker(hosted) == []
    # Saving the same watchlist again while unverified still records nothing.
    put_watchlist(client, "google", "stripe", "goldman-sachs")
    assert len(requests(hosted)) == 3

    assert verify(hosted, client, first_token) == 200

    [batch] = batches(hosted)
    assert (batch.kind, batch.due_at) == ("catch_up", hosted.clock())
    assert len(items(hosted)) == 30
    # The second outstanding link, a replayed link, and resaves add nothing.
    assert verify(hosted, client, second_token) == 200
    assert verify(hosted, client, first_token) == 400
    put_watchlist(client, "google", "stripe", "goldman-sachs")
    assert len(batches(hosted)) == 1
    assert len(items(hosted)) == 30

    [message] = run_worker(hosted)
    assert message.subject == "Your recent job matches (30)"
    assert run_worker(hosted) == []


def test_an_unverified_user_is_never_emailed(hosted) -> None:
    seed_history(hosted, count=3)
    client = account(hosted, verified=False)
    onboard(client, "google", "stripe", "goldman-sachs")
    hosted.clock.advance(days=30)
    import_final_jobs(hosted, [final_job(1, posted=hosted.clock().date())], "a")

    assert run_worker(hosted) == []
    assert batches(hosted) == []
    assert set(requests(hosted).values()) == {"awaiting_verification"}


def test_verification_while_paused_suppresses_the_catch_up(hosted) -> None:
    seed_history(hosted, count=3)
    client = account(hosted, verified=False)
    onboard(client, "google", alert_frequency="paused")

    assert verify(hosted, client) == 200

    assert batches(hosted) == []
    assert requests(hosted) == {"google": "suppressed"}
    # Resuming alerts later does not resurrect the historical digest.
    put_preferences(client, alert_frequency="as_detected")
    assert batches(hosted) == []
    assert run_worker(hosted) == []


# --- idempotency and later watchlist changes ---------------------------------


def test_repeated_saves_pauses_resumes_and_removals_never_add_a_digest(
    hosted,
) -> None:
    seed_history(hosted, companies=("google", "stripe"), count=10)
    client = account(hosted)
    onboard(client, "google", "stripe")
    onboard(client, "google", "stripe")
    [message] = run_worker(hosted)
    assert message.subject == "Your recent job matches (10)"

    hosted.clock.advance(hours=1)
    put_watchlist(client, "google", "stripe")
    put_watchlist(client, ("google", True), "stripe")
    put_watchlist(client, "google", "stripe")
    put_watchlist(client, "google")
    put_preferences(client, include_remote=False)
    put_preferences(client, alert_frequency="daily")
    put_preferences(client, alert_frequency="as_detected")
    put_preferences(client, include_recent_openings=False)
    put_preferences(client, include_recent_openings=True)

    assert len(batches(hosted)) == 1
    assert len(items(hosted)) == 10
    assert run_worker(hosted) == []


def test_adding_companies_later_sends_one_digest_for_only_those_companies(
    hosted,
) -> None:
    seed_history(hosted, companies=("google", "stripe", "meta"), count=30)
    client = account(hosted)
    onboard(client, "google")
    [first] = run_worker(hosted)
    assert first.subject == "Your recent job matches (10)"

    hosted.clock.advance(days=2)
    put_watchlist(client, "google", "stripe", "meta")

    catch_ups = [batch for batch in batches(hosted) if batch.status == "pending"]
    assert len(catch_ups) == 1
    [second] = run_worker(hosted)
    assert second.subject == "Your recent job matches (20)"
    assert "Google —" not in second.text
    assert second.text.count("Stripe —") == 10
    assert second.text.count("Meta —") == 10
    assert len(items(hosted)) == 30
    assert len(set(notified_titles(hosted, client.email))) == 30


def test_two_additions_before_delivery_share_one_digest(hosted) -> None:
    seed_history(hosted, companies=("google", "stripe"), count=4)
    client = account(hosted)
    onboard(client, "google", alert_frequency="daily")
    hosted.clock.advance(hours=2)
    put_watchlist(client, "google", "stripe")

    [batch] = batches(hosted)
    assert batch.due_at == NOW + timedelta(hours=24)
    hosted.clock.advance(hours=22)
    [message] = run_worker(hosted)
    assert message.subject == "Your recent job matches (4)"


def test_re_adding_a_company_never_resends_or_resurfaces_matches(hosted) -> None:
    seed_history(hosted, companies=("google",), count=3)
    client = account(hosted)
    onboard(client, "google")
    run_worker(hosted)

    hosted.clock.advance(days=1)
    put_watchlist(client)
    put_watchlist(client, "google")

    assert len(batches(hosted)) == 1
    assert requests(hosted) == {"google": "empty"}
    assert run_worker(hosted) == []


def test_removing_a_company_withdraws_its_unresolved_request(hosted) -> None:
    client = account(hosted, verified=False)
    onboard(client, "google", "stripe")
    put_watchlist(client, "google")
    seed_history(hosted, companies=("google", "stripe"), count=4)
    # Jobs stored after activation reach the user through the import path;
    # these direct rows only need matches to exist.
    with hosted.database.session_factory.begin() as db:
        reconcile_user(db, user_id(hosted, client.email), now=hosted.clock())

    assert verify(hosted, client) == 200

    with hosted.database.session_factory() as db:
        statuses = {
            (request.company_id, request.status)
            for request in db.scalars(select(HostedCatchUpRequest))
        }
    assert ("stripe", "cancelled") in statuses
    assert all(item.source_catch_up_id is not None for item in items(hosted))
    assert all("Stripe" not in title for title in notified_titles(hosted, client.email))


# --- delivery timing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("frequency", "delay"),
    [("three_hour", timedelta(hours=3)), ("daily", timedelta(hours=24))],
)
def test_rolling_frequencies_follow_their_delivery_windows(
    hosted, frequency, delay
) -> None:
    seed_history(hosted, count=5)
    client = account(hosted)
    onboard(client, "google", "stripe", "goldman-sachs", alert_frequency=frequency)

    [batch] = batches(hosted)
    assert (batch.kind, batch.frequency, batch.due_at) == (
        "catch_up",
        frequency,
        NOW + delay,
    )
    hosted.clock.advance(seconds=int(delay.total_seconds()) - 60)
    assert run_worker(hosted) == []
    hosted.clock.advance(minutes=1)
    [message] = run_worker(hosted)
    assert message.subject == "Your recent job matches (5)"


@pytest.mark.parametrize(
    "overrides",
    [{"alert_frequency": "paused"}, {"globally_paused": True}],
)
def test_paused_accounts_never_receive_the_catch_up(hosted, overrides) -> None:
    seed_history(hosted, count=5)
    client = account(hosted)
    onboard(client, "google", **overrides)

    assert batches(hosted) == []
    assert requests(hosted) == {"google": "suppressed"}
    hosted.clock.advance(days=2)
    assert run_worker(hosted) == []


def test_a_pending_catch_up_follows_a_frequency_change(hosted) -> None:
    seed_history(hosted, count=5)
    client = account(hosted)
    onboard(client, "google", "stripe", "goldman-sachs", alert_frequency="daily")
    hosted.clock.advance(hours=1)

    put_preferences(client, alert_frequency="as_detected")

    pending = [batch for batch in batches(hosted) if batch.status == "pending"]
    assert [(b.kind, b.frequency, b.due_at) for b in pending] == [
        ("catch_up", "as_detected", hosted.clock())
    ]
    [message] = run_worker(hosted)
    assert message.subject == "Your recent job matches (5)"
    hosted.clock.advance(days=2)
    assert run_worker(hosted) == []


def test_pausing_before_delivery_cancels_the_catch_up(hosted) -> None:
    seed_history(hosted, count=5)
    client = account(hosted)
    onboard(client, "google", alert_frequency="three_hour")
    put_preferences(client, alert_frequency="paused")
    hosted.clock.advance(hours=4)

    assert run_worker(hosted) == []
    [batch] = batches(hosted)
    assert (batch.status, batch.last_error_code) == ("cancelled", "frequency_paused")


def test_disabling_recent_openings_before_delivery_cancels_the_catch_up(
    hosted,
) -> None:
    seed_history(hosted, count=5)
    client = account(hosted)
    onboard(client, "google", alert_frequency="daily")
    put_preferences(client, alert_frequency="daily", include_recent_openings=False)
    hosted.clock.advance(days=1)

    assert run_worker(hosted) == []
    [batch] = batches(hosted)
    assert (batch.status, batch.last_error_code) == (
        "cancelled",
        "recent_openings_disabled",
    )


# --- empty catalog and imports -----------------------------------------------


def test_an_empty_catalog_completes_the_catch_up_on_the_first_import(
    hosted,
) -> None:
    client = account(hosted)
    onboard(client, "stripe")
    assert batches(hosted) == []
    assert requests(hosted) == {"stripe": "awaiting_jobs"}

    hosted.clock.advance(hours=2)
    imported = import_final_jobs(
        hosted,
        [
            final_job(1, posted=hosted.clock().date()),
            final_job(2, posted=days_ago(10)),
            final_job(3, posted=days_ago(20)),
        ],
        "a",
    )
    assert imported.outcome == "imported"
    assert requests(hosted) == {"stripe": "enqueued"}

    kinds = sorted(batch.kind for batch in batches(hosted))
    assert kinds == ["catch_up", "import"]
    messages = sorted(run_worker(hosted), key=lambda message: message.subject)
    assert [message.subject for message in messages] == [
        "New job matches (1)",
        "Your recent job matches (2)",
    ]
    # The genuinely new opening keeps its import alert and is not repeated.
    assert "Backend Software Engineer Intern 1\n" in messages[0].text
    assert "Backend Software Engineer Intern 1\n" not in messages[1].text

    # Later imports are ordinary again: old postings stay silent.
    hosted.clock.advance(hours=2)
    import_final_jobs(hosted, [final_job(4, posted=days_ago(5))], "b")
    assert run_worker(hosted) == []


def test_the_import_audit_accepts_catch_up_digest_items(hosted) -> None:
    client = account(hosted)
    onboard(client, "stripe")
    hosted.clock.advance(hours=1)
    imported = import_final_jobs(
        hosted,
        [final_job(1, posted=hosted.clock().date()), final_job(2, posted=days_ago(9))],
        "a",
    )

    audit = collect_import_audit(hosted.database, imported)

    assert (audit.new_opening_matches, audit.silent_catch_up_matches) == (1, 1)
    assert audit.notification_items_present == 2
    assert audit.silent_match_with_notification == 0
    assert not audit.needs_attention


def test_a_catch_up_waiting_for_jobs_is_bounded(hosted) -> None:
    client = account(hosted)
    onboard(client, "stripe")
    hosted.clock.advance(seconds=int(JOB_WAIT.total_seconds()) + 1)

    import_final_jobs(hosted, [final_job(1, posted=days_ago(10))], "a")

    assert requests(hosted) == {"stripe": "expired"}
    assert batches(hosted) == []
    assert run_worker(hosted) == []


def test_a_backfill_import_never_completes_a_catch_up(hosted) -> None:
    client = account(hosted)
    onboard(client, "stripe")
    JobImportService(
        hosted.database, hosted.services.catalog, clock=hosted.clock
    ).import_jobs(
        [final_job(1, posted=days_ago(10))],
        source_fingerprint="c" * 64,
        source_identifier="snapshot-c.json.gz",
        source_type="collection_snapshot",
        backfill=True,
    )

    assert requests(hosted) == {"stripe": "awaiting_jobs"}
    assert batches(hosted) == []


def test_already_notified_jobs_are_never_sent_again_by_catch_up(hosted) -> None:
    existing = account(hosted, "existing@example.com")
    onboard(existing, "stripe", include_recent_openings=False)
    hosted.clock.advance(hours=1)
    import_final_jobs(hosted, [final_job(1, posted=hosted.clock().date())], "a")
    [alert] = run_worker(hosted)
    assert alert.subject == "New job matches (1)"
    add_job(hosted, company_id="stripe", title="Recent Intern", posting_date=days_ago(3))

    # An open request whose window covers the already-alerted match.
    owner = user_id(hosted, existing.email)
    with hosted.database.session_factory.begin() as db:
        db.get(UserPreference, owner).include_recent_openings = True
        reconcile_user(db, owner, now=hosted.clock())
        db.add(
            HostedCatchUpRequest(
                user_id=owner,
                company_id="stripe",
                status="awaiting_verification",
                requested_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    with hosted.database.session_factory.begin() as db:
        assert resolve_user_catch_ups(db, owner, now=hosted.clock()) == 1

    [digest] = run_worker(hosted)
    assert digest.subject == "Your recent job matches (1)"
    assert "Recent Intern" in digest.text
    assert "Backend Software Engineer Intern 1" not in digest.text
    assert notified_titles(hosted, existing.email) == [
        "Backend Software Engineer Intern 1",
        "Recent Intern",
    ]
    hosted.clock.advance(days=1)
    put_watchlist(existing)
    put_watchlist(existing, "stripe")
    assert run_worker(hosted) == []


# --- concurrency, isolation, and existing accounts ---------------------------


def test_concurrent_resolution_queues_each_match_once(hosted) -> None:
    seed_history(hosted, count=12)
    client = account(hosted, verified=False)
    onboard(client, "google", "stripe", "goldman-sachs")
    owner = user_id(hosted, client.email)
    with hosted.database.session_factory.begin() as db:
        db.get(User, owner).email_verified_at = hosted.clock()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def resolve() -> None:
        try:
            with hosted.database.session_factory.begin() as db:
                barrier.wait(timeout=10)
                resolve_user_catch_ups(db, owner, now=hosted.clock())
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=resolve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(batches(hosted)) == 1
    assert len(items(hosted)) == 12
    [message] = run_worker(hosted)
    assert message.subject == "Your recent job matches (12)"


def test_overlapping_watchlists_have_independent_histories(hosted) -> None:
    seed_history(hosted, companies=("google",), count=4)
    first = account(hosted, "first@example.com")
    onboard(first, "google")
    [first_mail] = run_worker(hosted)

    hosted.clock.advance(days=1)
    second = account(hosted, "second@example.com")
    onboard(second, "google", "stripe")
    [second_mail] = run_worker(hosted)

    assert first_mail.recipient == "first@example.com"
    assert second_mail.recipient == "second@example.com"
    assert first_mail.subject == second_mail.subject == "Your recent job matches (4)"
    assert len(items(hosted)) == 8
    first_matches = first.get("/api/matches").json()["items"]
    second_matches = second.get("/api/matches").json()["items"]
    assert {m["id"] for m in first_matches}.isdisjoint(m["id"] for m in second_matches)
    assert second.get(f"/api/matches/{first_matches[0]['id']}").status_code == 404
    assert (
        second.patch(
            f"/api/matches/{first_matches[0]['id']}", json={"dismissed": True}
        ).status_code
        == 404
    )


def test_existing_accounts_get_no_historical_email_without_a_new_watch(
    hosted,
) -> None:
    seed_history(hosted, companies=("google",), count=5)
    with hosted.database.session_factory.begin() as db:
        user = User(
            email="legacy@example.com",
            normalized_email="legacy@example.com",
            password_hash=hash_password("secure password"),
            email_verified_at=NOW - timedelta(days=30),
            created_at=NOW - timedelta(days=30),
            updated_at=NOW - timedelta(days=30),
        )
        db.add(user)
        db.flush()
        db.add_all(
            [
                UserPreference(
                    user_id=user.id,
                    **{
                        key: value
                        for key, value in preferences().items()
                        if key != "career_levels"
                    },
                ),
                UserCompanyWatch(
                    user_id=user.id,
                    company_id="google",
                    paused=False,
                    created_at=NOW - timedelta(days=30),
                    updated_at=NOW - timedelta(days=30),
                ),
            ]
        )
    client = hosted.clients.enter_context(TestClient(app))
    assert (
        client.post(
            "/api/auth/login",
            json={"email": "legacy@example.com", "password": "secure password"},
        ).status_code
        == 200
    )

    put_watchlist(client, "google")
    put_preferences(client, include_remote=False)
    put_preferences(client, alert_frequency="daily")
    hosted.clock.advance(days=2)

    assert requests(hosted) == {}
    assert batches(hosted) == []
    assert run_worker(hosted) == []


# --- migration -----------------------------------------------------------------


def _row_snapshot(database: HostedDatabase) -> dict[str, list[str]]:
    removed = "ARRAY['kind', 'source_catch_up_id']"
    with database.engine.connect() as connection:
        return {
            table: list(
                connection.scalars(
                    text(
                        f"SELECT (to_jsonb(t) - {removed})::text AS row "
                        f"FROM {table} t ORDER BY row"
                    )
                )
            )
            for table in (
                "hosted_users",
                "hosted_user_company_watches",
                "hosted_user_job_matches",
                "hosted_notification_batches",
                "hosted_notification_items",
                "hosted_notification_attempts",
            )
        }


def test_migration_keeps_existing_notifications_and_queues_nothing(
    hosted, catch_up_postgres_url
) -> None:
    alembic = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic.set_main_option("sqlalchemy.url", alembic_config_url(catch_up_postgres_url))
    database = hosted.database
    try:
        # Existing accounts with delivered and still-pending import alerts.
        sent = account(hosted, "sent@example.com")
        onboard(sent, "stripe", include_recent_openings=False)
        daily = account(hosted, "daily@example.com")
        onboard(daily, "stripe", alert_frequency="daily", include_recent_openings=False)
        hosted.clock.advance(hours=1)
        import_final_jobs(hosted, [final_job(1, posted=hosted.clock().date())], "a")
        assert len(run_worker(hosted)) == 1
        populated = _row_snapshot(database)
        database.engine.dispose()

        command.downgrade(alembic, "20261011_0008")
        with database.engine.connect() as connection:
            assert "hosted_catch_up_requests" not in inspect(connection).get_table_names()
        assert _row_snapshot(database) == populated
        database.engine.dispose()

        command.upgrade(alembic, "head")
        command.check(alembic)
        assert _row_snapshot(database) == populated
        with database.engine.connect() as connection:
            assert connection.execute(
                text("SELECT DISTINCT kind FROM hosted_notification_batches")
            ).scalars().all() == ["import"]
            assert connection.scalar(
                text(
                    "SELECT count(*) FROM hosted_notification_items "
                    "WHERE source_import_run_id IS NULL "
                    "OR source_catch_up_id IS NOT NULL"
                )
            ) == 0
            assert connection.scalar(
                text("SELECT count(*) FROM hosted_catch_up_requests")
            ) == 0

        # Only the pending daily import alert is delivered; nothing historical.
        hosted.clock.advance(days=1)
        [message] = run_worker(hosted)
        assert (message.recipient, message.subject) == (
            "daily@example.com",
            "Your FindSooner digest (1)",
        )

        # A schema holding catch-up history refuses to downgrade.
        seed_history(hosted, companies=("google",), count=2)
        put_preferences(sent, include_recent_openings=True)
        put_watchlist(sent, "stripe", "google")
        assert batches(hosted, "catch_up")
        database.engine.dispose()
        with pytest.raises(RuntimeError, match="catch-up notification history"):
            command.downgrade(alembic, "20261011_0008")
        with database.engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "20261025_0010"
    finally:
        database.engine.dispose()
        command.upgrade(alembic, "head")


# --- listing freshness -------------------------------------------------------

GOOGLE_SOURCE = "direct:google:google"


def observe(
    hosted,
    source_key: str = GOOGLE_SOURCE,
    *,
    at: datetime,
    succeeded: bool = True,
    complete: bool | None = True,
) -> None:
    """Record one import run in which ``source_key`` reported this outcome."""

    run_id = uuid.uuid4()
    with hosted.database.session_factory.begin() as db:
        db.add(
            HostedJobImportRun(
                id=run_id,
                source_fingerprint=uuid.uuid4().hex * 2,
                source_identifier="hosted-collection.json.gz",
                source_type="hosted_collection",
                started_at=at,
                completed_at=at,
                status="succeeded",
                created_at=at,
                updated_at=at,
            )
        )
        db.flush()
        db.add(
            HostedSourceObservation(
                import_run_id=run_id,
                source_key=source_key,
                source_kind="direct",
                company_name="Google",
                adapter="google",
                observed_at=at,
                attempted=True,
                succeeded=succeeded,
                complete=complete if succeeded else None,
                rows_returned=1 if succeeded else None,
            )
        )


def queued_titles(hosted) -> list[str]:
    with hosted.database.session_factory() as db:
        return sorted(
            db.scalars(
                select(HostedJob.title)
                .join(UserJobMatch, UserJobMatch.job_id == HostedJob.id)
                .join(
                    HostedNotificationItem,
                    HostedNotificationItem.user_job_match_id == UserJobMatch.id,
                )
            )
        )


def test_an_old_posting_the_latest_collection_still_lists_is_included(hosted) -> None:
    add_job(
        hosted,
        title="Listed Since Last Month",
        posting_date=days_ago(30),
        first_seen_at=NOW - timedelta(days=30),
    )

    onboard(account(hosted), "google")

    assert queued_titles(hosted) == ["Listed Since Last Month"]


def test_a_posting_unseen_for_over_a_week_is_never_emailed(hosted) -> None:
    add_job(hosted, title="Fresh", posting_date=days_ago(3))
    add_job(
        hosted,
        title="Unseen Eight Days",
        posting_date=days_ago(20),
        first_seen_at=NOW - timedelta(days=20),
        last_seen_at=NOW - timedelta(days=8),
    )
    client = account(hosted)

    onboard(client, "google")

    assert queued_titles(hosted) == ["Fresh"]
    [message] = run_worker(hosted)
    assert "Unseen Eight Days" not in message.text
    # It still matches on the dashboard; only the email leaves it out.
    with hosted.database.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(UserJobMatch)) == 2


def test_a_posting_its_complete_source_no_longer_lists_is_excluded(hosted) -> None:
    add_job(
        hosted,
        title="Removed Yesterday",
        posting_date=days_ago(5),
        last_seen_at=NOW - timedelta(days=1),
        source_key=GOOGLE_SOURCE,
    )
    add_job(
        hosted,
        title="Still Listed",
        posting_date=days_ago(5),
        source_key=GOOGLE_SOURCE,
    )
    # A complete collection after the first was last seen, and before the
    # run that saw the second one, did not list the first.
    observe(hosted, at=NOW - timedelta(hours=2))

    onboard(account(hosted), "google")

    assert queued_titles(hosted) == ["Still Listed"]


@pytest.mark.parametrize(
    ("succeeded", "complete"),
    [(False, None), (True, False), (True, None)],
)
def test_failed_or_incomplete_collections_never_prove_a_posting_gone(
    hosted, succeeded, complete
) -> None:
    add_job(
        hosted,
        title="Temporarily Missing",
        posting_date=days_ago(5),
        last_seen_at=NOW - timedelta(days=2),
        source_key=GOOGLE_SOURCE,
    )
    observe(hosted, at=NOW - timedelta(hours=2), succeeded=succeeded, complete=complete)

    onboard(account(hosted), "google")

    assert queued_titles(hosted) == ["Temporarily Missing"]


def test_a_recovered_source_makes_its_postings_eligible_again(hosted) -> None:
    for days in (9, 8, 7):
        observe(hosted, at=NOW - timedelta(days=days), succeeded=False)
    add_job(
        hosted,
        title="Seen Again After Recovery",
        posting_date=days_ago(12),
        first_seen_at=NOW - timedelta(days=12),
        source_key=GOOGLE_SOURCE,
    )

    onboard(account(hosted), "google")

    assert queued_titles(hosted) == ["Seen Again After Recovery"]


def test_a_daily_digest_drops_a_posting_removed_before_delivery(hosted) -> None:
    add_job(hosted, title="Removed Before Delivery", posting_date=days_ago(4), source_key=GOOGLE_SOURCE)
    add_job(hosted, title="Kept Until Delivery", posting_date=days_ago(4), source_key="direct:google:other")
    client = account(hosted)
    onboard(client, "google", alert_frequency="daily")
    assert queued_titles(hosted) == ["Kept Until Delivery", "Removed Before Delivery"]

    hosted.clock.advance(hours=3)
    observe(hosted, at=hosted.clock())
    hosted.clock.advance(hours=21)
    [message] = run_worker(hosted)

    assert message.subject == "Your recent job matches (1)"
    assert "Kept Until Delivery" in message.text
    assert "Removed Before Delivery" not in message.text
    by_title = {}
    with hosted.database.session_factory() as db:
        for item, title in db.execute(
            select(HostedNotificationItem, HostedJob.title)
            .join(UserJobMatch, UserJobMatch.id == HostedNotificationItem.user_job_match_id)
            .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
        ):
            by_title[title] = (item.status, item.cancellation_reason)
    assert by_title == {
        "Kept Until Delivery": ("sent", None),
        "Removed Before Delivery": ("cancelled", "job_unconfirmed"),
    }


def test_excluded_stale_postings_never_create_late_or_duplicate_alerts(hosted) -> None:
    add_job(
        hosted,
        title="Unseen Ten Days",
        posting_date=days_ago(15),
        first_seen_at=NOW - timedelta(days=15),
        last_seen_at=NOW - timedelta(days=10),
    )
    client = account(hosted)

    onboard(client, "google")

    assert batches(hosted) == []
    assert requests(hosted) == {"google": "empty"}
    put_watchlist(client, "google")
    hosted.clock.advance(days=1)
    assert run_worker(hosted) == []
    assert items(hosted) == []
