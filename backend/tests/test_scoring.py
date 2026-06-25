from app.config import SCORE_WEIGHTS

from tests.conftest import analyze_row

STRONG = {
    "company": "Stripe", "title": "Backend Engineering Intern",
    "location": "New York, NY", "compensation": "$45/hr",
    "description": "Own a project end-to-end with a dedicated mentor, weekly 1:1s, "
                   "and code review from senior engineers.",
    "requirements": "Python, Flask, SQL, REST APIs, Git",
    "deadline": "2026-06-30",
}


def test_weights_sum_to_one():
    assert abs(sum(SCORE_WEIGHTS.values()) - 1.0) < 1e-9


def test_score_shape_is_complete():
    s = analyze_row(STRONG)["score"]
    assert set(s["categories"]) == set(SCORE_WEIGHTS)
    for name, c in s["categories"].items():
        assert 0 <= c["score"] <= 100
        assert c["weight"] == SCORE_WEIGHTS[name]
        assert c["explanation"]
    assert 1 <= len(s["reasons"]) <= 3
    assert 1 <= len(s["concerns"]) <= 3
    assert s["explanation"]


def test_strong_posting_is_high_apply_now():
    s = analyze_row(STRONG)["score"]
    assert s["total"] >= 70
    assert s["bucket"] == "high" and s["action"] == "apply_now"


def test_unpaid_social_media_is_low_skip():
    s = analyze_row({
        "title": "Social Media Intern", "compensation": "Unpaid",
        "description": "Create content for our clients. Great exposure!",
    })["score"]
    assert s["total"] < 45
    assert s["bucket"] == "low" and s["action"] == "skip"


def test_three_major_flags_cap_to_low_skip():
    s = analyze_row({
        "company": "HustleHub", "title": "Founding Engineer Intern",
        "compensation": "Equity only",
        "description": "Join the ground floor! Wear many hats and build our MVP from scratch.",
        "requirements": "3+ years building production apps, React, Node, AWS",
    })["score"]
    assert s["bucket"] == "low" and s["action"] == "skip"
    assert s["total"] <= 44


def test_critical_flag_caps_score_and_action():
    s = analyze_row({
        "company": "QuickStart Careers", "title": "Remote Data Intern",
        "compensation": "$45/hr",
        "description": "No interview needed. A $99 onboarding fee covers training. "
                       "Message us on WhatsApp.",
    })["score"]
    assert s["total"] <= 40
    assert s["bucket"] == "low" and s["action"] == "skip"
    # The headline pay must not rescue it.
    assert s["categories"]["compensation"]["score"] >= 80


def test_expired_deadline_forces_skip_with_concern():
    s = analyze_row({**STRONG, "deadline": "2026-06-01"})["score"]
    assert s["deadline_days_left"] < 0
    assert s["action"] == "skip"
    assert any("deadline" in c.lower() or "passed" in c.lower() for c in s["concerns"])


def test_urgent_decent_posting_is_apply_now():
    # >= 60 with a deadline inside 7 days and no major flags.
    s = analyze_row({**STRONG, "deadline": "2026-06-14"})["score"]
    assert s["deadline_days_left"] == 5
    assert s["action"] == "apply_now"


def test_scoring_is_deterministic():
    a = analyze_row(STRONG)["score"]
    b = analyze_row(STRONG)["score"]
    assert a == b


def test_blank_row_lands_in_research_not_apply():
    s = analyze_row({"company": "Orchid", "title": "Software Intern"})["score"]
    assert s["action"] in ("research_more", "skip")
    assert s["bucket"] != "high"
