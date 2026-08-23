"""Watchlist loading and dependency-direction tests.

These pin the accepted YAML shape, the configuration objects it produces, and
the exact errors it raises, so the extraction cannot quietly change what a
watchlist means. Validation rules live in `validation.py`; these tests assert
the loader calls that owner directly.
"""

import ast
import dataclasses
import os
import pathlib
import subprocess
import sys

import pytest

import watcher.config as config
import watcher.config.loader as loader
import watcher.config.models as models
import watcher.config.validation as validation

ROOT = pathlib.Path(__file__).resolve().parents[2]
HEAD = 'defaults:\n  terms: ["Summer 2027"]\n'


def write(tmp_path, text):
    path = tmp_path / "watchlist.yml"
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path, text):
    return config.load_watchlist(write(tmp_path, text))


# --------------------------------------------------------------------------
# Ownership and compatibility
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name", ["load_watchlist", "DEFAULT_WATCHLIST_PATH", "_parse_watchlist_yaml"]
)
def test_the_public_path_still_resolves_to_the_loader(name):
    assert getattr(config, name) is getattr(loader, name)


def test_the_loader_calls_the_validation_owner_directly():

    for name in (
        "_platform_family",
        "_validate_aliases",
        "_validate_company_entry",
        "_validate_company_identity",
        "_validate_coverage_status",
        "_validate_default_terms_present",
        "_validate_github_source_uniqueness",
        "_validate_github_listing_sources_value",
        "_validate_unique_company_names",
        "_validate_oracle_hcm_config",
        "_validate_icims_config",
        "_validate_paylocity_config",
        "_validate_platform_family_mode",
        "_validate_successfactors_config",
        "_validate_talentbrew_config",
        "_validate_terms_tuple",
        "_validate_token_config",
        "_validate_watchlist_sections",
        "_validate_workday_config",
        "_validated_github_listing_urls",
        "_validated_github_source_fields",
        "_validated_min_score",
    ):
        assert getattr(loader, name) is getattr(validation, name), name


