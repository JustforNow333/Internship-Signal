"""Shadow-mode generation tracking must never influence notification state."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from watcher.generation import (
    DEFAULT_GENERATION_ABSENCE_DAYS,
    GENERATION_ABSENCE_DAYS_ENV,
    TRIGGER_SEASON_CHANGE,
    TRIGGER_SUSTAINED_ABSENCE,
    GenerationConfigError,
    generation_absence_days,
    season_key_for_title,
)
from watcher.run import _generation_absence_health
from watcher.seen_store import (
    SEEN_SCHEMA_VERSION,
    SHADOW_EVENT_RETENTION_LIMIT,
    SeenStore,
)
from watcher.source_health import (
    COVERAGE_BACKSTOP_ONLY,
    COVERAGE_DIRECT,
    COVERAGE_DIRECT_EMPTY,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_EMPTY,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    DIRECT_STATUS_NOT_CONFIGURED,
    DIRECT_STATUS_UNKNOWN,
    CompanyCoverage,
)

BASE = datetime(2026, 7, 1, tzinfo=timezone.utc)
HEALTHY = {"Example": True}


def job(
    title="Software Engineer Intern",
    *,
    job_id="job-1",
    company="Example",
    url="https://job-boards.greenhouse.io/example/jobs/12345",
    location="New York, NY",
):
    return {
        "id": job_id,
        "company": company,
        "title": title,
        "location": location,
        "source_url": url,
        "extra": {"source": "direct", "source_adapter": "greenhouse"},
    }


def suppressing_rows(store):
    return store._conn.execute(
        "select count(*) from seen "
        "where emailed_at is not null or primed_at is not null"
    ).fetchone()[0]


# ---------------------------------------------------------------- season keys


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Technology Internship Program - Summer 2027", "season|summer|2027"),
        ("Fall 2026: Applied AI Co-op", "season|fall|2026"),
        ("2027 Spring Software Intern", "season|spring|2027"),
        ("Summer '27 Analyst", "season|summer|2027"),
        ("Autumn 2027 Intern", "season|fall|2027"),
    ],
)
def test_season_key_is_extracted_from_free_form_titles(title, expected):
    assert season_key_for_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Intern",
        # A bare year is not a season expression.
        "2027 Software Engineer Intern",
        "Software Developer Intern ⏳",
        "",
        None,
        # Two different seasons in one title fail closed.
        "Summer 2026 and Summer 2027 Intern",
    ],
)
def test_unresolved_or_ambiguous_titles_have_no_season_key(title):
    assert season_key_for_title(title) is None


def test_absence_threshold_default_and_validation(monkeypatch):
    monkeypatch.delenv(GENERATION_ABSENCE_DAYS_ENV, raising=False)
    assert generation_absence_days() == DEFAULT_GENERATION_ABSENCE_DAYS
    monkeypatch.setenv(GENERATION_ABSENCE_DAYS_ENV, "30")
    assert generation_absence_days() == 30
    monkeypatch.setenv(GENERATION_ABSENCE_DAYS_ENV, "0")
    with pytest.raises(GenerationConfigError):
        generation_absence_days()
    monkeypatch.setenv(GENERATION_ABSENCE_DAYS_ENV, "not-a-number")
    with pytest.raises(GenerationConfigError):
        generation_absence_days()


# ------------------------------------------------------------------ migration


def test_migration_is_idempotent_and_preserves_suppression_fields(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            create table seen(
              job_id text primary key, company text, title text, url text,
              first_source text, first_seen text, emailed_at text
            )
            """
        )
        conn.execute(
            "insert into seen(job_id, company, title, url, first_source, "
            "first_seen, emailed_at) values (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-1",
                "Example",
                "Technology Internship Program - Summer 2027",
                "https://example.test/jobs/1",
                "direct",
                "2026-07-01T00:00:00+00:00",
                "2026-07-02T00:00:00+00:00",
            ),
        )
        conn.execute(
            "insert into seen(job_id, company, title, url, first_source, "
            "first_seen, emailed_at) values (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-2",
                "Example",
                "Software Engineer Intern",
                "https://example.test/jobs/2",
                "direct",
                "2026-07-01T00:00:00+00:00",
                None,
            ),
        )

    for _pass in range(3):
        with SeenStore(db_path) as store:
            rows = {
                row["job_id"]: dict(row)
                for row in store._conn.execute("select * from seen").fetchall()
            }

    assert rows["legacy-1"]["emailed_at"] == "2026-07-02T00:00:00+00:00"
    assert rows["legacy-2"]["emailed_at"] is None
    assert rows["legacy-1"]["last_seen"] == "2026-07-01T00:00:00+00:00"
    assert rows["legacy-2"]["last_seen"] == "2026-07-01T00:00:00+00:00"
    assert rows["legacy-1"]["generation"] == 1
    assert rows["legacy-2"]["generation"] == 1
    assert rows["legacy-1"]["absence_epoch"] == 0
    assert rows["legacy-1"]["season_key"] == "season|summer|2027"
    # A title with no resolvable season stays NULL rather than guessing.
    assert rows["legacy-2"]["season_key"] is None


