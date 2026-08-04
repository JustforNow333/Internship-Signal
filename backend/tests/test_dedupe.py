from copy import deepcopy

import pytest

from app.dedupe import (
    analyzed_job_ids,
    canonical_key,
    dedupe,
    is_posting_specific_url,
    job_id,
    norm_company,
    norm_url,
    posting_identity_key,
    posting_specific_url_key,
    stable_requisition_key,
)


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


def test_norm_url_canonicalizes_greenhouse_host_and_redundant_job_id():
    direct = (
        "https://boards.greenhouse.io/andurilindustries/jobs/5148079007"
        "?gh_jid=5148079007&gh_src=board"
    )
    backstop = (
        "https://job-boards.greenhouse.io/andurilindustries/jobs/5148079007/"
    )

    assert norm_url(direct) == norm_url(backstop)


def test_norm_url_continues_to_ignore_all_fragments():
    base = "https://careers.example.test/jobs?department=engineering"

    assert norm_url(f"{base}#/job/ABC123") == norm_url(f"{base}#apply")


def test_exact_duplicate_removed_and_reported():
    r1 = _row(1, company="Stripe", title="Backend Intern", location="New York, NY")
    r2 = _row(2, company="Stripe", title="Backend Intern", location="New York, NY")
    kept, report = dedupe([r1, r2])
    assert len(kept) == 1
    assert report[0]["row_number"] == 2 and report[0]["duplicate_of"] == 1
    assert report[0]["matched_on"] == "company+title+location"


def test_case_and_whitespace_near_duplicate():
    r1 = _row(1, company="Datadog", title="SWE Intern - Platform", location="New York, NY")
    r2 = _row(2, company="  DATADOG ", title="swe intern - platform", location="new york, ny")
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


def test_source_priority_merge_preserves_canonical_fields_and_all_provenance():
    url = "https://example.com/jobs/shared"
    direct = _row(
        1,
        company="Direct Canonical",
        title="Software Engineer Intern",
        location="New York, NY",
        source_url=url,
        extra={"source": "direct", "source_adapter": "greenhouse"},
    )
    simplify = _row(
        2,
        company="Simplify Name",
        title="SWE Intern",
        location="Remote",
        source_url=f"{url}?utm_source=simplify",
        description="Structured description",
        extra={
            "source": "github",
            "source_adapter": "github_listings",
            "source_name": "simplify",
            "source_format": "simplify_json",
            "source_priority": 10,
            "active": True,
        },
    )
    markdown = _row(
        3,
        company="Markdown Name",
        title="Engineering Intern",
        location="Boston, MA",
        source_url=f"{url}?ref=readme",
        internship_type="Summer 2027",
        extra={
            "source": "github",
            "source_adapter": "github_markdown_table",
            "source_name": "sndsh404_summer_2027",
            "source_format": "github_markdown_table",
            "source_priority": 20,
            "source_added_date": "2026-07-20",
            "active": False,
            "closed": True,
            "no_sponsorship": True,
        },
    )

    kept, report = dedupe([markdown, simplify, direct])

    assert len(kept) == 1
    assert len(report) == 2
    merged = kept[0]
    assert merged["company"] == "Direct Canonical"
    assert merged["title"] == "Software Engineer Intern"
    assert merged["location"] == "New York, NY"
    assert merged["description"] == "Structured description"
    assert merged["internship_type"] == "Summer 2027"
    assert merged["extra"]["primary_source"] == "direct_ats"
    assert merged["extra"]["sources"] == [
        "direct_ats",
        "simplify",
        "sndsh404_summer_2027",
    ]
    assert merged["extra"]["active"] is True
    assert merged["extra"]["closed"] is False
    assert merged["extra"]["no_sponsorship"] is True
    assert merged["extra"]["source_added_date"] == "2026-07-20"
    assert (
        merged["extra"]["source_details"]["sndsh404_summer_2027"]["closed"]
        is True
    )


def test_source_priority_result_is_independent_of_feed_row_order():
    url = "https://example.com/jobs/shared"
    simplify = _row(
        1,
        company="Structured Canonical",
        title="Software Engineer Intern",
        source_url=url,
        extra={
            "source": "github",
            "source_adapter": "github_listings",
            "source_name": "simplify",
            "source_format": "simplify_json",
            "source_priority": 10,
            "active": True,
        },
    )
    markdown = _row(
        2,
        company="Markdown Copy",
        title="SWE Intern",
        source_url=url,
        extra={
            "source": "github",
            "source_adapter": "github_markdown_table",
            "source_name": "sndsh404_summer_2027",
            "source_format": "github_markdown_table",
            "source_priority": 20,
            "active": False,
        },
    )

    forward, _ = dedupe(deepcopy([simplify, markdown]))
    reverse, _ = dedupe(deepcopy([markdown, simplify]))

    assert forward == reverse
    assert forward[0]["company"] == "Structured Canonical"
    assert forward[0]["extra"]["primary_source"] == "simplify"


