"""Loader ownership, behavior, compatibility, and import-boundary tests."""

from __future__ import annotations

import ast
import dataclasses
import os
from pathlib import Path
import subprocess
import sys

import pytest

import watcher.config as config


ROOT = Path(__file__).resolve().parents[2]
HEAD = 'defaults:\n  terms: ["Summer 2027"]\n'


def _write_watchlist(tmp_path: Path, defaults: str = "", companies: str = "") -> Path:
    path = tmp_path / "watchlist.yml"
    path.write_text(
        HEAD + defaults + "companies:\n" + companies,
        encoding="utf-8",
    )
    return path


def test_facade_reexports_loader_objects_by_identity():
    from watcher.config import loader

    assert config.DEFAULT_WATCHLIST_PATH is loader.DEFAULT_WATCHLIST_PATH
    assert config.load_watchlist is loader.load_watchlist
    assert config._parse_watchlist_yaml is loader._parse_watchlist_yaml


def test_loader_reuses_validation_owner_objects():
    from watcher.config import loader, validation

    names = (
        "_validate_aliases",
        "_validate_company_entry",
        "_validate_company_identity",
        "_validate_default_terms_present",
        "_validate_github_source_uniqueness",
        "_validate_github_listing_sources_value",
        "_validate_icims_config",
        "_validate_oracle_hcm_config",
        "_validate_paylocity_config",
        "_validate_successfactors_config",
        "_validate_talentbrew_config",
        "_validate_terms_tuple",
        "_validate_token_config",
        "_validate_unique_company_names",
        "_validate_watchlist_sections",
        "_validate_workday_config",
        "_validated_github_listing_urls",
        "_validated_github_source_fields",
        "_validated_min_score",
    )

    assert all(getattr(loader, name) is getattr(validation, name) for name in names)


def test_validation_rules_are_not_duplicated_into_loader():
    source = (ROOT / "watcher" / "config" / "loader.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }

    assert not [name for name in defined if name.startswith("_validate")]
    assert "is_valid_hostname" not in defined
    assert "supported_ats" not in defined
    for message in (
        "entries require token",
        "defaults.min_score must be",
        "aliases may not contain",
        "duplicate feed identities",
    ):
        assert message not in source


def test_legacy_no_longer_defines_loader_owned_symbols_or_imports_loader():
    tree = ast.parse(
        (ROOT / "watcher" / "config" / "_legacy.py").read_text(encoding="utf-8")
    )
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    moved = {
        "load_watchlist",
        "_build_company",
        "_parse_watchlist_yaml",
        "_split_key_value",
        "_parse_value",
        "_string_tuple",
        "_aliases_tuple",
        "_terms_tuple",
        "_github_listing_urls",
        "_github_listing_sources",
        "_legacy_github_source",
        "_github_source_sort_key",
    }
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    assert not defined & moved
    assert "watcher.config.loader" not in imports


def test_minimal_watchlist_preserves_defaults_and_model_types(tmp_path):
    from watcher.config import loader, models

    loaded = loader.load_watchlist(
        _write_watchlist(
            tmp_path,
            companies="  - name: Example\n    ats: github_only\n",
        )
    )

    assert isinstance(loaded, models.WatcherConfig)
    assert loaded.terms == ("Summer 2027",)
    assert loaded.target_roles == frozenset({"swe"})
    assert loaded.min_score is None
    assert loaded.github_listing_sources == ()
    assert loaded.github_listing_urls == ()
    assert loaded.companies == (
        models.CompanyCfg(
            name="Example",
            ats="github_only",
            terms=("Summer 2027",),
        ),
    )


def test_company_order_and_optional_field_parsing_are_unchanged(tmp_path):
    loaded = config.load_watchlist(
        _write_watchlist(
            tmp_path,
            defaults=(
                "  target_roles: [\"data\", \"swe\"]\n"
                "  min_score: 17\n"
            ),
            companies=(
                "  - name: First\n"
                "    ats: greenhouse\n"
                "    token: first-token\n"
                "    aliases: [\"First Inc\", \"First Technologies\"]\n"
                "    alumni_match: [\"first alumni\"]\n"
                "    terms: [\"Fall 2027\"]\n"
                "  - name: Second\n"
                "    ats: github_only\n"
            ),
        )
    )

    assert [company.name for company in loaded.companies] == ["First", "Second"]
    assert loaded.companies[0].aliases == ("First Inc", "First Technologies")
    assert loaded.companies[0].alumni_match == ("first alumni",)
    assert loaded.companies[0].terms == ("Fall 2027",)
    assert loaded.companies[1].terms == ("Summer 2027",)
    assert loaded.target_roles == frozenset({"data", "swe"})
    assert loaded.min_score == 17


def test_typed_and_legacy_feeds_preserve_fixed_order_and_values(tmp_path):
    loaded = config.load_watchlist(
        _write_watchlist(
            tmp_path,
            defaults=(
                "  github_listing_sources:\n"
                "    - name: markdown\n"
                "      format: github_markdown_table\n"
                "      url: https://example.test/jobs.md\n"
                "      default_term: Summer 2027\n"
                "    - name: simplify\n"
                "      format: simplify_json\n"
                "      url: https://example.test/jobs.json\n"
                "  github_listing_urls: [\"https://example.test/legacy.json\"]\n"
            ),
            companies="  - name: Example\n    ats: github_only\n",
        )
    )

    assert [source.name for source in loaded.github_listing_sources] == [
        "markdown",
        "simplify",
    ]
    assert [source.priority for source in loaded.effective_github_listing_sources()] == [
        10,
        10,
        20,
    ]
    assert loaded.github_listing_urls == ("https://example.test/legacy.json",)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "watchlist must define at least one company"),
        ('defaults:\n  terms: ["Summer 2027"]\n', "at least one company"),
        ("companies:\n  - name: Example\n    ats: github_only\n", "defaults.terms"),
        (
            'defaults:\n  terms: ["Summer 2027"\ncompanies:\n'
            "  - name: Example\n    ats: github_only\n",
            "Invalid inline list",
        ),
        (
            'defaults:\n  terms: ["Summer 2027"]\ncompanies:\n'
            "  - name Example\n    ats: github_only\n",
            "Expected key/value pair",
        ),
    ],
)
def test_malformed_watchlists_keep_exact_failure_classes_and_messages(
    tmp_path, text, message
):
    path = tmp_path / "malformed.yml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(config.ConfigError, match=message):
        config.load_watchlist(path)