def test_legacy_rows_with_null_season_key_never_produce_a_season_candidate(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store._conn.execute(
            "insert into seen(job_id, company, title, url, first_source, "
            "first_seen, emailed_at, identity_key, last_seen, season_key) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, null)",
            (
                "legacy",
                "Example",
                "Software Engineer Intern",
                "https://job-boards.greenhouse.io/example/jobs/12345",
                "direct",
                "2026-07-01T00:00:00+00:00",
                "2026-07-01T00:00:00+00:00",
                "requisition|greenhouse|example|12345",
                "2026-07-01T00:00:00+00:00",
            ),
        )
        store._conn.commit()

        result = store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )

    assert result.shadow_candidates == ()


# ---------------------------------------------------------------- observation


def test_last_seen_updates_for_observed_postings_without_emailing_them(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = store.observe([job()], observed_at=BASE, collection_health=HEALTHY)

        assert result.observed == 1
        assert result.rows_created == 1
        assert suppressing_rows(store) == 0
        assert store.partition([job()]).pending == [job()]

        later = BASE + timedelta(days=3)
        store.observe([job()], observed_at=later, collection_health=HEALTHY)
        row = store._conn.execute(
            "select last_seen, emailed_at, primed_at from seen"
        ).fetchone()

    assert row["last_seen"] == later.isoformat()
    assert row["emailed_at"] is None
    assert row["primed_at"] is None


# ------------------------------------------------------------ season triggers


def test_explicit_season_change_under_one_identity_produces_a_candidate(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe(
            [job(title="Summer 2026 Software Engineer Intern")],
            observed_at=BASE,
            collection_health=HEALTHY,
        )
        result = store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )

        assert len(result.shadow_candidates) == 1
        candidate = result.shadow_candidates[0]
        assert candidate.trigger == TRIGGER_SEASON_CHANGE
        assert candidate.stored_season_key == "season|summer|2026"
        assert candidate.current_season_key == "season|summer|2027"
        assert candidate.current_generation == 1
        assert candidate.proposed_generation == 2

        # Idempotent: the stored season advanced, so a rerun stays quiet.
        repeat = store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=2),
            collection_health=HEALTHY,
        )
        assert repeat.shadow_candidates == ()


@pytest.mark.parametrize(
    "changed",
    [
        {"title": "Software Engineer Intern, BS"},
        {"location": "Mountain View, CA"},
        {"url": "https://job-boards.greenhouse.io/example/jobs/12345-software-intern"},
        {"title": "  software   engineer   intern  "},
    ],
)
def test_title_location_slug_and_formatting_changes_never_generate(tmp_path, changed):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        result = store.observe(
            [job(**changed)],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )

    assert result.shadow_candidates == ()


