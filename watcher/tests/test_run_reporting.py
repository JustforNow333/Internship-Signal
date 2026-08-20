"""Console report and single-line application heartbeat output contracts."""

from watcher.collection import WorkdayTransportSummary
from watcher.reporting import print_heartbeat, print_report


def test_print_report_for_matches_and_empty(capsys):
    result = type("Result", (), {
        "errors": [],
        "new_matches": [{
            "company": "DirectCo",
            "title": "Software Engineer Intern",
            "location": "New York, NY",
            "source_url": "https://example.com/jobs/1",
            "extra": {"source": "direct"},
            "score": {"total": 80, "action_label": "Apply now", "reasons": ["Strong role match"]},
            "red_flags": [{"label": "Compensation unclear or unstated"}],
        }],
        "eligibility_exclusions": ({
            "company": "Northrop Grumman",
            "title": "2027 Returning Intern Software Engineer",
            "exclusion_reason": "returning_intern_only",
            "evidence_source": "title",
            "evidence": "2027 Returning Intern Software Engineer",
        },),
    })()

    print_report(result)
    output = capsys.readouterr().out
    assert "New matches: 1" in output
    assert "Configured internship terms: (none)" in output
    assert "Season status: unknown" in output
    assert "GitHub backstop feeds: 0 configured, 0 succeeded" in output
    assert "[direct] DirectCo" in output
    assert "Strong role match" in output
    assert "Categorical eligibility exclusions: 1" in output
    assert "returning_intern_only [title]" in output

    empty = type("Result", (), {"errors": [], "new_matches": []})()
    print_report(empty)
    assert "No new matches." in capsys.readouterr().out


def test_print_heartbeat(capsys):
    result = type("Result", (), {
        "rows_fetched": 3,
        "jobs_scored": 2,
        "matches": [{}, {}],
        "new_matches": [{}],
        "errors": ["BrokenCo: boom"],
        "season_status": "ok",
        "configured_terms": ("Fall 2026", "Summer 2027"),
        "github_feeds_configured": 2,
        "github_feeds_succeeded": 1,
        "alumni_csv_status": "loaded",
        "alumni_records_loaded": 124,
        "alumni_employers_indexed": 80,
        "digest_sent": False,
        "seen_marked": 1,
        "workday_transport": WorkdayTransportSummary(
            attempted_tenants=3,
            successful_tenants=2,
            failed_tenants=1,
            retry_attempts=2,
        ),
    })()

    print_heartbeat(result)

    assert capsys.readouterr().out == (
        "HEARTBEAT: ran, rows_fetched=3, jobs_scored=2, matches=2, "
        "new=1, emailed_suppressed=0, primed_suppressed=0, dry_run_pending=0, "
        "cross_source_duplicates_merged=0, errors=1, notification_mode=unknown, "
        "season_status=ok, configured_terms=Fall_2026|Summer_2027, "
        "github_feeds_configured=2, github_feeds_succeeded=1, "
        "companies_configured=0, direct_healthy=0, direct_empty=0, direct_degraded=0, "
        "direct_failing=0, direct_unsupported=0, direct_healthy_with_listings=0, "
        "direct_healthy_empty=0, direct_failed=0, direct_not_configured=0, "
        "direct_unknown=0, github_feeds_healthy=0, "
        "backstop_only_companies=0, uncovered_companies=0, health_transitions=0, "
        "health_recoveries=0, health_email_mode=off, health_alert_candidates=0, "
        "health_alert_sent=no, health_alert_suppressed_by_cooldown=0, "
        "health_recovery_alerts=0, health_alert_error=no, "
        "source_comparison_github_only=0, source_comparison_direct_only=0, "
        "source_comparison_both=0, source_comparison_persisted=no, "
        "workday_attempted=3, workday_succeeded=2, workday_failed=1, "
        "workday_retry_attempts=2, workday_shared_incident=0, "
        "collection_mode=serial, collection_max_workers=0, "
        "collection_max_observed_concurrency=0, "
        "collection_max_observed_origin_concurrency=0, "
        "collection_max_observed_workday_concurrency=0, "
        "collection_unexpected_task_exceptions=0, "
        "alumni_csv_status=loaded, alumni_records_loaded=124, "
        "alumni_employers_indexed=80, sent=no, seen_marked=1\n"
    )
