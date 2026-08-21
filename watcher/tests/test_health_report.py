"""The sanitized JSON artifact, the GitHub Actions report, and the heartbeat.

Owned by :mod:`watcher.health.report`, which the scheduled workflow drives
through ``python -m watcher.source_health``.
"""

import io
import json

import pytest

from watcher.config import CompanyCfg
from watcher.source_health import (
    COVERAGE_UNCOVERED,
    ERROR_FETCH,
    SOURCE_KIND_DIRECT,
    STATUS_FAILING,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
    CompanyCoverage,
    HealthSummary,
    calculate_next_state,
    render_final_heartbeat,
    render_github_actions_report,
    transition_for,
    write_health_report,
)
from watcher.tests.health_state_helpers import (
    NOW,
    attempt,
    diagnostic_attempt,
)


def test_json_report_is_sanitized_and_github_annotations_use_transitions(tmp_path):
    company = CompanyCfg(name="Example Co", ats="greenhouse")
    healthy_attempt = diagnostic_attempt(run_id="run-1", rows=1)
    healthy = calculate_next_state(None, healthy_attempt)
    failed_attempt = attempt(
        run_id="run-2",
        succeeded=False,
        rows=None,
        error_kind=ERROR_FETCH,
        error_message="HTTP 503 https://example.test/jobs?token=secret",
    )
    failed = calculate_next_state(healthy, failed_attempt)
    transition = transition_for(healthy, failed)
    coverage = (
        CompanyCoverage(
            company=company.name,
            adapter=company.ats,
            state=COVERAGE_UNCOVERED,
            direct_status=failed.status,
            direct_attempt_succeeded=False,
            direct_rows_returned=None,
            github_backstop_available=False,
        ),
    )
    summary = HealthSummary(
        companies_configured=1,
        direct_attempts=1,
        direct_successes=0,
        direct_zero_successes=0,
        direct_failures=1,
        direct_healthy=0,
        direct_empty=0,
        direct_degraded=0,
        direct_failing=1,
        direct_unsupported=0,
        direct_unknown=0,
        github_feeds_configured=0,
        github_feeds_healthy=0,
        github_feeds_degraded=0,
        github_feeds_failing=0,
        backstop_only_companies=0,
        uncovered_companies=1,
        health_transitions=1,
        health_recoveries=0,
        direct_failed=1,
    )
    report = tmp_path / "health.json"
    write_health_report(
        report,
        run_id="fixed-run",
        observed_at=NOW,
        attempts=(failed_attempt,),
        states={failed.health_key: failed},
        transitions=(transition,),
        coverage=coverage,
        summary=summary,
        run_metadata={
            "configured_terms": "Summer_2027",
            "season_status": "ok",
            "workday_transport": {
                "attempted_tenants": 59,
                "successful_tenants": 35,
                "failed_tenants": 24,
                "retry_attempts": 48,
                "dominant_error": "html_challenge",
                "dominant_error_count": 24,
                "likely_shared_incident": True,
            },
        },
    )
    raw = report.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert data["run_id"] == "fixed-run"
    assert data["summary"]["direct_failed"] == 1
    assert data["run"]["workday_transport"]["dominant_error"] == "html_challenge"
    assert data["run"]["workday_transport"]["failed_tenants"] == 24
    assert "secret" not in raw

    output = io.StringIO()
    summary_path = tmp_path / "summary.md"
    render_github_actions_report(report, summary_path=summary_path, output=output)
    annotations = output.getvalue()
    assert "::warning::WORKDAY TRANSPORT INCIDENT:" in annotations
    assert "failed=24" in annotations
    assert "dominant_error=html_challenge" in annotations
    assert (
        "::warning::SOURCE HEALTH: Example Co: "
        "healthy_with_listings -> failed"
    ) in annotations
    assert "::error::SOURCE COVERAGE: Example Co was uncovered" in annotations
    assert "Internship watcher run" in summary_path.read_text(encoding="utf-8")
    assert "Likely shared incident: `yes`" in summary_path.read_text(encoding="utf-8")


def test_final_heartbeat_forwards_every_application_field_and_appends_persistence():
    application = (
        "HEARTBEAT: ran, rows_fetched=16295, jobs_scored=15121, matches=68, new=0, errors=1, "
        "season_status=ok, configured_terms=Summer_2027, github_feeds_configured=1, "
        "github_feeds_succeeded=1, companies_configured=129, direct_healthy=57, direct_empty=1, "
        "direct_degraded=1, direct_failing=0, direct_unsupported=70, github_feeds_healthy=1, "
        "backstop_only_companies=70, uncovered_companies=0, health_transitions=0, "
        "health_recoveries=0, alumni_csv_status=loaded-json-map, alumni_records_loaded=150, "
        "alumni_employers_indexed=128, sent=no, seen_marked=0, future_metric=123"
    )

    final = render_final_heartbeat(
        application,
        seen_loaded=70,
        seen_saved=70,
        load_status="loaded",
        save_status="pushed",
        scheduled_email_enabled="no",
        pending_due_to_email_disabled=1,
        scheduled_email_config="recognized_false",
    )

    assert final.startswith(application)
    assert "future_metric=123" in final
    assert "season_status=ok" in final
    assert "github_feeds_succeeded=1" in final
    assert "direct_degraded=1" in final
    assert "alumni_records_loaded=150" in final
    assert "sent=no, seen_marked=0" in final
    assert "scheduled_email_enabled=no" in final
    assert "pending_due_to_email_disabled=1" in final
    assert "scheduled_email_config=recognized_false" in final
    assert final.endswith("seen_loaded=70, seen_saved=70, seen_store=loaded/pushed")
    assert "\n" not in final
    assert "\r" not in final


