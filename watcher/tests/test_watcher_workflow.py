"""Scheduled GitHub Actions workflow contract for the watcher run."""

from pathlib import Path

import yaml

from watcher.source_health import render_final_heartbeat


def test_workflow_preserves_season_and_feed_heartbeat_fields():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    for field in (
        "season_status",
        "configured_terms",
        "github_feeds_configured",
        "github_feeds_succeeded",
    ):
        assert f"{field}=\\([^,]*\\)" in workflow or f"extract_count {field}" in workflow
        assert f'echo "{field}=' in workflow


def test_workflow_preserves_health_fields_validates_db_and_renders_summary():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    health_fields = (
        "companies_configured",
        "direct_healthy",
        "direct_empty",
        "direct_degraded",
        "direct_failing",
        "direct_unsupported",
        "direct_healthy_with_listings",
        "direct_healthy_empty",
        "direct_failed",
        "direct_not_configured",
        "direct_unknown",
        "github_feeds_healthy",
        "backstop_only_companies",
        "uncovered_companies",
        "health_transitions",
        "health_recoveries",
    )
    for field in health_fields:
        assert f"extract_count {field}" in workflow
        assert f'echo "{field}=' in workflow
    assert "WATCHER_HEALTH_REPORT_PATH" in workflow
    assert "$GITHUB_STEP_SUMMARY" in workflow
    assert "python -m watcher.source_health workflow-report" in workflow
    assert "source_health_attempts" in workflow
    assert "source_health_current" in workflow
    assert "select count(*) from seen" in workflow
    assert "where run_id = ?" in workflow
    assert "::error::SEEN-STORE" in workflow
    assert "git worktree add -B \"$DATA_BRANCH\"" in workflow
    assert "checkout --orphan \"$DATA_BRANCH\"" in workflow
    assert "push origin \"HEAD:$DATA_BRANCH\"" in workflow
    assert "git branch -D watcher-data" not in workflow


def test_workflow_caches_analysis_database_without_committing_it():
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml"
    ).read_text(encoding="utf-8")

    assert "WATCHER_ANALYSIS_CACHE_PATH:" in workflow
    assert "actions/cache@v4" in workflow
    assert "STATIC_ANALYSIS_CACHE_VERSION" in workflow
    assert "date -u +%Y-%m-%d" in workflow
    assert "watcher-analysis-${{ runner.os }}-v" in workflow
    assert "restore-keys:" in workflow
    assert "Validate restored analysis cache" in workflow
    assert "pragma quick_check" in workflow
    assert "corrupt-analysis-cache.sqlite" in workflow
    assert "scripts/migrate_analysis_cache.py" in workflow
    assert "--remove-source-table" in workflow
    assert '"analysis_cache" not in tables' in workflow
    save_step = workflow.split(
        "- name: Save seen-store to data branch",
        1,
    )[1].split("- name: Source-health summary", 1)[0]
    assert 'cp "$SEEN_DB_PATH" "$data_worktree/$DATA_DB_FILE"' in save_step
    assert "WATCHER_ANALYSIS_CACHE_PATH" not in save_step


def test_workflow_workday_probe_is_isolated_from_email_seen_and_data_branch():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    assert "workday_transport_probe" in workflow
    assert "python scripts/probe_workday_transport.py" in workflow
    assert 'WATCHER_SEND_EMAIL: "0"' in workflow
    assert 'WATCHER_HEALTH_EMAIL_MODE: "off"' in workflow
    assert "WATCHER_WORKDAY_MIN_INTERVAL_SECONDS" in workflow
    probe_job = workflow.split("  workday-transport-probe:", 1)[1].split("  watcher:", 1)[0]
    assert "--mark-seen-without-send" not in probe_job
    assert "watcher-data" not in probe_job
    assert "WATCHER_SEEN_DB" not in probe_job
    assert "SMTP_" not in probe_job


def test_workflow_health_email_is_independent_and_comparison_is_reported():
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml"
    ).read_text(encoding="utf-8")

    assert "health_email_mode:" in workflow
    assert "WATCHER_HEALTH_EMAIL_MODE" in workflow
    assert "WATCHER_HEALTH_EMAIL_HOUR_UTC" in workflow
    assert "WATCHER_HEALTH_ALERT_COOLDOWN_HOURS" in workflow
    assert "WATCHER_FEED_STALE_HOURS" in workflow
    for mode in ("off", "transitions_only", "failure_only", "daily_summary"):
        assert f'- "{mode}"' in workflow
    for field in (
        "health_alert_candidates",
        "health_alert_suppressed_by_cooldown",
        "health_recovery_alerts",
        "source_comparison_github_only",
        "source_comparison_direct_only",
        "source_comparison_both",
    ):
        assert f"extract_count {field}" in workflow
        assert f'echo "{field}=' in workflow
    assert "python -m watcher.audit" in workflow
    assert "--comparison-json" in workflow
    assert "--comparison-markdown" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert (
        "Source-health email delivery failed; internship-match outcome is unaffected."
        in workflow
    )


