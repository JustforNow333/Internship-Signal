#!/usr/bin/env python3
"""Copy a legacy analysis_cache table into its dedicated SQLite database."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.ingest import static_analysis_artifact_is_valid  # noqa: E402
from watcher.analysis_cache import (  # noqa: E402
    STATIC_ANALYSIS_CACHE_VERSION,
    AnalysisCache,
)

_CACHE_COLUMNS = (
    "fingerprint",
    "cache_version",
    "artifact_json",
    "created_at",
    "last_accessed_at",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class MigrationError(RuntimeError):
    """Raised when a cache migration cannot be completed safely."""


@dataclass(frozen=True)
class MigrationResult:
    source_table_found: bool
    source_rows: int
    copied_rows: int
    invalid_rows: int
    cache_versions: tuple[tuple[int, int], ...]
    source_table_removed: bool
    backup_path: Path | None


def migrate_analysis_cache(
    source: str | Path,
    destination: str | Path,
    *,
    remove_source_table: bool = False,
    backup_path: str | Path | None = None,
) -> MigrationResult:
    """Copy valid legacy cache rows and optionally remove only that table."""

    source_path = Path(source)
    destination_path = Path(destination)
    if not source_path.is_file():
        raise MigrationError(f"Source SQLite database does not exist: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise MigrationError("Source and destination SQLite paths must differ")

    source_connection = _connect_read_only(source_path)
    try:
        _require_quick_check(source_connection, "source")
        if not _table_exists(source_connection, "analysis_cache"):
            if destination_path.exists():
                destination_connection = _connect_read_only(destination_path)
                try:
                    _require_quick_check(destination_connection, "destination")
                finally:
                    destination_connection.close()
            return MigrationResult(
                source_table_found=False,
                source_rows=0,
                copied_rows=0,
                invalid_rows=0,
                cache_versions=(),
                source_table_removed=False,
                backup_path=None,
            )
        _require_cache_columns(source_connection)
        source_rows = source_connection.execute(
            """
            select fingerprint, cache_version, artifact_json,
                   created_at, last_accessed_at
            from analysis_cache
            order by fingerprint
            """
        ).fetchall()
    finally:
        source_connection.close()

    valid_rows, invalid_rows = _valid_cache_rows(source_rows)
    _initialize_destination(destination_path)
    destination_connection = sqlite3.connect(destination_path)
    try:
        _require_quick_check(destination_connection, "destination")
        with destination_connection:
            destination_connection.executemany(
                """
                insert into analysis_cache(
                  fingerprint, cache_version, artifact_json,
                  created_at, last_accessed_at
                )
                values (?, ?, ?, ?, ?)
                on conflict(fingerprint) do update set
                  cache_version = excluded.cache_version,
                  artifact_json = excluded.artifact_json,
                  created_at = excluded.created_at,
                  last_accessed_at = excluded.last_accessed_at
                """,
                valid_rows,
            )
        _verify_copied_rows(destination_connection, valid_rows)
        _require_quick_check(destination_connection, "destination")
    except sqlite3.Error as exc:
        raise MigrationError(
            f"Destination cache migration failed: {type(exc).__name__}"
        ) from exc
    finally:
        destination_connection.close()

    versions = tuple(sorted(Counter(row[1] for row in valid_rows).items()))
    actual_backup_path: Path | None = None
    removed = False
    if remove_source_table:
        actual_backup_path = (
            Path(backup_path)
            if backup_path is not None
            else _default_backup_path(source_path)
        )
        _backup_database(source_path, actual_backup_path)
        _remove_source_cache_table(source_path)
        removed = True

    return MigrationResult(
        source_table_found=True,
        source_rows=len(source_rows),
        copied_rows=len(valid_rows),
        invalid_rows=invalid_rows,
        cache_versions=versions,
        source_table_removed=removed,
        backup_path=actual_backup_path,
    )


def _connect_read_only(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
    except sqlite3.Error as exc:
        raise MigrationError(
            f"Could not open SQLite database read-only: {path}"
        ) from exc
    return connection


def _require_quick_check(connection: sqlite3.Connection, label: str) -> None:
    try:
        results = [
            str(row[0])
            for row in connection.execute("pragma quick_check").fetchall()
        ]
    except sqlite3.Error as exc:
        raise MigrationError(
            f"{label.capitalize()} SQLite quick_check could not run"
        ) from exc
    if results != ["ok"]:
        raise MigrationError(
            f"{label.capitalize()} SQLite quick_check failed"
        )


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        """
        select 1
        from sqlite_master
        where type = 'table' and name = ?
        """,
        (name,),
    ).fetchone()
    return row is not None


def _require_cache_columns(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute(
            "pragma table_info(analysis_cache)"
        ).fetchall()
    }
    missing = set(_CACHE_COLUMNS) - columns
    if missing:
        raise MigrationError(
            "Source analysis_cache schema is missing required columns: "
            + ", ".join(sorted(missing))
        )


def _valid_cache_rows(
    rows: Iterable[tuple[object, ...]],
) -> tuple[list[tuple[str, int, str, str, str]], int]:
    valid: list[tuple[str, int, str, str, str]] = []
    invalid = 0
    for row in rows:
        fingerprint, cache_version, artifact_json, created_at, last_accessed_at = row
        if (
            not isinstance(fingerprint, str)
            or _SHA256_RE.fullmatch(fingerprint) is None
            or isinstance(cache_version, bool)
            or not isinstance(cache_version, int)
            or cache_version < 1
            or not isinstance(artifact_json, str)
            or not isinstance(created_at, str)
            or not created_at
            or not isinstance(last_accessed_at, str)
            or not last_accessed_at
        ):
            invalid += 1
            continue
        try:
            artifact = json.loads(artifact_json)
        except (json.JSONDecodeError, TypeError, ValueError):
            invalid += 1
            continue
        if not isinstance(artifact, dict):
            invalid += 1
            continue
        if (
            cache_version == STATIC_ANALYSIS_CACHE_VERSION
            and not static_analysis_artifact_is_valid(artifact)
        ):
            invalid += 1
            continue
        valid.append(
            (
                fingerprint,
                cache_version,
                artifact_json,
                created_at,
                last_accessed_at,
            )
        )
    return valid, invalid


def _initialize_destination(path: Path) -> None:
    try:
        with AnalysisCache(path):
            pass
    except (OSError, sqlite3.Error) as exc:
        raise MigrationError(
            f"Could not initialize destination cache database: {path}"
        ) from exc


def _verify_copied_rows(
    connection: sqlite3.Connection,
    expected_rows: list[tuple[str, int, str, str, str]],
) -> None:
    expected = {row[0]: row for row in expected_rows}
    actual: dict[str, tuple[str, int, str, str, str]] = {}
    fingerprints = tuple(expected)
    for start in range(0, len(fingerprints), 500):
        batch = fingerprints[start:start + 500]
        placeholders = ",".join("?" for _ in batch)
        rows = connection.execute(
            f"""
            select fingerprint, cache_version, artifact_json,
                   created_at, last_accessed_at
            from analysis_cache
            where fingerprint in ({placeholders})
            """,
            batch,
        ).fetchall()
        actual.update((str(row[0]), tuple(row)) for row in rows)
    if actual != expected:
        raise MigrationError(
            "Destination verification failed: copied row values or counts differ"
        )
    expected_versions = Counter(row[1] for row in expected.values())
    actual_versions = Counter(row[1] for row in actual.values())
    if actual_versions != expected_versions:
        raise MigrationError(
            "Destination verification failed: cache-version counts differ"
        )


def _default_backup_path(source: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return source.with_name(
        f"{source.name}.pre-analysis-cache-removal-{timestamp}.bak"
    )


def _backup_database(source: Path, backup: Path) -> None:
    if backup.exists():
        raise MigrationError(f"Backup path already exists: {backup}")
    backup.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{backup.name}.",
        suffix=".tmp",
        dir=backup.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()
    try:
        source_connection = sqlite3.connect(source)
        backup_connection = sqlite3.connect(temporary_path)
        try:
            source_connection.backup(backup_connection)
            _require_quick_check(backup_connection, "backup")
        finally:
            backup_connection.close()
            source_connection.close()
        os.replace(temporary_path, backup)
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        if isinstance(exc, MigrationError):
            raise
        raise MigrationError("Could not create the pre-removal backup") from exc


def _remove_source_cache_table(source: Path) -> None:
    connection = sqlite3.connect(source)
    try:
        _require_quick_check(connection, "source")
        durable_tables_before = _non_cache_table_counts(connection)
        cache_indexes = [
            str(row[1])
            for row in connection.execute(
                "pragma index_list(analysis_cache)"
            ).fetchall()
            if not str(row[1]).startswith("sqlite_autoindex")
        ]
        with connection:
            for index_name in cache_indexes:
                connection.execute(
                    f"drop index if exists {_quote_identifier(index_name)}"
                )
            connection.execute("drop table analysis_cache")
        connection.execute("vacuum")
        _require_quick_check(connection, "source")
        if _table_exists(connection, "analysis_cache"):
            raise MigrationError("Source analysis_cache table still exists")
        if _non_cache_table_counts(connection) != durable_tables_before:
            raise MigrationError(
                "Durable table names or row counts changed during cache removal"
            )
    except sqlite3.Error as exc:
        raise MigrationError(
            f"Source cache-table removal failed: {type(exc).__name__}"
        ) from exc
    finally:
        connection.close()


def _non_cache_table_counts(
    connection: sqlite3.Connection,
) -> dict[str, int]:
    names = [
        str(row[0])
        for row in connection.execute(
            """
            select name
            from sqlite_master
            where type = 'table'
              and name != 'analysis_cache'
              and name not like 'sqlite_%'
            order by name
            """
        ).fetchall()
    ]
    return {
        name: int(
            connection.execute(
                f"select count(*) from {_quote_identifier(name)}"
            ).fetchone()[0]
        )
        for name in names
    }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument(
        "--remove-source-table",
        action="store_true",
        help=(
            "Back up the source, then drop only analysis_cache and its indexes."
        ),
    )
    parser.add_argument(
        "--backup",
        type=Path,
        help="Optional backup path used with --remove-source-table.",
    )
    args = parser.parse_args(argv)
    if args.backup is not None and not args.remove_source_table:
        parser.error("--backup requires --remove-source-table")
    try:
        result = migrate_analysis_cache(
            args.source,
            args.destination,
            remove_source_table=args.remove_source_table,
            backup_path=args.backup,
        )
    except MigrationError as exc:
        parser.exit(1, f"analysis-cache migration failed: {exc}\n")

    versions = (
        "|".join(f"{version}:{count}" for version, count in result.cache_versions)
        or "none"
    )
    backup = str(result.backup_path) if result.backup_path else "none"
    print(
        "ANALYSIS-CACHE-MIGRATION "
        f"source_table={'true' if result.source_table_found else 'false'} "
        f"source_rows={result.source_rows} copied={result.copied_rows} "
        f"invalid={result.invalid_rows} versions={versions} "
        f"source_removed={'true' if result.source_table_removed else 'false'} "
        f"backup={backup}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
