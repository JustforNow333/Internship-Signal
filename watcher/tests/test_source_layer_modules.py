"""Source-layer ownership and compatibility-facade boundaries."""

from __future__ import annotations

import ast
import importlib
import os
import pathlib
import subprocess
import sys

import pytest

from watcher.sources import (
    base,
    contracts,
    diagnostics,
    direct,
    parsing,
    retry,
    rows,
    sanitize,
    transport,
)


OWNERS = {
    "JsonHttpResponse": contracts,
    "Source": contracts,
    "SourceError": contracts,
    "SourceFetchError": contracts,
    "SourceSchemaError": contracts,
    "TextHttpResponse": contracts,
    "require_token": contracts,
    "DirectDiagnosticsMixin": diagnostics,
    "DirectSourceDiagnostics": diagnostics,
    "ensure_list": parsing,
    "page_fingerprint": parsing,
    "parse_records": parsing,
    "iso_date": rows,
    "make_row": rows,
    "MAX_SAFE_PREVIEW_CHARS": sanitize,
    "html_to_text": sanitize,
    "_safe_body_preview": sanitize,
    "_safe_error_code": sanitize,
    "_safe_url": sanitize,
    "_sanitize_fetch_message": sanitize,
    "DEFAULT_MAX_RESPONSE_BYTES": transport,
    "DEFAULT_TIMEOUT_SECONDS": transport,
    "USER_AGENT": transport,
    "fetch_json": transport,
    "fetch_text": transport,
    "get_json_response": transport,
    "get_text_response": transport,
    "post_json": transport,
    "post_json_response": transport,
    "_DecodedBodyTooLarge": transport,
    "_body_kind": transport,
    "_classify_http_failure": transport,
    "_content_charset": transport,
    "_decode_content_encoding": transport,
    "_decode_json_http_response": transport,
    "_decode_response_text": transport,
    "_decode_text_http_response": transport,
    "_header_value": transport,
    "_http_error_code": transport,
    "_is_access_challenge_text": transport,
    "_json_content_type": transport,
    "_network_error_code": transport,
    "_response_metadata": transport,
    "_response_url": transport,
    "_retry_after_seconds": transport,
}

CANONICAL_IMPLEMENTATION_MODULES = (
    contracts,
    diagnostics,
    direct,
    parsing,
    retry,
    rows,
    sanitize,
    transport,
)

DIRECT_OWNER_IMPORT_ADAPTERS = (
    "watcher.sources.ashby",
    "watcher.sources.bain",
    "watcher.sources.epic",
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
    "watcher.sources.workable",
    "watcher.sources.workday",
)