def test_validation_rules_are_not_duplicated_into_the_loader():
    source = (ROOT / "watcher/config/loader.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    assert not [name for name in defined if name.startswith("_validate")]
    assert "is_valid_hostname" not in defined
    assert "supported_ats" not in defined
    for validation_message in (
        "entries require token",
        "coverage_status must be one of",
        "platform_family must be",
        "defaults.min_score must be",
        "aliases may not contain",
        "duplicate feed identities",
    ):
        assert validation_message not in source


# --------------------------------------------------------------------------
# Ordinary direct sources
# --------------------------------------------------------------------------

def test_ordinary_direct_source_builds_the_expected_company(tmp_path):
    cfg = load(tmp_path, HEAD + "companies:\n  - name: Acme\n    ats: greenhouse\n    token: acme\n")

    assert len(cfg.companies) == 1
    company = cfg.companies[0]
    assert isinstance(company, models.CompanyCfg)
    assert company.name == "Acme"
    assert company.ats == "greenhouse"
    assert company.token == "acme"
    assert company.terms == ("Summer 2027",)
    assert company.workday_detail_policy == "internship_candidates"
    assert company.aliases == ()
    assert company.coverage_status == ""
    assert cfg.terms == ("Summer 2027",)
    assert cfg.target_roles == frozenset({"swe"})
    assert cfg.min_score is None


@pytest.mark.parametrize("ats", ["greenhouse", "lever", "ashby", "smartrecruiters", "workable"])
def test_token_backed_sources_require_a_token(tmp_path, ats):
    with pytest.raises(config.ConfigError, match=f"Acme: {ats} entries require token"):
        load(tmp_path, HEAD + f"companies:\n  - name: Acme\n    ats: {ats}\n")


# --------------------------------------------------------------------------
# Workday
# --------------------------------------------------------------------------

def test_workday_entry_carries_every_configured_field(tmp_path):
    cfg = load(tmp_path, HEAD + '''companies:
  - name: Wd
    ats: workday
    token: wd
    workday_shard: wd1
    workday_site: Careers
''')
    company = cfg.companies[0]
    assert (company.ats, company.token) == ("workday", "wd")
    assert (company.workday_shard, company.workday_site) == ("wd1", "Careers")
    assert company.workday_detail_policy == config.WORKDAY_DETAIL_INTERNSHIP


def test_workday_detail_policy_can_be_set_explicitly(tmp_path):
    cfg = load(tmp_path, HEAD + '''companies:
  - name: Wd
    ats: workday
    token: wd
    workday_shard: wd1
    workday_site: Careers
    workday_detail_policy: early_career_candidates
''')
    assert cfg.companies[0].workday_detail_policy == config.WORKDAY_DETAIL_EARLY_CAREER


@pytest.mark.parametrize(
    ("missing", "message"),
    [("token", "workday entries require token"),
     ("workday_shard", "workday entries require workday_shard"),
     ("workday_site", "workday entries require workday_site")],
)
def test_workday_requires_its_fields(tmp_path, missing, message):
    fields = {"token": "wd", "workday_shard": "wd1", "workday_site": "Careers"}
    del fields[missing]
    body = "".join(f"    {k}: {v}\n" for k, v in fields.items())
    with pytest.raises(config.ConfigError, match=message):
        load(tmp_path, HEAD + f"companies:\n  - name: Wd\n    ats: workday\n{body}")


def test_workday_rejects_an_unknown_detail_policy(tmp_path):
    with pytest.raises(config.ConfigError, match="workday_detail_policy must be one of"):
        load(tmp_path, HEAD + '''companies:
  - name: Wd
    ats: workday
    token: wd
    workday_shard: wd1
    workday_site: Careers
    workday_detail_policy: bogus
''')


# --------------------------------------------------------------------------
# Non-direct modes
# --------------------------------------------------------------------------

def test_bespoke_entry_loads_with_module_and_source_url(tmp_path):
    cfg = load(tmp_path, HEAD + '''companies:
  - name: Bespoke Co
    ats: bespoke
    module: custom.module
    source_url: "https://bespoke.test/careers"
''')
    company = cfg.companies[0]
    assert company.ats == "bespoke"
    assert company.module == "custom.module"
    assert company.source_url == "https://bespoke.test/careers"


def test_github_only_entry_needs_nothing_else(tmp_path):
    cfg = load(tmp_path, HEAD + "companies:\n  - name: GH Only\n    ats: github_only\n")
    assert cfg.companies[0].ats == "github_only"
    assert cfg.companies[0].token == ""


def test_non_direct_modes_accept_coverage_status_and_platform_family(tmp_path):
    cfg = load(tmp_path, HEAD + '''companies:
  - name: Bespoke Co
    ats: bespoke
    coverage_status: no_source_found
    platform_family: "Some  Platform"
''')
    company = cfg.companies[0]
    assert company.coverage_status == "no_source_found"
    # Interior whitespace is collapsed, exactly as before.
    assert company.platform_family == "Some Platform"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [("coverage_status", "no_source_found", "requires bespoke or github_only ats"),
     ("platform_family", "thing", "platform_family requires bespoke or github_only ats")],
)
def test_direct_sources_reject_non_direct_only_fields(tmp_path, field, value, message):
    with pytest.raises(config.ConfigError, match=message):
        load(tmp_path, HEAD + f'''companies:
  - name: Acme
    ats: greenhouse
    token: acme
    {field}: {value}
''')


def test_unsupported_ats_is_rejected_through_the_registry(tmp_path):
    with pytest.raises(config.ConfigError, match="Acme: unsupported ats 'nope'"):
        load(tmp_path, HEAD + "companies:\n  - name: Acme\n    ats: nope\n")


def test_every_registered_direct_adapter_and_non_direct_mode_stays_accepted():
    from watcher.sources.registry import DIRECT_ATS

    assert config.supported_ats() == DIRECT_ATS | config.NON_DIRECT_ATS
    assert config.NON_DIRECT_ATS == frozenset({"bespoke", "github_only"})


# --------------------------------------------------------------------------
# GitHub listing feeds
# --------------------------------------------------------------------------

GITHUB_FEEDS = '''defaults:
  terms: ["Summer 2027"]
  github_listing_sources:
    - name: simplify
      format: simplify_json
      url: "https://raw.test/listings.json"
    - name: table
      format: github_markdown_table
      url: "https://raw.test/README.md"
      default_term: "Summer 2027"
  github_listing_urls: ["https://raw.test/legacy.json"]
companies:
  - name: GH Only
    ats: github_only
'''


