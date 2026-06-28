from watcher.notify import render_digest


def match(
    company,
    title,
    score,
    *,
    action_label="Apply now",
    source="direct",
    adapter="fake",
    reasons=None,
    red_flags=None,
    alumni=None,
    description="",
):
    return {
        "company": company,
        "title": title,
        "description": description,
        "source_url": f"https://example.com/{company}/{title}".replace(" ", "-"),
        "score": {
            "total": score,
            "action_label": action_label,
            "action": action_label.lower().replace(" ", "_"),
            "reasons": reasons if reasons is not None else ["Strong role match", "Secondary reason"],
        },
        "red_flags": red_flags if red_flags is not None else [],
        "extra": {"source": source, "source_adapter": adapter},
        "alumni": alumni if alumni is not None else [],
    }


def test_render_digest_sorts_includes_backstop_and_alumni():
    direct = match(
        "OpenAI",
        "Software Engineer Intern",
        92,
        source="direct",
        adapter="ashby",
        alumni=[
            {
                "name": "Ada Example",
                "occupation": "Software Engineer",
                "linkedin_url": "https://www.linkedin.com/in/fake-ada-example",
            },
            {
                "name": "Grace Fixture",
                "occupation": "Recruiter",
                "linkedin_url": "https://www.linkedin.com/in/fake-grace-fixture",
            },
        ],
    )
    github_backstop = match(
        "ThinData Co",
        "Software Engineering Intern",
        31,
        action_label="Research more",
        source="github",
        adapter="github_listings",
        reasons=["Title is enough to classify the thin backstop row"],
        description="",
        alumni=[],
    )
    comp_unclear_only = match(
        "MutedFlag Co",
        "Backend Intern",
        65,
        red_flags=[{"label": "Compensation unclear or unstated", "severity": "minor"}],
    )

    subject, body = render_digest([github_backstop, comp_unclear_only, direct])

    assert subject == "Internship Watcher: 3 new SWE-intern matches"
    assert "3 new watched-company SWE-intern postings, sorted by score." in body
    assert body.index("1. OpenAI - Software Engineer Intern") < body.index("2. MutedFlag Co - Backend Intern")
    assert body.index("2. MutedFlag Co - Backend Intern") < body.index("3. ThinData Co - Software Engineering Intern")
    assert "score: 31" in body
    assert "source tag: github backstop (github_listings)" in body
    assert "alumni you know there: No alumni on file" in body
    assert "Ada Example - Software Engineer - https://www.linkedin.com/in/fake-ada-example" in body
    assert "Grace Fixture - Recruiter - https://www.linkedin.com/in/fake-grace-fixture" in body
    assert "top reason: Strong role match" in body
    assert "Compensation unclear or unstated (muted)" in body


def test_render_digest_zero_matches_returns_no_email_sentinel():
    assert render_digest([]) == ("", "")