def test_season_token_only_appearing_is_not_a_season_change(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        # Stored title has no resolvable season.
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        result = store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )

    assert result.shadow_candidates == ()


def test_season_token_disappearing_is_not_a_season_change(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE,
            collection_health=HEALTHY,
        )
        result = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )

        assert result.shadow_candidates == ()
        # A resolved season is never erased by a later unresolved title.
        row = store._conn.execute("select season_key from seen").fetchone()

    assert row["season_key"] == "season|summer|2027"


# ----------------------------------------------------------- absence triggers


def test_reappearance_after_healthy_absence_threshold_produces_one_candidate(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        # Healthy collections continue while the posting is absent.
        for day in (5, 10, 15, 20):
            store.observe([], observed_at=BASE + timedelta(days=day),
                          collection_health=HEALTHY)

        result = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=21),
            collection_health=HEALTHY,
        )

        assert len(result.shadow_candidates) == 1
        candidate = result.shadow_candidates[0]
        assert candidate.trigger == TRIGGER_SUSTAINED_ABSENCE
        assert candidate.absence_days == pytest.approx(21.0, abs=0.01)
        assert candidate.proposed_generation == 2

        # Idempotent: last_seen advanced, so the next run reports nothing.
        repeat = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=22),
            collection_health=HEALTHY,
        )
        assert repeat.shadow_candidates == ()
        epoch = store._conn.execute("select absence_epoch from seen").fetchone()[0]

    assert epoch == 1


def test_absence_below_the_threshold_produces_no_candidate(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        result = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=13),
            collection_health=HEALTHY,
        )

    assert result.shadow_candidates == ()


def test_absence_during_failed_or_degraded_collections_does_not_advance(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        # The source fails midway through the gap, which restarts the streak.
        store.observe([], observed_at=BASE + timedelta(days=10),
                      collection_health={"Example": False})

        result = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=21),
            collection_health=HEALTHY,
        )

        assert result.shadow_candidates == ()
        epoch = store._conn.execute("select absence_epoch from seen").fetchone()[0]

    assert epoch == 0


def test_absence_is_not_credited_without_healthy_collection_evidence(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        # No coverage reported at all for this company.
        result = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=40),
            collection_health={},
        )

    assert result.shadow_candidates == ()


def test_configured_absence_threshold_is_honoured(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        result = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=4),
            absence_days=3,
            collection_health=HEALTHY,
        )

    assert [c.trigger for c in result.shadow_candidates] == [TRIGGER_SUSTAINED_ABSENCE]


def test_season_change_wins_when_both_triggers_would_fire(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe(
            [job(title="Summer 2026 Software Engineer Intern")],
            observed_at=BASE,
            collection_health=HEALTHY,
        )
        result = store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=40),
            collection_health=HEALTHY,
        )

    assert [c.trigger for c in result.shadow_candidates] == [TRIGGER_SEASON_CHANGE]


# ------------------------------------------------- suppression stays untouched


def test_shadow_candidates_do_not_change_partitioning_or_suppression(tmp_path):
    emailed_at = datetime(2026, 7, 2, tzinfo=timezone.utc)
    with SeenStore(tmp_path / "seen.sqlite") as store:
        old = job(title="Summer 2026 Software Engineer Intern")
        store.observe([old], observed_at=BASE, collection_health=HEALTHY)
        store.mark_many_emailed([old], emailed_at=emailed_at)

        new_generation = job(title="Summer 2027 Software Engineer Intern")
        before = store.partition([new_generation])
        result = store.observe(
            [new_generation],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )
        after = store.partition([new_generation])

        assert len(result.shadow_candidates) == 1
        # A shadow candidate must never re-open a suppressed posting.
        assert before.pending == after.pending == []
        assert before.emailed == after.emailed == [new_generation]
        assert before.primed == after.primed == []
        row = store._conn.execute(
            "select emailed_at, primed_at, generation from seen "
            "where emailed_at is not null"
        ).fetchone()

    assert row["emailed_at"] == emailed_at.isoformat()
    assert row["primed_at"] is None
    # Shadow mode never advances the persisted generation.
    assert row["generation"] == 1


