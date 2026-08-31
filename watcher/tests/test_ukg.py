"""Offline contract, completeness, and diagnostics tests for UKG Recruiting."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from watcher.config import CompanyCfg, load_watchlist
from watcher.sources import SourceSchemaError, UkgSource
from watcher.sources.contracts import JsonHttpResponse, SourceError


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).with_name("fixtures")
SEARCH_PAGE = json.loads(
    (FIXTURES / "ukg_search_page.json").read_text(encoding="utf-8")
)
EMPTY_PAGE = json.loads(
    (FIXTURES / "ukg_empty_page.json").read_text(encoding="utf-8")
)

HOST = "recruiting.example.test"
TENANT = "EXA1047EXAI"
BOARD = "e33a1c2e-8d7a-4008-851e-f7bd1d7bf788"
ENDPOINT = (
    f"https://{HOST}/{TENANT}/JobBoard/{BOARD}/JobBoardView/LoadSearchResults"
)


def _company(**overrides) -> CompanyCfg:
    values = {
        "name": "Example",
        "ats": "ukg",
        "ukg_host": HOST,
        "ukg_tenant": TENANT,
        "ukg_board_id": BOARD,
        "source_url": f"https://{HOST}/{TENANT}/JobBoard/{BOARD}/",
    }
    values.update(overrides)
    return CompanyCfg(**values)


def _uuid(index: int) -> str:
    return f"{index:08x}-1111-4222-8333-444455556666"


def _posting(index: int, *, requisition: str | None = None, **overrides) -> dict:
    record = {
        "Id": _uuid(index),
        "Title": f"Engineer {index}",
        "RequisitionNumber": f"REQ{index:06d}" if requisition is None else requisition,
        "PostedDate": "2026-08-19T15:45:32.99Z",
        "BriefDescription": f"Description {index}",
        "JobCategoryName": "Engineering",
        "FullTime": True,
        "Locations": [
            {
                "LocalizedDescription": "Greer, SC",
                "Address": {
                    "City": "Greer",
                    "State": {"Code": "SC", "Name": "South Carolina"},
                    "Country": {"Code": "USA", "Name": "United States"},
                },
            }
        ],
    }
    record.update(overrides)
    return record


def _page(records: list, total: int) -> dict:
    return {"totalCount": total, "opportunities": records, "locations": []}


def _pages(total: int, page_size: int) -> list[dict]:
    built = []
    for start in range(0, max(total, 1), page_size):
        chunk = [_posting(i) for i in range(start + 1, min(total, start + page_size) + 1)]
        built.append(_page(chunk, total))
        if start + page_size >= total:
            break
    return built or [_page([], total)]


class _Recorder:
    """Serve queued JSON payloads and record each request."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, url: str, payload: dict) -> JsonHttpResponse:
        self.calls.append((url, json.loads(json.dumps(payload))))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return JsonHttpResponse(payload=response, metadata={"status": 200})

    @property
    def skips(self) -> list[int]:
        return [c[1]["opportunitySearch"]["Skip"] for c in self.calls]

    @property
    def orders(self) -> list[str]:
        return [c[1]["opportunitySearch"]["OrderBy"][0]["Value"] for c in self.calls]


def _source(responses: list[object], **kwargs) -> tuple[UkgSource, _Recorder]:
    recorder = _Recorder(responses)
    source = UkgSource(
        request_json=recorder,
        sleeper=lambda _delay: None,
        jitter=lambda _low, _high: 0.0,
        **kwargs,
    )
    return source, recorder


# --- request contract -----------------------------------------------------


def test_request_url_and_payload_match_the_official_search_contract():
    source, recorder = _source([_page([_posting(1)], 1)])

    source.fetch(_company())

    url, payload = recorder.calls[0]
    assert url == ENDPOINT
    assert payload == {
        "opportunitySearch": {
            "Top": 50,
            "Skip": 0,
            "QueryString": "",
            "OrderBy": [
                {
                    "Value": "postedDateDesc",
                    "PropertyName": "PostedDate",
                    "Ascending": False,
                }
            ],
            "Filters": [],
        },
        "matchCriteria": {
            "PreferredJobs": [],
            "Educations": [],
            "LicenseAndCertifications": [],
            "Skills": [],
            "hasNoLicenses": False,
            "SkippedSkills": [],
        },
    }
    assert UkgSource.board_url(HOST, TENANT, BOARD) == _company().source_url