def _imports_of(module) -> set[str]:
    source = pathlib.Path(importlib.import_module(module.__name__).__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
    return imports


def _base_facade_import_lines(module) -> list[int]:
    source = pathlib.Path(importlib.import_module(module.__name__).__file__)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        imports_base = False
        if isinstance(node, ast.Import):
            imports_base = any(
                alias.name == "watcher.sources.base" for alias in node.names
            )
        elif isinstance(node, ast.ImportFrom):
            imports_base = (
                node.level == 0 and node.module == "watcher.sources.base"
            ) or (
                node.level > 0
                and (
                    node.module == "base"
                    or (
                        node.module is None
                        and any(alias.name == "base" for alias in node.names)
                    )
                )
            )
        if imports_base:
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("name", sorted(OWNERS))
def test_base_reexports_the_same_object_as_its_owning_module(name):
    assert getattr(base, name) is getattr(OWNERS[name], name)


def test_source_exception_identity_is_preserved_across_public_import_paths():
    import watcher.sources as sources

    assert base.SourceError is contracts.SourceError is sources.SourceError
    assert (
        base.SourceFetchError
        is contracts.SourceFetchError
        is sources.SourceFetchError
    )
    assert (
        base.SourceSchemaError
        is contracts.SourceSchemaError
        is sources.SourceSchemaError
    )
    error = base.SourceFetchError(
        "failed https://example.test/jobs?token=secret",
        error_code="Temporary Failure",
        status_code=503,
        retryable=True,
        response_metadata={"attempt": 2},
        attempt_count=2,
    )
    assert str(error) == "failed https://example.test/jobs"
    assert error.error_code == "temporary_failure"
    assert error.status_code == 503
    assert error.retryable is True
    assert error.response_metadata == {"attempt": 2}
    assert error.attempt_count == 2


def test_every_public_facade_export_is_declared_and_resolvable():
    assert all(hasattr(base, name) for name in base.__all__)
    assert not [name for name in base.__all__ if name.startswith("_")]
    assert {name for name in OWNERS if not name.startswith("_")} == set(base.__all__)


def test_private_transport_and_sanitizer_seams_remain_importable_from_base():
    for name in sorted(name for name in OWNERS if name.startswith("_")):
        assert getattr(base, name) is getattr(OWNERS[name], name)


def test_source_sanitizers_keep_safe_text_conversion_for_unprintable_values():
    class Unprintable:
        def __bool__(self):
            raise RuntimeError("broken truth conversion")

        def __str__(self):
            raise RuntimeError("broken text conversion")

    value = Unprintable()

    assert sanitize.html_to_text(value) == ""
    assert sanitize._safe_url(value) == ""
    assert sanitize._sanitize_fetch_message(value) == ""
    assert sanitize._safe_error_code(value) == "fetch_failure"
    assert sanitize._safe_body_preview(value) == ""


@pytest.mark.parametrize(
    "module",
    CANONICAL_IMPLEMENTATION_MODULES,
    ids=lambda module: module.__name__.rsplit(".", 1)[-1],
)
def test_canonical_implementation_modules_do_not_import_base_facade(module):
    assert _base_facade_import_lines(module) == []


@pytest.mark.parametrize("module_name", DIRECT_OWNER_IMPORT_ADAPTERS)
def test_migrated_adapters_import_canonical_owners_directly(module_name):
    module = importlib.import_module(module_name)
    assert _base_facade_import_lines(module) == []


def test_split_modules_keep_the_reference_layering():
    source_imports = {
        module: {
            name for name in _imports_of(module) if name.startswith("watcher.sources")
        }
        for module in CANONICAL_IMPLEMENTATION_MODULES
    }
    assert source_imports[sanitize] == set()
    assert source_imports[rows] == set()
    assert source_imports[contracts] == {"watcher.sources.sanitize"}
    assert source_imports[diagnostics] == {"watcher.sources.sanitize"}
    assert source_imports[direct] == {
        "watcher.sources.diagnostics",
        "watcher.sources.parsing",
    }
    assert source_imports[parsing] == {"watcher.sources.contracts"}
    assert source_imports[retry] == {"watcher.sources.contracts"}
    assert source_imports[transport] == {
        "watcher.sources.contracts",
        "watcher.sources.sanitize",
    }


def test_low_level_modules_do_not_import_collection_pipeline_or_health_layers():
    forbidden = (
        "watcher.collection",
        "watcher.pipeline",
        "watcher.health",
        "watcher.source_health",
        "watcher.health_alerts",
    )
    for module in CANONICAL_IMPLEMENTATION_MODULES:
        imports = _imports_of(module)
        assert not [
            name
            for name in imports
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in forbidden)
        ], module.__name__


@pytest.mark.parametrize(
    "first",
    [
        "watcher.sources.base",
        "watcher.sources.brassring",
        "watcher.sources.taleo_sourcing",
        "watcher.sources.ukg",
        "watcher.sources.contracts",
        "watcher.sources.diagnostics",
        "watcher.sources.direct",
        "watcher.sources.parsing",
        "watcher.sources.rows",
        "watcher.sources.sanitize",
        "watcher.sources.transport",
        "watcher.sources.retry",
        "watcher.sources.registry",
        "watcher.sources",
        *DIRECT_OWNER_IMPORT_ADAPTERS,
    ],
)
def test_each_module_imports_first_without_a_cycle(first):
    root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "backend")])
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
