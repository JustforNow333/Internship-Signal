from __future__ import annotations

import csv
import hashlib
import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.app.ingest import analyze_rows
from scripts import build_us_holdout_benchmark as holdout
from scripts.build_scoring_benchmark import baseline_prediction, freeze_job
from scripts.evaluate_scoring_benchmark import evaluate_benchmark
from scripts.scoring_benchmark_common import (
    HUMAN_LABEL_COLUMNS,
    BenchmarkError,
    json_bytes,
    render_csv_bytes,
    sha256_bytes,
)
from watcher.eligibility import OUTSIDE_US, assess_us_location
from watcher.sources.base import make_row

AS_OF = date(2026, 7, 27)
SEED = 20260727
COMMIT = "a" * 40


def canonical_row(
    index: int,
    *,
    company: str | None = None,
    title: str = "Software Engineer Intern",
    location: str = "Boston, MA, United States",
    source_url: str | None = None,
    description: str = "Build backend APIs with Python and production code.",
    requirements: str = "Python, Java, SQL, REST APIs, Git",
    source: str = "direct",
    source_adapter: str = "fake",
    extra: dict | None = None,
) -> dict:
    return make_row(
        source=source,
        source_adapter=source_adapter,
        company=company or f"Company {index}",
        title=f"{title} {index}",
        location=location,
        compensation="$30/hour for a 12-week internship",
        description=description,
        requirements=requirements,
        source_url=source_url or f"https://jobs.example.test/{index}",
        internship_type="Intern",
        extra=extra,
    )


def analyzed(rows: list[dict]) -> list[dict]:
    return analyze_rows(rows, today=AS_OF)


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


def write_prior_benchmark(prefix: Path, rows: list[dict]) -> dict[str, Path]:
    jobs = analyzed(rows)
    frozen_rows = [freeze_job(job) for job in jobs]
    predictions = {
        str(job["id"]): baseline_prediction(job, ("random",))
        for job in jobs
    }
    rows_payload = b"".join(json_bytes(row, indent=None) for row in frozen_rows)
    predictions_payload = json_bytes(predictions)
    selected_ids = [str(job["id"]) for job in jobs]
    ids_payload = ("\n".join(selected_ids) + "\n").encode("utf-8")
    paths = holdout.prior_paths(prefix)
    paths["rows"].parent.mkdir(parents=True, exist_ok=True)
    paths["rows"].write_bytes(rows_payload)
    paths["predictions"].write_bytes(predictions_payload)
    paths["manifest"].write_bytes(
        json_bytes(
            {
                "as_of_date": AS_OF.isoformat(),
                "selected_count": len(jobs),
                "hashes": {
                    "frozen_rows_sha256": sha256_bytes(rows_payload),
                    "baseline_predictions_sha256": sha256_bytes(
                        predictions_payload
                    ),
                    "selected_job_ids_sha256": sha256_bytes(ids_payload),
                },
            }
        )
    )
    return paths


def make_private_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    private = tmp_path / "evaluation" / "private"
    prior_one = private / "scoring_20260724"
    prior_two = private / "scoring_us_rolefit_20260726"
    write_prior_benchmark(prior_one, [canonical_row(1)])
    write_prior_benchmark(prior_two, [canonical_row(2)])
    return private, prior_one, prior_two


def test_sampling_is_deterministic_and_stable_for_same_seed():
    jobs = analyzed(
        [
            canonical_row(index)
            for index in range(10, 30)
        ]
        + [
            canonical_row(
                30,
                title="Electrical Engineer Intern",
                description="Design circuits and hardware.",
                requirements="PCB and lab instrumentation",
            ),
            canonical_row(
                31,
                title="Consumer Insights Intern",
                description="Run consumer surveys and market research.",
                requirements="Survey design",
            ),
        ]
    )

    first = holdout.sample_jobs(
        jobs,
        seed=SEED,
        random_count=8,
        likely_match_count=6,
        difficult_negative_count=6,
    )
    second = holdout.sample_jobs(
        list(reversed(jobs)),
        seed=SEED,
        random_count=8,
        likely_match_count=6,
        difficult_negative_count=6,
    )

    assert [job["id"] for job in first[0]] == [job["id"] for job in second[0]]
    assert first[1:] == second[1:]


def test_sample_uses_complete_pool_when_random_request_exceeds_availability():
    jobs = analyzed([canonical_row(index) for index in range(10, 15)])

    selected, memberships, available = holdout.sample_jobs(
        jobs,
        seed=SEED,
        random_count=180,
        likely_match_count=50,
        difficult_negative_count=80,
    )

    assert len(selected) == len(jobs)
    assert available["random"] == len(jobs)
    assert all("random" in memberships[str(job["id"])] for job in selected)


