"""Configuration model and public-facade compatibility tests.

These tests pin the moved models against their pre-refactor definitions, pin
`watcher.config` as the unchanged public import path, and pin the import
direction so the extraction cannot reintroduce a cycle.
"""

import dataclasses
import os
import pathlib
import subprocess
import sys

import pytest

import watcher.config as config
import watcher.config.env as env
import watcher.config.loader as loader
import watcher.config.models as models
import watcher.config.validation as validation

ROOT = pathlib.Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Field-level parity with the pre-refactor definitions
# --------------------------------------------------------------------------

# Captured before the package extraction: every field name, in order, with its
# default. "<factory:tuple>" marks a default_factory.
EXPECTED_FIELDS = {
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
        ("coverage_status", ""),
        ("platform_family", ""),
    ],
    "GitHubListingSourceCfg": [
        ("name", "<required>"),
        ("format", "<required>"),
        ("url", "<required>"),
        ("default_term", ""),
    ],
    "CollectionConcurrencyCfg": [
        ("mode", "serial"),
        ("max_workers", 4),
        ("workday_max_concurrency", 1),
        ("per_origin_max_concurrency", 2),
    ],
    "WatcherConfig": [
        ("companies", "<required>"),
        ("terms", ()),
        ("github_listing_sources", ()),
        ("github_listing_urls", ()),
        ("target_roles", frozenset({"swe"})),
        ("min_score", None),
        ("seen_db_path", "<default>"),
        ("analysis_cache_enabled", True),
        ("analysis_cache_path", None),
        ("collection_concurrency", "<factory:CollectionConcurrencyCfg>"),
    ],
}

MODEL_NAMES = sorted(EXPECTED_FIELDS)


def describe(field):
    if field.default_factory is not dataclasses.MISSING:
        return f"<factory:{field.default_factory.__name__}>"
    if field.default is dataclasses.MISSING:
        return "<required>"
    return field.default


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_moved_model_field_names_order_and_defaults_are_unchanged(name):
    klass = getattr(models, name)
    observed = [(f.name, describe(f)) for f in dataclasses.fields(klass)]
    expected = EXPECTED_FIELDS[name]

    assert [f for f, _ in observed] == [f for f, _ in expected]
    for (field_name, got), (_, want) in zip(observed, expected):
        if want == "<default>":
            # seen_db_path's default is an environment-derived path.
            assert isinstance(got, pathlib.Path)
            continue
        assert got == want, field_name


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_moved_models_are_frozen_dataclasses_with_unchanged_options(name):
    klass = getattr(models, name)
    params = klass.__dataclass_params__

    assert dataclasses.is_dataclass(klass)
    assert params.frozen is True
    assert params.eq is True
    assert params.init is True
    assert params.repr is True
    assert params.order is False
    assert params.unsafe_hash is False


@pytest.mark.parametrize("name", MODEL_NAMES)
def test_moved_models_live_in_the_models_module(name):
    assert getattr(models, name).__module__ == "watcher.config.models"


# --------------------------------------------------------------------------
# watcher.config stays the public path, exporting the same objects
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", MODEL_NAMES)
def test_watcher_config_reexports_the_same_class_object(name):
    assert getattr(config, name) is getattr(models, name)


# Every symbol the repository imported from watcher.config before the split.
PREVIOUSLY_IMPORTED = [
    "COLLECTION_MODE_CONCURRENT",
    "COLLECTION_MODE_SERIAL",
    "COVERAGE_STATUS_NO_SOURCE_FOUND",
    "CollectionConcurrencyCfg",
    "CompanyCfg",
    "ConfigError",
    "DEFAULT_ANALYSIS_CACHE_FILENAME",
    "DEFAULT_WATCHLIST_PATH",
    "GitHubListingSourceCfg",
    "NON_DIRECT_ATS",
    "WORKDAY_DETAIL_EARLY_CAREER",
    "WORKDAY_DETAIL_INTERNSHIP",
    "WORKDAY_DETAIL_NONE",
    "WatcherConfig",
    "_parse_env_assignment",
    "_parse_watchlist_yaml",
    "analysis_cache_enabled",
    "is_valid_hostname",
    "load_collection_concurrency",
    "load_dotenv",
    "load_watchlist",
    "resolve_analysis_cache_path",
    "supported_ats",
    "workday_min_interval_seconds",
]

