"""The legacy CSV-analysis API is served only when explicitly enabled.

Each case imports ``app.main`` in a fresh interpreter so it sees exactly the
environment under test, whatever the developer's shell sets.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.main import legacy_api_enabled

REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_PATHS = {
    "/api/ingest",
    "/api/sample",
    "/api/datasets/{dataset_id}/jobs",
    "/api/datasets/{dataset_id}/jobs/{job_id}",
    "/api/datasets/{dataset_id}/summary",
    "/api/datasets/{dataset_id}/ask",
    "/api/profile",
}
HOSTED_PATHS = {
    "/api/health",
    "/api/auth/signup",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/resend-verification",
    "/api/auth/verify-email",
    "/api/me",
    "/api/companies",
    "/api/preferences",
    "/api/watchlist",
    "/api/matches",
    "/api/matches/{match_id}",
    "/api/company-requests",
}

PROBE = r"""
import json
from fastapi.testclient import TestClient
from app import store
from app.main import app

client = TestClient(app)
big_csv = "Company,Title\n" + "x" * (11 * 1024 * 1024)
result = {
    "paths": sorted(app.openapi()["paths"]),
    "sample": client.get("/api/sample").status_code,
    "profile": client.get("/api/profile").status_code,
    "jobs": client.get("/api/datasets/abc/jobs").status_code,
    "ask": client.post("/api/datasets/abc/ask", json={"question": "q"}).status_code,
    "ingest_big": client.post(
        "/api/ingest", content=big_csv, headers={"Content-Type": "text/plain"}
    ).status_code,
    "preferences_big": client.put(
        "/api/preferences",
        content=json.dumps({"pad": "x" * (65 * 1024)}),
        headers={"Content-Type": "application/json"},
    ).status_code,
    "health": client.get("/api/health").status_code,
    "stored_datasets": len(store._DATASETS),
}
print("PROBE" + json.dumps(result))
"""


def _probe(legacy_value: str | None) -> subprocess.CompletedProcess:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"LEGACY_API_ENABLED", "HOSTED_DATABASE_URL", "DATABASE_URL"}
    }
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "backend")])
    if legacy_value is not None:
        env["LEGACY_API_ENABLED"] = legacy_value
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def _result(legacy_value: str | None) -> dict:
    completed = _probe(legacy_value)
    assert completed.returncode == 0, completed.stderr[-2000:]
    line = next(l for l in completed.stdout.splitlines() if l.startswith("PROBE"))
    return json.loads(line[len("PROBE") :])


@pytest.mark.parametrize("value", [None, "", "  ", "false", "0", "off", "No"])
def test_legacy_api_disabled_unless_explicitly_enabled(monkeypatch, value) -> None:
    if value is None:
        monkeypatch.delenv("LEGACY_API_ENABLED", raising=False)
    else:
        monkeypatch.setenv("LEGACY_API_ENABLED", value)
    assert legacy_api_enabled() is False


@pytest.mark.parametrize("value", ["true", "1", "YES", " on "])
def test_legacy_api_enabled_values(monkeypatch, value) -> None:
    monkeypatch.setenv("LEGACY_API_ENABLED", value)
    assert legacy_api_enabled() is True


def test_unrecognized_value_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("LEGACY_API_ENABLED", "enabled")
    with pytest.raises(ValueError, match="LEGACY_API_ENABLED"):
        legacy_api_enabled()


def test_default_deployment_registers_no_legacy_routes() -> None:
    result = _result(None)
    paths = set(result["paths"])
    assert not paths & LEGACY_PATHS
    assert HOSTED_PATHS <= paths
    for name in ("sample", "profile", "jobs", "ask", "ingest_big"):
        assert result[name] == 404, (name, result[name])
    # Nothing was parsed or retained, and the hosted size guard still applies.
    assert result["stored_datasets"] == 0
    assert result["preferences_big"] == 413
    assert result["health"] == 200


def test_enabled_local_mode_serves_the_legacy_api() -> None:
    result = _result("true")
    paths = set(result["paths"])
    assert LEGACY_PATHS <= paths
    assert HOSTED_PATHS <= paths
    assert result["sample"] == 200
    assert result["profile"] == 200
    assert result["jobs"] == 404  # unknown dataset id, served by the legacy route
    assert result["ingest_big"] == 413  # the legacy 10 MiB limit still holds
    assert result["preferences_big"] == 413
    assert result["stored_datasets"] == 1


def test_invalid_value_fails_startup() -> None:
    completed = _probe("enabled")
    assert completed.returncode != 0
    assert "LEGACY_API_ENABLED must be true or false" in completed.stderr
