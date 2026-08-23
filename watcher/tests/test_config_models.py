"""`watcher/config.py` became a package; the data models moved to `models.py`.

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
from watcher.config import _legacy, models

ROOT = pathlib.Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Field-level parity with the pre-refactor definitions
# --------------------------------------------------------------------------

# Captured from watcher/config.py before it became a package: every field name,
# in order, with its default. "<factory:tuple>" marks a default_factory.
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


@pytest.mark.parametrize("name", PREVIOUSLY_IMPORTED)
def test_every_previously_imported_symbol_still_resolves(name):
    assert hasattr(config, name), name


def test_symbols_still_owned_by_legacy_are_the_same_objects():
    for name in ("load_watchlist", "load_dotenv", "ConfigError", "supported_ats"):
        assert getattr(config, name) is getattr(_legacy, name)


def test_public_all_lists_only_the_supported_surface():
    for name in config.__all__:
        assert hasattr(config, name), name
    assert not [name for name in config.__all__ if name.startswith("_")]


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
    assert models.WATCHER_DIR is config.WATCHER_DIR


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


def test_models_module_imports_no_watcher_source_code_at_module_scope():
    """models.py must stay importable without pulling in adapters."""

    import ast

    tree = ast.parse((ROOT / "watcher/config/models.py").read_text(encoding="utf-8"))
    module_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level.append(node.module)
    assert not [m for m in module_level if m.startswith("watcher")], module_level


def test_source_adapters_still_import_company_cfg():
    from watcher.sources.greenhouse import GreenhouseSource  # noqa: F401
    from watcher.sources.workday import WorkdaySource  # noqa: F401
    import watcher.sources.greenhouse as greenhouse

    assert greenhouse.CompanyCfg is models.CompanyCfg


@pytest.mark.parametrize(
    "first",
    [
        "watcher.config",
        "watcher.config.models",
        "watcher.config._legacy",
        "watcher.sources.registry",
        "watcher.sources.greenhouse",
        "watcher.sources.workday",
        "watcher.sources",
    ],
)
def test_no_import_cycle_whichever_module_loads_first(first):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "backend")])
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr


def test_constructing_models_works_when_models_is_imported_alone():
    """The deferred _legacy imports must resolve at construction time."""

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
