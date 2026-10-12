"""Phase 2 shadow tracking of stable job identities and source evidence."""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from app.hosted import identity_shadow
from app.hosted.catalog import CompanyCatalog, PublicCompany
from app.hosted.database import HostedDatabase, normalize_database_url
from app.hosted.identity_shadow import (
    SHADOW_FLAG_ENV,
    ImportedJob,
    SourceEvidence,
    derive_identities,
    identity_digest,
    identity_shadow_enabled,
    run_identity_shadow,
)
from app.hosted.import_snapshot import import_snapshot_into_hosted, import_summary_lines
from backend.app.dedupe import non_specific_posting_urls
from app.hosted.job_import import JobImportService
from app.hosted.job_mapper import map_final_jobs
from app.hosted.models import (
    HostedJob,
    HostedJobIdentity,
    HostedJobImportRun,
    HostedNotificationBatch,
    HostedNotificationItem,
    HostedSourceObservation,
    User,
    UserJobMatch,
)
from app.hosted.snapshot_jobs import snapshot_sha256
from sqlalchemy import event, func, select, text

from tests.test_hosted_notifications import (  # noqa: F401 - pytest fixtures
    MutableClock,
    clean_database,
    clock,
    create_user,
    database,
    notification_postgres_url,
)
from watcher.collection_snapshot import (
    CollectionBatch,
    collection_config_fingerprint,
    save_collection_snapshot,
)
from watcher.config import load_watchlist
from watcher.health.state import direct_health_key, github_feed_health_key
from watcher.source_health import SourceAttempt
from watcher.sources.base import make_row

WORKDAY_URL = (
    "https://stripe.wd5.myworkdayjobs.com/Stripe/job/New-York-NY/"
    "Backend-Engineer-Intern_{}"
)
WORKDAY_SCOPE = "stripe_stripe"
FEED_URL = "https://raw.githubusercontent.com/example/listings/main/listings.json"
DIRECT_KEY = direct_health_key("Stripe", "workday")
FEED_KEY = github_feed_health_key(FEED_URL)
CONFIG_FINGERPRINT = "f" * 64
NEW_STRUCTURE_NAMES = (
    "hosted_job_identities",
    "hosted_source_observations",
    "source_key",
    "last_direct_seen_at",
    "absent_complete_runs",
    "closure_reason",
    "superseded_by_job_id",
)


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


def direct_job(
    index: int = 1,
    *,
    requisition: str | None = "R1",
    url: str | None = None,
    **overrides,
) -> dict:
    extra = {"source": "direct", "source_adapter": "workday", "active": True}
    if requisition is not None:
        extra["source_requisition_id"] = requisition
    job = {
        "id": f"watcher-job-{index}",
        "company": "Stripe",
        "title": f"Backend Software Engineer Intern {index}",
        "location": "New York, NY",
        "remote_status": "Hybrid",
        "description": "Build production APIs.",
        "requirements": "Python and SQL",
        "source_url": url if url is not None else WORKDAY_URL.format(requisition or "R1"),
        "date_posted": "2026-08-03",
        "deadline": "2026-09-01",
        "deadline_days_left": 30,
        "internship_type": "Summer 2027 Internship",
        "role_classification": {"role": "swe", "role_track": "backend"},
        "extra": extra,
    }
    job.update(overrides)
    return job


def backstop_row(index: int = 1, *, url: str, listing_id: str = "listing-1", **overrides) -> dict:
    values = {
        "description": "",
        "requirements": "",
        "source_url": url,
        "deadline": "",
        "deadline_days_left": None,
        "extra": {
            "source": "github",
            "source_adapter": "github_listings",
            "source_id": listing_id,
            "listing_id": listing_id,
            "feed_url": FEED_URL,
            "active": True,
        },
    }
    values.update(overrides)
    return direct_job(index, **values)


def attempt(
    health_key: str = DIRECT_KEY,
    *,
    observed_at: datetime | None = None,
    source_kind: str = "direct",
    adapter: str = "workday",
    company: str | None = "Stripe",
    attempted: bool = True,
    succeeded: bool | None = True,
    rows_returned: int | None = 1,
    **diagnostics,
) -> SourceAttempt:
    return SourceAttempt(
        health_key=health_key,
        run_id="run-1",
        observed_at=observed_at or datetime(2026, 8, 2, 11, 30, tzinfo=UTC),
        source_kind=source_kind,
        company=company,
        adapter=adapter,
        attempted=attempted,
        succeeded=succeeded,
        rows_returned=rows_returned,
        **diagnostics,
    )


def evidence(*attempts: SourceAttempt) -> SourceEvidence:
    return SourceEvidence(
        attempts=attempts or (attempt(complete=True, incomplete=False),),
        config_fingerprint=CONFIG_FINGERPRINT,
    )


def run_import(
    database: HostedDatabase,
    catalog: CompanyCatalog,
    clock: MutableClock,
    jobs: list[dict],
    marker: str,
    *,
    shadow: bool = True,
    source_evidence: SourceEvidence | None = None,
):
    service = JobImportService(database, catalog, clock=clock, identity_shadow=shadow)
    return service.import_jobs(
        jobs,
        source_fingerprint=marker * 64,
        source_identifier=f"snapshot-{marker}.json.gz",
        source_type="collection_snapshot",
        source_evidence=source_evidence if source_evidence is not None else evidence(),
    )


