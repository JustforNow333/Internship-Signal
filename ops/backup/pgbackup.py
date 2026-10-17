"""Encrypted, verified, offsite PostgreSQL backups for FindSooner.

Commands (see ops/backup/README.md):

    pgbackup.py backup    dump, restore-verify, encrypt, upload, verify upload
    pgbackup.py monitor   report the latest verified backup; alert if stale
    pgbackup.py restore   restore a backup into an explicitly named, empty,
                          non-production database

A backup counts as usable only once its manifest exists. The manifest is
written last, after the dump was restored into a throwaway PostgreSQL server
and matched table for table, and after the uploaded object was downloaded
again and matched byte for byte. Nothing here ever writes to the source
database or deletes an object from storage.

Output is bounded and sanitized: counts, sizes, hashes, and object keys,
never connection strings, credentials, or row contents.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "HOSTED-BACKUP"
MANIFEST_VERSION = 1
# Distinct from the import lock, so a backup never waits on an import.
LOCK_SQL = "SELECT pg_try_advisory_lock(hashtextextended('findsooner-backup', 0))"
UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended('findsooner-backup', 0))"
OBJECT_SUFFIX = ".dump.age"
MANIFEST_SUFFIX = ".manifest.json"
_KEY_RE = re.compile(r"findsooner-(\d{8}T\d{6}Z)-[0-9a-f]{8}" + re.escape(MANIFEST_SUFFIX) + "$")
_CHUNK = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 1800
HTTP_TIMEOUT_SECONDS = 120


class BackupError(Exception):
    """A backup, verification, or restore step failed; ``stage`` is safe to log."""

    def __init__(self, stage: str, detail: str = "") -> None:
        super().__init__(f"{stage}: {detail}" if detail else stage)
        self.stage = stage
        self.detail = detail


class AlreadyRunning(BackupError):
    def __init__(self) -> None:
        super().__init__("lock", "another backup is running")


# --- configuration ------------------------------------------------------------


@dataclass(frozen=True)
class StorageConfig:
    endpoint: str
    region: str
    bucket: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    prefix: str = "findsooner/postgres"


def storage_config_from_env(env: Mapping[str, str] = os.environ) -> StorageConfig:
    values = {
        name: (env.get(f"BACKUP_S3_{name.upper()}") or "").strip()
        for name in ("endpoint", "region", "bucket", "access_key_id", "secret_access_key")
    }
    missing = [f"BACKUP_S3_{name.upper()}" for name, value in values.items() if not value]
    if missing:
        raise BackupError("config", "missing " + ",".join(missing))
    if not values["endpoint"].startswith("https://"):
        raise BackupError("config", "BACKUP_S3_ENDPOINT must use https")
    prefix = (env.get("BACKUP_S3_PREFIX") or "findsooner/postgres").strip().strip("/")
    return StorageConfig(prefix=prefix, **values)


def age_recipients_from_env(env: Mapping[str, str] = os.environ) -> list[str]:
    recipients = [
        value.strip()
        for value in (env.get("BACKUP_AGE_RECIPIENTS") or "").split(",")
        if value.strip()
    ]
    if not recipients or not all(value.startswith("age1") for value in recipients):
        raise BackupError("config", "BACKUP_AGE_RECIPIENTS must list age public keys")
    return recipients


# --- S3-compatible object storage ------------------------------------------------


class ObjectExists(BackupError):
    def __init__(self, key: str) -> None:
        super().__init__("upload", f"object already exists: {key}")


class S3Storage:
    """Minimal path-style S3 client (SigV4) with no delete operation.

    Writes are create-only (``If-None-Match: *``), so an existing backup can
    never be overwritten through this client.
    """

    def __init__(
        self,
        config: StorageConfig,
        *,
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self.config = config
        self._clock = clock
        self._timeout = timeout
        parsed = urllib.parse.urlsplit(config.endpoint)
        self._scheme = parsed.scheme
        self._host = parsed.netloc
        self._base = parsed.path.rstrip("/")

    def _path(self, key: str = "") -> str:
        bucket = urllib.parse.quote(self.config.bucket, safe="")
        if not key:
            return f"{self._base}/{bucket}"
        return f"{self._base}/{bucket}/{urllib.parse.quote(key, safe='/-_.~')}"

    def _request(
        self,
        method: str,
        key: str = "",
        *,
        query: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        body: Path | bytes | None = None,
        payload_hash: str | None = None,
    ) -> http.client.HTTPResponse:
        query = dict(query or {})
        if body is None:
            payload_hash = hashlib.sha256(b"").hexdigest()
        elif payload_hash is None:
            payload_hash = (
                sha256_file(body) if isinstance(body, Path) else hashlib.sha256(body).hexdigest()
            )
        signed = sign_v4(
            method=method,
            host=self._host,
            path=self._path(key),
            query=query,
            headers=dict(headers or {}),
            payload_hash=payload_hash,
            region=self.config.region,
            access_key_id=self.config.access_key_id,
            secret_access_key=self.config.secret_access_key,
            now=self._clock(),
        )
        target = self._path(key)
        if query:
            target += "?" + canonical_query(query)
        connection_class = (
            http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_class(self._host, timeout=self._timeout)
        try:
            if isinstance(body, Path):
                signed["Content-Length"] = str(body.stat().st_size)
                with body.open("rb") as stream:
                    connection.request(method, target, body=stream, headers=signed)
                    response = connection.getresponse()
            else:
                if body is not None:
                    signed["Content-Length"] = str(len(body))
                connection.request(method, target, body=body, headers=signed)
                response = connection.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            connection.close()
            raise BackupError("storage", f"{method} failed ({type(exc).__name__})") from None
        response.connection_to_close = connection  # type: ignore[attr-defined]
        return response

    @staticmethod
    def _finish(response: http.client.HTTPResponse) -> bytes:
        try:
            return response.read()
        finally:
            response.connection_to_close.close()  # type: ignore[attr-defined]

    def put_new(self, key: str, body: Path | bytes, *, content_type: str) -> None:
        """Create ``key``; fail if it exists or the upload is not confirmed."""

        response = self._request(
            "PUT",
            key,
            headers={"If-None-Match": "*", "Content-Type": content_type},
            body=body,
        )
        self._finish(response)
        if response.status == 412:
            raise ObjectExists(key)
        if response.status != 200:
            raise BackupError("upload", f"PUT returned HTTP {response.status}")

    def size(self, key: str) -> int | None:
        response = self._request("HEAD", key)
        self._finish(response)
        if response.status == 404:
            return None
        if response.status != 200:
            raise BackupError("storage", f"HEAD returned HTTP {response.status}")
        return int(response.getheader("Content-Length") or -1)

    def download(self, key: str, target: Path) -> str:
        """Stream ``key`` to ``target`` and return its SHA-256."""

        response = self._request("GET", key)
        digest = hashlib.sha256()
        try:
            if response.status != 200:
                raise BackupError("download", f"GET returned HTTP {response.status}")
            with target.open("wb") as stream:
                for block in iter(lambda: response.read(_CHUNK), b""):
                    digest.update(block)
                    stream.write(block)
        finally:
            response.connection_to_close.close()  # type: ignore[attr-defined]
        return digest.hexdigest()

    def get_bytes(self, key: str, *, limit: int = 1_000_000) -> bytes:
        response = self._request("GET", key)
        data = self._finish(response)
        if response.status != 200:
            raise BackupError("download", f"GET returned HTTP {response.status}")
        if len(data) > limit:
            raise BackupError("download", "object larger than expected")
        return data

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = ""
        for _page in range(1000):
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            response = self._request("GET", query=query)
            data = self._finish(response)
            if response.status != 200:
                raise BackupError("storage", f"LIST returned HTTP {response.status}")
            root = ElementTree.fromstring(data)
            namespace = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            keys.extend(
                node.text or "" for node in root.iter(f"{namespace}Key")
            )
            truncated = root.findtext(f"{namespace}IsTruncated") == "true"
            token = root.findtext(f"{namespace}NextContinuationToken") or ""
            if not truncated or not token:
                return keys
        raise BackupError("storage", "listing did not terminate")


def canonical_query(query: Mapping[str, str]) -> str:
    return "&".join(
        f"{urllib.parse.quote(key, safe='-_.~')}={urllib.parse.quote(value, safe='-_.~')}"
        for key, value in sorted(query.items())
    )


def sign_v4(
    *,
    method: str,
    host: str,
    path: str,
    query: Mapping[str, str],
    headers: dict[str, str],
    payload_hash: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    now: dt.datetime,
    service: str = "s3",
) -> dict[str, str]:
    """Return ``headers`` plus the AWS Signature Version 4 headers."""

    amz_date = now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    signed_headers = {key.lower(): str(value).strip() for key, value in headers.items()}
    signed_headers["host"] = host
    signed_headers["x-amz-content-sha256"] = payload_hash
    signed_headers["x-amz-date"] = amz_date
    names = sorted(signed_headers)
    canonical_request = "\n".join(
        [
            method,
            path or "/",
            canonical_query(query),
            "".join(f"{name}:{signed_headers[name]}\n" for name in names),
            ";".join(names),
            payload_hash,
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = ("AWS4" + secret_access_key).encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    result = {name: value for name, value in signed_headers.items() if name != "host"}
    result["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key_id}/{scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )
    return result


# --- PostgreSQL helpers --------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def libpq_env(database_url: str) -> dict[str, str]:
    """Connection settings as libpq variables, so no URL reaches argv."""

    from psycopg.conninfo import conninfo_to_dict

    url = re.sub(r"^postgres(ql)?\+\w+://", "postgresql://", database_url.strip())
    params = conninfo_to_dict(url)
    mapping = {
        "host": "PGHOST",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "dbname": "PGDATABASE",
        "sslmode": "PGSSLMODE",
    }
    return {variable: str(params[name]) for name, variable in mapping.items() if params.get(name)}


def connect(database_url: str, **kwargs):
    import psycopg

    return psycopg.connect(**_conninfo(database_url), **kwargs)


def _conninfo(database_url: str) -> dict[str, str]:
    env = libpq_env(database_url)
    names = {
        "PGHOST": "host",
        "PGPORT": "port",
        "PGUSER": "user",
        "PGPASSWORD": "password",
        "PGDATABASE": "dbname",
        "PGSSLMODE": "sslmode",
    }
    return {names[key]: value for key, value in env.items()}


def same_database(first: str, second: str) -> bool:
    try:
        a, b = _conninfo(first), _conninfo(second)
    except Exception:  # noqa: BLE001 - unparseable URLs are treated as identical
        return True
    def norm(params: Mapping[str, str]) -> tuple[str, str, str]:
        host = (params.get("host") or "localhost").casefold()
        if host in {"127.0.0.1", "::1", "[::1]"} or host.startswith("/"):
            host = "localhost"
        return host, params.get("port") or "5432", params.get("dbname") or ""
    return norm(a) == norm(b)


def table_counts(connection) -> dict[str, int]:
    """Exact row counts of every ordinary table in user schemas."""

    tables = connection.execute(
        "SELECT n.nspname, c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relkind IN ('r', 'p') AND n.nspname !~ '^pg_' "
        "AND n.nspname <> 'information_schema' ORDER BY 1, 2"
    ).fetchall()
    counts: dict[str, int] = {}
    for schema, table in tables:
        quoted = f'"{schema.replace(chr(34), chr(34) * 2)}"."{table.replace(chr(34), chr(34) * 2)}"'
        counts[f"{schema}.{table}"] = connection.execute(
            f"SELECT count(*) FROM {quoted}"
        ).fetchone()[0]
    return counts


def alembic_revision(connection) -> str | None:
    exists = connection.execute("SELECT to_regclass('public.alembic_version')").fetchone()[0]
    if exists is None:
        return None
    rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    return ",".join(sorted(row[0] for row in rows)) or None


Runner = Callable[..., subprocess.CompletedProcess]


def run_tool(
    runner: Runner,
    args: Sequence[str],
    *,
    stage: str,
    env: Mapping[str, str] | None = None,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    try:
        result = runner(
            list(args),
            env={**os.environ, **(env or {})},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        raise BackupError(stage, f"{Path(args[0]).name} not found") from None
    except subprocess.TimeoutExpired:
        raise BackupError(stage, "timed out") from None
    if result.returncode != 0:
        raise BackupError(stage, f"{Path(args[0]).name} exited {result.returncode}")
    return result


@dataclass(frozen=True)
class Tools:
    bin_dir: str = ""
    age: str = "age"

    def pg(self, name: str) -> str:
        return str(Path(self.bin_dir) / name) if self.bin_dir else name

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Tools:
        return cls(bin_dir=(env.get("PG_BIN") or "").strip(), age=(env.get("AGE_BIN") or "age").strip())


def archive_tables(runner: Runner, tools: Tools, archive: Path) -> set[str]:
    """Tables whose data the custom-format archive contains."""

    listing = run_tool(runner, [tools.pg("pg_restore"), "--list", str(archive)], stage="archive_list")
    tables = set()
    for line in listing.stdout.splitlines():
        match = re.search(r"\bTABLE DATA (\S+) (\S+) ", line)
        if match:
            tables.add(f"{match.group(1)}.{match.group(2)}")
    if not tables:
        raise BackupError("archive_list", "archive contains no table data")
    return tables


@contextlib.contextmanager
def scratch_server(runner: Runner, tools: Tools, workdir: Path) -> Iterator[str]:
    """A throwaway local PostgreSQL server; yields its connection URL."""

    data = workdir / "restore-data"
    sockets = workdir / "restore-socket"
    sockets.mkdir()
    port = str(_free_port())
    run_tool(
        runner,
        [tools.pg("initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8", "--no-sync"],
        stage="restore_initdb",
    )
    options = (
        f"-p {port} -c listen_addresses=localhost -c fsync=off "
        "-c shared_buffers=32MB -c max_connections=10"
    )
    if os.name != "nt":
        options += f" -k {sockets}"
    run_tool(
        runner,
        [tools.pg("pg_ctl"), "-D", str(data), "-o", options, "-l", str(workdir / "restore.log"), "-w", "-t", "120", "start"],
        stage="restore_start",
    )
    try:
        yield f"postgresql://postgres@localhost:{port}/postgres"
    finally:
        with contextlib.suppress(BackupError):
            run_tool(
                runner,
                [tools.pg("pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "-t", "60", "stop"],
                stage="restore_stop",
            )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def restore_archive(
    runner: Runner, tools: Tools, archive: Path, target_url: str, *, stage: str
) -> None:
    run_tool(
        runner,
        [
            tools.pg("pg_restore"),
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--single-transaction",
            "-d",
            libpq_env(target_url).get("PGDATABASE", "postgres"),
            str(archive),
        ],
        stage=stage,
        env=libpq_env(target_url),
    )


def verify_restored(target_url: str, counts: Mapping[str, int], revision: str | None, *, stage: str) -> None:
    with connect(target_url) as connection:
        restored = table_counts(connection)
        restored_revision = alembic_revision(connection)
    if set(restored) != set(counts):
        raise BackupError(stage, "restored tables differ from the source")
    mismatched = sorted(name for name, value in counts.items() if restored[name] != value)
    if mismatched:
        raise BackupError(stage, f"row counts differ in {len(mismatched)} tables")
    if restored_revision != revision:
        raise BackupError(stage, "alembic revision differs")


# --- backup ---------------------------------------------------------------------


@dataclass(frozen=True)
class BackupResult:
    key: str
    manifest_key: str
    size_bytes: int
    sha256: str
    tables: int
    rows: int
    seconds: float


def object_key(prefix: str, started: dt.datetime) -> str:
    stamp = started.astimezone(dt.UTC)
    return (
        f"{prefix}/{stamp:%Y/%m}/findsooner-{stamp:%Y%m%dT%H%M%SZ}-"
        f"{secrets.token_hex(4)}{OBJECT_SUFFIX}"
    )


def run_backup(
    database_url: str,
    storage: S3Storage,
    recipients: Sequence[str],
    *,
    tools: Tools = Tools(),
    runner: Runner = subprocess.run,
    clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    workroot: str | None = None,
) -> BackupResult:
    started = clock()
    began = time.monotonic()
    workdir = Path(tempfile.mkdtemp(prefix="findsooner-backup-", dir=workroot))
    try:
        with connect(database_url, autocommit=True) as lock_connection:
            if not lock_connection.execute(LOCK_SQL).fetchone()[0]:
                raise AlreadyRunning()
            try:
                return _backup_locked(
                    database_url, storage, recipients, tools, runner, started, began, workdir
                )
            finally:
                with contextlib.suppress(Exception):
                    lock_connection.execute(UNLOCK_SQL)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _backup_locked(database_url, storage, recipients, tools, runner, started, began, workdir):
    dump = workdir / "backup.dump"
    with connect(database_url, autocommit=True) as snapshot_connection:
        snapshot_connection.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot = snapshot_connection.execute("SELECT pg_export_snapshot()").fetchone()[0]
        counts = table_counts(snapshot_connection)
        revision = alembic_revision(snapshot_connection)
        server_version = snapshot_connection.execute("SHOW server_version").fetchone()[0]
        # The dump reads the same snapshot as the counts above.
        run_tool(
            runner,
            [
                tools.pg("pg_dump"),
                "--format=custom",
                "--compress=6",
                f"--snapshot={snapshot}",
                "--no-password",
                f"--file={dump}",
            ],
            stage="pg_dump",
            env=libpq_env(database_url),
        )
        snapshot_connection.execute("ROLLBACK")
    if not dump.is_file() or dump.stat().st_size == 0:
        raise BackupError("pg_dump", "empty archive")
    archived = archive_tables(runner, tools, dump)
    if not set(counts) <= archived | {name for name, value in counts.items() if value == 0}:
        raise BackupError("archive_list", "archive is missing table data")

    with scratch_server(runner, tools, workdir) as scratch_url:
        run_tool(
            runner,
            [tools.pg("createdb"), "restore_check"],
            stage="restore_createdb",
            env=libpq_env(scratch_url),
        )
        check_url = scratch_url.rsplit("/", 1)[0] + "/restore_check"
        restore_archive(runner, tools, dump, check_url, stage="restore_check")
        verify_restored(check_url, counts, revision, stage="restore_check")

    encrypted = workdir / "backup.dump.age"
    args = [tools.age]
    for recipient in recipients:
        args += ["-r", recipient]
    run_tool(runner, [*args, "-o", str(encrypted), str(dump)], stage="encrypt")
    plaintext_sha256 = sha256_file(dump)
    plaintext_size = dump.stat().st_size
    dump.unlink()

    size = encrypted.stat().st_size
    digest = sha256_file(encrypted)
    key = object_key(storage.config.prefix, started)
    storage.put_new(key, encrypted, content_type="application/octet-stream")
    if storage.size(key) != size:
        raise BackupError("upload_verify", "stored size differs")
    if storage.download(key, workdir / "verify.age") != digest:
        raise BackupError("upload_verify", "stored checksum differs")
    (workdir / "verify.age").unlink()

    finished = dt.datetime.now(dt.UTC)
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "object_key": key,
        "started_at": started.astimezone(dt.UTC).isoformat(),
        "finished_at": finished.isoformat(),
        "format": "pg_dump custom, age encrypted",
        "encrypted_size_bytes": size,
        "encrypted_sha256": digest,
        "plaintext_size_bytes": plaintext_size,
        "plaintext_sha256": plaintext_sha256,
        "server_version": server_version,
        "alembic_revision": revision,
        "table_counts": counts,
        "upload_verified": True,
        "restore_verified": True,
        "age_recipients": list(recipients),
    }
    manifest_key = key[: -len(OBJECT_SUFFIX)] + MANIFEST_SUFFIX
    storage.put_new(
        manifest_key,
        json.dumps(manifest, sort_keys=True, indent=2).encode(),
        content_type="application/json",
    )
    return BackupResult(
        key=key,
        manifest_key=manifest_key,
        size_bytes=size,
        sha256=digest,
        tables=len(counts),
        rows=sum(counts.values()),
        seconds=time.monotonic() - began,
    )


# --- monitoring -----------------------------------------------------------------


@dataclass(frozen=True)
class BackupStatus:
    status: str  # ok | stale | missing
    retained: int
    unverified_newer: int = 0
    latest_key: str | None = None
    finished_at: dt.datetime | None = None
    age_hours: float | None = None
    size_bytes: int | None = None
    upload_verified: bool = False
    restore_verified: bool = False

    def line(self) -> str:
        fields = {
            "status": self.status,
            "last_success": self.finished_at.isoformat() if self.finished_at else "none",
            "age_hours": f"{self.age_hours:.1f}" if self.age_hours is not None else "none",
            "size_bytes": self.size_bytes if self.size_bytes is not None else "none",
            "upload_verified": str(self.upload_verified).lower(),
            "restore_verified": str(self.restore_verified).lower(),
            "retained": self.retained,
            # Uploaded after the latest manifest but never verified: a failed run.
            "unverified_newer": self.unverified_newer,
        }
        return f"{PREFIX}-STATUS " + " ".join(f"{key}={value}" for key, value in fields.items())


def backup_status(
    storage: S3Storage,
    *,
    now: dt.datetime,
    max_age_hours: float = 36,
    retention_days: int = 35,
) -> BackupStatus:
    keys = set(storage.list_keys(storage.config.prefix + "/"))
    # A backup is usable only while both its manifest and its object exist.
    manifests = sorted(
        key
        for key in keys
        if _KEY_RE.search(key) and key[: -len(MANIFEST_SUFFIX)] + OBJECT_SUFFIX in keys
    )
    objects = sorted(key for key in keys if key.endswith(OBJECT_SUFFIX))
    if not manifests:
        return BackupStatus(status="missing", retained=0, unverified_newer=len(objects))
    cutoff = now - dt.timedelta(days=retention_days)
    retained = sum(1 for key in manifests if _manifest_time(key) >= cutoff)
    latest = manifests[-1]
    latest_object = latest[: -len(MANIFEST_SUFFIX)] + OBJECT_SUFFIX
    unverified_newer = sum(1 for key in objects if key > latest_object)
    manifest = json.loads(storage.get_bytes(latest))
    finished = dt.datetime.fromisoformat(manifest["finished_at"])
    age = (now - finished).total_seconds() / 3600
    verified = bool(manifest.get("upload_verified")) and bool(manifest.get("restore_verified"))
    return BackupStatus(
        status="ok" if age <= max_age_hours and verified else "stale",
        retained=retained,
        unverified_newer=unverified_newer,
        latest_key=latest,
        finished_at=finished,
        age_hours=age,
        size_bytes=manifest.get("encrypted_size_bytes"),
        upload_verified=bool(manifest.get("upload_verified")),
        restore_verified=bool(manifest.get("restore_verified")),
    )


def _manifest_time(key: str) -> dt.datetime:
    match = _KEY_RE.search(key)
    assert match is not None
    return dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC)


def should_alert(
    status: BackupStatus,
    *,
    now: dt.datetime,
    max_age_hours: float = 36,
    interval_hours: float = 6,
) -> bool:
    """Alert about one unresolved problem at most about once a day.

    Stateless: the monitor runs every ``interval_hours`` and alerts only in the
    first run of each 24-hour period of the outage, so an unresolved failure
    keeps resurfacing daily without paging every run.
    """

    if status.status == "ok":
        return False
    if status.status == "missing":
        return now.hour < interval_hours
    overdue = (status.age_hours or 0) - max_age_hours
    if overdue < 0:  # verified flags missing on an otherwise recent backup
        return now.hour < interval_hours
    return overdue % 24 < interval_hours


def send_alert(
    subject: str,
    text: str,
    *,
    env: Mapping[str, str] = os.environ,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bool:
    """Email the operator through the existing Resend account. Never raises."""

    api_key = (env.get("HOSTED_RESEND_API_KEY") or "").strip()
    sender = (env.get("HOSTED_RESEND_FROM_EMAIL") or "").strip()
    recipient = (env.get("BACKUP_ALERT_EMAIL") or "").strip()
    if not (api_key and sender and recipient):
        print(f"{PREFIX}-ALERT status=not_configured", flush=True)
        return False
    request = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({"from": sender, "to": [recipient], "subject": subject, "text": text}).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with opener(request, timeout=30):
            pass
    except Exception as exc:  # noqa: BLE001 - alert delivery must not mask the outcome
        print(f"{PREFIX}-ALERT status=failed error={type(exc).__name__}", flush=True)
        return False
    print(f"{PREFIX}-ALERT status=sent", flush=True)
    return True


def ping_healthcheck(url: str, *, failed: bool, opener: Callable[..., object] = urllib.request.urlopen) -> None:
    """Optional external dead-man's switch (e.g. healthchecks.io). Never raises."""

    if not url:
        return
    target = url.rstrip("/") + ("/fail" if failed else "")
    with contextlib.suppress(Exception):
        with opener(urllib.request.Request(target, method="POST"), timeout=15):
            pass


