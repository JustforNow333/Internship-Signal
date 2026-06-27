from app.dedupe import canonical_key, dedupe, job_id, norm_company, norm_url


def _row(n, **kw):
    base = {"company": "", "title": "", "location": "", "compensation": "",
            "description": "", "requirements": "", "source_url": "",
            "_row_number": n}
    base.update(kw)
    return base


def test_norm_company_strips_suffixes():
    assert norm_company("ZenithSoft Pvt Ltd") == norm_company("zenithsoft")
    assert norm_company("Stripe, Inc.") == norm_company("Stripe")


def test_norm_url_strips_tracking_and_slash():
    a = norm_url("https://careers.datadoghq.com/intern-platform?utm_source=linkedin&ref=board")
    b = norm_url("https://careers.datadoghq.com/intern-platform/")
    assert a == b


def test_norm_url_sorts_query_params():
    a = norm_url("https://example.com/job?department=eng&id=123")
    b = norm_url("https://example.com/job?id=123&department=eng")
    assert a == b


def test_exact_duplicate_removed_and_reported():
    r1 = _row(1, company="Stripe", title="Backend Intern", location="New York, NY")
    r2 = _row(2, company="Stripe", title="Backend Intern", location="New York, NY")
    kept, report = dedupe([r1, r2])
    assert len(kept) == 1
    assert report[0]["row_number"] == 2 and report[0]["duplicate_of"] == 1
    assert report[0]["matched_on"] == "company+title+location"


def test_case_and_whitespace_near_duplicate():
    r1 = _row(1, company="Datadog", title="SWE Intern - Platform", location="New York, NY")
    r2 = _row(2, company="  DATADOG ", title="swe intern - platform", location="new york")
    kept, report = dedupe([r1, r2])
    assert len(kept) == 1 and report[0]["matched_on"] == "company+title+location"


def test_url_duplicate_even_when_titles_differ():
    r1 = _row(1, company="Datadog", title="SWE Intern", source_url="https://x.com/job/1")
    r2 = _row(2, company="Datadog Inc", title="Software Intern",
              source_url="https://x.com/job/1?utm_source=li")
    kept, report = dedupe([r1, r2])
    assert len(kept) == 1 and report[0]["matched_on"] == "source_url"


def test_duplicate_fills_missing_fields_on_kept_row():
    r1 = _row(1, company="Plaid", title="SWE Intern", location="SF")  # no comp
    r2 = _row(2, company="Plaid", title="SWE Intern", location="SF",
              compensation="$48/hr", deadline="2026-07-01")
    kept, report = dedupe([r1, r2])
    assert kept[0]["compensation"] == "$48/hr"
    assert kept[0]["deadline"] == "2026-07-01"
    assert set(report[0]["merged_fields"]) == {"compensation", "deadline"}


def test_dedupe_indexes_fields_filled_from_duplicate():
    r1 = _row(1, company="Plaid", title="SWE Intern", location="SF")
    r2 = _row(2, company="Plaid", title="SWE Intern", location="SF",
              source_url="https://example.com/jobs/plaid-swe")
    r3 = _row(3, company="Plaid", title="Software Intern", location="SF",
              source_url="https://example.com/jobs/plaid-swe?utm_source=board")

    kept, report = dedupe([r1, r2, r3])

    assert kept == [r1]
    assert kept[0]["source_url"] == "https://example.com/jobs/plaid-swe"
    assert [entry["row_number"] for entry in report] == [2, 3]
    assert report[1]["matched_on"] == "source_url"


def test_blank_key_rows_are_not_collapsed_together():
    # Two rows with no company/title/location must both survive.
    r1 = _row(1, description="mystery one")
    r2 = _row(2, description="mystery two")
    kept, report = dedupe([r1, r2])
    assert len(kept) == 2 and report == []


def test_job_id_is_stable_across_formatting():
    a = _row(1, company="Stripe, Inc.", title="Backend Intern", location="New York, NY")
    b = _row(2, company="stripe", title="  backend intern", location="New York")
    assert canonical_key(a) == canonical_key(b)
    assert job_id(a) == job_id(b)
    assert len(job_id(a)) == 10