def identities(database: HostedDatabase) -> dict[tuple[str, str], str]:
    with database.session_factory() as db:
        rows = db.execute(
            select(
                HostedJobIdentity.kind,
                HostedJobIdentity.identity_key,
                HostedJob.watcher_job_id,
            ).join(HostedJob, HostedJob.id == HostedJobIdentity.job_id)
        ).all()
    return {(kind, key): watcher_id for kind, key, watcher_id in rows}


def job_evidence(database: HostedDatabase) -> dict[str, tuple]:
    with database.session_factory() as db:
        rows = db.execute(
            text(
                "SELECT watcher_job_id, source_key, last_direct_seen_at, "
                "absent_complete_runs, closure_reason, superseded_by_job_id "
                "FROM hosted_jobs"
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def observations(database: HostedDatabase) -> list[HostedSourceObservation]:
    with database.session_factory() as db:
        return list(
            db.scalars(
                select(HostedSourceObservation).order_by(
                    HostedSourceObservation.observed_at,
                    HostedSourceObservation.source_key,
                )
            )
        )


def requisition_key(native_id: str) -> str:
    return f"workday|{WORKDAY_SCOPE}|{native_id.casefold()}"


def url_key(native_id: str) -> str:
    return WORKDAY_URL.format(native_id)


# --- Flag ---------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "  ", "0", "false", "off", "no", "maybe", "enabled"])
def test_shadow_flag_is_off_unless_explicitly_true(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv(SHADOW_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(SHADOW_FLAG_ENV, value)
    assert identity_shadow_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_shadow_flag_accepts_only_explicit_true_values(monkeypatch, value) -> None:
    monkeypatch.setenv(SHADOW_FLAG_ENV, value)
    assert identity_shadow_enabled() is True


# --- Identity derivation (pure) ------------------------------------------


def test_identities_are_strong_keys_only() -> None:
    derived = derive_identities(direct_job(requisition="R7"))
    assert [(item.kind, item.key) for item in derived.identities] == [
        ("requisition", requisition_key("R7")),
        ("url", url_key("R7")),
    ]
    assert derived.native_url_disagreement is False
    for item in derived.identities:
        assert "intern 1" not in item.key.casefold()
        assert "new york" not in item.key.casefold()
        assert item.digest == hashlib.sha256(item.key.encode("utf-8")).hexdigest()


def test_workday_native_and_url_requisitions_are_both_kept() -> None:
    derived = derive_identities(
        direct_job(requisition="R7", url=WORKDAY_URL.format("R8"))
    )
    assert [(item.kind, item.key) for item in derived.identities] == [
        ("requisition", requisition_key("R7")),
        ("requisition", requisition_key("R8")),
        ("url", url_key("R8")),
    ]
    assert derived.native_url_disagreement is True


def test_listing_identity_is_scoped_to_its_feed() -> None:
    derived = derive_identities(
        backstop_row(url="https://simplify.example/apply/abc", listing_id="UUID-1")
    )
    kinds = {item.kind: item.key for item in derived.identities}
    assert kinds["listing"] == f"{FEED_KEY}|uuid-1"
    assert "requisition" not in kinds


def test_weak_rows_have_no_identity_and_never_fall_back_to_title() -> None:
    generic = direct_job(requisition=None, url="https://stripe.com/careers")
    assert derive_identities(generic).identities == ()
    # A URL that two distinct requisitions share names neither posting.
    shared = "https://stripe.com/jobs/shared-posting-77"
    rows = [
        direct_job(1, requisition=None, url=shared, extra={"source": "direct", "source_requisition_id": "A1"}),
        direct_job(2, requisition=None, url=shared, extra={"source": "direct", "source_requisition_id": "B2"}),
    ]
    non_specific = non_specific_posting_urls(rows)
    assert non_specific
    derived = derive_identities(
        direct_job(requisition=None, url=shared),
        non_specific_urls=non_specific,
    )
    assert derived.identities == ()


def test_oversized_or_control_character_keys_are_rejected() -> None:
    long_id = "R" + "1" * 5000
    derived = derive_identities(direct_job(requisition=long_id, url="https://stripe.com/careers"))
    assert derived.identities == ()
    assert derived.rejected == 1


# --- Disabled -----------------------------------------------------------


def test_disabled_import_never_touches_the_new_structures(database, catalog, clock) -> None:
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        first = run_import(database, catalog, clock, [direct_job(1)], "a", shadow=False)
        clock.advance(hours=1)
        second = run_import(
            database,
            catalog,
            clock,
            [direct_job(1, title="Retitled Backend Intern 1"), direct_job(2, requisition="R2")],
            "b",
            shadow=False,
        )
    finally:
        event.remove(database.engine, "before_cursor_execute", record)

    assert first.identity_shadow is None and second.identity_shadow is None
    assert not any(line.startswith("HOSTED-IDENTITY") for line in import_summary_lines(second))
    joined = "\n".join(statements).casefold()
    for name in NEW_STRUCTURE_NAMES:
        assert name not in joined
    assert identities(database) == {}
    assert observations(database) == []
    assert set(job_evidence(database).values()) == {(None, None, 0, None, None)}


def _state(database: HostedDatabase) -> dict[str, object]:
    with database.session_factory() as db:
        jobs = {
            job.watcher_job_id: (
                job.company_id,
                job.title,
                job.location,
                job.role_id,
                job.career_level,
                job.application_url,
                job.posting_date,
                job.deadline,
                job.is_open,
                job.closed_at,
                job.first_seen_at,
                job.last_seen_at,
                job.updated_at,
                job.source_metadata,
                job.first_seen_in_backfill,
            )
            for job in db.scalars(select(HostedJob))
        }
        matches = sorted(
            tuple(row)
            for row in db.execute(
                select(
                    User.email,
                    HostedJob.watcher_job_id,
                    UserJobMatch.match_reasons,
                    UserJobMatch.matched_at,
                    UserJobMatch.last_matched_at,
                    UserJobMatch.no_longer_matches_at,
                    UserJobMatch.saved_at,
                    UserJobMatch.dismissed_at,
                )
                .join(User, User.id == UserJobMatch.user_id)
                .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
            )
        )
        items = sorted(
            (watcher_id, status, reason)
            for watcher_id, status, reason in db.execute(
                select(
                    HostedJob.watcher_job_id,
                    HostedNotificationItem.status,
                    HostedNotificationItem.cancellation_reason,
                )
                .join(UserJobMatch, UserJobMatch.id == HostedNotificationItem.user_job_match_id)
                .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
            )
        )
        batches = sorted(
            (frequency, status, due_at)
            for frequency, status, due_at in db.execute(
                select(
                    HostedNotificationBatch.frequency,
                    HostedNotificationBatch.status,
                    HostedNotificationBatch.due_at,
                )
            )
        )
        runs = sorted(
            (
                run.source_fingerprint,
                run.status,
                run.jobs_received,
                run.jobs_inserted,
                run.jobs_updated,
                run.jobs_unchanged,
                run.jobs_skipped,
                run.matches_created,
            )
            for run in db.scalars(select(HostedJobImportRun))
        )
    return {"jobs": jobs, "matches": matches, "items": items, "batches": batches, "runs": runs}


def _scenario(database, catalog, clock, *, shadow: bool) -> list:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "watcher@example.com")
    results = []
    steps = [
        # New openings: matches and notification work.
        [direct_job(1), direct_job(2, requisition="R2"), direct_job(3, requisition=None, url="https://stripe.com/careers")],
        # A retitle (new legacy ID), an explicit closure, and a backstop-only
        # sighting of a direct job.
        [
            direct_job(10, requisition="R1", title="Retitled Backend Intern"),
            direct_job(2, requisition="R2", extra={"source": "direct", "source_adapter": "workday", "source_requisition_id": "R2", "active": False}),
            backstop_row(3, url="https://stripe.com/careers", listing_id="L-3"),
        ],
        # Deadline closure and the direct source returning.
        [
            direct_job(10, requisition="R1", title="Retitled Backend Intern", deadline="2026-08-01", deadline_days_left=-2),
            direct_job(3, requisition=None, url="https://stripe.com/careers"),
        ],
    ]
    for index, jobs in enumerate(steps):
        clock.advance(hours=1)
        results.append(
            run_import(database, catalog, clock, jobs, "abc"[index], shadow=shadow)
        )
    return results


def _truncate(database: HostedDatabase) -> None:
    with database.engine.begin() as connection:
        names = connection.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename <> 'alembic_version'"
            )
        ).scalars().all()
        connection.exec_driver_sql(
            "TRUNCATE TABLE " + ", ".join(f'"{name}"' for name in names) + " RESTART IDENTITY CASCADE"
        )


def test_shadow_mode_leaves_jobs_matches_notifications_and_closures_identical(
    database, catalog
) -> None:
    disabled_results = _scenario(database, catalog, MutableClock(), shadow=False)
    disabled = _state(database)
    assert identities(database) == {}
    _truncate(database)

    enabled_results = _scenario(database, catalog, MutableClock(), shadow=True)
    enabled = _state(database)

    assert enabled == disabled
    assert [result.counters for result in enabled_results] == [
        result.counters for result in disabled_results
    ]
    assert [result.outcome for result in enabled_results] == ["imported"] * 3
    assert all(result.identity_shadow.status == "ok" for result in enabled_results)
    # The scenario really exercised matching, notifications, and closures.
    assert disabled["matches"] and disabled["items"]
    assert any(values[8] is False for values in disabled["jobs"].values())
    assert identities(database)


# --- Enabled writes -----------------------------------------------------


def test_enabled_import_records_identities_ownership_and_observations(
    database, catalog, clock
) -> None:
    seen_at = datetime(2026, 8, 2, 11, 45, tzinfo=UTC)
    result = run_import(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="R1")],
        "a",
        source_evidence=evidence(
            attempt(observed_at=seen_at, complete=True, incomplete=False, truncated=False, degraded=False)
        ),
    )

    assert identities(database) == {
        ("requisition", requisition_key("R1")): "watcher-job-1",
        ("url", url_key("R1")): "watcher-job-1",
    }
    with database.session_factory() as db:
        stored = db.scalars(select(HostedJobIdentity)).all()
        assert db.scalar(
            text(
                "SELECT count(*) FROM hosted_job_identities WHERE identity_digest = "
                "encode(sha256(convert_to(identity_key, 'UTF8')), 'hex')"
            )
        ) == 2
    assert {row.source_key for row in stored} == {DIRECT_KEY}
    assert {row.company_id for row in stored} == {"stripe"}
    assert all(row.first_seen_at == row.last_seen_at == clock() for row in stored)

    assert job_evidence(database) == {
        "watcher-job-1": (DIRECT_KEY, seen_at, 0, None, None)
    }
    [observation] = observations(database)
    assert (
        observation.import_run_id,
        observation.source_key,
        observation.source_kind,
        observation.adapter,
        observation.observed_at,
        observation.attempted,
        observation.succeeded,
        observation.complete,
        observation.incomplete,
        observation.truncated,
        observation.degraded,
        observation.rows_returned,
        observation.identity_count,
        observation.config_fingerprint,
    ) == (
        result.run_id,
        DIRECT_KEY,
        "direct",
        "workday",
        seen_at,
        True,
        True,
        True,
        False,
        False,
        False,
        1,
        2,
        CONFIG_FINGERPRINT,
    )

    counters = result.identity_shadow.counters
    assert counters["new_legacy_inserted"] == 1
    assert counters["inserted"] == 2
    assert counters["jobs_source_key_set"] == 1
    assert counters["last_direct_seen_updated"] == 1
    assert counters["observations_written"] == 1


