from app.ask import ask, interpret


def _by_id(jobs):
    return {j["id"]: j for j in jobs}


def _ask_job(job_id, *, role_track, score=70, signals=None):
    return {
        "id": job_id,
        "company": f"{job_id.title()} Co",
        "title": f"{role_track.replace('_', ' ').title()} Intern",
        "compensation": {"kind": "paid"},
        "red_flags": [],
        "positive_signals": [{"id": sid, "label": sid, "strength": 1} for sid in (signals or [])],
        "role_classification": {
            "role": "swe",
            "role_track": role_track,
            "label": role_track.replace("_", " ").title(),
        },
        "score": {
            "total": score,
            "action_label": "Apply later",
            "categories": {
                "role_relevance": {"score": score},
                "technical_depth": {"score": score},
            },
            "reasons": [f"{role_track} fit"],
        },
    }


def test_backend_question(sample_jobs):
    r = ask("Which postings are best for backend experience?", sample_jobs)
    assert r["results"], "should return ranked results"
    jobs = _by_id(sample_jobs)
    top = jobs[r["results"][0]["id"]]
    assert top["role_classification"]["role"] == "swe" or any(
        s["id"] == "backend_focus" for s in top["positive_signals"]
    )
    assert "backend" in r["interpretation"].lower()
    # The exploitative equity-only posting must not be a top "best" pick.
    top3 = [r["results"][i]["company"] for i in range(3)]
    assert "HustleHub" not in top3


def test_backend_question_excludes_frontend_only_swe():
    jobs = [
        _ask_job("frontend", role_track="frontend", score=99),
        _ask_job("general", role_track="general_swe", score=98),
        _ask_job("backend", role_track="backend", score=70),
        _ask_job("platform", role_track="platform_infra", score=65),
        _ask_job("legacy", role_track="general_swe", score=60, signals=["backend_focus"]),
    ]

    r = ask("best backend internships", jobs)

    ids = [item["id"] for item in r["results"]]
    assert set(ids) == {"backend", "platform", "legacy"}
    assert "frontend" not in ids
    assert "general" not in ids
    assert "backend-adjacent" in "; ".join(r["filters_applied"])


def test_paid_data_science_only(sample_jobs):
    r = ask("Show paid data science internships only", sample_jobs)
    assert any("paid" in f for f in r["filters_applied"])
    assert any("role" in f for f in r["filters_applied"])
    jobs = _by_id(sample_jobs)
    assert r["results"]
    for item in r["results"]:
        j = jobs[item["id"]]
        assert j["role_classification"]["role"] == "data_science"
        assert j["compensation"]["kind"] in ("paid", "stipend_unspecified")


def test_exploitative_question(sample_jobs):
    r = ask("Which ones look exploitative?", sample_jobs)
    companies = {x["company"] for x in r["results"]}
    assert {"HustleHub", "QuickStart Careers"} <= companies
    jobs = _by_id(sample_jobs)
    for item in r["results"]:
        flags = jobs[item["id"]]["red_flags"]
        assert any(f["severity"] in ("critical", "major") for f in flags)
        assert item["headline_reason"]


def test_startups_question(sample_jobs):
    r = ask("Which companies seem like actual startups?", sample_jobs)
    companies = [x["company"] for x in r["results"]]
    assert "Meridian" in companies and "HustleHub" in companies
    # Grouped by company: no repeats.
    assert len(companies) == len(set(companies))


def test_apply_tonight_question(sample_jobs):
    r = ask("Which ones should I apply to tonight?", sample_jobs)
    assert r["results"]
    companies = [x["company"] for x in r["results"]]
    assert "Two Sigma" in companies[:3]      # deadline tomorrow
    assert "Lumen Health" not in companies   # expired
    # Sorted by urgency: first result mentions its deadline.
    assert "deadline" in r["results"][0]["headline_reason"].lower()


def test_modifiers_parse():
    p = interpret("show remote paid ml internships")
    assert p["paid_only"] and p["remote_only"] and p["role"] == "ml_ai"
    p2 = interpret("unpaid ones?")
    assert p2["unpaid_only"] and not p2["paid_only"]


def test_nonsense_falls_back_gracefully(sample_jobs):
    r = ask("purple elephant rodeo", sample_jobs)
    assert r["results"] == []
    assert "keyword" in r["interpretation"].lower() or "couldn't" in r["interpretation"].lower()


def test_empty_question_returns_help(sample_jobs):
    r = ask("", sample_jobs)
    assert r["results"] == [] and r.get("examples")


def test_every_answer_discloses_no_llm(sample_jobs):
    for q in ("", "best backend", "startups?"):
        assert "no LLM" in ask(q, sample_jobs)["llm_note"]
