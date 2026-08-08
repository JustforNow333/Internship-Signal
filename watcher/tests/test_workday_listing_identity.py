"""Listing-derived Workday requisition identity.

Workday exposes ``bulletFields`` as tenant-configurable *display* metadata, so
its first entry is only sometimes a requisition ID. The Air Products tenant
publishes a location there ("Bundang, Korea"), which collapsed every distinct
posting at one site onto a single requisition identity. These tests pin the
shape guard that keeps such values out of the requisition tier so those
postings fall through to the posting-specific URL tier instead.
"""

import pytest

from backend.app.dedupe import posting_identity_key, postings_match
from watcher.config import CompanyCfg
from watcher.sources.workday import WorkdaySource, _source_id

AIR_PRODUCTS_ACCOUNT_MANAGER = (
    "/job/Bundang-Korea/Account-Manager_JR-2026-21062-5"
)
AIR_PRODUCTS_SAFETY_ENGINEER = (
    "/job/Bundang-Korea/Process-Safety-Engineer_JR-2026-20639"
)


def company(name="Air Products"):
    return CompanyCfg(
        name=name,
        ats="workday",
        token="airproducts",
        workday_shard="wd5",
        workday_site="AP0001",
        workday_detail_policy="none",
    )


def posting(*, title, external_path, bullet, locations_text):
    return {
        "title": title,
        "externalPath": external_path,
        "locationsText": locations_text,
        "postedOn": "Posted Today",
        "bulletFields": [bullet],
    }


def parse(posting_payload, cfg=None):
    source = WorkdaySource(min_interval_seconds=0.0, sleeper=lambda _seconds: None)
    return source._parse_posting(
        posting_payload,
        cfg or company(),
        "airproducts",
        "wd5",
        "AP0001",
    )


# 1. Air Products-style location must not become a requisition ID.


@pytest.mark.parametrize(
    "value",
    [
        "Bundang, Korea",
        "Kuala Lumpur, Wilayah Persekutuan Kuala Lumpur",
        "Antofagasta, Chile",
        "L'Isle D'Abeau, France",
        "Decatur, Alabama",
        "Tucuman (0I35)",
        "Alloa GB (Plant) (0B23)",
        "Shah Alam MY (PG/Supply Chain) (0M02)",
        "Residence - Ohio (0936)",
        "Pengerang",
        "2 Locations",
        "",
        "   ",
    ],
)
def test_display_metadata_is_not_a_requisition_id(value):
    assert _source_id({"bulletFields": [value]}) == ""


def test_air_products_listing_yields_no_requisition_id():
    row = parse(
        posting(
            title="Account Manager",
            external_path=AIR_PRODUCTS_ACCOUNT_MANAGER,
            bullet="Bundang, Korea",
            locations_text="Bundang, Korea",
        )
    )
    # Both fields must clear together: stable_requisition_key falls back to
    # ``source_id`` for direct ATS rows when ``source_requisition_id`` is blank.
    assert row["extra"]["source_requisition_id"] == ""
    assert row["extra"]["source_id"] == ""


# 2. Distinct postings at one location keep distinct identities.


def test_distinct_postings_at_same_location_keep_distinct_identities():
    account_manager = parse(
        posting(
            title="Account Manager",
            external_path=AIR_PRODUCTS_ACCOUNT_MANAGER,
            bullet="Bundang, Korea",
            locations_text="Bundang, Korea",
        )
    )
    safety_engineer = parse(
        posting(
            title="Process Safety Engineer",
            external_path=AIR_PRODUCTS_SAFETY_ENGINEER,
            bullet="Bundang, Korea",
            locations_text="Bundang, Korea",
        )
    )

    first = posting_identity_key(account_manager)
    second = posting_identity_key(safety_engineer)

    assert first.startswith("url|")
    assert second.startswith("url|")
    assert first != second
    assert not postings_match(account_manager, safety_engineer)


# 3. Real requisition-shaped values keep working.


@pytest.mark.parametrize(
    "value",
    [
        "R244387",
        "JR260337",
        "R000107469",
        "JR040594",
        "R6849",
        "PT-JR040904",
        "R-113223",
        "R10242395",
        "UNI4118",
        "3166958",
        "2627-004",
        "JR-2026-21062-5",
    ],
)
def test_requisition_shaped_values_are_preserved(value):
    assert _source_id({"bulletFields": [value]}) == value


def test_requisition_identity_still_wins_for_valid_ids():
    row = parse(
        posting(
            title="Software Engineer Intern",
            external_path="/job/New-York/Software-Engineer-Intern_R244387",
            bullet="R244387",
            locations_text="New York, New York",
        ),
        cfg=company("Capital One"),
    )

    assert row["extra"]["source_requisition_id"] == "R244387"
    assert posting_identity_key(row).startswith("requisition|")


def test_same_requisition_at_distinct_urls_still_merges():
    """The requisition tier must keep outranking the URL tier."""

    first = parse(
        posting(
            title="Software Engineer Intern",
            external_path="/job/New-York/Software-Engineer-Intern_R244387",
            bullet="R244387",
            locations_text="New York, New York",
        ),
        cfg=company("Capital One"),
    )
    second = parse(
        posting(
            title="Software Engineer Intern",
            external_path="/job/Richmond/Software-Engineer-Intern_R244387-2",
            bullet="R244387",
            locations_text="Richmond, Virginia",
        ),
        cfg=company("Capital One"),
    )

    assert posting_identity_key(first) == posting_identity_key(second)
    assert postings_match(first, second)


# 4. A requisition-shaped value that merely repeats the location is display data.


def test_value_matching_location_text_is_rejected():
    assert (
        _source_id({"bulletFields": ["A1"], "locationsText": "A1"}) == ""
    )
    # Case and padding must not defeat the check.
    assert (
        _source_id({"bulletFields": [" r244387 "], "locationsText": "R244387"})
        == ""
    )
    # A genuine ID alongside an unrelated location survives.
    assert (
        _source_id({"bulletFields": ["R244387"], "locationsText": "New York"})
        == "R244387"
    )


# 5. Existing seen state must not re-notify after the identity change.


def test_legacy_seen_row_still_matches_the_same_posting():
    """Rows stored under the old collapsed key re-match on the URL tier."""

    stored = {
        "company": "Air Products",
        "title": "Account Manager",
        "location": "Bundang, Korea",
        "source_url": (
            "https://airproducts.wd5.myworkdayjobs.com/AP0001"
            "/job/Bundang-Korea/Account-Manager_JR-2026-21062-5"
        ),
        "extra": {
            "source": "direct",
            "posting_requisition_key": "workday|air_products|bundang,korea",
        },
    }
    current = parse(
        posting(
            title="Account Manager",
            external_path=AIR_PRODUCTS_ACCOUNT_MANAGER,
            bullet="Bundang, Korea",
            locations_text="Bundang, Korea",
        )
    )
    other = parse(
        posting(
            title="Process Safety Engineer",
            external_path=AIR_PRODUCTS_SAFETY_ENGINEER,
            bullet="Bundang, Korea",
            locations_text="Bundang, Korea",
        )
    )

    assert postings_match(stored, current)
    assert not postings_match(stored, other)