def test_shadow_telemetry_is_bounded_and_privacy_safe(database, catalog, clock) -> None:
    secret_title = "Secret Title Do Not Log"
    result = run_import(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="REQ-SECRET-9", title=f"Backend Engineer Intern {secret_title}")],
        "a",
    )
    lines = import_summary_lines(result)
    shadow_lines = [line for line in lines if line.startswith("HOSTED-IDENTITY-SHADOW")]
    assert [line.split(" ", 1)[0] for line in shadow_lines] == [
        "HOSTED-IDENTITY-SHADOW",
        "HOSTED-IDENTITY-SHADOW-IDENTITIES",
        "HOSTED-IDENTITY-SHADOW-SOURCES",
    ]
    text_output = "\n".join(shadow_lines)
    for forbidden in (
        secret_title,
        "req-secret-9",
        "REQ-SECRET-9",
        "myworkdayjobs",
        "http",
        "Stripe",
        "stripe",
        "watcher-job",
        DIRECT_KEY,
        "@",
    ):
        assert forbidden not in text_output
    for line in shadow_lines:
        for pair in line.split(" ")[1:]:
            name, value = pair.split("=")
            assert name.replace("_", "").isalpha()
            assert value.isdigit() or value == "ok"


def test_title_change_and_collision_suffix_churn_are_reported_not_applied(
    database, catalog, clock
) -> None:
    run_import(database, catalog, clock, [direct_job(1, requisition="R1", id="abc1234567")], "a")
    clock.advance(hours=1)
    suffix_id = "abc1234567-" + "0" * 64
    churn = run_import(
        database, catalog, clock, [direct_job(1, requisition="R1", id=suffix_id)], "b"
    )
    clock.advance(hours=1)
    retitled = run_import(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="R1", id="retitled01", title="Platform Engineer Intern")],
        "c",
    )

    for result in (churn, retitled):
        assert result.counters.jobs_inserted == 1
        assert result.identity_shadow.counters["disagree"] == 1
        assert result.identity_shadow.counters["ownership_conflicts"] == 2
    # Legacy behavior is unchanged: three jobs, all open, nothing superseded,
    # and the identities still name the job that first recorded them.
    with database.session_factory() as db:
        jobs = db.scalars(select(HostedJob)).all()
    assert sorted(job.watcher_job_id for job in jobs) == sorted(
        ["abc1234567", suffix_id, "retitled01"]
    )
    assert all(job.is_open for job in jobs)
    assert set(identities(database).values()) == {"abc1234567"}
    assert {values[3:] for values in job_evidence(database).values()} == {(None, None)}


