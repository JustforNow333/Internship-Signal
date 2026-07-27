from __future__ import annotations

import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.app.ingest import analyze_rows
from scripts import build_us_rolefit_benchmark as exporter
from scripts.scoring_benchmark_common import BenchmarkError, ordered_groups
from watcher.eligibility import OUTSIDE_US, assess_us_location
from watcher.sources.base import make_row

AS_OF = date(2026, 7, 26)


def canonical_row(
    index: int,
    *,
    title: str = "Software Engineer Intern",
    location: str = "Boston, MA, United States",
    remote_status: str = "",
    description: str = "Build backend APIs with Python and production code.",
    requirements: str = "Python, Java, SQL, REST APIs, Git",
) -> dict:
    return make_row(
        source="direct",
        source_adapter="fake",
        company=f"Company {index}",
        title=f"{title} {index}",
        location=location,
        remote_status=remote_status,
        compensation="$30/hour",
        description=description,
        requirements=requirements,
        source_url=f"https://jobs.example.test/{index}",
        internship_type="Intern",
    )


def write_watchlist(path: Path) -> None:
    path.write_text(
        "defaults:\n"
        '  terms: ["Summer 2027"]\n'
        "  github_listing_urls: []\n"
        "companies:\n"
        "  - name: Example\n"
        "    ats: github_only\n",
        encoding="utf-8",
    )


def analyzed_fixture() -> list[dict]:
    rows = [
        canonical_row(1),
        canonical_row(2, location="Berlin, Germany"),
        canonical_row(
            3,
            location="",
            remote_status="Remote — United States only",
        ),
        canonical_row(
            4,
            location="Toronto, Canada; New York, NY, United States",
        ),
        canonical_row(5, location="8 Locations"),
        canonical_row(
            6,
            title="Electrical Engineer Intern",
            description="Design circuits and test hardware.",
            requirements="Circuit design, PCB, lab instrumentation",
        ),
        canonical_row(
            7,
            title="Marketing Intern",
            description="Plan campaigns and customer newsletters.",
            requirements="Communications and market research",
        ),
        canonical_row(
            8,
            title="Machine Learning PhD Intern",
            description="Research computer vision models.",
            requirements="Currently pursuing a PhD in computer science.",
        ),
    ]
    return analyze_rows(rows, today=AS_OF)


def test_candidate_pool_excludes_only_confidently_international_locations():
    jobs = analyzed_fixture()
    candidates = exporter.candidate_pool(jobs)
    by_index = {
        int(job["source_url"].rsplit("/", 1)[-1]): job
        for job in jobs
    }

    assert by_index[2] not in candidates
    assert assess_us_location(by_index[2]).status == OUTSIDE_US
    assert by_index[1] in candidates
    assert by_index[3] in candidates
    assert by_index[4] in candidates
    assert by_index[5] in candidates
    assert {assess_us_location(job).status for job in candidates} == {"us", "ambiguous"}


def test_sampling_is_reproducible_deduplicated_and_assigns_expected_cohorts():
    jobs = exporter.candidate_pool(analyzed_fixture())
    duplicate = dict(jobs[0])

    first, first_groups, first_available = exporter.sample_jobs(
        [*jobs, duplicate],
        seed=20260726,
        random_count=20,
        likely_match_count=20,
        difficult_negative_count=20,
    )
    second, second_groups, second_available = exporter.sample_jobs(
        [duplicate, *reversed(jobs)],
        seed=20260726,
        random_count=20,
        likely_match_count=20,
        difficult_negative_count=20,
    )

    assert [job["id"] for job in first] == [job["id"] for job in second]
    assert first_groups == second_groups
    assert first_available == second_available
    assert len(first) == len({job["id"] for job in first}) == len(jobs)

    by_index = {
        int(job["source_url"].rsplit("/", 1)[-1]): job
        for job in first
    }
    assert "likely_match" in first_groups[by_index[1]["id"]]
    assert "likely_match" in first_groups[by_index[3]["id"]]
    assert "difficult_negative" in first_groups[by_index[6]["id"]]
    assert "difficult_negative" in first_groups[by_index[7]["id"]]
    assert "difficult_negative" in first_groups[by_index[8]["id"]]
    assert all("random" in groups for groups in first_groups.values())


def test_us_rolefit_cohort_names_have_stable_canonical_order():
    assert ordered_groups(
        ["difficult_negative", "likely_match", "random", "likely_match"]
    ) == ["random", "likely_match", "difficult_negative"]


