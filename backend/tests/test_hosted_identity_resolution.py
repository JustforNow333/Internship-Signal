"""Phase 3 guarded resolution of incoming postings to existing hosted jobs."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime

import psycopg
import pytest
from app.hosted import identity_shadow, job_import
from app.hosted.identity_shadow import (
    RESOLUTION_FLAG_ENV,
    RESOLUTION_REASONS,
    RESOLVE_COUNTER_NAMES,
    SHADOW_FLAG_ENV,
    JobIdentity,
    ResolutionRow,
    ResolutionTarget,
    decide_resolution,
    decide_resolutions,
    identity_resolution_enabled,
)
from app.hosted.import_snapshot import import_snapshot_into_hosted, import_summary_lines
from app.hosted.job_import import ImportLockUnavailable, JobImportService, JobUpsertFailed
from app.hosted.database import HostedDatabase
from app.hosted.models import (
    HostedJob,
    HostedJobIdentity,
    HostedJobImportRun,
    HostedNotificationBatch,
    HostedNotificationItem,
    UserJobMatch,
)
from sqlalchemy import event, func, select, text

from tests.test_hosted_identity_shadow import (  # noqa: F401 - helpers and fixtures
    FEED_KEY,
    WORKDAY_URL,
    _psycopg_url,
    _snapshot,
    _state,
    _truncate,
    attempt,
    backstop_row,
    catalog,
    direct_job,
    evidence,
    identities,
    requisition_key,
    url_key,
)
from tests.test_hosted_notifications import (  # noqa: F401 - pytest fixtures
    MutableClock,
    clean_database,
    clock,
    create_user,
    database,
    notification_postgres_url,
)

SUFFIX_A = "0" * 64
SUFFIX_B = "1" * 64
LOCK_SQL = "SELECT pg_advisory_lock(hashtextextended('hosted-job-import', 0))"
UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended('hosted-job-import', 0))"


def run(
    database,
    catalog,
    clock,
    jobs,
    marker,
    *,
    shadow=True,
    resolution=False,
    source_evidence=None,
    retry_failed=False,
    advance=True,
):
    if advance:
        clock.advance(hours=1)
    service = JobImportService(
        database,
        catalog,
        clock=clock,
        identity_shadow=shadow,
        identity_resolution=resolution,
    )
    return service.import_jobs(
        jobs,
        source_fingerprint=marker * 64,
        source_identifier=f"snapshot-{marker}.json.gz",
        source_type="collection_snapshot",
        retry_failed=retry_failed,
        source_evidence=source_evidence if source_evidence is not None else evidence(),
    )


def seed(database, catalog, clock, jobs, marker, **options):
    """A pre-Phase-3 import: shadow tracking on, resolution off."""

    return run(database, catalog, clock, jobs, marker, resolution=False, **options)


def enforce(database, catalog, clock, jobs, marker, **options):
    return run(database, catalog, clock, jobs, marker, resolution=True, **options)


def jobs_state(database) -> dict[str, dict[str, object]]:
    with database.session_factory() as db:
        return {
            job.watcher_job_id: {
                "id": job.id,
                "title": job.title,
                "location": job.location,
                "description": job.description,
                "application_url": job.application_url,
                "is_open": job.is_open,
                "closed_at": job.closed_at,
                "first_seen_at": job.first_seen_at,
                "last_seen_at": job.last_seen_at,
                "updated_at": job.updated_at,
            }
            for job in db.scalars(select(HostedJob))
        }


def counts(database) -> tuple[int, int, int]:
    with database.session_factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (HostedJob, UserJobMatch, HostedNotificationItem)
        )


def owner_rows(database) -> dict[tuple[str, str], tuple[str, datetime]]:
    with database.session_factory() as db:
        rows = db.execute(
            select(
                HostedJobIdentity.kind,
                HostedJobIdentity.identity_key,
                HostedJob.watcher_job_id,
                HostedJobIdentity.first_seen_at,
            ).join(HostedJob, HostedJob.id == HostedJobIdentity.job_id)
        ).all()
    return {(kind, key): (watcher_id, first) for kind, key, watcher_id, first in rows}


def resolve(result) -> dict[str, int]:
    report = result.identity_resolution
    assert report is not None and report.status == "ok"
    return dict(report.counters)


def nonzero(counters) -> dict[str, int]:
    return {name: value for name, value in counters.items() if value}


# --- Flag -----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "  ", "0", "false", "off", "maybe", "enabled"])
def test_resolution_flag_is_off_unless_explicitly_true(monkeypatch, value) -> None:
    monkeypatch.setenv(SHADOW_FLAG_ENV, "true")
    if value is None:
        monkeypatch.delenv(RESOLUTION_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(RESOLUTION_FLAG_ENV, value)
    assert identity_resolution_enabled() is False


@pytest.mark.parametrize("shadow", [None, "", "false", "maybe"])
def test_resolution_flag_requires_shadow_tracking(monkeypatch, shadow) -> None:
    monkeypatch.setenv(RESOLUTION_FLAG_ENV, "true")
    if shadow is None:
        monkeypatch.delenv(SHADOW_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(SHADOW_FLAG_ENV, shadow)
    assert identity_resolution_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", " yes ", "on"])
def test_resolution_flag_accepts_explicit_true_with_shadow(monkeypatch, value) -> None:
    monkeypatch.setenv(SHADOW_FLAG_ENV, "1")
    monkeypatch.setenv(RESOLUTION_FLAG_ENV, value)
    assert identity_resolution_enabled() is True


def test_service_never_resolves_without_shadow_tracking(database, catalog, clock) -> None:
    seed(database, catalog, clock, [direct_job(1, id="A")], "a")
    result = run(
        database,
        catalog,
        clock,
        [direct_job(1, id="B")],
        "b",
        shadow=False,
        resolution=True,
    )
    assert result.identity_shadow is None
    assert result.identity_resolution is None
    assert result.counters.jobs_inserted == 1
    assert set(jobs_state(database)) == {"A", "B"}


# --- Pure decisions ---------------------------------------------------------

COMPANY = "stripe"
X = uuid.UUID(int=1)
Y = uuid.UUID(int=2)
T = uuid.UUID(int=3)
R1 = JobIdentity("requisition", "workday|stripe_stripe|r1")
R2 = JobIdentity("requisition", "workday|stripe_stripe|r2")
U1 = JobIdentity("url", "https://stripe.wd5.myworkdayjobs.com/stripe/job/intern_r1")
U2 = JobIdentity("url", "https://stripe.com/jobs/posting-2")
L1 = JobIdentity("listing", "github:feed|l-1")
G_NATIVE = JobIdentity("requisition", "google|stripe|111")
G_URL = JobIdentity("requisition", "google_careers|www_google_com|111")
G_OTHER = JobIdentity("requisition", "google_careers|www_google_com|999")
UNSCOPED = JobIdentity("requisition", "not-a-scoped-key")


def owned(*pairs) -> dict[tuple[str, str, str], uuid.UUID]:
    return {(COMPANY, identity.kind, identity.digest): job for identity, job in pairs}


def row(*items, legacy=None, rejected=0, backstop=False) -> ResolutionRow:
    return ResolutionRow(
        company_id=COMPANY,
        identities=tuple(items),
        legacy_target=legacy,
        rejected=rejected,
        backstop=backstop,
    )


def target(job_id, *requisitions, is_open=True, superseded=False, direct=True):
    return ResolutionTarget(
        job_id=job_id,
        is_open=is_open,
        superseded=superseded,
        direct=direct,
        requisitions=tuple(item.key for item in requisitions),
    )


PURE_CASES = {
    "no identity": (row(), {}, {}, "no_strong_identity"),
    "unowned": (row(R1, U1), {}, {}, "unowned"),
    "agree": (row(R1, U1, legacy=X), owned((R1, X), (U1, X)), {X: target(X, R1)}, "agree"),
    "resolve": (row(R1, U1, legacy=T), owned((R1, X), (U1, X)), {X: target(X, R1)}, "resolve"),
    "resolve new row": (row(R1, U1), owned((R1, X)), {X: target(X, R1)}, "resolve"),
    "conflict": (row(R1, U1), owned((R1, X), (U1, Y)), {X: target(X, R1), Y: target(Y)}, "conflict"),
    "conflict with legacy owner": (
        row(R1, U1, legacy=X),
        owned((R1, X), (U1, Y)),
        {X: target(X, R1), Y: target(Y)},
        "conflict",
    ),
    "rejected identity": (row(R1, rejected=1), owned((R1, X)), {X: target(X, R1)}, "weak_identity"),
    "unscoped requisition": (row(UNSCOPED), owned((UNSCOPED, X)), {X: target(X)}, "weak_identity"),
    "same-scope native/url disagreement": (
        row(R1, R2, U1),
        owned((R1, X)),
        {X: target(X, R1)},
        "row_native_url_disagreement",
    ),
    "cross-provider native/url pair": (
        row(G_NATIVE, G_URL),
        owned((G_NATIVE, X), (G_URL, X)),
        {X: target(X, G_NATIVE, G_URL)},
        "resolve",
    ),
    "url owned, native requisition unowned": (
        row(R1, U1),
        owned((U1, X)),
        {X: target(X)},
        "primary_not_owned",
    ),
    "cross-provider url id owned, native unowned": (
        row(G_NATIVE, G_OTHER),
        owned((G_OTHER, Y)),
        {Y: target(Y, G_OTHER)},
        "primary_not_owned",
    ),
    "target missing": (row(R1), owned((R1, X)), {}, "target_missing"),
    "target superseded": (
        row(R1),
        owned((R1, X)),
        {X: target(X, R1, is_open=False, superseded=True)},
        "target_superseded",
    ),
    "target closed": (row(R1), owned((R1, X)), {X: target(X, R1, is_open=False)}, "target_closed"),
    "target owns two same-scope requisitions": (
        row(R1),
        owned((R1, X)),
        {X: target(X, R1, R2)},
        "target_ambiguous",
    ),
    "target owns an unscoped requisition": (
        row(R1),
        owned((R1, X)),
        {X: target(X, R1, UNSCOPED)},
        "target_ambiguous",
    ),
    "target owns a different requisition in a row scope": (
        row(G_NATIVE, G_URL),
        owned((G_NATIVE, X)),
        {X: target(X, G_NATIVE, G_OTHER)},
        "target_requisition_mismatch",
    ),
    "direct url-only row and a requisition target": (
        row(U2),
        owned((U2, X)),
        {X: target(X, R1)},
        "target_requisition_unverified",
    ),
    "backstop url-only row and a direct target": (
        row(U2, L1, backstop=True),
        owned((U2, X)),
        {X: target(X, R1, direct=True)},
        "resolve",
    ),
    "backstop url-only row and a backstop requisition target": (
        row(U2, backstop=True),
        owned((U2, X)),
        {X: target(X, R1, direct=False)},
        "target_requisition_unverified",
    ),
    "url-only row and url-only target": (row(U2), owned((U2, X)), {X: target(X)}, "resolve"),
}


@pytest.mark.parametrize("case", sorted(PURE_CASES))
def test_decide_resolution_guards(case) -> None:
    candidate, owners, targets, expected = PURE_CASES[case]
    decision = decide_resolution(candidate, owners=owners, targets=targets)
    assert decision.outcome == expected
    assert decision.resolves is (expected == "resolve")
    if expected == "resolve":
        assert decision.target_id in owners.values()
    else:
        assert decision.target_id is None
    assert expected in {"resolve", "agree", "unowned", "no_strong_identity", *RESOLUTION_REASONS}


def test_decide_resolution_rejects_batch_ambiguous_identities() -> None:
    claim = (COMPANY, U1.kind, U1.digest)
    decision = decide_resolution(
        row(R1, U1, legacy=T),
        owners=owned((R1, X), (U1, X)),
        targets={X: target(X, R1)},
        batch_ambiguous=frozenset({claim}),
    )
    assert decision.outcome == "batch_ambiguous" and decision.target_id is None


def test_decide_resolutions_rejects_contention_in_any_order() -> None:
    targets = {X: target(X)}
    owners = owned((U1, X), (U2, X))
    pair = [row(U1), row(U2)]
    assert [d.outcome for d in decide_resolutions(pair, owners=owners, targets=targets)] == [
        "target_contention",
        "target_contention",
    ]

    # The target's own watcher ID arrives on another row of the batch.
    other = JobIdentity("requisition", "workday|stripe_stripe|r9")
    rows = [row(R1, U1), row(other, legacy=X)]
    owners = owned((R1, X), (U1, X))
    targets = {X: target(X, R1)}
    forward = decide_resolutions(rows, owners=owners, targets=targets)
    backward = decide_resolutions(rows[::-1], owners=owners, targets=targets)
    assert [d.outcome for d in forward] == ["target_contention", "unowned"]
    assert [d.outcome for d in backward] == ["unowned", "target_contention"]


def test_decide_resolutions_rejects_identities_claimed_by_two_rows() -> None:
    owners = owned((R1, X), (U1, X))
    targets = {X: target(X, R1)}
    decisions = decide_resolutions([row(R1, U1), row(R1, U1)], owners=owners, targets=targets)
    assert [d.outcome for d in decisions] == ["batch_ambiguous", "batch_ambiguous"]
    unowned = decide_resolutions([row(R1, U1), row(R1, U1)], owners={}, targets={})
    assert [d.outcome for d in unowned] == ["unowned", "unowned"]


# --- Churn, retitles, and twins ----------------------------------------------


def test_collision_suffix_churn_resolves_in_both_directions(database, catalog, clock) -> None:
    legacy_r1 = "abc1234567"
    legacy_r2 = "def1234567"
    first = enforce(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id=legacy_r1),
            direct_job(2, requisition="R2", id=f"{legacy_r2}-{SUFFIX_B}"),
        ],
        "a",
    )
    assert first.counters.jobs_inserted == 2
    assert nonzero(resolve(first)) == {"rows": 2, "unowned": 2}
    original = jobs_state(database)

    flipped = enforce(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id=f"{legacy_r1}-{SUFFIX_A}"),
            direct_job(2, requisition="R2", id=legacy_r2),
        ],
        "b",
    )
    assert flipped.counters.jobs_inserted == 0
    assert nonzero(resolve(flipped)) == {"rows": 2, "would_resolve": 2, "resolved": 2}

    back = enforce(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id=legacy_r1),
            direct_job(2, requisition="R2", id=f"{legacy_r2}-{SUFFIX_B}"),
        ],
        "c",
    )
    assert back.counters.jobs_inserted == 0
    assert nonzero(resolve(back)) == {"rows": 2, "agree": 2}

    final = jobs_state(database)
    assert set(final) == {legacy_r1, f"{legacy_r2}-{SUFFIX_B}"}
    for watcher_id, values in final.items():
        assert values["id"] == original[watcher_id]["id"]
        assert values["last_seen_at"] == clock()


def test_repeated_identity_oscillation_keeps_one_job_one_match_one_alert(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "watcher@example.com")
    outcomes = []
    for marker, watcher_id in zip("abcd", ["A", "B", "A", "B"]):
        result = enforce(database, catalog, clock, [direct_job(1, id=watcher_id)], marker)
        outcomes.append(nonzero(resolve(result)))
    assert outcomes == [
        {"rows": 1, "unowned": 1},
        {"rows": 1, "would_resolve": 1, "resolved": 1},
        {"rows": 1, "agree": 1},
        {"rows": 1, "would_resolve": 1, "resolved": 1},
    ]
    assert set(jobs_state(database)) == {"A"}
    assert counts(database) == (1, 1, 1)


def test_existing_twin_groups_resolve_to_the_owner_and_twins_stay_untouched(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "watcher@example.com")
    # Production shape before Phase 3: churn has already created groups of
    # four (R1) and three (R2) jobs, and identities name the first of each.
    seed(database, catalog, clock, [direct_job(1, requisition="R1", id="g1-a"), direct_job(2, requisition="R2", id="g2-a")], "a")
    seed(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id="g1-b", title="Platform Engineer Intern"),
            direct_job(2, requisition="R2", id="g2-b"),
        ],
        "b",
    )
    seed(database, catalog, clock, [direct_job(1, requisition="R1", id="g1-c"), direct_job(2, requisition="R2", id="g2-c")], "c")
    seed(database, catalog, clock, [direct_job(1, requisition="R1", id="g1-d")], "d")
    assert counts(database) == (7, 7, 7)
    assert {value[0] for value in owner_rows(database).values()} == {"g1-a", "g2-a"}
    before = jobs_state(database)

    result = enforce(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id="g1-c", title="Infrastructure Engineer Intern"),
            direct_job(2, requisition="R2", id="g2-b"),
        ],
        "e",
    )

    assert nonzero(resolve(result)) == {"rows": 2, "would_resolve": 2, "resolved": 2}
    assert result.counters.jobs_inserted == 0
    assert result.counters.matches_created == 0
    after = jobs_state(database)
    assert after["g1-a"]["id"] == before["g1-a"]["id"]
    assert after["g1-a"]["title"] == "Infrastructure Engineer Intern"
    assert after["g1-a"]["last_seen_at"] == after["g2-a"]["last_seen_at"] == clock()
    for twin in ("g1-b", "g1-c", "g1-d", "g2-b", "g2-c"):
        assert after[twin] == before[twin]
    assert counts(database) == (7, 7, 7)


def test_resolution_follows_identity_ownership_even_to_the_newer_twin(
    database, catalog, clock
) -> None:
    run(database, catalog, clock, [direct_job(1, id="A")], "a", shadow=False)
    seed(database, catalog, clock, [direct_job(1, id="B")], "b")
    assert {value[0] for value in owner_rows(database).values()} == {"B"}
    before = jobs_state(database)

    result = enforce(database, catalog, clock, [direct_job(1, id="A")], "c")

    assert nonzero(resolve(result)) == {"rows": 1, "would_resolve": 1, "resolved": 1}
    after = jobs_state(database)
    assert after["A"] == before["A"]
    assert after["B"]["last_seen_at"] == clock()


def test_retitle_and_location_edit_update_the_existing_job_without_new_alerts(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "watcher@example.com")
    enforce(database, catalog, clock, [direct_job(1, id="A")], "a")
    original = jobs_state(database)["A"]

    retitled = enforce(
        database, catalog, clock, [direct_job(1, id="B", title="Platform Engineer Intern")], "b"
    )
    moved = enforce(
        database,
        catalog,
        clock,
        [direct_job(1, id="C", title="Platform Engineer Intern", location="Seattle, WA")],
        "c",
    )

    for result in (retitled, moved):
        assert nonzero(resolve(result)) == {"rows": 1, "would_resolve": 1, "resolved": 1}
        assert result.counters.jobs_inserted == 0
        assert result.counters.jobs_updated == 1
        assert result.counters.matches_created == 0
    state = jobs_state(database)
    assert set(state) == {"A"}
    assert state["A"]["id"] == original["id"]
    assert state["A"]["first_seen_at"] == original["first_seen_at"]
    assert (state["A"]["title"], state["A"]["location"]) == (
        "Platform Engineer Intern",
        "Seattle, WA",
    )
    assert counts(database) == (1, 1, 1)


def test_pending_and_sent_notifications_survive_resolution(database, catalog, clock) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "watcher@example.com")
    seed(database, catalog, clock, [direct_job(1, id="A")], "a")
    with database.session_factory.begin() as db:
        db.execute(
            text(
                "UPDATE hosted_notification_items SET status = 'sent', sent_at = :now"
            ),
            {"now": clock()},
        )
    # Legacy churn already created a twin with its own pending alert.
    seed(database, catalog, clock, [direct_job(1, id="B")], "b")

    def notification_state():
        with database.session_factory() as db:
            items = sorted(
                (str(row.id), str(row.user_job_match_id), row.status, row.sent_at)
                for row in db.scalars(select(HostedNotificationItem))
            )
            matches = sorted(
                (str(row.id), str(row.job_id), row.matched_at, row.no_longer_matches_at)
                for row in db.scalars(select(UserJobMatch))
            )
            batches = db.scalar(select(func.count()).select_from(HostedNotificationBatch))
        return items, matches, batches

    before = notification_state()
    assert sorted(item[2] for item in before[0]) == ["pending", "sent"]

    result = enforce(
        database, catalog, clock, [direct_job(1, id="B", title="Platform Engineer Intern")], "c"
    )

    assert nonzero(resolve(result)) == {"rows": 1, "would_resolve": 1, "resolved": 1}
    assert result.counters.matches_created == 0
    assert notification_state() == before
    state = jobs_state(database)
    assert state["A"]["title"] == "Platform Engineer Intern"
    assert state["B"]["title"] != "Platform Engineer Intern"


# --- Distinct postings are never merged ------------------------------------


def test_repost_with_a_new_requisition_is_never_resolved(database, catalog, clock) -> None:
    shared = "https://stripe.com/jobs/posting-55"
    seed(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="R1", id="p1"), direct_job(5, requisition="R5", id="p5", url=shared)],
        "a",
    )
    before = jobs_state(database)
    result = enforce(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="R2", id="p2"), direct_job(5, requisition="R6", id="p6", url=shared)],
        "b",
    )
    assert nonzero(resolve(result)) == {
        "rows": 2,
        "unowned": 1,
        "fallback": 1,
        "reject_primary_not_owned": 1,
    }
    assert result.counters.jobs_inserted == 2
    after = jobs_state(database)
    assert after["p1"] == before["p1"] and after["p5"] == before["p5"]


def test_distinct_postings_sharing_title_and_location_are_never_merged(
    database, catalog, clock
) -> None:
    enforce(database, catalog, clock, [direct_job(1, requisition="R1", id="L")], "a")
    # Two requisitions now share the legacy company/title/city ID, so the
    # watcher suffixes both.
    both = enforce(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id=f"L-{SUFFIX_A}"),
            direct_job(1, requisition="R2", id=f"L-{SUFFIX_B}"),
        ],
        "b",
    )
    assert nonzero(resolve(both)) == {"rows": 2, "would_resolve": 1, "resolved": 1, "unowned": 1}
    before = jobs_state(database)
    assert set(before) == {"L", f"L-{SUFFIX_B}"}

    # Only R2 remains, so it takes the unsuffixed ID that names R1's job.
    alone = enforce(database, catalog, clock, [direct_job(1, requisition="R2", id="L")], "c")

    assert nonzero(resolve(alone)) == {
        "rows": 1,
        "would_resolve": 1,
        "resolved": 1,
        "legacy_requisition_mismatch": 1,
    }
    after = jobs_state(database)
    assert after["L"] == before["L"]
    assert after[f"L-{SUFFIX_B}"]["last_seen_at"] == clock()
    assert owner_rows(database)[("requisition", requisition_key("R1"))][0] == "L"
    assert owner_rows(database)[("requisition", requisition_key("R2"))][0] == f"L-{SUFFIX_B}"


def test_same_requisition_with_a_url_owned_elsewhere_is_a_conflict(
    database, catalog, clock
) -> None:
    seed(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id="c1", url="https://stripe.com/careers"),
            direct_job(2, requisition=None, id="c2", url="https://stripe.com/jobs/posting-2"),
        ],
        "a",
    )
    before = owner_rows(database)
    result = enforce(
        database,
        catalog,
        clock,
        [direct_job(3, requisition="R1", id="c3", url="https://stripe.com/jobs/posting-2")],
        "b",
    )
    assert nonzero(resolve(result)) == {"rows": 1, "fallback": 1, "reject_conflict": 1}
    assert result.counters.jobs_inserted == 1
    after = owner_rows(database)
    assert {key: after[key] for key in before} == before
    assert {value[0] for value in after.values()} == {"c1", "c2"}


def test_same_scope_native_and_url_disagreement_never_resolves_either_way(
    database, catalog, clock
) -> None:
    seed(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R7", id="d1"),
            direct_job(3, requisition="R20", id="d3", url=WORKDAY_URL.format("R21")),
        ],
        "a",
    )
    before = jobs_state(database)
    result = enforce(
        database,
        catalog,
        clock,
        [
            # Native R7 (owned by d1) disagrees with the URL's R8.
            direct_job(1, requisition="R7", id="d2", url=WORKDAY_URL.format("R8")),
            # d3 owns both R20 and R21, so R21 cannot single it out.
            direct_job(3, requisition="R21", id="d4"),
        ],
        "b",
    )
    assert nonzero(resolve(result)) == {
        "rows": 2,
        "fallback": 2,
        "reject_row_native_url_disagreement": 1,
        "reject_target_ambiguous": 1,
    }
    assert result.counters.jobs_inserted == 2
    after = jobs_state(database)
    assert after["d1"] == before["d1"] and after["d3"] == before["d3"]


def google_job(index, native, url_id, **overrides) -> dict:
    values = {
        "source_url": (
            "https://www.google.com/about/careers/applications/jobs/results/"
            f"{url_id}-software-engineering-intern"
        ),
        "extra": {
            "source": "direct",
            "source_adapter": "google",
            "source_system": "google",
            "source_requisition_id": native,
            "active": True,
        },
    }
    values.update(overrides)
    return direct_job(index, **values)


def test_google_cross_provider_identities_resolve_only_through_the_native_id(
    database, catalog, clock
) -> None:
    seed(
        database,
        catalog,
        clock,
        [google_job(1, "111", "111", id="g1"), google_job(2, "222", "222", id="g2")],
        "a",
    )
    assert {
        key for kind, key in owner_rows(database) if kind == "requisition"
    } == {
        "google|stripe|111",
        "google_careers|www_google_com|111",
        "google|stripe|222",
        "google_careers|www_google_com|222",
    }
    before = jobs_state(database)
    result = enforce(
        database,
        catalog,
        clock,
        [
            google_job(1, "111", "111", id="g3", title="Platform Engineer Intern"),
            # Native 333 with a URL that names posting 222.
            google_job(4, "333", "222", id="g4"),
        ],
        "b",
    )
    assert nonzero(resolve(result)) == {
        "rows": 2,
        "would_resolve": 1,
        "resolved": 1,
        "fallback": 1,
        "reject_primary_not_owned": 1,
    }
    after = jobs_state(database)
    assert after["g1"]["title"] == "Platform Engineer Intern"
    assert after["g2"] == before["g2"]
    assert set(after) == {"g1", "g2", "g4"}


def test_workday_tenants_and_sites_keep_identical_requisition_text_apart(
    database, catalog, clock
) -> None:
    urls = {
        "w1": "https://stripe.wd5.myworkdayjobs.com/Stripe/job/New-York-NY/Backend-Engineer-Intern_R1",
        "w2": "https://stripe.wd5.myworkdayjobs.com/StripeUniversity/job/New-York-NY/Backend-Engineer-Intern_R1",
        "w3": "https://stripepay.wd1.myworkdayjobs.com/Stripe/job/New-York-NY/Backend-Engineer-Intern_R1",
    }
    seed(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="R1", id=watcher_id, url=url) for watcher_id, url in urls.items()],
        "a",
    )
    assert len({key for kind, key in owner_rows(database) if kind == "requisition"}) == 3
    original = jobs_state(database)
    result = enforce(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id=f"{watcher_id}-{SUFFIX_A}", url=url)
            for watcher_id, url in urls.items()
        ],
        "b",
    )
    assert nonzero(resolve(result)) == {"rows": 3, "would_resolve": 3, "resolved": 3}
    after = jobs_state(database)
    assert set(after) == set(urls)
    for watcher_id, url in urls.items():
        assert after[watcher_id]["id"] == original[watcher_id]["id"]
        assert after[watcher_id]["application_url"] == url
        assert after[watcher_id]["last_seen_at"] == clock()


# --- Direct and backstop ------------------------------------------------------

BACKSTOP_EVIDENCE = evidence(
    attempt(succeeded=False, rows_returned=None, incomplete=True, complete=False),
    attempt(FEED_KEY, source_kind="github_feed", adapter="github_listings", company=None),
)


def closed_backstop(index, *, url, listing_id, **overrides) -> dict:
    row = backstop_row(index, url=url, listing_id=listing_id, **overrides)
    row["extra"] = {**row["extra"], "active": False}
    return row


def test_backstop_row_resolves_to_its_direct_job_without_rewriting_it(
    database, catalog, clock
) -> None:
    seed(database, catalog, clock, [direct_job(1, requisition="R1", id="b1")], "a")
    before = jobs_state(database)["b1"]

    sighting = enforce(
        database,
        catalog,
        clock,
        [backstop_row(2, url=WORKDAY_URL.format("R1"), listing_id="L-1", id="b2")],
        "b",
        source_evidence=BACKSTOP_EVIDENCE,
    )
    assert nonzero(resolve(sighting)) == {"rows": 1, "would_resolve": 1, "resolved": 1}
    assert sighting.counters.jobs_unchanged == 1
    state = jobs_state(database)
    assert set(state) == {"b1"}
    for name in ("id", "title", "description", "application_url", "is_open", "updated_at"):
        assert state["b1"][name] == (
            clock() if name == "updated_at" else before[name]
        )
    assert state["b1"]["last_seen_at"] == clock()
    assert owner_rows(database)[("listing", f"{FEED_KEY}|l-1")][0] == "b1"

    # An explicit closure from the same verified posting closes the direct job.
    closure = enforce(
        database,
        catalog,
        clock,
        [closed_backstop(2, url=WORKDAY_URL.format("R1"), listing_id="L-1", id="b3")],
        "c",
        source_evidence=BACKSTOP_EVIDENCE,
    )
    assert nonzero(resolve(closure)) == {"rows": 1, "would_resolve": 1, "resolved": 1}
    assert jobs_state(database)["b1"]["is_open"] is False


def test_backstop_closure_of_a_resolved_job_with_a_shared_url_is_refused(
    database, catalog, clock
) -> None:
    seed(database, catalog, clock, [direct_job(3, requisition="R3", id="t1")], "a")
    seed(database, catalog, clock, [direct_job(3, requisition="R3", id="t2")], "b")
    result = enforce(
        database,
        catalog,
        clock,
        [closed_backstop(3, url=WORKDAY_URL.format("R3"), listing_id="L-3", id="t3")],
        "c",
        source_evidence=BACKSTOP_EVIDENCE,
    )
    assert nonzero(resolve(result)) == {"rows": 1, "would_resolve": 1, "resolved": 1}
    state = jobs_state(database)
    assert set(state) == {"t1", "t2"}
    assert state["t1"]["is_open"] is True and state["t2"]["is_open"] is True


def test_direct_row_never_resolves_to_a_backstop_job_through_a_url_alone(
    database, catalog, clock
) -> None:
    url = "https://stripe.com/jobs/posting-9"
    seed(
        database,
        catalog,
        clock,
        [backstop_row(1, url=url, listing_id="L-9", id="k1")],
        "a",
        source_evidence=BACKSTOP_EVIDENCE,
    )
    before = jobs_state(database)
    result = enforce(database, catalog, clock, [direct_job(2, requisition="R9", id="k2", url=url)], "b")
    assert nonzero(resolve(result)) == {"rows": 1, "fallback": 1, "reject_primary_not_owned": 1}
    after = jobs_state(database)
    assert after["k1"] == before["k1"]
    assert set(after) == {"k1", "k2"}


# --- Guards -------------------------------------------------------------------


def test_target_contention_and_batch_ambiguity_fall_back(database, catalog, clock) -> None:
    seed(database, catalog, clock, [direct_job(1, requisition="R1", id="x1"), direct_job(5, requisition="R5", id="x5")], "a")

    contended = enforce(
        database,
        catalog,
        clock,
        [
            direct_job(1, requisition="R1", id="x1-new"),
            # A different requisition arrives under x1's watcher ID.
            direct_job(9, requisition="R9", id="x1"),
        ],
        "b",
    )
    assert nonzero(resolve(contended)) == {
        "rows": 2,
        "unowned": 1,
        "fallback": 1,
        "reject_target_contention": 1,
        "legacy_requisition_mismatch": 1,
    }
    assert contended.counters.jobs_inserted == 1

    ambiguous = enforce(
        database,
        catalog,
        clock,
        [direct_job(5, requisition="R5", id="x5-b"), direct_job(5, requisition="R5", id="x5-c")],
        "c",
    )
    assert nonzero(resolve(ambiguous)) == {
        "rows": 2,
        "fallback": 2,
        "reject_batch_ambiguous": 2,
    }
    assert ambiguous.counters.jobs_inserted == 2
    assert owner_rows(database)[("requisition", requisition_key("R5"))][0] == "x5"


def test_closed_and_superseded_targets_fall_back(database, catalog, clock) -> None:
    seed(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="R1", id="z1"), direct_job(3, requisition="R3", id="z3"), direct_job(4, requisition="R4", id="z4")],
        "a",
    )
    closed = direct_job(1, requisition="R1", id="z1")
    closed["extra"] = {**closed["extra"], "active": False}
    seed(database, catalog, clock, [closed], "b")
    with database.session_factory.begin() as db:
        db.execute(
            text(
                "UPDATE hosted_jobs SET is_open = false, closed_at = last_seen_at, "
                "closure_reason = 'superseded', superseded_by_job_id = "
                "(SELECT id FROM hosted_jobs WHERE watcher_job_id = 'z4') "
                "WHERE watcher_job_id = 'z3'"
            )
        )
    before = jobs_state(database)

    result = enforce(
        database,
        catalog,
        clock,
        [direct_job(1, requisition="R1", id="z2"), direct_job(3, requisition="R3", id="z5")],
        "c",
    )
    assert nonzero(resolve(result)) == {
        "rows": 2,
        "fallback": 2,
        "reject_target_closed": 1,
        "reject_target_superseded": 1,
    }
    assert result.counters.jobs_inserted == 2
    after = jobs_state(database)
    for watcher_id in ("z1", "z3", "z4"):
        assert after[watcher_id] == before[watcher_id]
    with database.session_factory() as db:
        assert db.scalar(
            text("SELECT closure_reason FROM hosted_jobs WHERE watcher_job_id = 'z3'")
        ) == "superseded"


def test_identity_ownership_is_never_reassigned(database, catalog, clock) -> None:
    seed(database, catalog, clock, [direct_job(1, requisition="R1", id="o1")], "a")
    seed(database, catalog, clock, [direct_job(1, requisition="R1", id="o2")], "b")
    before = owner_rows(database)
    moved_url = (
        "https://stripe.wd5.myworkdayjobs.com/Stripe/job/Remote/Backend-Engineer-Intern_R1"
    )
    result = enforce(database, catalog, clock, [direct_job(1, requisition="R1", id="o2", url=moved_url)], "c")
    assert nonzero(resolve(result)) == {"rows": 1, "would_resolve": 1, "resolved": 1}
    after = owner_rows(database)
    for key, value in before.items():
        assert after[key] == value
    assert after[("url", moved_url)][0] == "o1"
    assert {value[0] for value in after.values()} == {"o1"}
    assert jobs_state(database)["o1"]["application_url"] == moved_url


# --- Idempotency, atomicity, and locking --------------------------------------


def test_resolved_imports_are_idempotent(database, catalog, clock) -> None:
    enforce(database, catalog, clock, [direct_job(1, id="A")], "a")
    resolved = enforce(database, catalog, clock, [direct_job(1, id="B")], "b")
    assert resolve(resolved)["resolved"] == 1
    snapshot = (jobs_state(database), owner_rows(database), counts(database))

    repeated = enforce(database, catalog, clock, [direct_job(1, id="B")], "b")
    assert repeated.already_imported
    assert repeated.identity_resolution is None and repeated.identity_shadow is None
    assert (jobs_state(database), owner_rows(database), counts(database)) == snapshot

    again = enforce(database, catalog, clock, [direct_job(1, id="B")], "c")
    assert resolve(again)["resolved"] == 1
    assert (again.counters.jobs_inserted, again.counters.jobs_updated) == (0, 0)
    assert again.counters.jobs_unchanged == 1
    assert set(jobs_state(database)) == {"A"}


def test_a_failed_import_rolls_back_resolution_and_identity_writes_together(
    database, catalog, clock, monkeypatch
) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "watcher@example.com")
    seed(database, catalog, clock, [direct_job(1, id="A")], "a")
    before = (jobs_state(database), owner_rows(database), counts(database))
    moved_url = (
        "https://stripe.wd5.myworkdayjobs.com/Stripe/job/Remote/Backend-Engineer-Intern_R1"
    )
    incoming = [direct_job(1, id="B", title="Platform Engineer Intern", url=moved_url)]

    monkeypatch.setattr(
        job_import,
        "reconcile_jobs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    with pytest.raises(JobUpsertFailed):
        enforce(database, catalog, clock, incoming, "b")
    assert (jobs_state(database), owner_rows(database), counts(database)) == before
    with database.session_factory() as db:
        run_row = db.scalar(
            select(HostedJobImportRun).where(HostedJobImportRun.source_fingerprint == "b" * 64)
        )
        assert run_row.status == "failed"

    monkeypatch.undo()
    retried = enforce(database, catalog, clock, incoming, "b", retry_failed=True)
    assert nonzero(resolve(retried)) == {"rows": 1, "would_resolve": 1, "resolved": 1}
    state = jobs_state(database)
    assert set(state) == {"A"}
    assert state["A"]["title"] == "Platform Engineer Intern"
    assert owner_rows(database)[("url", moved_url)][0] == "A"
    assert counts(database) == (1, 1, 1)


def test_enforced_imports_serialize_on_the_import_advisory_lock(
    database, catalog, clock, notification_postgres_url
) -> None:
    enforce(database, catalog, clock, [direct_job(1, id="A")], "a")
    clock.advance(hours=1)
    outcome: dict[str, object] = {}

    with psycopg.connect(_psycopg_url(notification_postgres_url), autocommit=True) as holder:
        holder.execute(LOCK_SQL)
        worker = threading.Thread(
            target=lambda: outcome.update(
                result=enforce(
                    database, catalog, clock, [direct_job(1, id="B")], "b", advance=False
                )
            )
        )
        worker.start()
        time.sleep(1.0)
        assert worker.is_alive()  # waiting for the competing import's lock
        assert jobs_state(database)["A"]["last_seen_at"] < clock()
        holder.execute(UNLOCK_SQL)
    worker.join(timeout=20)

    assert not worker.is_alive()
    assert resolve(outcome["result"])["resolved"] == 1
    assert set(jobs_state(database)) == {"A"}


def test_advisory_lock_timeout_fails_cleanly_and_a_retry_succeeds(
    database, catalog, clock, notification_postgres_url, monkeypatch
) -> None:
    enforce(database, catalog, clock, [direct_job(1, id="A")], "a")
    before = (jobs_state(database), owner_rows(database))
    monkeypatch.setattr(job_import, "_IMPORT_LOCK_TIMEOUT", "300ms")

    with psycopg.connect(_psycopg_url(notification_postgres_url), autocommit=True) as holder:
        holder.execute(LOCK_SQL)
        started = time.monotonic()
        with pytest.raises(ImportLockUnavailable):
            enforce(database, catalog, clock, [direct_job(1, id="B")], "b")
        assert time.monotonic() - started < 10
        # The legacy and diagnostic paths never take the lock.
        diagnostic = seed(database, catalog, clock, [direct_job(2, requisition="R2", id="C")], "c")
        assert diagnostic.identity_resolution.mode == "shadow"
        holder.execute(UNLOCK_SQL)

    with database.session_factory() as db:
        failed = db.scalar(
            select(HostedJobImportRun).where(HostedJobImportRun.source_fingerprint == "b" * 64)
        )
        assert (failed.status, failed.failure_summary) == (
            "failed",
            "hosted_import_lock_unavailable",
        )
    assert jobs_state(database)["A"] == before[0]["A"]

    retried = enforce(database, catalog, clock, [direct_job(1, id="B")], "b", retry_failed=True)
    assert resolve(retried)["resolved"] == 1
    assert set(jobs_state(database)) == {"A", "C"}


# --- Disabled path and diagnostics ----------------------------------------------


def _legacy_scenario(database, catalog, *, shadow: bool, resolution: bool) -> list:
    clock = MutableClock()
    with database.session_factory.begin() as db:
        create_user(db, clock(), "watcher@example.com")
    steps = [
        [direct_job(1, requisition="R1", id="s1"), direct_job(2, requisition="R2", id="s2"), direct_job(3, requisition="R3", id="s3")],
        [
            direct_job(1, requisition="R1", id=f"s1-{SUFFIX_A}"),
            direct_job(2, requisition="R2", id="s2-retitled", title="Platform Engineer Intern"),
            backstop_row(3, url=WORKDAY_URL.format("R3"), listing_id="L-3", id="s3-backstop"),
        ],
        [
            direct_job(1, requisition="R1", id="s1"),
            direct_job(2, requisition="R2", id="s2"),
            direct_job(9, requisition="R9", id="s3"),
        ],
    ]
    results = []
    for marker, jobs in zip("abc", steps):
        results.append(
            run(database, catalog, clock, jobs, marker, shadow=shadow, resolution=resolution)
        )
    return results


def test_disabled_resolution_keeps_jobs_matches_and_notifications_identical(
    database, catalog
) -> None:
    legacy_results = _legacy_scenario(database, catalog, shadow=False, resolution=False)
    legacy = _state(database)
    _truncate(database)
    requested_results = _legacy_scenario(database, catalog, shadow=False, resolution=True)
    requested = _state(database)
    assert identities(database) == {}
    _truncate(database)
    diagnostic_results = _legacy_scenario(database, catalog, shadow=True, resolution=False)
    diagnostic = _state(database)

    assert legacy == requested == diagnostic
    assert (
        [result.counters for result in legacy_results]
        == [result.counters for result in requested_results]
        == [result.counters for result in diagnostic_results]
    )
    # The scenario really churned: legacy inserted twins and alerted on them.
    assert len(legacy["jobs"]) == 6
    assert len(legacy["items"]) > 3
    assert all(result.identity_resolution is None for result in legacy_results)
    assert all(result.identity_resolution is None for result in requested_results)
    reports = [result.identity_resolution for result in diagnostic_results]
    assert all(report.mode == "shadow" and report.status == "ok" for report in reports)
    assert all(report.counters["resolved"] == 0 for report in reports)
    # Suffix churn, a retitle, and a backstop sighting of a direct job.
    assert reports[1].counters["would_resolve"] == 3
    assert reports[2].counters["legacy_requisition_mismatch"] == 1


def test_diagnostic_import_transaction_is_statement_identical_to_legacy(
    database, catalog
) -> None:
    def capture(*, shadow: bool) -> list[str]:
        statements: list[str] = []

        def record(_conn, _cursor, statement, *_args) -> None:
            statements.append(statement)

        clock = MutableClock()
        seed(database, catalog, clock, [direct_job(1, id="A")], "a", shadow=shadow)
        event.listen(database.engine, "before_cursor_execute", record)
        try:
            seed(
                database,
                catalog,
                clock,
                [direct_job(1, id="B"), direct_job(2, requisition="R2", id="C")],
                "b",
                shadow=shadow,
            )
        finally:
            event.remove(database.engine, "before_cursor_execute", record)
        return statements

    legacy = capture(shadow=False)
    _truncate(database)
    diagnostic = capture(shadow=True)

    assert diagnostic[: len(legacy)] == legacy
    assert diagnostic[len(legacy)].startswith("SET LOCAL lock_timeout")
    joined = "\n".join(diagnostic).casefold()
    assert "pg_advisory" not in joined
    assert "hosted_job_identities" not in "\n".join(legacy).casefold()


def test_diagnostic_decisions_match_enforced_decisions_on_equivalent_fixtures(
    database, catalog
) -> None:
    def build(*, resolution: bool):
        clock = MutableClock()
        seed(
            database,
            catalog,
            clock,
            [
                direct_job(1, requisition="R1", id="q1"),
                direct_job(2, requisition="R2", id="q2"),
                direct_job(3, requisition="R3", id="q3", url="https://stripe.com/careers"),
                direct_job(4, requisition=None, id="q4", url="https://stripe.com/jobs/posting-4"),
                direct_job(5, requisition="R5", id="q5"),
                direct_job(6, requisition="R60", id="q6", url=WORKDAY_URL.format("R61")),
                direct_job(7, requisition="R7", id="q7"),
                direct_job(8, requisition="R8", id="q8"),
            ],
            "a",
        )
        return run(
            database,
            catalog,
            clock,
            [
                direct_job(1, requisition="R1", id=f"q1-{SUFFIX_A}"),
                direct_job(2, requisition="R2", id="q2-new", title="Platform Engineer Intern"),
                direct_job(3, requisition="R3", id="q3-new", url="https://stripe.com/jobs/posting-4"),
                direct_job(5, requisition="R5", id="q5-new", url=WORKDAY_URL.format("R55")),
                direct_job(6, requisition="R61", id="q6-new"),
                direct_job(7, requisition="R7", id="q7-new"),
                direct_job(70, requisition="R70", id="q7"),
                backstop_row(8, url=WORKDAY_URL.format("R8"), listing_id="L-8", id="q8-new"),
                direct_job(9, requisition="R9", id="q9"),
            ],
            "b",
            resolution=resolution,
        )

    diagnostic = build(resolution=False)
    _truncate(database)
    enforced = build(resolution=True)

    assert diagnostic.identity_resolution.mode == "shadow"
    assert enforced.identity_resolution.mode == "enforced"
    shadow_counts = resolve(diagnostic)
    enforced_counts = resolve(enforced)
    assert shadow_counts["resolved"] == 0
    assert enforced_counts["resolved"] == enforced_counts["would_resolve"] == 3
    assert {name: value for name, value in shadow_counts.items() if name != "resolved"} == {
        name: value for name, value in enforced_counts.items() if name != "resolved"
    }
    assert nonzero(shadow_counts) == {
        "rows": 9,
        "would_resolve": 3,
        "unowned": 2,
        "fallback": 4,
        "reject_conflict": 1,
        "reject_row_native_url_disagreement": 1,
        "reject_target_ambiguous": 1,
        "reject_target_contention": 1,
        "legacy_requisition_mismatch": 1,
    }
    assert enforced.counters.jobs_inserted == diagnostic.counters.jobs_inserted - 3


def test_resolution_telemetry_is_bounded_and_privacy_safe(database, catalog, clock) -> None:
    with database.session_factory.begin() as db:
        create_user(db, clock(), "private.person@example.com")
    secret_title = "Secret Title Do Not Log"
    enforce(database, catalog, clock, [direct_job(1, requisition="REQ-SECRET-9", id="watcher-job-a")], "a")
    results = [
        enforce(
            database,
            catalog,
            clock,
            [direct_job(1, requisition="REQ-SECRET-9", id="watcher-job-b", title=f"Intern {secret_title}")],
            "b",
        ),
        seed(database, catalog, clock, [direct_job(1, requisition="REQ-SECRET-9", id="watcher-job-c")], "c"),
    ]
    for result, mode in zip(results, ("enforced", "shadow")):
        lines = [
            line
            for line in import_summary_lines(result)
            if line.startswith("HOSTED-IDENTITY-RESOLVE")
        ]
        assert len(lines) == 1
        [line] = lines
        assert len(line) < 1000
        pairs = [pair.split("=") for pair in line.split(" ")[1:]]
        assert [name for name, _value in pairs] == ["mode", "status", *RESOLVE_COUNTER_NAMES]
        assert dict(pairs)["mode"] == mode and dict(pairs)["status"] == "ok"
        for name, value in pairs[2:]:
            assert name.replace("_", "").isalpha() and value.isdigit()
        for forbidden in (
            secret_title,
            "req-secret-9",
            "REQ-SECRET-9",
            "myworkdayjobs",
            "http",
            "Stripe",
            "stripe",
            "watcher-job",
            "private.person",
            "@",
        ):
            assert forbidden not in line
    # The three Phase 2 shadow lines are unchanged.
    shadow_lines = [
        line.split(" ", 1)[0]
        for line in import_summary_lines(results[0])
        if line.startswith("HOSTED-IDENTITY-SHADOW")
    ]
    assert shadow_lines == [
        "HOSTED-IDENTITY-SHADOW",
        "HOSTED-IDENTITY-SHADOW-IDENTITIES",
        "HOSTED-IDENTITY-SHADOW-SOURCES",
    ]


def test_a_diagnostic_failure_never_changes_shadow_tracking_or_the_import(
    database, catalog, clock, monkeypatch
) -> None:
    seed(database, catalog, clock, [direct_job(1, id="A")], "a")

    def failing(*_args, **_kwargs):
        raise RuntimeError("secret detail https://example.com/token")

    monkeypatch.setattr(identity_shadow, "_resolution_targets", failing)
    result = seed(database, catalog, clock, [direct_job(1, id="B")], "b")
    assert result.outcome == "imported" and result.counters.jobs_inserted == 1
    assert result.identity_shadow.status == "ok"
    assert result.identity_shadow.counters["disagree"] == 1
    assert result.identity_resolution.lines() == [
        "HOSTED-IDENTITY-RESOLVE mode=shadow status=failed"
    ]


def test_a_failed_shadow_pass_reports_the_diagnostic_as_failed(
    database, catalog, clock, monkeypatch
) -> None:
    monkeypatch.setattr(identity_shadow, "_STATEMENT_TIMEOUT", "not-a-duration")
    result = seed(database, catalog, clock, [direct_job(1, id="A")], "a")
    assert result.outcome == "imported"
    assert result.identity_shadow.status == "failed"
    assert result.identity_resolution.lines() == [
        "HOSTED-IDENTITY-RESOLVE mode=shadow status=failed"
    ]


# --- Snapshot and scheduled paths ----------------------------------------------


@pytest.mark.parametrize(
    ("shadow", "resolution", "expected"),
    [
        (None, None, None),
        (None, "true", None),
        ("true", None, "shadow"),
        ("true", "false", "shadow"),
        ("true", "true", "enforced"),
    ],
)
def test_snapshot_import_enforces_only_with_both_flags(
    notification_postgres_url, tmp_path, monkeypatch, shadow, resolution, expected
) -> None:
    for name, value in ((SHADOW_FLAG_ENV, shadow), (RESOLUTION_FLAG_ENV, resolution)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    result = import_snapshot_into_hosted(_snapshot(tmp_path), database_url=notification_postgres_url)
    assert result.outcome == "imported"
    if expected is None:
        assert result.identity_resolution is None
        return
    assert result.identity_resolution.mode == expected
    assert nonzero(resolve(result)) == {"rows": 1, "unowned": 1}
    database = HostedDatabase(notification_postgres_url)
    try:
        assert {kind for kind, _key in identities(database)} == {"requisition", "url"}
    finally:
        database.dispose()


@pytest.mark.parametrize(("resolution", "mode"), [(None, "shadow"), ("1", "enforced")])
def test_collect_and_import_reports_the_resolution_line(
    notification_postgres_url, tmp_path, monkeypatch, capsys, resolution, mode
) -> None:
    from app.hosted import collect_and_import
    from watcher.collection_snapshot import load_collection_snapshot

    batch = load_collection_snapshot(_snapshot(tmp_path))
    monkeypatch.setenv("HOSTED_DATABASE_URL", notification_postgres_url)
    monkeypatch.setenv(SHADOW_FLAG_ENV, "1")
    if resolution is None:
        monkeypatch.delenv(RESOLUTION_FLAG_ENV, raising=False)
    else:
        monkeypatch.setenv(RESOLUTION_FLAG_ENV, resolution)
    assert collect_and_import.main([], collector=lambda _config: batch) == 0
    output = capsys.readouterr().out
    assert f"HOSTED-IDENTITY-RESOLVE mode={mode} status=ok rows=1 " in output
    assert "HOSTED-IDENTITY-SHADOW status=ok" in output
    assert "myworkdayjobs" not in output and "R555" not in output