def test_typed_and_legacy_github_feeds_load_together(tmp_path):
    cfg = load(tmp_path, GITHUB_FEEDS)

    assert [s.name for s in cfg.github_listing_sources] == ["simplify", "table"]
    assert [s.format for s in cfg.github_listing_sources] == [
        "simplify_json", "github_markdown_table"
    ]
    assert cfg.github_listing_sources[1].default_term == "Summer 2027"
    assert cfg.github_listing_urls == ("https://raw.test/legacy.json",)

    merged = cfg.effective_github_listing_sources()
    assert len(merged) == 3
    # Deterministic order: priority, then case-folded name, then url.
    assert [s.priority for s in merged] == sorted(s.priority for s in merged)
    legacy = [s for s in merged if s.name.startswith("legacy_simplify_")]
    assert len(legacy) == 1
    assert legacy[0].format == "simplify_json"
    assert legacy[0].url == "https://raw.test/legacy.json"


def test_legacy_feed_names_are_a_stable_digest_of_the_url(tmp_path):
    first = load(tmp_path, GITHUB_FEEDS).effective_github_listing_sources()
    second = load(tmp_path, GITHUB_FEEDS).effective_github_listing_sources()
    assert [s.name for s in first] == [s.name for s in second]


def test_markdown_table_feeds_require_a_default_term(tmp_path):
    with pytest.raises(config.ConfigError, match="default_term is required"):
        load(tmp_path, '''defaults:
  terms: ["Summer 2027"]
  github_listing_sources:
    - name: table
      format: github_markdown_table
      url: "https://raw.test/README.md"
companies:
  - name: GH Only
    ats: github_only
''')


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("ftp://raw.test/a.json", "invalid HTTP/HTTPS URL"),
        ("https://[broken/a.json", "invalid HTTP/HTTPS URL"),
        ("https://user:secret@raw.test/a.json", "must not contain URL credentials"),
        ("not-a-url", "invalid HTTP/HTTPS URL"),
    ],
)
def test_invalid_feed_urls_are_rejected(tmp_path, url, message):
    with pytest.raises(config.ConfigError, match=message):
        load(tmp_path, f'''defaults:
  terms: ["Summer 2027"]
  github_listing_urls: ["{url}"]
companies:
  - name: GH Only
    ats: github_only
''')


def test_duplicate_feed_names_and_identities_are_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match="duplicate source name"):
        load(tmp_path, '''defaults:
  terms: ["Summer 2027"]
  github_listing_sources:
    - name: dup
      format: simplify_json
      url: "https://raw.test/a.json"
    - name: DUP
      format: simplify_json
      url: "https://raw.test/b.json"
companies:
  - name: GH Only
    ats: github_only
''')
    with pytest.raises(config.ConfigError, match="duplicate feed identities"):
        load(tmp_path, '''defaults:
  terms: ["Summer 2027"]
  github_listing_sources:
    - name: one
      format: simplify_json
      url: "https://raw.test/a.json?x=1"
    - name: two
      format: simplify_json
      url: "https://raw.test/a.json?x=2"
companies:
  - name: GH Only
    ats: github_only
''')


def test_unknown_feed_format_is_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match="format must be one of"):
        load(tmp_path, '''defaults:
  terms: ["Summer 2027"]
  github_listing_sources:
    - name: x
      format: bogus
      url: "https://raw.test/a.json"
companies:
  - name: GH Only
    ats: github_only
''')


# --------------------------------------------------------------------------
# Defaults, optional values, and company overrides
# --------------------------------------------------------------------------

def test_optional_defaults_are_applied(tmp_path):
    cfg = load(tmp_path, '''defaults:
  terms: ["Summer 2027", "Fall 2027"]
  target_roles: ["swe", "data"]
  min_score: 40
companies:
  - name: GH Only
    ats: github_only
''')
    assert cfg.terms == ("Summer 2027", "Fall 2027")
    assert cfg.target_roles == frozenset({"swe", "data"})
    assert cfg.min_score == 40
    # Company terms fall back to the defaults.
    assert cfg.companies[0].terms == ("Summer 2027", "Fall 2027")


