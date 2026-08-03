"""PostgreSQL tests for match persistence, reconciliation, and match APIs."""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.hosted.catalog import CompanyCatalog, PublicCompany
from app.hosted.database import HostedDatabase, normalize_database_url
from app.hosted.job_import import JobImportService
from app.hosted.mailer import InMemoryMailer
from app.hosted.match_service import reconcile_jobs, reconcile_user
from app.hosted.models import (
    HostedJob,
    HostedJobImportRun,
    User,
    UserCompanyWatch,
    UserJobMatch,
    UserPreference,
)
from app.hosted.security import hash_password
from app.hosted.services import HostedServices
from app.hosted.settings import HostedSettings
from app.main import app
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

BACKEND_DIR = Path(__file__).resolve().parents[1]


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def _psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@pytest.fixture(scope="session")
def match_postgres_url() -> str:
    base_url = os.getenv("HOSTED_TEST_DATABASE_URL", "").strip()
    if not base_url:
        pytest.skip("HOSTED_TEST_DATABASE_URL is required for hosted PostgreSQL tests")
    parsed = make_url(normalize_database_url(base_url))
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("HOSTED_TEST_DATABASE_URL must use PostgreSQL")
    database_name = f"internship_signal_match_test_{uuid.uuid4().hex}"
    admin_url = parsed.set(database="postgres")
    with psycopg.connect(
        _psycopg_url(admin_url.render_as_string(hide_password=False)),
        autocommit=True,
    ) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    isolated_url = parsed.set(database=database_name).render_as_string(
        hide_password=False
    )
    alembic = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic.set_main_option("sqlalchemy.url", isolated_url.replace("%", "%%"))
    existing_logger = logging.getLogger("hosted.matches.migration.existing")
    existing_logger.disabled = False
    command.upgrade(alembic, "head")
    if existing_logger.disabled:
        pytest.fail("Alembic migration disabled an existing application logger")
    command.check(alembic)
    yield isolated_url
    with psycopg.connect(
        _psycopg_url(admin_url.render_as_string(hide_password=False)),
        autocommit=True,
    ) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (database_name,),
        )
        connection.execute(
            sql.SQL("DROP DATABASE {}").format(sql.Identifier(database_name))
        )


@pytest.fixture(autouse=True)
def clean_match_database(match_postgres_url: str):
    database = HostedDatabase(match_postgres_url)
    table_names = [
        name
        for name in inspect(database.engine).get_table_names()
        if name != "alembic_version"
    ]
    with database.engine.begin() as connection:
        if table_names:
            quoted = ", ".join(f'"{name}"' for name in table_names)
            connection.execute(
                text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
            )
    database.dispose()


@pytest.fixture
def catalog() -> CompanyCatalog:
    return CompanyCatalog(
        (
            PublicCompany(
                id="capital-one",
                name="Capital One",
                aliases=("Capital One Financial",),
                coverage="direct",
                selectable=True,
            ),
            PublicCompany(
                id="stripe",
                name="Stripe",
                aliases=(),
                coverage="direct",
                selectable=True,
            ),
        )
    )


@pytest.fixture
def hosted(match_postgres_url: str, catalog: CompanyCatalog):
    settings = replace(HostedSettings.from_env(), database_url=match_postgres_url)
    mailer = InMemoryMailer()
    clock = MutableClock()
    services = HostedServices.build(
        settings=settings, mailer=mailer, clock=clock, catalog=catalog
    )
    previous = app.state.hosted_services
    app.state.hosted_services = services
    try:
        yield services, mailer, clock
    finally:
        app.state.hosted_services = previous
        services.database.dispose()


@pytest.fixture
def make_client(hosted):
    opened: list[TestClient] = []

    def factory() -> TestClient:
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return client

    yield factory
    for client in opened:
        client.__exit__(None, None, None)


