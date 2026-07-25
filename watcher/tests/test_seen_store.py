from datetime import datetime, timezone

import pytest

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
        store.mark_seen(github, seen_at=datetime(2026, 6, 9, tzinfo=timezone.utc))

        assert store.unseen([direct]) == []


def test_mark_many_seen_rolls_back_the_entire_batch_on_failure(tmp_path):
    timestamp = datetime(2026, 7, 18, tzinfo=timezone.utc)
    with SeenStore(tmp_path / "seen.sqlite") as store:
        with pytest.raises(KeyError):
            store.mark_many_seen(
                [job("first"), {"company": "missing id"}],
                seen_at=timestamp,
            )

        assert store.has_seen("first") is False