def test_observation_does_not_create_suppressing_rows_or_prime(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe(
            [job(job_id=f"job-{index}", url=f"https://job-boards.greenhouse.io/example/jobs/{index}")
             for index in range(5)],
            observed_at=BASE,
            collection_health=HEALTHY,
        )

        assert suppressing_rows(store) == 0
        selection = store.partition([job()])

    assert selection.emailed == []
    assert selection.primed == []


def test_observe_refuses_to_write_through_a_read_only_store(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    with SeenStore(db_path) as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)

    with SeenStore(db_path, read_only=True) as store:
        with pytest.raises(RuntimeError):
            store.observe([job()], observed_at=BASE, collection_health=HEALTHY)


def test_row_creation_is_bounded_by_the_notifiable_set(tmp_path):
    """Observation refreshes every collected identity but only grows the store
    with postings that can ever suppress."""

    notifiable = job(job_id="eligible",
                     url="https://job-boards.greenhouse.io/example/jobs/1")
    other = job(job_id="ineligible", title="Senior Staff Engineer",
                url="https://job-boards.greenhouse.io/example/jobs/2")

    with SeenStore(tmp_path / "seen.sqlite") as store:
        first = store.observe(
            [notifiable, other],
            observed_at=BASE,
            collection_health=HEALTHY,
            create_rows_for=[notifiable],
        )

        assert first.rows_created == 1
        assert first.rows_skipped == 1
        stored = store._conn.execute("select title from seen").fetchall()
        assert [row["title"] for row in stored] == ["Software Engineer Intern"]

        # An identity that already exists is refreshed even when it is no longer
        # in the notifiable set.
        second = store.observe(
            [notifiable, other],
            observed_at=BASE + timedelta(days=2),
            collection_health=HEALTHY,
            create_rows_for=[],
        )

        assert second.rows_updated == 1
        assert second.rows_created == 0
        last_seen = store._conn.execute("select last_seen from seen").fetchone()[0]

    assert last_seen == (BASE + timedelta(days=2)).isoformat()


def test_backfill_runs_once_and_is_recorded_by_schema_version(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    with SeenStore(db_path) as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        version = store._conn.execute("pragma user_version").fetchone()[0]

    assert version == SEEN_SCHEMA_VERSION

    # Reopening does not rewrite shadow columns that were already resolved.
    with SeenStore(db_path) as store:
        store._conn.execute("update seen set season_key = 'season|summer|2099'")
        store._conn.commit()
    with SeenStore(db_path) as store:
        row = store._conn.execute("select season_key from seen").fetchone()

    assert row["season_key"] == "season|summer|2099"


def test_marking_after_observing_never_regresses_last_seen(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        posting = job()
        store.observe(
            [posting],
            observed_at=BASE + timedelta(days=10),
            collection_health=HEALTHY,
        )
        # Marking with an older explicit timestamp must not rewind `last_seen`.
        store.mark_many_emailed([posting], emailed_at=BASE)
        row = store._conn.execute(
            "select last_seen, emailed_at from seen"
        ).fetchone()

    assert row["last_seen"] == (BASE + timedelta(days=10)).isoformat()
    assert row["emailed_at"] == BASE.isoformat()


# ------------------------------------------- absence-evidence health (Part 1)


def coverage(company="Example", *, direct_status, state=COVERAGE_DIRECT,
             github=False, adapter="greenhouse"):
    return CompanyCoverage(
        company=company,
        adapter=adapter,
        state=state,
        direct_status=direct_status,
        direct_attempt_succeeded=None,
        direct_rows_returned=None,
        github_backstop_available=github,
    )


def test_direct_healthy_with_listings_contributes_healthy_absence():
    health = _generation_absence_health(
        (coverage(direct_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS),)
    )

    assert health == {"Example": True}


def test_direct_healthy_empty_contributes_healthy_absence():
    health = _generation_absence_health(
        (coverage(direct_status=DIRECT_STATUS_HEALTHY_EMPTY,
                  state=COVERAGE_DIRECT_EMPTY),)
    )

    assert health == {"Example": True}


@pytest.mark.parametrize(
    "direct_status",
    [
        DIRECT_STATUS_DEGRADED,
        DIRECT_STATUS_FAILED,
        DIRECT_STATUS_UNKNOWN,
        DIRECT_STATUS_NOT_CONFIGURED,
    ],
)
def test_non_healthy_direct_states_never_contribute_absence(direct_status):
    health = _generation_absence_health((coverage(direct_status=direct_status),))

    assert health == {"Example": False}


def test_backstop_only_never_contributes_absence_even_with_healthy_feeds():
    # A bespoke company with both GitHub feeds healthy still has no direct
    # source, so it reports `not_configured` and earns no absence evidence.
    health = _generation_absence_health(
        (
            coverage(
                company="BespokeCo",
                adapter="bespoke",
                direct_status=DIRECT_STATUS_NOT_CONFIGURED,
                state=COVERAGE_BACKSTOP_ONLY,
                github=True,
            ),
        )
    )

    assert health == {"BespokeCo": False}


def test_backstop_only_company_cannot_gain_sustained_absence_credit(tmp_path):
    backstop_health = _generation_absence_health(
        (
            coverage(
                company="Example",
                adapter="github_only",
                direct_status=DIRECT_STATUS_NOT_CONFIGURED,
                state=COVERAGE_BACKSTOP_ONLY,
                github=True,
            ),
        )
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=backstop_health)
        for day in (10, 20, 30):
            store.observe([], observed_at=BASE + timedelta(days=day),
                          collection_health=backstop_health)
        result = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=40),
            collection_health=backstop_health,
        )

        assert result.shadow_candidates == ()
        assert store.shadow_generation_events() == []


def test_backstop_only_company_still_emits_a_season_change_candidate(tmp_path):
    backstop_health = _generation_absence_health(
        (
            coverage(
                company="Example",
                adapter="github_only",
                direct_status=DIRECT_STATUS_NOT_CONFIGURED,
                state=COVERAGE_BACKSTOP_ONLY,
                github=True,
            ),
        )
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe(
            [job(title="Summer 2026 Software Engineer Intern")],
            observed_at=BASE,
            collection_health=backstop_health,
        )
        result = store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=1),
            collection_health=backstop_health,
        )

    assert [c.trigger for c in result.shadow_candidates] == [TRIGGER_SEASON_CHANGE]


