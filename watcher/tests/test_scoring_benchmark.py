from __future__ import annotations

import csv
import hashlib
import inspect
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.app.dedupe import job_id
from backend.app.ingest import analyze_rows
from scripts import build_scoring_benchmark as exporter
from scripts import evaluate_scoring_benchmark as evaluator
from scripts.scoring_benchmark_common import BenchmarkError, csv_safe, render_csv_bytes
from watcher.sources.base import make_row


AS_OF = date(2026, 7, 20)


def canonical_row(
    index: int,
    *,
    title: str = "Software Engineer Intern",
    company: str | None = None,
    extra: dict | None = None,
    deadline: str = "",
    description: str = "Build backend APIs with mentorship and production code.",
    requirements: str = "Python, Java, SQL, REST APIs, Git",
) -> dict:
    return make_row(
        source="direct",
        source_adapter="fake",
        extra=extra,
        company=company or f"Company {index}",
        title=f"{title} {index}",
        location=f"City {index}, NY",
        compensation="$30/hour",
        description=description,
        requirements=requirements,
        source_url=f"https://jobs.example.test/{index}",
        deadline=deadline,
        internship_type="Intern",
    )


def fake_job(index: int, *, fit: int | None = None, track: str = "general_swe") -> dict:
    fit = index if fit is None else fit
    return {
        "id": f"job-{index:03d}",
        "company": f"Company {index}",
        "title": f"Intern {index}",
        "score": {
            "fit_score": fit,
            "total": 50 + index,
            "watcher_eligible": fit > 0,
            "role_track": track,
        },
        "role_classification": {"role_track": track},
    }


def write_watchlist(path: Path) -> None:
    path.write_text(
        "defaults:\n"
        "  terms: [\"Summer 2027\"]\n"
        "  github_listing_urls: []\n"
        "companies:\n"
        "  - name: Example\n"
        "    ats: github_only\n",
        encoding="utf-8",
    )


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or ()), list(reader)


def test_sampling_is_deterministic_and_seeded_without_duplicates():
    candidates = [fake_job(index, track="unknown" if index % 3 == 0 else "general_swe") for index in range(60)]

    first, first_groups = exporter.sample_jobs(
        candidates, seed=41, random_count=20, top_count=8, difficult_count=12
    )
    second, second_groups = exporter.sample_jobs(
        list(reversed(candidates)), seed=41, random_count=20, top_count=8, difficult_count=12
    )
    different, different_groups = exporter.sample_jobs(
        candidates, seed=42, random_count=20, top_count=8, difficult_count=12
    )

    assert [job["id"] for job in first] == [job["id"] for job in second]
    assert first_groups == second_groups
    assert len({job["id"] for job in first}) == len(first)
    first_random = [job["id"] for job in first if "random" in first_groups[job["id"]]]
    different_random = [
        job["id"] for job in different if "random" in different_groups[job["id"]]
    ]
    assert first_random != different_random


def test_sampling_preserves_overlap_membership_and_handles_small_pools():
    candidates = [fake_job(index, track="unknown") for index in range(3)]

    selected, memberships = exporter.sample_jobs(
        candidates, seed=1, random_count=100, top_count=30, difficult_count=50
    )

    assert len(selected) == 3
    assert all(memberships[job["id"]] == ["random", "top", "difficult"] for job in selected)


def test_candidate_pool_keeps_currently_ineligible_open_internships(monkeypatch):
    jobs = analyze_rows(
        [
            canonical_row(1),
            canonical_row(
                2,
                title="Electrical Engineer Intern",
                description="Design circuits and test hardware.",
                requirements="Circuit analysis and lab instrumentation",
            ),
            canonical_row(3, title="Software Engineer New Grad"),
            canonical_row(4, deadline="2026-07-19"),
        ],
        today=AS_OF,
    )
    by_index = {int(job["source_url"].rsplit("/", 1)[-1]): job for job in jobs}
    monkeypatch.setattr(
        "watcher.filters.filter_matches",
        lambda *_args, **_kwargs: pytest.fail("candidate construction called filter_matches"),
    )

    candidates = exporter.candidate_pool(jobs)

    assert {job["id"] for job in candidates} == {by_index[1]["id"], by_index[2]["id"]}
    assert by_index[2]["score"]["watcher_eligible"] is False
    assert "filter_matches" not in inspect.getsource(exporter.candidate_pool)


