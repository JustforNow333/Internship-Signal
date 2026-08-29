"""Compatibility and boundary tests for the extracted configuration models."""

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

EXPECTED_FIELDS = {
    "CollectionConcurrencyCfg": [
        ("mode", "serial"),
        ("max_workers", 4),
        ("workday_max_concurrency", 1),
        ("per_origin_max_concurrency", 2),
    ],
    "CompanyCfg": [
        ("name", "<required>"),
        ("ats", ""),
        ("token", ""),
        ("workday_shard", ""),
        ("workday_site", ""),
        ("workday_detail_policy", "internship_candidates"),
        ("oracle_hcm_host", ""),
        ("oracle_hcm_site", ""),
        ("talentbrew_host", ""),
        ("talentbrew_site_id", ""),
        ("talentbrew_category_id", ""),
        ("talentbrew_category_name", ""),
        ("icims_variant", ""),
        ("icims_host", ""),
        ("icims_portals", "<factory:tuple>"),
        ("successfactors_host", ""),
        ("successfactors_site_prefix", ""),
        ("successfactors_locale", ""),
        ("paylocity_company_id", ""),
        ("paylocity_module_id", ""),
        ("paylocity_slug", ""),
        ("source_url", ""),
        ("module", ""),
        ("aliases", "<factory:tuple>"),
        ("alumni_match", "<factory:tuple>"),
        ("terms", "<factory:tuple>"),
    ],
    "GitHubListingSourceCfg": [
        ("name", "<required>"),
        ("format", "<required>"),
        ("url", "<required>"),
        ("default_term", ""),
    ],
    "WatcherConfig": [
        ("companies", "<required>"),
        ("terms", ()),
        ("github_listing_sources", ()),
        ("github_listing_urls", ()),
        ("target_roles", frozenset({"swe"})),
        ("min_score", None),
        ("seen_db_path", "<path-default>"),
        ("analysis_cache_enabled", True),
        ("analysis_cache_path", None),
        ("collection_concurrency", "<factory:CollectionConcurrencyCfg>"),
    ],
}

MODEL_NAMES = tuple(EXPECTED_FIELDS)


def _describe(field: dataclasses.Field):
    if field.default_factory is not dataclasses.MISSING:
        return f"<factory:{field.default_factory.__name__}>"
    if field.default is dataclasses.MISSING:
        return "<required>"
    return field.default


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_moved_model_fields_order_and_defaults_are_unchanged(name):
    from watcher.config import models

    observed = [(field.name, _describe(field)) for field in dataclasses.fields(getattr(models, name))]
    expected = EXPECTED_FIELDS[name]

    assert [item[0] for item in observed] == [item[0] for item in expected]
    for (field_name, got), (_, wanted) in zip(observed, expected):
        if wanted == "<path-default>":
            assert isinstance(got, Path), field_name
        else:
            assert got == wanted, field_name


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_moved_models_keep_dataclass_options_and_owner(name):
    from watcher.config import models

    klass = getattr(models, name)
    params = klass.__dataclass_params__

    assert dataclasses.is_dataclass(klass)
    assert params.frozen is True
    assert params.eq is True
    assert params.init is True
    assert params.repr is True
    assert params.order is False
    assert params.unsafe_hash is False
    assert klass.__module__ == "watcher.config.models"


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_watcher_config_reexports_the_canonical_model_object(name):
    from watcher.config import models

    assert getattr(config, name) is getattr(models, name)


def test_model_construction_equality_repr_hash_and_freezing_are_unchanged():
    company = config.CompanyCfg(name="Acme", aliases=("Acme Corp",))

    assert company == config.CompanyCfg(name="Acme", aliases=("Acme Corp",))
    assert hash(company) == hash(config.CompanyCfg(name="Acme", aliases=("Acme Corp",)))
    assert company.match_names() == ("Acme", "Acme Corp")
    assert repr(company).startswith("CompanyCfg(name='Acme'")
    with pytest.raises(dataclasses.FrozenInstanceError):
        company.name = "Changed"


def test_github_source_and_watcher_config_methods_are_unchanged():
    typed = config.GitHubListingSourceCfg(
        name="typed", format="simplify_json", url="https://example.test/jobs.json"
    )
    watcher_cfg = config.WatcherConfig(
        companies=(),
        github_listing_sources=(typed,),
        github_listing_urls=("https://example.test/jobs.md",),
    )

    assert typed.priority == 10
    assert config.GitHubListingSourceCfg(
        name="markdown", format="github_markdown_table", url="https://example.test/jobs.md"
    ).priority == 20
    assert watcher_cfg.analysis_cache_path == config.resolve_analysis_cache_path(
        watcher_cfg.seen_db_path, ""
    )
    assert len(watcher_cfg.effective_github_listing_sources()) == 2


def test_collection_concurrency_behavior_and_config_error_identity_are_unchanged():
    from watcher.config import _legacy, env, models, validation

    settings = config.CollectionConcurrencyCfg(
        mode="  CONCURRENT  ", max_workers="8", workday_max_concurrency="2"
    )

    assert settings.as_dict() == {
        "mode": "concurrent",
        "max_workers": 8,
        "workday_max_concurrency": 2,
        "per_origin_max_concurrency": 2,
    }
    assert settings.concurrent is True
    assert config.ConfigError is _legacy.ConfigError
    assert config.ConfigError is env.ConfigError
    assert config.supported_ats is validation.supported_ats
    assert config.is_valid_hostname is validation.is_valid_hostname
    assert models.CollectionConcurrencyCfg is config.CollectionConcurrencyCfg
    with pytest.raises(config.ConfigError, match="WATCHER_COLLECTION_MODE"):
        config.CollectionConcurrencyCfg(mode="parallel")


