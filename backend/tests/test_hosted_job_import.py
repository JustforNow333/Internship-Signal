from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from app.hosted.catalog import CompanyCatalog, PublicCompany
from app.hosted.database import HostedDatabase, normalize_database_url
from app.hosted.import_snapshot import main as import_snapshot_main
from app.hosted.job_import import (
    FailedImportRetryRequired,
    ImportAlreadyRunning,
    InvalidFinalJobs,
    JobImportService,
    JobUpsertFailed,
)
from app.hosted.job_mapper import map_final_jobs
from app.hosted.models import (
    HostedJob,
    HostedJobImportAttempt,
    HostedJobImportRun,
)
from app.hosted.snapshot_jobs import replay_snapshot_jobs, snapshot_sha256
from psycopg import sql
from sqlalchemy import inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from watcher.collection_snapshot import (
    CollectionBatch,
    CollectionSnapshotError,
    collection_config_fingerprint,
    save_collection_snapshot,
)
from watcher.config import load_watchlist
from watcher.sources.base import make_row

BACKEND_DIR = Path(__file__).resolve().parents[1]


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


def _psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


@pytest.fixture(scope="session")
def job_postgres_url() -> str:
    base_url = os.getenv("HOSTED_TEST_DATABASE_URL", "").strip()
    if not base_url:
        pytest.skip("HOSTED_TEST_DATABASE_URL is required for hosted PostgreSQL tests")
    parsed = make_url(normalize_database_url(base_url))
    if parsed.get_backend_name() != "postgresql":
        pytest.fail("HOSTED_TEST_DATABASE_URL must use PostgreSQL")
    database_name = f"internship_signal_job_test_{uuid.uuid4().hex}"
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
    existing_logger = logging.getLogger("hosted.jobs.migration.existing")
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
def clean_job_database(job_postgres_url: str):
    database = HostedDatabase(job_postgres_url)
    table_names = inspect(database.engine).get_table_names()
    with database.engine.begin() as connection:
        names = [name for name in table_names if name != "alembic_version"]
        if names:
            quoted = ", ".join(f'"{name}"' for name in names)
            connection.exec_driver_sql(
                f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"
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
                id="supported-backstop",
                name="Supported Backstop",
                aliases=(),
                coverage="backstop",
                selectable=True,
            ),
            PublicCompany(
                id="not-selectable",
                name="Not Selectable",
                aliases=(),
                coverage="backstop",
                selectable=False,
            ),
        )
    )


@pytest.fixture
def import_service(job_postgres_url: str, catalog: CompanyCatalog):
    database = HostedDatabase(job_postgres_url)
    clock = MutableClock()
    service = JobImportService(database, catalog, clock=clock)
    try:
        yield service, database, clock
    finally:
        database.dispose()


def final_job(**overrides) -> dict:
    job = {
        "id": "watcher-job-1",
        "company": "Capital One Financial",
        "title": "Backend Software Engineer Intern",
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "description": "Build production APIs and distributed services.",
        "requirements": "Python and SQL",
        "source_url": "https://example.com/jobs/123",
        "date_posted": "2026-08-01",
        "deadline": "2026-09-01",
        "deadline_days_left": 30,
        "internship_type": "Summer 2027 Internship",
        "role_classification": {
            "role": "swe",
            "role_track": "backend",
        },
        "extra": {
            "source": "direct",
            "source_adapter": "workday",
            "source_requisition_id": "R-123",
            "token": "must-not-persist",
            "feed_url": "https://secret.example/?token=hidden",
            "headers": {"Authorization": "secret"},
            "source_details": {
                "workday": {"source_adapter": "workday"},
                "simplify": {"source_adapter": "github_listings"},
            },
        },
    }
    job.update(overrides)
    return job


def import_jobs(service: JobImportService, jobs, fingerprint_character: str, **kwargs):
    return service.import_jobs(
        jobs,
        source_fingerprint=fingerprint_character * 64,
        source_identifier=f"snapshot-{fingerprint_character}.json.gz",
        source_type="collection_snapshot",
        **kwargs,
    )


def test_phase2_migration_creates_postgresql_tables_and_jsonb(
    job_postgres_url: str,
) -> None:
    database = HostedDatabase(job_postgres_url)
    inspector = inspect(database.engine)
    assert {
        "hosted_jobs",
        "hosted_job_import_runs",
        "hosted_job_import_attempts",
    }.issubset(inspector.get_table_names())
    with database.engine.connect() as connection:
        source_metadata_type = connection.exec_driver_sql(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='hosted_jobs' AND column_name='source_metadata'"
        ).scalar_one()
        revision = connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one()
    assert source_metadata_type == "jsonb"
    assert revision == "20260802_0002"
    database.dispose()