CANONICAL_PUBLIC_EXPORTS = {
    env: {
        "DEFAULT_ANALYSIS_CACHE_FILENAME",
        "DEFAULT_DOTENV_PATH",
        "DEFAULT_SEEN_DB_PATH",
        "DEFAULT_WORKDAY_MIN_INTERVAL_SECONDS",
        "MAX_WORKDAY_MIN_INTERVAL_SECONDS",
        "REPO_ROOT",
        "WATCHER_DIR",
        "ConfigError",
        "analysis_cache_enabled",
        "load_collection_concurrency",
        "load_dotenv",
        "resolve_analysis_cache_path",
        "workday_min_interval_seconds",
    },
    models: {
        "COLLECTION_MODE_CONCURRENT",
        "COLLECTION_MODE_SERIAL",
        "DEFAULT_ANALYSIS_CACHE_ENABLED",
        "DEFAULT_COLLECTION_MAX_WORKERS",
        "DEFAULT_COLLECTION_MODE",
        "DEFAULT_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
        "DEFAULT_WORKDAY_MAX_CONCURRENCY",
        "MAX_COLLECTION_MAX_WORKERS",
        "MAX_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
        "MAX_WORKDAY_MAX_CONCURRENCY",
        "MIN_COLLECTION_MAX_WORKERS",
        "MIN_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY",
        "MIN_WORKDAY_MAX_CONCURRENCY",
        "SUPPORTED_COLLECTION_MODES",
        "SUPPORTED_WORKDAY_DETAIL_POLICIES",
        "WORKDAY_DETAIL_EARLY_CAREER",
        "WORKDAY_DETAIL_INTERNSHIP",
        "WORKDAY_DETAIL_NONE",
        "CollectionConcurrencyCfg",
        "CompanyCfg",
        "GitHubListingSourceCfg",
        "WatcherConfig",
    },
    loader: {"DEFAULT_WATCHLIST_PATH", "load_watchlist"},
    validation: {
        "COVERAGE_STATUS_NO_SOURCE_FOUND",
        "MAX_PLATFORM_FAMILY_LENGTH",
        "NON_DIRECT_ATS",
        "SUPPORTED_COVERAGE_STATUSES",
        "SUPPORTED_GITHUB_LISTING_FORMATS",
        "is_valid_hostname",
        "supported_ats",
    },
}

PRIVATE_COMPATIBILITY_EXPORTS = {
    env: {"_parse_env_assignment"},
    loader: {"_parse_watchlist_yaml"},
}


@pytest.mark.parametrize("name", PREVIOUSLY_IMPORTED)
def test_every_previously_imported_symbol_still_resolves(name):
    assert hasattr(config, name), name


def test_complete_public_facade_resolves_to_canonical_objects():
    expected_all = set()
    for owner, names in CANONICAL_PUBLIC_EXPORTS.items():
        expected_all.update(names)
        for name in names:
            assert getattr(config, name) is getattr(owner, name), name

    assert set(config.__all__) == expected_all


def test_private_compatibility_seams_resolve_but_are_not_public_all():
    for owner, names in PRIVATE_COMPATIBILITY_EXPORTS.items():
        for name in names:
            assert getattr(config, name) is getattr(owner, name), name
            assert name not in config.__all__


def test_validation_symbols_resolve_directly_from_their_owner():
    for name in ("supported_ats", "is_valid_hostname", "NON_DIRECT_ATS",
                 "SUPPORTED_COVERAGE_STATUSES", "SUPPORTED_GITHUB_LISTING_FORMATS",
                 "MAX_PLATFORM_FAMILY_LENGTH", "COVERAGE_STATUS_NO_SOURCE_FOUND"):
        assert getattr(config, name) is getattr(validation, name)


def test_symbols_owned_by_the_loader_are_the_same_objects():
    for name in ("load_watchlist", "DEFAULT_WATCHLIST_PATH", "_parse_watchlist_yaml"):
        assert getattr(config, name) is getattr(loader, name)


def test_symbols_owned_by_env_are_the_same_objects():
    for name in ("load_dotenv", "ConfigError", "analysis_cache_enabled",
                 "workday_min_interval_seconds", "resolve_analysis_cache_path",
                 "load_collection_concurrency", "_parse_env_assignment",
                 "DEFAULT_SEEN_DB_PATH", "WATCHER_DIR", "REPO_ROOT",
                 "DEFAULT_DOTENV_PATH"):
        assert getattr(config, name) is getattr(env, name)


