from __future__ import annotations

import gzip
import hashlib
import logging
import os
import threading
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
    InvalidImportSource,
    JobImportService,
    JobUpsertFailed,
)
from app.hosted.job_mapper import FinalJobsStructureError, map_final_jobs
from app.hosted.models import (
    HostedJob,
    HostedJobImportAttempt,
    HostedJobImportRun,
)
from app.hosted.snapshot_jobs import replay_snapshot_jobs, snapshot_sha256
from psycopg import sql
from sqlalchemy import inspect, select, text
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
    # The database exists from here on, so its teardown has to run even when
    # the migration or drift check below raises during fixture setup;
    # otherwise a failed run leaks the scratch database.
    try:
        alembic = Config(str(BACKEND_DIR / "alembic.ini"))
        alembic.set_main_option("sqlalchemy.url", isolated_url.replace("%", "%%"))
        existing_logger = logging.getLogger("hosted.jobs.migration.existing")
        existing_logger.disabled = False
        command.upgrade(alembic, "head")
        if existing_logger.disabled:
            pytest.fail("Alembic migration disabled an existing application logger")
        command.check(alembic)
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


def backstop_job(**overrides) -> dict:
    """The same posting as ``final_job`` seen only through a GitHub backstop."""

    values = {
        "description": "",
        "requirements": "",
        "source_url": "https://simplify.example/listing/123",
        "date_posted": "2026-07-15",
        "deadline": "",
        "deadline_days_left": None,
        "location": "New York",
        "remote_status": "",
        "extra": {
            "source": "github",
            "source_adapter": "github_listings",
            "source_id": "listing-9",
            "active": True,
        },
    }
    values.update(overrides)
    return final_job(**values)


def closed_listing(**overrides) -> dict:
    """A GitHub markdown row explicitly marked closed (the lock emoji)."""

    values = {
        "extra": {
            "source": "github",
            "source_adapter": "github_markdown_table",
            "active": False,
            "closed": True,
        },
    }
    values.update(overrides)
    return backstop_job(**values)


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
        "hosted_user_job_matches",
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
    assert revision == "20261025_0010"
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


def test_mapper_preserves_multiline_posting_text(catalog: CompanyCatalog) -> None:
    mapped = map_final_jobs(
        [
            final_job(
                description="Build APIs.\nShip reliable services.",
                requirements="Python\r\nSQL\tPostgreSQL",
            )
        ],
        catalog,
    )

    assert mapped.skipped_reasons == {}
    assert mapped.jobs[0].description == "Build APIs.\nShip reliable services."
    assert mapped.jobs[0].requirements == "Python\r\nSQL\tPostgreSQL"


def test_mapper_still_rejects_unsafe_posting_text_controls(
    catalog: CompanyCatalog,
) -> None:
    mapped = map_final_jobs(
        [final_job(description="Build APIs.\x00hidden payload")],
        catalog,
    )

    assert mapped.jobs == ()
    assert mapped.skipped_reasons == {"invalid_description": 1}


@pytest.mark.parametrize(
    "relative_date",
    ["Posted Today", "Posted Yesterday", "Posted 2 Days Ago", "Posted 30+ Days Ago"],
)
def test_mapper_keeps_workday_relative_posting_dates_as_unknown(
    catalog: CompanyCatalog,
    relative_date: str,
) -> None:
    mapped = map_final_jobs([final_job(date_posted=relative_date)], catalog)

    assert mapped.skipped_reasons == {}
    assert mapped.jobs[0].posting_date is None


def test_catalog_resolution_matches_watcher_company_normalization(
    catalog: CompanyCatalog,
) -> None:
    resolved = catalog.resolve("Capital One, Inc.")

    assert resolved is not None
    assert resolved.id == "capital-one"


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


def test_out_of_order_observation_is_rejected_without_rewinding_last_seen(
    import_service,
) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    clock.advance(hours=2)
    import_jobs(service, [final_job()], "b")
    latest_observation = clock()

    clock.value -= timedelta(hours=1)
    with pytest.raises(InvalidImportSource):
        import_jobs(service, [final_job()], "c")

    with database.session_factory() as db:
        stored = db.scalar(select(HostedJob))
        failed_run = db.scalar(
            select(HostedJobImportRun).where(
                HostedJobImportRun.source_fingerprint == "c" * 64
            )
        )
    assert stored.last_seen_at == latest_observation
    assert failed_run.status == "failed"
    assert failed_run.failure_summary == "invalid_import_source"


def test_existing_job_updates_every_mutable_business_field(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [backstop_job()], "a")
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