def test_csv_extra_source_column_does_not_become_watcher_provenance():
    # A CSV header literally named "source" collides with the source_url alias
    # and lands in `extra`. That is user data, not adapter provenance: it must
    # not reorder rows or grow synthetic primary_source/sources/source_details.
    rows = [
        _row(1, company="Zeta", title="SWE Intern", source_url="https://z.test/1",
             extra={"source": "LinkedIn"}),
        _row(2, company="Alpha", title="Data Intern", source_url="https://a.test/2",
             extra={"source": "Indeed"}),
    ]

    kept, report = dedupe(deepcopy(rows))

    assert report == []
    assert [row["company"] for row in kept] == ["Zeta", "Alpha"]
    assert [row["_row_number"] for row in kept] == [1, 2]
    assert kept[0]["extra"] == {"source": "LinkedIn"}
    assert kept[1]["extra"] == {"source": "Indeed"}


def _watcher_row(
    n,
    *,
    requisition_id="",
    source="direct",
    source_adapter="greenhouse",
    company="Google",
    title="Software Engineering Intern",
    location="Mountain View, CA",
    source_url="https://careers.example.test/internships",
):
    extra = {
        "source": source,
        "source_adapter": source_adapter,
    }
    if requisition_id:
        extra["source_requisition_id"] = requisition_id
        extra["source_system"] = "greenhouse"
    return _row(
        n,
        company=company,
        title=title,
        location=location,
        source_url=source_url,
        extra=extra,
    )


def test_six_distinct_requisition_ids_at_one_company_all_survive_dedupe():
    rows = [
        _watcher_row(index, requisition_id=f"GOOG-{index}")
        for index in range(1, 7)
    ]

    kept, report = dedupe(rows)

    assert len(kept) == 6
    assert report == []
    assert len({posting_identity_key(row) for row in kept}) == 6


def test_distinct_requisition_ids_survive_even_with_same_generic_url():
    first = _watcher_row(1, requisition_id="GOOG-1")
    second = _watcher_row(2, requisition_id="GOOG-2")

    kept, report = dedupe([first, second])

    assert len(kept) == 2
    assert report == []
    assert is_posting_specific_url(first["source_url"]) is False
    assert (
        is_posting_specific_url(
            "https://careers.example.test/students/internships/software-engineering"
        )
        is False
    )


def test_same_requisition_id_from_direct_and_github_merges_with_direct_priority():
    direct = _watcher_row(
        1,
        requisition_id="4611422005",
        source_url="https://boards.greenhouse.io/google/jobs/4611422005?gh_src=direct",
    )
    github = _watcher_row(
        2,
        source="github",
        source_adapter="github_listings",
        company="Google LLC",
        title="SWE Intern - display wording",
        location="Remote",
        source_url="https://job-boards.greenhouse.io/google/jobs/4611422005?utm_source=github",
    )

    kept, report = dedupe([github, direct])

    assert len(kept) == 1
    assert report[0]["matched_on"] == "requisition_id"
    assert report[0]["cross_source"] is True
    assert kept[0]["extra"]["source"] == "direct"
    assert kept[0]["extra"]["primary_source"] == "direct_ats"


def test_posting_specific_url_tracking_variants_merge_without_requisition_id():
    first = _watcher_row(
        1,
        source_adapter="github_markdown_table",
        source="github",
        source_url="https://careers.example.test/jobs/backend-intern?utm_source=one",
    )
    second = _watcher_row(
        2,
        source_adapter="github_listings",
        source="github",
        source_url="https://careers.example.test/jobs/backend-intern/?ref=two",
    )

    kept, report = dedupe([first, second])

    assert len(kept) == 1
    assert report[0]["matched_on"] == "source_url"


