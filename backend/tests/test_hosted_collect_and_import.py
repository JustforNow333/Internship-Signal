"""PostgreSQL integration tests for the scheduled hosted collection command.

These prove the one-shot ``app.hosted.collect_and_import`` entry point reuses
the existing collection, snapshot-validation, import, matching, and
notification-enqueue behaviour without touching any legacy watcher state.
"""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.hosted import collect_and_import
from app.hosted.catalog import CompanyCatalog
from app.hosted.collect_and_import import main as collect_main
from app.hosted.database import HostedDatabase, normalize_database_url
from app.hosted.match_service import reconcile_user
from app.hosted.models import (
    HostedJob,
    HostedJobImportRun,
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
from psycopg import sql
from sqlalchemy import func, inspect, select, text
from sqlalchemy.engine import make_url

from watcher.collection_snapshot import (
    CollectionBatch,
    collection_config_fingerprint,
)
from watcher.config import load_watchlist
from watcher.sources.base import make_row

BACKEND_DIR = Path(__file__).resolve().parents[1]
NOW = datetime.now(UTC).replace(microsecond=0)


def _psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@pytest.fixture(scope="session")
def collection_postgres_url() -> str:
    base_url = os.getenv("HOSTED_TEST_DATABASE_URL", "").strip()
    if not base_url:
        pytest.skip("HOSTED_TEST_DATABASE_URL is required for hosted PostgreSQL tests")
    parsed = make_url(normalize_database_url(base_url))
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("HOSTED_TEST_DATABASE_URL must use PostgreSQL")
    database_name = f"internship_signal_collection_test_{uuid.uuid4().hex}"
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
    try:
        alembic = Config(str(BACKEND_DIR / "alembic.ini"))
        alembic.set_main_option("sqlalchemy.url", isolated_url.replace("%", "%%"))
        existing_logger = logging.getLogger("hosted.collection.migration.existing")
        existing_logger.disabled = False
        command.upgrade(alembic, "head")
        if existing_logger.disabled:
            pytest.fail("Alembic migration disabled an existing application logger")
        yield isolated_url
    finally:
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
def clean_collection_database(collection_postgres_url: str):
    database = HostedDatabase(collection_postgres_url)
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
def database(collection_postgres_url: str):
    value = HostedDatabase(collection_postgres_url)
    try:
        yield value
    finally:
        value.dispose()


@pytest.fixture(autouse=True)
def hosted_database_url(monkeypatch, collection_postgres_url: str):
    monkeypatch.setenv("HOSTED_DATABASE_URL", collection_postgres_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)


@pytest.fixture(autouse=True)
def forbid_legacy_side_effects(monkeypatch):
    """The hosted path must never reach legacy email or the seen store."""

    def forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden legacy watcher side effect")

    monkeypatch.setattr("watcher.notify.send_digest", forbidden)
    monkeypatch.setattr("watcher.seen_store.SeenStore.mark_emailed", forbidden)
    monkeypatch.setattr("watcher.seen_store.SeenStore.__init__", forbidden)


@pytest.fixture(scope="session")
def watcher_config():
    return load_watchlist()


@pytest.fixture(scope="session")
def watched_company(watcher_config):
    catalog = CompanyCatalog.from_watcher_config(watcher_config)
    return next(company for company in catalog.companies if company.selectable)


class RecordingTransport:
    def __init__(self) -> None:
        self.messages: list[NotificationEmail] = []

    def send(self, message: NotificationEmail) -> DeliveryResult:
        self.messages.append(message)
        return DeliveryResult("sent")


def batch_for(
    watcher_config,
    company_name: str,
    *,
    posting_date: str,
    identifier: str = "1",
    fingerprint: str | None = None,
) -> CollectionBatch:
    row = make_row(
        source="direct",
        source_adapter="workday",
        company=company_name,
        title="Backend Software Engineer Intern",
        location="New York, NY",
        description="Build backend APIs and production services.",
        requirements="Python and SQL",
        source_url=f"https://example.com/jobs/collection-{identifier}",
        date_posted=posting_date,
        internship_type="Summer 2027 Internship",
        extra={"source_requisition_id": f"COLLECT-{identifier}", "active": True},
    )
    return CollectionBatch.create(
        captured_at=NOW,
        collection_config_fingerprint=(
            fingerprint
            if fingerprint is not None
            else collection_config_fingerprint(watcher_config)
        ),
        rows=[row],
        errors=[],
        source_attempts=[],
    )


def collector_for(batch: CollectionBatch):
    def collector(_config):
        return batch

    return collector


def create_user(
    database: HostedDatabase,
    company_id: str | None,
    *,
    email: str = "student@example.com",
    watch_started: datetime | None = None,
    frequency: str = "as_detected",
) -> uuid.UUID:
    with database.session_factory.begin() as db:
        user = User(
            email=email,
            normalized_email=email.casefold(),
            password_hash=hash_password("secure password"),
            email_verified_at=NOW,
            is_active=True,
            created_at=NOW,
            updated_at=NOW,
        )
        db.add(user)
        db.flush()
        db.add(
            UserPreference(
                user_id=user.id,
                role_ids=["software_engineering"],
                preferred_locations=["New York, NY"],
                include_remote=True,
                internship_season="Any season",
                alert_frequency=frequency,
                globally_paused=False,
                include_recent_openings=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        if company_id is not None:
            started = watch_started or (NOW - timedelta(days=2))
            db.add(
                UserCompanyWatch(
                    user_id=user.id,
                    company_id=company_id,
                    paused=False,
                    created_at=started,
                    updated_at=started,
                )
            )
        return user.id


def counts(database: HostedDatabase) -> dict[str, int]:
    with database.session_factory() as db:
        return {
            "jobs": db.scalar(select(func.count()).select_from(HostedJob)),
            "runs": db.scalar(select(func.count()).select_from(HostedJobImportRun)),
            "matches": db.scalar(select(func.count()).select_from(UserJobMatch)),
            "batches": db.scalar(
                select(func.count()).select_from(HostedNotificationBatch)
            ),
            "items": db.scalar(
                select(func.count()).select_from(HostedNotificationItem)
            ),
        }


def test_a_hosted_collection_run_imports_jobs_and_notifies_watching_users(
    database, watcher_config, watched_company, capsys
) -> None:
    create_user(database, watched_company.id)
    batch = batch_for(
        watcher_config,
        watched_company.name,
        posting_date=NOW.date().isoformat(),
    )

    assert collect_main([], collector=collector_for(batch)) == 0

    output = capsys.readouterr().out
    assert "HOSTED-COLLECTION rows=1" in output
    assert "outcome=imported" in output
    assert "inserted=1" in output
    assert counts(database) == {
        "jobs": 1,
        "runs": 1,
        "matches": 1,
        "batches": 1,
        "items": 1,
    }
    with database.session_factory() as db:
        run = db.scalar(select(HostedJobImportRun))
        # Scheduled collection is distinguishable from an operator replay.
        assert run.source_type == "hosted_collection"
        assert (run.status, run.jobs_inserted, run.matches_created) == (
            "succeeded",
            1,
            1,
        )
        item = db.scalar(select(HostedNotificationItem))
        assert item.status == "pending"


def test_a_watchlist_backfill_after_collection_creates_no_notification_work(
    database, watcher_config, watched_company
) -> None:
    # The user is not watching the company when the posting is imported, so the
    # import creates nothing for them.
    user_id = create_user(database, None)
    batch = batch_for(
        watcher_config,
        watched_company.name,
        posting_date=(NOW - timedelta(days=30)).date().isoformat(),
    )
    assert collect_main([], collector=collector_for(batch)) == 0
    assert counts(database)["matches"] == 0

    # Adding the company backfills the dashboard through reconciliation, which
    # never enqueues notification work.
    with database.session_factory.begin() as db:
        db.add(
            UserCompanyWatch(
                user_id=user_id,
                company_id=watched_company.id,
                paused=False,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        db.flush()
        outcome = reconcile_user(db, user_id, now=NOW)

    assert outcome.created == 1
    after = counts(database)
    assert after["matches"] == 1
    assert (after["batches"], after["items"]) == (0, 0)


def test_rerunning_an_identical_collection_is_idempotent(
    database, watcher_config, watched_company, capsys
) -> None:
    create_user(database, watched_company.id)
    batch = batch_for(
        watcher_config,
        watched_company.name,
        posting_date=NOW.date().isoformat(),
    )

    assert collect_main([], collector=collector_for(batch)) == 0
    first = capsys.readouterr().out
    after_first = counts(database)

    # Snapshots are written deterministically, so an identical batch produces
    # the same source fingerprint and the import is a recognised no-op.
    assert collect_main([], collector=collector_for(batch)) == 0
    second = capsys.readouterr().out

    assert "outcome=imported" in first
    assert "outcome=already_imported" in second
    assert counts(database) == after_first
    assert after_first["items"] == 1


def test_a_changed_collection_reruns_without_duplicating_jobs_or_alerts(
    database, watcher_config, watched_company
) -> None:
    create_user(database, watched_company.id)
    posting_date = NOW.date().isoformat()
    assert (
        collect_main(
            [],
            collector=collector_for(
                batch_for(watcher_config, watched_company.name, posting_date=posting_date)
            ),
        )
        == 0
    )
    first = counts(database)

    # A later collection of the same posting is a new source fingerprint but
    # the same job identity, so nothing is duplicated and no alert repeats.
    later = CollectionBatch.create(
        captured_at=NOW + timedelta(hours=1),
        collection_config_fingerprint=collection_config_fingerprint(watcher_config),
        rows=list(
            batch_for(
                watcher_config, watched_company.name, posting_date=posting_date
            ).mutable_rows()
        ),
        errors=[],
        source_attempts=[],
    )
    assert collect_main([], collector=collector_for(later)) == 0

    after = counts(database)
    assert after["runs"] == 2
    assert (after["jobs"], after["matches"]) == (first["jobs"], first["matches"])
    assert (after["batches"], after["items"]) == (first["batches"], first["items"])


def test_invalid_collection_output_does_not_partially_import(
    database, watcher_config, watched_company, capsys
) -> None:
    create_user(database, watched_company.id)
    # A fingerprint that does not match the current collection configuration is
    # rejected by snapshot validation before anything is written.
    batch = batch_for(
        watcher_config,
        watched_company.name,
        posting_date=NOW.date().isoformat(),
        fingerprint="0" * 64,
    )

    assert collect_main([], collector=collector_for(batch)) == 1

    captured = capsys.readouterr()
    assert "invalid_collection_result" in captured.err
    assert "outcome=" not in captured.out
    assert counts(database) == {
        "jobs": 0,
        "runs": 0,
        "matches": 0,
        "batches": 0,
        "items": 0,
    }


def test_a_collection_failure_reports_a_nonzero_status_without_leaking(
    database, capsys
) -> None:
    def failing(_config):
        raise RuntimeError("postgresql://user:secret@private/database")

    assert collect_main([], collector=failing) == 1
    captured = capsys.readouterr()
    assert "hosted_import_unavailable" in captured.err
    assert "secret" not in captured.err and "postgresql" not in captured.err
    assert counts(database)["runs"] == 0


def test_a_missing_hosted_database_url_is_a_distinct_exit_code(
    monkeypatch, capsys
) -> None:
    monkeypatch.delenv("HOSTED_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def unreachable(_config):
        raise AssertionError("collection must not start without a database")

    assert collect_main([], collector=unreachable) == 2
    assert "hosted_database_not_configured" in capsys.readouterr().err


@pytest.mark.parametrize("failing", [False, True])
def test_the_temporary_snapshot_workspace_is_always_removed(
    watcher_config, watched_company, monkeypatch, failing: bool
) -> None:
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(
        collect_and_import.tempfile, "mkdtemp", recording_mkdtemp
    )
    batch = batch_for(
        watcher_config,
        watched_company.name,
        posting_date=NOW.date().isoformat(),
        fingerprint="0" * 64 if failing else None,
    )

    assert collect_main([], collector=collector_for(batch)) == (1 if failing else 0)

    assert len(created) == 1
    # No runtime snapshot survives the run, in the repository or anywhere else.
    assert not created[0].exists()


def test_collection_logging_never_exposes_posting_or_source_detail(
    watcher_config, watched_company, capsys
) -> None:
    batch = batch_for(
        watcher_config,
        watched_company.name,
        posting_date=NOW.date().isoformat(),
    )
    assert collect_main([], collector=collector_for(batch)) == 0
    output = capsys.readouterr().out
    for secret in (
        "Backend Software Engineer Intern",
        "https://example.com/jobs/collection-1",
        "Build backend APIs",
        "COLLECT-1",
    ):
        assert secret not in output


def test_the_notification_worker_runs_independently_after_a_collection(
    database, watcher_config, watched_company, monkeypatch
) -> None:
    from app.hosted import deliver_notifications
    from app.hosted.notification_mail import ResendNotificationTransport
    from app.hosted.notification_worker import WorkerSummary

    create_user(database, watched_company.id)
    batch = batch_for(
        watcher_config,
        watched_company.name,
        posting_date=NOW.date().isoformat(),
    )
    assert collect_main([], collector=collector_for(batch)) == 0

    # Delivery is a separate command over the same database, so a collection
    # failure could never block it.
    transport = RecordingTransport()
    summary = NotificationDeliveryWorker(
        database, transport, "https://internships.example"
    ).run(limit=5)
    assert (summary.sent, len(transport.messages)) == (1, 1)
    with database.session_factory() as db:
        assert db.scalar(select(HostedNotificationItem.status)) == "sent"

    # The delivery CLI still selects Resend when it is configured.
    monkeypatch.setenv("HOSTED_RESEND_API_KEY", "re_scheduled_key")
    monkeypatch.setenv("HOSTED_RESEND_FROM_EMAIL", "alerts@example.com")
    monkeypatch.delenv("HOSTED_SMTP_HOST", raising=False)
    monkeypatch.delenv("HOSTED_SMTP_FROM_EMAIL", raising=False)
    chosen: dict[str, object] = {}

    class StubWorker:
        def __init__(self, _database, transport, _public_frontend_url) -> None:
            chosen["transport"] = transport

        def run(self, *, limit: int) -> WorkerSummary:
            return WorkerSummary()

    monkeypatch.setattr(
        deliver_notifications, "NotificationDeliveryWorker", StubWorker
    )
    assert deliver_notifications.main(["--limit", "5"]) == 0
    assert isinstance(chosen["transport"], ResendNotificationTransport)
