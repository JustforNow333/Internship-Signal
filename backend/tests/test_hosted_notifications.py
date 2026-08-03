"""PostgreSQL integration tests for durable hosted notification delivery."""

from __future__ import annotations

import logging
import os
import threading
import uuid
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.hosted.catalog import CompanyCatalog, PublicCompany
from app.hosted.database import HostedDatabase, normalize_database_url
from app.hosted.job_import import JobImportService, JobUpsertFailed
from app.hosted.match_service import reconcile_user
from app.hosted.models import (
    HostedJob,
    HostedJobImportAttempt,
    HostedJobImportRun,
    HostedNotificationAttempt,
    HostedNotificationBatch,
    HostedNotificationItem,
    User,
    UserCompanyWatch,
    UserJobMatch,
    UserPreference,
)
from app.hosted.notification_mail import DeliveryResult, NotificationEmail
from app.hosted.notification_worker import NotificationDeliveryWorker
from app.hosted.security import hash_password
from app.hosted.snapshot_jobs import replay_snapshot_jobs, snapshot_sha256
from psycopg import sql
from sqlalchemy import func, inspect, select, text
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


class RecordingTransport:
    def __init__(self, *results: DeliveryResult) -> None:
        self.results = list(results) or [DeliveryResult("sent")]
        self.messages: list[NotificationEmail] = []

    def send(self, message: NotificationEmail) -> DeliveryResult:
        self.messages.append(message)
        return self.results.pop(0) if self.results else DeliveryResult("sent")