def test_insert_maps_alias_role_and_sanitized_provenance(import_service) -> None:
    service, database, clock = import_service
    result = import_jobs(service, [final_job()], "a")
    assert result.outcome == "imported"
    assert result.counters.jobs_inserted == 1
    assert result.counters.matches_created == 0
    with database.session_factory() as db:
        stored = db.scalar(select(HostedJob))
        run = db.scalar(select(HostedJobImportRun))
        attempt = db.scalar(select(HostedJobImportAttempt))
    assert stored.watcher_job_id == "watcher-job-1"
    assert stored.company_id == "capital-one"
    assert stored.company_name == "Capital One"
    assert stored.role_id == "software_engineering"
    assert stored.posting_date == date(2026, 8, 1)
    assert stored.first_seen_at == stored.last_seen_at == clock()
    assert stored.closed_at is None
    assert stored.source_metadata == {
        "adapter": "workday",
        "merged_adapters": ["github_listings", "workday"],
        "requisition_id": "R-123",
        "source_type": "direct",
    }
    serialized = str(stored.source_metadata)
    assert "secret" not in serialized and "token" not in serialized
    assert run.status == attempt.status == "succeeded"
    assert run.matches_created == 0


def test_unchanged_updates_and_open_closed_lifecycle(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    with database.session_factory() as db:
        original = db.scalar(select(HostedJob))
        original_id = original.id
        first_seen = original.first_seen_at
        created_at = original.created_at

    clock.advance(hours=1)
    unchanged = import_jobs(service, [final_job()], "b")
    assert unchanged.counters.jobs_unchanged == 1
    with database.session_factory() as db:
        stored = db.scalar(select(HostedJob))
        assert stored.id == original_id
        assert stored.first_seen_at == first_seen
        assert stored.created_at == created_at
        assert stored.last_seen_at == clock()

    clock.advance(hours=1)
    closed_job = final_job(extra={**final_job()["extra"], "active": False})
    closed = import_jobs(service, [closed_job], "c")
    assert closed.counters.jobs_updated == 1
    with database.session_factory() as db:
        stored = db.scalar(select(HostedJob))
        first_closed_at = stored.closed_at
        assert stored.is_open is False
        assert first_closed_at == clock()

    clock.advance(hours=1)
    repeated = import_jobs(service, [closed_job], "d")
    assert repeated.counters.jobs_unchanged == 1
    with database.session_factory() as db:
        stored = db.scalar(select(HostedJob))
        assert stored.closed_at == first_closed_at

    clock.advance(hours=1)
    reopened = import_jobs(service, [final_job()], "e")
    assert reopened.counters.jobs_updated == 1
    with database.session_factory() as db:
        stored = db.scalar(select(HostedJob))
        assert stored.is_open is True
        assert stored.closed_at is None


def test_existing_job_updates_every_mutable_business_field(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    clock.advance(days=1)
    changed = final_job(
        company="Supported Backstop",
        title="Machine Learning Engineer Intern",
        location="Remote",
        remote_status="Remote",
        description="Train production models.",
        requirements="PyTorch",
        source_url="https://example.org/apply/456",
        date_posted="2026-08-02",
        deadline="rolling",
        deadline_days_left=None,
        role_classification={"role": "ml_ai", "role_track": "ml_ai"},
        extra={
            "source": "github",
            "source_adapter": "github_listings",
            "active": True,
        },
    )
    result = import_jobs(service, [changed], "b")
    assert result.counters.jobs_updated == 1
    with database.session_factory() as db:
        stored = db.scalar(select(HostedJob))
    assert stored.company_id == "supported-backstop"
    assert stored.title == "Machine Learning Engineer Intern"
    assert stored.location == stored.remote_status == "Remote"
    assert stored.role_id == "machine_learning_ai"
    assert stored.description == "Train production models."
    assert stored.requirements == "PyTorch"
    assert stored.application_url == "https://example.org/apply/456"
    assert stored.posting_date == date(2026, 8, 2)
    assert stored.deadline is None
    assert stored.source_metadata == {
        "adapter": "github_listings",
        "source_type": "backstop",
    }


def test_invalid_and_unsupported_jobs_are_skipped_with_exact_counters(
    import_service,
) -> None:
    service, database, _clock = import_service
    jobs = [
        final_job(),
        final_job(id="unsupported", company="Unknown Incorporated"),
        final_job(id="missing-title", title=""),
        final_job(
            id="bad-role",
            role_classification={"role": "non_technical", "role_track": "non_technical"},
        ),
        final_job(id="bad-url", source_url="file:///private/job"),
        final_job(id="bad-date", date_posted="not-a-date"),
    ]
    result = import_jobs(service, jobs, "a")
    assert result.counters == result.counters.__class__(
        jobs_received=6,
        jobs_inserted=1,
        jobs_updated=0,
        jobs_unchanged=0,
        jobs_skipped=5,
    )
    assert result.skipped_reasons == {
        "invalid_application_url": 1,
        "invalid_posting_date": 1,
        "invalid_role": 1,
        "invalid_title": 1,
        "unsupported_company": 1,
    }
    with database.session_factory() as db:
        assert len(db.scalars(select(HostedJob)).all()) == 1


@pytest.mark.parametrize(
    ("track", "role", "expected"),
    [
        ("backend", "swe", "software_engineering"),
        ("ml_ai", "ml_ai", "machine_learning_ai"),
        ("data_science", "data_science", "data_science"),
        ("data_engineering", "data_science", "data_engineering"),
        ("quant_dev", "quant", "quantitative_development"),
        ("technical_product", "product", "product_management"),
        ("firmware", "swe", "hardware_embedded"),
        ("mechanical_manufacturing", "unknown", "other_engineering"),
    ],
)
def test_role_mapping_is_centralized_and_explicit(
    catalog: CompanyCatalog,
    track: str,
    role: str,
    expected: str,
) -> None:
    mapped = map_final_jobs(
        [final_job(role_classification={"role": role, "role_track": track})],
        catalog,
    )
    assert mapped.jobs[0].role_id == expected


def test_structurally_invalid_final_jobs_fail_and_record_no_partial_jobs(
    import_service,
) -> None:
    service, database, _clock = import_service
    duplicate = [final_job(), final_job(title="Different title, same watcher ID")]
    with pytest.raises(InvalidFinalJobs):
        import_jobs(service, duplicate, "a")
    with database.session_factory() as db:
        assert db.scalars(select(HostedJob)).all() == []
        run = db.scalar(select(HostedJobImportRun))
    assert run.status == "failed"
    assert run.failure_summary == "invalid_final_jobs"


def test_jobs_persist_across_database_and_service_recreation(
    job_postgres_url: str,
    catalog: CompanyCatalog,
) -> None:
    clock = MutableClock()
    first_database = HostedDatabase(job_postgres_url)
    first_service = JobImportService(first_database, catalog, clock=clock)
    import_jobs(first_service, [final_job()], "a")
    first_database.dispose()

    clock.advance(hours=1)
    second_database = HostedDatabase(job_postgres_url)
    second_service = JobImportService(second_database, catalog, clock=clock)
    result = import_jobs(second_service, [final_job()], "b")
    with second_database.session_factory() as db:
        stored = db.scalar(select(HostedJob))
    assert result.counters.jobs_unchanged == 1
    assert stored.first_seen_at < stored.last_seen_at
    second_database.dispose()


def test_same_source_is_idempotent_without_reprocessing(import_service) -> None:
    service, database, clock = import_service
    first = import_jobs(service, [final_job()], "a")
    clock.advance(days=3)
    second = import_jobs(service, [final_job(title="Changed but not replayed")], "a")
    assert second.already_imported is True
    assert second.run_id == first.run_id
    assert second.counters == first.counters
    with database.session_factory() as db:
        jobs = db.scalars(select(HostedJob)).all()
        runs = db.scalars(select(HostedJobImportRun)).all()
        attempts = db.scalars(select(HostedJobImportAttempt)).all()
    assert len(jobs) == len(runs) == len(attempts) == 1
    assert jobs[0].title == final_job()["title"]
    assert jobs[0].last_seen_at != clock()


def test_concurrent_duplicate_is_rejected_while_first_import_runs(
    import_service,
) -> None:
    service, _database, _clock = import_service
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []
    original_upsert = service._upsert_job

    def blocking_upsert(db, mapped, observed_at):
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test synchronization timeout")
        return original_upsert(db, mapped, observed_at)

    service._upsert_job = blocking_upsert

    def first_import() -> None:
        try:
            import_jobs(service, [final_job()], "a")
        except Exception as exc:  # noqa: BLE001 - capture failures across thread
            errors.append(exc)

    thread = threading.Thread(target=first_import)
    thread.start()
    assert entered.wait(timeout=10)
    try:
        with pytest.raises(ImportAlreadyRunning):
            import_jobs(service, [final_job()], "a")
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []


def test_concurrent_failed_retries_create_only_one_new_attempt(import_service) -> None:
    service, database, clock = import_service

    def fail_upsert(*_args, **_kwargs):
        raise RuntimeError("forced failure")

    service._upsert_job = fail_upsert
    with pytest.raises(JobUpsertFailed):
        import_jobs(service, [final_job()], "a")

    first = JobImportService(database, service.catalog, clock=clock)
    second = JobImportService(database, service.catalog, clock=clock)
    entered = threading.Event()
    release = threading.Event()
    first_claim = first._claim_existing
    outcomes: list[object] = []

    def blocking_claim(db, run, *, observed_at, retry_failed):
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test synchronization timeout")
        return first_claim(
            db,
            run,
            observed_at=observed_at,
            retry_failed=retry_failed,
        )

    first._claim_existing = blocking_claim

    def claim(service_to_use) -> None:
        try:
            outcomes.append(
                service_to_use._claim_source(
                    fingerprint="a" * 64,
                    identifier="snapshot-a.json.gz",
                    source_type="collection_snapshot",
                    observed_at=clock(),
                    retry_failed=True,
                )
            )
        except Exception as exc:  # noqa: BLE001 - capture failures across thread
            outcomes.append(exc)

    first_thread = threading.Thread(target=claim, args=(first,))
    second_thread = threading.Thread(target=claim, args=(second,))
    first_thread.start()
    assert entered.wait(timeout=10)
    second_thread.start()
    release.set()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)
    assert not first_thread.is_alive() and not second_thread.is_alive()
    assert sum(isinstance(value, ImportAlreadyRunning) for value in outcomes) == 1
    with database.session_factory() as db:
        attempts = db.scalars(
            select(HostedJobImportAttempt).order_by(
                HostedJobImportAttempt.attempt_number
            )
        ).all()
    assert [(item.attempt_number, item.status) for item in attempts] == [
        (1, "failed"),
        (2, "running"),
    ]


def test_concurrent_distinct_sources_insert_one_watcher_job(import_service) -> None:
    service, database, clock = import_service
    first = JobImportService(database, service.catalog, clock=clock)
    second = JobImportService(database, service.catalog, clock=clock)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    for item in (first, second):
        original = item._upsert_job

        def synchronized_upsert(db, mapped, observed_at, *, delegate=original):
            barrier.wait(timeout=10)
            return delegate(db, mapped, observed_at)

        item._upsert_job = synchronized_upsert

    def run_import(service_to_use, fingerprint_character: str) -> None:
        try:
            outcomes.append(
                import_jobs(
                    service_to_use,
                    [final_job()],
                    fingerprint_character,
                )
            )
        except Exception as exc:  # noqa: BLE001 - capture failures across thread
            outcomes.append(exc)

    threads = [
        threading.Thread(target=run_import, args=(first, "a")),
        threading.Thread(target=run_import, args=(second, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert all(not isinstance(value, Exception) for value in outcomes)
    assert sorted(
        (
            value.counters.jobs_inserted,
            value.counters.jobs_unchanged,
        )
        for value in outcomes
    ) == [(0, 1), (1, 0)]
    with database.session_factory() as db:
        assert len(db.scalars(select(HostedJob)).all()) == 1
        runs = db.scalars(select(HostedJobImportRun)).all()
    assert len(runs) == 2 and all(run.status == "succeeded" for run in runs)


def test_failed_import_rolls_back_jobs_and_explicit_retry_preserves_attempts(
    import_service,
) -> None:
    service, database, clock = import_service
    jobs = [final_job(), final_job(id="watcher-job-2", title="Frontend Intern")]
    original_upsert = service._upsert_job
    calls = 0

    def failing_upsert(db, mapped, observed_at):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("postgresql://user:secret@private/traceback")
        return original_upsert(db, mapped, observed_at)

    service._upsert_job = failing_upsert
    with pytest.raises(JobUpsertFailed):
        import_jobs(service, jobs, "a")
    with database.session_factory() as db:
        assert db.scalars(select(HostedJob)).all() == []
        run = db.scalar(select(HostedJobImportRun))
        attempt = db.scalar(select(HostedJobImportAttempt))
    assert run.status == attempt.status == "failed"
    assert run.completed_at is not None
    assert run.failure_summary == attempt.failure_summary == "job_upsert_failed"
    assert "secret" not in run.failure_summary
    assert run.jobs_inserted == run.jobs_updated == run.jobs_unchanged == 0
    with pytest.raises(FailedImportRetryRequired):
        import_jobs(service, jobs, "a")

    clock.advance(minutes=5)
    retry_service = JobImportService(database, service.catalog, clock=clock)
    retried = import_jobs(retry_service, jobs, "a", retry_failed=True)
    assert retried.counters.jobs_inserted == 2
    with database.session_factory() as db:
        run = db.scalar(select(HostedJobImportRun))
        attempts = db.scalars(
            select(HostedJobImportAttempt).order_by(
                HostedJobImportAttempt.attempt_number
            )
        ).all()
        assert len(db.scalars(select(HostedJob)).all()) == 2
    assert run.status == "succeeded" and run.failure_summary is None
    assert [(item.attempt_number, item.status) for item in attempts] == [
        (1, "failed"),
        (2, "succeeded"),
    ]
    assert attempts[0].failure_summary == "job_upsert_failed"


def test_database_constraints_reject_duplicate_ids_invalid_runs_and_orphan_attempts(
    import_service,
) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    with database.session_factory() as db:
        original = db.scalar(select(HostedJob))
        duplicate = HostedJob(
            watcher_job_id=original.watcher_job_id,
            company_id=original.company_id,
            company_name=original.company_name,
            title=original.title,
            location=original.location,
            remote_status=original.remote_status,
            role_id=original.role_id,
            description="",
            requirements="",
            application_url=None,
            posting_date=None,
            deadline=None,
            is_open=True,
            first_seen_at=clock(),
            last_seen_at=clock(),
            source_metadata={},
        )
        db.add(duplicate)
        with pytest.raises(IntegrityError):
            db.commit()

    with database.session_factory() as db:
        db.add(
            HostedJobImportRun(
                source_fingerprint="a" * 64,
                source_identifier="duplicate.json.gz",
                source_type="collection_snapshot",
                started_at=clock(),
                completed_at=clock(),
                status="succeeded",
                jobs_received=0,
                jobs_inserted=0,
                jobs_updated=0,
                jobs_unchanged=0,
                jobs_skipped=0,
                matches_created=0,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    with database.session_factory() as db:
        db.add(
            HostedJobImportRun(
                source_fingerprint="b" * 64,
                source_identifier="invalid-counters.json.gz",
                source_type="collection_snapshot",
                started_at=clock(),
                completed_at=clock(),
                status="succeeded",
                jobs_received=-1,
                jobs_inserted=0,
                jobs_updated=0,
                jobs_unchanged=0,
                jobs_skipped=0,
                matches_created=1,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    with database.session_factory() as db:
        db.add(
            HostedJobImportRun(
                source_fingerprint="c" * 64,
                source_identifier="invalid-status.json.gz",
                source_type="collection_snapshot",
                started_at=clock(),
                completed_at=clock(),
                status="complete",
                jobs_received=0,
                jobs_inserted=0,
                jobs_updated=0,
                jobs_unchanged=0,
                jobs_skipped=0,
                matches_created=0,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()

    with database.session_factory() as db:
        db.add(
            HostedJobImportAttempt(
                import_run_id=uuid.uuid4(),
                attempt_number=1,
                started_at=clock(),
                status="running",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_snapshot_cli_uses_official_replay_and_is_idempotent_without_side_effects(
    job_postgres_url: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config = load_watchlist()
    row = make_row(
        source="direct",
        source_adapter="workday",
        company="Capital One",
        title="Backend Software Engineer Intern",
        location="New York, NY",
        description="Build backend APIs and production services.",
        requirements="Python and SQL",
        source_url="https://example.com/jobs/snapshot-1",
        date_posted="2026-08-01",
        internship_type="Summer 2027 Internship",
        extra={"source_requisition_id": "SNAP-1", "active": True},
    )
    batch = CollectionBatch.create(
        captured_at=datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        collection_config_fingerprint=collection_config_fingerprint(config),
        rows=[row],
        errors=[],
        source_attempts=[],
    )
    snapshot = tmp_path / "official.json.gz"
    save_collection_snapshot(batch, snapshot)
    watcher_state = tmp_path / "seen.sqlite"
    watcher_state.write_bytes(b"unchanged-watcher-state")
    state_before = watcher_state.read_bytes()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden operational side effect")

    monkeypatch.setenv("HOSTED_DATABASE_URL", job_postgres_url)
    monkeypatch.setattr("watcher.run.collect_batch", forbidden)
    monkeypatch.setattr("watcher.notify.send_digest", forbidden)
    monkeypatch.setattr("watcher.seen_store.SeenStore.mark_emailed", forbidden)

    assert import_snapshot_main(["--snapshot", str(snapshot)]) == 0
    first_output = capsys.readouterr().out
    assert "outcome=imported" in first_output
    assert "inserted=1" in first_output and "matches_created=0" in first_output
    assert import_snapshot_main(["--snapshot", str(snapshot)]) == 0
    second_output = capsys.readouterr().out
    assert "outcome=already_imported" in second_output
    assert watcher_state.read_bytes() == state_before

    database = HostedDatabase(job_postgres_url)
    with database.session_factory() as db:
        assert len(db.scalars(select(HostedJob)).all()) == 1
        assert len(db.scalars(select(HostedJobImportRun)).all()) == 1
        run = db.scalar(select(HostedJobImportRun))
    assert run.source_fingerprint == snapshot_sha256(snapshot)
    assert run.source_identifier == snapshot.name
    assert run.matches_created == 0
    database.dispose()


def test_snapshot_cli_fails_cleanly_on_corrupt_input(
    job_postgres_url: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    corrupt = tmp_path / "private-secret-name.json.gz"
    corrupt.write_bytes(b"not gzip")
    monkeypatch.setenv("HOSTED_DATABASE_URL", job_postgres_url)
    assert import_snapshot_main(["--snapshot", str(corrupt)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid_collection_snapshot" in captured.err
    assert str(tmp_path) not in captured.err


def test_snapshot_cli_sanitizes_unexpected_failures(
    job_postgres_url: str,
    monkeypatch,
    capsys,
) -> None:
    class UnexpectedFailure(Exception):
        """Unexpected orchestration failure used at the CLI safety boundary."""

    def unexpected_failure(*_args, **_kwargs):
        raise UnexpectedFailure("postgresql://user:secret@private/database")

    monkeypatch.setenv("HOSTED_DATABASE_URL", job_postgres_url)
    monkeypatch.setattr(
        "app.hosted.import_snapshot.replay_snapshot_jobs",
        unexpected_failure,
    )
    assert import_snapshot_main(["--snapshot", "safe.json.gz"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "hosted_import_unavailable" in captured.err
    assert "secret" not in captured.err and "postgresql" not in captured.err


def test_snapshot_replay_calls_loader_and_analysis_with_captured_date(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "spied.json.gz"
    snapshot.write_bytes(b"immutable-test-bytes")
    config = load_watchlist()
    batch = CollectionBatch.create(
        captured_at=datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        collection_config_fingerprint=collection_config_fingerprint(config),
        rows=[],
        errors=[],
        source_attempts=[],
    )
    calls: list[object] = []

    def loader(path):
        calls.append(("loader", Path(path).name))
        return batch

    def analyzer(rows, *, today):
        calls.append(("analyzer", rows, today))
        return []

    replayed = replay_snapshot_jobs(snapshot, loader=loader, analyzer=analyzer)
    assert calls == [
        ("loader", "spied.json.gz"),
        ("analyzer", [], date(2026, 8, 2)),
    ]
    assert replayed.source_fingerprint == snapshot_sha256(snapshot)


def test_snapshot_replay_requires_explicit_config_mismatch_override(
    tmp_path: Path,
) -> None:
    batch = CollectionBatch.create(
        captured_at=datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        collection_config_fingerprint="0" * 64,
        rows=[],
        errors=[],
        source_attempts=[],
    )
    snapshot = tmp_path / "mismatch.json.gz"
    save_collection_snapshot(batch, snapshot)
    with pytest.raises(CollectionSnapshotError):
        replay_snapshot_jobs(snapshot)
    allowed = replay_snapshot_jobs(
        snapshot,
        allow_collection_config_mismatch=True,
    )
    assert allowed.jobs == ()
