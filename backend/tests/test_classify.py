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


@pytest.mark.parametrize("payload", [[], {"tech": "Stripe"}, {"tech": 123}])
def test_invalid_known_company_shapes_fall_back_safely(tmp_path, monkeypatch, payload):
    path = tmp_path / "known_companies.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(config, "KNOWN_COMPANIES_PATH", path)

    known = config.load_known_companies()

    assert "stripe" in known["tech"]


def test_invalid_known_company_entries_do_not_become_company_names(
    tmp_path, monkeypatch
):
    path = tmp_path / "known_companies.json"
    path.write_text(
        json.dumps(
            {
                "tech": [{"company": "Fabricated"}],
                "non_tech": [],
                "reputable": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "KNOWN_COMPANIES_PATH", path)

    known = config.load_known_companies()

    assert "stripe" in known["tech"]
    assert "company fabricated" not in known["tech"]


def test_mixed_known_company_entries_fall_back_for_whole_category(
    tmp_path, monkeypatch
):
    path = tmp_path / "known_companies.json"
    path.write_text(
        json.dumps(
            {
                "tech": ["Custom Company", {"company": "Fabricated"}],
                "non_tech": [],
                "reputable": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "KNOWN_COMPANIES_PATH", path)

    known = config.load_known_companies()

    assert "stripe" in known["tech"]
    assert "custom company" not in known["tech"]
    assert "company fabricated" not in known["tech"]


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


@pytest.mark.parametrize(
    "description",
    [
        "Use Python and SQL to analyze data and build API-backed software reports.",
        "Partner with software engineers and apply analytics to strategic planning.",
        "Our company builds APIs, cloud platforms, and machine-learning software.",
    ],
)
def test_business_strategy_title_is_not_rescued_by_technical_body(description):
    got = classify_role(
        _row(
            company="Capital One",
            title="Intern, Strategy Analyst - Summer 2027",
            description=description,
            requirements="Python, SQL, APIs, analytics, and software tools.",
        )
    )

    assert got["role"] == "non_technical"
    assert got["role_track"] == "non_technical"


@pytest.mark.parametrize(
    ("title", "expected_track"),
    [
        ("Software Engineer Intern, Strategy Platform", "general_swe"),
        ("Data Engineer Intern, Corporate Strategy Technology", "data_engineering"),
        ("Machine Learning Engineer Intern, Strategy Systems", "ml_ai"),
        ("Quantitative Developer Intern, Strategy", "quant_dev"),
        ("Technical Product Manager Intern, Product Strategy", "technical_product"),
    ],
)
def test_explicit_technical_titles_with_strategy_remain_technical(
    title,
    expected_track,
):
    got = classify_role(_row(title=title))

    assert got["role_track"] == expected_track


def test_unclassifiable_title_is_unknown():
    got = classify_role(_row(title="Team Member"))
    assert got["role"] == "unknown" and got["confidence"] <= 0.3


@pytest.mark.parametrize(
    "title",
    [
        "Data & AI Intern - Analyst",
        "Data and AI Intern - Analyst",
    ],
)
def test_exact_data_and_ai_intern_analyst_titles_are_ml_ai(title):
    got = classify_role(_row(title=title))

    assert got["role"] == "ml_ai"
    assert got["role_track"] == "ml_ai"


@pytest.mark.parametrize(
    "title",
    [
        "Data & Analytics Intern - Analyst",
        "Data & AI Analyst",
        "AI Marketing Intern - Analyst",
        "Data & AI Intern - Analyst, Operations",
    ],
)
def test_data_and_ai_intern_analyst_override_rejects_nearby_titles(title):
    got = classify_role(_row(title=title))

    assert (got["role"], got["role_track"]) != ("ml_ai", "ml_ai")


@pytest.mark.parametrize(
    "company",
    ["Capital One", "Capital One Financial"],
)
@pytest.mark.parametrize(
    "title",
    [
        "Technology Intern",
        "Technology Internship Program",
        "Technology Internship Program Intern",
    ],
)
def test_capital_one_exact_technology_internship_titles_are_general_swe(
    company,
    title,
):
    got = classify_role(_row(company=company, title=title))

    assert got["role"] == "swe"
    assert got["role_track"] == "general_swe"


@pytest.mark.parametrize("company", ["Capital One", "Capital One Financial"])
def test_capital_one_summer_program_title_is_general_swe(company):
    got = classify_role(_row(
        company=company,
        title="Technology Internship Program - Summer 2027",
    ))

    assert got["role"] == "swe"
    assert got["role_track"] == "general_swe"


def test_capital_one_technology_internship_override_is_company_and_title_exact():
    unrelated_company = classify_role(
        _row(company="Unrelated Company", title="Technology Intern")
    )
    broader_title = classify_role(
        _row(company="Capital One", title="Technology Operations Intern")
    )

    assert unrelated_company["role_track"] == "unknown"
    assert broader_title["role_track"] == "non_technical"


@pytest.mark.parametrize(
    ("company", "title", "expected_track"),
    [
        (
            "Unrelated Company",
            "Technology Internship Program - Summer 2027",
            "unknown",
        ),
        (
            "Capital One",
            "Technology Operations Internship Program - Summer 2027",
            "non_technical",
        ),
        (
            "Capital One",
            "Technology Internship Program - Summer 2027 Leadership",
            "unknown",
        ),
    ],
)
def test_capital_one_summer_program_override_rejects_nearby_titles(
    company,
    title,
    expected_track,
):
    got = classify_role(_row(company=company, title=title))

    assert got["role_track"] == expected_track
    assert not any(
        "Capital One technology internship title" in evidence
        for evidence in got["evidence"]
    )


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


@pytest.mark.parametrize(
    ("title", "track"),
    [
        ("A.I. Integration Intern", "ml_ai"),
        ("AI-Integration Internship", "ml_ai"),
        ("Markets Quantitative Analyst Summer Internship", "quant_dev"),
        ("Systematic Investing Intern", "quant_research"),
        ("Associate Product Manager Intern (APM)", "technical_product"),
        ("Technical Product Management Intern", "technical_product"),
        ("Product Development Internship Program", "technical_product"),
        ("Training Technology Digital Solutions Intern", "solutions_engineering"),
        ("Intelligent Systems and Workflow Automation Intern", "solutions_engineering"),
    ],
)
def test_scoped_technical_adjacent_titles_are_recognized(title, track):
    got = classify_role(_row(title=title))

    assert got["role_track"] == track


def test_umbrella_program_requires_an_explicit_technical_track():
    technical = classify_role(_row(
        title="Summer Internship Program, All Tracks",
        description=(
            "Applicants select from business areas and tracks including "
            "Technology, Analytics & Risk, Engineering, or Finance."
        ),
    ))
    broad = classify_role(_row(
        title="Summer Internship Program, All Tracks",
        description="Applicants rotate across teams and learn about the business.",
    ))

    assert technical["role_track"] == "general_swe"
    assert "technical program track" in technical["evidence"][0]
    assert broad["role_track"] == "unknown"


@pytest.mark.parametrize(
    ("title", "description", "requirements", "track"),
    [
        (
            "Naval Architect Co-op",
            "The company builds autonomous vehicles using AI and computer vision.",
            "Design hull structures and manufacturing plans. Python preferred.",
            "mechanical_manufacturing",
        ),
        (
            "Product Design Engineering Co-op",
            "Develop physical consumer products through industrial design and prototyping.",
            "Mechanical engineering, CAD, materials, and user testing.",
            "mechanical_manufacturing",
        ),
        (
            "Consumer Insights Co-op",
            "Analyze qualitative and quantitative surveys for product and marketing decisions.",
            "Market research and consumer behavior.",
            "non_technical",
        ),
        (
            "Electrical Engineer Intern",
            "Design PCBs and troubleshoot hardware/software boundaries; assist with firmware.",
            "Electrical engineering, circuits, oscilloscopes, and Python.",
            "electrical_hardware",
        ),
        (
            "Quality Engineering Intern",
            "Inspect manufacturing processes and supplier quality systems.",
            "Manufacturing quality and process control.",
            "quality_test",
        ),
    ],
)
def test_strong_unrelated_title_and_core_duties_beat_incidental_technical_terms(
    title,
    description,
    requirements,
    track,
):
    got = classify_role(_row(
        title=title,
        description=description,
        requirements=requirements,
    ))

    assert got["role_track"] == track
    assert got["role"] != "swe"


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


@pytest.mark.parametrize(
    ("title", "description", "track", "role"),
    [
        ("[Bosch R&D - Internship] Java Web Developing Intern", "Build Java web applications.", "full_stack", "swe"),
        ("[BD] PowerShell Intern", "Develop PowerShell automation scripts.", "devops", "swe"),
        ("[SX/EIT] Automation Tester Intern (Selenium)", "Build automated Selenium test suites.", "sdet_qa_automation", "swe"),
        ("Intern, AI Research", "Research machine-learning models.", "ml_ai", "ml_ai"),
        ("[Internship] Data and AI Intern (Data focused)", "Develop AI data pipelines.", "ml_ai", "ml_ai"),
        ("Hanoi - Embedded Intern", "Develop embedded firmware in C++.", "embedded_software", "swe"),
        ("Embedded Intern", "Design embedded electronic controls.", "electrical_hardware", "unknown"),
        ("[EMN] Internship – Automotive Hardware Penetration Testing", "Test automotive electronics.", "electrical_hardware", "unknown"),
        ("Quant and Portfolio Analytics Summer Intern", "Develop quantitative portfolio models.", "quant_dev", "quant"),
        ("[BD] IT Product Business Analyst / Product Owner Assistant", "Support technical delivery for an IT product.", "technical_product", "product"),
        ("[BD] IT Global Support Intern", "Provide enterprise IT support.", "it_support", "it"),
        ("Internship – Engineering in Manufacturing (Connector Assembly)", "Design manufacturing equipment.", "mechanical_manufacturing", "unknown"),
        ("Internship – R&D Engineering Design", "Design and prototype engineered components.", "other_engineering", "unknown"),
        ("Praktikum in der i4.0 Softwareentwicklung", "Software development internship.", "general_swe", "swe"),
        ("Szoftverfejlesztő gyakornok", "Software development internship.", "general_swe", "swe"),
        ("Praktikum: Entwicklung einer Web-App", "Develop a web application.", "full_stack", "swe"),
        ("Stagiaire Développeur IA et Automatisation", "Développer des modèles d'intelligence artificielle.", "ml_ai", "ml_ai"),
        ("Prácticas en Ingeniería Informática", "Desarrollo de software.", "general_swe", "swe"),
        ("Robotics System Development Intern", "Develop robotic control systems.", "other_engineering", "unknown"),
        ("3D-CFD Simulation Intern", "Develop computational fluid-dynamics simulations.", "other_engineering", "unknown"),
        ("Research Intern – World-Action Model / VLA for Autonomous Driving", "Research machine-learning models for autonomy.", "ml_ai", "ml_ai"),
        ("Internship 2026, Engineering Mechanics and Hydraulic", "Design hydraulic systems.", "mechanical_manufacturing", "unknown"),
        ("Internship 2025, Technical Engineering Function, Chassis System", "Support chassis engineering.", "other_engineering", "unknown"),
        ("Mandatory Internship AI-Based Arc Detection for Automotive Electrical Systems", "Develop AI detection models.", "ml_ai", "ml_ai"),
        ("Pflichtpraktikum im Bereich Datenanalyse und Generative KI", "Develop generative AI analysis.", "ml_ai", "ml_ai"),
        ("Praktikum Data & AI Solutions im industriellen Sondermaschinenbau", "Develop data and AI solutions.", "ml_ai", "ml_ai"),
        ("Praktikum Datenanalyse und Softwaremonitoring", "Develop software monitoring tools.", "general_swe", "swe"),
        ("Pflichtpraktikum FEM Simulation und Konstruktion elektrischer Antriebe", "Simulate electrical drives.", "electrical_hardware", "unknown"),
        ("Praktikum Hardware-Kalibrierung im Bereich MEMS-Sensorik", "Calibrate MEMS hardware.", "electrical_hardware", "unknown"),
        ("Praktikum in der Entwicklung robotischer Systeme", "Develop robotic systems.", "other_engineering", "unknown"),
        ("Internship in Information System and Technology", "Support information systems.", "it_support", "it"),
        ("Praktikum IT-Entwicklung Connected Truck Parking", "Develop connected-parking software.", "general_swe", "swe"),
        ("Praktikum im Digital Experience Product Ownership", "Own delivery of a digital product.", "technical_product", "product"),
    ],
)
def test_audit_confirmed_supported_title_families(title, description, track, role):
    got = classify_role(_row(title=title, description=description))

    assert got["role_track"] == track
    assert got["role"] == role


@pytest.mark.parametrize(
    ("title", "description", "expected_track"),
    [
        ("Marketing Analytics Intern", "Measure campaign performance.", "non_technical"),
        ("AI Marketing Intern", "Create campaigns for AI products.", "non_technical"),
        ("Technical Sales Engineering Intern", "Support sales demonstrations.", "solutions_engineering"),
        ("Product Owner Intern", "Own a business backlog.", "unknown"),
        ("Market Research Intern", "Run consumer surveys.", "non_technical"),
        ("R&D Engineering Inspection Intern", "Perform manual visual inspection.", "unknown"),
        ("General Operations Intern", "Coordinate business operations.", "non_technical"),
        ("Technology Operations Intern", "Coordinate vendors and schedules.", "non_technical"),
        ("Systems Coordinator Intern", "Coordinate records and meetings.", "unknown"),
        ("Data Governance Intern", "Maintain business policies and records.", "unknown"),
    ],
)
def test_audit_nearby_negative_controls_remain_excluded(
    title,
    description,
    expected_track,
):
    got = classify_role(_row(title=title, description=description))

    assert got["role_track"] == expected_track


# Exact titles that GitHub backstop feeds publish without a description, from
# the production coverage audit where every one of them became invalid_role.
@pytest.mark.parametrize(
    ("title", "track", "role"),
    [
        ("Data Scientist Intern", "data_science", "data_science"),
        ("Sector Data Scientist Intern", "data_science", "data_science"),
        ("Data Scientist Intern - Product Analytics", "data_science", "data_science"),
        ("AI Applied Scientist Intern", "ml_ai", "ml_ai"),
        ("Applied Scientist Intern", "ml_ai", "ml_ai"),
        ("Applied Science Intern - Gen AI & Large Language Models", "ml_ai", "ml_ai"),
        ("Researcher Intern - Coreai", "ml_ai", "ml_ai"),
        ("Quantitative Trader Intern - Quantitative Trader", "quant_research", "quant"),
        (
            "Quantitative Strategist Intern - Americas - The Core Quantitative Group",
            "quant_research",
            "quant",
        ),
        ("AI Intern", "ml_ai", "ml_ai"),
        (
            "Artificial Intelligence Intern - Tax - Tax Technology and Transformation",
            "ml_ai",
            "ml_ai",
        ),
        (
            "Technology Summer Analyst Intern - Application Development",
            "general_swe",
            "swe",
        ),
        ("Technology Analyst Intern - Application Development", "general_swe", "swe"),
    ],
)
def test_title_only_backstop_titles_name_their_discipline(title, track, role):
    got = classify_role(_row(title=title))

    assert got["role_track"] == track
    assert got["role"] == role


@pytest.mark.parametrize(
    ("title", "expected_track"),
    [
        # Research or science without an AI/ML qualifier names no discipline.
        ("Applied Research Intern", "unknown"),
        ("Researcher Intern", "unknown"),
        ("Research Scientist Intern", "unknown"),
        ("Quantum Applied Science Intern - Quantum Technologies team", "unknown"),
        ("Quantum Applied Scientist Intern", "unknown"),
        ("AI Marketing Research Intern", "non_technical"),
        # Banking and investment programs that merely mention quantitative work.
        ("Quantitative Master’s Intern - Investments - Global Capital Markets", "unknown"),
        ("Summer Associate Intern - Corporate Finance - Quantitative Finance", "unknown"),
        ("Economics Intern - Structural IO", "unknown"),
        ("Operations Research Intern", "unknown"),
        # AI as a product or audience, not the role.
        ("AI Marketing Intern", "non_technical"),
        ("AI Sales Intern", "non_technical"),
        ("AI Trainer Intern", "unknown"),
        ("Data & AI Intern - Analyst, Operations", "unknown"),
        ("Technology Summer Analyst Intern - Production Support", "unknown"),
        ("Business Application Analyst Intern", "unknown"),
        ("People Analytics Intern", "unknown"),
        ("GPU Intern", "unknown"),
        ("Panasonic Transformation Group Intern", "unknown"),
        ("Data Governance Intern", "unknown"),
    ],
)
def test_title_only_ambiguous_titles_stay_unadmitted(title, expected_track):
    got = classify_role(_row(title=title))

    assert got["role_track"] == expected_track


@pytest.mark.parametrize(
    ("title", "description", "expected_track"),
    [
        (
            "Business Operations Intern",
            "Partner with data scientists, applied scientists, AI researchers, "
            "and quantitative traders on reporting.",
            "non_technical",
        ),
        (
            "Finance Intern",
            "Support application development budgets and AI intern onboarding.",
            "unknown",
        ),
    ],
)
def test_title_only_role_phrases_are_not_read_from_descriptions(
    title,
    description,
    expected_track,
):
    got = classify_role(_row(title=title, description=description))

    assert got["role_track"] == expected_track


# --- data science and quantitative trading & research categories -----------


@pytest.mark.parametrize(
    ("title", "description", "track", "role"),
    [
        ("Data Scientist Intern", "", "data_science", "data_science"),
        ("Data Science Intern", "", "data_science", "data_science"),
        ("Decision Scientist Intern", "", "data_science", "data_science"),
        ("Statistician Intern", "", "data_science", "data_science"),
        ("Statistical Modeling Intern", "", "data_science", "data_science"),
        ("Experimentation Scientist Intern", "", "data_science", "data_science"),
        (
            "Data Scientist, Product",
            "Train machine learning models and build Python pipelines.",
            "data_science",
            "data_science",
        ),
        ("Data Engineer Intern", "", "data_engineering", "data_science"),
        ("Data Infrastructure Intern", "", "data_engineering", "data_science"),
        ("Data Analyst Intern", "", "data_engineering", "data_science"),
        ("Machine Learning Engineer Intern", "", "ml_ai", "ml_ai"),
        ("Applied Scientist Intern", "", "ml_ai", "ml_ai"),
        ("Quant Developer Intern", "", "quant_dev", "quant"),
        ("Quantitative Developer Intern", "", "quant_dev", "quant"),
        ("Quantitative Analyst Intern", "", "quant_dev", "quant"),
        (
            "Quantitative Developer Intern - Systematic Trading",
            "",
            "quant_dev",
            "quant",
        ),
        ("Quantitative Trader Intern", "", "quant_research", "quant"),
        ("Quantitative Trading Intern", "", "quant_research", "quant"),
        ("Quantitative Research Intern", "", "quant_research", "quant"),
        ("Quantitative Researcher Intern", "", "quant_research", "quant"),
        ("Quantitative Strategist Intern", "", "quant_research", "quant"),
        ("Systematic Trading Intern", "", "quant_research", "quant"),
        ("Systematic Research Intern", "", "quant_research", "quant"),
        ("Trading Intern", "", "quant_research", "quant"),
        (
            "Quant Intern",
            "Develop alpha signals and statistical arbitrage trading strategies.",
            "quant_research",
            "quant",
        ),
    ],
)
def test_data_science_and_quant_categories_are_distinguished(
    title,
    description,
    track,
    role,
):
    got = classify_role(_row(title=title, description=description))

    assert got["role_track"] == track
    assert got["role"] == role


@pytest.mark.parametrize(
    ("title", "description", "expected_track"),
    [
        ("Sales and Trading Intern", "", "non_technical"),
        ("Sales & Trading Summer Analyst", "", "non_technical"),
        ("Financial Analyst Intern", "", "unknown"),
        ("Investment Banking Summer Analyst", "", "unknown"),
        ("Equity Research Intern", "", "unknown"),
        ("Research Intern", "", "unknown"),
        ("Market Research Intern", "", "non_technical"),
        ("Quantitative Finance Intern", "", "unknown"),
        ("Quant Intern", "", "unknown"),
        (
            "Summer Analyst Intern - Americas - Investment Banking Quantitative Strategies",
            "",
            "unknown",
        ),
        # Colleagues named in a description are not the role itself.
        (
            "Business Analyst Intern",
            "Partner with data scientists and quantitative researchers.",
            "unknown",
        ),
        (
            "Analyst Intern",
            "Apply statistical modeling and experimentation to revenue forecasts.",
            "unknown",
        ),
    ],
)
def test_finance_and_research_titles_need_quant_or_data_science_evidence(
    title,
    description,
    expected_track,
):
    got = classify_role(_row(title=title, description=description))

    assert got["role_track"] == expected_track


def test_quant_intern_with_infrastructure_duties_is_not_quant_research():
    got = classify_role(_row(
        title="Quant Intern",
        description="Build low-latency C++ trading infrastructure and developer tooling.",
    ))

    assert got["role_track"] != "quant_research"
    assert got["role"] == "swe"


def test_new_tracks_carry_labels_and_software_membership():
    from app.classify import ROLE_TRACK_LABELS, ROLE_TRACK_TO_ROLE, SOFTWARE_ROLE_TRACKS

    assert ROLE_TRACK_LABELS["data_science"] == "Data science"
    assert ROLE_TRACK_LABELS["quant_research"] == "Quant trading / research"
    assert ROLE_TRACK_TO_ROLE["data_science"] == "data_science"
    assert ROLE_TRACK_TO_ROLE["quant_research"] == "quant"
    assert {"data_science", "quant_research"} <= SOFTWARE_ROLE_TRACKS