def test_top_and_skip_advance_by_the_configured_page_size():
    source, recorder = _source(_pages(25, 10) + _pages(25, 10), page_size=10)

    rows = source.fetch(_company())

    assert len(rows) == 25
    assert source.pages_requested == 3
    assert recorder.skips[:3] == [0, 10, 20]
    assert {c[1]["opportunitySearch"]["Top"] for c in recorder.calls} == {10}


def test_missing_or_invalid_configuration_fails_before_any_request():
    for overrides, expected in (
        ({"ukg_host": ""}, "ukg_host"),
        ({"ukg_tenant": "bad tenant"}, "ukg_tenant"),
        ({"ukg_board_id": "not-a-uuid"}, "ukg_board_id"),
    ):
        source, recorder = _source([])
        with pytest.raises(SourceError, match=expected):
            source.fetch(_company(**overrides))
        assert recorder.calls == []


def test_constructor_bounds_page_size_and_page_count():
    with pytest.raises(ValueError, match="page_size"):
        UkgSource(page_size=0)
    with pytest.raises(ValueError, match="page_size"):
        UkgSource(page_size=1000)
    with pytest.raises(ValueError, match="max_pages"):
        UkgSource(max_pages=0)


# --- canonical mapping ----------------------------------------------------


def test_sanitized_fixture_maps_identity_dates_and_structured_locations():
    source, _ = _source([SEARCH_PAGE])

    rows = source.fetch(_company())

    assert len(rows) == 2
    first, second = rows
    assert first["title"] == "Equipment Maintenance Technician"
    assert first["location"] == "Greer, South Carolina, United States"
    assert first["date_posted"] == "2026-08-19"
    assert first["description"] == (
        "This position will provide comprehensive maintenance support."
    )
    assert first["source_url"] == (
        f"https://{HOST}/{TENANT}/JobBoard/{BOARD}/OpportunityDetail"
        "?opportunityId=c7386a75-3c4e-47ff-a821-36b2dd61f1d2"
    )
    assert first["extra"]["source_requisition_id"] == (
        f"ukg:{HOST}:{TENANT}:{BOARD}:c7386a75-3c4e-47ff-a821-36b2dd61f1d2"
    )
    assert first["extra"]["ukg_native_id"] == "c7386a75-3c4e-47ff-a821-36b2dd61f1d2"
    assert first["extra"]["ukg_requisition_number"] == "EQUIP004239"
    assert first["extra"]["source_adapter"] == "ukg"
    assert first["extra"]["location_countries"] == "United States"
    assert first["extra"]["location_states"] == "South Carolina"
    assert first["extra"]["job_category"] == "Battery Manufacturing"
    assert first["extra"]["full_time"] is True

    # A null PostedDate stays empty rather than being invented.
    assert second["date_posted"] == ""
    assert second["description"] == ""
    assert second["location"] == "Munich, Germany"
    assert second["extra"]["location_countries"] == "Germany"
    assert "location_states" not in second["extra"]
    assert second["extra"]["full_time"] is False


def test_multiple_and_missing_locations_are_handled_without_invention():
    multi = _posting(
        1,
        Locations=[
            {
                "Address": {
                    "City": "Greer",
                    "State": {"Code": "SC", "Name": "South Carolina"},
                    "Country": {"Code": "USA", "Name": "United States"},
                }
            },
            {
                "Address": {
                    "City": "Munich",
                    "State": None,
                    "Country": {"Code": "DEU", "Name": "Germany"},
                }
            },
        ],
    )
    absent = _posting(2, Locations=None)
    localized = _posting(3, Locations=[{"LocalizedDescription": "Remote - US"}])
    source, _ = _source([_page([multi, absent, localized], 3)])

    rows = source.fetch(_company())

    assert rows[0]["location"] == (
        "Greer, South Carolina, United States; Munich, Germany"
    )
    assert rows[0]["extra"]["location_countries"] == "United States; Germany"
    assert rows[0]["extra"]["location_states"] == "South Carolina"
    assert rows[1]["location"] == ""
    assert rows[2]["location"] == "Remote - US"