def _psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@pytest.fixture(scope="session")
def notification_postgres_url() -> str:
    base_url = os.getenv("HOSTED_TEST_DATABASE_URL", "").strip()
    if not base_url:
        pytest.skip("HOSTED_TEST_DATABASE_URL is required for hosted PostgreSQL tests")
    parsed = make_url(normalize_database_url(base_url))
    database_name = f"internship_signal_notification_test_{uuid.uuid4().hex}"
    admin_url = parsed.set(database="postgres")
    with psycopg.connect(
        _psycopg_url(admin_url.render_as_string(hide_password=False)), autocommit=True
    ) as connection:
        connection.execute(
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name))
        )
    isolated_url = parsed.set(database=database_name).render_as_string(
        hide_password=False
    )
    alembic = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic.set_main_option("sqlalchemy.url", isolated_url.replace("%", "%%"))
    existing_logger = logging.getLogger("hosted.notifications.migration.existing")
    existing_logger.disabled = False
    command.upgrade(alembic, "head")
    command.downgrade(alembic, "20260803_0003")
    migration_database = HostedDatabase(isolated_url)
    try:
        remaining = set(inspect(migration_database.engine).get_table_names())
        assert not {
            "hosted_notification_batches",
            "hosted_notification_items",
            "hosted_notification_attempts",
        } & remaining
    finally:
        migration_database.dispose()
    command.upgrade(alembic, "head")
    command.check(alembic)
    assert not existing_logger.disabled
    yield isolated_url
    with psycopg.connect(
        _psycopg_url(admin_url.render_as_string(hide_password=False)), autocommit=True
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
def clean_database(notification_postgres_url: str):
    database = HostedDatabase(notification_postgres_url)
    tables = [
        name
        for name in inspect(database.engine).get_table_names()
        if name != "alembic_version"
    ]
    with database.engine.begin() as connection:
        if tables:
            connection.execute(
                text(
                    "TRUNCATE TABLE "
                    + ", ".join(f'"{name}"' for name in tables)
                    + " RESTART IDENTITY CASCADE"
                )
            )
    database.dispose()


@pytest.fixture
def database(notification_postgres_url: str):
    value = HostedDatabase(notification_postgres_url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture
def catalog() -> CompanyCatalog:
    return CompanyCatalog(
        (
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
def clock() -> MutableClock:
    return MutableClock()


def create_user(
    db,
    now: datetime,
    email: str,
    *,
    frequency: str = "as_detected",
    verified: bool = True,
    active: bool = True,
    globally_paused: bool = False,
) -> uuid.UUID:
    user = User(
        email=email,
        normalized_email=email.casefold(),
        password_hash=hash_password("secure password"),
        email_verified_at=now if verified else None,
        is_active=active,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.flush()
    db.add_all(
        [
            UserPreference(
                user_id=user.id,
                role_ids=["software_engineering"],
                preferred_locations=["New York, NY"],
                include_remote=True,
                internship_season="Any season",
                alert_frequency=frequency,
                globally_paused=globally_paused,
                created_at=now,
                updated_at=now,
            ),
            UserCompanyWatch(
                user_id=user.id,
                company_id="stripe",
                paused=False,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    return user.id


def final_job(index: int = 1, **overrides) -> dict:
    value = {
        "id": f"watcher-job-{index}",
        "company": "Stripe",
        "title": f"Backend Software Engineer Intern {index}",
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "description": "Build production APIs.",
        "requirements": "Python and SQL",
        "source_url": f"https://example.com/jobs/{index}",
        "date_posted": "2026-08-01",
        "deadline": "2026-09-01",
        "deadline_days_left": 30,
        "internship_type": "Summer 2027 Internship",
        "role_classification": {"role": "swe", "role_track": "backend"},
        "extra": {"source": "direct", "source_adapter": "workday"},
    }
    value.update(overrides)
    return value


def import_jobs(
    database: HostedDatabase,
    catalog: CompanyCatalog,
    clock: MutableClock,
    jobs: list[dict],
    marker: str,
):
    return JobImportService(database, catalog, clock=clock).import_jobs(
        jobs,
        source_fingerprint=marker * 64,
        source_identifier=f"snapshot-{marker}.json.gz",
        source_type="collection_snapshot",
    )


def worker(
    database: HostedDatabase,
    clock: MutableClock,
    transport: RecordingTransport,
) -> NotificationDeliveryWorker:
    return NotificationDeliveryWorker(
        database,
        transport,
        "https://internships.example",
        clock=clock,
    )


def test_import_eligibility_and_due_windows(database, catalog, clock) -> None:
    settings = [
        ("immediate@example.com", "as_detected", True, True, False),
        ("three@example.com", "three_hour", True, True, False),
        ("daily@example.com", "daily", True, True, False),
        ("paused@example.com", "paused", True, True, False),
        ("global@example.com", "daily", True, True, True),
        ("inactive@example.com", "daily", True, False, False),
        ("unverified@example.com", "daily", False, True, False),
    ]
    with database.session_factory.begin() as db:
        for email, frequency, verified, active, globally_paused in settings:
            create_user(
                db,
                clock(),
                email,
                frequency=frequency,
                verified=verified,
                active=active,
                globally_paused=globally_paused,
            )

    result = import_jobs(database, catalog, clock, [final_job()], "a")
    assert result.counters.matches_created == len(settings)
    with database.session_factory() as db:
        batches = {batch.frequency: batch for batch in db.scalars(select(HostedNotificationBatch))}
        assert set(batches) == {"as_detected", "three_hour", "daily"}
        assert batches["as_detected"].due_at == clock()
        assert batches["three_hour"].due_at == clock() + timedelta(hours=3)
        assert batches["daily"].due_at == clock() + timedelta(hours=24)
        assert db.scalar(select(func.count()).select_from(HostedNotificationItem)) == 3


def test_rolling_batches_group_until_claim_and_as_detected_uses_import_run(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "three@example.com", frequency="three_hour")
        create_user(db, clock(), "instant@example.com", frequency="as_detected")
    first = import_jobs(database, catalog, clock, [final_job(1)], "a")
    clock.advance(hours=1)
    second = import_jobs(database, catalog, clock, [final_job(2)], "b")

    with database.session_factory() as db:
        rolling = list(
            db.scalars(
                select(HostedNotificationBatch).where(
                    HostedNotificationBatch.frequency == "three_hour"
                )
            )
        )
        instant = list(
            db.scalars(
                select(HostedNotificationBatch).where(
                    HostedNotificationBatch.frequency == "as_detected"
                )
            )
        )
        assert len(rolling) == 1
        assert len(rolling[0].items) == 2
        assert rolling[0].due_at == datetime(2026, 8, 3, 15, 0, tzinfo=UTC)
        assert {batch.source_import_run_id for batch in instant} == {
            first.run_id,
            second.run_id,
        }


def test_reconciliation_unpause_and_watch_changes_never_create_backlog(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        user_id = create_user(
            db, clock(), "paused@example.com", frequency="paused"
        )
    import_jobs(database, catalog, clock, [final_job()], "a")
    with database.session_factory.begin() as db:
        preference = db.get(UserPreference, user_id)
        preference.alert_frequency = "as_detected"
        reconcile_user(db, user_id, now=clock())
        watch = db.get(UserCompanyWatch, (user_id, "stripe"))
        watch.paused = True
        reconcile_user(db, user_id, now=clock())
        watch.paused = False
        reconcile_user(db, user_id, now=clock())
    with database.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(HostedNotificationItem)) == 0


def test_already_imported_is_complete_notification_noop(database, catalog, clock) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "instant@example.com")
    first = import_jobs(database, catalog, clock, [final_job()], "a")
    with database.session_factory() as db:
        batch = db.scalar(select(HostedNotificationBatch))
        before = (batch.id, batch.created_at, batch.updated_at, batch.due_at)
        counts = tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (
                HostedNotificationBatch,
                HostedNotificationItem,
                HostedNotificationAttempt,
            )
        )
    clock.advance(days=1)
    repeated = import_jobs(database, catalog, clock, [final_job()], "a")
    assert repeated.already_imported
    assert repeated.counters.matches_created == first.counters.matches_created
    with database.session_factory() as db:
        batch = db.scalar(select(HostedNotificationBatch))
        assert (batch.id, batch.created_at, batch.updated_at, batch.due_at) == before
        assert counts == tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (
                HostedNotificationBatch,
                HostedNotificationItem,
                HostedNotificationAttempt,
            )
        )


def test_notification_item_is_unique_for_match_lifetime(database, catalog, clock) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "unique@example.com")
    result = import_jobs(database, catalog, clock, [final_job()], "a")
    with pytest.raises(IntegrityError):
        with database.session_factory.begin() as db:
            item = db.scalar(select(HostedNotificationItem))
            db.add(
                HostedNotificationItem(
                    batch_id=item.batch_id,
                    user_job_match_id=item.user_job_match_id,
                    source_import_run_id=result.run_id,
                    status="pending",
                    created_at=clock(),
                    updated_at=clock(),
                )
            )


def test_notification_failure_rolls_back_import_and_matches(
    database, catalog, clock, monkeypatch
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "instant@example.com")

    def fail(*_args, **_kwargs):
        raise RuntimeError("do not persist this raw failure")

    monkeypatch.setattr("app.hosted.job_import.enqueue_import_notifications", fail)
    with pytest.raises(JobUpsertFailed):
        import_jobs(database, catalog, clock, [final_job()], "a")
    with database.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(HostedJob)) == 0
        assert db.scalar(select(func.count()).select_from(UserJobMatch)) == 0
        assert db.scalar(select(func.count()).select_from(HostedNotificationBatch)) == 0
        assert db.scalar(select(func.count()).select_from(HostedNotificationItem)) == 0
        run = db.scalar(select(HostedJobImportRun))
        attempt = db.scalar(select(HostedJobImportAttempt))
        assert (run.status, run.matches_created) == ("failed", 0)
        assert attempt.status == "failed"
        assert "do not persist" not in (run.failure_summary or "")


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("inactive", "user_inactive"),
        ("unverified", "email_unverified"),
        ("global", "globally_paused"),
        ("paused", "frequency_paused"),
        ("different", "frequency_changed"),
    ],
)
def test_delivery_time_user_cancellation(
    database, catalog, clock, change, code
) -> None:
    with database.session_factory.begin() as db:
        user_id = create_user(db, clock(), f"{change}@example.com")
    import_jobs(database, catalog, clock, [final_job()], "a")
    with database.session_factory.begin() as db:
        user = db.get(User, user_id)
        preference = db.get(UserPreference, user_id)
        if change == "inactive":
            user.is_active = False
        elif change == "unverified":
            user.email_verified_at = None
        elif change == "global":
            preference.globally_paused = True
        elif change == "paused":
            preference.alert_frequency = "paused"
        else:
            preference.alert_frequency = "daily"
    transport = RecordingTransport()
    summary = worker(database, clock, transport).run()
    assert summary.cancelled == 1
    assert not transport.messages
    with database.session_factory() as db:
        batch = db.scalar(select(HostedNotificationBatch))
        item = db.scalar(select(HostedNotificationItem))
        assert (batch.status, batch.last_error_code) == ("cancelled", code)
        assert (item.status, item.cancellation_reason) == ("cancelled", code)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("dismiss", "match_dismissed"),
        ("inactive", "match_inactive"),
        ("closed", "job_closed"),
    ],
)
def test_delivery_time_item_cancellation(database, catalog, clock, change, code) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), f"{change}@example.com")
    import_jobs(database, catalog, clock, [final_job()], "a")
    with database.session_factory.begin() as db:
        match = db.scalar(select(UserJobMatch))
        if change == "dismiss":
            match.dismissed_at = clock()
        elif change == "inactive":
            match.no_longer_matches_at = clock()
        else:
            db.get(HostedJob, match.job_id).is_open = False
    transport = RecordingTransport()
    worker(database, clock, transport).run()
    assert not transport.messages
    with database.session_factory() as db:
        item = db.scalar(select(HostedNotificationItem))
        assert (item.status, item.cancellation_reason) == ("cancelled", code)
        assert db.scalar(select(HostedNotificationBatch.status)) == "cancelled"