def create_user(
    db,
    clock,
    email: str,
    *,
    role_ids=("software_engineering",),
    preferred_locations=("New York, NY",),
    include_remote: bool = True,
    internship_season: str = "Any season",
    alert_frequency: str = "as_detected",
    globally_paused: bool = False,
    watches=(("stripe", False),),
) -> uuid.UUID:
    now = clock()
    user = User(
        email=email,
        normalized_email=email.casefold(),
        password_hash=hash_password("secure password"),
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.flush()
    db.add(
        UserPreference(
            user_id=user.id,
            role_ids=list(role_ids),
            preferred_locations=list(preferred_locations),
            include_remote=include_remote,
            internship_season=internship_season,
            alert_frequency=alert_frequency,
            globally_paused=globally_paused,
            created_at=now,
            updated_at=now,
        )
    )
    for company_id, paused in watches:
        db.add(
            UserCompanyWatch(
                user_id=user.id,
                company_id=company_id,
                paused=paused,
                created_at=now,
                updated_at=now,
            )
        )
    return user.id


def create_job(
    db,
    clock,
    *,
    watcher_job_id: str = "watcher-1",
    company_id: str = "stripe",
    company_name: str = "Stripe",
    title: str = "Software Engineering Intern",
    location: str = "New York, NY",
    remote_status: str = "",
    role_id: str = "software_engineering",
    is_open: bool = True,
) -> uuid.UUID:
    now = clock()
    job = HostedJob(
        watcher_job_id=watcher_job_id,
        company_id=company_id,
        company_name=company_name,
        title=title,
        location=location,
        remote_status=remote_status,
        role_id=role_id,
        description="",
        requirements="",
        application_url="https://example.com/apply",
        posting_date=None,
        deadline=None,
        is_open=is_open,
        first_seen_at=now,
        last_seen_at=now,
        closed_at=None if is_open else now,
        source_metadata={},
        created_at=now,
        updated_at=now,
    )
    db.add(job)
    db.flush()
    return job.id


def signup(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/auth/signup", json={"email": email, "password": "secure password"}
    )
    assert response.status_code == 201, response.text
    return response.json()["user"]["id"]


# --- persistence and reconciliation ---------------------------------------


def test_reconciliation_creates_a_new_match(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        outcome = reconcile_jobs(db, [job_id], now=clock())
    assert outcome.created == 1
    with services.database.session_factory() as db:
        match = db.scalar(select(UserJobMatch))
    assert match.matched_at == match.last_matched_at == clock()
    assert match.no_longer_matches_at is None
    assert [reason["code"] for reason in match.match_reasons] == [
        "company_watched",
        "role_selected",
        "location_preferred",
        "season_any",
    ]


def test_duplicate_user_job_rows_are_rejected(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())
    with pytest.raises(IntegrityError):
        with services.database.session_factory.begin() as db:
            db.add(
                UserJobMatch(
                    user_id=user_id,
                    job_id=job_id,
                    match_reasons=[],
                    matched_at=clock(),
                    last_matched_at=clock(),
                    created_at=clock(),
                    updated_at=clock(),
                )
            )


def test_repeated_reconciliation_is_idempotent(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())
    with services.database.session_factory() as db:
        before = db.scalar(select(UserJobMatch))
        first_seen = (before.matched_at, before.last_matched_at, before.updated_at)

    clock.advance(hours=6)
    with services.database.session_factory.begin() as db:
        outcome = reconcile_jobs(db, [job_id], now=clock())
    assert outcome == type(outcome)()

    with services.database.session_factory() as db:
        after = db.scalar(select(UserJobMatch))
        assert (after.matched_at, after.last_matched_at, after.updated_at) == first_seen
        assert db.scalar(select(UserJobMatch.id).where(UserJobMatch.id != after.id)) is None


def test_match_deactivates_when_preferences_stop_matching(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())

    clock.advance(hours=1)
    with services.database.session_factory.begin() as db:
        preferences = db.get(UserPreference, user_id)
        preferences.role_ids = ["data_science"]
        outcome = reconcile_user(db, user_id, now=clock())
    assert outcome.deactivated == 1

    with services.database.session_factory() as db:
        match = db.scalar(select(UserJobMatch))
    assert match.no_longer_matches_at == clock()
    # History is retained rather than deleted.
    assert match.matched_at < match.no_longer_matches_at


def test_historical_match_reactivates(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())
        preferences = db.get(UserPreference, user_id)
        preferences.role_ids = ["data_science"]
        reconcile_user(db, user_id, now=clock())

    clock.advance(days=1)
    with services.database.session_factory.begin() as db:
        preferences = db.get(UserPreference, user_id)
        preferences.role_ids = ["software_engineering"]
        outcome = reconcile_user(db, user_id, now=clock())
    assert outcome.reactivated == 1

    with services.database.session_factory() as db:
        match = db.scalar(select(UserJobMatch))
    assert match.no_longer_matches_at is None
    assert match.last_matched_at == clock()
    assert match.matched_at < match.last_matched_at


def test_saved_and_dismissed_survive_reconciliation(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())
        match = db.scalar(select(UserJobMatch))
        match.saved_at = clock()
        match.dismissed_at = clock()
        saved_at, dismissed_at = match.saved_at, match.dismissed_at

    clock.advance(days=1)
    # Deactivate, then reactivate; user actions must survive both directions.
    with services.database.session_factory.begin() as db:
        db.get(UserPreference, user_id).role_ids = ["data_science"]
        reconcile_user(db, user_id, now=clock())
    with services.database.session_factory.begin() as db:
        db.get(UserPreference, user_id).role_ids = ["software_engineering"]
        reconcile_user(db, user_id, now=clock())

    with services.database.session_factory() as db:
        match = db.scalar(select(UserJobMatch))
    assert match.saved_at == saved_at
    assert match.dismissed_at == dismissed_at


def test_paused_and_resumed_company_watch_reconciles(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())

    clock.advance(hours=1)
    with services.database.session_factory.begin() as db:
        db.get(UserCompanyWatch, (user_id, "stripe")).paused = True
        assert reconcile_user(db, user_id, now=clock()).deactivated == 1
    with services.database.session_factory() as db:
        assert db.scalar(select(UserJobMatch)).no_longer_matches_at is not None

    clock.advance(hours=1)
    with services.database.session_factory.begin() as db:
        db.get(UserCompanyWatch, (user_id, "stripe")).paused = False
        assert reconcile_user(db, user_id, now=clock()).reactivated == 1
    with services.database.session_factory() as db:
        assert db.scalar(select(UserJobMatch)).no_longer_matches_at is None


def test_removing_a_company_watch_deactivates_its_matches(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())

    clock.advance(hours=1)
    with services.database.session_factory.begin() as db:
        db.delete(db.get(UserCompanyWatch, (user_id, "stripe")))
        db.flush()
        outcome = reconcile_user(db, user_id, now=clock(), company_ids=["stripe"])
    assert outcome.deactivated == 1


def test_closed_job_deactivates_existing_matches(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())

    clock.advance(hours=1)
    with services.database.session_factory.begin() as db:
        db.get(HostedJob, job_id).is_open = False
        outcome = reconcile_jobs(db, [job_id], now=clock())
    assert outcome.deactivated == 1


def test_multiple_users_receive_independent_results(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        swe = create_user(db, clock, "swe@example.com")
        data = create_user(
            db,
            clock,
            "data@example.com",
            role_ids=("data_science",),
        )
        unwatched = create_user(
            db,
            clock,
            "other@example.com",
            watches=(("capital-one", False),),
        )
        job_id = create_job(db, clock)
        outcome = reconcile_jobs(db, [job_id], now=clock())
    assert outcome.created == 1

    with services.database.session_factory() as db:
        owners = set(db.scalars(select(UserJobMatch.user_id)))
    assert owners == {swe}
    assert data not in owners and unwatched not in owners


# --- import integration ----------------------------------------------------


def final_job(**overrides) -> dict:
    job = {
        "id": "watcher-job-1",
        "company": "Stripe",
        "title": "Backend Software Engineer Intern",
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "description": "Build production APIs.",
        "requirements": "Python and SQL",
        "source_url": "https://example.com/jobs/123",
        "date_posted": "2026-08-01",
        "deadline": "2026-09-01",
        "deadline_days_left": 30,
        "internship_type": "Summer 2027 Internship",
        "role_classification": {"role": "swe", "role_track": "backend"},
        "extra": {"source": "direct", "source_adapter": "workday"},
    }
    job.update(overrides)
    return job


def test_import_counts_only_newly_created_matches(hosted, catalog) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        create_user(db, clock, "a@example.com")
    service = JobImportService(services.database, catalog, clock=clock)

    first = service.import_jobs(
        [final_job()],
        source_fingerprint="a" * 64,
        source_identifier="snapshot-a.json.gz",
        source_type="collection_snapshot",
    )
    assert first.counters.matches_created == 1
    with services.database.session_factory() as db:
        assert db.scalar(select(HostedJobImportRun.matches_created)) == 1
        assert len(db.scalars(select(UserJobMatch)).all()) == 1

    # A second distinct import of the same job creates no new match rows.
    clock.advance(hours=1)
    second = service.import_jobs(
        [final_job(title="Backend Software Engineer Intern II")],
        source_fingerprint="b" * 64,
        source_identifier="snapshot-b.json.gz",
        source_type="collection_snapshot",
    )
    assert second.counters.jobs_updated == 1
    assert second.counters.matches_created == 0


def test_reactivated_matches_do_not_count_as_created(hosted, catalog) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, "a@example.com")
    service = JobImportService(services.database, catalog, clock=clock)
    service.import_jobs(
        [final_job()],
        source_fingerprint="a" * 64,
        source_identifier="snapshot-a.json.gz",
        source_type="collection_snapshot",
    )
    clock.advance(hours=1)
    with services.database.session_factory.begin() as db:
        db.get(UserPreference, user_id).role_ids = ["data_science"]
        reconcile_user(db, user_id, now=clock())
        db.get(UserPreference, user_id).role_ids = ["software_engineering"]

    clock.advance(hours=1)
    result = service.import_jobs(
        [final_job(title="Backend Software Engineer Intern III")],
        source_fingerprint="c" * 64,
        source_identifier="snapshot-c.json.gz",
        source_type="collection_snapshot",
    )
    assert result.counters.matches_created == 0
    with services.database.session_factory() as db:
        assert db.scalar(select(UserJobMatch)).no_longer_matches_at is None


def test_already_imported_snapshot_changes_nothing(hosted, catalog) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        create_user(db, clock, "a@example.com")
    service = JobImportService(services.database, catalog, clock=clock)
    first = service.import_jobs(
        [final_job()],
        source_fingerprint="a" * 64,
        source_identifier="snapshot-a.json.gz",
        source_type="collection_snapshot",
    )
    with services.database.session_factory() as db:
        before = db.scalar(select(UserJobMatch))
        snapshot = (
            before.id,
            before.matched_at,
            before.last_matched_at,
            before.updated_at,
            before.no_longer_matches_at,
        )

    clock.advance(days=1)
    repeat = service.import_jobs(
        [final_job()],
        source_fingerprint="a" * 64,
        source_identifier="snapshot-a.json.gz",
        source_type="collection_snapshot",
    )
    assert repeat.already_imported is True
    assert repeat.counters.matches_created == first.counters.matches_created

    with services.database.session_factory() as db:
        after = db.scalar(select(UserJobMatch))
        assert (
            after.id,
            after.matched_at,
            after.last_matched_at,
            after.updated_at,
            after.no_longer_matches_at,
        ) == snapshot
        assert len(db.scalars(select(UserJobMatch)).all()) == 1


def test_failed_import_leaves_no_partial_match_rows(hosted, catalog) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        create_user(db, clock, "a@example.com")
    service = JobImportService(services.database, catalog, clock=clock)

    # A duplicate watcher ID aborts the mapped batch before any persistence.
    with pytest.raises(Exception):
        service.import_jobs(
            [final_job(), final_job()],
            source_fingerprint="d" * 64,
            source_identifier="snapshot-d.json.gz",
            source_type="collection_snapshot",
        )
    with services.database.session_factory() as db:
        assert db.scalars(select(UserJobMatch)).all() == []
        run = db.scalar(select(HostedJobImportRun))
    assert run.status == "failed"
    assert run.matches_created == 0


# --- API -------------------------------------------------------------------


def seeded_match(services, clock, email: str = "owner@example.com"):
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, email)
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())
        match_id = db.scalar(select(UserJobMatch.id))
    return user_id, job_id, match_id


