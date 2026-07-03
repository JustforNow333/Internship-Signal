import copy
import logging

from watcher.config import CompanyCfg
from watcher.alumni import attach_alumni, load_alumni, load_default_alumni, match_alumni
from watcher.notify import render_digest


CSV_TEXT = """First Name,Last Name,Occupation,Employer,LinkedIn URL
Ada,Exact,Software Engineer,OpenAI,https://www.linkedin.com/in/fake-ada
Dennis,Exact,Engineering Manager,OpenAI,https://www.linkedin.com/in/fake-dennis
Grace,Alias,Data Engineer,Capital One,https://www.linkedin.com/in/fake-grace
Linus,Fuzzy,Security Engineer,Chainanalysis,https://www.linkedin.com/in/fake-linus
Katherine,Fuzzy,Platform Engineer,Salesforce,https://www.linkedin.com/in/fake-katherine
"""


def fake_index(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(CSV_TEXT, encoding="utf-8")
    return load_alumni(path)


def digest_job(company="Bosch", title="IT Internship (BackEnd, Java)"):
    return {
        "company": company,
        "title": title,
        "source_url": "https://example.com/job",
        "score": {
            "total": 82,
            "fit_score": 90,
            "watcher_eligible": True,
            "role_track": "backend",
            "fit_explanation": "Backend Java role with API/database overlap.",
            "watcher_action_label": "Apply now",
            "reasons": ["Role fit: Backend Java role"],
        },
        "role_classification": {"role": "swe", "role_track": "backend"},
        "red_flags": [],
        "extra": {"source": "direct", "source_adapter": "fake"},
    }


def test_exact_match_returns_record(tmp_path):
    index = fake_index(tmp_path)

    matches = match_alumni("OpenAI", index)

    assert [match["name"] for match in matches] == ["Ada Exact", "Dennis Exact"]
    assert matches[0]["occupation"] == "Software Engineer"
    assert matches[0]["linkedin_url"] == "https://www.linkedin.com/in/fake-ada"
    assert matches[0]["employer"] == "OpenAI"


def test_alias_match_returns_record_and_logs(tmp_path, caplog):
    index = fake_index(tmp_path)
    caplog.set_level(logging.INFO, logger="watcher.alumni")

    matches = match_alumni("Capitol One", index)

    assert [match["name"] for match in matches] == ["Grace Alias"]
    assert "ALIAS Capitol One -> capital one" in caplog.text


def test_alias_roster_typo_attaches_to_canonical_posting_and_logs(tmp_path, caplog):
    path = tmp_path / "alumni.csv"
    path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Pablo,Typo,Software Engineer,Capitol One,https://www.linkedin.com/in/fake-pablo
""",
        encoding="utf-8",
    )
    index = load_alumni(path)
    caplog.set_level(logging.INFO, logger="watcher.alumni")

    matches = match_alumni("Capital One", index)

    assert [match["name"] for match in matches] == ["Pablo Typo"]
    assert matches[0]["employer"] == "Capitol One"
    assert "ALIAS Capitol One -> capital one" in caplog.text
    assert "FUZZY" not in caplog.text


def test_mandatory_chainanalysis_fuzzy_logs(tmp_path, caplog):
    index = fake_index(tmp_path)
    caplog.set_level(logging.INFO, logger="watcher.alumni")

    matches = match_alumni("Chainalysis", index)

    assert [match["name"] for match in matches] == ["Linus Fuzzy"]
    assert "FUZZY Chainalysis ~ Chainanalysis (ratio=0.92)" in caplog.text


def test_fuzzy_match_logs_and_near_miss_returns_nothing(tmp_path, caplog):
    index = fake_index(tmp_path)
    caplog.set_level(logging.INFO, logger="watcher.alumni")

    matches = match_alumni("Salesfore", index)
    near_miss = match_alumni("Sales", index)

    assert [match["name"] for match in matches] == ["Katherine Fuzzy"]
    assert "FUZZY Salesfore ~ Salesforce (ratio=0.95)" in caplog.text
    assert near_miss == []


def test_no_match_job_survives_with_empty_alumni_and_no_other_changes(tmp_path):
    index = fake_index(tmp_path)
    job = {
        "id": "abc123",
        "company": "No Match Co",
        "title": "Software Engineer Intern",
        "score": {"total": 75},
        "extra": {"source": "direct"},
    }
    before = copy.deepcopy(job)

    annotated = attach_alumni([job], index)

    assert job == before
    assert len(annotated) == 1
    assert annotated[0]["alumni"] == []
    without_alumni = dict(annotated[0])
    without_alumni.pop("alumni")
    assert without_alumni == before


def test_multiple_alumni_return_all_records(tmp_path):
    index = fake_index(tmp_path)

    matches = match_alumni("OpenAI", index)

    assert [match["name"] for match in matches] == ["Ada Exact", "Dennis Exact"]


def test_attach_alumni_uses_watchlist_alumni_match_aliases(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Ada,Alias,Software Engineer,Ab Initio,https://www.linkedin.com/in/fake-abinitio
""",
        encoding="utf-8",
    )
    index = load_alumni(path)
    jobs = [{"company": "Ab Initio Software", "title": "Software Engineer Intern"}]
    companies = (
        CompanyCfg(
            name="Ab Initio Software",
            ats="github_only",
            alumni_match=("ab initio",),
        ),
    )

    annotated = attach_alumni(jobs, index, companies=companies)

    assert [record["name"] for record in annotated[0]["alumni"]] == ["Ada Alias"]


