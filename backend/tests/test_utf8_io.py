import json
from datetime import date

from app import config
from app.ingest import process_csv


def test_json_data_file_reads_utf8_non_ascii(tmp_path):
    path = tmp_path / "known_companies.json"
    expected = {
        "tech": ["Caf\u00e9 Robotics", "Montr\u00e9al AI"],
        "non_tech": [],
        "reputable": ["Caf\u00e9 Robotics"],
    }
    path.write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")

    loaded = config._load_json(path, fallback={})

    assert loaded == expected


def test_csv_file_round_trips_utf8_non_ascii(tmp_path):
    csv_text = "\n".join([
        "company,title,location,description,source_url,date_posted",
        (
            "Caf\u00e9 Robotics,Software Engineer Intern,\"Montr\u00e9al, QC\","
            "\"Build CXL\u00ae tools for students\u2019 apps\","
            "https://example.com/jobs/cafe-intern,2026-01-15"
        ),
        "",
    ])
    path = tmp_path / "postings.csv"
    path.write_text(csv_text, encoding="utf-8")

    result = process_csv(path.read_text(encoding="utf-8"), today=date(2026, 6, 9))
    job = result["jobs"][0]

    assert job["company"] == "Caf\u00e9 Robotics"
    assert job["location"] == "Montr\u00e9al, QC"
    assert "CXL\u00ae" in job["description"]
    assert "students' apps" in job["description"]
    assert "\u00c2\u00ae" not in job["description"]
    assert "\u00e2\u20ac\u2122" not in job["description"]