# --- restore --------------------------------------------------------------------

PRODUCTION_URL_ENV = ("DATABASE_URL", "HOSTED_DATABASE_URL", "BACKUP_DATABASE_URL")


def check_restore_target(
    target_url: str,
    confirm_name: str,
    *,
    env: Mapping[str, str] = os.environ,
) -> None:
    """Refuse any target that could be production or holds data."""

    try:
        name = _conninfo(target_url).get("dbname") or ""
    except Exception:  # noqa: BLE001
        raise BackupError("restore_target", "target URL is not a PostgreSQL URL") from None
    if not name or confirm_name != name:
        raise BackupError("restore_target", "--confirm-target must repeat the target database name")
    for variable in PRODUCTION_URL_ENV:
        known = (env.get(variable) or "").strip()
        if known and same_database(target_url, known):
            raise BackupError("restore_target", f"target matches {variable}")
    with connect(target_url) as connection:
        tables = table_counts(connection)
    if tables:
        raise BackupError("restore_target", f"target database is not empty ({len(tables)} tables)")


def run_restore(
    storage: S3Storage,
    *,
    manifest_key: str,
    identity_file: Path,
    target_url: str,
    confirm_name: str,
    tools: Tools = Tools(),
    runner: Runner = subprocess.run,
    env: Mapping[str, str] = os.environ,
    workroot: str | None = None,
) -> dict:
    check_restore_target(target_url, confirm_name, env=env)
    manifest = json.loads(storage.get_bytes(manifest_key))
    workdir = Path(tempfile.mkdtemp(prefix="findsooner-restore-", dir=workroot))
    try:
        encrypted = workdir / "backup.dump.age"
        if storage.download(manifest["object_key"], encrypted) != manifest["encrypted_sha256"]:
            raise BackupError("restore_download", "checksum differs from manifest")
        dump = workdir / "backup.dump"
        run_tool(
            runner,
            [tools.age, "-d", "-i", str(identity_file), "-o", str(dump), str(encrypted)],
            stage="decrypt",
        )
        if sha256_file(dump) != manifest["plaintext_sha256"]:
            raise BackupError("decrypt", "decrypted checksum differs from manifest")
        restore_archive(runner, tools, dump, target_url, stage="restore")
        verify_restored(
            target_url, manifest["table_counts"], manifest["alembic_revision"], stage="restore_verify"
        )
        return manifest
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def latest_manifest_key(storage: S3Storage) -> str:
    manifests = sorted(
        key for key in storage.list_keys(storage.config.prefix + "/") if _KEY_RE.search(key)
    )
    if not manifests:
        raise BackupError("restore", "no verified backups found")
    return manifests[-1]