def test_cross_source_backstop_resolves_to_the_direct_job_without_merging(
    database, catalog, clock
) -> None:
    run_import(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="R7", url=WORKDAY_URL.format("R8"))],
        "a",
    )
    clock.advance(hours=1)
    # The direct source is down; the backstop lists the same Workday posting
    # under a different legacy ID.
    result = run_import(
        database,
        catalog,
        clock,
        [backstop_row(2, url=WORKDAY_URL.format("R8"), listing_id="L-8")],
        "b",
        source_evidence=evidence(
            attempt(succeeded=False, rows_returned=None, incomplete=True, complete=False),
            attempt(FEED_KEY, source_kind="github_feed", adapter="github_listings", company=None),
        ),
    )
    counters = result.identity_shadow.counters
    assert counters["disagree"] == 1
    assert counters["ownership_conflicts"] == 2
    assert counters["inserted"] == 1  # only the new listing identity
    assert identities(database) == {
        ("requisition", requisition_key("R7")): "watcher-job-1",
        ("requisition", requisition_key("R8")): "watcher-job-1",
        ("url", url_key("R8")): "watcher-job-1",
        ("listing", f"{FEED_KEY}|l-8"): "watcher-job-2",
    }
    evidence_by_job = job_evidence(database)
    assert evidence_by_job["watcher-job-2"][:2] == (FEED_KEY, None)
    assert evidence_by_job["watcher-job-1"][0] == DIRECT_KEY