def test_missing_watchlist_failure_is_unchanged(tmp_path):
    path = tmp_path / "missing.yml"

    with pytest.raises(config.ConfigError) as caught:
        config.load_watchlist(path)

    assert str(caught.value) == f"Watchlist not found: {path}"


def test_environment_defaults_are_applied_during_watchlist_construction(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WATCHER_ANALYSIS_CACHE_ENABLED", "0")
    monkeypatch.setenv("WATCHER_ANALYSIS_CACHE_PATH", " custom/cache.sqlite ")
    monkeypatch.setenv("WATCHER_COLLECTION_MODE", "concurrent")
    monkeypatch.setenv("WATCHER_COLLECTION_MAX_WORKERS", "4")
    monkeypatch.setenv("WATCHER_WORKDAY_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY", "2")

    loaded = config.load_watchlist(
        _write_watchlist(
            tmp_path,
            companies="  - name: Example\n    ats: github_only\n",
        )
    )

    assert loaded.seen_db_path == config.DEFAULT_SEEN_DB_PATH
    assert loaded.analysis_cache_enabled is False
    assert loaded.analysis_cache_path == Path("custom/cache.sqlite")
    assert loaded.collection_concurrency.mode == "concurrent"


def test_real_watchlist_round_trips_through_current_dataclass_fields():
    from watcher.config import loader, models

    loaded = loader.load_watchlist(loader.DEFAULT_WATCHLIST_PATH)
    rebuilt = models.WatcherConfig(
        **{
            field.name: getattr(loaded, field.name)
            for field in dataclasses.fields(models.WatcherConfig)
        }
    )

    assert loaded == rebuilt
    assert loaded.companies
    assert all(isinstance(company, models.CompanyCfg) for company in loaded.companies)


@pytest.mark.parametrize(
    "first",
    (
        "watcher.config",
        "watcher.config.loader",
        "watcher.config.validation",
        "watcher.config._legacy",
        "watcher.config.models",
        "watcher.config.env",
        "watcher.sources.registry",
        "watcher.sources.workday",
    ),
)
def test_no_import_cycle_whichever_module_loads_first(first):
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "backend")))
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=child_env,
    )

    assert result.returncode == 0, result.stderr
