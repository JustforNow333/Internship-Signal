"""The process-environment layer moved out of `_legacy.py` into `env.py`.

These pin every `WATCHER_*` setting's name, default, accepted values, coercion,
and invalid-value behaviour, the dotenv parser, environment precedence, and the
import-time evaluation order that makes `.env` reach `DEFAULT_SEEN_DB_PATH`.
"""

import json
import os
import pathlib
import subprocess
import sys

import pytest

import watcher.config as config
from watcher.config import env

ROOT = pathlib.Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# WATCHER_ANALYSIS_CACHE_ENABLED
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, True),
        (True, True),
        (False, False),
        ("1", True),
        ("true", True),
        ("TRUE", True),
        (" yes ", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("off", False),
        ("", True),
        ("   ", True),
    ],
)
def test_analysis_cache_enabled_accepts_the_same_values(value, expected):
    assert env.analysis_cache_enabled(value) is expected


@pytest.mark.parametrize("value", ["bogus", "maybe", "2", 5, "truthy"])
def test_analysis_cache_enabled_rejects_everything_else(value):
    with pytest.raises(config.ConfigError, match="WATCHER_ANALYSIS_CACHE_ENABLED"):
        env.analysis_cache_enabled(value)


def test_analysis_cache_enabled_reads_its_variable_at_call_time(monkeypatch):
    monkeypatch.delenv("WATCHER_ANALYSIS_CACHE_ENABLED", raising=False)
    assert env.analysis_cache_enabled() is True
    monkeypatch.setenv("WATCHER_ANALYSIS_CACHE_ENABLED", "0")
    assert env.analysis_cache_enabled() is False
    # An explicit argument still wins over the environment.
    assert env.analysis_cache_enabled(True) is True


# --------------------------------------------------------------------------
# WATCHER_WORKDAY_MIN_INTERVAL_SECONDS
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0.5),
        ("", 0.5),
        # Only None and "" fall back; an explicit 0 is a real zero interval.
        (0, 0.0),
        ("0", 0.0),
        (0.5, 0.5),
        (1, 1.0),
        ("2", 2.0),
        ("10", 10.0),
        (True, 1.0),
    ],
)
def test_workday_min_interval_accepts_the_same_values(value, expected):
    assert env.workday_min_interval_seconds(value) == expected


@pytest.mark.parametrize("value", ["10.1", "-1", "abc", "nan", "inf", " "])
def test_workday_min_interval_rejects_out_of_range_and_non_numeric(value):
    with pytest.raises(config.ConfigError, match="WATCHER_WORKDAY_MIN_INTERVAL_SECONDS"):
        env.workday_min_interval_seconds(value)


def test_workday_min_interval_reads_its_variable_at_call_time(monkeypatch):
    monkeypatch.delenv("WATCHER_WORKDAY_MIN_INTERVAL_SECONDS", raising=False)
    assert env.workday_min_interval_seconds() == 0.5
    monkeypatch.setenv("WATCHER_WORKDAY_MIN_INTERVAL_SECONDS", "3")
    assert env.workday_min_interval_seconds() == 3.0


# --------------------------------------------------------------------------
# WATCHER_ANALYSIS_CACHE_PATH
# --------------------------------------------------------------------------

def test_analysis_cache_path_defaults_beside_the_seen_database(monkeypatch):
    monkeypatch.delenv("WATCHER_ANALYSIS_CACHE_PATH", raising=False)
    resolved = env.resolve_analysis_cache_path("/base/seen.sqlite")
    assert resolved == pathlib.Path("/base") / "analysis-cache.sqlite"
    assert env.DEFAULT_ANALYSIS_CACHE_FILENAME == "analysis-cache.sqlite"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_cache_path_falls_back_to_the_default(blank):
    assert env.resolve_analysis_cache_path("/base/seen.sqlite", blank) == (
        pathlib.Path("/base") / "analysis-cache.sqlite"
    )


def test_cache_path_is_stripped_and_env_is_read_at_call_time(monkeypatch):
    assert env.resolve_analysis_cache_path("/base/s.sqlite", " /x/c.sqlite ") == (
        pathlib.Path("/x/c.sqlite")
    )
    monkeypatch.setenv("WATCHER_ANALYSIS_CACHE_PATH", "/from/env.sqlite")
    assert env.resolve_analysis_cache_path("/base/s.sqlite") == pathlib.Path(
        "/from/env.sqlite"
    )


# --------------------------------------------------------------------------
# Collection concurrency environment
# --------------------------------------------------------------------------