def test_backstop_never_takes_source_ownership_of_a_direct_job(
    database, catalog, clock
) -> None:
    run_import(database, catalog, clock, [direct_job(1)], "a")
    before = job_evidence(database)["watcher-job-1"]
    clock.advance(hours=1)
    result = run_import(
        database,
        catalog,
        clock,
        [backstop_row(1, url=WORKDAY_URL.format("R1"), listing_id="L-1")],
        "b",
        source_evidence=evidence(
            attempt(FEED_KEY, source_kind="github_feed", adapter="github_listings", company=None),
        ),
    )
    assert result.identity_shadow.counters["agree"] == 1
    assert result.identity_shadow.counters["jobs_source_key_set"] == 0
    assert job_evidence(database)["watcher-job-1"] == before


def test_missing_weak_and_batch_ambiguous_identities_are_not_written(
    database, catalog, clock
) -> None:
    shared = "https://stripe.com/jobs/shared-posting-77"
    result = run_import(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition=None, url="https://stripe.com/careers"),
            direct_job(2, requisition=None, url=shared),
            direct_job(3, requisition=None, url=shared),
        ],
        "a",
    )
    counters = result.identity_shadow.counters
    assert counters["no_strong_identity"] == 1
    assert counters["batch_ambiguous"] == 1
    assert counters["inserted"] == 0
    assert identities(database) == {}
    assert result.counters.jobs_inserted == 3


def test_identities_owned_by_different_jobs_are_a_conflict_and_never_reassigned(
    database, catalog, clock
) -> None:
    run_import(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", url="https://stripe.com/careers"),
            direct_job(2, requisition=None, url="https://stripe.com/jobs/posting-2"),
        ],
        "a",
    )
    before = identities(database)
    clock.advance(hours=1)
    result = run_import(
        database,
        catalog,
        clock,
        [direct_job(3, requisition="R1", url="https://stripe.com/jobs/posting-2")],
        "b",
    )
    assert result.identity_shadow.counters["conflict"] == 1
    assert result.identity_shadow.counters["ownership_conflicts"] == 2
    assert identities(database) == before
    with database.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(HostedJob)) == 3
        assert db.scalar(
            select(func.count()).select_from(HostedJob).where(HostedJob.is_open.is_(False))
        ) == 0


def test_source_attempt_outcomes_never_infer_completeness(
    database, catalog, clock
) -> None:
    result = run_import(
        database,
        catalog,
        clock,
        [direct_job(1)],
        "a",
        source_evidence=evidence(
            attempt(complete=True, incomplete=False),
            attempt("company:unknown:direct:greenhouse", adapter="greenhouse", rows_returned=4),
            attempt(
                "company:failed:direct:workday",
                succeeded=False,
                rows_returned=None,
                incomplete=True,
                complete=False,
            ),
            attempt(
                "company:truncated:direct:workday",
                rows_returned=50,
                truncated=True,
                degraded=True,
                incomplete=True,
                complete=False,
            ),
            attempt(
                "company:skipped:direct:workday",
                attempted=False,
                succeeded=None,
                rows_returned=None,
            ),
            attempt(
                "company:contradictory:direct:workday",
                succeeded=False,
                rows_returned=None,
                complete=True,
            ),
            attempt(FEED_KEY, source_kind="github_feed", adapter="github_listings", company=None, rows_returned=0),
            attempt(DIRECT_KEY, rows_returned=99),  # duplicate key: first wins
            attempt("company:bad:other:x", source_kind="unknown_kind"),
        ),
    )
    stored = {row.source_key: row for row in observations(database)}
    assert set(stored) == {
        DIRECT_KEY,
        "company:unknown:direct:greenhouse",
        "company:failed:direct:workday",
        "company:truncated:direct:workday",
        "company:skipped:direct:workday",
        "company:contradictory:direct:workday",
        FEED_KEY,
    }

    def outcome(key: str) -> tuple:
        row = stored[key]
        return (
            row.attempted,
            row.succeeded,
            row.complete,
            row.incomplete,
            row.truncated,
            row.degraded,
            row.rows_returned,
            row.identity_count,
        )

    assert outcome(DIRECT_KEY) == (True, True, True, False, None, None, 1, 2)
    assert outcome("company:unknown:direct:greenhouse") == (True, True, None, None, None, None, 4, 0)
    assert outcome("company:failed:direct:workday") == (True, False, False, True, None, None, None, None)
    assert outcome("company:truncated:direct:workday") == (True, True, False, True, True, True, 50, 0)
    assert outcome("company:skipped:direct:workday") == (False, None, None, None, None, None, None, None)
    assert outcome("company:contradictory:direct:workday") == (True, False, None, None, None, None, None, None)
    assert outcome(FEED_KEY) == (True, True, None, None, None, None, 0, 0)

    counters = result.identity_shadow.counters
    assert counters["attempts"] == 9
    assert counters["observations_written"] == 7
    assert counters["complete"] == 1
    assert counters["inconsistent"] == 1
    assert counters["duplicate_source_keys"] == 1
    assert counters["invalid_attempts"] == 1
    assert counters["not_attempted"] == 1
    assert counters["truncated"] == 1 and counters["degraded"] == 1
    # The duplicate and the invalid attempt are not counted as outcomes.
    assert (counters["direct"], counters["github_feed"]) == (6, 1)
    assert (counters["succeeded"], counters["failed"]) == (4, 2)
    assert counters["complete_unknown"] == 4


