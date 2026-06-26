import json
from pathlib import Path

import pytest

from backend.app.normalize import CANONICAL_COLUMNS
from watcher.config import CompanyCfg
from watcher.sources import GitHubListingsSource, GreenhouseSource, LeverSource, SourceSchemaError

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def assert_canonical_row(row: dict) -> None:
    assert set(row) == set(CANONICAL_COLUMNS) | {"extra"}
    assert row["source_url"]
    assert "source" in row["extra"]
    assert "source_adapter" in row["extra"]


def test_fixture_json_round_trips_utf8_non_ascii(tmp_path):
    expected = {
        "company_name": "Caf\u00e9 Robotics",
        "title": "Students\u2019 CXL\u00ae Internship",
        "locations": ["Montr\u00e9al, QC"],
    }
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")

    loaded = json.loads(path.read_text(encoding="utf-8"))
    output = tmp_path / "fixture_roundtrip.json"
    output.write_text(json.dumps(loaded, ensure_ascii=False), encoding="utf-8")

    assert json.loads(output.read_text(encoding="utf-8")) == expected
    assert b"\xe2\x80\x99" in output.read_bytes()
    assert b"\xc2\xae" in output.read_bytes()


def test_greenhouse_fixture_to_canonical_rows():
    fixture_path = FIXTURES / "greenhouse_asteraearlycareer2026.json"
    fixture_path.read_bytes().decode("ascii")
    payload = load_fixture(fixture_path.name)
    company = CompanyCfg(name="Astera Labs", ats="greenhouse", token="asteraearlycareer2026")

    rows = GreenhouseSource().parse(payload, company)

    assert len(rows) == 5
    first = rows[0]
    assert_canonical_row(first)
    assert first["company"] == "Astera Labs"
    assert first["title"] == "Design Verification Engineer (Intern 2026)"
    assert first["location"] == "Toronto, Ontario, Canada"
    assert first["source_url"] == "https://job-boards.greenhouse.io/asteraearlycareer2026/jobs/4611422005"
    assert first["date_posted"] == "2025-09-22"
    assert first["internship_type"] == "Internship"
    assert first["extra"]["source"] == "direct"
    assert first["extra"]["source_adapter"] == "greenhouse"
    assert first["extra"]["greenhouse_company_name"] == "Astera Labs Early Career"
    assert "<" not in first["description"]
    assert "\u2019" in first["description"]
    assert "\u00ae" in first["description"]
    assert "\u2122" in first["description"]
    for mojibake in (
        "\u00e2\u20ac\u2122",
        "\u00c2\u00ae",
        "\u00e2\u201e\u00a2",
        "\\u00e2\\u20ac\\u2122",
        "\\u00c2\\u00ae",
        "\\u00e2\\u201e\\u00a2",
        "\ufffd",
    ):
        assert mojibake not in first["description"]


def test_greenhouse_unexpected_shape_raises():
    with pytest.raises(SourceSchemaError, match="jobs"):
        GreenhouseSource().parse({"openings": []}, CompanyCfg(name="Astera Labs", token="asteraearlycareer2026"))


def test_lever_fixture_to_canonical_rows():
    payload = load_fixture("lever_ifm_us.json")
    company = CompanyCfg(name="Institute of Foundation Models", ats="lever", token="ifm-us")

    rows = LeverSource().parse(payload, company)

    assert len(rows) == 43
    first = rows[0]
    assert_canonical_row(first)
    assert first["company"] == "Institute of Foundation Models"
    assert first["title"] == "AI Research Internship - LLM"
    assert first["location"] == "Sunnyvale, CA"
    assert first["source_url"] == "https://jobs.lever.co/ifm-us/5342e333-61b9-406d-bfea-61a687a94d1f/apply"
    assert first["date_posted"] == "2025-07-24"
    assert first["remote_status"] == "On-site"
    assert first["compensation"] == "$100,000 - $140,000 per year"
    assert first["extra"]["source"] == "direct"
    assert first["extra"]["source_adapter"] == "lever"
    assert first["extra"]["posting_url"] == "https://jobs.lever.co/ifm-us/5342e333-61b9-406d-bfea-61a687a94d1f"
    assert "Institute of Foundation Models" in first["description"]


def test_lever_unexpected_shape_raises():
    with pytest.raises(SourceSchemaError, match="list"):
        LeverSource().parse({"postings": []}, CompanyCfg(name="Institute of Foundation Models", token="ifm-us"))


def test_github_listings_fixture_filters_active_company_and_terms():
    payload = load_fixture("github_listings_subset.json")
    company = CompanyCfg(name="GitHub", terms=("Summer 2026",))

    rows = GitHubListingsSource().parse(payload, company)

    assert len(rows) == 1
    row = rows[0]
    assert_canonical_row(row)
    assert row["company"] == "GitHub"
    assert row["title"] == "Software Engineering Intern"
    assert row["location"] == "Remote in USA"
    assert row["source_url"] == "https://githubinc.jibeapply.com/jobs/4640"
    assert row["date_posted"] == "2025-10-30"
    assert row["internship_type"] == "Summer 2026"
    assert row["extra"]["source"] == "github"
    assert row["extra"]["source_adapter"] == "github_listings"


def test_github_listings_matches_aliases_and_filters_inactive_or_wrong_term():
    payload = load_fixture("github_listings_subset.json")
    source = GitHubListingsSource()

    alias_rows = source.parse(
        payload,
        CompanyCfg(name="IFM", aliases=("Institute of Foundation Models",), terms=("Summer 2026",)),
    )
    inactive_rows = source.parse(
        payload,
        CompanyCfg(name="Thermo Fisher Scientific", terms=("Summer 2026",)),
    )
    wrong_term_rows = source.parse(
        payload,
        CompanyCfg(name="Tesla", terms=("Summer 2026",)),
    )

    assert [row["company"] for row in alias_rows] == ["Institute of Foundation Models"]
    assert inactive_rows == []
    assert wrong_term_rows == []


def test_github_schema_change_logs_and_raises(caplog):
    with pytest.raises(SourceSchemaError, match="missing keys"):
        GitHubListingsSource().parse([{"company_name": "GitHub"}], CompanyCfg(name="GitHub"))

    assert "GitHub listings schema problem" in caplog.text