def test_a_single_direct_outage_breaks_absence_credit(tmp_path):
    healthy = _generation_absence_health(
        (coverage(direct_status=DIRECT_STATUS_HEALTHY_WITH_LISTINGS),)
    )
    outage = _generation_absence_health(
        (coverage(direct_status=DIRECT_STATUS_FAILED),)
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=healthy)
        # One failed run mid-gap is enough to reset the streak.
        store.observe([], observed_at=BASE + timedelta(days=10),
                      collection_health=outage)
        result = store.observe(
            [job()],
            observed_at=BASE + timedelta(days=30),
            collection_health=healthy,
        )

    assert result.shadow_candidates == ()


# ------------------------------------------ persisted shadow events (Part 2)


def test_season_change_event_is_persisted_before_season_key_advances(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe(
            [job(title="Summer 2026 Software Engineer Intern")],
            observed_at=BASE,
            collection_health=HEALTHY,
        )
        result = store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )

        assert result.shadow_events_persisted == 1
        events = store.shadow_generation_events()
        stored_season = store._conn.execute(
            "select season_key from seen"
        ).fetchone()[0]

    assert len(events) == 1
    assert events[0]["trigger"] == TRIGGER_SEASON_CHANGE
    assert events[0]["stored_season_key"] == "season|summer|2026"
    assert events[0]["current_season_key"] == "season|summer|2027"
    assert events[0]["current_generation"] == 1
    assert events[0]["proposed_generation"] == 2
    # The event captured the transition even though stored state has moved on.
    assert stored_season == "season|summer|2027"