CONCURRENCY_VARS = [
    "WATCHER_COLLECTION_MODE",
    "WATCHER_COLLECTION_MAX_WORKERS",
    "WATCHER_WORKDAY_MAX_CONCURRENCY",
    "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
]


@pytest.fixture
def clean_concurrency_env(monkeypatch):
    for name in CONCURRENCY_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_collection_concurrency_defaults_when_nothing_is_set(clean_concurrency_env):
    assert env.load_collection_concurrency().as_dict() == {
        "mode": "serial",
        "max_workers": 4,
        "workday_max_concurrency": 1,
        "per_origin_max_concurrency": 2,
    }


def test_collection_concurrency_reads_every_variable(clean_concurrency_env):
    clean_concurrency_env.setenv("WATCHER_COLLECTION_MODE", "concurrent")
    clean_concurrency_env.setenv("WATCHER_COLLECTION_MAX_WORKERS", "4")
    clean_concurrency_env.setenv("WATCHER_WORKDAY_MAX_CONCURRENCY", "1")
    clean_concurrency_env.setenv("WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY", "2")

    assert env.load_collection_concurrency().as_dict() == {
        "mode": "concurrent",
        "max_workers": 4,
        "workday_max_concurrency": 1,
        "per_origin_max_concurrency": 2,
    }


def test_explicit_arguments_win_over_the_environment(clean_concurrency_env):
    clean_concurrency_env.setenv("WATCHER_COLLECTION_MODE", "concurrent")
    assert env.load_collection_concurrency(mode="serial").mode == "serial"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_environment_values_fall_back_to_defaults(clean_concurrency_env, blank):
    for name in CONCURRENCY_VARS:
        clean_concurrency_env.setenv(name, blank)
    assert env.load_collection_concurrency().as_dict()["mode"] == "serial"


def test_invalid_environment_mode_is_rejected(clean_concurrency_env):
    clean_concurrency_env.setenv("WATCHER_COLLECTION_MODE", "parallel")
    with pytest.raises(config.ConfigError, match="WATCHER_COLLECTION_MODE"):
        env.load_collection_concurrency()


def test_invalid_environment_worker_count_is_rejected(clean_concurrency_env):
    clean_concurrency_env.setenv("WATCHER_COLLECTION_MAX_WORKERS", "0")
    with pytest.raises(config.ConfigError, match="WATCHER_COLLECTION_MAX_WORKERS"):
        env.load_collection_concurrency()


# --------------------------------------------------------------------------
# Coercion helpers
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "serial"), ("", "serial"), ("  ", "serial"),
     ("serial", "serial"), ("CONCURRENT", "concurrent"), (" concurrent ", "concurrent")],
)
def test_collection_mode_coercion_is_unchanged(value, expected):
    assert env._collection_mode_value(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(1, 1), ("3", 3), (" 3 ", 3), (3.0, 3), (5, 5)]
)
def test_bounded_int_accepts_the_same_values(value, expected):
    assert env._bounded_int(value, "LABEL", 1, 5) == expected


@pytest.mark.parametrize("value", [0, 6, True, False, 2.5, "abc", None, ""])
def test_bounded_int_rejects_the_same_values(value):
    with pytest.raises(config.ConfigError, match="LABEL must be an integer between 1 and 5"):
        env._bounded_int(value, "LABEL", 1, 5)


@pytest.mark.parametrize(
    ("preset", "explicit", "expected"),
    [
        (None, None, "fallback"),
        (None, "explicit", "explicit"),
        ("from_env", None, "from_env"),
        ("from_env", "explicit", "explicit"),
        ("", None, "fallback"),
        ("   ", None, "fallback"),
    ],
)
def test_env_or_default_precedence_is_unchanged(monkeypatch, preset, explicit, expected):
    monkeypatch.delenv("WATCHER_TEST_ONLY", raising=False)
    if preset is not None:
        monkeypatch.setenv("WATCHER_TEST_ONLY", preset)
    assert env._env_or_default("WATCHER_TEST_ONLY", explicit, "fallback") == expected


