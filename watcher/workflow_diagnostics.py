"""Pure scheduled-delivery diagnostics used by the GitHub Actions workflow."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
FALSE_VALUES = frozenset({"0", "false", "no", "n", "off"})

CONFIG_RECOGNIZED_TRUE = "recognized_true"
CONFIG_RECOGNIZED_FALSE = "recognized_false"
CONFIG_MISSING_OR_BLANK = "missing_or_blank"
CONFIG_INVALID = "invalid"
NOT_APPLICABLE = "not_applicable"

_CONFIG_LABELS = {
    CONFIG_RECOGNIZED_TRUE: "recognized true value",
    CONFIG_RECOGNIZED_FALSE: "recognized false value",
    CONFIG_MISSING_OR_BLANK: "missing or blank",
    CONFIG_INVALID: "invalid (treated as false)",
    NOT_APPLICABLE: "not applicable",
}
_INVALID_SEND_WARNING = (
    "Repository variable WATCHER_SEND_EMAIL contains an unrecognized "
    "nonblank value; treating scheduled internship email delivery as disabled."
)


@dataclass(frozen=True)
class SendModeResolution:
    send_email: bool
    config_status: str
    scheduled_email_enabled: str
    configuration_warning: str | None = None


@dataclass(frozen=True)
class ScheduledDeliveryDiagnostics:
    scheduled_email_enabled: str
    scheduled_email_config: str
    pending_due_to_email_disabled: int
    configuration_warning: str | None
    delivery_warning: str | None
    summary_markdown: str


def resolve_send_mode(event_name: str, raw_value: object) -> SendModeResolution:
    """Resolve the existing send boolean while retaining configuration quality."""

    normalized = "" if raw_value is None else str(raw_value).strip().lower()
    if normalized in TRUE_VALUES:
        enabled = True
        parsed_status = CONFIG_RECOGNIZED_TRUE
    elif normalized in FALSE_VALUES:
        enabled = False
        parsed_status = CONFIG_RECOGNIZED_FALSE
    elif not normalized:
        enabled = False
        parsed_status = CONFIG_MISSING_OR_BLANK
    else:
        enabled = False
        parsed_status = CONFIG_INVALID

    if event_name != "schedule":
        return SendModeResolution(
            send_email=enabled,
            config_status=NOT_APPLICABLE,
            scheduled_email_enabled=NOT_APPLICABLE,
        )

    warning = None
    if parsed_status == CONFIG_INVALID:
        warning = _INVALID_SEND_WARNING
    return SendModeResolution(
        send_email=enabled,
        config_status=parsed_status,
        scheduled_email_enabled="yes" if enabled else "no",
        configuration_warning=warning,
    )


def build_scheduled_delivery_diagnostics(
    resolution: SendModeResolution,
    *,
    new_postings: object,
) -> ScheduledDeliveryDiagnostics:
    """Build one bounded warning and the schedule section for the run summary."""

    count = _nonnegative_count(new_postings)
    scheduled_disabled = resolution.scheduled_email_enabled == "no"
    pending = count if scheduled_disabled else 0
    delivery_warning = None

    if scheduled_disabled and count == 0:
        delivery_warning = (
            "Scheduled internship email delivery is disabled. "
            "No new eligible postings were pending in this run. "
            "Set repository variable WATCHER_SEND_EMAIL=true to enable automatic delivery."
        )
    elif scheduled_disabled:
        posting_label = "posting is" if count == 1 else "postings are"
        posting_reference = "The posting was" if count == 1 else "The postings were"
        delivery_warning = (
            f"{count} new eligible {posting_label} pending, but scheduled internship "
            f"email delivery is disabled. {posting_reference} not emailed or marked seen. "
            "Set repository variable WATCHER_SEND_EMAIL=true to enable automatic delivery."
        )

    enabled_label = resolution.scheduled_email_enabled.replace("_", " ")
    config_label = _CONFIG_LABELS.get(resolution.config_status, "unknown")
    summary = (
        "## Scheduled delivery\n\n"
        f"Enabled: `{enabled_label}`\n\n"
        f"Configuration: `{config_label}`\n\n"
        f"Pending because delivery is disabled: `{pending}`\n"
    )
    if resolution.configuration_warning:
        summary += f"\n> {resolution.configuration_warning}\n"
    if delivery_warning:
        summary += f"\n> {delivery_warning}\n"

    return ScheduledDeliveryDiagnostics(
        scheduled_email_enabled=resolution.scheduled_email_enabled,
        scheduled_email_config=resolution.config_status,
        pending_due_to_email_disabled=pending,
        configuration_warning=resolution.configuration_warning,
        delivery_warning=delivery_warning,
        summary_markdown=summary,
    )


def _nonnegative_count(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("new posting count must be a nonnegative integer")
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("new posting count must be a nonnegative integer") from exc
    if count < 0 or str(value).strip() != str(count):
        raise ValueError("new posting count must be a nonnegative integer")
    return count


def _resolution_from_outputs() -> SendModeResolution:
    event_name = os.getenv("GITHUB_EVENT_NAME", "")
    scheduled_enabled = os.getenv("SCHEDULED_EMAIL_ENABLED", NOT_APPLICABLE)
    config_status = os.getenv("SCHEDULED_EMAIL_CONFIG", NOT_APPLICABLE)
    send_email = os.getenv("RESOLVED_SEND_EMAIL", "false") == "true"
    warning = None
    if event_name == "schedule" and config_status == CONFIG_INVALID:
        warning = _INVALID_SEND_WARNING
    return SendModeResolution(
        send_email=send_email,
        config_status=config_status,
        scheduled_email_enabled=scheduled_enabled,
        configuration_warning=warning,
    )


def _append_outputs(path: str, values: dict[str, object]) -> None:
    with Path(path).open("a", encoding="utf-8", newline="\n") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render watcher workflow diagnostics.")
    parser.add_argument("command", choices=("resolve-send-mode", "scheduled-report"))
    args = parser.parse_args(argv)

    if args.command == "resolve-send-mode":
        resolution = resolve_send_mode(
            os.getenv("GITHUB_EVENT_NAME", ""),
            os.getenv("WATCHER_SEND_EMAIL_RAW"),
        )
        print(
            str(resolution.send_email).lower(),
            resolution.config_status,
            resolution.scheduled_email_enabled,
        )
        if resolution.configuration_warning:
            print(f"::warning::{resolution.configuration_warning}", file=sys.stderr)
        return 0

    try:
        diagnostics = build_scheduled_delivery_diagnostics(
            _resolution_from_outputs(),
            new_postings=os.getenv("NEW_POSTINGS", ""),
        )
    except ValueError as exc:
        print(f"::error::SCHEDULED DELIVERY: {exc}", file=sys.stderr)
        return 1

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    output_path = os.getenv("GITHUB_OUTPUT")
    if not summary_path or not output_path:
        print(
            "::error::SCHEDULED DELIVERY: GITHUB_STEP_SUMMARY and GITHUB_OUTPUT are required",
            file=sys.stderr,
        )
        return 1

    with Path(summary_path).open("a", encoding="utf-8", newline="\n") as summary:
        summary.write(diagnostics.summary_markdown)
    _append_outputs(
        output_path,
        {
            "scheduled_email_enabled": diagnostics.scheduled_email_enabled,
            "scheduled_email_config": diagnostics.scheduled_email_config,
            "pending_due_to_email_disabled": (
                diagnostics.pending_due_to_email_disabled
            ),
        },
    )
    if diagnostics.delivery_warning:
        print(f"::warning::{diagnostics.delivery_warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
