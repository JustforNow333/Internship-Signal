import json

import pytest

from app import config
from app.classify import classify_company, classify_role


def _row(**kw):
    base = {"company": "", "title": "", "location": "", "description": "", "requirements": ""}
    base.update(kw)
    return base


# --- company: layered classification ---------------------------------------

def test_known_list_beats_everything():
    c = classify_company(_row(company="Stripe", description="bakery retail boutique"))
    assert c["category"] == "tech" and c["confidence"] >= 0.9
    assert any("known tech-company list" in e for e in c["evidence"])


def test_known_company_overrides_use_same_normalization(tmp_path, monkeypatch):
    path = tmp_path / "known_companies.json"
    path.write_text(
        json.dumps({"tech": ["Acme, Inc."], "non_tech": [], "reputable": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "KNOWN_COMPANIES_PATH", path)

    known = config.load_known_companies()
    assert "acme" in known["tech"]

    c = classify_company(_row(company="Acme"), known)
    assert c["category"] == "tech"


def test_name_token_ai():
    c = classify_company(_row(company="Nimbus AI", title="ML Intern"))
    assert c["category"] in ("tech", "startup")
    assert any("company name" in e.lower() for e in c["evidence"])


def test_ambiguous_name_resolved_by_description():
    c = classify_company(_row(
        company="Meridian",
        title="Backend Intern",
        description="Seed-funded 8-person team building logistics APIs with Flask and PostgreSQL.",
        requirements="Flask or Django, SQL, Git",
    ), role_is_technical=True)
    assert c["category"] == "startup" and c["is_startup"] is True
    assert c["confidence"] >= 0.5
    assert any("startup" in e.lower() or "team" in e.lower() for e in c["evidence"])


def test_non_tech_resolved_by_description():
    c = classify_company(_row(
        company="Bluebird",
        title="Marketing Intern",
        description="Family bakery and cafe. Help with weekend retail events.",
    ), role_is_technical=False)
    assert c["category"] == "non_tech"


def test_technical_role_blocks_non_tech_verdict():
    # Same bakery-flavored employer, but the role itself is technical:
    # keep it for review instead of writing it off.
    c = classify_company(_row(
        company="Bluebird",
        title="Backend Intern",
        description="Family bakery and cafe building an online ordering site.",
    ), role_is_technical=True)
    assert c["category"] != "non_tech"


def test_blank_company_is_unknown_low_confidence():
    c = classify_company(_row())
    assert c["category"] == "unknown" and c["confidence"] <= 0.3


def test_series_a_language_counts_as_startup():
    c = classify_company(_row(
        company="Kite", title="Product Management Intern",
        description="Series A startup. Ship one feature end-to-end with our engineers.",
    ))
    assert c["category"] == "startup"


# --- roles -------------------------------------------------------------------

def test_role_classification_table():
    cases = [
        (_row(title="Backend Engineering Intern", requirements="Python, SQL"), "swe"),
        (_row(title="Founding Engineer Intern", requirements="React, Node"), "swe"),
        (_row(title="Machine Learning Intern", requirements="PyTorch"), "ml_ai"),
        (_row(title="Research Assistant (ML)",
              description="PyTorch experiment runs and dataset labeling pipelines."), "ml_ai"),
        (_row(title="Quantitative Trading Intern", requirements="Python, NumPy"), "quant"),
        (_row(title="Data Science Intern", requirements="pandas, scikit-learn"), "data_science"),
        (_row(title="IT Support Intern", description="help desk ticket queue"), "it"),
        (_row(title="Marketing Intern", description="run our Instagram"), "non_technical"),
        (_row(title="Product Management Intern",
              description="write specs and run user interviews"), "product"),
    ]
    for row, expected in cases:
        got = classify_role(row)
        assert got["role"] == expected, f'{row["title"]!r}: expected {expected}, got {got["role"]}'
        assert got["evidence"], "evidence must always be returned"


def test_data_entry_is_not_data_science():
    got = classify_role(_row(title="Data Entry Intern",
                             description="Enter supplier invoices into our spreadsheet."))
    assert got["role"] == "non_technical"


def test_unclassifiable_title_is_unknown():
    got = classify_role(_row(title="Team Member"))
    assert got["role"] == "unknown" and got["confidence"] <= 0.3


@pytest.mark.parametrize(
    ("title", "track"),
    [
        ("2027 Electrical Engineer Intern", "electrical_hardware"),
        ("2027 Manufacturing Engineer Intern", "mechanical_manufacturing"),
        ("Mechanical Design Engineer", "mechanical_manufacturing"),
        ("Industrial Engineer Intern", "mechanical_manufacturing"),
        ("Hardware Engineer Intern", "electrical_hardware"),
        ("RF Engineer Intern", "electrical_hardware"),
        ("Test Engineer Intern", "quality_test"),
        ("Quality Engineer Intern", "quality_test"),
        ("Process Engineer Intern", "mechanical_manufacturing"),
        ("Factory Automation Engineering Intern", "factory_automation"),
        ("Civil Engineer Intern", "civil_structural"),
        ("Customer Experience Engineer - Intern", "customer_experience"),
        ("IT Support Intern", "it_support"),
    ],
)
def test_non_swe_engineering_titles_are_not_broad_swe(title, track):
    got = classify_role(_row(title=title))

    assert got["role"] != "swe"
    assert got["role_track"] == track
    assert got["non_swe_evidence"]


@pytest.mark.parametrize(
    ("title", "track", "role"),
    [
        ("2027 Software Engineer Intern", "general_swe", "swe"),
        ("Software Engineer Intern", "general_swe", "swe"),
        ("Backend Engineer Intern", "backend", "swe"),
        ("Full Stack Engineer Intern", "full_stack", "swe"),
        ("Frontend Engineer Intern", "frontend", "swe"),
        ("Platform Software Engineer Intern", "platform_infra", "swe"),
        ("Infrastructure Software Engineer Intern", "platform_infra", "swe"),
        ("Data Engineer Intern", "data_engineering", "data_science"),
        ("Machine Learning Engineer Intern", "ml_ai", "ml_ai"),
        ("Quant Developer Intern", "quant_dev", "quant"),
        ("Embedded Software Engineer Intern", "embedded_software", "swe"),
        ("Firmware Software Engineer Intern", "firmware", "swe"),
        ("SDET Intern", "sdet_qa_automation", "swe"),
        ("Software QA Automation Intern", "sdet_qa_automation", "swe"),
    ],
)
def test_software_adjacent_titles_get_specific_role_tracks(title, track, role):
    got = classify_role(_row(title=title))

    assert got["role"] == role
    assert got["role_track"] == track
    assert got["software_evidence"]


def test_it_backend_java_is_rescued_by_strong_backend_evidence():
    got = classify_role(_row(
        title="IT Internship (BackEnd, Java)",
        description="Build BackEnd services and REST APIs in Java.",
        requirements="Java, SQL, Git",
    ))

    assert got["role"] == "swe"
    assert got["role_track"] == "backend"
    assert any("java" in item.lower() for item in got["software_evidence"])


def test_plain_embedded_engineer_is_not_automatically_software():
    got = classify_role(_row(title="Embedded Engineer Intern"))

    assert got["role"] != "swe"
    assert got["role_track"] == "electrical_hardware"


def test_cloud_and_devops_need_software_ownership_evidence():
    cloud = classify_role(_row(title="Cloud Developer Internship"))
    cloud_with_context = classify_role(_row(
        title="Cloud Developer Internship",
        description="Build cloud APIs and platform services in Python.",
    ))
    devops = classify_role(_row(title="DevOps Engineering Intern"))
    devops_with_context = classify_role(_row(
        title="DevOps Engineering Intern",
        description="Own developer tooling and automation code for backend infrastructure APIs.",
    ))

    assert cloud["role_track"] == "cloud"
    assert cloud_with_context["role_track"] == "cloud"
    assert devops["role_track"] == "devops"
    assert devops_with_context["role_track"] == "devops"