def test_imports_without_attempt_metadata_record_identities_only(
    database, catalog, clock
) -> None:
    result = run_import(
        database,
        catalog,
        clock,
        [direct_job(1)],
        "a",
        source_evidence=SourceEvidence(attempts=()),
    )
    assert len(identities(database)) == 2
    assert observations(database) == []
    assert job_evidence(database)["watcher-job-1"][:2] == (None, None)
    assert result.identity_shadow.counters["jobs_source_unmatched"] == 0


# --- Idempotency and concurrency ----------------------------------------


def test_repeated_and_replayed_imports_never_duplicate_evidence(
    database, catalog, clock
) -> None:
    first = run_import(database, catalog, clock, [direct_job(1)], "a")
    first_seen = clock()
    repeated = run_import(database, catalog, clock, [direct_job(1)], "a")
    assert repeated.outcome == "already_imported"
    assert repeated.identity_shadow is None

    clock.advance(hours=2)
    later = run_import(
        database,
        catalog,
        clock,
        [direct_job(1)],
        "b",
        source_evidence=evidence(
            attempt(observed_at=datetime(2026, 8, 2, 13, 50, tzinfo=UTC), complete=True)
        ),
    )
    assert later.identity_shadow.counters["agree"] == 1
    assert later.identity_shadow.counters["refreshed"] == 2
    assert later.identity_shadow.counters["inserted"] == 0
    with database.session_factory() as db:
        stored = db.scalars(select(HostedJobIdentity)).all()
    assert len(stored) == 2
    assert {(row.first_seen_at, row.last_seen_at) for row in stored} == {(first_seen, clock())}
    assert job_evidence(database)["watcher-job-1"][1] == datetime(2026, 8, 2, 13, 50, tzinfo=UTC)
    assert len(observations(database)) == 2

    # Replaying the shadow pass for an already-recorded run adds nothing.
    with database.session_factory() as db:
        job = db.scalar(select(HostedJob))
    mapped = map_final_jobs([direct_job(1)], catalog).jobs[0]
    replay = run_identity_shadow(
        database,
        import_run_id=first.run_id,
        observed_at=clock(),
        final_jobs=[direct_job(1)],
        imported=[ImportedJob(mapped, job.id, "unchanged")],
        evidence=evidence(),
    )
    assert replay.status == "ok"
    assert replay.counters["observations_written"] == 0
    assert replay.counters["observations_existing"] == 1
    assert len(observations(database)) == 2
    assert len(identities(database)) == 2

    # An older observation never rewinds last-direct-seen.
    clock.advance(hours=1)
    run_import(
        database,
        catalog,
        clock,
        [direct_job(1)],
        "c",
        source_evidence=evidence(attempt(observed_at=datetime(2026, 8, 1, tzinfo=UTC))),
    )
    assert job_evidence(database)["watcher-job-1"][1] == datetime(2026, 8, 2, 13, 50, tzinfo=UTC)


def _psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace("postgresql+psycopg://", "postgresql://", 1)


def _two_jobs(database, catalog, clock) -> tuple[uuid.UUID, uuid.UUID]:
    run_import(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition=None, url="https://stripe.com/careers"),
            direct_job(2, requisition=None, url="https://stripe.com/careers"),
        ],
        "a",
        shadow=False,
    )
    with database.session_factory() as db:
        ids = {
            job.watcher_job_id: job.id for job in db.scalars(select(HostedJob))
        }
    return ids["watcher-job-1"], ids["watcher-job-2"]


def _shadow_claim(database, catalog, clock, job_id, run_id, *, requisition="R50"):
    mapped = map_final_jobs([direct_job(2, requisition=requisition)], catalog).jobs[0]
    return run_identity_shadow(
        database,
        import_run_id=run_id,
        observed_at=clock(),
        final_jobs=[direct_job(2, requisition=requisition)],
        imported=[ImportedJob(mapped, job_id, "unchanged")],
        evidence=None,
    )


