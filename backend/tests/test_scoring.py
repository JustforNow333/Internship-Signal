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


def test_not_every_eligible_swe_role_gets_perfect_fit_score():
    rows = [
        _scored("Backend Engineer Intern", description="Build Java REST APIs.", requirements="Java, SQL, Git"),
        _scored("Software Engineer Intern", description="Build simulation infrastructure.", requirements="Rust, Go, C++"),
        _scored("Cloud Developer Internship", description="Build cloud APIs and platform services in Python.", requirements="AWS, Python"),
        _scored("Embedded Software Engineer Intern", description="Write embedded software for robotics hardware.", requirements="C++, Linux, Git"),
        _scored("Software Engineer Assistant", description="Assist with bug fixes.", requirements="Git"),
    ]
    fits = [row["score"]["fit_score"] for row in rows if row["score"]["watcher_eligible"]]

    assert len(fits) == 5
    assert any(score < 85 for score in fits)
    assert len(set(fits)) >= 4
    assert fits.count(100) <= 1


def test_anduril_style_rust_go_cpp_swe_is_eligible_but_not_perfect():
    result = _scored(
        "2027 Software Engineer Intern",
        company="Anduril Industries",
        description="Build simulation infrastructure for autonomous systems.",
        requirements="Rust, Go, C++",
    )
    score = result["score"]

    assert result["role"]["role_track"] == "general_swe"
    assert score["watcher_eligible"] is True
    assert score["fit_score"] < 85
    assert "Go/Rust" in score["fit_explanation"]


def test_backend_java_beats_generic_rust_go_cpp_swe():
    backend_java = _scored(
        "Backend Java Intern",
        description="Build backend REST APIs and database-backed services.",
        requirements="Java, SQL, Git",
    )["score"]
    generic_systems = _scored(
        "Software Engineer Intern",
        description="Build low-level systems infrastructure.",
        requirements="Rust, Go, C++",
    )["score"]

    assert backend_java["fit_score"] > generic_systems["fit_score"]
    assert backend_java["fit_score"] <= 96
    assert generic_systems["fit_score"] < 85


def test_python_fastapi_postgres_full_stack_roles_receive_highest_fit_scores():
    near_perfect = _scored(
        "Backend Engineer Intern",
        description="Build Python FastAPI services with Flask, SQLAlchemy, PostgreSQL and RESTful APIs.",
        requirements="Python, FastAPI, Flask, SQLAlchemy, SQL, PostgreSQL, GitHub, Pytest",
    )["score"]
    full_stack = _scored(
        "Full Stack Engineer Intern",
        description="Build full-stack web apps with React, TypeScript, Next.js, Python APIs, SQL and Postgres.",
        requirements="React, TypeScript, Next.js, Python, SQL, PostgreSQL, GitHub",
    )["score"]
    backend_java = _scored(
        "Backend Java Intern",
        description="Build backend REST APIs and database-backed services.",
        requirements="Java, SQL, Git",
    )["score"]

    assert near_perfect["fit_score"] == 100
    assert full_stack["fit_score"] >= 95
    assert near_perfect["fit_score"] >= full_stack["fit_score"] > backend_java["fit_score"]


def test_technical_intern_is_not_perfect_without_clear_duties_or_stack():
    result = _scored(
        "Technical Intern",
        description="Assist the engineering team with technical tasks.",
        requirements="Linux",
    )
    score = result["score"]

    assert score["fit_score"] == 0
    assert score["watcher_eligible"] is False
    assert score["watcher_action"] == "skip"


def test_analytics_reporting_does_not_score_like_data_engineering_without_software_evidence():
    reporting = _scored(
        "Data Analytics Intern",
        description="Create analytics reports and dashboards for stakeholders.",
        requirements="Excel, PowerPoint",
    )["score"]
    pipeline = _scored(
        "Data Analytics Intern",
        description="Build Python data pipelines and analytics apps with Pandas.",
        requirements="Python, Pandas, SQL, Pytest",
    )["score"]

    assert reporting["watcher_eligible"] is True
    assert reporting["fit_score"] < 90
    assert reporting["fit_score"] < pipeline["fit_score"] <= 94


def test_commercial_and_product_coops_require_clear_software_focus():
    commercial = _scored(
        "Commercial Co-op",
        description="Support client proposals and commercial strategy.",
        requirements="Excel",
    )["score"]
    product = _scored(
        "Product Development Co-op",
        description="Coordinate product requirements and user interviews.",
        requirements="Roadmaps, communication",
    )["score"]
    software_product = _scored(
        "Product Development Co-op",
        description="Build product features in React, TypeScript, and backend APIs.",
        requirements="React, TypeScript, SQL, GitHub",
    )["score"]

    assert commercial["watcher_eligible"] is False
    assert product["watcher_eligible"] is False
    assert software_product["watcher_eligible"] is True
    assert software_product["fit_score"] >= 85


def test_top_ten_fit_scores_have_spread_not_all_perfect():
    rows = [
        _scored("Backend Engineer Intern", description="Build Python FastAPI services with PostgreSQL REST APIs.", requirements="Python, FastAPI, SQL, PostgreSQL, GitHub"),
        _scored("Full Stack Engineer Intern", description="Build React TypeScript full-stack web apps with Python APIs.", requirements="React, TypeScript, Next.js, Python, SQL"),
        _scored("Backend Java Intern", description="Build Java REST APIs and database-backed services.", requirements="Java, SQL, Git"),
        _scored("Data Engineer Intern", description="Build Python data ingestion pipelines with Pandas.", requirements="Python, SQL, Pandas"),
        _scored("Software Engineer Intern", description="Build product features.", requirements="JavaScript, Git"),
        _scored("Frontend Engineer Intern", description="Build React UI components.", requirements="React, TypeScript"),
        _scored("Cloud Developer Internship", description="Build cloud APIs and platform services in Python.", requirements="AWS, Python"),
        _scored("DevOps Engineering Intern", description="Own developer tooling and automation code for backend services.", requirements="Python, Docker"),
        _scored("Embedded Software Engineer Intern", description="Write embedded software for robotics hardware.", requirements="C++, Linux, Git"),
        _scored("Software Engineer Assistant", description="Assist with bug fixes.", requirements="Git"),
    ]
    fits = sorted((row["score"]["fit_score"] for row in rows), reverse=True)[:10]

    assert fits.count(100) <= 1
    assert len(set(fits)) >= 6


def test_fit_score_100_requires_strong_resume_overlap_and_target_track():
    perfect = _scored(
        "Backend Engineer Intern",
        description="Build Python FastAPI REST APIs with SQL and PostgreSQL, deployed on Render.",
        requirements="Python, FastAPI, SQL, PostgreSQL, GitHub",
    )
    two_match_backend = _scored(
        "Backend Engineer Intern",
        description="Build backend APIs.",
        requirements="FastAPI, GitHub",
    )
    cloud_many_matches = _scored(
        "Cloud Developer Internship",
        description="Build cloud APIs and platform services with Python, SQL, React, TypeScript and GitHub.",
        requirements="AWS, Python, SQL, React, TypeScript, GitHub",
    )

    assert perfect["score"]["fit_score"] == 100
    assert perfect["role"]["role_track"] in {"backend", "full_stack", "data_engineering", "ml_ai", "general_swe"}
    assert two_match_backend["score"]["fit_score"] < 100
    assert cloud_many_matches["score"]["fit_score"] < 100
