import gzip
import json
import sqlite3
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app import config as backend_config
from watcher.collection_snapshot import (
    COLLECTION_SNAPSHOT_SCHEMA_VERSION,
    CollectionBatch,
    CollectionSnapshotError,
    collection_config_fingerprint,
    load_collection_snapshot,
    save_collection_snapshot,
)
from watcher.config import CompanyCfg, GitHubListingSourceCfg, WatcherConfig
from watcher.run import (
    RUN_MODE_DRY,
    collect_batch,
    main as watcher_main,
    run_once,
)
from watcher.seen_store import SeenStore
from watcher.source_health import (
    ERROR_FETCH,
    SOURCE_KIND_DIRECT,
    SOURCE_KIND_GITHUB_FEED,
    SourceAttempt,
)
from watcher.sources.base import SourceFetchError, make_row


CAPTURED_AT = datetime(2026, 7, 18, 13, 14, 15, tzinfo=timezone.utc)


def _config(*, cache_enabled=False):
    return WatcherConfig(
        companies=(
            CompanyCfg(
                name="DirectCo",
                ats="greenhouse",
                token="directco",
                aliases=("Direct Company",),
                terms=("Summer 2027",),
            ),
            CompanyCfg(
                name="GitHubCo",
                ats="github_only",
                aliases=("Git Hub Company",),
                terms=("Summer 2027",),
            ),
        ),
        terms=("Summer 2027",),
        github_listing_sources=(
            GitHubListingSourceCfg(
                name="simplify",
                format="simplify_json",
                url="https://example.test/listings.json",
            ),
        ),
        analysis_cache_enabled=cache_enabled,
    )


def _row(
    company,
    title,
    *,
    source="direct",
    adapter="greenhouse",
    url=None,
    deadline="",
    description="Build Python backend APIs with SQL.",
):
    return make_row(
        source=source,
        source_adapter=adapter,
        company=company,
        title=title,
        location="New York, NY",
        description=description,
        requirements="Python, SQL, Git, REST APIs",
        source_url=url or f"https://example.test/jobs/{company}/{title}",
        deadline=deadline,
        internship_type="Summer 2027",
    )


def _attempt(
    *,
    health_key,
    source_kind,
    adapter,
    company=None,
    succeeded=True,
    rows=1,
    feed_label=None,
):
    direct_diagnostics = (
        {
            "malformed_row_count": 0,
            "schema_error_row_count": 0,
            "duplicate_row_count": 2,
            "failed_request_count": 0,
            "incomplete": False,
            "truncated": False,
            "reason_codes": ("duplicates_removed",),
            "degraded": False,
            "complete": True,
        }
        if source_kind == SOURCE_KIND_DIRECT and succeeded
        else {}
    )
    return SourceAttempt(
        health_key=health_key,
        run_id="20260718T131415Z-fixed",
        observed_at=CAPTURED_AT,
        source_kind=source_kind,
        company=company,
        adapter=adapter,
        attempted=True,
        succeeded=succeeded,
        rows_returned=rows if succeeded else None,
        error_kind=None if succeeded else ERROR_FETCH,
        error_message=None if succeeded else "safe failure",
        feed_label=feed_label,
        **direct_diagnostics,
    )


def _batch(config=None, *, rows=None):
    config = config or _config()
    return CollectionBatch.create(
        captured_at=CAPTURED_AT,
        collection_config_fingerprint=collection_config_fingerprint(config),
        rows=rows
        or (
            _row(
                "DirectCo",
                "Software Engineer Intern",
                url="https://example.test/jobs/shared",
            ),
            _row(
                "GitHubCo",
                "Backend Engineer Intern",
                source="github",
                adapter="github_listings",
            ),
        ),
        errors=("first safe collection warning", "second safe collection warning"),
        source_attempts=(
            _attempt(
                health_key="company:directco:direct:greenhouse",
                source_kind=SOURCE_KIND_DIRECT,
                adapter="greenhouse",
                company="DirectCo",
            ),
            SourceAttempt(
                health_key="company:githubco:direct:github_only",
                run_id="20260718T131415Z-fixed",
                observed_at=CAPTURED_AT,
                source_kind=SOURCE_KIND_DIRECT,
                company="GitHubCo",
                adapter="github_only",
                attempted=False,
                succeeded=None,
                rows_returned=None,
                unsupported_reason="github_only",
            ),
            _attempt(
                health_key="github_feed:abc123",
                source_kind=SOURCE_KIND_GITHUB_FEED,
                adapter="github_listings",
                feed_label="https://example.test/listings.json",
            ),
        ),
        github_feeds_configured=1,
        github_feeds_succeeded=1,
        workday_attempted=2,
        workday_succeeded=1,
        workday_failed=1,
        workday_request_attempts=7,
        workday_retry_attempts=2,
        workday_failure_codes={"timeout": 1},
    )