def test_prior_exclusion_uses_id_url_and_fallback_keys_independently():
    prior_job = analyzed([canonical_row(1)])[0]
    prior = holdout.PriorBenchmarkIndex(
        job_ids=frozenset({str(prior_job["id"])}),
        normalized_urls=frozenset(
            {holdout.norm_url(str(prior_job["source_url"]))}
        ),
        fallback_keys=frozenset({holdout.canonical_key(prior_job)}),
        inputs=(),
    )
    id_only = analyzed(
        [
            canonical_row(
                10,
                company="Different",
                title="Data Intern",
                source_url="https://new.example.test/id-only",
            )
        ]
    )[0]
    id_only["id"] = prior_job["id"]
    url_only = analyzed(
        [
            canonical_row(
                11,
                source_url=f"{prior_job['source_url']}?utm_source=holdout",
            )
        ]
    )[0]
    fallback_only = analyzed(
        [
            canonical_row(
                1,
                source_url="https://new.example.test/fallback-only",
            )
        ]
    )[0]
    fallback_only["id"] = "deliberately-different-stable-id"
    unseen = analyzed([canonical_row(12)])[0]

    remaining, counts = holdout.exclude_prior_overlaps(
        [id_only, url_only, fallback_only, unseen],
        prior,
    )

    assert remaining == [unseen]
    assert counts == {
        "job_id": 1,
        "normalized_url": 1,
        "fallback_company_title_location": 1,
        "unique_candidates_excluded": 3,
        "candidates_remaining": 1,
    }


def test_prior_loader_validates_hashes_without_using_labels(tmp_path):
    private, prior_one, prior_two = make_private_inputs(tmp_path)
    labels_path = Path(f"{prior_one}_labels.csv")
    labels_path.write_text("DO NOT READ\n", encoding="utf-8")

    index = holdout.load_prior_benchmark_index(
        [prior_one, prior_two],
        private_root=private,
    )

    assert len(index.job_ids) == 2
    assert "labels" not in holdout.prior_paths(prior_one)
    paths = holdout.prior_paths(prior_one)
    paths["rows"].write_text("{}\n", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="frozen_rows_sha256 mismatch"):
        holdout.load_prior_benchmark_index(
            [prior_one, prior_two],
            private_root=private,
        )


@pytest.mark.parametrize(
    ("git_result", "message"),
    [
        ((COMMIT, True), "tracked working tree is dirty"),
        (("b" * 40, False), "HEAD mismatch"),
        (("unknown", "unknown"), "cannot verify"),
    ],
)
def test_export_refuses_dirty_unknown_or_unexpected_commit_before_collection(
    tmp_path,
    git_result,
    message,
):
    private = tmp_path / "evaluation" / "private"
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    calls = []

    with pytest.raises(BenchmarkError, match=message):
        holdout.export_benchmark(
            watchlist_path=watchlist,
            prior_prefixes=[private / "missing"],
            as_of=AS_OF,
            seed=SEED,
            expected_commit=COMMIT,
            output_prefix=private / "scoring_us_holdout_20260727",
            collector=lambda _config: calls.append(True),
            git_state=lambda: git_result,
            private_root=private,
        )

    assert calls == []
    assert not list(private.glob("scoring_us_holdout_20260727*"))


def test_export_refuses_nonprivate_artifact_path_before_collection(tmp_path):
    private, prior_one, prior_two = make_private_inputs(tmp_path)
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    calls = []

    with pytest.raises(BenchmarkError, match="must remain under"):
        holdout.export_benchmark(
            watchlist_path=watchlist,
            prior_prefixes=[prior_one, prior_two],
            as_of=AS_OF,
            seed=SEED,
            expected_commit=COMMIT,
            output_prefix=tmp_path / "scoring_us_holdout_20260727",
            collector=lambda _config: calls.append(True),
            git_state=lambda: (COMMIT, False),
            private_root=private,
        )

    assert calls == []


def test_export_refuses_to_freeze_if_tree_becomes_dirty_during_collection(
    tmp_path,
):
    private, prior_one, prior_two = make_private_inputs(tmp_path)
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    states = iter(((COMMIT, False), (COMMIT, True)))
    prefix = private / "scoring_us_holdout_20260727"

    with pytest.raises(BenchmarkError, match="tracked working tree is dirty"):
        holdout.export_benchmark(
            watchlist_path=watchlist,
            prior_prefixes=[prior_one, prior_two],
            as_of=AS_OF,
            seed=SEED,
            expected_commit=COMMIT,
            output_prefix=prefix,
            collector=lambda _config: ([canonical_row(10)], []),
            git_state=lambda: next(states),
            private_root=private,
        )

    assert not list(private.glob("scoring_us_holdout_20260727*"))


