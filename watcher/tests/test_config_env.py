"""Environment ownership, compatibility, and import-order regression tests."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import watcher.config as config


ROOT = Path(__file__).resolve().parents[2]


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
    from watcher.config import env

    assert env.analysis_cache_enabled(value) is expected


@pytest.mark.parametrize("value", ["bogus", "maybe", "2", 5, "truthy"])
def test_analysis_cache_enabled_rejects_everything_else(value):
    from watcher.config import env

    with pytest.raises(config.ConfigError, match="WATCHER_ANALYSIS_CACHE_ENABLED"):
        env.analysis_cache_enabled(value)


def test_analysis_cache_enabled_reads_environment_at_call_time(monkeypatch):
    from watcher.config import env

    monkeypatch.delenv("WATCHER_ANALYSIS_CACHE_ENABLED", raising=False)
    assert env.analysis_cache_enabled() is True
    monkeypatch.setenv("WATCHER_ANALYSIS_CACHE_ENABLED", "0")
    assert env.analysis_cache_enabled() is False
    assert env.analysis_cache_enabled(True) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, 0.5),
        ("", 0.5),
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
    from watcher.config import env

    assert env.workday_min_interval_seconds(value) == expected


@pytest.mark.parametrize("value", ["10.1", "-1", "abc", "nan", "inf", " "])
def test_workday_min_interval_rejects_the_same_values(value):
    from watcher.config import env

    with pytest.raises(config.ConfigError, match="WATCHER_WORKDAY_MIN_INTERVAL_SECONDS"):
        env.workday_min_interval_seconds(value)


def test_environment_paths_and_cache_resolution_are_unchanged(monkeypatch):
    from watcher.config import env

    monkeypatch.delenv("WATCHER_ANALYSIS_CACHE_PATH", raising=False)
    assert env.WATCHER_DIR == ROOT / "watcher"
    assert env.REPO_ROOT == ROOT
    assert env.DEFAULT_DOTENV_PATH == ROOT / ".env"
    assert env.resolve_analysis_cache_path("/base/seen.sqlite") == (
        Path("/base") / "analysis-cache.sqlite"
    )
    assert env.resolve_analysis_cache_path("/base/seen.sqlite", "   ") == (
        Path("/base") / "analysis-cache.sqlite"
    )
    assert env.resolve_analysis_cache_path("/base/seen.sqlite", " /x/cache.sqlite ") == Path(
        "/x/cache.sqlite"
    )


CONCURRENCY_VARS = (
    "WATCHER_COLLECTION_MODE",
    "WATCHER_COLLECTION_MAX_WORKERS",
    "WATCHER_WORKDAY_MAX_CONCURRENCY",
    "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
)


@pytest.fixture
def clean_concurrency_env(monkeypatch):
    for name in CONCURRENCY_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_collection_concurrency_defaults_and_environment_are_unchanged(
    clean_concurrency_env,
):
    from watcher.config import env

    assert env.load_collection_concurrency().as_dict() == {
        "mode": "serial",
        "max_workers": 4,
        "workday_max_concurrency": 1,
        "per_origin_max_concurrency": 2,
    }
    clean_concurrency_env.setenv("WATCHER_COLLECTION_MODE", "concurrent")
    clean_concurrency_env.setenv("WATCHER_COLLECTION_MAX_WORKERS", "8")
    clean_concurrency_env.setenv("WATCHER_WORKDAY_MAX_CONCURRENCY", "2")
    clean_concurrency_env.setenv(
        "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY", "3"
    )
    assert env.load_collection_concurrency().as_dict() == {
        "mode": "concurrent",
        "max_workers": 8,
        "workday_max_concurrency": 2,
        "per_origin_max_concurrency": 3,
    }
    assert env.load_collection_concurrency(mode="serial").mode == "serial"


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("WATCHER_COLLECTION_MODE", "parallel", "WATCHER_COLLECTION_MODE"),
        ("WATCHER_COLLECTION_MAX_WORKERS", "0", "WATCHER_COLLECTION_MAX_WORKERS"),
        ("WATCHER_WORKDAY_MAX_CONCURRENCY", "6", "WATCHER_WORKDAY_MAX_CONCURRENCY"),
    ],
)
def test_invalid_collection_environment_still_fails(
    clean_concurrency_env, variable, value, message
):
    from watcher.config import env

    clean_concurrency_env.setenv(variable, value)
    with pytest.raises(config.ConfigError, match=message):
        env.load_collection_concurrency()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "serial"),
        ("", "serial"),
        ("  ", "serial"),
        ("serial", "serial"),
        ("CONCURRENT", "concurrent"),
        (" concurrent ", "concurrent"),
    ],
)
def test_collection_mode_coercion_is_unchanged(value, expected):
    from watcher.config import env

    assert env._collection_mode_value(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1), ("3", 3), (" 3 ", 3), (3.0, 3), (5, 5)],
)
def test_bounded_int_accepts_the_same_values(value, expected):
    from watcher.config import env

    assert env._bounded_int(value, "LABEL", 1, 5) == expected


@pytest.mark.parametrize("value", [0, 6, True, False, 2.5, "abc", None, ""])
def test_bounded_int_rejects_the_same_values(value):
    from watcher.config import env

    with pytest.raises(config.ConfigError, match="LABEL must be an integer between 1 and 5"):
        env._bounded_int(value, "LABEL", 1, 5)


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
        ("not an assignment", None),
        ("1BAD=value", None),
    ],
)
def test_dotenv_line_parsing_is_unchanged(line, expected):
    from watcher.config import env

    assert env._parse_env_assignment(line) == expected


def test_load_dotenv_preserves_process_environment_precedence(tmp_path, monkeypatch):
    from watcher.config import env

    dotenv = tmp_path / ".env"
    dotenv.write_text("PRESET=from-file\nFRESH=from-file\n", encoding="utf-8")
    monkeypatch.setenv("PRESET", "from-process")
    monkeypatch.delenv("FRESH", raising=False)

    env.load_dotenv(dotenv)

    assert os.environ["PRESET"] == "from-process"
    assert os.environ["FRESH"] == "from-file"


def _clean_import(code: str, *, dotenv_body: str | None = None, preset=None):
    """Run an import with a virtual repository .env in a clean process."""

    setup = ""
    if dotenv_body is not None:
        setup = f"""
