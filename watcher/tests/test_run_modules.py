"""Module-boundary guards for the split watcher run implementation.

`watcher/run.py` is a compatibility facade over `watcher.collection`,
`watcher.pipeline`, `watcher.reporting`, `watcher.cli`, and
`watcher.run_logging`. These tests pin the facade surface, the ownership of
each moved responsibility, and the acyclic dependency direction between the
new modules, so a later change cannot quietly move a seam that callers,
scripts, or monkeypatching tests still name.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

import watcher.cli as cli
import watcher.collection as collection
import watcher.pipeline as pipeline
import watcher.reporting as reporting
import watcher.run as run
import watcher.run_logging as run_logging

# Every name external callers import from `watcher.run`, and the module that
# must actually own it now.
FACADE_OWNERS = {
    "CollectionStats": collection,
    "WorkdayTransportSummary": collection,
    "WORKDAY_TRANSPORT_ERROR_CODES": collection,
    "collect_rows": collection,
    "collect_batch": collection,
    "summarize_workday_transport": collection,
    "_default_direct_sources": collection,
    "_DirectSourceProvider": collection,
    "_DirectFetchOutcome": collection,
    "_GithubFetchOutcome": collection,
    "_direct_outcome_from_result": collection,
    "_direct_diagnostics_from_source": collection,
    "_http_status_from_error": collection,
    "RunResult": pipeline,
    "run_once": pipeline,
    "RUN_MODE_LIVE": pipeline,
    "RUN_MODE_DRY": pipeline,
    "RUN_MODE_PRIME": pipeline,
    "RUN_MODES": pipeline,
    "print_report": reporting,
    "print_heartbeat": reporting,
    "main": cli,
    "LOGGER": run_logging,
}


@pytest.mark.parametrize("name", sorted(FACADE_OWNERS))
def test_facade_reexports_the_owning_module_object(name):
    """`watcher.run` keeps working and hands back the owner's object."""

    assert getattr(run, name) is getattr(FACADE_OWNERS[name], name)


def test_facade_declares_no_implementation_of_its_own():
    """run.py must stay a facade: imports, `__all__`, and the entry point."""

    tree = ast.parse(Path(run.__file__).read_text(encoding="utf-8"))
    defined = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assigned = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    ]

    assert defined == []
    assert assigned == ["__all__"]


def test_run_logger_name_is_pinned_for_console_format_and_log_filters():
    """The console format prints the name and callers filter records by it."""

    assert run_logging.LOGGER.name == "watcher.run"
    assert logging.getLogger("watcher.run") is run_logging.LOGGER


def test_module_dependencies_stay_acyclic_and_layered():
    """Low-level modules never import the orchestration layers above them."""

    def watcher_imports(module):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "watcher"
            ):
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(a.name for a in node.names if a.name.startswith("watcher"))
        return found

    assert watcher_imports(run_logging) == set()
    for forbidden in ("watcher.pipeline", "watcher.reporting", "watcher.cli", "watcher.run"):
        assert forbidden not in watcher_imports(collection)
    for forbidden in ("watcher.reporting", "watcher.cli", "watcher.run"):
        assert forbidden not in watcher_imports(pipeline)
    for forbidden in ("watcher.cli", "watcher.run"):
        assert forbidden not in watcher_imports(reporting)
    assert "watcher.run" not in watcher_imports(cli)


def test_each_responsibility_lives_in_exactly_one_module():
    """A moved seam must not be duplicated back into another module."""

    owners = {}
    for module in (run_logging, collection, pipeline, reporting, cli):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in tree.body:
            name = getattr(node, "name", None)
            if name is None:
                continue
            assert name not in owners, f"{name} defined in both {owners.get(name)} and {module.__name__}"
            owners[name] = module.__name__

    assert owners["_collect_rows"] == "watcher.collection"
    assert owners["run_once"] == "watcher.pipeline"
    assert owners["print_report"] == "watcher.reporting"
    assert owners["main"] == "watcher.cli"
    assert owners["_timed_stage"] == "watcher.run_logging"
