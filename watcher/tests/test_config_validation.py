"""Validation ownership, behavior, compatibility, and boundary tests."""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

import watcher.config as config


ROOT = Path(__file__).resolve().parents[2]
HEAD = 'defaults:\n  terms: ["Summer 2027"]\n'

VALID_COMPANY_FIELDS = {
    "ashby": '    token: "example"\n',
    "bain": "",
    "epic": "",
    "greenhouse": '    token: "example"\n',
    "ibm": "",
    "icims": (
        "    icims_variant: jibe_json\n"
        '    icims_host: "jobs.example.test"\n'
        '    source_url: "https://jobs.example.test/jobs"\n'
    ),
    "lever": '    token: "example"\n',
    "oracle_hcm": (
        '    oracle_hcm_host: "example.fa.oraclecloud.com"\n'
        '    oracle_hcm_site: "CX_1"\n'
        '    source_url: "https://example.fa.oraclecloud.com/hcmUI/'
        'CandidateExperience/en/sites/CX_1/jobs"\n'
    ),
    "paylocity": (
        '    paylocity_company_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"\n'
        '    paylocity_module_id: "1"\n'
        '    paylocity_slug: "Example"\n'
        '    source_url: "https://recruiting.paylocity.com/recruiting/jobs/All/'
        'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/Example"\n'
    ),
    "smartrecruiters": '    token: "Example"\n',
    "successfactors": (
        '    successfactors_host: "careers.example.test"\n'
        '    successfactors_site_prefix: "Example"\n'
        '    successfactors_locale: "en_US"\n'
        '    source_url: "https://careers.example.test/Example/"\n'
    ),
    "talentbrew": (
        '    talentbrew_host: "careers.example.test"\n'
        '    talentbrew_site_id: "123"\n'
        '    talentbrew_category_id: "456"\n'
        '    talentbrew_category_name: "Early Careers"\n'
        '    source_url: "https://careers.example.test/search-jobs"\n'
    ),
    "workable": '    token: "example"\n',
    "workday": (
        '    token: "example"\n'
        '    workday_shard: "wd1"\n'
        '    workday_site: "Careers"\n'
    ),
    "bespoke": '    module: "example"\n',
    "github_only": "",
}


def _load(tmp_path: Path, companies: str, defaults: str = ""):
    path = tmp_path / "watchlist.yml"
    path.write_text(HEAD + defaults + "companies:\n" + companies, encoding="utf-8")
    return config.load_watchlist(path)


def _company(name: str, ats: str, fields: str = "") -> str:
    return f"  - name: {name}\n    ats: {ats}\n{fields}"


@pytest.mark.parametrize(
    "name",
    (
        "NON_DIRECT_ATS",
        "SUPPORTED_GITHUB_LISTING_FORMATS",
        "is_valid_hostname",
        "supported_ats",
    ),
)
def test_public_validation_symbols_are_direct_reexports(name):
    from watcher.config import validation

    assert getattr(config, name) is getattr(validation, name)


@pytest.mark.parametrize("ats", sorted(VALID_COMPANY_FIELDS))
def test_every_supported_ats_and_non_direct_mode_is_accepted(tmp_path, ats):
    loaded = _load(tmp_path, _company("Example", ats, VALID_COMPANY_FIELDS[ats]))

    assert loaded.companies[0].ats == ats


def test_acceptance_matrix_exactly_matches_registry_backed_supported_ats():
    from watcher.sources.registry import DIRECT_ATS

    assert set(VALID_COMPANY_FIELDS) == set(config.supported_ats())
    assert config.supported_ats() == DIRECT_ATS | config.NON_DIRECT_ATS
    assert config.NON_DIRECT_ATS == frozenset({"bespoke", "github_only"})
    assert not DIRECT_ATS & config.NON_DIRECT_ATS