def test_final_heartbeat_represents_unknown_save_state_honestly():
    final = render_final_heartbeat(
        "HEARTBEAT: ran, errors=0",
        seen_loaded="unknown",
        seen_saved="unknown",
        load_status="new",
        save_status="skipped-or-failed",
    )

    assert final.endswith(
        "seen_loaded=unknown, seen_saved=unknown, seen_store=new/skipped-or-failed"
    )


@pytest.mark.parametrize(
    "application",
    ("", "ran, errors=0", "HEARTBEAT: ran, errors=0\nHEARTBEAT: injected"),
)
def test_final_heartbeat_rejects_missing_or_multiline_application_value(application):
    with pytest.raises(ValueError):
        render_final_heartbeat(application)


def _actionable_detail_rows(summary_text: str) -> list[list[str]]:
    """Return the parsed cells of the 'Actionable source details' table."""

    lines = summary_text.splitlines()
    start = lines.index("### Actionable source details")
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0] in {"Category", "---"} or set(cells[0]) == {"-"}:
            continue
        rows.append(cells)
    return rows


def test_actionable_rows_render_company_not_diagnostic_label(tmp_path):
    """Regression: run 20260810T031240Z-e85ea6464f9f rendered a degraded Merck
    Workday source with `failed_requests` in the Company/feed column, because
    the diagnostic loop variable shadowed the source label."""

    summary_path = tmp_path / "summary.md"
    payload = {
        "run_id": "20260810T031240Z-e85ea6464f9f",
        "run": {},
        "summary": {},
        "states": [
            {
                "status": DIRECT_STATUS_DEGRADED,
                "company": "Merck",
                "adapter": "workday",
                "health_key": "company:merck:direct:workday",
                "source_kind": SOURCE_KIND_DIRECT,
                "last_rows_returned": 818,
                "last_malformed_row_count": 0,
                "last_schema_error_row_count": 2,
                "last_duplicate_row_count": 0,
                "last_failed_request_count": 0,
                "last_reason_codes": ["schema_invalid_records_skipped"],
            }
        ],
        "transitions": [],
        "coverage": [],
    }

    report_path = tmp_path / "health.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    render_github_actions_report(
        report_path, output=io.StringIO(), summary_path=summary_path
    )

    rows = _actionable_detail_rows(summary_path.read_text(encoding="utf-8"))
    assert len(rows) == 1
    category, label, adapter, detail = rows[0]
    assert category == DIRECT_STATUS_DEGRADED
    assert label == "Merck"
    assert adapter == "workday"
    assert "rows=818" in detail
    assert "schema=2" in detail
    assert "reasons=schema_invalid_records_skipped" in detail


def test_actionable_rows_render_degraded_failed_and_recovered_labels(tmp_path):
    """Degraded, failed, recovered, and uncovered rows all keep their own
    company/feed label and adapter."""

    summary_path = tmp_path / "summary.md"
    payload = {
        "run_id": "run",
        "run": {},
        "summary": {},
        "states": [
            {
                "status": DIRECT_STATUS_DEGRADED,
                "company": "Merck",
                "adapter": "workday",
                "last_rows_returned": 818,
                "last_schema_error_row_count": 2,
            },
            {
                "status": DIRECT_STATUS_FAILED,
                "company": "Adobe",
                "adapter": "workday",
                "last_error_kind": "fetch_failure/redirected_to_html",
                "last_error_message": "workday returned non-JSON content",
                "last_failed_request_count": 1,
            },
            {
                "status": STATUS_FAILING,
                "feed_label": "github-internships",
                "adapter": "github_markdown_table",
                "last_rows_returned": 0,
            },
        ],
        "transitions": [
            {
                "recovery": True,
                "company": "Workday",
                "adapter": "workday",
                "from_status": DIRECT_STATUS_FAILED,
                "to_status": DIRECT_STATUS_HEALTHY_WITH_LISTINGS,
            }
        ],
        "coverage": [
            {
                "state": COVERAGE_UNCOVERED,
                "company": "Blackstone",
                "adapter": "workday",
            }
        ],
    }

    report_path = tmp_path / "health.json"
    report_path.write_text(json.dumps(payload), encoding="utf-8")
    render_github_actions_report(
        report_path, output=io.StringIO(), summary_path=summary_path
    )

    rows = _actionable_detail_rows(summary_path.read_text(encoding="utf-8"))
    assert [(row[0], row[1], row[2]) for row in rows] == [
        (DIRECT_STATUS_DEGRADED, "Merck", "workday"),
        (DIRECT_STATUS_FAILED, "Adobe", "workday"),
        (STATUS_FAILING, "github-internships", "github_markdown_table"),
        ("recovered", "Workday", "workday"),
        ("uncovered", "Blackstone", "workday"),
    ]
    # No row may carry a diagnostic key where the label belongs.
    assert not any(
        row[1] in {"malformed", "schema", "duplicates", "failed_requests"} for row in rows
    )
