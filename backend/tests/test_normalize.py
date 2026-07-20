from datetime import date

from app.normalize import (
    build_row, clean_cell, days_until, infer_fields, map_headers, parse_date,
)


def test_messy_headers_map_to_canonical_columns():
    headers = ["Company Name", " Job Title ", "Pay", "Qualifications",
               "Remote?", "Apply By", "Link", "Posted", "Type", "Mystery Col"]
    mapping, report = map_headers(headers)
    assert mapping["Company Name"] == "company"
    assert mapping[" Job Title "] == "title"
    assert mapping["Pay"] == "compensation"
    assert mapping["Qualifications"] == "requirements"
    assert mapping["Remote?"] == "remote_status"
    assert mapping["Apply By"] == "deadline"
    assert mapping["Link"] == "source_url"
    assert mapping["Posted"] == "date_posted"
    assert mapping["Type"] == "internship_type"
    assert mapping["Mystery Col"] is None
    assert "Mystery Col" in report["unmapped"]


def test_substring_alias_pass():
    mapping, _ = map_headers(["company name (cleaned)", "apply by date"])
    assert mapping["company name (cleaned)"] == "company"
    assert mapping["apply by date"] == "deadline"


def test_identifier_headers_do_not_map_to_generic_job_or_source_fields():
    mapping, report = map_headers(["Job ID", "Source ID"])

    assert mapping["Job ID"] is None
    assert mapping["Source ID"] is None
    assert {"Job ID", "Source ID"} <= set(report["unmapped"])


def test_duplicate_headers_collide_first_wins():
    mapping, report = map_headers(["Pay", "Salary"])
    assert mapping["Pay"] == "compensation"
    assert mapping["Salary"] is None
    assert report["collisions"] and report["collisions"][0]["header"] == "Salary"


def test_clean_cell_nullish_and_unicode():
    assert clean_cell("  N/A ") == ""
    assert clean_cell("-") == ""
    assert clean_cell("none") == ""
    assert clean_cell("\u00a0Stripe\u00a0") == "Stripe"          # NBSP
    assert clean_cell("₹1.5L – ₹2.4L") == "₹1.5L - ₹2.4L"        # en dash
    assert clean_cell("line1\nline2", single_line=True) == "line1 line2"


def test_build_row_collects_unmapped_into_extra():
    mapping, _ = map_headers(["Company Name", "Pay", "Recruiter Email"])
    row = build_row(
        {"Company Name": "Stripe", "Pay": "$45/hr", "Recruiter Email": "x@y.com"},
        mapping,
    )
    assert row["company"] == "Stripe"
    assert row["compensation"] == "$45/hr"
    assert row["extra"] == {"Recruiter Email": "x@y.com"}


def test_infer_remote_status_and_type():
    row = {"company": "X", "title": "Backend Intern", "location": "Remote",
           "remote_status": "", "internship_type": "",
           "description": "Join us this summer to build APIs."}
    inferred = infer_fields(row)
    assert row["remote_status"] == "Remote"
    assert row["internship_type"] == "Summer"
    assert set(inferred) == {"remote_status", "internship_type"}


def test_infer_location_from_remote_status():
    row = {"location": "", "remote_status": "Remote", "title": "", "description": "",
           "internship_type": "Summer"}
    infer_fields(row)
    assert row["location"] == "Remote"


def test_parse_date_formats():
    assert parse_date("2026-06-30") == date(2026, 6, 30)
    assert parse_date("06/30/2026") == date(2026, 6, 30)
    assert parse_date("June 30, 2026") == date(2026, 6, 30)
    assert parse_date("June 21st, 2026") == date(2026, 6, 21)
    assert parse_date("Rolling") is None
    assert parse_date("") is None


def test_days_until():
    today = date(2026, 6, 9)
    assert days_until("2026-06-12", today) == 3
    assert days_until("2026-06-01", today) == -8
    assert days_until("Rolling", today) is None
