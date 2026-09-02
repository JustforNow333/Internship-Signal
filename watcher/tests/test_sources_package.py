"""Compatibility and import-isolation tests for :mod:`watcher.sources`."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]

EXPORT_OWNERS = {
    "AshbySource": "watcher.sources.ashby",
    "BainSource": "watcher.sources.bain",
    "BrassRingSource": "watcher.sources.brassring",
    "DirectSourceDiagnostics": "watcher.sources.diagnostics",
    "EpicSource": "watcher.sources.epic",
    "EightfoldSource": "watcher.sources.eightfold",
    "GitHubListingsSource": "watcher.sources.github_listings",
    "GitHubMarkdownTableSource": "watcher.sources.github_markdown_table",
    "GreenhouseSource": "watcher.sources.greenhouse",
    "IcimsSource": "watcher.sources.icims",
    "IbmSource": "watcher.sources.ibm",
    "LeverSource": "watcher.sources.lever",
    "OracleHcmSource": "watcher.sources.oracle_hcm",
    "PaylocitySource": "watcher.sources.paylocity",
    "SmartRecruitersSource": "watcher.sources.smartrecruiters",
    "SuccessFactorsSource": "watcher.sources.successfactors",
    "TalentBrewSource": "watcher.sources.talentbrew",
    "TaleoSourcingSource": "watcher.sources.taleo_sourcing",
    "UkgSource": "watcher.sources.ukg",
    "Source": "watcher.sources.contracts",
    "SourceError": "watcher.sources.contracts",
    "SourceFetchError": "watcher.sources.contracts",
    "SourceSchemaError": "watcher.sources.contracts",
    "WorkableSource": "watcher.sources.workable",
    "WorkdaySource": "watcher.sources.workday",
    "make_row": "watcher.sources.rows",
}

EXPECTED_ALL = (
    "AshbySource",
    "BainSource",
    "BrassRingSource",
    "DirectSourceDiagnostics",
    "EpicSource",
    "EightfoldSource",
    "GitHubListingsSource",
    "GitHubMarkdownTableSource",
    "GreenhouseSource",
    "IcimsSource",
    "IbmSource",
    "LeverSource",
    "OracleHcmSource",
    "PaylocitySource",
    "SmartRecruitersSource",
    "SuccessFactorsSource",
    "TalentBrewSource",
    "TaleoSourcingSource",
    "UkgSource",
    "Source",
    "SourceError",
    "SourceFetchError",
    "SourceSchemaError",
    "WorkableSource",
    "WorkdaySource",
    "make_row",
)

ADAPTER_MODULES = frozenset(
    {
        "watcher.sources.ashby",
        "watcher.sources.bain",
        "watcher.sources.brassring",
        "watcher.sources.epic",
        "watcher.sources.eightfold",
        "watcher.sources.github_listings",
        "watcher.sources.github_markdown_table",
        "watcher.sources.greenhouse",
        "watcher.sources.ibm",
        "watcher.sources.icims",
        "watcher.sources.lever",
        "watcher.sources.oracle_hcm",
        "watcher.sources.paylocity",
        "watcher.sources.smartrecruiters",
        "watcher.sources.successfactors",
        "watcher.sources.talentbrew",
        "watcher.sources.taleo_sourcing",
        "watcher.sources.ukg",
        "watcher.sources.workable",
        "watcher.sources.workday",
    }
)

SUPPORT_SUBMODULES = (
    "base",
    "contracts",
    "diagnostics",
    "direct",
    "parsing",
    "retry",
    "rows",
    "sanitize",
    "transport",
)


def _run_clean(code: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "backend")])
    return subprocess.run(
        [sys.executable, "-c", code, *arguments],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
        check=False,
    )


def test_documented_package_exports_are_exact():
    sources = importlib.import_module("watcher.sources")

    assert tuple(sources.__all__) == EXPECTED_ALL
    assert set(EXPORT_OWNERS) == set(EXPECTED_ALL)
    assert set(EXPECTED_ALL) <= set(dir(sources))


@pytest.mark.parametrize("name, owner_name", EXPORT_OWNERS.items())
def test_package_export_is_identical_to_canonical_owner(name, owner_name):
    sources = importlib.import_module("watcher.sources")
    owner = importlib.import_module(owner_name)

    exported = getattr(sources, name)

    assert exported is getattr(owner, name)
    assert vars(sources)[name] is exported


@pytest.mark.parametrize(
    "target, expected_source_modules",
    [
        ("watcher.sources", {"watcher.sources"}),
        (
            "watcher.sources.sanitize",
            {"watcher.sources", "watcher.sources.sanitize"},
        ),
        (
            "watcher.sources.contracts",
            {
                "watcher.sources",
                "watcher.sources.contracts",
                "watcher.sources.sanitize",
            },
        ),
        (
            "watcher.sources.retry",
            {
                "watcher.sources",
                "watcher.sources.contracts",
                "watcher.sources.retry",
                "watcher.sources.sanitize",
            },
        ),
    ],
)
def test_lightweight_imports_do_not_load_adapters(target, expected_source_modules):
    code = """