def test_match_endpoints_require_authentication(hosted, make_client) -> None:
    services, _mailer, clock = hosted
    _user_id, _job_id, match_id = seeded_match(services, clock)
    client = make_client()
    assert client.get("/api/matches").status_code == 401
    assert client.get(f"/api/matches/{match_id}").status_code == 401
    assert (
        client.patch(f"/api/matches/{match_id}", json={"saved": True}).status_code
        == 401
    )


def test_list_returns_only_the_authenticated_users_matches(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    owner_client = make_client()
    other_client = make_client()
    owner_id = uuid.UUID(signup(owner_client, "owner@example.com"))
    signup(other_client, "other@example.com")

    with services.database.session_factory.begin() as db:
        now = clock()
        for user_id in db.scalars(select(User.id)):
            db.add(
                UserCompanyWatch(
                    user_id=user_id,
                    company_id="stripe",
                    paused=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        job_id = create_job(db, clock)
        db.flush()
        reconcile_jobs(db, [job_id], now=now)

    owner_matches = owner_client.get("/api/matches").json()
    other_matches = other_client.get("/api/matches").json()
    assert owner_matches["total"] == 1
    assert other_matches["total"] == 1
    owner_match_id = owner_matches["items"][0]["id"]
    other_match_id = other_matches["items"][0]["id"]
    assert owner_match_id != other_match_id

    with services.database.session_factory() as db:
        stored_owner = db.get(UserJobMatch, uuid.UUID(owner_match_id))
    assert stored_owner.user_id == owner_id

    # Guessing a valid UUID owned by somebody else is an ordinary 404.
    assert owner_client.get(f"/api/matches/{other_match_id}").status_code == 404
    assert (
        owner_client.patch(
            f"/api/matches/{other_match_id}", json={"saved": True}
        ).status_code
        == 404
    )
    with services.database.session_factory() as db:
        assert db.get(UserJobMatch, uuid.UUID(other_match_id)).saved_at is None


def test_match_payload_carries_the_fields_the_frontend_renders(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    with services.database.session_factory.begin() as db:
        now = clock()
        user_id = db.scalar(select(User.id))
        db.add(
            UserCompanyWatch(
                user_id=user_id,
                company_id="stripe",
                paused=False,
                created_at=now,
                updated_at=now,
            )
        )
        job_id = create_job(db, clock, remote_status="Remote")
        db.flush()
        reconcile_jobs(db, [job_id], now=now)

    item = client.get("/api/matches").json()["items"][0]
    assert item["job_id"] == str(job_id)
    assert item["company"] == "Stripe"
    assert item["title"] == "Software Engineering Intern"
    assert item["application_url"] == "https://example.com/apply"
    assert item["role_id"] == "software_engineering"
    assert item["remote"] is True
    assert item["is_open"] is True
    assert item["matched_at"] and item["last_matched_at"]
    assert item["saved_at"] is None and item["dismissed_at"] is None
    assert all(reason["code"] for reason in item["match_reasons"])
    detail = client.get(f"/api/matches/{item['id']}").json()
    assert detail["id"] == item["id"]


def test_save_and_dismiss_are_independent_and_persist(hosted, make_client) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    with services.database.session_factory.begin() as db:
        now = clock()
        user_id = db.scalar(select(User.id))
        db.add(
            UserCompanyWatch(
                user_id=user_id,
                company_id="stripe",
                paused=False,
                created_at=now,
                updated_at=now,
            )
        )
        job_id = create_job(db, clock)
        db.flush()
        reconcile_jobs(db, [job_id], now=now)
    match_id = client.get("/api/matches").json()["items"][0]["id"]

    saved = client.patch(f"/api/matches/{match_id}", json={"saved": True}).json()
    assert saved["saved_at"] is not None
    dismissed = client.patch(
        f"/api/matches/{match_id}", json={"dismissed": True}
    ).json()
    # Documented behavior: dismissing never clears an existing save.
    assert dismissed["saved_at"] is not None
    assert dismissed["dismissed_at"] is not None

    assert client.get("/api/matches").json()["total"] == 0
    assert client.get("/api/matches?view=dismissed").json()["total"] == 1
    assert client.get("/api/matches?view=saved").json()["total"] == 1
    assert client.get("/api/matches?view=all").json()["total"] == 1

    cleared = client.patch(f"/api/matches/{match_id}", json={"saved": False}).json()
    assert cleared["saved_at"] is None


def test_historical_view_lists_matches_that_stopped_matching(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    with services.database.session_factory.begin() as db:
        now = clock()
        user_id = db.scalar(select(User.id))
        db.add(
            UserCompanyWatch(
                user_id=user_id,
                company_id="stripe",
                paused=False,
                created_at=now,
                updated_at=now,
            )
        )
        job_id = create_job(db, clock)
        db.flush()
        reconcile_jobs(db, [job_id], now=now)
        db.get(HostedJob, job_id).is_open = False
        reconcile_jobs(db, [job_id], now=now)

    assert client.get("/api/matches").json()["total"] == 0
    historical = client.get("/api/matches?view=historical").json()
    assert historical["total"] == 1
    assert historical["items"][0]["no_longer_matches_at"] is not None


def test_invalid_filters_and_pagination_bounds_are_rejected(
    hosted, make_client
) -> None:
    client = make_client()
    signup(client, "owner@example.com")
    assert client.get("/api/matches?view=everything").status_code == 400
    assert client.get("/api/matches?limit=0").status_code == 422
    assert client.get("/api/matches?limit=101").status_code == 422
    assert client.get("/api/matches?offset=-1").status_code == 422
    assert client.get("/api/matches?offset=10001").status_code == 422
    assert client.get("/api/matches?limit=abc").status_code == 422

    page = client.get("/api/matches?limit=1&offset=0").json()
    assert page["limit"] == 1 and page["offset"] == 0 and page["has_more"] is False


def test_patch_rejects_unknown_and_empty_payloads(hosted, make_client) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    with services.database.session_factory.begin() as db:
        now = clock()
        user_id = db.scalar(select(User.id))
        db.add(
            UserCompanyWatch(
                user_id=user_id,
                company_id="stripe",
                paused=False,
                created_at=now,
                updated_at=now,
            )
        )
        job_id = create_job(db, clock)
        db.flush()
        reconcile_jobs(db, [job_id], now=now)
    match_id = client.get("/api/matches").json()["items"][0]["id"]

    for payload in (
        {},
        {"user_id": str(uuid.uuid4())},
        {"match_reasons": []},
        {"matched_at": "2026-01-01T00:00:00Z"},
        {"title": "hacked"},
        {"saved": "yes"},
    ):
        assert (
            client.patch(f"/api/matches/{match_id}", json=payload).status_code == 422
        ), payload

    assert client.get(f"/api/matches/{uuid.uuid4()}").status_code == 404
    assert client.get("/api/matches/not-a-uuid").status_code == 422


def test_preference_update_endpoint_reconciles_matches(hosted, make_client) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    client.put(
        "/api/watchlist",
        json={"companies": [{"company_id": "stripe", "paused": False}]},
    )
    with services.database.session_factory.begin() as db:
        job_id = create_job(db, clock, role_id="data_science")
        db.flush()
        reconcile_jobs(db, [job_id], now=clock())
    assert client.get("/api/matches").json()["total"] == 0

    payload = {
        "role_ids": ["data_science"],
        "preferred_locations": ["New York, NY"],
        "include_remote": True,
        "internship_season": "Any season",
        "alert_frequency": "as_detected",
        "globally_paused": False,
    }
    assert client.put("/api/preferences", json=payload).status_code == 200
    assert client.get("/api/matches").json()["total"] == 1

    # Notification-only changes must not alter match state.
    clock.advance(hours=1)
    with services.database.session_factory() as db:
        before = db.scalar(select(UserJobMatch.last_matched_at))
    paused_payload = {**payload, "alert_frequency": "paused", "globally_paused": True}
    assert client.put("/api/preferences", json=paused_payload).status_code == 200
    assert client.get("/api/matches").json()["total"] == 1
    with services.database.session_factory() as db:
        assert db.scalar(select(UserJobMatch.last_matched_at)) == before


def test_watchlist_endpoint_reconciles_add_pause_and_remove(
    hosted, make_client
) -> None:
    services, _mailer, clock = hosted
    client = make_client()
    signup(client, "owner@example.com")
    with services.database.session_factory.begin() as db:
        job_id = create_job(db, clock)
        db.flush()
        reconcile_jobs(db, [job_id], now=clock())
    assert client.get("/api/matches").json()["total"] == 0

    watch = {"companies": [{"company_id": "stripe", "paused": False}]}
    assert client.put("/api/watchlist", json=watch).status_code == 200
    assert client.get("/api/matches").json()["total"] == 1

    paused = {"companies": [{"company_id": "stripe", "paused": True}]}
    assert client.put("/api/watchlist", json=paused).status_code == 200
    assert client.get("/api/matches").json()["total"] == 0
    assert client.get("/api/matches?view=historical").json()["total"] == 1

    assert client.put("/api/watchlist", json=watch).status_code == 200
    assert client.get("/api/matches").json()["total"] == 1

    assert client.put("/api/watchlist", json={"companies": []}).status_code == 200
    assert client.get("/api/matches").json()["total"] == 0


def test_deleting_an_account_removes_its_match_rows(hosted) -> None:
    services, _mailer, clock = hosted
    with services.database.session_factory.begin() as db:
        user_id = create_user(db, clock, "a@example.com")
        job_id = create_job(db, clock)
        reconcile_jobs(db, [job_id], now=clock())
    with services.database.session_factory.begin() as db:
        db.delete(db.get(User, user_id))
    with services.database.session_factory() as db:
        assert db.scalars(select(UserJobMatch)).all() == []
        assert db.scalar(select(HostedJob.id)) == job_id
