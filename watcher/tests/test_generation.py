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
from watcher.seen_store import SEEN_SCHEMA_VERSION, SeenStore

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