def test_company_terms_override_the_defaults(tmp_path):
    cfg = load(tmp_path, HEAD + '''companies:
  - name: Acme
    ats: greenhouse
    token: acme
    terms: ["Winter 2028"]
''')
    assert cfg.terms == ("Summer 2027",)
    assert cfg.companies[0].terms == ("Winter 2028",)


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_min_score_is_none(tmp_path, blank):
    cfg = load(tmp_path, f'defaults:\n  terms: ["Summer 2027"]\n  min_score: {blank}\ncompanies:\n  - name: X\n    ats: github_only\n')
    assert cfg.min_score is None


def test_non_integer_min_score_is_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match="defaults.min_score must be an integer when set"):
        load(tmp_path, 'defaults:\n  terms: ["Summer 2027"]\n  min_score: "forty"\ncompanies:\n  - name: X\n    ats: github_only\n')


def test_aliases_and_alumni_match_load_in_order(tmp_path):
    cfg = load(tmp_path, HEAD + '''companies:
  - name: Acme
    ats: greenhouse
    token: acme
    aliases: ["Acme Labs", "AcmeCo"]
    alumni_match: ["acme", "acme labs"]
''')
    company = cfg.companies[0]
    assert company.aliases == ("Acme Labs", "AcmeCo")
    assert company.alumni_match == ("acme", "acme labs")
    assert company.match_names() == ("Acme", "Acme Labs", "AcmeCo")


@pytest.mark.parametrize(
    ("aliases", "message"),
    [('["", "x"]', "aliases may not contain blank values"),
     ('["Acme Inc", "acme inc."]', "normalize to the same value")],
)
def test_invalid_aliases_are_rejected(tmp_path, aliases, message):
    with pytest.raises(config.ConfigError, match=message):
        load(tmp_path, HEAD + f'''companies:
  - name: Acme
    ats: greenhouse
    token: acme
    aliases: {aliases}
''')


def test_ambiguous_company_names_are_rejected(tmp_path):
    with pytest.raises(config.ConfigError, match="is ambiguous between"):
        load(tmp_path, HEAD + '''companies:
  - name: Acme
    ats: greenhouse
    token: a
  - name: acme
    ats: greenhouse
    token: b
''')


# --------------------------------------------------------------------------
# Environment-derived fields on the loaded config
# --------------------------------------------------------------------------

def test_loaded_config_carries_the_environment_settings(tmp_path):
    cfg = load(tmp_path, HEAD + "companies:\n  - name: X\n    ats: github_only\n")

    assert cfg.seen_db_path == config.DEFAULT_SEEN_DB_PATH
    assert cfg.analysis_cache_enabled is config.analysis_cache_enabled()
    assert cfg.analysis_cache_path == config.resolve_analysis_cache_path(
        config.DEFAULT_SEEN_DB_PATH
    )
    assert cfg.collection_concurrency == config.load_collection_concurrency()


# --------------------------------------------------------------------------
# Structural and malformed input
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('defaults:\n  target_roles: ["swe"]\ncompanies:\n  - name: X\n    ats: github_only\n',
         "defaults.terms must explicitly define at least one nonblank term"),
        ('defaults:\n  terms: ["  "]\ncompanies:\n  - name: X\n    ats: github_only\n',
         "defaults.terms must define at least one nonblank term"),
        ("defaults:\n  terms: []\ncompanies:\n  - name: X\n    ats: github_only\n",
         "defaults.terms must define at least one nonblank term"),
        (HEAD, "watchlist must define at least one company"),
        ("", "watchlist must define at least one company"),
        (HEAD + "companies:\n  - ats: github_only\n", "company entry missing name"),
        ("!!!not yaml at all\n", "Unknown watchlist line"),
        ('defaults: "nope"\ncompanies:\n  - name: X\n    ats: github_only\n',
         "Unknown watchlist line"),
    ],
)
def test_structural_errors_are_unchanged(tmp_path, text, message):
    with pytest.raises(config.ConfigError, match=message):
        load(tmp_path, text)


def test_a_missing_watchlist_file_reports_its_path(tmp_path):
    missing = tmp_path / "absent.yml"
    with pytest.raises(config.ConfigError, match=f"Watchlist not found: {missing}".replace("\\", "\\\\")):
        config.load_watchlist(missing)


# --------------------------------------------------------------------------
# The YAML subset parser
# --------------------------------------------------------------------------

