from pathlib import Path

import pytest

from watcher.workflow_diagnostics import (
    CONFIG_INVALID,
    CONFIG_MISSING_OR_BLANK,
    CONFIG_RECOGNIZED_FALSE,
    CONFIG_RECOGNIZED_TRUE,
    NOT_APPLICABLE,
    build_scheduled_delivery_diagnostics,
    resolve_send_mode,
)


@pytest.mark.parametrize("raw_value", ("1", "true", "TRUE", " yes ", "y", "on"))
def test_scheduled_recognized_true_values_enable_email(raw_value):
    resolution = resolve_send_mode("schedule", raw_value)

    assert resolution.send_email is True
    assert resolution.config_status == CONFIG_RECOGNIZED_TRUE
    assert resolution.scheduled_email_enabled == "yes"


@pytest.mark.parametrize("raw_value", ("0", "false", "FALSE", " no ", "n", "off"))
def test_scheduled_recognized_false_values_disable_email(raw_value):
    resolution = resolve_send_mode("schedule", raw_value)

    assert resolution.send_email is False
    assert resolution.config_status == CONFIG_RECOGNIZED_FALSE
    assert resolution.scheduled_email_enabled == "no"


@pytest.mark.parametrize("raw_value", (None, "", " \t "))
def test_scheduled_missing_or_blank_value_is_distinct_and_disabled(raw_value):
    resolution = resolve_send_mode("schedule", raw_value)

    assert resolution.send_email is False
    assert resolution.config_status == CONFIG_MISSING_OR_BLANK
    assert resolution.scheduled_email_enabled == "no"


@pytest.mark.parametrize("raw_value", ("ture", "enable", "tru"))
def test_scheduled_invalid_nonblank_value_is_distinct_and_warned(raw_value):
    resolution = resolve_send_mode("schedule", raw_value)
    diagnostics = build_scheduled_delivery_diagnostics(resolution, new_postings=0)

    assert resolution.send_email is False
    assert resolution.config_status == CONFIG_INVALID
    assert "WATCHER_SEND_EMAIL" in resolution.configuration_warning
    assert "unrecognized nonblank value" in resolution.configuration_warning
    assert diagnostics.pending_due_to_email_disabled == 0


def test_scheduled_enabled_run_reports_zero_pending_without_warning():
    resolution = resolve_send_mode("schedule", "true")
    diagnostics = build_scheduled_delivery_diagnostics(resolution, new_postings=3)

    assert diagnostics.scheduled_email_enabled == "yes"
    assert diagnostics.pending_due_to_email_disabled == 0
    assert diagnostics.delivery_warning is None
    assert "Enabled: `yes`" in diagnostics.summary_markdown


def test_scheduled_disabled_run_with_zero_new_reports_zero_pending():
    resolution = resolve_send_mode("schedule", None)
    diagnostics = build_scheduled_delivery_diagnostics(resolution, new_postings=0)

    assert diagnostics.scheduled_email_enabled == "no"
    assert diagnostics.pending_due_to_email_disabled == 0
    assert diagnostics.delivery_warning is not None
    assert "No new eligible postings were pending" in diagnostics.delivery_warning
    assert "Pending because delivery is disabled: `0`" in diagnostics.summary_markdown


@pytest.mark.parametrize(
    ("new_postings", "expected_phrase"),
    (
        (1, "1 new eligible posting is pending"),
        (7, "7 new eligible postings are pending"),
    ),
)
def test_scheduled_disabled_run_reports_actual_pending_count(
    new_postings,
    expected_phrase,
):
    resolution = resolve_send_mode("schedule", "false")
    diagnostics = build_scheduled_delivery_diagnostics(
        resolution,
        new_postings=new_postings,
    )

    assert diagnostics.pending_due_to_email_disabled == new_postings
    assert expected_phrase in diagnostics.delivery_warning
    assert "not emailed or marked seen" in diagnostics.delivery_warning
    assert (
        f"Pending because delivery is disabled: `{new_postings}`"
        in diagnostics.summary_markdown
    )


@pytest.mark.parametrize(("raw_value", "expected_send"), (("false", False), ("true", True)))
def test_manual_send_modes_remain_unchanged_and_have_no_scheduled_warning(
    raw_value,
    expected_send,
):
    resolution = resolve_send_mode("workflow_dispatch", raw_value)
    diagnostics = build_scheduled_delivery_diagnostics(
        resolution,
        new_postings=2,
    )

    assert resolution.send_email is expected_send
    assert resolution.config_status == NOT_APPLICABLE
    assert diagnostics.scheduled_email_enabled == NOT_APPLICABLE
    assert diagnostics.pending_due_to_email_disabled == 0
    assert diagnostics.configuration_warning is None
    assert diagnostics.delivery_warning is None
    assert "Enabled: `not applicable`" in diagnostics.summary_markdown


def test_diagnostics_reject_negative_or_noninteger_counts():
    resolution = resolve_send_mode("schedule", "false")

    with pytest.raises(ValueError):
        build_scheduled_delivery_diagnostics(resolution, new_postings=-1)
    with pytest.raises(ValueError):
        build_scheduled_delivery_diagnostics(resolution, new_postings="one")


def test_workflow_renders_schedule_section_before_other_run_summaries():
    workflow = (
        Path(__file__).parents[2] / ".github" / "workflows" / "watcher.yml"
    ).read_text(encoding="utf-8")

    diagnostics_position = workflow.index(
        "      - name: Scheduled delivery diagnostics"
    )
    persistence_position = workflow.index("      - name: Save seen-store")
    health_summary_position = workflow.index(
        "      - name: Source-health summary and annotations"
    )
    assert diagnostics_position < persistence_position < health_summary_position
    assert "python -m watcher.workflow_diagnostics resolve-send-mode" in workflow
    assert "python -m watcher.workflow_diagnostics scheduled-report" in workflow
    final_heartbeat_step = workflow.split("      - name: Final heartbeat", 1)[1]
    assert (
        "SCHEDULED_EMAIL_ENABLED: "
        "${{ steps.scheduled_delivery.outputs.scheduled_email_enabled }}"
        in final_heartbeat_step
    )
    assert (
        "PENDING_DUE_TO_EMAIL_DISABLED: "
        "${{ steps.scheduled_delivery.outputs.pending_due_to_email_disabled }}"
        in final_heartbeat_step
    )
    assert (
        "SCHEDULED_EMAIL_CONFIG: "
        "${{ steps.scheduled_delivery.outputs.scheduled_email_config }}"
        in final_heartbeat_step
    )
