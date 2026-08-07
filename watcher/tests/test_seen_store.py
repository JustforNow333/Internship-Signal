import sqlite3
from datetime import datetime, timezone

import pytest

from backend.app.dedupe import canonical_key, job_id
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


def test_emailed_fragment_posting_suppresses_equivalent_later_url(tmp_path):
    emailed = job("shared-content")
    emailed["source_url"] = (
        "https://careers.example.test/jobs?utm_source=direct#/job/ABC123"
    )
    later = job("changed-content")
    later["source_url"] = (
        "https://careers.example.test/jobs?ref=feed#?JOBID=abc123"
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.mark_emailed(
            emailed,
            emailed_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )

        assert store.unseen([later]) == []


def test_emailed_fragment_posting_does_not_suppress_different_fragment_id(
    tmp_path,
):
    emailed = job("legacy-collision")
    emailed["source_url"] = "https://careers.example.test/jobs#/job/ABC123"
    different = job("legacy-collision")
    different["source_url"] = "https://careers.example.test/jobs#/job/XYZ789"

    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.mark_emailed(
            emailed,
            emailed_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )

        selection = store.partition([different])

    assert selection.pending == [different]
    assert selection.emailed == []


def test_legacy_seen_raw_fragment_url_suppresses_same_current_posting(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    legacy = job("legacy-fragment-content")
    legacy["source_url"] = "https://careers.example.test/jobs#/job/ABC123"
    _create_legacy_seen_db(
        db_path,
        legacy,
        emailed_at="2026-08-01T00:00:00+00:00",
    )
    current = job("current-fragment-content")
    current["source_url"] = (
        "https://careers.example.test/jobs?utm_source=later#jobId=abc123"
    )

    with SeenStore(db_path) as store:
        assert store.unseen([current]) == []


def test_emailed_plain_c_does_not_suppress_cpp_or_csharp(tmp_path):
    plain_c = _fallback_job("C Intern", "Austin, TX")
    cpp = _fallback_job("C++ Intern", "Austin, TX")
    csharp = _fallback_job("C# Intern", "Austin, TX")

    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.mark_emailed(
            plain_c,
            emailed_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )

        selection = store.partition([cpp, csharp])

    assert selection.pending == [cpp, csharp]
    assert selection.emailed == []


def test_emailed_springfield_illinois_does_not_suppress_massachusetts(tmp_path):
    illinois = _fallback_job("Software Engineer Intern", "Springfield, IL")
    massachusetts = _fallback_job(
        "Software Engineer Intern",
        "Springfield, MA",
    )

    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.mark_emailed(
            illinois,
            emailed_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )

        selection = store.partition([massachusetts])

    assert selection.pending == [massachusetts]
    assert selection.emailed == []


def test_legacy_fallback_id_collision_does_not_suppress_distinct_language(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    plain_c = _fallback_job("C Intern", "Austin, TX")
    cpp = _fallback_job("C++ Intern", "Austin, TX")
    assert plain_c["id"] == cpp["id"]

    _store_with_legacy_fallback_identity(db_path, plain_c)

    with SeenStore(db_path) as store:
        selection = store.partition([cpp])

    assert selection.pending == [cpp]
    assert selection.emailed == []


def test_same_posting_with_legacy_fallback_identity_remains_suppressed(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    stored = _fallback_job("C++ Intern", "Springfield, IL")
    current = _fallback_job("c ++ intern", "  SPRINGFIELD ,  il ")
    assert stored["id"] == current["id"]

    _store_with_legacy_fallback_identity(db_path, stored)

    with SeenStore(db_path) as store:
        selection = store.partition([current])

    assert selection.pending == []
    assert selection.emailed == [current]


def _fallback_job(title, location):
    posting = job(source="github")
    posting.update(
        {
            "title": title,
            "location": location,
            "source_url": "https://careers.example.test/internships",
        }
    )
    posting["id"] = job_id(posting)
    return posting


def _store_with_legacy_fallback_identity(path, posting):
    with SeenStore(path) as store:
        store.mark_emailed(
            posting,
            emailed_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "update seen set identity_key = ?",
            (f"fallback|{canonical_key(posting)}",),
        )


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


def test_legacy_duplicate_query_url_row_still_suppresses_after_canonicalization(tmp_path):
    """A row stored before the duplicate-parameter fix must keep suppressing.

    The stored `identity_key` was computed with the old canonicalization, so the
    exact-identity arm misses. Reconstructing the stored row and re-running the
    shared matcher recomputes both sides with current logic, which keeps the
    posting suppressed instead of re-notifying it.
    """

    db_path = tmp_path / "seen.sqlite"
    doubled = "https://careers.aqr.com/jobs?gh_jid=7895562&gh_jid=7895562"
    with SeenStore(db_path) as store:
        store._conn.execute(
            "insert into seen(job_id, company, title, url, first_source, "
            "first_seen, emailed_at, identity_key) values (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-aqr",
                "AQR Capital",
                "2027 Trading Summer Analyst",
                doubled,
                "github",
                "2026-07-16T05:06:15+00:00",
                "2026-07-28T06:36:56+00:00",
                f"url|{doubled}",
            ),
        )
        store._conn.commit()

        current = {
            "id": "aqr-current",
            "company": "AQR Capital",
            "title": "2027 Trading Summer Analyst",
            "location": "Greenwich, CT",
            "source_url": "https://careers.aqr.com/jobs?gh_jid=7895562",
            "extra": {"source": "github"},
        }
        selection = store.partition([current])

    assert selection.emailed == [current]
    assert selection.pending == []
