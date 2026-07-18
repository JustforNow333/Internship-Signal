import io
import logging
import os

import pytest

from watcher.config import load_dotenv
from watcher.notify import DRY_RUN_HEADER, SMTP_TIMEOUT_SECONDS, render_digest, send_digest


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
    fit_score=None,
    role_track="general_swe",
    watcher_eligible=True,
    fit_explanation=None,
):
    return {
        "company": company,
        "title": title,
        "description": description,
        "source_url": f"https://example.com/{company}/{title}".replace(" ", "-"),
        "score": {
            "total": score,
            "fit_score": score if fit_score is None else fit_score,
            "watcher_eligible": watcher_eligible,
            "role_track": role_track,
            "fit_explanation": fit_explanation or f"{role_track} role fit",
            "watcher_action_label": action_label,
            "action_label": action_label,
            "action": action_label.lower().replace(" ", "_"),
            "reasons": reasons if reasons is not None else ["Strong role match", "Secondary reason"],
        },
        "role_classification": {"role": "swe", "role_track": role_track},
        "red_flags": red_flags if red_flags is not None else [],
        "extra": {"source": source, "source_adapter": adapter},
        "alumni": alumni if alumni is not None else [],
    }


def test_render_digest_sorts_includes_backstop_and_alumni():
    direct = match(
        "OpenAI",
        "Software Engineer Intern",
        92,
        fit_score=92,
        role_track="general_swe",
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
        fit_score=31,
        role_track="general_swe",
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
        fit_score=100,
        role_track="backend",
        red_flags=[{"label": "Compensation unclear or unstated", "severity": "minor"}],
    )

    subject, body = render_digest([github_backstop, comp_unclear_only, direct])

    assert subject == "Internship Watcher: 3 new SWE-intern matches"
    assert "3 new watched-company SWE-intern postings, sorted by fit score." in body
    assert body.index("1. MutedFlag Co - Backend Intern") < body.index("2. OpenAI - Software Engineer Intern")
    assert body.index("2. OpenAI - Software Engineer Intern") < body.index("3. ThinData Co - Software Engineering Intern")
    assert "score: 31" in body
    assert "fit score: 100" in body
    assert "role track: backend" in body
    assert "fit reason: backend role fit" in body
    assert "source tag: github backstop (github_listings)" in body
    assert "alumni you know there: No alumni matched; alumni index status unknown" in body
    assert "Ada Example - Software Engineer - https://www.linkedin.com/in/fake-ada-example" in body
    assert "Grace Fixture - Recruiter - https://www.linkedin.com/in/fake-grace-fixture" in body
    assert "top reason: Strong role match" in body
    assert "Compensation unclear or unstated (muted)" in body


def test_render_digest_zero_matches_returns_no_email_sentinel():
    assert render_digest([]) == ("", "")


def test_render_digest_excludes_watcher_ineligible_matches():
    backend = match("Bosch", "IT Internship (BackEnd, Java)", 82, fit_score=100, role_track="backend")
    electrical = match(
        "Anduril",
        "Electrical Engineer Intern",
        95,
        fit_score=0,
        role_track="electrical_hardware",
        watcher_eligible=False,
    )

    subject, body = render_digest([electrical, backend])

    assert subject == "Internship Watcher: 1 new SWE-intern match"
    assert "IT Internship (BackEnd, Java)" in body
    assert "Electrical Engineer Intern" not in body


def test_render_digest_excludes_degree_ineligible_matches():
    backend = match("Bosch", "IT Internship (BackEnd, Java)", 82, fit_score=90, role_track="backend")
    phd = match("ResearchCo", "Machine Learning Engineer PhD Intern", 98, fit_score=0, role_track="ml_ai")
    phd["degree_eligible"] = False
    phd["degree_ineligible_reason"] = "Graduate/PhD-level internship outside undergraduate target."
    phd["score"]["degree_eligible"] = False
    phd["score"]["degree_ineligible_reason"] = "Graduate/PhD-level internship outside undergraduate target."

    subject, body = render_digest([phd, backend])

    assert subject == "Internship Watcher: 1 new SWE-intern match"
    assert "IT Internship (BackEnd, Java)" in body
    assert "Machine Learning Engineer PhD Intern" not in body