# --- command line -----------------------------------------------------------------


def _guarded(action):
    """Run ``action``; report unexpected errors by class name only."""

    try:
        return action()
    except BackupError:
        raise
    except Exception as exc:  # noqa: BLE001 - never print connection details
        raise BackupError("unexpected", type(exc).__name__) from None


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup")
    monitor = commands.add_parser("monitor")
    monitor.add_argument("--max-age-hours", type=float, default=36)
    monitor.add_argument("--interval-hours", type=float, default=6)
    restore = commands.add_parser("restore")
    restore.add_argument("--manifest", help="manifest object key (default: latest)")
    restore.add_argument("--identity", required=True, type=Path, help="age private key file")
    restore.add_argument("--target", required=True, help="empty, non-production database URL")
    restore.add_argument("--confirm-target", required=True, help="repeat the target database name")
    args = parser.parse_args(argv)
    tools = Tools.from_env(env)

    if args.command == "backup":
        try:
            storage = S3Storage(storage_config_from_env(env))
            database_url = (env.get("BACKUP_DATABASE_URL") or "").strip()
            if not database_url:
                raise BackupError("config", "missing BACKUP_DATABASE_URL")
            result = _guarded(
                lambda: run_backup(database_url, storage, age_recipients_from_env(env), tools=tools)
            )
        except BackupError as exc:
            print(f"{PREFIX} status=failed stage={exc.stage} detail={exc.detail or 'none'}", flush=True)
            if not isinstance(exc, AlreadyRunning):
                send_alert(
                    "FindSooner backup FAILED",
                    f"The scheduled PostgreSQL backup failed at stage '{exc.stage}'"
                    f" ({exc.detail or 'no detail'}). The last verified backup is unchanged.",
                    env=env,
                )
                ping_healthcheck((env.get("BACKUP_HEALTHCHECK_URL") or "").strip(), failed=True)
            return 1
        print(
            f"{PREFIX} status=ok key={result.key} size_bytes={result.size_bytes} "
            f"sha256={result.sha256[:16]} tables={result.tables} rows={result.rows} "
            f"upload_verified=true restore_verified=true seconds={result.seconds:.0f}",
            flush=True,
        )
        ping_healthcheck((env.get("BACKUP_HEALTHCHECK_URL") or "").strip(), failed=False)
        return 0

    if args.command == "monitor":
        now = dt.datetime.now(dt.UTC)
        try:
            status = _guarded(
                lambda: backup_status(
                    S3Storage(storage_config_from_env(env)), now=now, max_age_hours=args.max_age_hours
                )
            )
        except BackupError as exc:
            print(f"{PREFIX}-STATUS status=unknown stage={exc.stage} detail={exc.detail or 'none'}", flush=True)
            if now.hour < args.interval_hours:
                send_alert(
                    "FindSooner backup monitor cannot read backups",
                    f"The backup monitor failed at stage '{exc.stage}'. Backup health is unknown.",
                    env=env,
                )
            return 1
        print(status.line(), flush=True)
        if status.status == "ok":
            return 0
        if should_alert(status, now=now, max_age_hours=args.max_age_hours, interval_hours=args.interval_hours):
            send_alert(
                "FindSooner backup is overdue",
                (
                    "No verified PostgreSQL backup exists."
                    if status.status == "missing"
                    else f"The newest verified backup finished {status.age_hours:.0f} hours ago "
                    f"(threshold {args.max_age_hours:.0f}). {status.retained} verified backups are retained."
                ),
                env=env,
            )
        return 1

    try:
        storage = S3Storage(storage_config_from_env(env))
        key = args.manifest or _guarded(lambda: latest_manifest_key(storage))
        manifest = _guarded(lambda: run_restore(
            storage,
            manifest_key=key,
            identity_file=args.identity,
            target_url=args.target,
            confirm_name=args.confirm_target,
            tools=tools,
            env=env,
        ))
    except BackupError as exc:
        print(f"{PREFIX}-RESTORE status=failed stage={exc.stage} detail={exc.detail or 'none'}", flush=True)
        return 1
    print(
        f"{PREFIX}-RESTORE status=ok manifest={key} tables={len(manifest['table_counts'])} "
        f"rows={sum(manifest['table_counts'].values())} alembic={manifest['alembic_revision']}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