def test_saved_match_delivers_and_two_workers_cannot_claim_same_batch(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "saved@example.com")
    import_jobs(database, catalog, clock, [final_job()], "a")
    with database.session_factory.begin() as db:
        db.scalar(select(UserJobMatch)).saved_at = clock()
    first_transport = RecordingTransport()
    first = worker(database, clock, first_transport)
    second = worker(database, clock, RecordingTransport())
    claims = first.claim_due_batches(limit=25)
    assert len(claims) == 1
    assert second.claim_due_batches(limit=25) == []
    prepared = first._prepare_delivery(*claims[0])
    assert prepared is not None
    assert first._apply_result(prepared, first_transport.send(prepared.message)) == "sent"
    assert len(first_transport.messages) == 1


def test_lease_recovery_before_and_after_send_started(database, catalog, clock) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "lease@example.com")
    import_jobs(database, catalog, clock, [final_job()], "a")
    delivery = worker(database, clock, RecordingTransport())
    [(batch_id, token)] = delivery.claim_due_batches(limit=1)
    clock.advance(minutes=11)
    assert delivery.recover_expired_leases() == (1, 0)
    [(batch_id, token)] = delivery.claim_due_batches(limit=1)
    prepared = delivery._prepare_delivery(batch_id, token)
    assert prepared is not None
    clock.advance(minutes=11)
    assert delivery.recover_expired_leases() == (0, 1)
    with database.session_factory() as db:
        batch = db.get(HostedNotificationBatch, batch_id)
        attempt = db.scalar(select(HostedNotificationAttempt))
        assert (batch.status, batch.last_error_code) == (
            "uncertain",
            "lease_expired_after_send_started",
        )
        assert (attempt.outcome, attempt.error_code) == (
            "uncertain",
            "lease_expired_after_send_started",
        )