def test_mapper_applies_career_level_scope_before_role_mapping(
    catalog: CompanyCatalog,
) -> None:
    mapped = map_final_jobs(
        [
            final_job(id="supported-intern"),
            final_job(
                id="full-time-supported",
                title="Backend Software Engineer",
                internship_type="FullTime",
            ),
            final_job(
                id="full-time-unmappable",
                title="Business Operations Associate",
                internship_type="FullTime",
                role_classification={"role": "unknown", "role_track": "unknown"},
            ),
            final_job(
                id="unmappable-intern",
                title="Unclassified Internship",
                role_classification={"role": "unknown", "role_track": "unknown"},
            ),
        ],
        catalog,
    )

    assert [job.watcher_job_id for job in mapped.jobs] == ["supported-intern"]
    assert mapped.jobs[0].career_level == "internship"
    assert mapped.skipped_reasons == {"invalid_role": 1, "career_level_unknown": 2}


def test_closed_internship_can_map_for_lifecycle_updates(
    catalog: CompanyCatalog,
) -> None:
    mapped = map_final_jobs(
        [final_job(extra={**final_job()["extra"], "active": False})],
        catalog,
    )

    assert mapped.skipped_reasons == {}
    assert mapped.jobs[0].is_open is False


def test_generic_full_time_title_cannot_enter_hosted_jobs(import_service) -> None:
    service, database, _clock = import_service
    result = import_jobs(
        service,
        [
            final_job(
                title="Backend Software Engineer",
                internship_type="FullTime",
            )
        ],
        "e",
    )

    assert result.counters.jobs_inserted == 0
    assert result.counters.jobs_skipped == 1
    assert result.skipped_reasons == {"career_level_unknown": 1}
    with database.session_factory() as db:
        assert db.scalar(select(HostedJob)) is None


