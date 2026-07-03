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


def _scored(title, **overrides):
    row = {
        "company": "ExampleCo",
        "title": title,
        "location": "New York, NY",
        "compensation": "$35/hr",
        "description": "Structured internship with mentorship and code review.",
        "requirements": "Python, Java, SQL, REST APIs, Git",
    }
    row.update(overrides)
    return analyze_row(row)


def test_watcher_score_fields_are_present_for_strong_backend_role():
    s = _scored(
        "IT Internship (BackEnd, Java)",
        description="Build BackEnd services and REST APIs in Java.",
    )["score"]

    assert s["watcher_eligible"] is True
    assert s["fit_score"] >= 90
    assert s["role_track"] == "backend"
    assert "backend" in s["fit_explanation"].lower()


def test_non_swe_engineering_roles_have_zero_watcher_fit():
    for title in (
        "2027 Electrical Engineer Intern",
        "2027 Manufacturing Engineer Intern",
        "Mechanical Design Engineer",
        "Factory Automation Engineering Intern",
        "Customer Experience Engineer - Intern",
    ):
        result = _scored(title, company="Anduril Industries")
        score = result["score"]

        assert result["role"]["role"] != "swe"
        assert score["watcher_eligible"] is False
        assert score["fit_score"] == 0
        assert score["watcher_action"] == "skip"
        assert score["watcher_action_label"] == "Skip"
        assert score["watcher_ineligible_reason"]


def test_low_priority_it_quality_and_solutions_are_visible_but_capped():
    for title, track in (
        ("IT Support Intern", "it_support"),
        ("Quality Engineer Intern", "quality_test"),
        ("Solutions Engineer Intern", "solutions_engineering"),
    ):
        result = _scored(title, requirements="Python, Linux")
        score = result["score"]

        assert result["role"]["role_track"] == track
        assert score["watcher_eligible"] is True
        assert score["fit_score"] == 20
        assert score["watcher_action"] == "research_more"


def test_backend_java_ranks_above_adjacent_software_tracks():
    backend = _scored(
        "IT Internship (BackEnd, Java)",
        description="Build BackEnd services and REST APIs in Java.",
    )["score"]
    cloud = _scored(
        "Cloud Developer Internship",
        description="Build cloud APIs and platform services in Python.",
        requirements="AWS, Python, Docker",
    )["score"]
    devops = _scored(
        "DevOps Engineering Intern",
        description="Own developer tooling and automation code for backend infrastructure APIs.",
        requirements="Python, Docker, Linux",
    )["score"]
    embedded = _scored(
        "Embedded Software Engineer Intern",
        description="Write embedded software for devices.",
        requirements="C++, Linux, Git",
    )["score"]

    assert backend["fit_score"] > cloud["fit_score"]
    assert backend["fit_score"] > devops["fit_score"]
    assert backend["fit_score"] > embedded["fit_score"]


def test_vague_cloud_and_devops_are_not_watcher_eligible():
    cloud = _scored("Cloud Developer Internship", description="", requirements="AWS, Linux")["score"]
    devops = _scored("DevOps Engineering Intern", description="", requirements="Docker, Linux")["score"]

    assert cloud["watcher_eligible"] is False
    assert cloud["fit_score"] == 0
    assert "lacks clear" in cloud["watcher_ineligible_reason"]
    assert devops["watcher_eligible"] is False
    assert devops["fit_score"] == 0


def test_prestige_mentorship_and_pay_do_not_rescue_customer_or_hardware_roles():
    backend = _scored(
        "Backend Engineer Intern",
        company="SmallCo",
        compensation="$20/hr",
        description="Build backend APIs in Java.",
    )["score"]
    customer = _scored(
        "Customer Experience Engineer - Intern",
        company="Stripe",
        compensation="$60/hr",
        description="Mentorship, structured program, and support for customer issues.",
        requirements="Python, SQL",
    )["score"]
    electrical = _scored(
        "Electrical Engineer Intern",
        company="Stripe",
        compensation="$60/hr",
        description="Mentorship and structured program.",
        requirements="Python, Linux",
    )["score"]

    assert backend["fit_score"] > customer["fit_score"]
    assert backend["fit_score"] > electrical["fit_score"]
    assert customer["watcher_eligible"] is False
    assert electrical["watcher_eligible"] is False


def test_non_swe_top_reason_does_not_claim_strong_software_relevance():
    score = _scored(
        "Electrical Engineer Intern",
        description="Mentorship and great pay.",
        requirements="Python, Linux",
    )["score"]

    assert not any("software engineering role" in reason.lower() for reason in score["reasons"])
    assert not any("strong role relevance" in reason.lower() for reason in score["reasons"])