def test_retries_stable_message_id_attempt_history_and_exhaustion(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "retry@example.com")
    import_jobs(database, catalog, clock, [final_job()], "a")
    transport = RecordingTransport(
        *(DeliveryResult("retryable_failure", "smtp_4xx") for _ in range(5))
    )
    delivery = worker(database, clock, transport)
    expected_delays = [1, 5, 15, 60]
    for delay in expected_delays:
        summary = delivery.run()
        assert summary.retryable_failures == 1
        with database.session_factory() as db:
            batch = db.scalar(select(HostedNotificationBatch))
            assert batch.status == "pending"
            assert batch.next_attempt_at == clock() + timedelta(minutes=delay)
        clock.advance(minutes=delay)
    delivery.run()
    with database.session_factory() as db:
        batch = db.scalar(select(HostedNotificationBatch))
        attempts = list(
            db.scalars(
                select(HostedNotificationAttempt).order_by(
                    HostedNotificationAttempt.attempt_number
                )
            )
        )
        assert (batch.status, batch.attempt_count, batch.last_error_code) == (
            "permanent_failed",
            5,
            "retry_exhausted",
        )
        assert [attempt.outcome for attempt in attempts] == [
            "retryable_failure"
        ] * 5
        assert [attempt.attempt_number for attempt in attempts] == [1, 2, 3, 4, 5]
    assert len({message.message_id for message in transport.messages}) == 1


@pytest.mark.parametrize(
    ("result", "status", "attempt_outcome"),
    [
        (DeliveryResult("permanent_failure", "recipient_rejected"), "permanent_failed", "permanent_failure"),
        (DeliveryResult("uncertain", "connection_lost_after_submission"), "uncertain", "uncertain"),
    ],
)
def test_permanent_and_ambiguous_outcomes_stop(
    database, catalog, clock, result, status, attempt_outcome
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), f"{status}@example.com")
    import_jobs(database, catalog, clock, [final_job()], "a")
    transport = RecordingTransport(result)
    delivery = worker(database, clock, transport)
    delivery.run()
    clock.advance(days=1)
    assert delivery.run().claimed == 0
    with database.session_factory() as db:
        assert db.scalar(select(HostedNotificationBatch.status)) == status
        attempt = db.scalar(select(HostedNotificationAttempt))
        assert attempt.outcome == attempt_outcome
        assert attempt.completed_at is not None