def test_workflow_forwards_exact_application_heartbeat_and_keeps_existing_outputs():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    assert "grep '^HEARTBEAT:' \"$RUNNER_TEMP/watcher-run.log\" | tail -n 1" in workflow
    assert 'echo "application_heartbeat<<WATCHER_HEARTBEAT_EOF"' in workflow
    assert "printf '%s\\n' \"$heartbeat\"" in workflow
    assert "[[ \"$heartbeat\" == *$'\\n'* || \"$heartbeat\" == *$'\\r'* ]]" in workflow
    assert "APPLICATION_HEARTBEAT: ${{ steps.run_watcher.outputs.application_heartbeat }}" in workflow
    assert "python -m watcher.source_health final-heartbeat" in workflow
    assert 'echo "HEARTBEAT: ran, rows_fetched=' not in workflow
    assert "application heartbeat unavailable; no final success heartbeat was fabricated" in workflow
    assert "watcher.run did not emit an application heartbeat" in workflow
    assert "Watcher completed with $ERRORS source error(s)" in workflow
    assert "eval " not in workflow
    assert 'source "$' not in workflow

    application = (
        "HEARTBEAT: ran, rows_fetched=10, jobs_scored=9, matches=2, new=1, errors=0, "
        "season_status=ok, configured_terms=Summer_2027, github_feeds_configured=1, "
        "github_feeds_succeeded=1, companies_configured=3, direct_healthy=2, "
        "direct_empty=0, direct_degraded=0, direct_failing=0, direct_unsupported=1, "
        "github_feeds_healthy=1, backstop_only_companies=1, uncovered_companies=0, "
        "health_transitions=0, health_recoveries=0, alumni_csv_status=loaded-json-map, "
        "alumni_records_loaded=2, alumni_employers_indexed=2, sent=no, seen_marked=0, "
        "future_metric=123"
    )
    final = render_final_heartbeat(
        application,
        seen_loaded=7,
        seen_saved=8,
        load_status="loaded",
        save_status="pushed",
    )
    assert final.startswith(application)
    assert "future_metric=123" in final
    assert "season_status=ok" in final
    assert "github_feeds_succeeded=1" in final
    assert "direct_healthy=2" in final
    assert "alumni_csv_status=loaded-json-map" in final
    assert "sent=no, seen_marked=0" in final
    assert final.endswith("seen_loaded=7, seen_saved=8, seen_store=loaded/pushed")
    assert "\n" not in final and "\r" not in final

    for field in (
        "rows_fetched",
        "season_status",
        "github_feeds_configured",
        "direct_healthy",
        "alumni_csv_status",
        "sent",
        "seen_marked",
    ):
        assert f'echo "{field}=' in workflow


def test_workflow_dry_and_prime_modes_are_explicit_and_incompatible_with_live_send():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    assert "prime_seen:" in workflow
    assert "inputs.prime_seen" in workflow
    assert "ACTIONS_WATCHER_PRIME_SEEN" in workflow
    assert "export WATCHER_SEND_EMAIL=0" in workflow
    assert "export WATCHER_SUPPRESS_DRY_RUN_DIGEST=1" in workflow
    assert "unset WATCHER_SEND_EMAIL" not in workflow
    assert 'if [ "${{ steps.mode.outputs.prime_seen }}" = "true" ]; then' in workflow
    assert "args+=(--prime-seen)" in workflow
    assert "cannot both be true" in workflow

    run_step = workflow.split("      - name: Run watcher", 1)[1].split(
        "      - name: Save seen-store", 1
    )[0]
    dry_branch = run_step.split("else", 1)[1]
    assert "--mark-seen-without-send" not in workflow
    assert "args+=(--prime-seen)" in dry_branch


def test_workflow_scheduled_dry_runs_do_not_silently_prime():
    workflow = (Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml").read_text(
        encoding="utf-8"
    )

    assert "raw_prime=\"${ACTIONS_WATCHER_PRIME_SEEN:-}\"" in workflow
    assert 'ACTIONS_WATCHER_PRIME_SEEN: ${{ vars.WATCHER_PRIME_SEEN }}' in workflow
    assert "prime_seen=false" in workflow


def test_workflow_yaml_parses_successfully():
    workflow_path = Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml"
    document = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

    assert isinstance(document, dict)
    assert "jobs" in document
    assert "watcher" in document["jobs"]