class _DirectSource:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def fetch(self, _company):
        self.calls += 1
        return list(self.rows)


class _WorkdaySource(_DirectSource):
    def __init__(self, rows=(), *, error=None, requests=0, retries=0):
        super().__init__(rows)
        self.error = error
        self.last_diagnostics = SimpleNamespace(
            request_attempts=requests,
            retry_attempts=retries,
        )

    def fetch(self, company):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.rows)


class _GithubSource:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
        self.url = "https://example.test/listings.json"
        self.source_name = "simplify"
        self.name = "github_listings"

    def fetch_many(self, _companies):
        self.calls += 1
        return list(self.rows)


def test_collection_batch_snapshot_round_trip_preserves_every_field_and_order(
    tmp_path,
):
    batch = _batch()
    path = tmp_path / "capture.json.gz"

    save_collection_snapshot(batch, path)
    loaded = load_collection_snapshot(path)

    assert loaded == batch
    assert loaded.as_dict() == batch.as_dict()
    assert [row["company"] for row in loaded.rows] == ["DirectCo", "GitHubCo"]
    assert [attempt.source_kind for attempt in loaded.source_attempts] == [
        SOURCE_KIND_DIRECT,
        SOURCE_KIND_DIRECT,
        SOURCE_KIND_GITHUB_FEED,
    ]
    assert list(loaded.errors) == [
        "first safe collection warning",
        "second safe collection warning",
    ]
    assert loaded.workday_request_attempts == 7
    assert loaded.workday_failure_codes == (("timeout", 1),)
    assert loaded.source_attempts[0].duplicate_row_count == 2
    assert loaded.source_attempts[0].reason_codes == ("duplicates_removed",)
    assert loaded.source_attempts[0].complete is True
    with pytest.raises(FrozenInstanceError):
        loaded.captured_at = datetime.now(timezone.utc)
    with pytest.raises(TypeError):
        loaded.rows[0]["title"] = "mutated"


def test_schema_version_two_snapshot_loads_with_unknown_direct_diagnostics(tmp_path):
    payload = _batch().as_dict()
    payload["schema_version"] = 2
    diagnostic_fields = {
        "malformed_row_count",
        "schema_error_row_count",
        "duplicate_row_count",
        "failed_request_count",
        "incomplete",
        "truncated",
        "reason_codes",
        "degraded",
        "complete",
    }
    for item in payload["source_attempts"]:
        for field in diagnostic_fields:
            item.pop(field)
    path = tmp_path / "legacy-v2.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)

    loaded = load_collection_snapshot(path)

    assert loaded.schema_version == COLLECTION_SNAPSHOT_SCHEMA_VERSION
    assert loaded.source_attempts[0].complete is None
    assert loaded.source_attempts[0].reason_codes == ()


def test_snapshot_serialization_is_byte_deterministic(tmp_path):
    batch = _batch()
    first = tmp_path / "first.json.gz"
    second = tmp_path / "second.json.gz"

    save_collection_snapshot(batch, first)
    save_collection_snapshot(batch, second)

    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize(
    ("source", "succeeded", "failed", "requests", "retries", "failure_codes"),
    [
        (
            _WorkdaySource(
                [_row("WorkdayCo", "SWE Intern")],
                requests=4,
                retries=1,
            ),
            1,
            0,
            4,
            1,
            (),
        ),
        (
            _WorkdaySource(
                error=SourceFetchError(
                    "temporary failure",
                    error_code="timeout",
                    attempt_count=3,
                ),
                requests=3,
                retries=2,
            ),
            0,
            1,
            3,
            2,
            (("timeout", 1),),
        ),
    ],
)
def test_live_batch_captures_workday_request_and_outcome_diagnostics(
    source,
    succeeded,
    failed,
    requests,
    retries,
    failure_codes,
):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="WorkdayCo",
                ats="workday",
                token="workdayco",
                workday_shard="wd5",
                workday_site="WorkdayCo",
            ),
        ),
    )

    batch = collect_batch(
        config,
        direct_sources={"workday": source},
        github_source=[],
        run_id="20260718T131415Z-fixed",
        observed_at=CAPTURED_AT,
        captured_at=CAPTURED_AT,
    )

    assert batch.workday_attempted == 1
    assert batch.workday_succeeded == succeeded
    assert batch.workday_failed == failed
    assert batch.workday_request_attempts == requests
    assert batch.workday_retry_attempts == retries
    assert batch.workday_failure_codes == failure_codes


