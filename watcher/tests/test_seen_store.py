import sqlite3
from datetime import datetime, timezone

import pytest

from watcher.seen_store import SeenStore


def job(job_id="abc123", source="direct"):
    return {
        "id": job_id,
        "company": "Example",
        "title": "Software Engineer Intern",
        "location": "New York, NY",
        "source_url": "https://example.com/jobs/abc123",
        "extra": {"source": source},
    }


def test_seen_store_first_sighting_is_new_then_seen(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        first = job()

        assert store.unseen([first]) == [first]
        store.mark_emailed(first, emailed_at=datetime(2026, 6, 9, tzinfo=timezone.utc))

        assert store.unseen([first]) == []
        assert store.has_seen("abc123") is True


def test_seen_store_github_then_direct_is_not_new_by_normalized_url(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        github = job(job_id="github-wording", source="github")
        github["source_url"] = (
            "https://job-boards.greenhouse.io/example/jobs/12345"
        )
        direct = job(job_id="direct-wording", source="direct")
        direct["source_url"] = (
            "https://boards.greenhouse.io/example/jobs/12345?gh_jid=12345"
        )
        store.mark_emailed(github, emailed_at=datetime(2026, 6, 9, tzinfo=timezone.utc))

        assert store.unseen([direct]) == []


def test_mark_many_seen_rolls_back_the_entire_batch_on_failure(tmp_path):
    timestamp = datetime(2026, 7, 18, tzinfo=timezone.utc)
    with SeenStore(tmp_path / "seen.sqlite") as store:
        with pytest.raises(KeyError):
            store.mark_many_emailed(
                [job("first"), {"company": "missing id"}],
                emailed_at=timestamp,
            )

        assert store.has_seen("first") is False


def test_explicit_prime_uses_distinct_marker_and_remains_suppressed(tmp_path):
    timestamp = datetime(2026, 7, 28, tzinfo=timezone.utc)
    db_path = tmp_path / "seen.sqlite"
    seventh = job("seventh")

    with SeenStore(db_path) as store:
        store.mark_primed(seventh, primed_at=timestamp)
        assert store.unseen([seventh]) == []

    with sqlite3.connect(db_path) as conn:
        row = conn.execute("select emailed_at, primed_at from seen").fetchone()
    assert row == (None, "2026-07-28T00:00:00+00:00")


def test_legacy_blank_emailed_row_without_prime_remains_pending(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    legacy = job("legacy-pending")
    _create_legacy_seen_db(db_path, legacy, emailed_at=None)

    with SeenStore(db_path) as store:
        assert store.unseen([legacy]) == [legacy]


@pytest.mark.parametrize("company", ["Google", "Uber"])
def test_legacy_unemailed_company_row_is_returned_by_unseen(tmp_path, company):
    db_path = tmp_path / f"{company}.sqlite"
    legacy = job(f"{company.lower()}-legacy")
    legacy["company"] = company
    legacy["source_url"] = f"https://careers.example.test/jobs/{company.lower()}-intern-123"
    _create_legacy_seen_db(db_path, legacy, emailed_at=None)

    with SeenStore(db_path) as store:
        assert store.unseen([legacy]) == [legacy]


def test_already_emailed_legacy_row_remains_suppressed(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    legacy = job("legacy-emailed")
    _create_legacy_seen_db(
        db_path,
        legacy,
        emailed_at="2026-07-20T00:00:00+00:00",
    )

    with SeenStore(db_path) as store:
        assert store.unseen([legacy]) == []


def test_schema_migration_preserves_rows_and_adds_primed_marker(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    legacy = job("legacy-preserved")
    _create_legacy_seen_db(db_path, legacy, emailed_at=None)

    with SeenStore(db_path):
        pass

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("pragma table_info(seen)").fetchall()
        }
        row = conn.execute(
            "select job_id, company, title, first_seen, emailed_at, primed_at from seen"
        ).fetchone()
    assert "primed_at" in columns
    assert row == (
        "legacy-preserved",
        "Example",
        "Software Engineer Intern",
        "2026-07-01T00:00:00+00:00",
        None,
        None,
    )


def test_read_only_store_migrates_an_in_memory_snapshot_without_touching_disk(
    tmp_path,
):
    db_path = tmp_path / "legacy-read-only.sqlite"
    legacy = job("legacy-read-only")
    _create_legacy_seen_db(db_path, legacy, emailed_at=None)

    with SeenStore(db_path, read_only=True) as store:
        assert store.records()[0]["job_id"] == "legacy-read-only"
        with pytest.raises(RuntimeError, match="opened read-only"):
            store.mark_emailed(legacy)

    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("pragma table_info(seen)").fetchall()
        }
    assert "primed_at" not in columns
    assert "identity_key" not in columns


def test_seen_store_uses_same_requisition_identity_as_collection_dedupe(tmp_path):
    direct = job("content-direct", source="direct")
    direct["company"] = "Identity Co"
    direct["source_url"] = "https://careers.example.test/internships"
    direct["extra"].update(
        {
            "source_adapter": "greenhouse",
            "source_system": "greenhouse",
            "source_requisition_id": "REQ-123",
        }
    )
    github = job("content-github", source="github")
    github["company"] = "Identity Co"
    github["title"] = "SWE Intern display wording"
    github["location"] = "Remote"
    github["source_url"] = "https://careers.example.test/internships"
    github["extra"].update(
        {
            "source_adapter": "github_listings",
            "source_system": "greenhouse",
            "source_requisition_id": "REQ-123",
        }
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.mark_emailed(direct, emailed_at=datetime(2026, 7, 28, tzinfo=timezone.utc))
        assert store.unseen([github]) == []


def _create_legacy_seen_db(path, legacy_job, *, emailed_at):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            create table seen(
              job_id text primary key,
              company text,
              title text,
              url text,
              first_source text,
              first_seen text,
              emailed_at text
            )
            """
        )
        conn.execute(
            """
            insert into seen(job_id, company, title, url, first_source, first_seen, emailed_at)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_job["id"],
                legacy_job["company"],
                legacy_job["title"],
                legacy_job["source_url"],
                legacy_job["extra"]["source"],
                "2026-07-01T00:00:00+00:00",
                emailed_at,
            ),
        )