@pytest.mark.parametrize(
    "fragment",
    [
        "job_id=ABC123",
        "jobId=ABC123",
        "?jobId=ABC123",
        "/search?jobId=ABC123",
        "/job/ABC123",
        "jobs/ABC123",
        "/position/ABC123",
        "positions/ABC123",
        "/role/ABC123",
        "roles/%41BC123",
    ],
)
def test_supported_fragment_posting_id_syntaxes_share_one_identity(fragment):
    canonical = _watcher_row(
        1,
        requisition_id="",
        source_url=(
            "https://careers.example.test/jobs?utm_source=direct"
            "#/job/abc123"
        ),
    )
    variant = _watcher_row(
        2,
        requisition_id="",
        source_url=f"https://careers.example.test/jobs?ref=feed#{fragment}",
    )

    kept, report = dedupe([canonical, variant])

    assert len(kept) == 1
    assert report[0]["matched_on"] == "requisition_id"
    assert stable_requisition_key(canonical) == stable_requisition_key(variant)
    assert posting_specific_url_key(canonical) == posting_specific_url_key(variant)
    assert is_posting_specific_url(variant["source_url"]) is True


def test_fragment_ids_keep_same_fallback_postings_and_analyzed_ids_distinct():
    first = _watcher_row(
        1,
        requisition_id="",
        source_url="https://careers.example.test/jobs#/job/ABC123",
    )
    second = _watcher_row(
        2,
        requisition_id="",
        source_url="https://careers.example.test/jobs#/job/XYZ789",
    )

    kept, report = dedupe([first, second])

    assert len(kept) == 2
    assert report == []
    assert len({posting_identity_key(row) for row in kept}) == 2
    assert len(set(analyzed_job_ids(kept))) == 2


def test_fragment_identity_is_scoped_by_host_and_normalized_base_path():
    rows = [
        _watcher_row(
            1,
            requisition_id="",
            source_url="https://one.example.test/jobs/#/job/ABC123",
        ),
        _watcher_row(
            2,
            requisition_id="",
            source_url="https://two.example.test/jobs#/job/ABC123",
        ),
        _watcher_row(
            3,
            requisition_id="",
            source_url="https://one.example.test/careers#/job/ABC123",
        ),
    ]

    kept, report = dedupe(rows)

    assert len(kept) == 3
    assert report == []
    assert len({stable_requisition_key(row) for row in kept}) == 3


@pytest.mark.parametrize(
    "fragment",
    [
        "apply",
        "description",
        "requirements",
        "benefits",
        "overview",
        "top",
        "job_id=",
        "?jobId=",
        "/job/",
        "/opening/ABC123",
        "/jobs/ABC123/details",
        "jobId",
    ],
)
def test_ordinary_blank_and_unsupported_fragments_are_ignored(fragment):
    row = _watcher_row(
        1,
        requisition_id="",
        source_url=f"https://careers.example.test/jobs#{fragment}",
    )

    assert stable_requisition_key(row) == ""
    assert posting_specific_url_key(row) == ""
    assert is_posting_specific_url(row["source_url"]) is False


def test_different_ordinary_anchors_do_not_create_distinct_identities():
    first = _watcher_row(
        1,
        requisition_id="",
        source_url="https://careers.example.test/jobs#apply",
    )
    second = _watcher_row(
        2,
        requisition_id="",
        source_url="https://careers.example.test/jobs#description",
    )

    kept, report = dedupe([first, second])

    assert len(kept) == 1
    assert report[0]["matched_on"] == "company+title+location"
    assert posting_identity_key(first) == posting_identity_key(second)


def test_explicit_source_requisition_id_precedes_fragment_identity():
    explicit = _watcher_row(
        1,
        requisition_id="NATIVE-999",
        source_url="https://careers.example.test/jobs#/job/ABC123",
    )
    fragment_only = _watcher_row(
        2,
        requisition_id="",
        source_url="https://careers.example.test/jobs#/job/ABC123",
    )

    assert stable_requisition_key(explicit).endswith("|native-999")
    assert stable_requisition_key(explicit) != stable_requisition_key(fragment_only)
    assert posting_identity_key(explicit) != posting_identity_key(fragment_only)


def test_different_posting_specific_urls_remain_distinct_despite_same_fallback():
    first = _watcher_row(
        1,
        requisition_id="",
        source_url="https://careers.example.test/jobs/backend-intern-east",
    )
    second = _watcher_row(
        2,
        requisition_id="",
        source_url="https://careers.example.test/jobs/backend-intern-west",
    )

    kept, report = dedupe([first, second])

    assert {row["source_url"] for row in kept} == {
        first["source_url"],
        second["source_url"],
    }
    assert report == []


def test_generic_url_uses_full_fallback_and_keeps_different_roles():
    first = _watcher_row(1, requisition_id="")
    second = _watcher_row(
        2,
        requisition_id="",
        title="Software Engineering Intern, Cloud",
        location="New York, NY",
    )

    kept, report = dedupe([first, second])

    assert {row["title"] for row in kept} == {
        "Software Engineering Intern",
        "Software Engineering Intern, Cloud",
    }
    assert report == []