def test_original_and_loaded_batch_produce_identical_pipeline_outputs(tmp_path):
    config = _config(cache_enabled=False)
    shared = "https://example.test/jobs/shared"
    direct_source = _DirectSource(
        [_row("DirectCo", "Software Engineer Intern", url=shared)]
    )
    github_source = _GithubSource(
        [
            _row(
                "DirectCo",
                "SWE Intern",
                source="github",
                adapter="github_listings",
                url=shared,
                description="",
            ),
            _row(
                "GitHubCo",
                "Backend Engineer Intern",
                source="github",
                adapter="github_listings",
            ),
        ]
    )
    original = collect_batch(
        config,
        direct_sources={"greenhouse": direct_source},
        github_source=github_source,
        run_id="20260718T131415Z-fixed",
        observed_at=CAPTURED_AT,
        captured_at=CAPTURED_AT,
    )
    path = tmp_path / "capture.json.gz"
    save_collection_snapshot(original, path)
    replayed = load_collection_snapshot(path)
    original_payload = original.as_dict()
    replayed_payload = replayed.as_dict()

    with SeenStore(tmp_path / "original.sqlite") as original_store:
        original_result = run_once(
            config,
            seen_store=original_store,
            alumni_index={},
            collection_batch=original,
            notification_mode=RUN_MODE_DRY,
        )
    with SeenStore(tmp_path / "replayed.sqlite") as replay_store:
        replayed_result = run_once(
            config,
            seen_store=replay_store,
            alumni_index={},
            collection_batch=replayed,
            notification_mode=RUN_MODE_DRY,
        )

    assert original_result.jobs == replayed_result.jobs
    assert original_result.duplicate_report == replayed_result.duplicate_report
    assert original_result.matches == replayed_result.matches
    assert (
        original_result.source_comparison.as_dict()
        == replayed_result.source_comparison.as_dict()
    )
    assert original.as_dict() == original_payload
    assert replayed.as_dict() == replayed_payload
    assert original_result.source_comparison_persisted is False
    assert replayed_result.source_comparison_persisted is False


def test_replay_skips_network_and_all_operational_side_effects(
    tmp_path,
    monkeypatch,
):
    config = _config(cache_enabled=False)
    batch = _batch(config)
    db_path = tmp_path / "seen.sqlite"
    with SeenStore(db_path):
        pass

    calls = []

    def fail_if_called(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("replay invoked a forbidden side effect")

    monkeypatch.setattr("watcher.pipeline.collect_batch", fail_if_called)

    class ExplodingHealthStore:
        def record_attempts(self, _attempts):
            return fail_if_called()

    with SeenStore(db_path) as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": object()},
            github_source=object(),
            alumni_index={},
            digest_sender=fail_if_called,
            health_store=ExplodingHealthStore(),
            health_alert_sender=fail_if_called,
            collection_batch=batch,
        )

    assert calls == []
    assert result.collection_replayed is True
    assert result.notification_mode == RUN_MODE_DRY
    assert result.digest_sent is False
    assert result.seen_marked == 0
    assert result.source_comparison_persisted is False
    assert result.health_alert_result.mode == "off"
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        assert connection.execute("select count(*) from seen").fetchone()[0] == 0
    assert "source_health_attempts" not in tables
    assert "source_comparison_runs" not in tables
    assert "source_health_alert_events" not in tables