def test_persisted_event_survives_later_runs_that_no_longer_emit_it(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe(
            [job(title="Summer 2026 Software Engineer Intern")],
            observed_at=BASE,
            collection_health=HEALTHY,
        )
        store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )
        later = store.observe(
            [job(title="Summer 2027 Software Engineer Intern")],
            observed_at=BASE + timedelta(days=2),
            collection_health=HEALTHY,
        )

        assert later.shadow_candidates == ()
        assert len(store.shadow_generation_events()) == 1


def test_replaying_the_same_candidate_does_not_duplicate_the_event(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    for _attempt in range(3):
        with SeenStore(db_path) as store:
            # Reset stored season each time so the same transition re-fires.
            store._conn.execute("update seen set season_key='season|summer|2026'")
            store._conn.commit()
            store.observe(
                [job(title="Summer 2027 Software Engineer Intern")],
                observed_at=BASE + timedelta(days=1),
                collection_health=HEALTHY,
            )

    with SeenStore(db_path) as store:
        events = store.shadow_generation_events()

    assert len(events) == 1


def test_two_distinct_season_transitions_remain_distinct_events(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job(title="Summer 2026 Intern")], observed_at=BASE,
                      collection_health=HEALTHY)
        store.observe([job(title="Summer 2027 Intern")],
                      observed_at=BASE + timedelta(days=1),
                      collection_health=HEALTHY)
        store.observe([job(title="Summer 2028 Intern")],
                      observed_at=BASE + timedelta(days=2),
                      collection_health=HEALTHY)
        events = store.shadow_generation_events()

    assert len(events) == 2
    assert {(e["stored_season_key"], e["current_season_key"]) for e in events} == {
        ("season|summer|2026", "season|summer|2027"),
        ("season|summer|2027", "season|summer|2028"),
    }


