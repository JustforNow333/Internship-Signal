"""The neutral domain package owns concepts shared by the watcher and backend.

`internship_signal.domain` is the one package both application layers may
depend on, and it depends on neither. These tests pin that direction, pin the
backend compatibility re-exports as the *same objects* rather than copies, and
pin the moved values against the behaviour recorded before the move.
"""

import ast
import os
import pathlib
import subprocess
import sys

import pytest

import backend.app.dedupe as backend_dedupe
import backend.app.eligibility as backend_eligibility
import backend.app.normalize as backend_normalize
from internship_signal.domain import eligibility as domain_eligibility
from internship_signal.domain import identity as domain_identity
from internship_signal.domain import jobs as domain_jobs

ROOT = pathlib.Path(__file__).resolve().parents[2]
DOMAIN_DIR = ROOT / "internship_signal"


# --------------------------------------------------------------------------
# Canonical job schema
# --------------------------------------------------------------------------

# Captured from the branch before the move; the column set and order are frozen.
EXPECTED_CANONICAL_COLUMNS = [
    "company",
    "title",
    "location",
    "compensation",
    "description",
    "requirements",
    "source_url",
    "date_posted",
    "deadline",
    "remote_status",
    "internship_type",
]


def test_canonical_columns_value_order_and_type_are_unchanged():
    assert domain_jobs.CANONICAL_COLUMNS == EXPECTED_CANONICAL_COLUMNS
    assert isinstance(domain_jobs.CANONICAL_COLUMNS, list)


def test_backend_normalize_reexports_the_same_canonical_columns_object():
    assert backend_normalize.CANONICAL_COLUMNS is domain_jobs.CANONICAL_COLUMNS
    # dedupe reaches it through normalize; still one object, not a copy.
    assert backend_dedupe.CANONICAL_COLUMNS is domain_jobs.CANONICAL_COLUMNS


def test_watcher_row_construction_uses_the_neutral_schema():
    from watcher.sources import rows as watcher_rows

    assert watcher_rows.CANONICAL_COLUMNS is domain_jobs.CANONICAL_COLUMNS

    row = watcher_rows.make_row(source="direct", source_adapter="acme", title="Intern")
    assert [key for key in row if key != "extra"] == EXPECTED_CANONICAL_COLUMNS
    assert row["title"] == "Intern"
    assert row["extra"] == {"source": "direct", "source_adapter": "acme"}


def test_collection_snapshot_uses_the_neutral_schema():
    import watcher.collection_snapshot as snapshot

    assert snapshot.CANONICAL_COLUMNS is domain_jobs.CANONICAL_COLUMNS


# --------------------------------------------------------------------------
# Identity normalization
# --------------------------------------------------------------------------

# Values recorded from the branch before the move.
NORM_COMPANY_CASES = [
    ("ZenithSoft Pvt Ltd", "zenithsoft"),
    ("zenithsoft", "zenithsoft"),
    ("Stripe, Inc.", "stripe"),
    ("Stripe", "stripe"),
    ("Bain & Company", "bain"),
    ("Bain", "bain"),
    ("Ernst & Young LLP", "ernst young llp"),
    ("IBM", "ibm"),
    ("  Spaced   Out  Corp. ", "spaced out"),
    ("3M", "3m"),
    ("AT&T Inc", "at t"),
    ("A.B.C.", "a b c"),
    ("", ""),
    ("   ", ""),
    ("\t\n", ""),
    ("日本電気", ""),
    ("Foo-Bar_Baz", "foo bar baz"),
]

NORM_TITLE_CASES = [
    ("Software Engineer Intern", "software engineer intern"),
    ("SOFTWARE  ENGINEER   INTERN", "software engineer intern"),
    ("Intern - Summer 2027 (Remote)", "intern summer 2027 remote"),
    ("", ""),
    ("   ", ""),
    ("C++ Developer", "c developer"),
    ("Data/ML Intern", "data ml intern"),
]


