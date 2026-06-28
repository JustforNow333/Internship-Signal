import copy
import logging

from watcher.alumni import attach_alumni, load_alumni, match_alumni


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