def test_capture_mode_runs_live_collection_saves_and_continues(tmp_path, caplog):
    caplog.set_level("INFO", logger="watcher.run")
    config = _config(cache_enabled=False)
    direct_source = _DirectSource(
        [_row("DirectCo", "Software Engineer Intern")]
    )
    github_source = _GithubSource([])
    snapshot_path = tmp_path / "live.json.gz"
    db_path = tmp_path / "seen.sqlite"

    with SeenStore(db_path) as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": direct_source},
            github_source=github_source,
            alumni_index={},
            notification_mode=RUN_MODE_DRY,
            capture_collection_snapshot_path=snapshot_path,
            health_observed_at=CAPTURED_AT,
        )

    loaded = load_collection_snapshot(snapshot_path)
    assert direct_source.calls == 1
    assert github_source.calls == 1
    assert len(loaded.rows) == result.rows_fetched == 1
    assert result.collection_replayed is False
    assert result.source_comparison_persisted is True
    capture_logs = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("COLLECTION-SNAPSHOT ")
    ]
    assert len(capture_logs) == 1
    assert "mode=capture" in capture_logs[0]
    assert " rows=1 " in capture_logs[0]
    assert "https://" not in capture_logs[0]
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute(
                "select count(*) from source_health_attempts"
            ).fetchone()[0]
            == len(loaded.source_attempts)
        )


def test_collection_config_mismatch_fails_unless_explicitly_allowed(
    tmp_path,
    caplog,
):
    caplog.set_level("INFO", logger="watcher.run")
    original_config = _config(cache_enabled=False)
    changed_config = replace(
        original_config,
        companies=(
            replace(original_config.companies[0], token="different-token"),
            original_config.companies[1],
        ),
    )
    batch = _batch(original_config)
    with SeenStore(tmp_path / "seen.sqlite") as store:
        with pytest.raises(
            CollectionSnapshotError,
            match="configuration does not match",
        ):
            run_once(
                changed_config,
                seen_store=store,
                alumni_index={},
                collection_batch=batch,
            )
        allowed = run_once(
            changed_config,
            seen_store=store,
            alumni_index={},
            collection_batch=batch,
            allow_collection_config_mismatch=True,
        )

    assert allowed.collection_replayed is True
    replay_logs = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("COLLECTION-SNAPSHOT ")
    ]
    assert len(replay_logs) == 1
    assert "mode=replay" in replay_logs[0]
    assert "config_match=false" in replay_logs[0]
    assert "https://" not in replay_logs[0]


def test_scoring_filter_cache_and_profile_changes_do_not_block_loading(
    tmp_path,
    monkeypatch,
):
    config = _config(cache_enabled=False)
    batch = _batch(config)
    path = tmp_path / "capture.json.gz"
    save_collection_snapshot(batch, path)
    changed_runtime_config = replace(
        config,
        target_roles=frozenset({"data_science"}),
        min_score=99,
        analysis_cache_enabled=True,
    )
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(
        json.dumps({"skills": ["Rust"], "goal": "Changed after capture"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(backend_config, "PROFILE_PATH", profile_path)

    loaded = load_collection_snapshot(path)

    assert (
        collection_config_fingerprint(changed_runtime_config)
        == batch.collection_config_fingerprint
    )
    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            changed_runtime_config,
            seen_store=store,
            alumni_index={},
            collection_batch=loaded,
        )
    assert result.collection_replayed is True


def test_snapshot_replay_uses_dedicated_analysis_cache(tmp_path):
    seen_db_path = tmp_path / "seen.sqlite"
    cache_db_path = tmp_path / "analysis-cache.sqlite"
    config = replace(
        _config(cache_enabled=True),
        seen_db_path=seen_db_path,
        analysis_cache_path=cache_db_path,
    )
    batch = _batch(config)
    with SeenStore(seen_db_path):
        pass
    durable_before = seen_db_path.read_bytes()

    with SeenStore(seen_db_path, read_only=True) as store:
        cold = run_once(
            config,
            seen_store=store,
            alumni_index={},
            collection_batch=batch,
        )
    with SeenStore(seen_db_path, read_only=True) as store:
        warm = run_once(
            config,
            seen_store=store,
            alumni_index={},
            collection_batch=batch,
        )

    assert cold.analysis_cache_stats.misses == cold.jobs_scored
    assert warm.analysis_cache_stats.hits == warm.jobs_scored
    assert warm.analysis_cache_stats.misses == 0
    assert cold.jobs == warm.jobs
    assert cold.duplicate_report == warm.duplicate_report
    assert seen_db_path.read_bytes() == durable_before
    with sqlite3.connect(seen_db_path) as connection:
        assert connection.execute(
            """
            select count(*)
            from sqlite_master
            where type = 'table' and name = 'analysis_cache'
            """
        ).fetchone()[0] == 0
    with sqlite3.connect(cache_db_path) as connection:
        assert connection.execute(
            "select count(*) from analysis_cache"
        ).fetchone()[0] == warm.jobs_scored


def test_collection_relevant_alias_and_source_changes_change_fingerprint():
    config = _config()
    alias_changed = replace(
        config,
        companies=(
            replace(config.companies[0], aliases=("Another Alias",)),
            config.companies[1],
        ),
    )
    source_changed = replace(
        config,
        github_listing_sources=(
            replace(
                config.github_listing_sources[0],
                url="https://example.test/other-listings.json",
            ),
        ),
    )

    assert collection_config_fingerprint(alias_changed) != collection_config_fingerprint(config)
    assert collection_config_fingerprint(source_changed) != collection_config_fingerprint(config)


def test_workday_detail_policy_changes_collection_fingerprint():
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Workday Example",
                ats="workday",
                token="tenant",
                workday_shard="wd5",
                workday_site="Site",
            ),
        )
    )
    disabled = replace(
        config,
        companies=(replace(config.companies[0], workday_detail_policy="none"),),
    )

    assert collection_config_fingerprint(disabled) != collection_config_fingerprint(config)