@pytest.mark.parametrize(("raw", "expected"), NORM_COMPANY_CASES)
def test_norm_company_is_unchanged(raw, expected):
    assert domain_identity.norm_company(raw) == expected


@pytest.mark.parametrize(("raw", "expected"), NORM_TITLE_CASES)
def test_norm_title_is_unchanged(raw, expected):
    assert domain_identity.norm_title(raw) == expected


def test_norm_url_rules_are_unchanged():
    # The cases already covered by backend/tests/test_dedupe.py, run against the
    # neutral definition.
    norm_url = domain_identity.norm_url
    assert norm_url(
        "https://careers.datadoghq.com/intern-platform?utm_source=linkedin&ref=board"
    ) == norm_url("https://careers.datadoghq.com/intern-platform/")
    assert norm_url("https://example.com/job?department=eng&id=123") == norm_url(
        "https://example.com/job?id=123&department=eng"
    )
    assert norm_url("https://example.test/jobs?gh_jid=7895562&gh_jid=7895562") == (
        "https://example.test/jobs?gh_jid=7895562"
    )
    assert norm_url("https://example.test/jobs?team=1&team=2") != norm_url(
        "https://example.test/jobs?team=1"
    )
    base = "https://job-boards.greenhouse.io/acme/jobs/123"
    assert norm_url(f"{base}#/job/ABC123") == norm_url(f"{base}#apply")
    assert norm_url("") == ""
    assert norm_url("   ") == ""


@pytest.mark.parametrize("name", ["norm_company", "norm_title", "norm_url", "_squash"])
def test_backend_dedupe_reexports_the_same_function_object(name):
    assert getattr(backend_dedupe, name) is getattr(domain_identity, name)


@pytest.mark.parametrize(
    "module_name",
    [
        "watcher.alumni",
        "watcher.source_comparison",
        "watcher.health.state",
        "watcher.audit",
        "watcher.audit_trace",
    ],
)
def test_watcher_identity_consumers_use_the_neutral_function(module_name):
    import importlib

    module = importlib.import_module(module_name)
    assert module.norm_company is domain_identity.norm_company


def test_watcher_url_consumers_use_the_neutral_function():
    import watcher.audit_trace as audit_trace
    import watcher.sources.github_markdown_table as markdown_table

    assert markdown_table.norm_url is domain_identity.norm_url
    assert audit_trace.norm_url is domain_identity.norm_url


def test_backend_dedupe_keeps_its_own_location_normalizer():
    # norm_location is not used across the boundary, so it stayed in the
    # backend and still shares the neutral _squash primitive.
    assert backend_dedupe.norm_location("New York, NY") == "new york"
    assert not hasattr(domain_identity, "norm_location")


# --------------------------------------------------------------------------
# Eligibility reason codes
# --------------------------------------------------------------------------

EXPECTED_EXCLUSION_REASONS = frozenset(
    {"phd_only", "graduate_only", "freshman_only", "returning_intern_only"}
)


def test_categorical_exclusion_reasons_are_unchanged():
    assert domain_eligibility.CATEGORICAL_EXCLUSION_REASONS == EXPECTED_EXCLUSION_REASONS
    assert isinstance(domain_eligibility.CATEGORICAL_EXCLUSION_REASONS, frozenset)
    assert domain_eligibility.PHD_ONLY == "phd_only"
    assert domain_eligibility.GRADUATE_ONLY == "graduate_only"
    assert domain_eligibility.FRESHMAN_ONLY == "freshman_only"
    assert domain_eligibility.RETURNING_INTERN_ONLY == "returning_intern_only"


@pytest.mark.parametrize(
    "name",
    [
        "CATEGORICAL_EXCLUSION_REASONS",
        "PHD_ONLY",
        "GRADUATE_ONLY",
        "FRESHMAN_ONLY",
        "RETURNING_INTERN_ONLY",
    ],
)
def test_backend_eligibility_reexports_the_same_objects(name):
    assert getattr(backend_eligibility, name) is getattr(domain_eligibility, name)