# --- completeness ---------------------------------------------------------


def test_short_final_page_completes_the_reported_total():
    source, _ = _source(_pages(23, 10) + _pages(23, 10), page_size=10)

    rows = source.fetch(_company())

    assert len(rows) == 23
    assert source.pages_requested == 3
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.retained_row_count == 23


def test_changing_total_count_fails_closed():
    first = _page([_posting(i) for i in range(1, 11)], 20)
    second = _page([_posting(i) for i in range(11, 21)], 21)
    source, _ = _source([first, second], page_size=10)
    with pytest.raises(SourceSchemaError, match="totalCount changed"):
        source.fetch(_company())


def test_repeated_pagination_page_fails_closed():
    first = _page([_posting(i) for i in range(1, 11)], 20)
    source, _ = _source([first, first], page_size=10)
    with pytest.raises(SourceSchemaError, match="repeated pagination page"):
        source.fetch(_company())


def test_premature_empty_or_short_page_fails_closed():
    first = _page([_posting(i) for i in range(1, 11)], 20)
    empty, _ = _source([first, _page([], 20)], page_size=10)
    with pytest.raises(SourceSchemaError, match="ended before the reported totalCount"):
        empty.fetch(_company())

    short, _ = _source([_page([_posting(i) for i in range(1, 10)], 20)], page_size=10)
    with pytest.raises(SourceSchemaError, match="ended prematurely"):
        short.fetch(_company())


def test_more_records_than_the_total_or_the_page_fails_closed():
    # Within the requested page size, but more records than the board reports.
    over, _ = _source([_page([_posting(i) for i in range(1, 12)], 10)], page_size=20)
    with pytest.raises(SourceSchemaError, match="more records than the reported"):
        over.fetch(_company())

    # More records than the page the adapter actually asked for.
    oversized, _ = _source([_page([_posting(i) for i in range(1, 12)], 50)], page_size=10)
    with pytest.raises(SourceSchemaError, match="more records than the requested page"):
        oversized.fetch(_company())


def test_bounded_maximum_page_safeguard_stops_an_endless_crawl():
    source, _ = _source(_pages(50, 10), page_size=10, max_pages=2)
    with pytest.raises(SourceSchemaError, match="maximum page safeguard"):
        source.fetch(_company())


def test_explicit_zero_result_board_terminates_cleanly():
    source, recorder = _source([EMPTY_PAGE])

    assert source.fetch(_company()) == []
    assert source.pages_requested == 1
    assert source.verification_pages_requested == 0
    assert len(recorder.calls) == 1
    assert source.last_health_diagnostics.complete is True


def test_zero_total_with_records_is_inconsistent():
    source, _ = _source([_page([_posting(1)], 0)])
    with pytest.raises(SourceSchemaError, match="zero-result response was inconsistent"):
        source.fetch(_company())


def test_invalid_envelope_shapes_fail_closed():
    for payload, expected in (
        ([], "expected a JSON object"),
        ({"opportunities": []}, "totalCount"),
        ({"totalCount": -1, "opportunities": []}, "totalCount"),
        ({"totalCount": True, "opportunities": []}, "totalCount"),
        ({"totalCount": 1, "opportunities": {}}, "opportunities to be a list"),
        (
            {"totalCount": 1, "opportunities": [_posting(1)], "locations": {}},
            "locations to be a list",
        ),
    ):
        source, _ = _source([payload])
        with pytest.raises(SourceSchemaError, match=expected):
            source.fetch(_company())


# --- identity -------------------------------------------------------------


def test_duplicate_posting_id_fails_closed():
    source, _ = _source([_page([_posting(1), _posting(1)], 2)])
    with pytest.raises(SourceSchemaError, match="duplicate posting Id"):
        source.fetch(_company())


def test_conflicting_requisition_number_fails_closed():
    clash = _posting(2, requisition="REQ000001")
    source, _ = _source([_page([_posting(1), clash], 2)])
    with pytest.raises(SourceSchemaError, match="RequisitionNumber for conflicting Ids"):
        source.fetch(_company())