def test_export_writes_blind_hashed_artifacts_without_touching_old_prefix(tmp_path):
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    private = tmp_path / "private"
    old_paths = [
        private / "scoring_20260724_labels.csv",
        private / "scoring_20260724_labels_blank_backup.csv",
        private / "scoring_20260724_rows.jsonl",
        private / "scoring_20260724_predictions.json",
        private / "scoring_20260724_manifest.json",
        private / "scoring_20260724_report.md",
        private / "scoring_20260724_metrics.json",
    ]
    private.mkdir()
    for index, path in enumerate(old_paths):
        path.write_text(f"historical-{index}\n", encoding="utf-8")
    old_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in old_paths}

    rows = [
        canonical_row(1),
        canonical_row(2, location="Berlin, Germany"),
        canonical_row(3, location="", remote_status="Remote - U.S. only"),
        canonical_row(4, location="Toronto, Canada; Boston, MA, United States"),
        canonical_row(5, location="Location varies"),
        canonical_row(
            6,
            title="Naval Architect Co-op",
            description="Design naval vessels and mechanical structures.",
            requirements="Naval architecture",
        ),
        canonical_row(
            7,
            title="Quality Engineering Intern",
            description="Inspect manufacturing quality processes.",
            requirements="Quality systems and manufacturing",
        ),
    ]

    manifest = exporter.export_benchmark(
        watchlist_path=watchlist,
        as_of=AS_OF,
        seed=20260726,
        output_prefix=private / "scoring_us_rolefit_20260726",
        random_count=20,
        likely_match_count=20,
        difficult_negative_count=20,
        collector=lambda _config: (rows, []),
        created_at=datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
    )
    paths = exporter.output_paths(private / "scoring_us_rolefit_20260726")

    assert all(path.exists() for path in paths.values())
    assert manifest["benchmark_kind"] == "us_rolefit"
    assert manifest["seed"] == 20260726
    assert manifest["candidate_pool_count"] == 6
    assert manifest["outside_us_excluded_count"] == 1
    assert manifest["location_gate"]["all_selected_passed_or_ambiguous"] is True
    assert set(manifest["location_status_counts"]) == {"ambiguous", "us"}
    assert manifest["source_counts"]["source"] == {"direct": manifest["selected_count"]}

    with paths["labels"].open("r", encoding="utf-8-sig", newline="") as handle:
        label_rows = list(csv.DictReader(handle))
    assert label_rows
    assert all(
        not row[field]
        for row in label_rows
        for field in exporter.HUMAN_LABEL_COLUMNS
    )
    assert all(
        field not in label_rows[0]
        for field in (
            "fit_score",
            "watcher_eligible",
            "watcher_action",
            "role_track",
            "watcher_ineligible_reason",
        )
    )
    assert len({row["job_id"] for row in label_rows}) == len(label_rows)

    frozen_rows = [
        json.loads(line)
        for line in paths["rows"].read_text(encoding="utf-8").splitlines()
    ]
    assert all(assess_us_location(row).status != OUTSIDE_US for row in frozen_rows)
    exporter.validate_frozen_artifact_hashes(manifest, paths)
    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    assert metrics["coverage"]["labeled_rows"] == 0
    assert metrics["coverage"]["unlabeled_rows"] == manifest["selected_count"]

    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in old_paths
    } == old_hashes


def test_frozen_artifact_hash_validation_detects_tampering(tmp_path):
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    prefix = tmp_path / "scoring_us_rolefit"
    manifest = exporter.export_benchmark(
        watchlist_path=watchlist,
        as_of=AS_OF,
        seed=7,
        output_prefix=prefix,
        random_count=1,
        likely_match_count=1,
        difficult_negative_count=1,
        collector=lambda _config: ([canonical_row(1)], []),
    )
    paths = exporter.output_paths(prefix)

    original_watchlist = watchlist.read_bytes()
    watchlist.write_text("changed: true\n", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="watchlist_sha256 mismatch"):
        exporter.validate_frozen_artifact_hashes(manifest, paths)
    watchlist.write_bytes(original_watchlist)

    paths["rows"].write_text("{}\n", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="frozen_rows_sha256 mismatch"):
        exporter.validate_frozen_artifact_hashes(manifest, paths)


def test_exporter_has_no_watcher_state_alumni_or_notification_path():
    source = Path(exporter.__file__).read_text(encoding="utf-8")

    assert "run_once" not in source
    assert "SeenStore" not in source
    assert "mark_many_seen" not in source
    assert "load_default_alumni" not in source
    assert "send_digest" not in source
    assert "filter_matches" not in source