def test_oracle_hcm_host_and_site_change_collection_fingerprint():
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Oracle Example",
                ats="oracle_hcm",
                oracle_hcm_host="one.fa.oraclecloud.com",
                oracle_hcm_site="CX_ONE",
                source_url=(
                    "https://one.fa.oraclecloud.com/hcmUI/CandidateExperience/en/"
                    "sites/CX_ONE/jobs"
                ),
            ),
        )
    )

    host_changed = replace(
        config,
        companies=(
            replace(config.companies[0], oracle_hcm_host="two.fa.oraclecloud.com"),
        ),
    )
    site_changed = replace(
        config,
        companies=(replace(config.companies[0], oracle_hcm_site="CX_TWO"),),
    )

    assert collection_config_fingerprint(host_changed) != collection_config_fingerprint(config)
    assert collection_config_fingerprint(site_changed) != collection_config_fingerprint(config)


def test_paylocity_identity_changes_collection_fingerprint():
    company = CompanyCfg(
        name="Paylocity Example",
        ats="paylocity",
        paylocity_company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        paylocity_module_id="1",
        paylocity_slug="Example",
    )
    config = WatcherConfig(companies=(company,))

    for field, value in (
        ("paylocity_company_id", "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        ("paylocity_module_id", "2"),
        ("paylocity_slug", "Other"),
    ):
        changed = replace(
            config,
            companies=(replace(company, **{field: value}),),
        )
        assert collection_config_fingerprint(changed) != (
            collection_config_fingerprint(config)
        )


def test_replay_defaults_to_snapshot_date_and_today_can_override(tmp_path):
    config = _config(cache_enabled=False)
    captured = datetime(2026, 6, 9, 23, 59, tzinfo=timezone.utc)
    batch = CollectionBatch.create(
        captured_at=captured,
        collection_config_fingerprint=collection_config_fingerprint(config),
        rows=(
            _row(
                "DirectCo",
                "Software Engineer Intern",
                deadline="2026-06-10",
            ),
        ),
        errors=(),
        source_attempts=(),
    )

    with SeenStore(tmp_path / "captured.sqlite") as store:
        captured_result = run_once(
            config,
            seen_store=store,
            alumni_index={},
            collection_batch=batch,
        )
    with SeenStore(tmp_path / "override.sqlite") as store:
        override_result = run_once(
            config,
            seen_store=store,
            alumni_index={},
            collection_batch=batch,
            today=date(2026, 6, 11),
        )

    assert captured_result.health_observed_at == captured
    assert captured_result.jobs[0]["deadline_days_left"] == 1
    assert override_result.jobs[0]["deadline_days_left"] == -1


@pytest.mark.parametrize(
    "kind",
    ["corrupt", "truncated", "malformed", "invalid_structure", "unsupported"],
)
def test_invalid_snapshots_fail_before_processing(tmp_path, kind):
    path = tmp_path / f"{kind}.json.gz"
    if kind == "corrupt":
        path.write_bytes(b"not gzip")
    elif kind == "truncated":
        valid = tmp_path / "valid.json.gz"
        save_collection_snapshot(_batch(), valid)
        path.write_bytes(valid.read_bytes()[:20])
    elif kind == "malformed":
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write('{"schema_version":')
    elif kind == "unsupported":
        payload = _batch().as_dict()
        payload["schema_version"] = COLLECTION_SNAPSHOT_SCHEMA_VERSION + 1
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)
    else:
        payload = _batch().as_dict()
        payload["rows"] = {"not": "an array"}
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            json.dump(payload, stream)

    match = (
        "Unsupported collection snapshot"
        if kind == "unsupported"
        else "rows must be an array"
        if kind == "invalid_structure"
        else "corrupt or truncated"
    )
    with pytest.raises(CollectionSnapshotError, match=match):
        load_collection_snapshot(path)