def test_watcher_eligibility_reads_the_neutral_reason_set():
    import watcher.eligibility as watcher_eligibility

    assert (
        watcher_eligibility.CATEGORICAL_EXCLUSION_REASONS
        is domain_eligibility.CATEGORICAL_EXCLUSION_REASONS
    )


@pytest.mark.parametrize("reason", sorted(EXPECTED_EXCLUSION_REASONS))
def test_watcher_gate_still_excludes_each_categorical_reason(reason):
    from watcher.eligibility import determine_watcher_eligibility

    job = {
        "company": "Acme",
        "title": "Software Engineer Intern",
        "location": "Boston, MA",
        "score": {"role_track": "backend", "fit_score": 80, "total": 80},
        "role_classification": {"role": "swe", "role_track": "backend"},
        "student_eligibility": {"exclusion_reason": reason},
    }

    excluded = determine_watcher_eligibility(job)
    assert excluded["watcher_eligible"] is False
    assert excluded["ineligible_reason"] == reason
    assert excluded["eligibility_exclusion_reason"] == reason

    # With categorical restrictions off, the same reason no longer excludes.
    relaxed = determine_watcher_eligibility(job, apply_student_restrictions=False)
    assert relaxed["ineligible_reason"] != reason


# --------------------------------------------------------------------------
# Dependency direction
# --------------------------------------------------------------------------

def domain_modules():
    return sorted(DOMAIN_DIR.rglob("*.py"))


def test_the_domain_package_has_the_expected_modules():
    names = {path.relative_to(DOMAIN_DIR).as_posix() for path in domain_modules()}
    assert names == {
        "__init__.py",
        "domain/__init__.py",
        "domain/eligibility.py",
        "domain/identity.py",
        "domain/jobs.py",
    }


@pytest.mark.parametrize(
    "path", domain_modules(), ids=lambda p: p.relative_to(DOMAIN_DIR).as_posix()
)
def test_domain_modules_never_import_backend_or_watcher(path):
    """Static AST check, so a lazy in-function import cannot slip through."""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"backend", "watcher", "app", "flask", "fastapi", "sqlalchemy"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [
                a.name for a in node.names if a.name.split(".")[0] in forbidden
            ]
        elif isinstance(node, ast.ImportFrom):
            # A relative import cannot escape the domain package.
            if node.level == 0 and node.module:
                if node.module.split(".")[0] in forbidden:
                    offenders.append(node.module)
    assert offenders == [], f"{path} imports {offenders}"


@pytest.mark.parametrize(
    "module",
    [
        "internship_signal",
        "internship_signal.domain",
        "internship_signal.domain.eligibility",
        "internship_signal.domain.identity",
        "internship_signal.domain.jobs",
    ],
)
def test_domain_modules_import_cleanly_and_pull_in_no_application_code(module):
    """Import each module first in a clean interpreter, with nothing preloaded."""

    code = (
        f"import {module}, sys;"
        "loaded = {n.split('.')[0] for n in sys.modules};"
        "heavy = loaded & {'backend', 'watcher', 'app', 'flask', 'fastapi', 'sqlalchemy'};"
        "assert not heavy, heavy;"
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


@pytest.mark.parametrize(
    "first",
    [
        "internship_signal.domain.identity",
        "backend.app.dedupe",
        "backend.app.normalize",
        "backend.app.eligibility",
        "watcher.sources.rows",
        "watcher.eligibility",
        "watcher.health.state",
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


def test_watcher_no_longer_imports_backend_for_the_migrated_concepts():
    """The migrated concepts must not come from backend.app anywhere in watcher."""

    migrated = {
        "CANONICAL_COLUMNS",
        "CATEGORICAL_EXCLUSION_REASONS",
        "norm_company",
        "norm_title",
        "norm_url",
    }
    offenders = []
    for path in sorted((ROOT / "watcher").rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("backend"):
                for alias in node.names:
                    if alias.name in migrated:
                        rel = path.relative_to(ROOT).as_posix()
                        offenders.append(f"{rel}:{node.lineno} {alias.name}")
    assert offenders == []