def test_overlapping_shadow_transactions_keep_the_first_owner(
    database, catalog, clock, notification_postgres_url
) -> None:
    first_job, second_job = _two_jobs(database, catalog, clock)
    with database.session_factory() as db:
        run_id = db.scalar(select(HostedJobImportRun.id))
    key = requisition_key("R50")
    report: dict[str, object] = {}

    with psycopg.connect(_psycopg_url(notification_postgres_url)) as competitor:
        competitor.execute(
            "INSERT INTO hosted_job_identities (id, job_id, company_id, kind, "
            "identity_key, identity_digest, first_seen_at, last_seen_at) "
            "VALUES (%s, %s, 'stripe', 'requisition', %s, %s, now(), now())",
            (uuid.uuid4(), first_job, key, identity_digest(key)),
        )
        worker = threading.Thread(
            target=lambda: report.update(
                result=_shadow_claim(database, catalog, clock, second_job, run_id)
            )
        )
        worker.start()
        time.sleep(0.5)
        assert worker.is_alive()  # waiting on the competitor's uncommitted key
        competitor.commit()
    worker.join(timeout=10)

    result = report["result"]
    assert result.status == "ok"
    assert result.counters["ownership_conflicts"] >= 1
    assert identities(database)[("requisition", key)] == "watcher-job-1"
    with database.session_factory() as db:
        assert db.scalar(
            select(func.count()).select_from(HostedJobIdentity).where(
                HostedJobIdentity.identity_key == key
            )
        ) == 1


def test_shadow_skips_job_rows_locked_by_a_running_import(
    database, catalog, clock, notification_postgres_url
) -> None:
    _first_job, second_job = _two_jobs(database, catalog, clock)
    with database.session_factory() as db:
        run_id = db.scalar(select(HostedJobImportRun.id))
    with psycopg.connect(_psycopg_url(notification_postgres_url)) as running_import:
        running_import.execute(
            "SELECT id FROM hosted_jobs WHERE id = %s FOR UPDATE", (second_job,)
        )
        started = time.monotonic()
        result = _shadow_claim(database, catalog, clock, second_job, run_id)
        elapsed = time.monotonic() - started
        running_import.rollback()
    assert result.status == "ok"
    assert elapsed < 3
    assert result.counters["jobs_skipped_locked"] == 1
    assert identities(database) == {}


# --- Failure isolation --------------------------------------------------


@pytest.mark.parametrize(
    ("target", "stage"),
    [
        ("_plan", "derive"),
        ("_upsert_identities", "identity_upsert"),
        ("_record_job_evidence", "job_evidence"),
        ("_record_observations", "source_observations"),
    ],
)
def test_shadow_failure_rolls_back_only_shadow_work(
    database, catalog, clock, monkeypatch, target, stage
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "watcher@example.com")
    original = getattr(identity_shadow, target)

    def failing(*args, **kwargs):
        if target != "_plan":
            original(*args, **kwargs)
        raise RuntimeError("secret detail https://example.com/token")

    monkeypatch.setattr(identity_shadow, target, failing)
    result = run_import(database, catalog, clock, [direct_job(1)], "a")

    assert result.outcome == "imported"
    assert result.counters.jobs_inserted == 1
    assert result.counters.matches_created == 1
    assert result.identity_shadow.status == "failed"
    assert result.identity_shadow.lines() == [
        f"HOSTED-IDENTITY-SHADOW status=failed stage={stage} error=RuntimeError"
    ]
    with database.session_factory() as db:
        run = db.scalar(select(HostedJobImportRun))
        assert run.status == "succeeded" and run.failure_summary is None
        assert db.scalar(select(func.count()).select_from(HostedNotificationItem)) == 1
    assert identities(database) == {}
    assert observations(database) == []
    assert job_evidence(database)["watcher-job-1"] == (None, None, 0, None, None)


def test_database_errors_in_shadow_work_never_fail_the_import(
    database, catalog, clock, monkeypatch
) -> None:
    monkeypatch.setattr(identity_shadow, "_STATEMENT_TIMEOUT", "not-a-duration")
    result = run_import(database, catalog, clock, [direct_job(1)], "a")
    assert result.outcome == "imported"
    assert result.identity_shadow.status == "failed"
    assert result.identity_shadow.error in {"DataError", "ProgrammingError", "InvalidParameterValue"}
    with database.session_factory() as db:
        assert db.scalar(select(HostedJobImportRun.status)) == "succeeded"
        assert db.scalar(select(func.count()).select_from(HostedJob)) == 1