def test_attach_alumni_uses_watchlist_alias_to_find_company_then_alumni_match(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Grace,APL,Research Engineer,JHU Applied Physics Laboratory,https://www.linkedin.com/in/fake-apl
""",
        encoding="utf-8",
    )
    index = load_alumni(path)
    jobs = [{"company": "JHU APL", "title": "Software Engineer Intern"}]
    companies = (
        CompanyCfg(
            name="Johns Hopkins University Applied Physics Laboratory",
            ats="github_only",
            aliases=("JHU APL",),
            alumni_match=("jhu applied physics laboratory",),
        ),
    )

    annotated = attach_alumni(jobs, index, companies=companies)

    assert [record["name"] for record in annotated[0]["alumni"]] == ["Grace APL"]


def test_bosch_exact_alumni_appears_in_digest(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Ada,Bosch,Backend Engineer,Bosch,https://www.linkedin.com/in/fake-bosch
""",
        encoding="utf-8",
    )
    index = load_alumni(path)
    annotated = attach_alumni([digest_job("Bosch")], index)

    _subject, body = render_digest(
        annotated,
        alumni_summary={"status": "loaded", "records_loaded": 1, "employers_indexed": 1},
    )

    assert "Alumni index: 1 records across 1 employers" in body
    assert "Ada Bosch - Backend Engineer - https://www.linkedin.com/in/fake-bosch" in body


def test_bosch_group_alias_matches_bosch_job(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Grace,Bosch,Software Engineer,Bosch Group,https://www.linkedin.com/in/fake-bosch-group
""",
        encoding="utf-8",
    )
    index = load_alumni(path)

    matches = match_alumni("Bosch", index)

    assert [record["name"] for record in matches] == ["Grace Bosch"]


def test_common_alias_to_alias_match_works_before_fuzzy(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Ada,Bosch,Software Engineer,Bosch Group,https://www.linkedin.com/in/fake-bosch-group
""",
        encoding="utf-8",
    )
    index = load_alumni(path)

    matches = match_alumni("Robert Bosch", index)

    assert [record["name"] for record in matches] == ["Ada Bosch"]


def test_tesla_exact_alumni_appears_for_tesla_job(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Nikola,Tesla,Software Engineer,Tesla,https://www.linkedin.com/in/fake-tesla
""",
        encoding="utf-8",
    )
    index = load_alumni(path)
    annotated = attach_alumni([digest_job("Tesla", "Fullstack Software Engineer Intern")], index)

    _subject, body = render_digest(
        annotated,
        alumni_summary={"status": "loaded", "records_loaded": 1, "employers_indexed": 1},
    )

    assert "Nikola Tesla - Software Engineer - https://www.linkedin.com/in/fake-tesla" in body


def test_missing_alumni_csv_in_live_mode_is_not_quiet_empty_roster(tmp_path, monkeypatch, caplog):
    missing_path = tmp_path / "missing.csv"
    monkeypatch.setenv("WATCHER_SEND_EMAIL", "1")
    caplog.set_level(logging.ERROR, logger="watcher.alumni")

    index, status = load_default_alumni(missing_path)
    _subject, body = render_digest([digest_job("Bosch")], alumni_summary=status.as_dict())

    assert index == {}
    assert status.status == "missing"
    assert "Alumni CSV missing, alumni matching disabled." in caplog.text
    assert "Alumni index missing, no alumni matching was performed" in body


def test_loaded_alumni_with_true_no_match_can_say_no_alumni_on_file(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(
        """First Name,Last Name,Occupation,Employer,LinkedIn URL
Ada,Exact,Software Engineer,OpenAI,https://www.linkedin.com/in/fake-ada
""",
        encoding="utf-8",
    )
    index = load_alumni(path)
    annotated = attach_alumni([digest_job("No Match Co")], index)

    _subject, body = render_digest(
        annotated,
        alumni_summary={"status": "loaded", "records_loaded": 1, "employers_indexed": 1},
    )

    assert "Alumni index: 1 records across 1 employers" in body
    assert "alumni you know there: No matching alumni in loaded roster" in body