def test_missing_requisition_numbers_stay_eligible_and_unconstrained():
    source, _ = _source(
        [_page([_posting(1, requisition=""), _posting(2, requisition="")], 2)]
    )

    rows = source.fetch(_company())

    assert len(rows) == 2
    assert all("ukg_requisition_number" not in row["extra"] for row in rows)


def test_posting_url_is_derived_from_the_stable_id():
    source, _ = _source([_page([_posting(7)], 1)])

    rows = source.fetch(_company())

    assert rows[0]["source_url"] == (
        f"https://{HOST}/{TENANT}/JobBoard/{BOARD}/OpportunityDetail"
        f"?opportunityId={_uuid(7)}"
    )
    assert rows[0]["extra"]["source_scope"] == f"{HOST}:{TENANT}:{BOARD}"


# --- record quality and diagnostics ---------------------------------------


def test_malformed_records_are_skipped_and_diagnosed():
    mixed = _page(
        [_posting(1), "not-an-object", _posting(3, Id="not-a-uuid"), _posting(4)],
        4,
    )
    source, _ = _source([mixed])

    rows = source.fetch(_company())

    assert [row["extra"]["ukg_native_id"] for row in rows] == [_uuid(1), _uuid(4)]
    diagnostics = source.last_health_diagnostics
    assert diagnostics.malformed_row_count == 1
    assert diagnostics.schema_error_row_count == 1
    assert set(diagnostics.reason_codes) == {
        "malformed_records_skipped",
        "schema_invalid_records_skipped",
    }
    assert diagnostics.incomplete is True
    assert diagnostics.degraded is True
    assert diagnostics.complete is False


def test_all_invalid_records_fail_rather_than_reporting_completeness():
    source, _ = _source([_page(["not-an-object"], 1)])
    with pytest.raises(SourceSchemaError, match="none were valid"):
        source.fetch(_company())


def test_wrongly_typed_posting_fields_are_rejected():
    for record, expected in (
        (_posting(1, Title=5), "Title must be a string"),
        (_posting(1, PostedDate=17), "PostedDate must be a string"),
        (_posting(1, Locations={}), "Locations must be a list"),
        (_posting(1, Locations=["x"]), "Locations entries must be objects"),
        (_posting(1, Locations=[{"Address": 5}]), "Address must be an object"),
        (
            _posting(1, Locations=[{"Address": {"Country": 5}}]),
            "Country must be an object",
        ),
    ):
        source, _ = _source([_page([record], 1)])
        with pytest.raises(SourceSchemaError, match="none were valid"):
            source.fetch(_company())


def test_transient_failures_retry_within_the_bounded_policy():
    from watcher.sources.contracts import SourceFetchError

    source, _ = _source(
        [SourceFetchError("temporary", retryable=True), _page([_posting(1)], 1)]
    )
    rows = source.fetch(_company())

    assert len(rows) == 1
    assert source.retry_attempts == 1
    assert source.last_health_diagnostics.failed_request_count == 1
    assert source.last_health_diagnostics.reason_codes == ("request_retry_recovered",)
    assert source.last_health_diagnostics.complete is True


# --- reverse-order verification -------------------------------------------


def test_single_page_crawls_skip_the_reverse_order_pass():
    source, recorder = _source([_page([_posting(1), _posting(2)], 2)])

    rows = source.fetch(_company())

    assert len(rows) == 2
    assert source.pages_requested == 1
    assert source.verification_pages_requested == 0
    assert recorder.orders == ["postedDateDesc"]


def test_multi_page_crawls_verify_against_the_reverse_ordering():
    forward = _pages(15, 10)
    reverse = [
        _page([_posting(i) for i in range(15, 5, -1)], 15),
        _page([_posting(i) for i in range(5, 0, -1)], 15),
    ]
    source, recorder = _source(forward + reverse, page_size=10)

    rows = source.fetch(_company())

    assert len(rows) == 15
    assert source.pages_requested == 2
    assert source.verification_pages_requested == 2
    assert recorder.orders == ["postedDateDesc"] * 2 + ["postedDateAsc"] * 2
    assert recorder.calls[2][1]["opportunitySearch"]["OrderBy"][0] == {
        "Value": "postedDateAsc",
        "PropertyName": "PostedDate",
        "Ascending": True,
    }
    assert source.last_health_diagnostics.complete is True
    assert source.last_health_diagnostics.retained_row_count == 15


