import base64
import copy
import json
import logging

from watcher.config import CompanyCfg
from watcher.alumni import (
    AlumniError,
    attach_alumni,
    load_alumni,
    load_company_alumni_json,
    load_default_alumni,
    match_alumni,
)
from watcher.notify import render_digest
import pytest


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


def company_json_text():
    return json.dumps({
        "bosch": [
            {
                "name": "David Chu",
                "occupation": "Commercial Project Management - APAC",
                "linkedin_url": "https://www.linkedin.com/in/fake-david-chu",
                "employer": "Bosch",
            }
        ]
    })


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


def test_default_csv_load_reports_loaded_csv_status(tmp_path):
    path = tmp_path / "alumni.csv"
    path.write_text(CSV_TEXT, encoding="utf-8")

    index, status = load_default_alumni(path, require=True)

    assert status.status == "loaded-csv"
    assert status.records_loaded == 5
    assert status.employers_indexed == 4
    assert [match["name"] for match in match_alumni("OpenAI", index)] == ["Ada Exact", "Dennis Exact"]


def test_company_alumni_json_map_loads_from_file_path(tmp_path):
    path = tmp_path / "company_alumni.json"
    path.write_text(company_json_text(), encoding="utf-8")

    index = load_company_alumni_json(path)

    assert set(index) == {"bosch"}
    assert index["bosch"][0] == {
        "name": "David Chu",
        "occupation": "Commercial Project Management - APAC",
        "linkedin_url": "https://www.linkedin.com/in/fake-david-chu",
        "employer": "Bosch",
    }


def test_company_alumni_json_map_loads_from_base64_env(monkeypatch):
    encoded = base64.b64encode(company_json_text().encode("utf-8")).decode("ascii")
    monkeypatch.setenv("WATCHER_COMPANY_ALUMNI_JSON_B64", encoded)
    monkeypatch.setenv("WATCHER_ALUMNI_CSV", "/missing/alumni.csv")

    index, status = load_default_alumni(require=True)

    assert status.status == "loaded-json-map"
    assert status.path == "env:WATCHER_COMPANY_ALUMNI_JSON_B64"
    assert status.records_loaded == 1
    assert [record["name"] for record in match_alumni("Bosch", index)] == ["David Chu"]


def test_company_alumni_json_map_loads_from_raw_env_var(monkeypatch):
    monkeypatch.setenv("WATCHER_COMPANY_ALUMNI_JSON", company_json_text())

    index, status = load_default_alumni(require=True)

    assert status.status == "loaded-json-map"
    assert status.path == "env:WATCHER_COMPANY_ALUMNI_JSON"
    assert status.records_loaded == 1
    assert [record["name"] for record in match_alumni("Bosch", index)] == ["David Chu"]


def test_invalid_company_alumni_json_raises_clear_error(monkeypatch):
    monkeypatch.setenv("WATCHER_COMPANY_ALUMNI_JSON", "{not valid json")

    with pytest.raises(AlumniError, match="invalid JSON"):
        load_default_alumni(require=True)


def test_bosch_job_matches_bosch_alumni_from_json_map(tmp_path):
    path = tmp_path / "company_alumni.json"
    path.write_text(company_json_text(), encoding="utf-8")
    index = load_company_alumni_json(path)

    annotated = attach_alumni([digest_job("Bosch")], index)
    _subject, body = render_digest(
        annotated,
        alumni_summary={"status": "loaded-json-map", "records_loaded": 1, "employers_indexed": 1},
    )

    assert "David Chu - Commercial Project Management - APAC - https://www.linkedin.com/in/fake-david-chu" in body
    assert "Alumni matching disabled; roster not loaded" not in body


def test_company_alumni_json_is_preferred_over_missing_csv(monkeypatch, tmp_path):
    monkeypatch.setenv("WATCHER_COMPANY_ALUMNI_JSON", company_json_text())
    monkeypatch.setenv("WATCHER_ALUMNI_CSV", str(tmp_path / "missing.csv"))

    index, status = load_default_alumni(require=True)

    assert status.status == "loaded-json-map"
    assert status.path == "env:WATCHER_COMPANY_ALUMNI_JSON"
    assert status.records_loaded == 1
    assert [record["name"] for record in match_alumni("Bosch", index)] == ["David Chu"]


def test_live_email_requires_json_or_csv_alumni_source(monkeypatch, tmp_path):
    for name in (
        "WATCHER_COMPANY_ALUMNI_JSON_B64",
        "WATCHER_COMPANY_ALUMNI_JSON",
        "WATCHER_COMPANY_ALUMNI_JSON_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WATCHER_REQUIRE_ALUMNI", "1")

    with pytest.raises(AlumniError, match="Alumni CSV missing"):
        load_default_alumni(tmp_path / "missing.csv")


def test_dry_run_allows_missing_alumni_with_warning(monkeypatch, tmp_path, caplog):
    for name in (
        "WATCHER_COMPANY_ALUMNI_JSON_B64",
        "WATCHER_COMPANY_ALUMNI_JSON",
        "WATCHER_COMPANY_ALUMNI_JSON_PATH",
        "WATCHER_REQUIRE_ALUMNI",
        "WATCHER_SEND_EMAIL",
    ):
        monkeypatch.delenv(name, raising=False)
    caplog.set_level(logging.WARNING, logger="watcher.alumni")

    index, status = load_default_alumni(tmp_path / "missing.csv")

    assert index == {}
    assert status.status == "missing"
    assert "Alumni CSV missing, alumni matching disabled." in caplog.text


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


def test_compact_json_bosch_group_and_robert_bosch_aliases_match_bosch(tmp_path):
    path = tmp_path / "company_alumni.json"
    path.write_text(
        json.dumps({
            "bosch": [
                {
                    "name": "Grace Bosch",
                    "occupation": "Software Engineer",
                    "linkedin_url": "https://www.linkedin.com/in/fake-bosch-group",
                    "employer": "Bosch Group",
                }
            ]
        }),
        encoding="utf-8",
    )
    index = load_company_alumni_json(path)

    assert [record["name"] for record in match_alumni("Bosch", index)] == ["Grace Bosch"]
    assert [record["name"] for record in match_alumni("Robert Bosch", index)] == ["Grace Bosch"]


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