def test_yaml_subset_parses_scalars_and_inline_lists():
    parsed = config._parse_watchlist_yaml(
        'defaults:\n'
        '  a: 1\n'
        '  b: true\n'
        '  c: false\n'
        '  d: null\n'
        '  e: text\n'
        '  f: "quoted"\n'
        '  terms: ["a", "b"]\n'
        '  empty: []\n'
    )
    assert parsed["defaults"] == {
        "a": 1, "b": True, "c": False, "d": None,
        "e": "text", "f": "quoted", "terms": ["a", "b"], "empty": [],
    }


def test_yaml_subset_handles_comments_and_quoted_hashes():
    parsed = config._parse_watchlist_yaml(
        'defaults:\n  # a comment\n  terms: ["a"]  # trailing\n'
    )
    assert parsed["defaults"]["terms"] == ["a"]
    quoted = config._parse_watchlist_yaml('defaults:\n  terms: ["a # b"]\n')
    assert quoted["defaults"]["terms"] == ["a # b"]


def test_yaml_subset_reads_company_and_nested_default_lists():
    parsed = config._parse_watchlist_yaml(
        "defaults:\n"
        "  github_listing_sources:\n"
        "    - name: x\n"
        "      format: simplify_json\n"
        "companies:\n"
        "  - name: A\n    ats: github_only\n"
        "  - name: B\n    ats: bespoke\n"
    )
    assert parsed["defaults"]["github_listing_sources"] == [
        {"name": "x", "format": "simplify_json"}
    ]
    assert parsed["companies"] == [
        {"name": "A", "ats": "github_only"},
        {"name": "B", "ats": "bespoke"},
    ]


# --------------------------------------------------------------------------
# The real production watchlist
# --------------------------------------------------------------------------

def test_the_real_watchlist_loads_the_same_through_every_path():
    explicit = config.load_watchlist(config.DEFAULT_WATCHLIST_PATH)
    implicit = config.load_watchlist()
    direct = loader.load_watchlist(config.DEFAULT_WATCHLIST_PATH)

    assert explicit == implicit == direct
    assert explicit.companies
    assert all(isinstance(c, models.CompanyCfg) for c in explicit.companies)
    # Every entry uses a supported ats value.
    supported = config.supported_ats()
    assert {c.ats for c in explicit.companies} <= supported


def test_the_real_watchlist_round_trips_through_dataclass_fields():
    cfg = config.load_watchlist()
    rebuilt = tuple(
        models.CompanyCfg(**dataclasses.asdict(company)) for company in cfg.companies
    )
    assert rebuilt == cfg.companies


# --------------------------------------------------------------------------
# Import direction
# --------------------------------------------------------------------------

def test_validation_does_not_import_the_loader():
    """Validation stays below loading and never creates config/source cycles."""

    tree = ast.parse((ROOT / "watcher/config/validation.py").read_text(encoding="utf-8"))
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules += [a.name for a in node.names]
    assert "watcher.config.loader" not in modules, modules
    assert "loader" not in modules, modules


def test_transitional_config_module_is_removed_and_unreferenced():
    transitional = "_" + "legacy"
    assert not (ROOT / "watcher" / "config" / f"{transitional}.py").exists()

    for path in (ROOT / "watcher" / "config").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
            elif isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
        assert f"watcher.config.{transitional}" not in imports, path


def test_config_submodules_import_owners_without_using_the_facade():
    expected_relative_dependencies = {
        "env.py": {"models"},
        "models.py": {"env", "loader"},
        "validation.py": {"env", "models"},
        "loader.py": {"env", "models", "validation"},
    }

    for filename, expected in expected_relative_dependencies.items():
        tree = ast.parse(
            (ROOT / "watcher" / "config" / filename).read_text(encoding="utf-8")
        )
        relative = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
        }
        absolute_facade_imports = [
            node
            for node in ast.walk(tree)
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "watcher.config"
            )
            or (
                isinstance(node, ast.Import)
                and any(alias.name == "watcher.config" for alias in node.names)
            )
        ]
        assert relative == expected, filename
        assert absolute_facade_imports == [], filename


@pytest.mark.parametrize(
    "first",
    [
        "watcher.config",
        "watcher.config.loader",
        "watcher.config.validation",
        "watcher.config.models",
        "watcher.config.env",
        "watcher.sources.registry",
    ],
)
def test_no_import_cycle_whichever_module_loads_first(first):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "backend")])
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}"],
        capture_output=True, text=True, cwd=str(ROOT), env=env,
    )
    assert result.returncode == 0, result.stderr
