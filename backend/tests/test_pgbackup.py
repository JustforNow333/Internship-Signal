"""Backup, verification, monitoring, and restore safeguards (ops/backup).

Integration tests use disposable PostgreSQL databases created from
HOSTED_TEST_DATABASE_URL, the PostgreSQL client and server tools in PG_BIN
(matching that server's major version), ``age`` for encryption, and an
in-process fake S3 server. No production data or real storage is used.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

import psycopg
import pytest
from psycopg import sql

from ops.backup import pgbackup
from ops.backup.pgbackup import (
    AlreadyRunning,
    BackupError,
    BackupStatus,
    ObjectExists,
    S3Storage,
    StorageConfig,
    Tools,
    backup_status,
    check_restore_target,
    run_backup,
    run_restore,
    should_alert,
    sign_v4,
)

NOW = dt.datetime(2026, 10, 18, 3, 45, tzinfo=dt.UTC)


# --- fake S3 ----------------------------------------------------------------------


class FakeS3:
    """Path-style S3 subset: create-only PUT, GET, HEAD, paginated LIST."""

    page_size = 2

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.methods: list[str] = []
        self.fail_put = False
        self.corrupt_get = False
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _key(self) -> tuple[str, dict[str, str]]:
                parsed = urllib.parse.urlsplit(self.path)
                parts = parsed.path.split("/", 2)
                key = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
                return key, dict(urllib.parse.parse_qsl(parsed.query))

            def _signed(self) -> bool:
                return self.headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256 ")

            def _send(self, status: int, body: bytes = b"", headers=None) -> None:
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_PUT(self) -> None:
                fake.methods.append("PUT")
                key, _ = self._key()
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if not self._signed():
                    return self._send(403)
                if self.headers.get("x-amz-content-sha256") != hashlib.sha256(body).hexdigest():
                    return self._send(400)
                if fake.fail_put:
                    return self._send(500)
                with fake.lock:
                    if self.headers.get("If-None-Match") == "*" and key in fake.objects:
                        return self._send(412)
                    fake.objects[key] = body
                self._send(200)

            def do_HEAD(self) -> None:
                fake.methods.append("HEAD")
                key, _ = self._key()
                if key not in fake.objects:
                    return self._send(404)
                self.send_response(200)
                self.send_header("Content-Length", str(len(fake.objects[key])))
                self.end_headers()

            def do_GET(self) -> None:
                fake.methods.append("GET")
                key, query = self._key()
                if not self._signed():
                    return self._send(403)
                if not key:
                    return self._list(query)
                if key not in fake.objects:
                    return self._send(404)
                body = fake.objects[key]
                if fake.corrupt_get and key.endswith(".dump.age"):
                    body = bytes([body[0] ^ 1]) + body[1:]
                self._send(200, body)

            def do_DELETE(self) -> None:
                fake.methods.append("DELETE")
                self._send(405)

            def _list(self, query: dict[str, str]) -> None:
                keys = sorted(key for key in fake.objects if key.startswith(query.get("prefix", "")))
                start = int(query.get("continuation-token") or 0)
                page = keys[start : start + fake.page_size]
                more = start + fake.page_size < len(keys)
                body = (
                    '<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                    + "".join(f"<Contents><Key>{escape(key)}</Key></Contents>" for key in page)
                    + f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>"
                    + (f"<NextContinuationToken>{start + fake.page_size}</NextContinuationToken>" if more else "")
                    + "</ListBucketResult>"
                ).encode()
                self._send(200, body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def storage(self, prefix: str = "findsooner/postgres") -> S3Storage:
        host, port = self.server.server_address
        return S3Storage(
            StorageConfig(
                endpoint=f"http://{host}:{port}",
                region="auto",
                bucket="backups",
                access_key_id="test-access",
                secret_access_key="test-secret",
                prefix=prefix,
            )
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def s3():
    fake = FakeS3()
    yield fake
    fake.close()


# --- PostgreSQL fixtures ------------------------------------------------------------


def _admin_url() -> str:
    url = os.getenv("HOSTED_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("HOSTED_TEST_DATABASE_URL is required for backup tests")
    return re.sub(r"^postgres(ql)?\+\w+://", "postgresql://", url)


def _with_db(url: str, name: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(parsed._replace(path=f"/{name}"))


@pytest.fixture
def make_database():
    admin = _admin_url()
    created: list[str] = []

    def make(prefix: str = "backup_test") -> str:
        name = f"{prefix}_{uuid.uuid4().hex[:12]}"
        with psycopg.connect(admin, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        created.append(name)
        return _with_db(admin, name)

    yield make
    with psycopg.connect(admin, autocommit=True) as connection:
        for name in created:
            connection.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture
def source_database(make_database) -> str:
    url = make_database("backup_src")
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('20261011_0008')")
        connection.execute(
            "CREATE TABLE hosted_users (id serial PRIMARY KEY, email text NOT NULL UNIQUE)"
        )
        connection.execute(
            "INSERT INTO hosted_users (email) SELECT 'user' || g || '@example.test' "
            "FROM generate_series(1, 25) g"
        )
        connection.execute(
            "CREATE TABLE hosted_user_job_matches (id bigserial PRIMARY KEY, "
            "user_id int REFERENCES hosted_users(id), payload jsonb)"
        )
        connection.execute(
            "INSERT INTO hosted_user_job_matches (user_id, payload) "
            "SELECT 1 + g % 25, jsonb_build_object('n', g) FROM generate_series(1, 400) g"
        )
        connection.execute("CREATE TABLE hosted_password_reset_tokens (id serial PRIMARY KEY)")
    return url


def _tools() -> Tools:
    bin_dir = os.getenv("PG_BIN", "").strip()
    pg_dump = shutil.which("pg_dump", path=bin_dir or None)
    if not pg_dump:
        pytest.skip("PostgreSQL client and server tools (PG_BIN) are required for backup tests")
    return Tools(bin_dir=bin_dir, age=os.getenv("AGE_BIN", "age"))


@pytest.fixture
def tools() -> Tools:
    return _tools()


@pytest.fixture
def age_key(tmp_path, tools) -> tuple[Path, str]:
    keygen = shutil.which("age-keygen")
    if not keygen or not shutil.which(tools.age):
        pytest.skip("age and age-keygen are required for encrypted backup tests")
    identity = tmp_path / "identity.txt"
    subprocess.run([keygen, "-o", str(identity)], check=True, capture_output=True)
    public = re.search(r"public key: (age1\w+)", identity.read_text()).group(1)
    return identity, public


def leftovers(root: Path) -> list[Path]:
    return [path for path in root.iterdir() if path.name.startswith("findsooner-")]


# --- signing ------------------------------------------------------------------------


def test_sigv4_matches_the_aws_reference_example() -> None:
    # "GET Object" example from the AWS Signature Version 4 documentation.
    headers = sign_v4(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        query={},
        headers={"Range": "bytes=0-9"},
        payload_hash=hashlib.sha256(b"").hexdigest(),
        region="us-east-1",
        access_key_id="AKIAIOSFODNN7EXAMPLE",
        secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        now=dt.datetime(2013, 5, 24, tzinfo=dt.UTC),
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


# --- backup and restore ----------------------------------------------------------------


def test_backup_is_verified_uploaded_and_restorable_into_an_isolated_database(
    s3, source_database, make_database, tools, age_key, tmp_path
) -> None:
    identity, public = age_key
    storage = s3.storage()
    result = run_backup(source_database, storage, [public], tools=tools, workroot=str(tmp_path))

    assert re.fullmatch(
        r"findsooner/postgres/\d{4}/\d{2}/findsooner-\d{8}T\d{6}Z-[0-9a-f]{8}\.dump\.age", result.key
    )
    assert set(s3.objects) == {result.key, result.manifest_key}
    assert hashlib.sha256(s3.objects[result.key]).hexdigest() == result.sha256
    assert b"user1@example.test" not in s3.objects[result.key]  # encrypted
    manifest = json.loads(s3.objects[result.manifest_key])
    assert manifest["upload_verified"] and manifest["restore_verified"]
    assert manifest["alembic_revision"] == "20261011_0008"
    assert manifest["table_counts"] == {
        "public.alembic_version": 1,
        "public.hosted_password_reset_tokens": 0,
        "public.hosted_user_job_matches": 400,
        "public.hosted_users": 25,
    }
    assert result.rows == 426 and result.tables == 4
    assert leftovers(tmp_path) == []
    assert "DELETE" not in s3.methods

    status = backup_status(storage, now=dt.datetime.now(dt.UTC))
    assert status.status == "ok" and status.retained == 1 and status.unverified_newer == 0

    target = make_database("backup_restore")
    restored = run_restore(
        storage,
        manifest_key=result.manifest_key,
        identity_file=identity,
        target_url=target,
        confirm_name=target.rsplit("/", 1)[1],
        tools=tools,
        env={},
        workroot=str(tmp_path),
    )
    assert restored["table_counts"] == manifest["table_counts"]
    with psycopg.connect(target) as connection:
        assert connection.execute("SELECT count(*) FROM hosted_user_job_matches").fetchone()[0] == 400
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "20261011_0008"
    assert leftovers(tmp_path) == []


def failing_runner(tool: str, *, corrupt: bool = False):
    """Run tools for real, but make ``tool`` fail (or corrupt its output)."""

    def run(args, **kwargs):
        if Path(args[0]).name.split(".")[0] == tool:
            if corrupt:
                result = subprocess.run(args, **kwargs)
                dump = Path(next(arg for arg in args if arg.startswith("--file=")).split("=", 1)[1])
                dump.write_bytes(dump.read_bytes()[: dump.stat().st_size // 3])
                return result
            return subprocess.CompletedProcess(args, 1, "", "simulated failure")
        return subprocess.run(args, **kwargs)

    return run


def test_pg_dump_failure_uploads_nothing(s3, source_database, tools, tmp_path) -> None:
    with pytest.raises(BackupError) as raised:
        run_backup(
            source_database, s3.storage(), ["age1unused"], tools=tools,
            runner=failing_runner("pg_dump"), workroot=str(tmp_path),
        )
    assert raised.value.stage == "pg_dump"
    assert s3.objects == {} and leftovers(tmp_path) == []


def test_a_truncated_archive_is_rejected_before_upload(s3, source_database, tools, tmp_path) -> None:
    with pytest.raises(BackupError) as raised:
        run_backup(
            source_database, s3.storage(), ["age1unused"], tools=tools,
            runner=failing_runner("pg_dump", corrupt=True), workroot=str(tmp_path),
        )
    assert raised.value.stage in {"archive_list", "restore_check"}
    assert s3.objects == {} and leftovers(tmp_path) == []


def test_upload_failure_never_writes_a_manifest(s3, source_database, tools, age_key, tmp_path) -> None:
    s3.fail_put = True
    with pytest.raises(BackupError) as raised:
        run_backup(source_database, s3.storage(), [age_key[1]], tools=tools, workroot=str(tmp_path))
    assert raised.value.stage == "upload"
    assert s3.objects == {} and leftovers(tmp_path) == []


def test_checksum_mismatch_leaves_the_backup_unverified(
    s3, source_database, tools, age_key, tmp_path
) -> None:
    s3.corrupt_get = True
    storage = s3.storage()
    with pytest.raises(BackupError) as raised:
        run_backup(source_database, storage, [age_key[1]], tools=tools, workroot=str(tmp_path))
    assert raised.value.stage == "upload_verify"
    # The object stays (the writer cannot delete) but never counts as a backup.
    assert all(key.endswith(".dump.age") for key in s3.objects)
    status = backup_status(storage, now=NOW)
    assert status.status == "missing" and status.unverified_newer == 1
    assert leftovers(tmp_path) == []


def test_a_concurrent_backup_refuses_to_run(s3, source_database, tools, tmp_path) -> None:
    with psycopg.connect(source_database, autocommit=True) as holder:
        assert holder.execute(pgbackup.LOCK_SQL).fetchone()[0]
        with pytest.raises(AlreadyRunning):
            run_backup(source_database, s3.storage(), ["age1unused"], tools=tools, workroot=str(tmp_path))
    assert s3.objects == {} and leftovers(tmp_path) == []


def test_restore_refuses_production_mismatched_or_non_empty_targets(
    source_database, make_database
) -> None:
    target = make_database("backup_target")
    name = target.rsplit("/", 1)[1]
    with pytest.raises(BackupError, match="repeat the target"):
        check_restore_target(target, "railway", env={})
    for variable in ("DATABASE_URL", "HOSTED_DATABASE_URL", "BACKUP_DATABASE_URL"):
        with pytest.raises(BackupError, match=variable):
            check_restore_target(target, name, env={variable: target.replace("127.0.0.1", "localhost")})
    with pytest.raises(BackupError, match="not empty"):
        check_restore_target(source_database, source_database.rsplit("/", 1)[1], env={})
    check_restore_target(target, name, env={"DATABASE_URL": source_database})


# --- storage safety ---------------------------------------------------------------------


def test_objects_are_create_only_and_the_client_cannot_delete(s3) -> None:
    storage = s3.storage()
    storage.put_new("findsooner/postgres/a.dump.age", b"first", content_type="application/octet-stream")
    with pytest.raises(ObjectExists):
        storage.put_new("findsooner/postgres/a.dump.age", b"second", content_type="application/octet-stream")
    assert s3.objects["findsooner/postgres/a.dump.age"] == b"first"
    assert not any(hasattr(storage, name) for name in ("delete", "delete_object", "remove"))
    assert "DELETE" not in s3.methods


# --- monitoring --------------------------------------------------------------------------


def seed_manifest(s3: FakeS3, finished: dt.datetime, *, verified: bool = True, with_object: bool = True) -> str:
    stem = f"findsooner/postgres/{finished:%Y/%m}/findsooner-{finished:%Y%m%dT%H%M%SZ}-0000abcd"
    if with_object:
        s3.objects[stem + ".dump.age"] = b"encrypted"
    s3.objects[stem + ".manifest.json"] = json.dumps(
        {
            "finished_at": finished.isoformat(),
            "encrypted_size_bytes": 9,
            "upload_verified": verified,
            "restore_verified": verified,
        }
    ).encode()
    return stem


def test_monitor_reports_the_latest_verified_backup_across_pages(s3) -> None:
    for days in range(5):
        seed_manifest(s3, NOW - dt.timedelta(days=days, hours=2))
    seed_manifest(s3, NOW - dt.timedelta(days=60))  # beyond retention
    seed_manifest(s3, NOW - dt.timedelta(hours=1), with_object=False)  # object already gone
    status = backup_status(s3.storage(), now=NOW)

    assert status.status == "ok"
    assert status.retained == 5
    assert round(status.age_hours) == 2
    assert status.size_bytes == 9
    line = status.line()
    assert line.startswith("HOSTED-BACKUP-STATUS status=ok ") and "retained=5" in line


@pytest.mark.parametrize(
    ("age_hours", "expected_status"),
    [(12, "ok"), (37, "stale"), (100, "stale")],
)
def test_monitor_classifies_backup_age(s3, age_hours, expected_status) -> None:
    seed_manifest(s3, NOW - dt.timedelta(hours=age_hours))
    assert backup_status(s3.storage(), now=NOW).status == expected_status


def test_monitor_treats_an_unverified_manifest_as_stale(s3) -> None:
    seed_manifest(s3, NOW - dt.timedelta(hours=2), verified=False)
    assert backup_status(s3.storage(), now=NOW).status == "stale"


def test_missing_and_overdue_backups_alert_about_once_a_day() -> None:
    def status(age):
        return BackupStatus(status="stale", retained=3, age_hours=age)

    # Runs every 6 hours: alert once per 24-hour period of the outage.
    alerts = [age for age in range(36, 36 + 72, 6) if should_alert(status(age + 0.5), now=NOW)]
    assert alerts == [36, 60, 84]
    assert not should_alert(BackupStatus(status="ok", retained=3, age_hours=2), now=NOW)
    missing = BackupStatus(status="missing", retained=0)
    assert should_alert(missing, now=NOW.replace(hour=2))
    assert not should_alert(missing, now=NOW.replace(hour=14))


def test_alerts_are_sent_through_resend_without_leaking_secrets() -> None:
    sent = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(request, timeout):
        sent.append(request)
        return Response()

    env = {
        "HOSTED_RESEND_API_KEY": "re_secret",
        "HOSTED_RESEND_FROM_EMAIL": "alerts@example.test",
        "BACKUP_ALERT_EMAIL": "operator@example.test",
    }
    assert pgbackup.send_alert("subject", "body", env=env, opener=opener)
    payload = json.loads(sent[0].data)
    assert payload == {
        "from": "alerts@example.test",
        "to": ["operator@example.test"],
        "subject": "subject",
        "text": "body",
    }
    assert not pgbackup.send_alert("subject", "body", env={}, opener=opener)


def test_monitor_command_fails_when_no_backup_exists(s3, capsys, monkeypatch) -> None:
    storage = s3.storage()
    monkeypatch.setattr(pgbackup, "storage_config_from_env", lambda env: storage.config)
    assert pgbackup.main(["monitor"], env={}) == 1
    output = capsys.readouterr().out
    assert "HOSTED-BACKUP-STATUS status=missing" in output
    assert "test-secret" not in output


def test_backup_command_reports_failures_without_connection_details(capsys) -> None:
    env = {
        "BACKUP_S3_ENDPOINT": "https://storage.example.test",
        "BACKUP_S3_REGION": "auto",
        "BACKUP_S3_BUCKET": "backups",
        "BACKUP_S3_ACCESS_KEY_ID": "key",
        "BACKUP_S3_SECRET_ACCESS_KEY": "very-secret",
        "BACKUP_AGE_RECIPIENTS": "age1example",
        "BACKUP_DATABASE_URL": "postgresql://user:hunter2@127.0.0.1:1/none",
    }
    assert pgbackup.main(["backup"], env=env) == 1
    output = capsys.readouterr().out
    assert "HOSTED-BACKUP status=failed stage=unexpected" in output
    for secret in ("hunter2", "very-secret", "127.0.0.1"):
        assert secret not in output


def test_configuration_requires_https_and_every_credential() -> None:
    with pytest.raises(BackupError, match="BACKUP_S3_BUCKET"):
        pgbackup.storage_config_from_env({"BACKUP_S3_ENDPOINT": "https://x"})
    env = {
        "BACKUP_S3_ENDPOINT": "http://insecure.example",
        "BACKUP_S3_REGION": "auto",
        "BACKUP_S3_BUCKET": "b",
        "BACKUP_S3_ACCESS_KEY_ID": "k",
        "BACKUP_S3_SECRET_ACCESS_KEY": "s",
    }
    with pytest.raises(BackupError, match="https"):
        pgbackup.storage_config_from_env(env)
    with pytest.raises(BackupError, match="age public keys"):
        pgbackup.age_recipients_from_env({"BACKUP_AGE_RECIPIENTS": "not-a-key"})
