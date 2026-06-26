from datetime import datetime, timezone

from watcher.seen_store import SeenStore


def job(job_id="abc123", source="direct"):
    return {
        "id": job_id,
        "company": "Example",
        "title": "Software Engineer Intern",
        "source_url": "https://example.com/jobs/abc123",
        "extra": {"source": source},
    }


def test_seen_store_first_sighting_is_new_then_seen(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        first = job()

        assert store.unseen([first]) == [first]
        store.mark_seen(first, seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc))

        assert store.unseen([first]) == []
        assert store.has_seen("abc123")


def test_seen_store_github_then_direct_is_not_new(tmp_path):
    with SeenStore(tmp_path / "seen.sqlite") as store:
        store.mark_seen(job(source="github"), seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc))

        assert store.unseen([job(source="direct")]) == []

