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