def test_package_conversion_preserves_resolved_paths():
    from watcher.config import env

    assert config.WATCHER_DIR == ROOT / "watcher"
    assert config.REPO_ROOT == ROOT
    assert config.DEFAULT_DOTENV_PATH == ROOT / ".env"
    assert config.DEFAULT_WATCHLIST_PATH == ROOT / "watcher" / "watchlist.yml"
    assert config.DEFAULT_WATCHLIST_PATH.exists()
    assert env.WATCHER_DIR is config.WATCHER_DIR


def test_all_current_public_facade_symbols_resolve():
    expected = {
        "COLLECTION_MODE_CONCURRENT",
        "COLLECTION_MODE_SERIAL",
        "CollectionConcurrencyCfg",
        "CompanyCfg",
        "ConfigError",
        "DEFAULT_ANALYSIS_CACHE_ENABLED",
        "DEFAULT_ANALYSIS_CACHE_FILENAME",
        "DEFAULT_COLLECTION_MAX_WORKERS",
        "DEFAULT_COLLECTION_MODE",
        "DEFAULT_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
        "DEFAULT_DOTENV_PATH",
        "DEFAULT_SEEN_DB_PATH",
        "DEFAULT_WATCHLIST_PATH",
        "DEFAULT_WORKDAY_MAX_CONCURRENCY",
        "DEFAULT_WORKDAY_MIN_INTERVAL_SECONDS",
        "GitHubListingSourceCfg",
        "MAX_COLLECTION_MAX_WORKERS",
        "MAX_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
        "MAX_WORKDAY_MAX_CONCURRENCY",
        "MAX_WORKDAY_MIN_INTERVAL_SECONDS",
        "MIN_COLLECTION_MAX_WORKERS",
        "MIN_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
        "MIN_WORKDAY_MAX_CONCURRENCY",
        "NON_DIRECT_ATS",
        "REPO_ROOT",
        "SUPPORTED_COLLECTION_MODES",
        "SUPPORTED_GITHUB_LISTING_FORMATS",
        "SUPPORTED_WORKDAY_DETAIL_POLICIES",
        "WATCHER_DIR",
        "WORKDAY_DETAIL_EARLY_CAREER",
        "WORKDAY_DETAIL_INTERNSHIP",
        "WORKDAY_DETAIL_NONE",
        "WatcherConfig",
        "analysis_cache_enabled",
        "is_valid_hostname",
        "load_collection_concurrency",
        "load_dotenv",
        "load_watchlist",
        "resolve_analysis_cache_path",
        "supported_ats",
        "workday_min_interval_seconds",
    }

    assert set(config.__all__) == expected
    assert all(hasattr(config, name) for name in expected)
    assert config._parse_env_assignment is config.env._parse_env_assignment
    assert config._parse_watchlist_yaml is config.loader._parse_watchlist_yaml


def test_real_watchlist_and_source_registry_use_the_canonical_models():
    from watcher.config import models
    from watcher.sources.registry import DIRECT_ATS, build_direct_sources

    loaded = config.load_watchlist(config.DEFAULT_WATCHLIST_PATH)

    assert isinstance(loaded, models.WatcherConfig)
    assert loaded.companies
    assert all(isinstance(company, models.CompanyCfg) for company in loaded.companies)
    assert config.supported_ats() == DIRECT_ATS | config.NON_DIRECT_ATS
    assert set(build_direct_sources()) == set(DIRECT_ATS)


def test_config_low_level_modules_have_no_high_level_dependencies():
    model_path = ROOT / "watcher" / "config" / "models.py"
    prohibited = (
        "backend",
        "watcher.collection",
        "watcher.pipeline",
        "watcher.health",
        "watcher.sources",
        "watcher.seen_store",
        "watcher.notify",
    )
    for path in (model_path, ROOT / "watcher" / "config" / "env.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = []
        module_level = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for node in tree.body:
            if isinstance(node, ast.Import):
                module_level.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                module_level.append(node.module)

        assert not [name for name in imported if name.startswith(prohibited)]
        if path.name == "env.py":
            assert not [name for name in module_level if name.startswith("watcher")]
        else:
            assert [name for name in module_level if name.startswith("watcher")] == [
                "watcher.config.env"
            ]


@pytest.mark.parametrize(
    "first",
    [
        "watcher.config",
        "watcher.config.env",
        "watcher.config.models",
        "watcher.config.loader",
        "watcher.config.validation",
        "watcher.config._legacy",
        "watcher.sources.registry",
        "watcher.sources.greenhouse",
        "watcher.sources.workday",
    ],
)
def test_no_import_cycle_whichever_module_loads_first(first):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "backend")))
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr


def test_dotenv_seen_database_default_keeps_pre_split_import_order():
    code = f"""
import os
from pathlib import Path

root = Path({str(ROOT)!r})
dotenv = root / '.env'
original_exists = Path.exists
original_read_text = Path.read_text

def fake_exists(self):
    return True if self == dotenv else original_exists(self)

def fake_read_text(self, *args, **kwargs):
    if self == dotenv:
        return 'WATCHER_SEEN_DB=from-dotenv.sqlite\\n'
    return original_read_text(self, *args, **kwargs)

Path.exists = fake_exists
Path.read_text = fake_read_text
os.environ.pop('WATCHER_SEEN_DB', None)

import watcher.config as imported_config
assert imported_config.DEFAULT_SEEN_DB_PATH == Path('from-dotenv.sqlite')
"""
    env = dict(os.environ)
    env.pop("WATCHER_SEEN_DB", None)
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(ROOT / "backend")))
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )

    assert result.returncode == 0, result.stderr