def test_a_failed_import_records_no_shadow_evidence(database, catalog, clock, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.hosted.job_import.reconcile_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(Exception):
        run_import(database, catalog, clock, [direct_job(1)], "a")
    with database.session_factory() as db:
        assert db.scalar(select(HostedJobImportRun.status)) == "failed"
        assert db.scalar(select(func.count()).select_from(HostedJob)) == 0
    assert identities(database) == {}
    assert observations(database) == []


# --- Snapshot path ------------------------------------------------------


def _snapshot(tmp_path: Path) -> Path:
    config = load_watchlist()
    row = make_row(
        source="direct",
        source_adapter="workday",
        company="Capital One",
        title="Backend Software Engineer Intern",
        location="New York, NY",
        description="Build backend APIs and production services.",
        requirements="Python and SQL",
        source_url=(
            "https://capitalone.wd12.myworkdayjobs.com/Capital_One/job/"
            "New-York-NY/Backend-Software-Engineer-Intern_R-555"
        ),
        date_posted="2026-08-01",
        internship_type="Summer 2027 Internship",
        extra={"source_requisition_id": "R555", "active": True},
    )
    key = direct_health_key("Capital One", "workday")
    batch = CollectionBatch.create(
        captured_at=datetime(2026, 8, 2, 9, 0, tzinfo=UTC),
        collection_config_fingerprint=collection_config_fingerprint(config),
        rows=[row],
        errors=[],
        source_attempts=[
            attempt(
                key,
                company="Capital One",
                observed_at=datetime(2026, 8, 2, 8, 55, tzinfo=UTC),
                complete=True,
                incomplete=False,
                truncated=False,
                degraded=False,
                malformed_row_count=0,
                schema_error_row_count=0,
                duplicate_row_count=0,
                failed_request_count=0,
            ),
            attempt(
                "company:other:direct:workday",
                company="Other",
                observed_at=datetime(2026, 8, 2, 8, 56, tzinfo=UTC),
                succeeded=False,
                rows_returned=None,
                error_kind="fetch",
            ),
        ],
    )
    snapshot = tmp_path / "shadow.json.gz"
    save_collection_snapshot(batch, snapshot)
    return snapshot


@pytest.mark.parametrize("enabled", [False, True])
def test_snapshot_import_passes_attempt_metadata_only_when_enabled(
    notification_postgres_url, tmp_path, monkeypatch, enabled
) -> None:
    snapshot = _snapshot(tmp_path)
    fingerprint = snapshot_sha256(snapshot)
    if enabled:
        monkeypatch.setenv(SHADOW_FLAG_ENV, "true")
    else:
        monkeypatch.delenv(SHADOW_FLAG_ENV, raising=False)

    result = import_snapshot_into_hosted(snapshot, database_url=notification_postgres_url)
    database = HostedDatabase(notification_postgres_url)
    try:
        assert result.outcome == "imported"
        assert result.source_fingerprint == fingerprint == snapshot_sha256(snapshot)
        if not enabled:
            assert result.identity_shadow is None
            assert identities(database) == {}
            assert observations(database) == []
            return
        assert result.identity_shadow.status == "ok"
        stored = {row.source_key: row for row in observations(database)}
        key = direct_health_key("Capital One", "workday")
        assert set(stored) == {key, "company:other:direct:workday"}
        assert stored[key].complete is True and stored[key].identity_count == 2
        assert stored["company:other:direct:workday"].succeeded is False
        assert stored["company:other:direct:workday"].complete is None
        assert stored[key].config_fingerprint == collection_config_fingerprint(load_watchlist())
        [job] = job_evidence(database).values()
        assert job[:2] == (key, datetime(2026, 8, 2, 8, 55, tzinfo=UTC))
        assert {kind for kind, _key in identities(database)} == {"requisition", "url"}
    finally:
        database.dispose()


def test_collect_and_import_reports_shadow_lines_when_enabled(
    notification_postgres_url, tmp_path, monkeypatch, capsys
) -> None:
    from app.hosted import collect_and_import
    from watcher.collection_snapshot import load_collection_snapshot

    batch = load_collection_snapshot(_snapshot(tmp_path))
    monkeypatch.setenv("HOSTED_DATABASE_URL", notification_postgres_url)
    monkeypatch.setenv(SHADOW_FLAG_ENV, "1")
    assert collect_and_import.main([], collector=lambda _config: batch) == 0
    output = capsys.readouterr().out
    assert "HOSTED-IDENTITY-SHADOW status=ok" in output
    assert "HOSTED-IDENTITY-SHADOW-SOURCES attempts=2 observations_written=2" in output
    assert "myworkdayjobs" not in output and "R555" not in output


def test_shadow_writes_preserve_lifecycle_columns(database, catalog, clock) -> None:
    run_import(database, catalog, clock, [direct_job(1)], "a")
    clock.advance(hours=1)
    run_import(
        database,
        catalog,
        clock,
        [
            direct_job(
                1,
                extra={
                    "source": "direct",
                    "source_adapter": "workday",
                    "source_requisition_id": "R1",
                    "active": False,
                },
            )
        ],
        "b",
    )
    with database.session_factory() as db:
        job = db.scalar(select(HostedJob))
        assert job.is_open is False and job.closed_at == clock()
    assert job_evidence(database)["watcher-job-1"][2:] == (0, None, None)


def test_identity_timestamps_follow_the_import_clock(database, catalog, clock) -> None:
    run_import(database, catalog, clock, [direct_job(1)], "a")
    clock.advance(days=1)
    run_import(database, catalog, clock, [direct_job(1)], "b")
    with database.session_factory() as db:
        rows = db.scalars(select(HostedJobIdentity)).all()
    assert {row.last_seen_at - row.first_seen_at for row in rows} == {timedelta(days=1)}


def test_a_future_attempt_time_never_pins_last_direct_seen_ahead(
    database, catalog, clock
) -> None:
    run_import(
        database,
        catalog,
        clock,
        [direct_job(1)],
        "a",
        source_evidence=evidence(attempt(observed_at=clock() + timedelta(days=30))),
    )
    assert job_evidence(database)["watcher-job-1"][1] == clock()
    clock.advance(hours=2)
    later = clock() - timedelta(minutes=5)
    run_import(
        database,
        catalog,
        clock,
        [direct_job(1)],
        "b",
        source_evidence=evidence(attempt(observed_at=later)),
    )
    assert job_evidence(database)["watcher-job-1"][1] == later
