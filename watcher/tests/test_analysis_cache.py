import copy
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone

from backend.app import ingest as backend_ingest
from backend.app import config as backend_config
from backend.app.dedupe import norm_company
from backend.app.ingest import analyze_rows, analyze_static_row, process_csv
from backend.app.profile import load_profile
from watcher.analysis_cache import (
    ANALYSIS_CACHE_RETENTION_DAYS,
    STATIC_ANALYSIS_CACHE_VERSION,
    AnalysisCache,
    analyze_rows_with_cache,
    static_analysis_fingerprint,
)
from watcher.eligibility import determine_watcher_eligibility
from watcher.filters import filter_matches
from watcher.seen_store import SeenStore
from watcher.sources.base import make_row


AS_OF = date(2026, 7, 30)


def test_static_scoring_artifact_bumps_cache_version():
    assert STATIC_ANALYSIS_CACHE_VERSION == 8


def _row(
    company,
    title,
    *,
    source_id,
    location="New York, NY",
    compensation="$35/hr",
    description="Build Python REST APIs with mentorship and code review.",
    requirements="Python, SQL, REST APIs, Git",
    deadline="2026-08-15",
    remote_status="",
    extra=None,
):
    metadata = {
        "source_id": source_id,
        "source_requisition_id": source_id,
    }
    metadata.update(extra or {})
    return make_row(
        source="direct",
        source_adapter="greenhouse",
        extra=metadata,
        company=company,
        title=title,
        location=location,
        compensation=compensation,
        description=description,
        requirements=requirements,
        source_url=f"https://example.test/jobs/{source_id}",
        deadline=deadline,
        remote_status=remote_status,
        internship_type="internship",
    )


def _representative_rows():
    direct = _row(
        "Stripe",
        "Backend Engineering Intern",
        source_id="stripe-101",
    )
    github_duplicate = copy.deepcopy(direct)
    github_duplicate["extra"] = {
        "source": "github",
        "source_adapter": "github_listings",
        "source_id": "stripe-101",
    }
    github_duplicate["description"] = ""
    github_duplicate["requirements"] = ""
    second = _row(
        "ExampleCo",
        "Software Engineering Intern",
        source_id="example-202",
        description="Build Java services and own a production feature.",
        requirements="Java, SQL, Docker, Git",
    )
    return [direct, github_duplicate, second]