def test_exact_fallback_duplicate_merges_when_no_stronger_identity_exists():
    first = _watcher_row(1, requisition_id="")
    second = _watcher_row(2, requisition_id="")

    kept, report = dedupe([first, second])

    assert len(kept) == 1
    assert report[0]["matched_on"] == "company+title+location"


def test_fallback_language_titles_keep_distinct_postings_and_analyzed_ids():
    rows = [
        _watcher_row(
            index,
            requisition_id="",
            title=title,
            location="Austin, TX",
            source_url="https://careers.example.test/internships",
        )
        for index, title in enumerate(
            ("C++ Intern", "C# Intern", "C Intern"),
            start=1,
        )
    ]

    kept, report = dedupe(rows)

    # The historical content identity remains unchanged, while posting
    # identity distinguishes the language-specific fallback postings.
    assert len({canonical_key(row) for row in rows}) == 1
    assert len(kept) == 3
    assert report == []
    assert len({posting_identity_key(row) for row in kept}) == 3
    assert len(set(analyzed_job_ids(kept))) == 3


def test_fallback_language_title_formatting_variants_still_merge():
    rows = [
        _watcher_row(
            1,
            requisition_id="",
            title="C++ Intern",
            source_url="",
        ),
        _watcher_row(
            2,
            requisition_id="",
            title="c ++ intern",
            source_url="",
        ),
        _watcher_row(
            3,
            requisition_id="",
            title="C# Intern",
            source_url="",
        ),
        _watcher_row(
            4,
            requisition_id="",
            title="c # intern",
            source_url="",
        ),
    ]

    kept, report = dedupe(rows)

    assert [row["title"] for row in kept] == ["C++ Intern", "C# Intern"]
    assert [entry["matched_on"] for entry in report] == [
        "company+title+location",
        "company+title+location",
    ]


def test_fallback_location_keeps_same_city_in_different_states_distinct():
    illinois = _watcher_row(
        1,
        requisition_id="",
        location="Springfield, IL",
        source_url="",
    )
    massachusetts = _watcher_row(
        2,
        requisition_id="",
        location="Springfield, MA",
        source_url="",
    )

    kept, report = dedupe([illinois, massachusetts])

    assert len(kept) == 2
    assert report == []
    assert posting_identity_key(illinois) != posting_identity_key(massachusetts)


def test_fallback_complete_location_case_and_whitespace_variants_merge():
    first = _watcher_row(
        1,
        requisition_id="",
        location="Springfield, IL",
        source_url="",
    )
    second = _watcher_row(
        2,
        requisition_id="",
        location="  SPRINGFIELD ,   il  ",
        source_url="",
    )

    kept, report = dedupe([first, second])

    assert len(kept) == 1
    assert report[0]["matched_on"] == "company+title+location"


def test_stronger_identities_still_merge_different_titles_and_locations():
    requisition_first = _watcher_row(
        1,
        requisition_id="GOOG-SHARED",
        title="C++ Intern",
        location="Springfield, IL",
    )
    requisition_second = _watcher_row(
        2,
        requisition_id="GOOG-SHARED",
        title="C# Intern",
        location="Springfield, MA",
    )
    url_first = _watcher_row(
        3,
        requisition_id="",
        title="C++ Intern",
        location="Springfield, IL",
        source_url="https://careers.example.test/jobs/shared-language-role",
    )
    url_second = _watcher_row(
        4,
        requisition_id="",
        title="C# Intern",
        location="Springfield, MA",
        source_url=(
            "https://careers.example.test/jobs/shared-language-role"
            "?utm_source=backstop"
        ),
    )

    requisition_kept, requisition_report = dedupe(
        [requisition_first, requisition_second]
    )
    url_kept, url_report = dedupe([url_first, url_second])

    assert len(requisition_kept) == 1
    assert requisition_report[0]["matched_on"] == "requisition_id"
    assert len(url_kept) == 1
    assert url_report[0]["matched_on"] == "source_url"


def test_similar_titles_alone_do_not_merge():
    first = _watcher_row(
        1,
        requisition_id="",
        title="Software Engineer Intern I",
        location="",
        source_url="",
    )
    second = _watcher_row(
        2,
        requisition_id="",
        title="Software Engineer Intern II",
        location="",
        source_url="",
    )

    kept, report = dedupe([first, second])

    assert {row["title"] for row in kept} == {
        "Software Engineer Intern I",
        "Software Engineer Intern II",
    }
    assert report == []