def test_snapshot_schema_rejects_unexpected_fields(tmp_path):
    path = tmp_path / "unexpected.json.gz"
    payload = _batch().as_dict()
    payload["unexpected"] = True
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(payload, stream)

    with pytest.raises(CollectionSnapshotError, match="unexpected fields"):
        load_collection_snapshot(path)


def test_snapshot_write_is_atomic_when_replacement_fails(tmp_path, monkeypatch):
    path = tmp_path / "capture.json.gz"
    first = _batch(rows=(_row("DirectCo", "First Intern"),))
    second = _batch(rows=(_row("DirectCo", "Second Intern"),))
    save_collection_snapshot(first, path)

    def failed_replace(_source, _target):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr("watcher.collection_snapshot.os.replace", failed_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        save_collection_snapshot(second, path)

    assert load_collection_snapshot(path) == first
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_snapshot_path_must_use_json_gz_extension(tmp_path):
    with pytest.raises(CollectionSnapshotError, match=r"\.json\.gz"):
        save_collection_snapshot(_batch(), tmp_path / "capture.json")


def test_snapshot_cli_options_are_mutually_exclusive(tmp_path):
    with pytest.raises(SystemExit) as raised:
        watcher_main(
            [
                "--capture-collection-snapshot",
                str(tmp_path / "capture.json.gz"),
                "--replay-collection-snapshot",
                str(tmp_path / "replay.json.gz"),
            ]
        )
    assert raised.value.code == 2


def test_cli_replay_is_network_free_and_side_effect_free_even_when_email_enabled(
    tmp_path,
    monkeypatch,
):
    config = _config(cache_enabled=False)
    snapshot_path = tmp_path / "capture.json.gz"
    db_path = tmp_path / "seen.sqlite"
    save_collection_snapshot(_batch(config), snapshot_path)
    with SeenStore(db_path):
        pass

    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("CLI replay invoked a forbidden operation")

    monkeypatch.setattr("watcher.cli.load_watchlist", lambda _path: config)
    monkeypatch.setattr("watcher.cli.email_sending_enabled", lambda: True)
    monkeypatch.setattr("watcher.collection._default_direct_sources", forbidden)
    monkeypatch.setattr("watcher.pipeline.send_digest", forbidden)
    monkeypatch.setattr("watcher.pipeline.evaluate_and_send_health_alerts", forbidden)
    monkeypatch.setattr("watcher.cli.print_report", lambda _result: None)
    monkeypatch.setattr("watcher.cli.print_heartbeat", lambda _result: None)
    monkeypatch.delenv("WATCHER_HEALTH_REPORT_PATH", raising=False)

    exit_code = watcher_main(
        [
            "--watchlist",
            str(tmp_path / "unused.yml"),
            "--seen-db",
            str(db_path),
            "--replay-collection-snapshot",
            str(snapshot_path),
            "--today",
            "2026-07-20",
        ]
    )

    assert exit_code == 0
    assert calls == []
    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        assert connection.execute("select count(*) from seen").fetchone()[0] == 0
    assert "source_health_attempts" not in tables
    assert "source_comparison_runs" not in tables
    assert "source_health_alert_events" not in tables