# --------------------------------------------------------------------------
# dotenv parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("KEY=value", ("KEY", "value")),
        ("  KEY = value  ", ("KEY", "value")),
        ("export KEY=value", ("KEY", "value")),
        ('$env:KEY = "value"', ("KEY", "value")),
        ("$env:KEY='value'", ("KEY", "value")),
        ('KEY="quoted value"', ("KEY", "quoted value")),
        ("KEY=value # trailing comment", ("KEY", "value")),
        ("KEY=value#nospace", ("KEY", "value#nospace")),
        ('KEY="has # hash"', ("KEY", "has # hash")),
        ("KEY=a=b", ("KEY", "a=b")),
        ("KEY=", ("KEY", "")),
        ("# whole line comment", None),
        ("", None),
        ("   ", None),
        ("not an assignment", None),
        ("1BAD=value", None),
    ],
)
def test_dotenv_line_parsing_is_unchanged(line, expected):
    assert env._parse_env_assignment(line) == expected


def test_load_dotenv_never_overrides_the_process_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("PRESET=from_dotenv\nFRESH=from_dotenv\n", encoding="utf-8")
    monkeypatch.setenv("PRESET", "from_shell")
    monkeypatch.delenv("FRESH", raising=False)

    env.load_dotenv(dotenv)

    assert os.environ["PRESET"] == "from_shell"
    assert os.environ["FRESH"] == "from_dotenv"
    monkeypatch.delenv("FRESH", raising=False)


def test_load_dotenv_on_a_missing_file_is_a_no_op(tmp_path):
    env.load_dotenv(tmp_path / "absent.env")


def test_default_dotenv_path_is_the_repository_env_file():
    assert env.DEFAULT_DOTENV_PATH == ROOT / ".env"
    assert env.REPO_ROOT == ROOT
    assert env.WATCHER_DIR == ROOT / "watcher"


# --------------------------------------------------------------------------
# Import-time evaluation order
# --------------------------------------------------------------------------

def run_in_clean_interpreter(code, dotenv_body=None, preset=None):
    """Import watcher.config in a fresh process, optionally with a .env."""

    dotenv = ROOT / ".env"
    assert not dotenv.exists(), "refusing to overwrite a real .env"
    if dotenv_body is not None:
        dotenv.write_text(dotenv_body, encoding="utf-8")
    child_env = {
        k: v for k, v in os.environ.items() if not k.startswith("WATCHER_")
    }
    child_env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "backend")])
    for key, value in (preset or {}).items():
        child_env[key] = value
    try:
        return subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=str(ROOT), env=child_env,
        )
    finally:
        if dotenv_body is not None:
            dotenv.unlink(missing_ok=True)


PRINT_SEEN_DB = (
    "import json, watcher.config as c;"
    "print(json.dumps({'seen': str(c.DEFAULT_SEEN_DB_PATH),"
    " 'cache': c.analysis_cache_enabled()}))"
)


def test_dotenv_is_loaded_before_the_seen_db_default_is_evaluated():
    """`.env` must still reach DEFAULT_SEEN_DB_PATH, which is import-time."""

    result = run_in_clean_interpreter(
        PRINT_SEEN_DB,
        dotenv_body="WATCHER_SEEN_DB=/tmp/from-dotenv.sqlite\n"
        "WATCHER_ANALYSIS_CACHE_ENABLED=0\n",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert pathlib.Path(payload["seen"]) == pathlib.Path("/tmp/from-dotenv.sqlite")
    assert payload["cache"] is False


def test_the_process_environment_still_wins_over_dotenv_at_import_time():
    result = run_in_clean_interpreter(
        PRINT_SEEN_DB,
        dotenv_body="WATCHER_SEEN_DB=/tmp/from-dotenv.sqlite\n",
        preset={"WATCHER_SEEN_DB": "/tmp/from-shell.sqlite"},
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert pathlib.Path(payload["seen"]) == pathlib.Path("/tmp/from-shell.sqlite")


def test_without_a_dotenv_the_seen_db_default_sits_beside_the_watcher_package():
    result = run_in_clean_interpreter(PRINT_SEEN_DB)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert pathlib.Path(payload["seen"]) == ROOT / "watcher" / "seen.sqlite"


def test_importing_env_alone_already_runs_load_dotenv():
    """load_dotenv() is an import-time side effect of env.py, not of the facade."""

    result = run_in_clean_interpreter(
        "import os, watcher.config.env; print(os.environ.get('DOTENV_MARKER'))",
        dotenv_body="DOTENV_MARKER=applied\n",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "applied"


@pytest.mark.parametrize(
    "first",
    [
        "watcher.config",
        "watcher.config.env",
        "watcher.config.models",
        "watcher.config._legacy",
        "watcher.sources.registry",
        "watcher.sources.workday",
    ],
)
def test_no_import_cycle_whichever_module_loads_first(first):
    result = run_in_clean_interpreter(f"import {first}")
    assert result.returncode == 0, result.stderr