def test_repository_ignores_private_holdout_artifact_paths():
    result = subprocess.run(
        [
            "git",
            "check-ignore",
            "-q",
            "evaluation/private/scoring_us_holdout_20990101_manifest.json",
        ],
        cwd=holdout.REPO_ROOT,
        check=False,
    )

    assert result.returncode == 0


def test_export_writes_blind_independent_holdout_and_preserves_prior_inputs(
    tmp_path,
    monkeypatch,
):
    private, prior_one, prior_two = make_private_inputs(tmp_path)
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    prior_paths = [
        path
        for prefix in (prior_one, prior_two)
        for path in holdout.prior_paths(prefix).values()
    ]
    prior_hashes = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in prior_paths
    }
    rows = [
        canonical_row(1),
        canonical_row(2),
        canonical_row(
            3,
            extra={
                "location": {"city": "Boston", "country": "United States"},
                "sources": ["direct_ats", "simplify"],
                "source_details": {
                    "direct_ats": {
                        "source_adapter": "greenhouse",
                        "active": True,
                    }
                },
                "smtp_password": "DO_NOT_EXPORT",
                "alumni": [{"name": "Private Person"}],
            },
        ),
        canonical_row(
            4,
            title="Electrical Engineer Intern",
            description="Design circuits and test hardware.",
            requirements="PCB and lab instrumentation",
        ),
        canonical_row(
            5,
            title="Consumer Insights Intern",
            description="Conduct surveys and qualitative market research.",
            requirements="Survey design and presentations",
            source="github",
            source_adapter="github_listings",
        ),
        canonical_row(
            6,
            title="Machine Learning PhD Intern",
            description="Research computer vision models.",
            requirements="Currently pursuing a PhD in computer science.",
            location="8 Locations",
        ),
        canonical_row(7, location="Berlin, Germany"),
    ]

    def forbidden(*_args, **_kwargs):
        raise AssertionError("production-only path was called")

    monkeypatch.setattr("watcher.alumni.load_default_alumni", forbidden)
    monkeypatch.setattr("watcher.notify.send_digest", forbidden)
    monkeypatch.setattr("watcher.seen_store.SeenStore", forbidden)

    prefix = private / "scoring_us_holdout_20260727"
    manifest = holdout.export_benchmark(
        watchlist_path=watchlist,
        prior_prefixes=[prior_one, prior_two],
        as_of=AS_OF,
        seed=SEED,
        expected_commit=COMMIT,
        output_prefix=prefix,
        random_count=20,
        likely_match_count=20,
        difficult_negative_count=20,
        collector=lambda _config: (
            rows,
            [
                "feed failed https://user:password@example.test/jobs"
                "?access_token=DO_NOT_EXPORT"
            ],
        ),
        git_state=lambda: (COMMIT, False),
        created_at=datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
        private_root=private,
    )
    paths = holdout.output_paths(prefix, private_root=private)

    assert all(path.exists() for path in paths.values())
    assert manifest["benchmark_kind"] == "us_holdout"
    assert manifest["construction_version"] == 1
    assert manifest["frozen_git_commit"] == COMMIT
    assert manifest["git_dirty"] is False
    assert manifest["candidate_pool_before_prior_exclusion"] == 6
    assert manifest["candidate_pool_size"] == 4
    assert manifest["outside_us_excluded_count"] == 1
    assert manifest["prior_overlap_exclusions"] == {
        "job_id": 2,
        "normalized_url": 2,
        "fallback_company_title_location": 2,
        "unique_candidates_excluded": 2,
        "candidates_remaining": 4,
    }
    assert manifest["selected_prior_overlap_counts"] == {
        "job_id": 0,
        "normalized_url": 0,
        "fallback_company_title_location": 0,
    }
    assert manifest["runtime_isolation"] == {
        "email_disabled": True,
        "alumni_loaded": False,
        "seen_state_used": False,
        "production_seen_state_modified": False,
    }
    serialized_manifest = paths["manifest"].read_text(encoding="utf-8")
    assert "DO_NOT_EXPORT" not in serialized_manifest
    assert "password" not in serialized_manifest

    with paths["labels"].open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        label_rows = list(csv.DictReader(handle))
    assert label_rows
    assert all(
        not row[field]
        for row in label_rows
        for field in HUMAN_LABEL_COLUMNS
    )
    assert all(
        field not in label_rows[0]
        for field in (
            "fit_score",
            "watcher_eligible",
            "watcher_action",
            "role_track",
            "watcher_ineligible_reason",
            "fit_explanation",
        )
    )

    frozen_rows = [
        json.loads(line)
        for line in paths["rows"].read_text(encoding="utf-8").splitlines()
    ]
    assert all(
        assess_us_location(row).status != OUTSIDE_US for row in frozen_rows
    )
    structured = next(row for row in frozen_rows if row["company"] == "Company 3")
    assert structured["extra"]["location"]["country"] == "United States"
    assert structured["extra"]["sources"] == ["direct_ats", "simplify"]
    assert "smtp_password" not in structured["extra"]
    assert "alumni" not in structured["extra"]

    metrics = json.loads(paths["metrics"].read_text(encoding="utf-8"))
    assert metrics["coverage"]["labeled_rows"] == 0
    assert metrics["coverage"]["evaluated_rows"] == 0
    assert metrics["coverage"]["labeled_coverage"] == 0
    assert metrics["headline_random_sample"]["current"]["evaluated_count"] == 0
    assert "Label coverage: 0.0%" in paths["report"].read_text(encoding="utf-8")
    holdout.validate_frozen_artifacts(
        manifest,
        paths,
        prior=holdout.load_prior_benchmark_index(
            [prior_one, prior_two],
            private_root=private,
        ),
        private_root=private,
    )
    assert {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in prior_paths
    } == prior_hashes


