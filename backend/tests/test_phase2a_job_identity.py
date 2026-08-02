from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from app.dedupe import canonical_key, job_id
from app.ingest import analyze_rows


FIXTURE = Path(__file__).parent / "fixtures" / "phase2a_duplicate_watcher_ids.json"


def _rows() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _jobs_by_url(rows: list[dict]) -> dict[str, dict]:
    return {str(job["source_url"]).split("?", 1)[0]: job for job in rows}


def test_distinct_watcher_postings_disambiguate_legacy_id_collision() -> None:
    rows = _rows()
    assert canonical_key(rows[0]) == canonical_key(rows[1])
    assert job_id(rows[0]) == job_id(rows[1])

    jobs = analyze_rows(deepcopy(rows))
    by_url = _jobs_by_url(jobs)

    assert len(jobs) == 3
    assert len({job["id"] for job in jobs}) == len(jobs)
    assert by_url["https://example.test/jobs/backend-intern-a"]["id"] != (
        by_url["https://example.test/jobs/backend-intern-b"]["id"]
    )


def test_true_duplicate_merges_reordering_is_stable_and_unaffected_id_survives() -> None:
    rows = _rows()
    forward = analyze_rows(deepcopy(rows))
    reverse = analyze_rows(deepcopy(list(reversed(rows))))

    assert forward == reverse
    by_url = _jobs_by_url(forward)
    merged = by_url["https://example.test/jobs/backend-intern-a"]
    assert merged["extra"]["primary_source"] == "direct_ats"
    assert merged["extra"]["sources"] == ["direct_ats", "fixture_backstop"]

    unaffected = by_url["https://example.test/jobs/data-intern"]
    assert unaffected["id"] == job_id(rows[3])