def test_public_all_lists_only_the_supported_surface():
    for name in config.__all__:
        assert hasattr(config, name), name
    assert not [name for name in config.__all__ if name.startswith("_")]


def test_repository_callers_resolve_without_import_changes():
    """Every named facade import in production, tests, and scripts still works."""

    import ast

    roots = (ROOT / "watcher", ROOT / "backend", ROOT / "scripts")
    imported = set()
    for root in roots:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "watcher.config":
                    imported.update(alias.name for alias in node.names if alias.name != "*")

    missing = sorted(name for name in imported if not hasattr(config, name))
    assert missing == []


# --------------------------------------------------------------------------
# Behaviour: construction, equality, repr, validation
# --------------------------------------------------------------------------

def test_company_cfg_constructs_and_compares_identically():
    plain = config.CompanyCfg(name="Acme")
    same = config.CompanyCfg(name="Acme")
    other = config.CompanyCfg(name="Acme", ats="greenhouse", aliases=("A", "B"))

    assert plain == same
    assert hash(plain) == hash(same)
    assert plain != other
    assert plain.match_names() == ("Acme",)
    assert other.match_names() == ("Acme", "A", "B")
    assert plain.workday_detail_policy == config.WORKDAY_DETAIL_INTERNSHIP
    assert plain.icims_portals == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        plain.name = "changed"


def test_github_listing_source_priority_is_unchanged():
    simplify = config.GitHubListingSourceCfg(
        name="s", format="simplify_json", url="https://x.test/a.json"
    )
    markdown = config.GitHubListingSourceCfg(
        name="t", format="github_markdown_table", url="https://x.test/b.md"
    )

    assert simplify.priority == 10
    assert markdown.priority == 20
    assert simplify.default_term == ""


def test_collection_concurrency_defaults_and_coercion_are_unchanged():
    default = config.CollectionConcurrencyCfg()

    assert default.as_dict() == {
        "mode": "serial",
        "max_workers": 4,
        "workday_max_concurrency": 1,
        "per_origin_max_concurrency": 2,
    }
    assert default.concurrent is False
    assert config.CollectionConcurrencyCfg(mode="  CONCURRENT  ").mode == "concurrent"
    assert config.CollectionConcurrencyCfg(mode=None).mode == "serial"
    assert config.CollectionConcurrencyCfg(max_workers="8").max_workers == 8
    assert config.CollectionConcurrencyCfg(mode="concurrent").concurrent is True


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"mode": "parallel"}, "WATCHER_COLLECTION_MODE"),
        ({"max_workers": 0}, "WATCHER_COLLECTION_MAX_WORKERS"),
        ({"max_workers": 17}, "WATCHER_COLLECTION_MAX_WORKERS"),
        ({"workday_max_concurrency": 6}, "WATCHER_WORKDAY_MAX_CONCURRENCY"),
        ({"per_origin_max_concurrency": 5}, "WATCHER_COLLECTION_PER_ORIGIN_MAX_CONCURRENCY"),
        (
            {"max_workers": 1, "workday_max_concurrency": 2},
            "cannot exceed",
        ),
        (
            {"max_workers": 1, "per_origin_max_concurrency": 2},
            "cannot exceed",
        ),
    ],
)
def test_collection_concurrency_validation_still_raises_config_error(kwargs, message):
    with pytest.raises(config.ConfigError, match=message):
        config.CollectionConcurrencyCfg(**kwargs)


def test_watcher_config_post_init_resolves_paths_as_before():
    cfg = config.WatcherConfig(companies=())

    assert isinstance(cfg.seen_db_path, pathlib.Path)
    assert cfg.analysis_cache_path == config.resolve_analysis_cache_path(
        cfg.seen_db_path, ""
    )
    assert cfg.seen_db_path == config.DEFAULT_SEEN_DB_PATH

    explicit = config.WatcherConfig(companies=(), seen_db_path="rel/seen.sqlite")
    assert explicit.seen_db_path == pathlib.Path("rel/seen.sqlite")


def test_watcher_config_merges_typed_and_legacy_github_sources():
    typed = config.GitHubListingSourceCfg(
        name="typed", format="simplify_json", url="https://x.test/a.json"
    )
    cfg = config.WatcherConfig(
        companies=(),
        github_listing_sources=(typed,),
        github_listing_urls=("https://x.test/legacy.md",),
    )

    merged = cfg.effective_github_listing_sources()
    assert len(merged) == 2
    assert merged[0].priority <= merged[1].priority
    assert typed in merged