def test_source_registry_import_remains_deferred_inside_supported_ats():
    tree = ast.parse(
        (ROOT / "watcher/config/validation.py").read_text(encoding="utf-8")
    )
    module_imports = {
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "supported_ats"
    )
    deferred_imports = {
        node.module
        for node in ast.walk(function)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "watcher.sources.registry" not in module_imports
    assert "watcher.sources.registry" in deferred_imports


@pytest.mark.parametrize(
    "ats", ("ashby", "greenhouse", "lever", "smartrecruiters", "workable")
)
def test_token_backed_sources_keep_exact_missing_token_error(tmp_path, ats):
    with pytest.raises(config.ConfigError) as caught:
        _load(tmp_path, _company("Example", ats))

    assert str(caught.value) == f"Example: {ats} entries require token"


@pytest.mark.parametrize(
    ("ats", "expected"),
    (
        ("workday", "Example: workday entries require token"),
        ("oracle_hcm", "Example: oracle_hcm entries require oracle_hcm_host"),
        ("talentbrew", "Example: talentbrew entries require a valid talentbrew_host"),
        ("icims", "Example: icims_variant must be one of: classic, jibe_json"),
        ("successfactors", "Example: successfactors_host must be a hostname"),
        ("paylocity", "Example: paylocity_company_id must be a lower-case UUID"),
    ),
)
def test_multi_field_invalid_entries_keep_first_error(tmp_path, ats, expected):
    with pytest.raises(config.ConfigError) as caught:
        _load(tmp_path, _company("Example", ats))

    assert str(caught.value) == expected


def test_unknown_ats_keeps_exact_error(tmp_path):
    with pytest.raises(config.ConfigError) as caught:
        _load(tmp_path, _company("Example", "unknown"))

    assert str(caught.value) == "Example: unsupported ats 'unknown'"


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("jobs.example.com", True),
        ("jobs.example.com.", False),
        ("Jobs.example.com", False),
        ("https://jobs.example.com", False),
        ("jobs..example.com", False),
        ("", False),
    ),
)
def test_hostname_validation_outputs_are_unchanged(value, expected):
    assert config.is_valid_hostname(value) is expected


def test_github_source_validation_keeps_field_order_and_exact_message(tmp_path):
    defaults = (
        "  github_listing_sources:\n"
        "    - name: feed\n"
        "      format: invalid\n"
        "      url: not-a-url\n"
    )
    with pytest.raises(config.ConfigError) as caught:
        _load(tmp_path, _company("Example", "github_only"), defaults)

    assert str(caught.value) == (
        "defaults.github_listing_sources[1].format must be one of: "
        "github_markdown_table, simplify_json"
    )


def test_github_source_name_and_identity_conflicts_are_exact(tmp_path):
    duplicate_names = (
        "  github_listing_sources:\n"
        "    - name: Feed\n"
        "      format: simplify_json\n"
        "      url: https://example.test/one.json\n"
        "    - name: feed\n"
        "      format: simplify_json\n"
        "      url: https://example.test/two.json\n"
    )
    with pytest.raises(config.ConfigError) as caught:
        _load(tmp_path, _company("Example", "github_only"), duplicate_names)
    assert str(caught.value) == (
        "defaults.github_listing_sources contains duplicate source name: feed"
    )

    duplicate_identity = (
        "  github_listing_sources:\n"
        "    - name: one\n"
        "      format: simplify_json\n"
        "      url: https://example.test/feed.json?one=1\n"
        "    - name: two\n"
        "      format: simplify_json\n"
        "      url: https://example.test/feed.json#two\n"
    )
    with pytest.raises(config.ConfigError) as caught:
        _load(tmp_path, _company("Example", "github_only"), duplicate_identity)
    assert str(caught.value) == (
        "GitHub listing sources contain duplicate feed identities after removing query or fragment"
    )


def test_company_name_conflict_error_is_exact(tmp_path):
    companies = _company("Acme", "github_only") + _company(
        "acme", "github_only"
    )

    with pytest.raises(config.ConfigError) as caught:
        _load(tmp_path, companies)

    assert str(caught.value) == (
        "watchlist company/alias 'acme' is ambiguous between 'Acme' and 'acme'"
    )


def test_loader_calls_validation_owner_without_duplicating_rules():
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
    source = (ROOT / "watcher/config/loader.py").read_text(encoding="utf-8")

    assert all(getattr(loader, name) is getattr(validation, name) for name in names)
    for message in (
        "entries require token",
        "defaults.min_score must be",
        "aliases may not contain",
        "duplicate feed identities",
    ):
        assert message not in source


def test_validation_stays_below_loader_and_registry_import_is_deferred():
    tree = ast.parse(
        (ROOT / "watcher/config/validation.py").read_text(encoding="utf-8")
    )
    imports = []
    module_imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
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
        "watcher.seen_store",
        "watcher.notify",
        "watcher.config.loader",
    )
    assert not [name for name in imports if name.startswith(prohibited)]
    assert [name for name in imports if name.startswith("watcher.sources")] == [
        "watcher.sources.registry"
    ]
    assert not [name for name in module_imports if name.startswith("watcher.sources")]


@pytest.mark.parametrize(
    "first",
    (
        "watcher.config",
        "watcher.config.loader",
        "watcher.config.validation",
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
