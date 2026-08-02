import hashlib
import sqlite3
from datetime import datetime, timezone

from backend.app import config as backend_config
from backend.app.ingest import analyze_static_row
from backend.app.profile import load_profile
from scripts.migrate_analysis_cache import migrate_analysis_cache
from watcher.analysis_cache import (
    STATIC_ANALYSIS_CACHE_VERSION,
    AnalysisCache,
)
from watcher.health_alerts import HealthAlertStore
from watcher.seen_store import SeenStore
from watcher.source_comparison import SourceComparisonStore
from watcher.source_health import SourceHealthStore
from watcher.sources.base import make_row


def _artifact():
    row = make_row(
        source="direct",
        source_adapter="greenhouse",
        company="Stripe",
        title="Backend Engineering Intern",
        location="New York, NY",
        compensation="$35/hr",
        description="Build Python REST APIs with mentorship.",
        requirements="Python, SQL, REST APIs, Git",
        source_url="https://example.test/jobs/migration",
        internship_type="internship",
    )
    return analyze_static_row(
        row,
        profile=load_profile(),
        known=backend_config.load_known_companies(),
    )


def _initialize_legacy_database(path):
    with SeenStore(path):
        pass
    with SourceHealthStore(path):
        pass
    with HealthAlertStore(path):
        pass
    with SourceComparisonStore(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute(
            "create table durable_probe(name text primary key, value text)"
        )
        connection.execute(
            "insert into durable_probe(name, value) values ('keep', 'unchanged')"
        )
        connection.execute(
            """
            insert into seen(
              job_id, company, title, url, first_source, first_seen
            ) values (?, ?, ?, ?, ?, ?)
            """,
            (
                "durable-job",
                "Stripe",
                "Backend Intern",
                "https://example.test/jobs/durable",
                "direct",
                "2026-07-30T12:00:00+00:00",
            ),
        )
    keys = (
        hashlib.sha256(b"first").hexdigest(),
        hashlib.sha256(b"second").hexdigest(),
    )
    with AnalysisCache(path) as cache:
        assert cache.store_many(
            {keys[0]: _artifact(), keys[1]: _artifact()},
            accessed_at=datetime(2026, 7, 30, 12, tzinfo=timezone.utc),
        ) == 2
    return keys


def _cache_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            """
            select fingerprint, cache_version, artifact_json,
                   created_at, last_accessed_at
            from analysis_cache
            order by fingerprint
            """
        ).fetchall()


def _durable_state(path):
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                """
                select name
                from sqlite_master
                where type = 'table'
                  and name != 'analysis_cache'
                  and name not like 'sqlite_%'
                """
            )
        }
        return {
            table: connection.execute(
                f'select count(*) from "{table}"'
            ).fetchone()[0]
            for table in sorted(tables)
        }


def test_migration_copies_valid_artifacts_exactly_and_leaves_source_unchanged(
    tmp_path,
):
    source = tmp_path / "seen.sqlite"
    destination = tmp_path / "analysis-cache.sqlite"
    _initialize_legacy_database(source)
    expected_rows = _cache_rows(source)
    source_bytes = source.read_bytes()

    result = migrate_analysis_cache(source, destination)

    assert result.source_table_found is True
    assert result.source_rows == 2
    assert result.copied_rows == 2
    assert result.invalid_rows == 0
    assert result.cache_versions == ((STATIC_ANALYSIS_CACHE_VERSION, 2),)
    assert result.source_table_removed is False
    assert result.backup_path is None
    assert _cache_rows(destination) == expected_rows
    assert _cache_rows(source) == expected_rows
    assert source.read_bytes() == source_bytes


def test_migration_without_legacy_table_is_a_noop(tmp_path):
    source = tmp_path / "seen.sqlite"
    destination = tmp_path / "analysis-cache.sqlite"
    with SeenStore(source):
        pass
    source_bytes = source.read_bytes()

    result = migrate_analysis_cache(source, destination)

    assert result.source_table_found is False
    assert result.copied_rows == 0
    assert result.source_table_removed is False
    assert source.read_bytes() == source_bytes
    assert not destination.exists()


def test_destructive_migration_backs_up_and_preserves_all_non_cache_tables(
    tmp_path,
):
    source = tmp_path / "seen.sqlite"
    destination = tmp_path / "analysis-cache.sqlite"
    backup = tmp_path / "seen-before-removal.sqlite"
    _initialize_legacy_database(source)
    expected_cache_rows = _cache_rows(source)
    expected_durable_state = _durable_state(source)

    result = migrate_analysis_cache(
        source,
        destination,
        remove_source_table=True,
        backup_path=backup,
    )

    assert result.source_table_removed is True
    assert result.backup_path == backup
    assert backup.is_file()
    assert _cache_rows(backup) == expected_cache_rows
    assert _cache_rows(destination) == expected_cache_rows
    assert _durable_state(source) == expected_durable_state
    with sqlite3.connect(source) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type = 'index'"
            )
        }
        assert connection.execute("pragma quick_check").fetchone()[0] == "ok"
        assert connection.execute(
            "select value from durable_probe where name = 'keep'"
        ).fetchone()[0] == "unchanged"
    assert "analysis_cache" not in tables
    assert "analysis_cache_last_accessed_idx" not in indexes
    assert "seen" in tables
    assert "source_health_attempts" in tables
    assert "source_health_alert_state" in tables
    assert "source_comparison_runs" in tables


def test_destructive_migration_skips_invalid_cache_rows_but_keeps_backup(
    tmp_path,
):
    source = tmp_path / "seen.sqlite"
    destination = tmp_path / "analysis-cache.sqlite"
    backup = tmp_path / "seen-before-removal.sqlite"
    keys = _initialize_legacy_database(source)
    with sqlite3.connect(source) as connection:
        connection.execute(
            """
            update analysis_cache
            set artifact_json = '{invalid-json'
            where fingerprint = ?
            """,
            (keys[0],),
        )

    result = migrate_analysis_cache(
        source,
        destination,
        remove_source_table=True,
        backup_path=backup,
    )

    assert result.source_rows == 2
    assert result.copied_rows == 1
    assert result.invalid_rows == 1
    assert len(_cache_rows(destination)) == 1
    assert len(_cache_rows(backup)) == 2
    with sqlite3.connect(source) as connection:
        assert connection.execute(
            """
            select count(*)
            from sqlite_master
            where type = 'table' and name = 'analysis_cache'
            """
        ).fetchone()[0] == 0
        assert connection.execute("pragma quick_check").fetchone()[0] == "ok"