@pytest.mark.parametrize(
    ("track", "role", "expected"),
    [
        ("backend", "swe", "software_engineering"),
        ("ml_ai", "ml_ai", "machine_learning_ai"),
        ("data_science", "data_science", "data_science"),
        ("data_engineering", "data_science", "data_engineering"),
        ("quant_dev", "quant", "quantitative_development"),
        ("quant_research", "quant", "quantitative_trading_research"),
        ("technical_product", "product", "product_management"),
        ("firmware", "swe", "hardware_embedded"),
        ("it_support", "it", "other_engineering"),
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
    duplicate = [
        final_job(),
        final_job(
            title="private-title-must-not-leak",
            description="private-description-must-not-leak",
            source_url="https://private.example/?token=must-not-leak",
            extra={"raw_private_provenance": "must-not-leak"},
        ),
    ]
    with pytest.raises(
        FinalJobsStructureError,
        match="^duplicate_watcher_job_id$",
    ):
        map_final_jobs(duplicate, service.catalog)
    with pytest.raises(InvalidFinalJobs):
        import_jobs(service, duplicate, "a")
    with database.session_factory() as db:
        assert db.scalars(select(HostedJob)).all() == []
        run = db.scalar(select(HostedJobImportRun))
        attempt = db.scalar(select(HostedJobImportAttempt))
    assert run.status == "failed"
    assert run.failure_summary == attempt.failure_summary == (
        "invalid_final_jobs:duplicate_watcher_job_id"
    )
    for forbidden in (
        "private-title",
        "private-description",
        "private.example",
        "must-not-leak",
    ):
        assert forbidden not in run.failure_summary


def test_unknown_structural_reason_is_not_exposed() -> None:
    error = FinalJobsStructureError(
        "private-title https://private.example/?token=must-not-leak"
    )

    assert error.reason == str(error) == "invalid_structure"


def test_snapshot_cli_keeps_structural_failure_output_public_and_bounded(
    job_postgres_url: str,
    monkeypatch,
    capsys,
) -> None:
    duplicate = (
        final_job(),
        final_job(
            description="private-description-must-not-leak",
            source_url="https://private.example/?token=must-not-leak",
        ),
    )
    replayed = SimpleNamespace(
        jobs=duplicate,
        config=load_watchlist(),
        source_fingerprint="e" * 64,
        source_identifier="bounded.json.gz",
    )
    monkeypatch.setenv("HOSTED_DATABASE_URL", job_postgres_url)
    monkeypatch.setattr(
        "app.hosted.import_snapshot.replay_snapshot_jobs",
        lambda *_args, **_kwargs: replayed,
    )

    assert import_snapshot_main(["--snapshot", "bounded.json.gz"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Snapshot import failed: invalid_final_jobs\n"

    database = HostedDatabase(job_postgres_url)
    with database.session_factory() as db:
        run = db.scalar(select(HostedJobImportRun))
        attempt = db.scalar(select(HostedJobImportAttempt))
    database.dispose()
    assert run.failure_summary == attempt.failure_summary == (
        "invalid_final_jobs:duplicate_watcher_job_id"
    )
    assert len(run.failure_summary) < 100
    assert "private" not in run.failure_summary
    assert "token" not in run.failure_summary


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

    def blocking_upsert(db, mapped, observed_at, **kwargs):
        entered.set()
        if not release.wait(timeout=10):
            raise RuntimeError("test synchronization timeout")
        return original_upsert(db, mapped, observed_at, **kwargs)

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

        def synchronized_upsert(db, mapped, observed_at, *, delegate=original, **kwargs):
            barrier.wait(timeout=10)
            return delegate(db, mapped, observed_at, **kwargs)

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
    monkeypatch.setattr("watcher.collection.collect_batch", forbidden)
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


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"not gzip", id="not_gzip"),
        # A gzip header around an undecodable deflate stream (zlib.error).
        pytest.param(
            gzip.compress(b"{}", mtime=0)[:10]
            + b"\xff" * 32
            + gzip.compress(b"{}", mtime=0)[-8:],
            id="corrupt_deflate_body",
        ),
        pytest.param(
            gzip.compress(b"[" * 200_000 + b"]" * 200_000), id="too_deeply_nested"
        ),
    ],
)
def test_snapshot_cli_fails_cleanly_on_corrupt_input(
    job_postgres_url: str,
    tmp_path: Path,
    monkeypatch,
    capsys,
    payload: bytes,
) -> None:
    corrupt = tmp_path / "private-secret-name.json.gz"
    corrupt.write_bytes(payload)
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


def test_mapper_persists_selectable_career_levels_and_skips_the_rest(
    catalog: CompanyCatalog,
) -> None:
    full_time = {"internship_type": "FullTime"}
    mapped = map_final_jobs(
        [
            final_job(id="intern"),
            final_job(id="new-grad", title="Software Engineer, New Grad", **full_time),
            final_job(id="junior", title="Jr. Backend Software Engineer", **full_time),
            final_job(id="senior", title="Senior Backend Software Engineer", **full_time),
            final_job(id="staff", title="Staff Software Engineer", **full_time),
            final_job(id="mid", title="Software Engineer II", **full_time),
            final_job(id="ladder", title="Senior Associate, Software Engineer", **full_time),
            final_job(id="generic", title="Backend Software Engineer", **full_time),
            final_job(id="manager", title="Engineering Manager", **full_time),
            final_job(
                id="conflict",
                title="2026 New Grad Software Engineer Intern",
            ),
        ],
        catalog,
    )

    assert {job.watcher_job_id: job.career_level for job in mapped.jobs} == {
        "intern": "internship",
        "new-grad": "new_grad_junior",
        "junior": "new_grad_junior",
        "senior": "senior_plus",
        "staff": "senior_plus",
    }
    assert mapped.skipped_reasons == {
        "career_level_mid_level": 2,
        "career_level_unknown": 3,
    }
    assert all(
        job.business_values()["career_level"] == job.career_level
        for job in mapped.jobs
    )


def test_career_level_ignores_posting_text(catalog: CompanyCatalog) -> None:
    mapped = map_final_jobs(
        [
            final_job(
                title="Backend Software Engineer",
                internship_type="FullTime",
                description="Senior staff role. New grads welcome.",
                requirements="8+ years of experience",
            )
        ],
        catalog,
    )
    assert mapped.jobs == ()
    assert mapped.skipped_reasons == {"career_level_unknown": 1}


def test_other_engineering_stays_internship_only(catalog: CompanyCatalog) -> None:
    other = {"role": "it", "role_track": "it_support"}
    mapped = map_final_jobs(
        [
            final_job(id="intern", role_classification=other),
            final_job(
                id="senior",
                title="Senior Support Engineer",
                internship_type="FullTime",
                role_classification=other,
            ),
        ],
        catalog,
    )
    assert [(job.watcher_job_id, job.role_id) for job in mapped.jobs] == [
        ("intern", "other_engineering")
    ]
    assert mapped.skipped_reasons == {"invalid_role": 1}


def test_a_career_level_change_is_a_meaningful_update(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    clock.advance(hours=1)
    result = import_jobs(
        service,
        [
            final_job(
                title="Senior Backend Software Engineer",
                internship_type="FullTime",
            )
        ],
        "b",
    )
    assert (result.counters.jobs_updated, result.counters.jobs_unchanged) == (1, 0)
    with database.session_factory() as db:
        job = db.scalar(select(HostedJob))
    assert job.career_level == "senior_plus"
    assert job.first_seen_in_backfill is False


def test_snapshot_cli_accepts_the_explicit_backfill_flag() -> None:
    from app.hosted.import_snapshot import build_parser

    assert build_parser().parse_args(["--snapshot", "x"]).career_stage_backfill is False
    assert (
        build_parser()
        .parse_args(["--snapshot", "x", "--career-stage-backfill"])
        .career_stage_backfill
        is True
    )


# --- source precedence -------------------------------------------------------

_PROTECTED_JOB_FIELDS = (
    "company_id",
    "company_name",
    "title",
    "location",
    "remote_status",
    "role_id",
    "career_level",
    "description",
    "requirements",
    "application_url",
    "posting_date",
    "deadline",
    "is_open",
    "closed_at",
    "source_metadata",
    "first_seen_at",
    "first_seen_in_backfill",
    "created_at",
)


def _stored_job(database: HostedDatabase) -> HostedJob:
    with database.session_factory() as db:
        jobs = db.scalars(select(HostedJob)).all()
    assert len(jobs) == 1
    return jobs[0]


def _protected_values(job: HostedJob) -> dict[str, object]:
    return {field: getattr(job, field) for field in _PROTECTED_JOB_FIELDS}


def test_backstop_observation_never_overwrites_a_direct_job(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    direct = _stored_job(database)
    assert direct.source_metadata["source_type"] == "direct"
    assert direct.source_metadata["requisition_id"] == "R-123"
    before = _protected_values(direct)

    clock.advance(hours=1)
    result = import_jobs(service, [backstop_job()], "b")

    assert result.counters.jobs_unchanged == 1
    assert result.counters.jobs_updated == result.counters.jobs_inserted == 0
    stored = _stored_job(database)
    assert stored.id == direct.id
    assert _protected_values(stored) == before
    assert stored.description == "Build production APIs and distributed services."
    assert stored.application_url == "https://example.com/jobs/123"
    assert stored.source_metadata == {
        "adapter": "workday",
        "merged_adapters": ["github_listings", "workday"],
        "requisition_id": "R-123",
        "source_type": "direct",
    }
    # The backstop still confirms the posting is listed.
    assert stored.last_seen_at == clock()


def test_direct_source_outage_keeps_direct_data_until_the_direct_source_returns(
    import_service,
) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    before = _protected_values(_stored_job(database))

    for marker in "bcd":
        clock.advance(hours=1)
        outage = import_jobs(service, [backstop_job()], marker)
        assert outage.counters.jobs_unchanged == 1
        stored = _stored_job(database)
        assert _protected_values(stored) == before
        assert stored.last_seen_at == clock()

    clock.advance(hours=1)
    recovered = final_job(
        description="Updated responsibilities from the direct source.",
        extra={**final_job()["extra"], "source_requisition_id": "R-123-B"},
    )
    result = import_jobs(service, [recovered], "e")
    assert result.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert stored.description == "Updated responsibilities from the direct source."
    assert stored.source_metadata["requisition_id"] == "R-123-B"
    assert stored.source_metadata["source_type"] == "direct"


def test_backstop_job_is_upgraded_by_direct_source_data(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [backstop_job()], "a")
    backstop = _stored_job(database)
    assert backstop.source_metadata == {
        "adapter": "github_listings",
        "source_type": "backstop",
    }

    clock.advance(hours=1)
    result = import_jobs(service, [final_job()], "b")

    assert result.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert stored.id == backstop.id
    assert stored.first_seen_at == backstop.first_seen_at
    assert stored.description == "Build production APIs and distributed services."
    assert stored.requirements == "Python and SQL"
    assert stored.application_url == "https://example.com/jobs/123"
    assert stored.posting_date == date(2026, 8, 1)
    assert stored.deadline == date(2026, 9, 1)
    assert stored.source_metadata["source_type"] == "direct"
    assert stored.source_metadata["requisition_id"] == "R-123"


def test_backstop_only_job_keeps_updating_from_the_backstop(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [backstop_job()], "a")

    clock.advance(hours=1)
    result = import_jobs(
        service,
        [
            backstop_job(
                description="Listing now includes a summary.",
                source_url="https://simplify.example/listing/123-v2",
                date_posted="2026-07-20",
            )
        ],
        "b",
    )

    assert result.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert stored.description == "Listing now includes a summary."
    assert stored.application_url == "https://simplify.example/listing/123-v2"
    assert stored.posting_date == date(2026, 7, 20)
    assert stored.source_metadata["source_type"] == "backstop"


def test_direct_source_changes_to_a_direct_job_still_apply(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")

    clock.advance(hours=1)
    changed = final_job(
        location="Remote",
        description="",
        source_url="https://example.com/jobs/123?v=2",
        deadline="2026-09-15",
        extra={"source": "direct", "source_adapter": "greenhouse"},
    )
    result = import_jobs(service, [changed], "b")

    assert result.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert stored.location == "Remote"
    assert stored.description == ""
    assert stored.application_url == "https://example.com/jobs/123?v=2"
    assert stored.deadline == date(2026, 9, 15)
    assert stored.source_metadata == {"adapter": "greenhouse", "source_type": "direct"}


def test_backstop_may_close_but_never_reopen_a_direct_job(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    before = _protected_values(_stored_job(database))

    clock.advance(hours=1)
    closed = import_jobs(
        service,
        [closed_listing(source_url="https://example.com/jobs/123?utm_source=github")],
        "b",
    )
    assert closed.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert stored.is_open is False
    assert stored.closed_at == clock()
    expected = {**before, "is_open": False, "closed_at": clock()}
    assert _protected_values(stored) == expected

    clock.advance(hours=1)
    reopened_listing = import_jobs(
        service, [backstop_job(source_url="https://example.com/jobs/123")], "c"
    )
    assert reopened_listing.counters.jobs_unchanged == 1
    assert _protected_values(_stored_job(database)) == expected

    clock.advance(hours=1)
    reopened = import_jobs(service, [final_job()], "d")
    assert reopened.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert stored.is_open is True
    assert stored.closed_at is None


def test_job_without_stored_direct_provenance_is_updated_normally(
    import_service,
) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job(extra={"source_requisition_id": "R-123"})], "a")
    assert _stored_job(database).source_metadata == {"requisition_id": "R-123"}

    clock.advance(hours=1)
    result = import_jobs(service, [backstop_job()], "b")

    assert result.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert stored.description == ""
    assert stored.source_metadata["source_type"] == "backstop"


def test_repeated_backstop_imports_over_a_direct_job_are_idempotent(
    import_service,
) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    clock.advance(hours=1)
    first = import_jobs(service, [backstop_job()], "b")
    seen_at = clock()
    before = _protected_values(_stored_job(database))

    clock.advance(hours=1)
    repeated = import_jobs(service, [backstop_job()], "b")

    assert repeated.already_imported
    assert repeated.run_id == first.run_id
    stored = _stored_job(database)
    assert _protected_values(stored) == before
    assert stored.last_seen_at == seen_at


def test_failed_import_rolls_back_a_protected_backstop_observation(
    import_service,
) -> None:
    service, database, clock = import_service
    import_jobs(service, [final_job()], "a")
    before = _stored_job(database)
    original_upsert = service._upsert_job
    calls = 0

    def failing_upsert(db, mapped, observed_at, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fail after the protected observation")
        return original_upsert(db, mapped, observed_at, **kwargs)

    service._upsert_job = failing_upsert
    clock.advance(hours=1)
    with pytest.raises(JobUpsertFailed):
        import_jobs(
            service,
            [backstop_job(), final_job(id="watcher-job-2", title="Frontend Intern")],
            "b",
        )

    assert calls == 2
    stored = _stored_job(database)
    assert _protected_values(stored) == _protected_values(before)
    assert stored.last_seen_at == before.last_seen_at
    assert stored.updated_at == before.updated_at



# --- backstop closure authority over direct jobs -------------------------------

_GREENHOUSE_DIRECT_URL = "https://job-boards.greenhouse.io/acme/jobs/4455"
_ORACLE_AMEX_DIRECT_URL = (
    "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
    "CX_1/job/25012345"
)


def _direct_at(url: str | None, **overrides) -> dict:
    return final_job(source_url=url, **overrides)


def _assert_still_open_and_protected(database, before, observed_at) -> None:
    stored = _stored_job(database)
    assert _protected_values(stored) == before
    assert stored.is_open is True
    assert stored.closed_at is None
    assert stored.last_seen_at == observed_at


def test_old_closed_listing_with_same_title_and_city_cannot_close_a_live_requisition(
    import_service,
) -> None:
    service, database, clock = import_service
    import_jobs(service, [_direct_at(_GREENHOUSE_DIRECT_URL)], "a")
    before = _protected_values(_stored_job(database))

    clock.advance(hours=1)
    # Last season's posting: same company, title, and city, so the same legacy
    # watcher job ID, but a different requisition that the source closed.
    result = import_jobs(
        service,
        [closed_listing(source_url="https://job-boards.greenhouse.io/acme/jobs/1111")],
        "b",
    )

    assert result.counters.jobs_unchanged == 1
    assert result.counters.jobs_updated == 0
    _assert_still_open_and_protected(database, before, clock())


@pytest.mark.parametrize(
    ("direct_url", "listing_url"),
    [
        # Same Greenhouse requisition through the legacy board host.
        (_GREENHOUSE_DIRECT_URL, "https://boards.greenhouse.io/acme/jobs/4455?gh_src=simplify"),
        # Same Workday requisition with a re-posting suffix.
        (
            "https://acme.wd5.myworkdayjobs.com/External/job/New-York/Intern_R12345",
            "https://acme.wd5.myworkdayjobs.com/External/job/New-York-NY/Intern_R12345-1",
        ),
        # Regional Oracle HCM: standard direct URL, preview listing URL.
        (
            _ORACLE_AMEX_DIRECT_URL,
            "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en-US/sites/"
            "CX_1/requisitions/preview/25012345?utm_source=github",
        ),
    ],
)
def test_explicit_closure_with_a_matching_requisition_closes_the_direct_job(
    import_service, direct_url, listing_url
) -> None:
    service, database, clock = import_service
    import_jobs(service, [_direct_at(direct_url)], "a")
    before = _protected_values(_stored_job(database))

    clock.advance(hours=1)
    result = import_jobs(service, [closed_listing(source_url=listing_url)], "b")

    assert result.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert _protected_values(stored) == {
        **before,
        "is_open": False,
        "closed_at": clock(),
    }


def test_explicit_closure_with_the_same_posting_url_closes_the_direct_job(
    import_service,
) -> None:
    service, database, clock = import_service
    direct_url = "https://careers.example.com/jobs/backend-intern-123"
    import_jobs(service, [_direct_at(direct_url)], "a")

    clock.advance(hours=1)
    result = import_jobs(
        service,
        [closed_listing(source_url=f"{direct_url}/?utm_source=simplify&ref=github")],
        "b",
    )

    assert result.counters.jobs_updated == 1
    assert _stored_job(database).is_open is False


@pytest.mark.parametrize(
    ("direct_url", "listing_url"),
    [
        # Different requisitions sharing a title and location.
        (_GREENHOUSE_DIRECT_URL, "https://job-boards.greenhouse.io/acme/jobs/9999"),
        (
            "https://acme.wd5.myworkdayjobs.com/External/job/New-York/Intern_R12345",
            "https://acme.wd5.myworkdayjobs.com/External/job/New-York/Intern_R67890",
        ),
        (
            _ORACLE_AMEX_DIRECT_URL,
            "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
            "CX_1/requisitions/preview/25099999",
        ),
        # Same requisition number, different Oracle site or Greenhouse board.
        (
            _ORACLE_AMEX_DIRECT_URL,
            "https://egug.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/"
            "CX_2/job/25012345",
        ),
        (_GREENHOUSE_DIRECT_URL, "https://job-boards.greenhouse.io/other/jobs/4455"),
        # Requisition on one side only: the URLs differ, so nothing is verified.
        (_GREENHOUSE_DIRECT_URL, "https://careers.example.com/jobs/backend-intern-4455"),
        ("https://careers.example.com/jobs/backend-intern-123", _GREENHOUSE_DIRECT_URL),
        # Different posting-specific URLs without requisitions.
        (
            "https://careers.example.com/jobs/backend-intern-123",
            "https://careers.example.com/jobs/backend-intern-456",
        ),
        # Generic or search URLs are never posting identity.
        ("https://careers.example.com/careers", "https://careers.example.com/careers"),
        (
            "https://careers.example.com/jobs?q=intern",
            "https://careers.example.com/jobs?q=intern",
        ),
        ("https://careers.example.com/", "https://careers.example.com/"),
        # Missing URLs.
        (None, "https://careers.example.com/jobs/backend-intern-123"),
        ("https://careers.example.com/jobs/backend-intern-123", ""),
        (None, ""),
    ],
)
def test_explicit_closure_without_verified_shared_identity_keeps_the_job_open(
    import_service, direct_url, listing_url
) -> None:
    service, database, clock = import_service
    import_jobs(service, [_direct_at(direct_url)], "a")
    before = _protected_values(_stored_job(database))

    clock.advance(hours=1)
    result = import_jobs(service, [closed_listing(source_url=listing_url)], "b")

    assert result.counters.jobs_unchanged == 1
    _assert_still_open_and_protected(database, before, clock())


def test_malformed_closed_listing_url_is_skipped_and_cannot_close(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [_direct_at(_GREENHOUSE_DIRECT_URL)], "a")
    before = _stored_job(database)

    clock.advance(hours=1)
    result = import_jobs(service, [closed_listing(source_url="https://[bad/jobs/4455")], "b")

    assert result.counters.jobs_skipped == 1
    stored = _stored_job(database)
    assert _protected_values(stored) == _protected_values(before)
    assert stored.last_seen_at == before.last_seen_at


def test_a_posting_url_shared_by_another_stored_job_is_ambiguous(import_service) -> None:
    service, database, clock = import_service
    shared = "https://careers.example.com/jobs/backend-intern-123"
    import_jobs(
        service,
        [
            _direct_at(shared),
            final_job(id="watcher-job-2", title="Frontend Intern", source_url=shared),
        ],
        "a",
    )

    clock.advance(hours=1)
    result = import_jobs(service, [closed_listing(source_url=shared)], "b")

    assert result.counters.jobs_unchanged == 1
    with database.session_factory() as db:
        jobs = db.scalars(select(HostedJob)).all()
    assert [job.is_open for job in jobs] == [True, True]


def test_a_backstop_deadline_alone_never_closes_a_direct_job(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [_direct_at(_GREENHOUSE_DIRECT_URL)], "a")
    before = _protected_values(_stored_job(database))

    clock.advance(hours=1)
    expired = backstop_job(
        source_url=_GREENHOUSE_DIRECT_URL,
        deadline="2026-07-01",
        deadline_days_left=-32,
    )
    result = import_jobs(service, [expired], "b")

    assert result.counters.jobs_unchanged == 1
    _assert_still_open_and_protected(database, before, clock())


def test_direct_source_closures_still_apply_explicitly_and_by_deadline(
    import_service,
) -> None:
    service, database, clock = import_service
    import_jobs(
        service,
        [
            final_job(),
            final_job(id="watcher-job-2", title="Frontend Intern", source_url="https://example.com/jobs/456"),
        ],
        "a",
    )

    clock.advance(hours=1)
    result = import_jobs(
        service,
        [
            final_job(extra={**final_job()["extra"], "active": False}),
            final_job(
                id="watcher-job-2",
                title="Frontend Intern",
                source_url="https://example.com/jobs/456",
                deadline_days_left=-1,
            ),
        ],
        "b",
    )

    assert result.counters.jobs_updated == 2
    with database.session_factory() as db:
        jobs = db.scalars(select(HostedJob)).all()
    assert {job.is_open for job in jobs} == {False}
    assert {job.closed_at for job in jobs} == {clock()}


@pytest.mark.parametrize(
    "closing_row",
    [
        # Explicit closure under a different URL.
        lambda: closed_listing(source_url="https://simplify.example/listing/other"),
        # Deadline-inferred closure.
        lambda: backstop_job(deadline="2026-07-01", deadline_days_left=-32),
    ],
)
def test_backstop_only_jobs_keep_their_existing_closure_behavior(
    import_service, closing_row
) -> None:
    service, database, clock = import_service
    import_jobs(service, [backstop_job()], "a")

    clock.advance(hours=1)
    result = import_jobs(service, [closing_row()], "b")

    assert result.counters.jobs_updated == 1
    stored = _stored_job(database)
    assert stored.is_open is False
    assert stored.closed_at == clock()
    assert stored.source_metadata["source_type"] == "backstop"


def test_repeated_unverified_closed_listings_stay_idempotent(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [_direct_at(_GREENHOUSE_DIRECT_URL)], "a")
    before = _protected_values(_stored_job(database))
    stale = closed_listing(source_url="https://job-boards.greenhouse.io/acme/jobs/1111")

    for marker in "bcd":
        clock.advance(hours=1)
        result = import_jobs(service, [stale], marker)
        assert result.counters.jobs_unchanged == 1
        _assert_still_open_and_protected(database, before, clock())
    last_seen = clock()

    clock.advance(hours=1)
    repeated = import_jobs(service, [stale], "d")
    assert repeated.already_imported
    _assert_still_open_and_protected(database, before, last_seen)


def test_failed_import_rolls_back_a_verified_backstop_closure(import_service) -> None:
    service, database, clock = import_service
    import_jobs(service, [_direct_at(_GREENHOUSE_DIRECT_URL)], "a")
    before = _stored_job(database)
    original_upsert = service._upsert_job
    calls = 0

    def failing_upsert(db, mapped, observed_at, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fail after the verified closure")
        return original_upsert(db, mapped, observed_at, **kwargs)

    service._upsert_job = failing_upsert
    clock.advance(hours=1)
    with pytest.raises(JobUpsertFailed):
        import_jobs(
            service,
            [
                closed_listing(source_url=_GREENHOUSE_DIRECT_URL),
                final_job(id="watcher-job-2", title="Frontend Intern"),
            ],
            "b",
        )

    assert calls == 2
    stored = _stored_job(database)
    assert _protected_values(stored) == _protected_values(before)
    assert stored.last_seen_at == before.last_seen_at


# --- revision 20261011_0008 constraints ----------------------------------------


def _insert_identity(connection, job_id, *, key, company="capital-one", kind="requisition", digest=None):
    connection.execute(
        text(
            "INSERT INTO hosted_job_identities (id, job_id, company_id, kind, "
            "identity_key, identity_digest, first_seen_at, last_seen_at) "
            "VALUES (:id, :job, :company, :kind, :key, :digest, :now, :now)"
        ),
        {
            "id": uuid.uuid4(),
            "job": job_id,
            "company": company,
            "kind": kind,
            "key": key,
            "digest": digest or hashlib.sha256(key.encode("utf-8")).hexdigest(),
            "now": datetime(2026, 10, 11, tzinfo=UTC),
        },
    )


def _rejects(database: HostedDatabase, statement: str, params=None) -> None:
    with pytest.raises(IntegrityError):
        with database.engine.begin() as connection:
            connection.execute(text(statement), params or {})


def test_job_identity_and_lifecycle_constraints(import_service) -> None:
    service, database, clock = import_service
    import_jobs(
        service,
        [final_job(), final_job(id="watcher-job-2", title="Frontend Intern")],
        "a",
    )
    with database.session_factory() as db:
        first, second = (
            job.id for job in db.scalars(select(HostedJob).order_by(HostedJob.watcher_job_id))
        )

    with database.engine.begin() as connection:
        _insert_identity(connection, first, key="greenhouse|acme|4455")
        # The same key at another company, or as another kind, is distinct.
        _insert_identity(connection, second, key="greenhouse|acme|4455", company="stripe")
        _insert_identity(connection, second, key="greenhouse|acme|4455", kind="url")
        _insert_identity(connection, first, key="x" * 4096, kind="listing")

    def identity(job_id, **kwargs):
        def insert(connection):
            _insert_identity(connection, job_id, **kwargs)

        with pytest.raises(IntegrityError):
            with database.engine.begin() as connection:
                insert(connection)

    # One key names at most one job per company and kind.
    identity(second, key="greenhouse|acme|4455")
    identity(first, key="greenhouse|acme|4455")
    # The digest must be the SHA-256 of the key; kinds and lengths are bounded.
    identity(first, key="greenhouse|acme|1", digest="0" * 64)
    identity(first, key="greenhouse|acme|2", kind="title")
    identity(first, key="", digest=hashlib.sha256(b"").hexdigest())
    identity(first, key="x" * 4097, kind="listing")
    identity(uuid.uuid4(), key="greenhouse|acme|3")

    # Identity rows protect their job from deletion.
    _rejects(database, "DELETE FROM hosted_jobs WHERE id = :id", {"id": first})

    open_job = {"id": first}
    _rejects(database, "UPDATE hosted_jobs SET absent_complete_runs = -1 WHERE id = :id", open_job)
    _rejects(database, "UPDATE hosted_jobs SET is_open = false, closure_reason = 'gone' WHERE id = :id", open_job)
    _rejects(database, "UPDATE hosted_jobs SET closure_reason = 'deadline_passed' WHERE id = :id", open_job)
    _rejects(database, "UPDATE hosted_jobs SET superseded_by_job_id = :other WHERE id = :id", {"id": first, "other": second})
    _rejects(
        database,
        "UPDATE hosted_jobs SET is_open = false, closure_reason = 'superseded', "
        "superseded_by_job_id = id WHERE id = :id",
        open_job,
    )
    _rejects(
        database,
        "UPDATE hosted_jobs SET is_open = false, closure_reason = 'superseded', "
        "superseded_by_job_id = :missing WHERE id = :id",
        {"id": first, "missing": uuid.uuid4()},
    )
    with database.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE hosted_jobs SET is_open = false, closed_at = last_seen_at, "
                "closure_reason = 'superseded', superseded_by_job_id = :other "
                "WHERE id = :id"
            ),
            {"id": first, "other": second},
        )
    # A job others point at cannot be deleted either.
    _rejects(database, "DELETE FROM hosted_jobs WHERE id = :id", {"id": second})


def test_source_observation_constraints(import_service) -> None:
    service, database, _clock = import_service
    run_id = import_jobs(service, [final_job()], "a").run_id
    columns = (
        "id, import_run_id, source_key, source_kind, adapter, observed_at, "
        "attempted, succeeded, complete, rows_returned, identity_count"
    )

    def observation(**overrides):
        values = {
            "id": uuid.uuid4(),
            "run": run_id,
            "key": "company:acme:direct:greenhouse",
            "kind": "direct",
            "adapter": "greenhouse",
            "now": datetime(2026, 10, 11, tzinfo=UTC),
            "attempted": True,
            "succeeded": True,
            "complete": True,
            "rows": 3,
            "identities": 3,
        }
        values.update(overrides)
        return (
            f"INSERT INTO hosted_source_observations ({columns}) VALUES "
            "(:id, :run, :key, :kind, :adapter, :now, :attempted, :succeeded, "
            ":complete, :rows, :identities)",
            values,
        )

    with database.engine.begin() as connection:
        connection.execute(text(observation()[0]), observation()[1])
        connection.execute(
            text(observation(key="github_feed:abc", kind="github_feed")[0]),
            observation(key="github_feed:abc", kind="github_feed")[1],
        )
        statement, values = observation(
            key="company:other:direct:workday",
            attempted=False,
            succeeded=None,
            complete=None,
            rows=None,
            identities=None,
        )
        connection.execute(text(statement), values)

    for overrides in (
        {},  # one observation per source per run
        {"key": "k1", "kind": "rss"},
        {"key": "k2", "succeeded": False, "complete": True},
        {"key": "k3", "succeeded": None, "complete": True},
        {"key": "k4", "attempted": False},
        {"key": "k5", "rows": -1},
        {"key": "k6", "identities": -1},
        {"key": "k7", "run": uuid.uuid4()},
    ):
        _rejects(database, *observation(**overrides))

    # Observations belong to their run.
    with database.engine.begin() as connection:
        connection.execute(text("DELETE FROM hosted_job_import_attempts"))
        connection.execute(
            text("DELETE FROM hosted_job_import_runs WHERE id = :id"), {"id": run_id}
        )
        assert connection.scalar(text("SELECT count(*) FROM hosted_source_observations")) == 0