def test_export_writes_blind_safe_rescorable_files_and_correct_hashes(tmp_path, monkeypatch):
    watchlist = tmp_path / "watchlist.yml"
    write_watchlist(watchlist)
    prefix = tmp_path / "private" / "scoring"
    rows = [
        canonical_row(
            1,
            company="=HYPERLINK(\"https://evil.test\")",
            extra={
                "alumni": [{"name": "Private Person"}],
                "linkedin_url": "https://private.example.test",
                "feed_url": "https://feed.example.test/listings.json?token=secret",
            },
        ),
        canonical_row(2, title="Electrical Engineer Intern"),
        canonical_row(3, title="Data Engineering Intern"),
    ]
    calls = []

    def collector(config):
        calls.append(config)
        return rows, ["feed failed: https://feed.example.test/x?token=secret"]

    monkeypatch.setattr("watcher.notify.send_digest", lambda *_a, **_k: pytest.fail("email invoked"))
    monkeypatch.setattr("watcher.alumni.load_default_alumni", lambda: pytest.fail("alumni invoked"))
    monkeypatch.setattr("watcher.seen_store.SeenStore", lambda *_a, **_k: pytest.fail("seen store invoked"))

    manifest = exporter.export_benchmark(
        watchlist_path=watchlist,
        as_of=AS_OF,
        seed=20260720,
        output_prefix=prefix,
        random_count=3,
        top_count=2,
        difficult_count=2,
        collector=collector,
        created_at=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
    )

    paths = exporter.output_paths(prefix)
    headers, label_rows = read_csv(paths["labels"])
    predictions = json.loads(paths["predictions"].read_text(encoding="utf-8"))
    frozen = [json.loads(line) for line in paths["rows"].read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 1
    assert all(not row[field] for row in label_rows for field in exporter.HUMAN_LABEL_COLUMNS)
    assert not {
        "fit_score", "watcher_eligible", "watcher_action", "role_track",
        "fit_explanation", "watcher_ineligible_reason", "total_score", "degree_eligible",
    } & set(headers)
    formula_company = next(row["company"] for row in label_rows if "HYPERLINK" in row["company"])
    assert formula_company.startswith("'=")
    assert set(predictions) == {row["job_id"] for row in label_rows}
    assert {
        "job_id", "sample_groups", "watcher_eligible", "fit_score", "watcher_action",
        "watcher_action_label", "role_track", "degree_level", "degree_eligible",
        "watcher_ineligible_reason", "fit_explanation", "total_score", "bucket", "action",
    } <= set(next(iter(predictions.values())))
    rescored = analyze_rows(frozen, today=AS_OF)
    assert {job["id"] for job in rescored} == set(predictions)
    rendered_files = "\n".join(path.read_text(encoding="utf-8") for path in paths.values())
    assert "Private Person" not in rendered_files
    assert "linkedin_url" not in rendered_files
    assert "token=secret" not in json.dumps(manifest)
    assert manifest["source_errors"][0].endswith("https://feed.example.test/x")
    assert manifest["hashes"]["frozen_rows_sha256"] == hashlib.sha256(paths["rows"].read_bytes()).hexdigest()
    assert manifest["hashes"]["baseline_predictions_sha256"] == hashlib.sha256(paths["predictions"].read_bytes()).hexdigest()
    selected_payload = ("\n".join(row["job_id"] for row in label_rows) + "\n").encode("utf-8")
    assert manifest["hashes"]["selected_job_ids_sha256"] == hashlib.sha256(selected_payload).hexdigest()
    assert manifest["rows_collected"] == 3
    assert manifest["jobs_scored"] == 3
    assert manifest["candidate_pool_size"] == 3
    assert manifest["selected_count"] == len(label_rows)
    assert manifest["requested_group_counts"] == {"random": 3, "top": 2, "difficult": 2}
    assert manifest["actual_group_counts"] == {
        group: sum(group in row["sample_groups"].split("|") for row in label_rows)
        for group in ("random", "top", "difficult")
    }
    assert [job_id(row) for row in frozen] == [row["job_id"] for row in label_rows]


def test_csv_formula_injection_guards_all_spreadsheet_prefixes():
    for value in ("=1+1", "+cmd", "-2+3", "@SUM(A1:A2)", " \t=hidden"):
        assert csv_safe(value).startswith("'")
    assert csv_safe("ordinary text") == "ordinary text"


def test_exporter_source_has_no_watcher_state_or_email_path():
    source = Path(exporter.__file__).read_text(encoding="utf-8")

    assert "run_once" not in source
    assert "SeenStore" not in source
    assert "send_digest" not in source
    assert "mark_many_seen" not in source
    assert "filter_matches" not in source
    evaluator_source = Path(evaluator.__file__).read_text(encoding="utf-8")
    assert "collect_rows" not in evaluator_source
    assert "watcher.sources" not in evaluator_source


def test_exporter_cli_forces_email_off_before_collection(monkeypatch, tmp_path, capsys):
    observed = []

    def fake_export(**_kwargs):
        import os

        observed.append(os.environ.get("WATCHER_SEND_EMAIL"))
        return {
            "rows_collected": 1,
            "candidate_pool_size": 1,
            "selected_count": 1,
            "source_errors": [],
            "output_files": {"labels": str(tmp_path / "labels.csv")},
        }

    monkeypatch.setenv("WATCHER_SEND_EMAIL", "true")
    monkeypatch.setattr(exporter, "export_benchmark", fake_export)

    result = exporter.main(
        [
            "--watchlist", "watcher/watchlist.yml",
            "--as-of", AS_OF.isoformat(),
            "--seed", "1",
            "--output-prefix", str(tmp_path / "benchmark"),
        ]
    )

    assert result == 0
    assert observed == ["0"]
    assert "BENCHMARK-ONLY MODE" in capsys.readouterr().out


def _evaluation_files(tmp_path: Path) -> dict[str, Path]:
    raw_rows = [
        canonical_row(1, title="Backend Engineer Intern"),
        canonical_row(
            2,
            title="Electrical Engineer Intern",
            description="Design circuits and test hardware.",
            requirements="Circuit analysis and lab instrumentation",
        ),
        canonical_row(
            3,
            title="Marketing Intern",
            description="Plan campaigns and write customer newsletters.",
            requirements="Communication and market research",
        ),
        canonical_row(4, title="Backend Engineer Intern"),
        canonical_row(5, title="Software Engineer Intern"),
    ]
    jobs = analyze_rows(raw_rows, today=AS_OF)
    jobs_by_index = {int(job["source_url"].rsplit("/", 1)[-1]): job for job in jobs}
    groups = {
        str(jobs_by_index[index]["id"]): (["random"] if index <= 4 else ["top"])
        for index in range(1, 6)
    }
    labels = []
    human = {
        1: ("yes", "4", "apply_now"),
        2: ("no", "0", "skip"),
        3: ("yes", "3", "apply_later"),
        4: ("no", "0", "skip"),
        5: ("uncertain", "", ""),
    }
    ordered_jobs = [jobs_by_index[index] for index in range(1, 6)]
    for index, job in enumerate(ordered_jobs, start=1):
        row = exporter.labels_row(job, groups[str(job["id"])])
        row["human_eligible"], row["human_priority"], row["human_action"] = human[index]
        row["human_role_track"] = "backend" if index in {1, 4} else ""
        row["error_category"] = "role" if index in {3, 4} else ""
        labels.append(row)

    paths = {
        "labels": tmp_path / "labels.csv",
        "rows": tmp_path / "rows.jsonl",
        "manifest": tmp_path / "manifest.json",
        "baseline": tmp_path / "predictions.json",
        "report": tmp_path / "report.md",
        "metrics": tmp_path / "metrics.json",
    }
    paths["labels"].write_bytes(render_csv_bytes(exporter.LABEL_FIELDS, labels))
    paths["rows"].write_text(
        "".join(json.dumps(exporter.freeze_job(job), sort_keys=True) + "\n" for job in ordered_jobs),
        encoding="utf-8",
    )
    baseline = {
        str(job["id"]): exporter.baseline_prediction(job, groups[str(job["id"])])
        for job in ordered_jobs
    }
    paths["baseline"].write_text(json.dumps(baseline, sort_keys=True), encoding="utf-8")
    paths["manifest"].write_text(
        json.dumps({"schema_version": 1, "as_of_date": AS_OF.isoformat()}),
        encoding="utf-8",
    )
    return paths


def evaluate_paths(paths: dict[str, Path], *, allow_partial: bool = False) -> dict[str, object]:
    return evaluator.evaluate_benchmark(
        labels_path=paths["labels"],
        rows_path=paths["rows"],
        manifest_path=paths["manifest"],
        baseline_predictions_path=paths["baseline"],
        report_path=paths["report"],
        metrics_path=paths["metrics"],
        allow_partial_labels=allow_partial,
    )


def test_evaluator_confusion_metrics_use_only_random_and_exclude_uncertain(tmp_path):
    paths = _evaluation_files(tmp_path)

    metrics = evaluate_paths(paths)
    current = metrics["headline_random_sample"]["current"]

    assert current == {
        "evaluated_count": 4,
        "true_positives": 1,
        "false_positives": 1,
        "false_negatives": 1,
        "true_negatives": 1,
        "precision": 0.5,
        "recall": 0.5,
        "specificity": 0.5,
        "accuracy": 0.5,
        "f1": 0.5,
    }
    assert metrics["coverage"]["uncertain_rows"] == 1
    assert metrics["coverage"]["evaluated_rows"] == 4
    assert metrics["baseline_vs_current"] == {
        "eligibility_decisions_changed": 0,
        "fit_scores_changed": 0,
        "role_tracks_changed": 0,
        "actions_changed": 0,
        "degree_eligibility_changed": 0,
    }


def test_partial_labels_fail_by_default_and_work_when_allowed(tmp_path):
    paths = _evaluation_files(tmp_path)
    headers, rows = read_csv(paths["labels"])
    rows[0]["human_priority"] = ""
    paths["labels"].write_bytes(render_csv_bytes(headers, rows))

    with pytest.raises(BenchmarkError, match="incomplete labels"):
        evaluate_paths(paths)

    metrics = evaluate_paths(paths, allow_partial=True)
    assert metrics["coverage"]["evaluated_rows"] == 3
    assert metrics["coverage"]["incomplete_rows"] == 1
    assert metrics["coverage"]["coverage"] == pytest.approx(3 / 5)


def test_ranking_metrics_use_available_cutoff_and_priority_rubric():
    ids = [f"id-{index}" for index in range(7)]
    predictions = {
        job_id: {
            "job_id": job_id,
            "watcher_eligible": True,
            "fit_score": 100 - index,
            "total_score": 80,
        }
        for index, job_id in enumerate(ids)
    }
    contexts = {job_id: {"company": "C", "title": job_id} for job_id in ids}
    labels = {
        job_id: evaluator.HumanLabel(
            job_id,
            ("random",),
            "yes" if index < 4 else "no",
            4 if index < 4 else 0,
            "apply_now" if index < 4 else "skip",
            "",
            "",
            "",
        )
        for index, job_id in enumerate(ids)
    }

    metrics = evaluator.ranking_metrics(predictions, contexts, labels, ids)

    assert metrics["at_10"]["label"] == "Precision@7"
    assert metrics["at_10"]["precision"] == pytest.approx(4 / 7)
    assert metrics["at_20"]["used_k"] == 7
    assert metrics["at_10"]["average_human_priority"] == pytest.approx(16 / 7)


def test_score_bands_and_zero_denominators_are_safe():
    predictions = {
        "high": {"watcher_eligible": True, "fit_score": 90},
        "zero": {"watcher_eligible": False, "fit_score": 0},
    }
    labels = {
        "high": evaluator.HumanLabel("high", ("random",), "no", 0, "skip", "", "", ""),
    }

    bands = evaluator.score_band_diagnostics(predictions, labels, ["high", "zero"])
    empty_metrics = evaluator.eligibility_metrics(predictions, {}, [])

    assert bands[0]["row_count"] == 1
    assert bands[0]["false_positives"] == 1
    assert bands[-1]["row_count"] == 1
    assert bands[-1]["human_eligible_rate"] is None
    assert empty_metrics["precision"] is None
    assert empty_metrics["recall"] is None
    assert empty_metrics["specificity"] is None
    assert empty_metrics["accuracy"] is None
    assert empty_metrics["f1"] is None


def test_baseline_current_comparison_counts_each_change():
    baseline = {
        "a": {
            "watcher_eligible": True, "fit_score": 80, "role_track": "backend",
            "watcher_action": "apply_now", "action": "apply_now", "degree_eligible": True,
        }
    }
    current = {
        "a": {
            "watcher_eligible": False, "fit_score": 0, "role_track": "unknown",
            "watcher_action": "skip", "action": "skip", "degree_eligible": False,
        }
    }

    assert evaluator.baseline_current_changes(baseline, current, ["a"]) == {
        "eligibility_decisions_changed": 1,
        "fit_scores_changed": 1,
        "role_tracks_changed": 1,
        "actions_changed": 1,
        "degree_eligibility_changed": 1,
    }


def test_duplicate_and_unknown_label_ids_fail_clearly(tmp_path):
    paths = _evaluation_files(tmp_path)
    headers, rows = read_csv(paths["labels"])
    paths["labels"].write_bytes(render_csv_bytes(headers, [*rows, rows[0]]))
    with pytest.raises(BenchmarkError, match="duplicate label job_id"):
        evaluate_paths(paths)

    rows[-1] = dict(rows[-1], job_id="unknown-id")
    paths["labels"].write_bytes(render_csv_bytes(headers, rows))
    with pytest.raises(BenchmarkError, match="not in frozen benchmark"):
        evaluate_paths(paths)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("human_eligible", "maybe", "invalid human_eligible"),
        ("human_priority", "5", "invalid human_priority"),
        ("human_action", "email_now", "invalid human_action"),
    ],
)
def test_invalid_label_values_fail_clearly(tmp_path, field, value, message):
    paths = _evaluation_files(tmp_path)
    headers, rows = read_csv(paths["labels"])
    rows[0][field] = value
    paths["labels"].write_bytes(render_csv_bytes(headers, rows))

    with pytest.raises(BenchmarkError, match=message):
        evaluate_paths(paths)