def test_evaluator_uses_only_random_cohort_for_headline_metrics(tmp_path):
    private, prior_one, prior_two = make_private_inputs(tmp_path)
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    rows = [
        canonical_row(10),
        canonical_row(11),
        canonical_row(
            12,
            title="Electrical Engineer Intern",
            description="Design circuits and hardware.",
            requirements="PCB",
        ),
        canonical_row(
            13,
            title="Marketing Intern",
            description="Plan campaigns.",
            requirements="Communication",
        ),
    ]
    prefix = private / "scoring_us_holdout_20260727"
    holdout.export_benchmark(
        watchlist_path=watchlist,
        prior_prefixes=[prior_one, prior_two],
        as_of=AS_OF,
        seed=SEED,
        expected_commit=COMMIT,
        output_prefix=prefix,
        random_count=1,
        likely_match_count=4,
        difficult_negative_count=4,
        collector=lambda _config: (rows, []),
        git_state=lambda: (COMMIT, False),
        private_root=private,
    )
    paths = holdout.output_paths(prefix, private_root=private)
    with paths["labels"].open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as handle:
        headers = list(csv.DictReader(handle).fieldnames or ())
        handle.seek(0)
        label_rows = list(csv.DictReader(handle))
    assert len(label_rows) > 1
    for row in label_rows:
        row["human_eligible"] = "yes"
    paths["labels"].write_bytes(render_csv_bytes(headers, label_rows))

    metrics = evaluate_benchmark(
        labels_path=paths["labels"],
        rows_path=paths["rows"],
        manifest_path=paths["manifest"],
        baseline_predictions_path=paths["predictions"],
        report_path=paths["report"],
        metrics_path=paths["metrics"],
    )

    random_rows = [
        row for row in label_rows if "random" in row["sample_groups"].split("|")
    ]
    assert len(random_rows) == 1
    assert metrics["coverage"]["evaluated_rows"] == len(label_rows)
    assert (
        metrics["headline_random_sample"]["current"]["evaluated_count"]
        == len(random_rows)
    )


def test_frozen_hash_validation_detects_holdout_or_prior_tampering(tmp_path):
    private, prior_one, prior_two = make_private_inputs(tmp_path)
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    prefix = private / "scoring_us_holdout_20260727"
    manifest = holdout.export_benchmark(
        watchlist_path=watchlist,
        prior_prefixes=[prior_one, prior_two],
        as_of=AS_OF,
        seed=SEED,
        expected_commit=COMMIT,
        output_prefix=prefix,
        random_count=1,
        likely_match_count=0,
        difficult_negative_count=0,
        collector=lambda _config: ([canonical_row(10)], []),
        git_state=lambda: (COMMIT, False),
        private_root=private,
    )
    paths = holdout.output_paths(prefix, private_root=private)
    prior = holdout.load_prior_benchmark_index(
        [prior_one, prior_two],
        private_root=private,
    )

    paths["rows"].write_text("{}\n", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="frozen_rows_sha256 mismatch"):
        holdout.validate_frozen_artifacts(
            manifest,
            paths,
            prior=prior,
            private_root=private,
        )

    holdout.prior_paths(prior_one)["manifest"].write_text(
        "{}\n",
        encoding="utf-8",
    )
    with pytest.raises(BenchmarkError, match="prior benchmark manifest hash changed"):
        holdout.validate_prior_inputs(prior)


def test_exporter_has_no_notification_alumni_seen_or_digest_filter_path():
    source = Path(holdout.__file__).read_text(encoding="utf-8")

    assert "filter_matches" not in source
    assert "run_once" not in source
    assert "SeenStore" not in source
    assert "mark_many_seen" not in source
    assert "load_default_alumni" not in source
    assert "send_digest" not in source