# --------------------------------------------------------------------------
# Paths must still resolve against watcher/, not watcher/config/
# --------------------------------------------------------------------------

def test_package_conversion_did_not_shift_the_resolved_directories():
    assert config.WATCHER_DIR.name == "watcher"
    assert config.WATCHER_DIR == ROOT / "watcher"
    assert config.REPO_ROOT == ROOT
    assert config.DEFAULT_WATCHLIST_PATH == ROOT / "watcher" / "watchlist.yml"
    assert config.DEFAULT_WATCHLIST_PATH.exists()
    assert config.DEFAULT_DOTENV_PATH == ROOT / ".env"
    assert env.WATCHER_DIR is config.WATCHER_DIR


def test_the_real_watchlist_still_loads():
    loaded = config.load_watchlist(config.DEFAULT_WATCHLIST_PATH)

    assert isinstance(loaded, models.WatcherConfig)
    assert loaded.companies
    assert all(isinstance(c, models.CompanyCfg) for c in loaded.companies)


# --------------------------------------------------------------------------
# Registry behaviour and import direction
# --------------------------------------------------------------------------

def test_supported_ats_still_defers_to_the_registry():
    from watcher.sources.registry import DIRECT_ATS

    assert config.supported_ats() == DIRECT_ATS | config.NON_DIRECT_ATS
    assert config.NON_DIRECT_ATS == frozenset({"bespoke", "github_only"})


def module_scope_imports(relative_path):
    import ast

    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_models_module_imports_no_watcher_code_outside_the_config_package():
    """models.py must stay importable without pulling in adapters."""

    imports = module_scope_imports("watcher/config/models.py")
    outside = [
        m for m in imports
        if m.startswith("watcher") and not m.startswith("watcher.config.")
    ]
    assert outside == [], imports
    # Its only module-scope in-package dependency is the environment layer.
    assert [m for m in imports if m in {"env", "loader", "validation"}] == ["env"]


def test_env_module_is_the_lowest_layer_of_the_config_package():
    """env.py must import nothing else from the package at module scope.

    models.py reads DEFAULT_SEEN_DB_PATH from env.py while its class bodies
    execute, so any module-scope import back into the package would cycle.
    """

    imports = module_scope_imports("watcher/config/env.py")
    assert [m for m in imports if m.startswith("watcher")] == [], imports


def test_source_adapters_still_import_company_cfg():
    from watcher.sources.greenhouse import GreenhouseSource  # noqa: F401
    from watcher.sources.workday import WorkdaySource  # noqa: F401
    import watcher.sources.greenhouse as greenhouse

    assert greenhouse.CompanyCfg is models.CompanyCfg


@pytest.mark.parametrize(
    "order",
    [
        (
            "watcher.config",
            "watcher.config.models",
            "watcher.config.env",
            "watcher.config.loader",
            "watcher.config.validation",
            "watcher.sources.registry",
            "watcher.sources.greenhouse",
            "watcher.sources.workday",
        ),
        (
            "watcher.config.models",
            "watcher.config.validation",
            "watcher.sources.registry",
            "watcher.config.loader",
            "watcher.config.env",
            "watcher.config",
            "watcher.sources.workday",
        ),
        (
            "watcher.sources.registry",
            "watcher.sources.greenhouse",
            "watcher.config.validation",
            "watcher.config.loader",
            "watcher.config.models",
            "watcher.config.env",
            "watcher.config",
        ),
        (
            "watcher.sources.workday",
            "watcher.config.env",
            "watcher.config.models",
            "watcher.config.validation",
            "watcher.config.loader",
            "watcher.config",
            "watcher.sources.registry",
        ),
    ],
)
def test_representative_clean_interpreter_import_orders_do_not_cycle(order):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "backend")])
    code = ";".join(f"import {module}" for module in order)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr


def test_constructing_models_works_when_models_is_imported_alone():
    """Deferred in-package imports must resolve at construction time."""

    code = (
        "from watcher.config.models import CollectionConcurrencyCfg, WatcherConfig;"
        "c = CollectionConcurrencyCfg(mode='concurrent');"
        "w = WatcherConfig(companies=());"
        "assert c.concurrent is True;"
        "assert w.analysis_cache_path is not None;"
        "print('ok')"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "backend")])
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