def test_frozen_rows_must_join_exactly_and_baseline_fields_are_required(tmp_path):
    paths = _evaluation_files(tmp_path)
    lines = paths["rows"].read_text(encoding="utf-8").splitlines()
    paths["rows"].write_text("\n".join([*lines[:-1], lines[0]]) + "\n", encoding="utf-8")

    with pytest.raises(BenchmarkError, match="do not join exactly"):
        evaluate_paths(paths)

    baseline = json.loads(paths["baseline"].read_text(encoding="utf-8"))
    baseline[next(iter(baseline))].pop("fit_score")
    paths["baseline"].write_text(json.dumps(baseline), encoding="utf-8")
    with pytest.raises(BenchmarkError, match="missing fields: fit_score"):
        evaluator.load_predictions(paths["baseline"])


def test_rescoring_receives_frozen_as_of_date(monkeypatch):
    observed = []
    row = canonical_row(1)

    def analyzer(rows, today=None):
        observed.append(today)
        return analyze_rows(rows, today=today)

    monkeypatch.setattr(evaluator, "analyze_rows", analyzer)
    evaluator.rescore_rows([row], AS_OF)

    assert observed == [AS_OF]


def test_report_rendering_is_stable_and_contains_diagnostics(tmp_path):
    paths = _evaluation_files(tmp_path)
    metrics = evaluate_paths(paths)

    first = evaluator.render_report(metrics)
    second = evaluator.render_report(metrics)

    assert first == second
    assert "Headline eligibility metrics (random cohort only)" in first
    assert "Largest disagreements" in first
    assert "Marketing Intern" in first
    assert paths["report"].read_text(encoding="utf-8") == first
    assert json.loads(paths["metrics"].read_text(encoding="utf-8")) == metrics


def test_missing_and_malformed_inputs_return_nonzero(tmp_path, capsys):
    result = evaluator.main(
        [
            "--labels", str(tmp_path / "missing.csv"),
            "--rows", str(tmp_path / "missing.jsonl"),
            "--manifest", str(tmp_path / "missing.json"),
            "--baseline-predictions", str(tmp_path / "missing-predictions.json"),
            "--report", str(tmp_path / "report.md"),
            "--metrics-json", str(tmp_path / "metrics.json"),
        ]
    )
    assert result == 1
    assert "ERROR: file not found" in capsys.readouterr().err

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    with pytest.raises(BenchmarkError, match="invalid UTF-8 JSON"):
        evaluator.load_manifest(malformed)
