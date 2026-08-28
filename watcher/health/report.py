"""The JSON health artifact, workflow output, heartbeat, and CLI."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence
from typing import TextIO

from watcher.health.models import (
    COVERAGE_UNCOVERED,
    DIRECT_STATUS_DEGRADED,
    DIRECT_STATUS_FAILED,
    DIRECT_STATUS_UNKNOWN,
    STATUS_DEGRADED,
    STATUS_FAILING,
    CompanyCoverage,
    HealthSummary,
    HealthTransition,
    SourceAttempt,
    SourceHealthState,
)
from watcher.health.sanitize import (
    iso_utc,
    safe_error_kind,
    safe_run_id,
    safe_token,
    sanitize_error,
    sanitize_feed_label,
    sanitize_plain,
)
from watcher.health.state import normalize_attempt

def write_health_report(
    path: str | Path,
    *,
    run_id: str,
    observed_at: datetime,
    attempts: Sequence[SourceAttempt],
    states: Mapping[str, SourceHealthState],
    transitions: Sequence[HealthTransition],
    coverage: Sequence[CompanyCoverage],
    summary: HealthSummary,
    run_metadata: Mapping[str, object] | None = None,
) -> None:
    payload = {
        "schema_version": 2,
        "run_id": safe_run_id(run_id),
        "observed_at": iso_utc(observed_at),
        "run": _json_safe(dict(run_metadata or {})),
        "summary": asdict(summary),
        "attempts": [_attempt_dict(attempt) for attempt in attempts],
        "states": [_state_dict(state) for state in sorted(states.values(), key=lambda item: item.health_key)],
        "transitions": [_transition_dict(transition) for transition in transitions],
        "coverage": [_coverage_dict(item) for item in coverage],
    }
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _attempt_dict(attempt: SourceAttempt) -> dict:
    data = asdict(normalize_attempt(attempt))
    data["observed_at"] = iso_utc(attempt.observed_at)
    return data


def _state_dict(state: SourceHealthState) -> dict:
    data = asdict(state)
    for key in ("last_attempt_at", "last_success_at", "last_nonzero_at"):
        data[key] = iso_utc(data[key]) if data[key] else None
    data["feed_label"] = sanitize_feed_label(data["feed_label"]) if data["feed_label"] else None
    data["last_error_kind"] = safe_error_kind(data["last_error_kind"]) if data["last_error_kind"] else None
    data["last_error_message"] = sanitize_error(data["last_error_message"]) if data["last_error_message"] else None
    data["company"] = sanitize_plain(data["company"]) if data["company"] else None
    return data


def _transition_dict(transition: HealthTransition) -> dict:
    data = asdict(transition)
    data["company"] = sanitize_plain(data["company"]) if data["company"] else None
    data["feed_label"] = sanitize_feed_label(data["feed_label"]) if data["feed_label"] else None
    data["adapter"] = safe_token(data["adapter"])
    return data


def _coverage_dict(coverage: CompanyCoverage) -> dict:
    data = asdict(coverage)
    data["company"] = sanitize_plain(data["company"])
    data["adapter"] = safe_token(data["adapter"])
    return data


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return sanitize_error(value)
    if isinstance(value, Mapping):
        return {safe_token(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return sanitize_error(value)


def render_github_actions_report(
    report_path: str | Path,
    *,
    summary_path: str | Path | None,
    output: TextIO = sys.stdout,
    seen_loaded: str = "unknown",
    seen_saved: str = "unknown",
    load_status: str = "unknown",
    save_status: str = "unknown",
) -> None:
    data = json.loads(Path(report_path).read_text(encoding="utf-8"))
    run = data.get("run", {})
    workday = run.get("workday_transport", {}) if isinstance(run, Mapping) else {}
    if isinstance(workday, Mapping) and workday.get("likely_shared_incident"):
        print(
            "::warning::WORKDAY TRANSPORT INCIDENT: "
            f"attempted={int(workday.get('attempted_tenants', 0) or 0)}, "
            f"failed={int(workday.get('failed_tenants', 0) or 0)}, "
            f"dominant_error={safe_error_kind(workday.get('dominant_error', 'unknown')) or 'unknown'}, "
            f"dominant_error_count={int(workday.get('dominant_error_count', 0) or 0)}",
            file=output,
        )
    for transition in data.get("transitions", []):
        label = _json_source_label(transition)
        if transition.get("recovery"):
            print(
                f"::warning::SOURCE HEALTH RECOVERY: {label}: "
                f"{transition.get('from_status')} -> {transition.get('to_status')}",
                file=output,
            )
        elif transition.get("to_status") in {
            STATUS_DEGRADED,
            STATUS_FAILING,
            DIRECT_STATUS_DEGRADED,
            DIRECT_STATUS_FAILED,
            DIRECT_STATUS_UNKNOWN,
        }:
            print(
                f"::warning::SOURCE HEALTH: {label}: "
                f"{transition.get('from_status')} -> {transition.get('to_status')}",
                file=output,
            )
    for item in data.get("coverage", []):
        if item.get("state") == COVERAGE_UNCOVERED:
            print(
                f"::error::SOURCE COVERAGE: {sanitize_error(item.get('company'))} was uncovered for this run",
                file=output,
            )

    if not summary_path:
        return
    summary = data.get("summary", {})
    states = data.get("states", [])
    transitions = data.get("transitions", [])
    coverage = data.get("coverage", [])
    lines = [
        "## Internship watcher run",
        "",
        f"- Run ID: `{data.get('run_id', 'unknown')}`",
        f"- Active terms: {run.get('configured_terms', 'unknown')}",
        f"- Season status: `{run.get('season_status', 'unknown')}`",
        f"- Rows/jobs/matches/new/errors: {run.get('rows_fetched', 'unknown')} / {run.get('jobs_scored', 'unknown')} / {run.get('matches', 'unknown')} / {run.get('new_matches', 'unknown')} / {run.get('errors', 'unknown')}",
        f"- Seen store: loaded {seen_loaded} ({load_status}); saved {seen_saved} ({save_status})",
        f"- Match email sent: `{run.get('digest_sent', False)}`",
        f"- Health email: mode `{run.get('health_email_mode', 'unknown')}`, sent `{run.get('health_alert_sent', False)}`, candidates {run.get('health_alert_candidates', 0)}, cooldown-suppressed {run.get('health_alert_suppressed_by_cooldown', 0)}",
        "",
        "### Source health",
        "",
        "| Metric | Count |",
        "|---|---:|",
    ]
    for label, key in (
        ("Companies configured", "companies_configured"),
        ("Direct degraded", "direct_degraded"),
        ("Direct healthy with listings", "direct_healthy_with_listings"),
        ("Direct healthy empty", "direct_healthy_empty"),
        ("Direct failed", "direct_failed"),
        ("Direct not configured", "direct_not_configured"),
        ("Direct unknown", "direct_unknown"),
        ("GitHub feeds healthy", "github_feeds_healthy"),
        ("Backstop-only companies", "backstop_only_companies"),
        ("Uncovered companies", "uncovered_companies"),
        ("Health transitions", "health_transitions"),
        ("Health recoveries", "health_recoveries"),
    ):
        lines.append(f"| {label} | {int(summary.get(key, 0) or 0)} |")
    workday = run.get("workday_transport", {})
    if isinstance(workday, Mapping):
        lines.extend(
            [
                "",
                "### Workday transport",
                "",
                f"- Attempted/succeeded/failed tenants: {int(workday.get('attempted_tenants', 0) or 0)} / {int(workday.get('successful_tenants', 0) or 0)} / {int(workday.get('failed_tenants', 0) or 0)}",
                f"- Retry attempts: {int(workday.get('retry_attempts', 0) or 0)}",
                f"- Dominant error: `{safe_error_kind(workday.get('dominant_error', 'none')) or 'none'}` ({int(workday.get('dominant_error_count', 0) or 0)})",
                f"- Likely shared incident: `{'yes' if workday.get('likely_shared_incident') else 'no'}`",
            ]
        )
    details = _workflow_detail_rows(states, transitions, coverage)
    lines.extend(["", "### Actionable source details", "", "| Category | Company/feed | Adapter | Detail |", "|---|---|---|---|"])
    lines.extend(details or ["| none | — | — | No degraded, failing, recovered, or uncovered sources |"])
    with Path(summary_path).open("a", encoding="utf-8") as summary_file:
        summary_file.write("\n".join(lines) + "\n")


def _workflow_detail_rows(states: list[dict], transitions: list[dict], coverage: list[dict]) -> list[str]:
    rows = []
    for state in states:
        if state.get("status") not in {
            STATUS_DEGRADED,
            STATUS_FAILING,
            DIRECT_STATUS_DEGRADED,
            DIRECT_STATUS_FAILED,
            DIRECT_STATUS_UNKNOWN,
        }:
            continue
        label = _json_source_label(state)
        error_kind = safe_error_kind(state.get("last_error_kind"))
        detail = state.get("last_error_message") or f"rows={state.get('last_rows_returned')}"
        diagnostic_parts = []
        for diagnostic_label, key in (
            ("malformed", "last_malformed_row_count"),
            ("schema", "last_schema_error_row_count"),
            ("duplicates", "last_duplicate_row_count"),
            ("failed_requests", "last_failed_request_count"),
        ):
            if state.get(key) is not None:
                diagnostic_parts.append(f"{diagnostic_label}={int(state[key])}")
        reasons = state.get("last_reason_codes")
        if isinstance(reasons, (list, tuple)) and reasons:
            diagnostic_parts.append(
                "reasons=" + ",".join(safe_token(item) for item in reasons[:12])
            )
        if diagnostic_parts:
            detail = f"{detail}; {' '.join(diagnostic_parts)}"
        if error_kind:
            detail = f"{error_kind}: {detail}"
        rows.append(_markdown_row(state.get("status"), label, state.get("adapter"), detail))
    for transition in transitions:
        if transition.get("recovery"):
            detail = f"{transition.get('from_status')} -> {transition.get('to_status')}"
            rows.append(_markdown_row("recovered", _json_source_label(transition), transition.get("adapter"), detail))
    for item in coverage:
        if item.get("state") == COVERAGE_UNCOVERED:
            rows.append(_markdown_row("uncovered", item.get("company"), item.get("adapter"), "No successful direct source or GitHub feed"))
    return rows


def _markdown_row(category: object, label: object, adapter: object, detail: object) -> str:
    values = [category, label, adapter, detail]
    clean = [sanitize_error(value).replace("|", "/") for value in values]
    return "| " + " | ".join(clean) + " |"


def _json_source_label(value: Mapping[str, object]) -> str:
    return sanitize_error(value.get("company") or value.get("feed_label") or value.get("health_key") or "unknown")


def render_final_heartbeat(
    application_heartbeat: str,
    *,
    seen_loaded: object = "unknown",
    seen_saved: object = "unknown",
    load_status: object = "unknown",
    save_status: object = "unknown",
    scheduled_email_enabled: object = "unknown",
    pending_due_to_email_disabled: object = "unknown",
    scheduled_email_config: object = "unknown",
) -> str:
    """Append workflow-only diagnostics to an exact application heartbeat."""

    if not application_heartbeat or not application_heartbeat.startswith("HEARTBEAT: "):
        raise ValueError("application heartbeat is missing or invalid")
    if "\n" in application_heartbeat or "\r" in application_heartbeat:
        raise ValueError("application heartbeat must be exactly one line")
    values = (
        _heartbeat_workflow_value(scheduled_email_enabled),
        _heartbeat_workflow_value(pending_due_to_email_disabled),
        _heartbeat_workflow_value(scheduled_email_config),
        _heartbeat_workflow_value(seen_loaded),
        _heartbeat_workflow_value(seen_saved),
        _heartbeat_workflow_value(load_status),
        _heartbeat_workflow_value(save_status),
    )
    return (
        f"{application_heartbeat}, scheduled_email_enabled={values[0]}, "
        f"pending_due_to_email_disabled={values[1]}, scheduled_email_config={values[2]}, "
        f"seen_loaded={values[3]}, seen_saved={values[4]}, "
        f"seen_store={values[5]}/{values[6]}"
    )


def _heartbeat_workflow_value(value: object) -> str:
    text = str(value or "unknown").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", text):
        return "unknown"
    return text[:80]


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render source-health GitHub Actions output.")
    parser.add_argument("command", choices=("workflow-report", "final-heartbeat"))
    parser.add_argument("report_path", nargs="?")
    args = parser.parse_args(argv)
    if args.command == "workflow-report":
        if not args.report_path:
            parser.error("workflow-report requires report_path")
        render_github_actions_report(
            args.report_path,
            summary_path=os.getenv("GITHUB_STEP_SUMMARY"),
            seen_loaded=os.getenv("SEEN_LOADED", "unknown"),
            seen_saved=os.getenv("SEEN_SAVED", "unknown"),
            load_status=os.getenv("LOAD_STATUS", "unknown"),
            save_status=os.getenv("SAVE_STATUS", "unknown"),
        )
    else:
        try:
            print(
                render_final_heartbeat(
                    os.getenv("APPLICATION_HEARTBEAT", ""),
                    seen_loaded=os.getenv("SEEN_LOADED", "unknown"),
                    seen_saved=os.getenv("SEEN_SAVED", "unknown"),
                    load_status=os.getenv("LOAD_STATUS", "unknown"),
                    save_status=os.getenv("SAVE_STATUS", "unknown"),
                    scheduled_email_enabled=os.getenv(
                        "SCHEDULED_EMAIL_ENABLED",
                        "unknown",
                    ),
                    pending_due_to_email_disabled=os.getenv(
                        "PENDING_DUE_TO_EMAIL_DISABLED",
                        "unknown",
                    ),
                    scheduled_email_config=os.getenv(
                        "SCHEDULED_EMAIL_CONFIG",
                        "unknown",
                    ),
                )
            )
        except ValueError as exc:
            print(f"::error::WATCHER HEARTBEAT: {exc}", file=sys.stderr)
            return 1
    return 0