def test_success_marks_all_items_sent_even_when_only_25_are_rendered(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "digest@example.com")
    import_jobs(
        database,
        catalog,
        clock,
        [final_job(index) for index in range(1, 28)],
        "a",
    )
    transport = RecordingTransport()
    worker(database, clock, transport).run()
    assert "2 additional matches" in transport.messages[0].text
    with database.session_factory() as db:
        assert db.scalar(
            select(func.count()).select_from(HostedNotificationItem).where(
                HostedNotificationItem.status == "sent"
            )
        ) == 27


def test_concurrent_imports_share_one_rolling_batch(database, catalog, clock) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "concurrent@example.com", frequency="daily")
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def run(index: int, marker: str) -> None:
        try:
            barrier.wait()
            import_jobs(database, catalog, clock, [final_job(index)], marker)
        except Exception as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(1, "a")),
        threading.Thread(target=run, args=(2, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors
    with database.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(HostedNotificationBatch)) == 1
        assert db.scalar(select(func.count()).select_from(HostedNotificationItem)) == 2


def test_verified_snapshot_notification_smoke(database, clock) -> None:
    snapshot_value = os.getenv("HOSTED_NOTIFICATION_SMOKE_SNAPSHOT", "").strip()
    if not snapshot_value:
        pytest.skip("HOSTED_NOTIFICATION_SMOKE_SNAPSHOT is required for snapshot smoke")
    snapshot = Path(snapshot_value)
    expected_sha = "c8192163709444f55c98d35508dcec09efccf7a637fe8dca9e61b4fda57388db"
    assert snapshot_sha256(snapshot) == expected_sha
    watcher_paths = [
        Path(value)
        for value in os.getenv("HOSTED_NOTIFICATION_SMOKE_WATCHER_FILES", "").split(",")
        if value.strip()
    ]

    def fingerprints() -> dict[str, str]:
        return {
            str(path): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in watcher_paths
        }

    watcher_before = fingerprints()
    replayed = replay_snapshot_jobs(snapshot)
    smoke_catalog = CompanyCatalog.from_watcher_config(replayed.config)
    company_ids = [company.id for company in smoke_catalog.companies if company.selectable]
    frequencies = {
        "immediate": "as_detected",
        "three": "three_hour",
        "daily": "daily",
        "paused": "paused",
        "global": "daily",
        "cancel": "as_detected",
        "retry": "as_detected",
        "permanent": "as_detected",
        "uncertain": "as_detected",
    }
    user_ids: dict[str, uuid.UUID] = {}
    with database.session_factory.begin() as db:
        for name, frequency in frequencies.items():
            user_id = create_user(
                db,
                clock(),
                f"smoke-{name}@example.com",
                frequency=frequency,
                globally_paused=name == "global",
            )
            user_ids[name] = user_id
            preference = db.get(UserPreference, user_id)
            preference.role_ids = [
                "software_engineering",
                "machine_learning_ai",
                "data_science",
                "data_engineering",
                "quantitative_development",
                "product_management",
                "hardware_embedded",
                "other_engineering",
            ]
            preference.preferred_locations = []
            db.delete(db.get(UserCompanyWatch, (user_id, "stripe")))
            for company_id in company_ids:
                db.add(
                    UserCompanyWatch(
                        user_id=user_id,
                        company_id=company_id,
                        paused=False,
                        created_at=clock(),
                        updated_at=clock(),
                    )
                )

    service = JobImportService(database, smoke_catalog, clock=clock)
    first = service.import_jobs(
        replayed.jobs,
        source_fingerprint=replayed.source_fingerprint,
        source_identifier=replayed.source_identifier,
        source_type="collection_snapshot",
    )
    with database.session_factory() as db:
        match_count = db.scalar(select(func.count()).select_from(UserJobMatch))
        item_count = db.scalar(select(func.count()).select_from(HostedNotificationItem))
        per_user_matches = match_count // len(frequencies)
        assert first.counters.matches_created == match_count
        assert item_count == per_user_matches * 7

    # Preference and watchlist reconciliation can change match activity but
    # cannot enqueue the paused user's already-existing matches.
    with database.session_factory.begin() as db:
        paused_id = user_ids["paused"]
        before = db.scalar(select(func.count()).select_from(HostedNotificationItem))
        db.get(UserPreference, paused_id).alert_frequency = "as_detected"
        reconcile_user(db, paused_id, now=clock())
        first_company = company_ids[0]
        watch = db.get(UserCompanyWatch, (paused_id, first_company))
        watch.paused = True
        reconcile_user(db, paused_id, now=clock(), company_ids=[first_company])
        watch.paused = False
        reconcile_user(db, paused_id, now=clock(), company_ids=[first_company])
        db.flush()
        assert db.scalar(select(func.count()).select_from(HostedNotificationItem)) == before
        db.get(UserPreference, user_ids["cancel"]).globally_paused = True

    transport = RecordingTransport()
    delivery = worker(database, clock, transport)
    claims = delivery.claim_due_batches(limit=100)
    assert len(claims) == 5
    assert worker(database, clock, RecordingTransport()).claim_due_batches(limit=100) == []
    with database.session_factory() as db:
        owner_by_batch = {
            batch.id: batch.user_id
            for batch in db.scalars(
                select(HostedNotificationBatch).where(
                    HostedNotificationBatch.id.in_([batch_id for batch_id, _ in claims])
                )
            )
        }
    retry_message_id = None
    for batch_id, token in claims:
        owner = owner_by_batch[batch_id]
        prepared = delivery._prepare_delivery(batch_id, token)
        if owner == user_ids["cancel"]:
            assert prepared is None
            continue
        assert prepared is not None
        if owner == user_ids["retry"]:
            retry_message_id = prepared.message.message_id
            delivery._apply_result(prepared, DeliveryResult("retryable_failure", "smtp_4xx"))
        elif owner == user_ids["permanent"]:
            delivery._apply_result(
                prepared,
                DeliveryResult("permanent_failure", "recipient_rejected"),
            )
        elif owner == user_ids["uncertain"]:
            delivery._apply_result(
                prepared,
                DeliveryResult("uncertain", "connection_lost_after_submission"),
            )
        else:
            delivery._apply_result(prepared, DeliveryResult("sent"))

    clock.advance(minutes=1)
    [(retry_batch_id, retry_token)] = delivery.claim_due_batches(limit=100)
    prepared = delivery._prepare_delivery(retry_batch_id, retry_token)
    assert prepared is not None and prepared.message.message_id == retry_message_id
    delivery._apply_result(prepared, DeliveryResult("sent"))
    clock.advance(minutes=179)
    assert worker(database, clock, RecordingTransport()).run().sent == 1
    clock.advance(hours=21)
    assert worker(database, clock, RecordingTransport()).run().sent == 1

    with database.session_factory() as db:
        before = (
            db.scalar(select(func.count()).select_from(HostedNotificationBatch)),
            db.scalar(select(func.count()).select_from(HostedNotificationItem)),
            db.scalar(select(func.count()).select_from(HostedNotificationAttempt)),
            tuple(
                db.execute(
                    select(
                        HostedNotificationBatch.id,
                        HostedNotificationBatch.status,
                        HostedNotificationBatch.updated_at,
                    ).order_by(HostedNotificationBatch.id)
                ).all()
            ),
        )
    repeated = service.import_jobs(
        replayed.jobs,
        source_fingerprint=replayed.source_fingerprint,
        source_identifier=replayed.source_identifier,
        source_type="collection_snapshot",
    )
    assert repeated.already_imported
    with database.session_factory() as db:
        after = (
            db.scalar(select(func.count()).select_from(HostedNotificationBatch)),
            db.scalar(select(func.count()).select_from(HostedNotificationItem)),
            db.scalar(select(func.count()).select_from(HostedNotificationAttempt)),
            tuple(
                db.execute(
                    select(
                        HostedNotificationBatch.id,
                        HostedNotificationBatch.status,
                        HostedNotificationBatch.updated_at,
                    ).order_by(HostedNotificationBatch.id)
                ).all()
            ),
        )
    assert after == before
    assert fingerprints() == watcher_before
    print(
        "PHASE3A-SMOKE "
        f"sha256={expected_sha} jobs={len(replayed.jobs)} "
        f"matches_created={first.counters.matches_created} "
        f"notification_items={item_count} watcher_files={len(watcher_before)}"
    )