def test_sustained_absence_events_are_idempotent_within_one_epoch(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    with SeenStore(db_path) as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        for _attempt in range(3):
            # Rewind only the observation state so the same epoch re-fires.
            store._conn.execute(
                "update seen set last_seen=?, absence_epoch=0",
                (BASE.isoformat(),),
            )
            store._conn.commit()
            store.observe([job()], observed_at=BASE + timedelta(days=21),
                          collection_health=HEALTHY)
        events = store.shadow_generation_events()

    assert len(events) == 1
    assert events[0]["trigger"] == TRIGGER_SUSTAINED_ABSENCE
    assert events[0]["absence_epoch"] == 0


def test_a_later_absence_epoch_creates_a_new_event(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        first = store.observe([job()], observed_at=BASE + timedelta(days=21),
                              collection_health=HEALTHY)
        second = store.observe([job()], observed_at=BASE + timedelta(days=60),
                               collection_health=HEALTHY)
        events = store.shadow_generation_events()

    assert len(first.shadow_candidates) == 1
    assert len(second.shadow_candidates) == 1
    assert len(events) == 2
    assert sorted(e["absence_epoch"] for e in events) == [0, 1]


def test_event_history_is_newest_first_and_bounded_to_one_thousand(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        for index in range(1005):
            store._conn.execute(
                "insert into shadow_generation_events(event_id, identity_key, "
                "company, trigger, current_generation, proposed_generation, "
                "absence_epoch, observed_at) values (?, ?, ?, ?, 1, 2, 0, ?)",
                (
                    f"event-{index:05d}",
                    "identity",
                    "Example",
                    TRIGGER_SEASON_CHANGE,
                    (BASE + timedelta(seconds=index)).isoformat(),
                ),
            )
        store._conn.commit()
        store.observe([job()], observed_at=BASE + timedelta(days=1),
                      collection_health=HEALTHY)

        total = store._conn.execute(
            "select count(*) from shadow_generation_events"
        ).fetchone()[0]
        newest = store.shadow_generation_events(limit=3)

    assert total == SHADOW_EVENT_RETENTION_LIMIT
    observed = [event["observed_at"] for event in newest]
    assert observed == sorted(observed, reverse=True)


def test_events_older_than_the_retention_window_are_removed_only_from_events(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.observe([job()], observed_at=BASE, collection_health=HEALTHY)
        seen_before = store._conn.execute("select count(*) from seen").fetchone()[0]
        store._conn.execute(
            "insert into shadow_generation_events(event_id, identity_key, company, "
            "trigger, current_generation, proposed_generation, absence_epoch, "
            "observed_at) values ('ancient', 'identity', 'Example', ?, 1, 2, 0, ?)",
            (TRIGGER_SEASON_CHANGE, (BASE - timedelta(days=400)).isoformat()),
        )
        store._conn.execute(
            "insert into shadow_generation_events(event_id, identity_key, company, "
            "trigger, current_generation, proposed_generation, absence_epoch, "
            "observed_at) values ('recent', 'identity', 'Example', ?, 1, 2, 0, ?)",
            (TRIGGER_SEASON_CHANGE, (BASE - timedelta(days=10)).isoformat()),
        )
        store._conn.commit()

        store.observe([job()], observed_at=BASE + timedelta(days=1),
                      collection_health=HEALTHY)

        remaining = {
            row["event_id"]
            for row in store._conn.execute(
                "select event_id from shadow_generation_events"
            ).fetchall()
        }
        seen_after = store._conn.execute("select count(*) from seen").fetchone()[0]
        health_rows = store._conn.execute(
            "select count(*) from seen_collection_health"
        ).fetchone()[0]

    assert "ancient" not in remaining
    assert "recent" in remaining
    assert seen_after == seen_before
    assert health_rows == 1


def test_shadow_event_persistence_never_touches_notification_state(tmp_path):
    emailed_at = datetime(2026, 7, 2, tzinfo=timezone.utc)
    with SeenStore(tmp_path / "seen.sqlite") as store:
        old = job(title="Summer 2026 Software Engineer Intern")
        store.observe([old], observed_at=BASE, collection_health=HEALTHY)
        store.mark_many_emailed([old], emailed_at=emailed_at)

        new_generation = job(title="Summer 2027 Software Engineer Intern")
        before = store.partition([new_generation])
        result = store.observe(
            [new_generation],
            observed_at=BASE + timedelta(days=1),
            collection_health=HEALTHY,
        )
        after = store.partition([new_generation])
        row = store._conn.execute(
            "select emailed_at, primed_at, generation from seen"
        ).fetchone()

    assert result.shadow_events_persisted == 1
    assert before.pending == after.pending == []
    assert before.emailed == after.emailed == [new_generation]
    assert before.primed == after.primed == []
    assert row["emailed_at"] == emailed_at.isoformat()
    assert row["primed_at"] is None
    # Shadow mode still never advances the persisted generation.
    assert row["generation"] == 1


def test_shadow_event_query_is_bounded(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        for index in range(40):
            store._conn.execute(
                "insert into shadow_generation_events(event_id, identity_key, "
                "company, trigger, current_generation, proposed_generation, "
                "absence_epoch, observed_at) values (?, 'identity', 'Example', ?, "
                "1, 2, 0, ?)",
                (
                    f"bounded-{index:03d}",
                    TRIGGER_SEASON_CHANGE,
                    (BASE + timedelta(seconds=index)).isoformat(),
                ),
            )
        store._conn.commit()

        assert len(store.shadow_generation_events()) == 25
        assert len(store.shadow_generation_events(limit=5)) == 5
        assert len(store.shadow_generation_events(limit=10_000)) == 40
        assert len(store.shadow_generation_events(limit=0)) == 1