def test_reverse_order_identity_disagreement_fails_closed():
    forward = _pages(15, 10)
    swapped = [
        _page([_posting(i) for i in range(15, 5, -1)], 15),
        # One posting silently replaced while the total stayed the same.
        _page([_posting(i) for i in (5, 4, 3, 2, 99)], 15),
    ]
    source, _ = _source(forward + swapped, page_size=10)

    with pytest.raises(SourceSchemaError, match="did not agree on the posting identity"):
        source.fetch(_company())


def test_reverse_pass_rows_are_not_added_to_the_result():
    forward = _pages(15, 10)
    reverse = _pages(15, 10)
    source, _ = _source(forward + reverse, page_size=10)

    rows = source.fetch(_company())

    assert len(rows) == 15
    assert len({row["extra"]["ukg_native_id"] for row in rows}) == 15


# --- configuration, registry, and boundary integration --------------------


def test_real_watchlist_builds_exact_proterra_ukg_configuration():
    config = load_watchlist()
    proterra = next(c for c in config.companies if c.name == "Proterra")

    assert proterra.ats == "ukg"
    assert proterra.ukg_host == "recruiting2.ultipro.com"
    assert proterra.ukg_tenant == "PRO1047PROTI"
    assert proterra.ukg_board_id == "e33a1c2e-8d7a-4008-851e-f7bd1d7bf788"
    assert proterra.source_url == (
        "https://recruiting2.ultipro.com/PRO1047PROTI/JobBoard/"
        "e33a1c2e-8d7a-4008-851e-f7bd1d7bf788/"
    )
    assert proterra.module == ""


def test_registry_builds_the_adapter_without_extra_construction_arguments():
    from watcher.sources.registry import DIRECT_ATS, build_direct_sources

    assert "ukg" in DIRECT_ATS
    built = build_direct_sources()
    assert isinstance(built["ukg"], UkgSource)
    assert built["ukg"].name == "ukg"


def test_tenants_on_one_ultipro_host_share_a_single_origin_limit():
    from watcher.collection_concurrency import direct_origin_key

    one = direct_origin_key("ukg", ukg_host="recruiting2.ultipro.com")
    two = direct_origin_key("ukg", ukg_host="recruiting2.ultipro.com")
    other = direct_origin_key("ukg", ukg_host="recruiting.ultipro.com")

    assert one == two == "https://recruiting2.ultipro.com"
    assert other == "https://recruiting.ultipro.com"
    assert direct_origin_key("ukg") == "adapter:ukg"


def test_board_identity_changes_the_collection_fingerprint():
    from dataclasses import replace

    from watcher.collection_snapshot import collection_config_fingerprint
    from watcher.config import WatcherConfig

    company = _company()
    config = WatcherConfig(companies=(company,))
    for field, value in (
        ("ukg_host", "recruiting.other.test"),
        ("ukg_tenant", "OTH1047OTHI"),
        ("ukg_board_id", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
    ):
        changed = replace(config, companies=(replace(company, **{field: value}),))
        assert collection_config_fingerprint(changed) != (
            collection_config_fingerprint(config)
        )


def test_adapter_reuses_canonical_owners_and_avoids_the_base_facade():
    tree = ast.parse((ROOT / "watcher/sources/ukg.py").read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "watcher.sources.base" not in imported
    assert {
        "watcher.sources.contracts",
        "watcher.sources.direct",
        "watcher.sources.parsing",
        "watcher.sources.retry",
        "watcher.sources.rows",
        "watcher.sources.transport",
    } <= imported
    assert not any(name.startswith("watcher.collection") for name in imported)

    from watcher.sources.direct import DirectRecordAdapter, SinglePayloadDirectAdapter

    assert issubclass(UkgSource, DirectRecordAdapter)
    assert not issubclass(UkgSource, SinglePayloadDirectAdapter)
