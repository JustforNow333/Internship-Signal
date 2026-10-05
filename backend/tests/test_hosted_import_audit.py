"""The post-import HOSTED-AUDIT is bounded, sanitized, read only, and nonfatal.

State is built directly with the ORM on SQLite so the audit's classification
and integrity rules are exercised without PostgreSQL. The end-to-end CLI path
is covered against PostgreSQL in ``test_hosted_collect_and_import``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import DateTime, create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.hosted import import_audit, import_snapshot
from app.hosted.database import Base
from app.hosted.import_audit import (
    MAX_AUDITED_MATCHES,
    PREFIX,
    format_audit_lines,
    import_audit_lines,
    run_import_audit,
)
from app.hosted.job_import import ImportCounters, JobImportResult
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

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
COMPANY = "american-express"
SECRETS = (
    "Software Engineer Intern",
    "New York",
    "american-express",
    "American Express",
    "person@example.com",
    "https://example.com",
    "25012345",
)


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


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    engine.dialect.colspecs = {**engine.dialect.colspecs, DateTime: AwareDateTime}
    Base.metadata.create_all(engine)
    try:
        yield SimpleNamespace(
            session_factory=sessionmaker(bind=engine, expire_on_commit=False)
        )
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.fixture
def run_id(database) -> uuid.UUID:
    run = HostedJobImportRun(
        id=uuid.uuid4(),
        source_fingerprint="a" * 64,
        source_identifier="hosted-collection",
        source_type="hosted_collection",
        started_at=NOW,
        completed_at=NOW,
        status="succeeded",
        created_at=NOW,
        updated_at=NOW,
    )
    with database.session_factory.begin() as db:
        db.add(run)
    return run.id


def add_user(database, *, verified: bool = True, paused: bool = False) -> uuid.UUID:
    user_id = uuid.uuid4()
    with database.session_factory.begin() as db:
        db.add(
            User(
                id=user_id,
                email="person@example.com",
                normalized_email=f"{user_id}@example.com",
                password_hash="x",
                email_verified_at=NOW - timedelta(days=90) if verified else None,
                is_active=True,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        db.flush()
        db.add(
            UserPreference(
                user_id=user_id,
                role_ids=["software_engineering"],
                alert_frequency="as_detected",
                globally_paused=paused,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        db.add(
            UserCompanyWatch(
                user_id=user_id,
                company_id=COMPANY,
                paused=False,
                created_at=NOW - timedelta(days=60),
                updated_at=NOW,
            )
        )
    return user_id


def add_job(
    database,
    *,
    posting_date: date | None = None,
    first_seen_at: datetime = NOW,
    source_type: str = "direct",
    requisition_id: str | None = "25012345",
    location: str = "New York, NY, United States",
    title: str = "Software Engineer Intern",
) -> uuid.UUID:
    metadata: dict[str, object] = {"source_type": source_type}
    metadata["adapter"] = "oracle_hcm" if source_type == "direct" else "simplify"
    if requisition_id:
        metadata["requisition_id"] = requisition_id
    job_id = uuid.uuid4()
    with database.session_factory.begin() as db:
        db.add(
            HostedJob(
                id=job_id,
                watcher_job_id=uuid.uuid4().hex,
                company_id=COMPANY,
                company_name="American Express",
                title=title,
                location=location,
                remote_status="",
                role_id="software_engineering",
                career_level="internship",
                application_url="https://example.com/jobs/1",
                posting_date=posting_date or NOW.date(),
                is_open=True,
                first_seen_at=first_seen_at,
                last_seen_at=NOW,
                source_metadata=metadata,
                created_at=first_seen_at,
                updated_at=NOW,
            )
        )
    return job_id


def add_match(database, user_id: uuid.UUID, job_id: uuid.UUID) -> uuid.UUID:
    match_id = uuid.uuid4()
    with database.session_factory.begin() as db:
        db.add(
            UserJobMatch(
                id=match_id,
                user_id=user_id,
                job_id=job_id,
                match_reasons=[],
                matched_at=NOW,
                last_matched_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return match_id


def add_item(
    database,
    *,
    user_id: uuid.UUID,
    match_id: uuid.UUID,
    run_id: uuid.UUID,
    batch_status: str = "pending",
) -> uuid.UUID:
    batch_id = uuid.uuid4()
    lifecycle: dict[str, object] = {}
    if batch_status == "sent":
        lifecycle = {"send_started_at": NOW, "sent_at": NOW}
    elif batch_status == "cancelled":
        lifecycle = {"cancelled_at": NOW}
    elif batch_status in {"permanent_failed", "uncertain"}:
        lifecycle = {"send_started_at": NOW}
    with database.session_factory.begin() as db:
        db.add(
            HostedNotificationBatch(
                id=batch_id,
                user_id=user_id,
                frequency="as_detected",
                status=batch_status,
                due_at=NOW,
                next_attempt_at=NOW,
                attempt_count=0,
                email_message_id=f"<notification-{batch_id}@internship-signal.invalid>",
                source_import_run_id=run_id,
                created_at=NOW,
                updated_at=NOW,
                **lifecycle,
            )
        )
        db.flush()
        db.add(
            HostedNotificationItem(
                id=uuid.uuid4(),
                batch_id=batch_id,
                user_job_match_id=match_id,
                source_import_run_id=run_id,
                status="pending",
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return batch_id


def result_for(
    run_id: uuid.UUID,
    *,
    created: tuple[uuid.UUID, ...] = (),
    new_openings: tuple[uuid.UUID, ...] = (),
    outcome: str = "imported",
) -> JobImportResult:
    return JobImportResult(
        run_id=run_id,
        source_fingerprint="a" * 64,
        outcome=outcome,
        counters=ImportCounters(
            jobs_received=len(created),
            jobs_inserted=len(created),
            jobs_updated=0,
            jobs_unchanged=0,
            jobs_skipped=0,
            matches_created=len(created),
        ),
        skipped_reasons={},
        created_match_ids=created,
        new_opening_match_ids=new_openings,
    )


def parse(lines: list[str]) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = {}
    for line in lines:
        prefix, *pairs = line.split(" ")
        assert prefix == PREFIX
        fields = dict(pair.split("=", 1) for pair in pairs)
        parsed[fields.pop("kind")] = fields
    return parsed


def audit(database, result, **options) -> dict[str, dict[str, str]]:
    return parse(import_audit_lines(database, result, **options))


def snapshot_rows(database) -> dict[str, list[tuple]]:
    with database.session_factory() as db:
        return {
            table.name: sorted(
                (tuple(str(value) for value in row) for row in db.execute(select(table))),
            )
            for table in Base.metadata.sorted_tables
        }


def test_a_notified_new_opening_is_consistent(database, run_id) -> None:
    user_id = add_user(database)
    match_id = add_match(database, user_id, add_job(database))
    add_item(database, user_id=user_id, match_id=match_id, run_id=run_id)

    report = audit(
        database, result_for(run_id, created=(match_id,), new_openings=(match_id,))
    )

    assert report["summary"]["mode"] == "standard"
    assert report["summary"]["matches_created"] == "1"
    assert report["summary"]["new_opening_matches"] == "1"
    assert report["summary"]["notification_eligible_matches"] == "1"
    assert report["summary"]["notification_items_created"] == "1"
    assert report["summary"]["notification_items_present"] == "1"
    assert report["summary"]["silent_catch_up_matches"] == "0"
    assert report["integrity"]["status"] == "ok"
    assert report["integrity"]["new_opening_missing_notification"] == "0"
    assert report["integrity"]["notification_integrity_anomalies"] == "0"


def test_an_intentional_catch_up_stays_silent_without_an_anomaly(
    database, run_id
) -> None:
    user_id = add_user(database)
    job_id = add_job(database, posting_date=NOW.date() - timedelta(days=40))
    match_id = add_match(database, user_id, job_id)

    report = audit(database, result_for(run_id, created=(match_id,)))

    assert report["summary"]["silent_catch_up_matches"] == "1"
    assert report["summary"]["notification_eligible_matches"] == "0"
    assert report["summary"]["notification_items_present"] == "0"
    assert report["integrity"]["status"] == "ok"
    assert report["integrity"]["new_opening_missing_notification"] == "0"
    # First stored today for a posting dated 40 days ago.
    assert report["integrity"]["late_discoveries"] == "1"


def test_a_backfill_counts_every_match_as_intentionally_silent(
    database, run_id
) -> None:
    user_id = add_user(database)
    match_id = add_match(database, user_id, add_job(database))

    report = audit(
        database,
        result_for(run_id, created=(match_id,), new_openings=(match_id,)),
        backfill=True,
    )

    assert report["summary"]["mode"] == "backfill"
    assert report["summary"]["backfill_silent_matches"] == "1"
    assert report["summary"]["new_opening_matches"] == "0"
    assert report["integrity"]["status"] == "ok"


def test_a_notifiable_new_opening_without_an_item_needs_attention(
    database, run_id
) -> None:
    user_id = add_user(database)
    match_id = add_match(database, user_id, add_job(database))

    report = audit(
        database, result_for(run_id, created=(match_id,), new_openings=(match_id,))
    )

    assert report["integrity"]["status"] == "attention"
    assert report["integrity"]["new_opening_missing_notification"] == "1"


def test_a_suppressed_recipient_is_not_reported_as_missing_work(
    database, run_id
) -> None:
    unverified = add_user(database, verified=False)
    paused = add_user(database, paused=True)
    job_id = add_job(database)
    matches = (add_match(database, unverified, job_id), add_match(database, paused, job_id))

    report = audit(database, result_for(run_id, created=matches, new_openings=matches))

    assert report["summary"]["new_opening_suppressed"] == "2"
    assert report["integrity"]["new_opening_missing_notification"] == "0"
    assert report["integrity"]["status"] == "ok"


def test_notification_work_on_a_catch_up_match_is_an_integrity_anomaly(
    database, run_id
) -> None:
    user_id = add_user(database)
    match_id = add_match(database, user_id, add_job(database))
    add_item(database, user_id=user_id, match_id=match_id, run_id=run_id)

    report = audit(database, result_for(run_id, created=(match_id,)))

    assert report["integrity"]["silent_match_with_notification"] == "1"
    assert report["integrity"]["notification_integrity_anomalies"] == "1"
    assert report["integrity"]["status"] == "attention"


def test_item_under_another_users_batch_is_an_integrity_anomaly(
    database, run_id
) -> None:
    owner = add_user(database)
    other = add_user(database)
    match_id = add_match(database, owner, add_job(database))
    add_item(database, user_id=other, match_id=match_id, run_id=run_id)

    report = audit(
        database, result_for(run_id, created=(match_id,), new_openings=(match_id,))
    )

    assert report["integrity"]["batch_user_mismatch"] == "1"
    assert report["integrity"]["status"] == "attention"


def test_pending_items_under_closed_and_failed_batches_are_distinguished(
    database, run_id
) -> None:
    user_id = add_user(database)
    for status in ("sent", "cancelled", "permanent_failed", "uncertain"):
        match_id = add_match(database, user_id, add_job(database, title=status))
        add_item(
            database,
            user_id=user_id,
            match_id=match_id,
            run_id=run_id,
            batch_status=status,
        )
        # The (user, run) slot is unique per as-detected batch.
        with database.session_factory.begin() as db:
            batch = db.scalar(
                select(HostedNotificationBatch).where(
                    HostedNotificationBatch.status == status
                )
            )
            batch.source_import_run_id = None
            batch.frequency = "daily"

    report = audit(database, result_for(run_id))

    assert report["integrity"]["pending_items_closed_batch"] == "2"
    # Failed or uncertain delivery leaves items pending by design.
    assert report["integrity"]["pending_items_failed_batch"] == "2"
    assert report["integrity"]["notification_integrity_anomalies"] == "2"


def test_a_backstop_copy_of_a_direct_requisition_is_suspicious(
    database, run_id
) -> None:
    user_id = add_user(database)
    direct_job = add_job(database)
    direct_match = add_match(database, user_id, direct_job)
    add_item(database, user_id=user_id, match_id=direct_match, run_id=run_id)
    # Same posting reached through a backstop feed whose URL dedupe could not
    # tie to the direct requisition: no requisition_id, an older listing date,
    # and a shorter spelling of the same location.
    backstop_job = add_job(
        database,
        source_type="backstop",
        requisition_id=None,
        location="New York, NY",
        posting_date=NOW.date() - timedelta(days=45),
    )
    backstop_match = add_match(database, user_id, backstop_job)

    report = audit(
        database,
        result_for(
            run_id,
            created=(direct_match, backstop_match),
            new_openings=(direct_match,),
        ),
    )

    assert report["integrity"]["duplicate_scan"] == "ok"
    assert report["integrity"]["suspicious_cross_source_duplicates"] == "1"
    assert report["integrity"]["duplicate_requisition_asymmetric"] == "1"
    assert report["integrity"]["suspicious_duplicate_matches"] == "2"
    assert report["summary"]["backstop_matches"] == "1"
    assert report["summary"]["silent_catch_up_matches"] == "1"
    assert report["integrity"]["late_discoveries"] == "1"
    # A heuristic never escalates the deterministic status.
    assert report["integrity"]["status"] == "ok"


def test_an_existing_direct_row_is_found_for_a_new_backstop_match(
    database, run_id
) -> None:
    user_id = add_user(database)
    add_job(database, first_seen_at=NOW - timedelta(days=30))
    backstop_match = add_match(
        database,
        user_id,
        add_job(database, source_type="backstop", requisition_id=None),
    )

    report = audit(database, result_for(run_id, created=(backstop_match,)))

    assert report["integrity"]["suspicious_cross_source_duplicates"] == "1"
    assert report["integrity"]["suspicious_duplicate_matches"] == "1"


@pytest.mark.parametrize(
    ("other", "reason"),
    [
        ({"requisition_id": "25099999"}, "two direct requisitions"),
        ({"location": "Phoenix, AZ"}, "different location"),
        ({"title": "Data Science Intern"}, "different title"),
    ],
)
def test_distinct_postings_are_not_reported_as_duplicates(
    database, run_id, other, reason
) -> None:
    user_id = add_user(database)
    first = add_match(database, user_id, add_job(database))
    second = add_match(database, user_id, add_job(database, **other))

    report = audit(database, result_for(run_id, created=(first, second)))

    assert report["integrity"]["suspicious_cross_source_duplicates"] == "0", reason


def test_the_audit_never_mutates_database_state(database, run_id) -> None:
    user_id = add_user(database)
    new_match = add_match(database, user_id, add_job(database))
    silent_match = add_match(
        database,
        user_id,
        add_job(database, source_type="backstop", requisition_id=None),
    )
    before = snapshot_rows(database)

    lines = import_audit_lines(
        database,
        result_for(run_id, created=(new_match, silent_match), new_openings=(new_match,)),
    )

    assert len(lines) == 2
    assert snapshot_rows(database) == before


def test_output_is_bounded_and_sanitized(database, run_id) -> None:
    user_id = add_user(database)
    matches = tuple(
        add_match(database, user_id, add_job(database, title=f"Intern {index}"))
        for index in range(25)
    )

    lines = import_audit_lines(
        database, result_for(run_id, created=matches, new_openings=matches)
    )

    assert len(lines) == 2
    assert all(len(line) < 700 for line in lines)
    joined = "\n".join(lines)
    for secret in (*SECRETS, str(user_id), str(run_id), *map(str, matches)):
        assert secret.casefold() not in joined.casefold()
    for line in lines:
        for pair in line.split(" ")[1:]:
            key, value = pair.split("=", 1)
            assert key.replace("_", "").isalpha()
            assert value.replace("_", "").isalnum()


def test_an_oversized_run_skips_per_match_checks_instead_of_truncating(
    database, run_id
) -> None:
    created = tuple(uuid.uuid4() for _ in range(MAX_AUDITED_MATCHES + 1))

    report = audit(database, result_for(run_id, created=created))

    assert report["summary"]["match_checks"] == "skipped_over_budget"
    assert report["integrity"]["duplicate_scan"] == "skipped_over_budget"


def test_an_already_imported_result_is_not_audited(run_id) -> None:
    def explode():
        raise AssertionError("no database access expected")

    lines = import_audit_lines(
        SimpleNamespace(session_factory=explode),
        result_for(run_id, outcome="already_imported"),
    )

    assert lines == [f"{PREFIX} kind=summary status=skipped reason=already_imported"]


def test_an_audit_failure_returns_one_safe_line(run_id) -> None:
    def broken():
        raise RuntimeError("password=hunter2 host=db.internal")

    lines = import_audit_lines(
        SimpleNamespace(session_factory=broken), result_for(run_id)
    )

    assert lines == [f"{PREFIX} kind=summary status=skipped reason=audit_unavailable"]
    assert run_import_audit("not a database url", result_for(run_id)) == lines


def test_an_audit_failure_cannot_fail_a_successful_import(
    monkeypatch, capsys, run_id
) -> None:
    monkeypatch.setenv("HOSTED_DATABASE_URL", "postgresql+psycopg://u:p@localhost/x")
    monkeypatch.setattr(
        import_snapshot,
        "import_snapshot_into_hosted",
        lambda *args, **kwargs: result_for(run_id),
    )

    def broken(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(import_audit, "collect_import_audit", broken)

    assert import_snapshot.main(["--snapshot", "unused.json.gz"]) == 0
    output = capsys.readouterr().out
    assert "HOSTED-JOB-IMPORT mode=standard outcome=imported" in output
    assert "HOSTED-AUDIT kind=summary status=skipped reason=audit_unavailable" in output


def test_format_is_stable_for_log_parsers() -> None:
    lines = format_audit_lines(import_audit.ImportAudit(), elapsed_ms=3)

    assert lines[0].startswith("HOSTED-AUDIT kind=summary mode=standard match_checks=ok ")
    assert lines[0].endswith(" elapsed_ms=3")
    assert lines[1].startswith("HOSTED-AUDIT kind=integrity status=ok ")