def test_render_digest_shows_alumni_index_summary():
    subject, body = render_digest(
        [match("Bosch", "IT Internship (BackEnd, Java)", 82, fit_score=90, role_track="backend")],
        alumni_summary={"status": "loaded", "records_loaded": 124, "employers_indexed": 80},
    )

    assert subject == "Internship Watcher: 1 new SWE-intern match"
    assert "Alumni index: 124 records across 80 employers" in body
    assert "alumni you know there: No matching alumni in loaded roster" in body


def test_render_digest_shows_active_terms_and_non_ok_season_warning_with_alumni_header():
    subject, body = render_digest(
        [match("Bosch", "IT Internship (BackEnd, Java)", 82, fit_score=90, role_track="backend")],
        alumni_summary={"status": "loaded", "records_loaded": 124, "employers_indexed": 80},
        active_terms=("Fall 2026", "Summer 2027"),
        season_status="rollover_due",
    )

    assert subject == "Internship Watcher: 1 new SWE-intern match"
    assert "Active internship terms: Fall 2026, Summer 2027" in body
    assert "Season configuration warning: rollover_due" in body
    assert "Alumni index: 124 records across 80 employers" in body


def test_render_digest_does_not_claim_no_alumni_when_roster_missing():
    _subject, body = render_digest(
        [match("Bosch", "IT Internship (BackEnd, Java)", 82, fit_score=90, role_track="backend")],
        alumni_summary={"status": "missing", "records_loaded": 0, "employers_indexed": 0},
    )

    assert "Alumni index missing, no alumni matching was performed" in body
    assert "alumni you know there: Alumni matching disabled; roster not loaded" in body
    assert "No alumni on file" not in body


def test_send_digest_uses_timeout_for_live_smtp_without_logging_recipient(monkeypatch, caplog):
    calls = {}

    class FakeSMTP:
        def __init__(self, host, port, *, timeout):
            calls["host"] = host
            calls["port"] = port
            calls["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, message):
            calls["to"] = message["To"]

    monkeypatch.setenv("WATCHER_SEND_EMAIL", "1")
    monkeypatch.setenv("SMTP_USER", "from@example.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "app-password")
    monkeypatch.setenv("EMAIL_TO", "to@example.com")
    monkeypatch.setattr("watcher.notify.smtplib.SMTP_SSL", FakeSMTP)
    caplog.set_level(logging.INFO, logger="watcher.notify")

    assert send_digest([match("DirectCo", "Software Engineer Intern", 80)]) is True
    assert calls["timeout"] == SMTP_TIMEOUT_SECONDS
    assert calls["login"] == ("from@example.com", "app-password")
    assert calls["to"] == "to@example.com"
    assert "to@example.com" not in caplog.text


@pytest.mark.parametrize("false_value", ["0", "false", "no", "off"])
def test_explicit_false_send_mode_overrides_truthy_dotenv(tmp_path, monkeypatch, false_value):
    env_path = tmp_path / ".env"
    env_path.write_text("WATCHER_SEND_EMAIL=1\n", encoding="utf-8")
    monkeypatch.setenv("WATCHER_SEND_EMAIL", false_value)

    class ForbiddenSMTP:
        def __init__(self, *args, **kwargs):
            raise AssertionError("SMTP must not be opened in explicit false mode")

    monkeypatch.setattr("watcher.notify.smtplib.SMTP_SSL", ForbiddenSMTP)
    load_dotenv(env_path)
    output = io.StringIO()

    assert send_digest([match("DirectCo", "Software Engineer Intern", 80)], output=output) is False
    assert os.environ["WATCHER_SEND_EMAIL"] == false_value
    assert DRY_RUN_HEADER in output.getvalue()


def test_actions_priming_can_suppress_private_alumni_from_dry_run_output(monkeypatch, caplog):
    private_name = "Private Alumni Person"
    private_link = "https://linkedin.example/private-person"
    private_match = match(
        "DirectCo",
        "Software Engineer Intern",
        80,
        alumni=[
            {
                "name": private_name,
                "occupation": "Engineer",
                "linkedin_url": private_link,
            }
        ],
    )
    monkeypatch.setenv("WATCHER_SEND_EMAIL", "0")
    monkeypatch.setenv("WATCHER_SUPPRESS_DRY_RUN_DIGEST", "1")
    caplog.set_level(logging.INFO, logger="watcher.notify")
    output = io.StringIO()

    assert send_digest([private_match], output=output) is False
    assert output.getvalue() == ""
    assert private_name not in caplog.text
    assert private_link not in caplog.text
    assert "Dry-run digest output suppressed" in caplog.text