from pathlib import Path
_dotenv = Path({str(ROOT / '.env')!r})
_original_exists = Path.exists
_original_read_text = Path.read_text
def _exists(self):
    return True if self == _dotenv else _original_exists(self)
def _read_text(self, *args, **kwargs):
    return {dotenv_body!r} if self == _dotenv else _original_read_text(self, *args, **kwargs)
Path.exists = _exists
Path.read_text = _read_text
"""
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("WATCHER_")}
    child_env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "backend")))
    child_env.update(preset or {})
    return subprocess.run(
        [sys.executable, "-c", setup + code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=child_env,
    )


PRINT_SETTINGS = (
    "import json, watcher.config as c;"
    "print(json.dumps({'seen': str(c.DEFAULT_SEEN_DB_PATH), "
    "'cache': c.analysis_cache_enabled()}))"
)


def test_dotenv_loads_before_environment_derived_defaults_are_evaluated():
    result = _clean_import(
        PRINT_SETTINGS,
        dotenv_body=(
            "WATCHER_SEEN_DB=from-dotenv.sqlite\n"
            "WATCHER_ANALYSIS_CACHE_ENABLED=0\n"
        ),
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"seen": "from-dotenv.sqlite", "cache": False}


def test_process_environment_wins_over_dotenv_during_import():
    result = _clean_import(
        PRINT_SETTINGS,
        dotenv_body="WATCHER_SEEN_DB=from-dotenv.sqlite\n",
        preset={"WATCHER_SEEN_DB": "from-process.sqlite"},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["seen"] == "from-process.sqlite"


def test_repeated_config_import_keeps_the_import_time_default_stable():
    result = _clean_import(
        "import os, watcher.config as first;"
        "before = first.DEFAULT_SEEN_DB_PATH;"
        "os.environ['WATCHER_SEEN_DB'] = 'set-after-import.sqlite';"
        "import watcher.config as second;"
        "assert second.DEFAULT_SEEN_DB_PATH is before"
    )

    assert result.returncode == 0, result.stderr


def test_importing_environment_module_alone_runs_dotenv_initialization():
    result = _clean_import(
        "import os, watcher.config.env; print(os.environ.get('DOTENV_MARKER'))",
        dotenv_body="DOTENV_MARKER=applied\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "applied"


def test_facade_reexports_environment_objects_by_identity():
    from watcher.config import env

    names = (
        "ConfigError",
        "DEFAULT_ANALYSIS_CACHE_FILENAME",
        "DEFAULT_DOTENV_PATH",
        "DEFAULT_SEEN_DB_PATH",
        "DEFAULT_WORKDAY_MIN_INTERVAL_SECONDS",
        "MAX_WORKDAY_MIN_INTERVAL_SECONDS",
        "REPO_ROOT",
        "WATCHER_DIR",
        "_parse_env_assignment",
        "analysis_cache_enabled",
        "load_collection_concurrency",
        "load_dotenv",
        "resolve_analysis_cache_path",
        "workday_min_interval_seconds",
    )

    assert all(getattr(config, name) is getattr(env, name) for name in names)


def test_environment_module_has_no_high_level_dependencies():
    tree = ast.parse(
        (ROOT / "watcher" / "config" / "env.py").read_text(encoding="utf-8")
    )
    module_imports = []
    all_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            all_imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            all_imports.append(node.module)
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_imports.append(node.module)

    prohibited = (
        "backend",
        "watcher.collection",
        "watcher.pipeline",
        "watcher.health",
        "watcher.sources",
        "watcher.seen_store",
        "watcher.notify",
    )
    assert not [name for name in all_imports if name.startswith(prohibited)]
    assert not [name for name in module_imports if name.startswith("watcher")]


@pytest.mark.parametrize(
    "first",
    (
        "watcher.config",
        "watcher.config.env",
        "watcher.config.models",
        "watcher.config.validation",
        "watcher.sources.registry",
        "watcher.sources.workday",
    ),
)
def test_no_import_cycle_whichever_module_loads_first(first):
    result = _clean_import(f"import {first}")

    assert result.returncode == 0, result.stderr