def _serialized(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _run(
    rows,
    db_path,
    *,
    enabled=True,
    today=AS_OF,
    profile=None,
    known=None,
    cache_version=STATIC_ANALYSIS_CACHE_VERSION,
    cache_factory=AnalysisCache,
):
    return analyze_rows_with_cache(
        copy.deepcopy(rows),
        db_path=db_path,
        enabled=enabled,
        today=today,
        include_audit_diagnostics=True,
        profile=profile,
        known=known,
        cache_version=cache_version,
        cache_factory=cache_factory,
        accessed_at=datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
    )


def test_first_run_misses_and_writes_then_second_run_hits(tmp_path):
    db_path = tmp_path / "analysis-cache.sqlite"
    rows = _representative_rows()

    first = _run(rows, db_path)
    second = _run(rows, db_path)

    assert first.stats.rows == 2
    assert first.stats.hits == 0
    assert first.stats.misses == 2
    assert first.stats.writes == 2
    assert second.stats.hits == 2
    assert second.stats.misses == 0
    assert second.stats.writes == 0
    assert second.stats.hit_rate == 1.0


def test_cache_table_is_created_only_in_dedicated_database(tmp_path):
    seen_db_path = tmp_path / "seen.sqlite"
    cache_db_path = tmp_path / "analysis-cache.sqlite"
    rows = [_row("Stripe", "Backend Intern", source_id="separate-1")]

    with SeenStore(seen_db_path):
        pass
    result = _run(rows, cache_db_path)

    assert result.stats.writes == 1
    with sqlite3.connect(seen_db_path) as connection:
        durable_tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
    with sqlite3.connect(cache_db_path) as connection:
        cache_tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
    assert "seen" in durable_tables
    assert "analysis_cache" not in durable_tables
    assert cache_tables == {"analysis_cache"}


def test_deleted_or_corrupt_cache_does_not_affect_durable_state(
    tmp_path,
    caplog,
):
    seen_db_path = tmp_path / "seen.sqlite"
    cache_db_path = tmp_path / "analysis-cache.sqlite"
    rows = [_row("Stripe", "Backend Intern", source_id="cache-loss-1")]
    job = _run(rows, cache_db_path, enabled=False).jobs[0]
    with SeenStore(seen_db_path) as store:
        store.mark_many_primed(
            [job],
            primed_at=datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
        )
        expected_records = store.records()

    _run(rows, cache_db_path)
    cache_db_path.unlink()
    rebuilt = _run(rows, cache_db_path)
    cache_db_path.write_bytes(b"not a sqlite database")
    with caplog.at_level(logging.WARNING):
        fallback = _run(rows, cache_db_path)

    assert rebuilt.stats.misses == 1
    assert fallback.stats.misses == 1
    assert fallback.stats.writes == 0
    assert "cache" in caplog.text.lower()
    with SeenStore(seen_db_path) as store:
        assert store.records() == expected_records


def test_cache_failure_does_not_modify_or_roll_back_durable_database(
    tmp_path,
):
    seen_db_path = tmp_path / "seen.sqlite"
    cache_db_path = tmp_path / "unavailable"
    cache_db_path.mkdir()
    rows = [_row("Stripe", "Backend Intern", source_id="cache-failure-1")]
    with SeenStore(seen_db_path):
        pass
    before = seen_db_path.read_bytes()

    result = _run(rows, cache_db_path)

    assert result.stats.misses == 1
    assert result.stats.writes == 0
    assert seen_db_path.read_bytes() == before
    with SeenStore(seen_db_path) as store:
        assert store.records() == []


def test_cached_uncached_jobs_and_dedupe_reports_are_serialized_identically(
    tmp_path,
):
    rows = _representative_rows()
    expected_jobs, expected_duplicates = analyze_rows(
        copy.deepcopy(rows),
        today=AS_OF,
        include_dedupe_report=True,
        include_audit_diagnostics=True,
    )

    cold = _run(rows, tmp_path / "seen.sqlite")
    warm = _run(rows, tmp_path / "seen.sqlite")
    disabled = _run(
        rows,
        tmp_path / "disabled.sqlite",
        enabled=False,
    )

    assert _serialized(cold.jobs) == _serialized(expected_jobs)
    assert _serialized(warm.jobs) == _serialized(expected_jobs)
    assert _serialized(disabled.jobs) == _serialized(expected_jobs)
    assert cold.duplicate_report == expected_duplicates
    assert warm.duplicate_report == expected_duplicates
    assert disabled.duplicate_report == expected_duplicates


def test_warm_cache_recomputes_date_sensitive_scoring(tmp_path):
    rows = [
        _row(
            "Stripe",
            "Backend Engineering Intern",
            source_id="stripe-deadline",
            deadline="2026-08-01",
        )
    ]
    db_path = tmp_path / "seen.sqlite"

    before = _run(rows, db_path, today=date(2026, 7, 30))
    after = _run(rows, db_path, today=date(2026, 8, 2))

    assert after.stats.hits == 1
    assert before.jobs[0]["deadline_days_left"] == 2
    assert after.jobs[0]["deadline_days_left"] == -1
    assert before.jobs[0]["score"]["action"] != after.jobs[0]["score"]["action"]
    assert (
        before.jobs[0]["score"]["categories"]["deadline_urgency"]
        != after.jobs[0]["score"]["categories"]["deadline_urgency"]
    )
    assert (
        before.jobs[0]["score"]["categories"]["role_relevance"]
        == after.jobs[0]["score"]["categories"]["role_relevance"]
    )
    assert any(
        "deadline" in concern.casefold()
        for concern in after.jobs[0]["score"]["concerns"]
    )


def test_warm_cache_reuses_static_student_eligibility(
    tmp_path,
    monkeypatch,
):
    rows = [
        _row(
            "ExampleCo",
            "Software Engineering Intern",
            source_id="eligibility-reuse",
            requirements=(
                "Minimum qualifications: currently pursuing a bachelor's "
                "degree. Preferred: master's degree."
            ),
        )
    ]
    calls = 0
    original = backend_ingest.analyze_student_eligibility

    def counted(row):
        nonlocal calls
        calls += 1
        return original(row)

    monkeypatch.setattr(
        backend_ingest,
        "analyze_student_eligibility",
        counted,
    )
    db_path = tmp_path / "seen.sqlite"

    cold = _run(rows, db_path)
    warm = _run(rows, db_path)

    assert cold.stats.misses == 1
    assert warm.stats.hits == 1
    assert calls == 1
    assert (
        cold.jobs[0]["student_eligibility"]
        == warm.jobs[0]["student_eligibility"]
    )


def test_static_eligibility_matrix_is_identical_cached_and_uncached(
    tmp_path,
):
    rows = [
        _row(
            "UndergradCo",
            "Software Engineering Intern",
            source_id="matrix-undergrad",
            location="United States",
            requirements="Currently pursuing a bachelor's degree.",
        ),
        _row(
            "GraduateCo",
            "Software Engineering Intern",
            source_id="matrix-graduate",
            location="Remote",
            remote_status="remote",
            requirements="Graduate students only. Python required.",
        ),
        _row(
            "PhdCo",
            "Machine Learning PhD Intern",
            source_id="matrix-phd",
            location="Ithaca, NY",
        ),
        _row(
            "ReturnCo",
            "Returning Intern Software Engineer",
            source_id="matrix-returning",
            location="London, United Kingdom",
        ),
        _row(
            "PolicyCo",
            "Software Engineering Intern",
            source_id="matrix-policy",
            location="Remote - US",
            requirements=(
                "Must graduate in 2028. United States citizenship required. "
                "Employment sponsorship is not available."
            ),
        ),
        _row(
            "MixedCo",
            "Software Engineering Intern",
            source_id="matrix-mixed",
            location="",
            requirements=(
                "Undergraduate or graduate students may apply; an advanced "
                "degree is not required."
            ),
        ),
        _row(
            "MissingCo",
            "Software Engineering Intern",
            source_id="matrix-missing",
            location="",
            description="Build Python APIs.",
            requirements="",
            deadline="2026-07-01",
        ),
    ]
    disabled = _run(
        rows,
        tmp_path / "disabled.sqlite",
        enabled=False,
    )
    cold = _run(rows, tmp_path / "cache.sqlite")
    warm = _run(rows, tmp_path / "cache.sqlite")

    assert warm.stats.hits == len(rows)
    assert _serialized(cold.jobs) == _serialized(disabled.jobs)
    assert _serialized(warm.jobs) == _serialized(disabled.jobs)
    for cached, expected in zip(warm.jobs, disabled.jobs):
        assert cached["student_eligibility"] == expected["student_eligibility"]
        assert cached["score"]["categories"] == expected["score"]["categories"]
        assert cached["score"]["reasons"] == expected["score"]["reasons"]
        assert cached["score"]["concerns"] == expected["score"]["concerns"]
        assert determine_watcher_eligibility(cached) == (
            determine_watcher_eligibility(expected)
        )


def test_analysis_relevant_change_misses_but_volatile_metadata_change_hits(
    tmp_path,
):
    db_path = tmp_path / "seen.sqlite"
    original = _row(
        "ExampleCo",
        "Software Engineering Intern",
        source_id="metadata-1",
        extra={
            "fetch_observed_at": "2026-07-30T10:00:00Z",
            "request_count": 1,
            "retry_count": 0,
        },
    )
    _run([original], db_path)

    volatile_changed = copy.deepcopy(original)
    volatile_changed["extra"].update(
        {
            "fetch_observed_at": "2026-07-30T11:00:00Z",
            "request_count": 4,
            "retry_count": 2,
            "health_status": "healthy",
        }
    )
    hit = _run([volatile_changed], db_path)

    relevant_changed = copy.deepcopy(original)
    relevant_changed["requirements"] += ", Kubernetes"
    miss = _run([relevant_changed], db_path)

    assert hit.stats.hits == 1
    assert hit.stats.misses == 0
    assert miss.stats.hits == 0
    assert miss.stats.misses == 1


def test_static_location_and_structured_eligibility_changes_miss(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    original = _row(
        "ExampleCo",
        "Software Engineering Intern",
        source_id="static-inputs",
        location="New York, NY",
        extra={
            "eligibility": {
                "academic_level": "undergraduate students",
            }
        },
    )
    _run([original], db_path)

    location_changed = copy.deepcopy(original)
    location_changed["location"] = "Remote"
    location_changed["remote_status"] = "remote"
    location_result = _run([location_changed], db_path)

    eligibility_changed = copy.deepcopy(original)
    eligibility_changed["extra"]["eligibility"][
        "academic_level"
    ] = "doctoral program only"
    eligibility_result = _run([eligibility_changed], db_path)

    assert location_result.stats.misses == 1
    assert eligibility_result.stats.misses == 1


def test_dynamic_active_state_and_target_roles_do_not_invalidate_artifact(
    tmp_path,
):
    db_path = tmp_path / "seen.sqlite"
    original = _row(
        "ExampleCo",
        "Software Engineering Intern",
        source_id="dynamic-policy",
        extra={"active": True},
    )
    first = _run([original], db_path)

    closed = copy.deepcopy(original)
    closed["extra"]["active"] = False
    second = _run([closed], db_path)

    assert second.stats.hits == 1
    assert second.jobs[0]["extra"]["active"] is False
    assert filter_matches(
        first.jobs,
        target_roles=frozenset({"swe"}),
    ) == first.jobs
    assert filter_matches(
        second.jobs,
        target_roles=frozenset({"swe"}),
    ) == []
    assert filter_matches(
        first.jobs,
        target_roles=frozenset(),
    ) == []


def test_profile_known_company_and_cache_version_changes_each_miss(tmp_path):
    rows = [
        _row(
            "Mystery Employer",
            "Software Engineering Intern",
            source_id="config-1",
        )
    ]
    profile = load_profile()
    known = backend_config.load_known_companies()
    db_path = tmp_path / "seen.sqlite"
    _run(rows, db_path, profile=profile, known=known)

    changed_profile = copy.deepcopy(profile)
    changed_profile["skills"] = [*profile["skills"], "kubernetes operators"]
    profile_result = _run(
        rows,
        db_path,
        profile=changed_profile,
        known=known,
    )

    changed_known = copy.deepcopy(known)
    changed_known["tech"] = set(changed_known["tech"])
    changed_known["tech"].add(norm_company("Mystery Employer"))
    known_result = _run(
        rows,
        db_path,
        profile=profile,
        known=changed_known,
    )

    version_result = _run(
        rows,
        db_path,
        profile=profile,
        known=known,
        cache_version=STATIC_ANALYSIS_CACHE_VERSION + 1,
    )

    assert profile_result.stats.misses == 1
    assert known_result.stats.misses == 1
    assert version_result.stats.misses == 1


def test_mixed_hit_miss_run_preserves_final_ordering(tmp_path):
    cached_row = _row(
        "Stripe",
        "Backend Engineering Intern",
        source_id="mixed-cached",
    )
    new_row = _row(
        "ExampleCo",
        "Software Engineering Intern",
        source_id="mixed-new",
        compensation="$20/hr",
    )
    db_path = tmp_path / "seen.sqlite"
    _run([cached_row], db_path)

    mixed = _run([new_row, cached_row], db_path)
    expected = analyze_rows(
        copy.deepcopy([new_row, cached_row]),
        today=AS_OF,
    )

    assert mixed.stats.hits == 1
    assert mixed.stats.misses == 1
    assert [job["id"] for job in mixed.jobs] == [
        job["id"] for job in expected
    ]
    assert _serialized(mixed.jobs) == _serialized(expected)


def test_cached_analysis_preserves_distinct_ids_for_canonical_collision(tmp_path):
    first = _row(
        "Capital One",
        "Backend Software Engineer Intern",
        source_id="collision-1",
    )
    second = _row(
        "Capital One",
        "Backend Software Engineer Intern",
        source_id="collision-2",
    )
    rows = [first, second]
    db_path = tmp_path / "collision-cache.sqlite"

    cold = _run(rows, db_path)
    warm = _run(list(reversed(rows)), db_path)
    expected = analyze_rows(copy.deepcopy(rows), today=AS_OF)

    assert len({job["id"] for job in cold.jobs}) == 2
    assert _serialized(cold.jobs) == _serialized(warm.jobs)
    assert _serialized(cold.jobs) == _serialized(expected)


def test_role_recall_classification_is_identical_cold_warm_and_uncached(tmp_path):
    rows = [
        _row(
            "ExampleCo",
            "[SX/EIT] Automation Tester Intern (Selenium)",
            source_id="recall-cache-1",
            description="Build automated Selenium test suites.",
        ),
        _row(
            "ExampleCo",
            "Intern, AI Research",
            source_id="recall-cache-2",
            description="Research machine-learning models.",
        ),
    ]
    db_path = tmp_path / "role-recall-cache.sqlite"

    cold = _run(rows, db_path)
    warm = _run(rows, db_path)
    uncached = _run(rows, db_path, enabled=False)

    assert sorted(job["role_classification"]["role_track"] for job in cold.jobs) == [
        "ml_ai",
        "sdet_qa_automation",
    ]
    assert [job["id"] for job in cold.jobs] == [job["id"] for job in warm.jobs]
    assert [job["id"] for job in cold.jobs] == [job["id"] for job in uncached.jobs]
    assert _serialized(cold.jobs) == _serialized(warm.jobs)
    assert _serialized(cold.jobs) == _serialized(uncached.jobs)


def test_corrupt_json_and_schema_entries_fall_back_to_fresh_analysis(
    tmp_path,
    caplog,
):
    rows = [_row("Stripe", "Backend Intern", source_id="corrupt-1")]
    db_path = tmp_path / "seen.sqlite"
    expected = analyze_rows(copy.deepcopy(rows), today=AS_OF)
    _run(rows, db_path)

    for corrupt_value in ("{not-json", '{"schema_version":999}'):
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "update analysis_cache set artifact_json = ?",
                (corrupt_value,),
            )
        caplog.clear()
        with caplog.at_level(logging.WARNING):
            result = _run(rows, db_path)

        assert result.stats.invalid == 1
        assert result.stats.misses == 1
        assert result.stats.writes == 1
        assert _serialized(result.jobs) == _serialized(expected)
        assert "invalid" in caplog.text.lower()


def test_sqlite_failure_warns_and_falls_back_to_fresh_analysis(
    tmp_path,
    caplog,
):
    class BrokenCache:
        def __init__(self, *args, **kwargs):
            raise sqlite3.OperationalError("database unavailable")

    rows = [_row("Stripe", "Backend Intern", source_id="sqlite-1")]
    expected = analyze_rows(copy.deepcopy(rows), today=AS_OF)

    with caplog.at_level(logging.WARNING):
        result = _run(
            rows,
            tmp_path / "seen.sqlite",
            cache_factory=BrokenCache,
        )

    assert result.stats.hits == 0
    assert result.stats.misses == 1
    assert result.stats.writes == 0
    assert _serialized(result.jobs) == _serialized(expected)
    assert "cache" in caplog.text.lower()


def test_disabling_cache_does_not_create_cache_table(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    rows = [_row("Stripe", "Backend Intern", source_id="disabled-1")]

    disabled = _run(rows, db_path, enabled=False)
    expected = analyze_rows(copy.deepcopy(rows), today=AS_OF)

    assert disabled.stats.enabled is False
    assert disabled.stats.hits == 0
    assert disabled.stats.misses == 1
    assert disabled.stats.writes == 0
    assert _serialized(disabled.jobs) == _serialized(expected)
    assert not db_path.exists()


def test_backend_csv_path_is_cache_independent(tmp_path, monkeypatch):
    import watcher.analysis_cache as cache_module

    def fail_if_constructed(*args, **kwargs):
        raise AssertionError("backend path must not construct watcher cache")

    monkeypatch.setattr(cache_module, "AnalysisCache", fail_if_constructed)
    csv_text = (
        "Company,Title,Description,Requirements\n"
        "Stripe,Backend Intern,Build APIs,Python and SQL\n"
    )

    result = process_csv(csv_text, today=AS_OF)

    assert len(result["jobs"]) == 1
    assert not (tmp_path / "seen.sqlite").exists()


def test_retention_cleanup_removes_expired_but_not_active_entries(tmp_path):
    db_path = tmp_path / "seen.sqlite"
    profile = load_profile()
    known = backend_config.load_known_companies()
    artifact = analyze_static_row(
        _row("Stripe", "Backend Intern", source_id="retention"),
        profile=profile,
        known=known,
    )
    old_time = datetime(2026, 6, 1, tzinfo=timezone.utc)
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)

    with SeenStore(db_path):
        pass
    with AnalysisCache(db_path) as cache:
        assert cache.store_many(
            {"expired": artifact, "active": artifact},
            accessed_at=old_time,
        ) == 2
        lookup = cache.lookup_many(["active"], accessed_at=now)
        assert set(lookup.artifacts) == {"active"}
        removed = cache.cleanup_expired(
            accessed_at=now,
            retention_days=ANALYSIS_CACHE_RETENTION_DAYS,
        )

    cutoff = now - timedelta(days=ANALYSIS_CACHE_RETENTION_DAYS)
    assert old_time < cutoff
    assert removed == 1
    with sqlite3.connect(db_path) as connection:
        remaining = connection.execute(
            "select fingerprint from analysis_cache"
        ).fetchall()
    assert remaining == [("active",)]


def test_fingerprint_uses_sha256_and_excludes_volatile_metadata():
    profile = load_profile()
    known = backend_config.load_known_companies()
    row = _row(
        "Stripe",
        "Backend Intern",
        source_id="fingerprint-1",
        extra={"fetch_observed_at": "first", "request_count": 1},
    )
    changed = copy.deepcopy(row)
    changed["extra"].update(
        {"fetch_observed_at": "second", "request_count": 99}
    )

    first = static_analysis_fingerprint(row, profile=profile, known=known)
    second = static_analysis_fingerprint(
        changed,
        profile=profile,
        known=known,
    )

    assert len(first) == 64
    assert first == second


def test_cache_emits_one_safe_machine_readable_summary_line(
    tmp_path,
    caplog,
):
    sensitive_description = "private candidate note must not be logged"
    rows = [
        _row(
            "Stripe",
            "Backend Intern",
            source_id="summary-1",
            description=sensitive_description,
        )
    ]

    with caplog.at_level(logging.INFO):
        _run(rows, tmp_path / "seen.sqlite")

    summaries = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("ANALYSIS-CACHE ")
    ]
    assert len(summaries) == 1
    assert (
        "enabled=true rows=1 hits=0 misses=1 invalid=0 writes=1 "
        "hit_rate=0.000 lookup_seconds="
    ) in summaries[0]
    assert " static_analysis_seconds=" in summaries[0]
    assert " scoring_seconds=" in summaries[0]
    assert sensitive_description not in summaries[0]
    assert "https://" not in summaries[0]
