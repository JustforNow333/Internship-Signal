"""The source layer is split by responsibility behind a compatibility facade.

`watcher/sources/base.py` used to hold contracts, diagnostics, transport,
parsing, sanitizers, and canonical rows in one module. These tests pin the
facade's re-export surface and the import direction between the split modules,
so an adapter importing from `watcher.sources.base` keeps working and the
lower layers stay independent.
"""

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
    parsing,
    rows,
    sanitize,
    transport,
)

# Every name the facade promises, and the module that now owns it.
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


@pytest.mark.parametrize("name", sorted(OWNERS))
def test_base_reexports_the_same_object_as_its_owning_module(name):
    assert getattr(base, name) is getattr(OWNERS[name], name)


def test_every_public_facade_export_is_declared_and_resolvable():
    for name in base.__all__:
        assert hasattr(base, name), name
    # __all__ is the public surface; private seams stay reachable but unlisted.
    assert not [name for name in base.__all__ if name.startswith("_")]
    public = {
        name
        for name in OWNERS
        if not name.startswith("_")
    }
    assert public == set(base.__all__)


def test_private_transport_and_sanitizer_seams_remain_importable_from_base():
    # Nothing in the tree imports these today, but they were reachable through
    # `watcher.sources.base` before the split and stay reachable after it.
    for name in sorted(n for n in OWNERS if n.startswith("_")):
        assert getattr(base, name) is getattr(OWNERS[name], name)


def test_adapters_still_import_the_facade_surface_they_relied_on():
    from watcher.sources.greenhouse import GreenhouseSource  # noqa: F401
    from watcher.sources.workday import WorkdaySource  # noqa: F401

    greenhouse = importlib.import_module("watcher.sources.greenhouse")
    assert greenhouse.fetch_json is transport.fetch_json
    assert greenhouse.make_row is rows.make_row


def test_the_split_modules_stay_layered():
    """Lower layers must not import the modules stacked on top of them."""

    def imports_of(module):
        source = importlib.import_module(module.__name__).__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        return {
            line.split("import")[0].replace("from", "").strip()
            for line in text.splitlines()
            if line.startswith("from watcher.sources")
        }

    assert imports_of(sanitize) == set()
    assert imports_of(rows) == set()
    assert imports_of(contracts) == {"watcher.sources.sanitize"}
    assert imports_of(diagnostics) == {"watcher.sources.sanitize"}
    assert imports_of(parsing) == {"watcher.sources.contracts"}
    assert imports_of(transport) == {
        "watcher.sources.contracts",
        "watcher.sources.sanitize",
    }


@pytest.mark.parametrize(
    "first",
    [
        "watcher.sources.base",
        "watcher.sources.contracts",
        "watcher.sources.diagnostics",
        "watcher.sources.parsing",
        "watcher.sources.rows",
        "watcher.sources.sanitize",
        "watcher.sources.transport",
        "watcher.sources.retry",
        "watcher.sources",
    ],
)
def test_each_module_imports_first_without_a_cycle(first):
    """Import each module into a clean interpreter before any sibling."""

    root = pathlib.Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "backend")])
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True,
        text=True,
        cwd=str(root),
        env=env,
    )
    assert result.returncode == 0, result.stderr
