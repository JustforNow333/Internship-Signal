from tests.conftest import analyze_row
from app.signals import count_tech_tools


def _ids(items):
    return {x["id"] for x in items}


def test_unpaid_flag():
    a = analyze_row({"title": "Social Media Intern", "compensation": "Unpaid",
                     "description": "Great exposure!"})
    assert "unpaid" in _ids(a["red"])
    sev = next(f["severity"] for f in a["red"] if f["id"] == "unpaid")
    assert sev == "major"


def test_equity_only_founder_dump():
    a = analyze_row({
        "company": "HustleHub", "title": "Founding Engineer Intern",
        "compensation": "Equity only",
        "description": "Join the ground floor! Wear many hats and build our MVP from scratch.",
        "requirements": "3+ years building production apps, React, Node, AWS",
    })
    ids = _ids(a["red"])
    assert {"equity_only", "founder_responsibilities", "unrealistic_experience"} <= ids


def test_scam_pattern_is_critical():
    a = analyze_row({
        "company": "QuickStart Careers", "title": "Remote Data Intern",
        "compensation": "$45/hr",
        "description": "No interview needed - start today. A $99 onboarding fee covers "
                       "your training materials. Message us on WhatsApp to begin.",
    })
    ids = _ids(a["red"])
    assert "scam_fee" in ids
    assert next(f["severity"] for f in a["red"] if f["id"] == "scam_fee") == "critical"
    assert {"no_interview", "offplatform_recruiting"} <= ids


def test_very_low_pay_flag():
    a = analyze_row({"title": "Backend Intern", "compensation": "$5/hr"})
    assert "very_low_pay" in _ids(a["red"])


def test_grunt_work_without_learning():
    a = analyze_row({"title": "Data Entry Intern",
                     "description": "Enter supplier invoices into our spreadsheet. Repetitive work."})
    assert "grunt_work" in _ids(a["red"])


def test_positive_signals_on_strong_posting():
    a = analyze_row({
        "company": "Stripe", "title": "Backend Engineering Intern",
        "location": "New York, NY", "compensation": "$45/hr",
        "description": "Own a project end-to-end with a dedicated mentor and weekly 1:1s.",
        "requirements": "Python, Flask, SQL, REST APIs, Git",
    })
    ids = _ids(a["pos"])
    assert {"paid_well", "stack_match", "mentorship", "ownership", "reputable",
            "specific_tech", "backend_focus"} <= ids
    stack = next(s for s in a["pos"] if s["id"] == "stack_match")
    assert stack["strength"] == 3  # >= 2 profile skills matched
    assert "flask" in stack["evidence"].lower()


def test_paid_vs_paid_well_threshold():
    a = analyze_row({"title": "Backend Intern", "compensation": "$18/hr"})
    ids = _ids(a["pos"])
    assert "paid" in ids and "paid_well" not in ids


def test_no_learning_flag_only_for_nontechnical_roles():
    tech = analyze_row({"title": "Backend Intern", "compensation": "$30/hr",
                        "description": "Build APIs."})
    nontech = analyze_row({"title": "Operations Intern", "compensation": "$16/hr",
                           "description": "Inventory counts and gift wrapping."})
    assert "no_learning_mention" not in _ids(tech["red"])
    assert "no_learning_mention" in _ids(nontech["red"])


def test_tech_tool_detection_does_not_match_inside_unrelated_words():
    tools = count_tech_tools(
        "JavaScript for digital marketing analytics with trust and reactive workflows"
    )

    assert tools == ["javascript"]