import importlib
import json
import sys

importlib.import_module(sys.argv[1])
source_modules = sorted(
    name
    for name in sys.modules
    if name == "watcher.sources" or name.startswith("watcher.sources.")
)
print(json.dumps(source_modules))
"""
    result = _run_clean(code, target)

    assert result.returncode == 0, result.stderr
    loaded = set(json.loads(result.stdout))
    assert loaded == expected_source_modules
    assert loaded.isdisjoint(ADAPTER_MODULES)


def test_explicit_adapter_import_loads_only_that_adapter():
    code = f"""
import importlib
import json
import sys

importlib.import_module("watcher.sources.greenhouse")
adapters = sorted(set(sys.modules) & set({sorted(ADAPTER_MODULES)!r}))
print(json.dumps(adapters))
"""
    result = _run_clean(code)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["watcher.sources.greenhouse"]


def test_lazy_export_is_cached_on_first_access():
    code = """
import watcher.sources as sources

assert "GreenhouseSource" not in vars(sources)
first = sources.GreenhouseSource
assert vars(sources)["GreenhouseSource"] is first
assert sources.GreenhouseSource is first
"""
    result = _run_clean(code)

    assert result.returncode == 0, result.stderr


def test_star_import_preserves_documented_surface_and_identity():
    namespace: dict[str, object] = {}
    exec("from watcher.sources import *", namespace)

    imported = set(namespace) - {"__builtins__"}
    assert imported == set(EXPECTED_ALL)
    for name, owner_name in EXPORT_OWNERS.items():
        owner = importlib.import_module(owner_name)
        assert namespace[name] is getattr(owner, name)


@pytest.mark.parametrize("short_name", SUPPORT_SUBMODULES)
def test_explicit_support_submodule_imports_remain_package_attributes(short_name):
    sources = importlib.import_module("watcher.sources")
    module = importlib.import_module(f"watcher.sources.{short_name}")

    assert getattr(sources, short_name) is module


def test_from_package_import_rows_keeps_normal_submodule_behavior():
    from watcher.sources import rows

    assert rows is importlib.import_module("watcher.sources.rows")


@pytest.mark.parametrize(
    "code",
    [
        "import watcher.sources.sanitize; "
        "from watcher.sources import GreenhouseSource, SourceFetchError",
        "from watcher.sources import SourceFetchError; "
        "import watcher.sources.contracts as contracts; "
        "assert SourceFetchError is contracts.SourceFetchError",
        "import watcher.config; import watcher.sources.retry; "
        "from watcher.sources import make_row",
        "import watcher.sources.registry; "
        "from watcher.sources import GreenhouseSource; "
        "from watcher.sources.greenhouse import GreenhouseSource as canonical; "
        "assert GreenhouseSource is canonical",
        "import watcher.sources.rows; from watcher.sources import rows",
    ],
)
def test_clean_interpreter_import_orders(code):
    result = _run_clean(code)

    assert result.returncode == 0, result.stderr
