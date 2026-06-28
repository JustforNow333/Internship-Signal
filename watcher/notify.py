"""Plain-text email digest rendering and sending for watcher matches."""

from __future__ import annotations

import os
import smtplib
import sys
from email.message import EmailMessage
from typing import Sequence, TextIO

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
DRY_RUN_HEADER = "[DRY RUN - not sent]"
COMPENSATION_UNCLEAR_LABEL = "Compensation unclear or unstated"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "y", "on"}


class NotifyConfigError(RuntimeError):
    """Raised when live email sending is enabled but env config is incomplete."""


def render_digest(matches: Sequence[dict]) -> tuple[str, str]:
    """Return the digest subject and plain-text body, or empty strings for no email."""

    if not matches:
        return "", ""

    sorted_matches = sorted(matches, key=_sort_key)
    count = len(sorted_matches)
    match_word = "match" if count == 1 else "matches"
    posting_word = "posting" if count == 1 else "postings"
    subject = f"Internship Watcher: {count} new SWE-intern {match_word}"
    lines = [
        subject,
        "",
        f"{count} new watched-company SWE-intern {posting_word}, sorted by score.",
        "",
    ]

    for index, job in enumerate(sorted_matches, start=1):
        score = job.get("score", {})
        lines.extend(
            [
                f"{index}. {job.get('company', '')} - {job.get('title', '')}",
                f"   score: {score.get('total', 0)}",
                f"   action / recommendation: {score.get('action_label') or score.get('action') or 'unknown'}",
                f"   top reason: {_top_reason(score)}",
                f"   red flags: {_red_flags(job.get('red_flags') or [])}",
                f"   apply URL: {job.get('source_url') or '(not listed)'}",
                f"   source tag: {_source_tag(job)}",
                f"   alumni you know there: {_alumni_line(job.get('alumni') or [])}",
                "",
            ]
        )
    return subject, "\n".join(lines).rstrip() + "\n"


def send_digest(matches: Sequence[dict], *, output: TextIO | None = None) -> bool:
    """Render and send the digest. Dry-run stdout output is the default."""

    subject, body = render_digest(matches)
    if not subject:
        return False

    if not _send_enabled():
        output = output or sys.stdout
        print(DRY_RUN_HEADER, file=output)
        print(f"Subject: {subject}", file=output)
        print("", file=output)
        print(body, end="" if body.endswith("\n") else "\n", file=output)
        return False

    env = _email_env()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = env["EMAIL_FROM"]
    message["To"] = env["EMAIL_TO"]
    message.set_content(body)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(env["SMTP_USER"], env["SMTP_APP_PASSWORD"])
        smtp.send_message(message)
    return True


def _sort_key(job: dict) -> tuple[int, str, str]:
    score = job.get("score", {}).get("total", 0)
    try:
        score_value = int(score)
    except (TypeError, ValueError):
        score_value = 0
    return (-score_value, str(job.get("company") or "").lower(), str(job.get("title") or "").lower())


def _top_reason(score: dict) -> str:
    reasons = score.get("reasons") or []
    if isinstance(reasons, list) and reasons:
        return str(reasons[0])
    return "(none)"


def _red_flags(flags: Sequence[dict]) -> str:
    normal = []
    muted = []
    for flag in flags:
        label = str(flag.get("label") if isinstance(flag, dict) else flag)
        if label == COMPENSATION_UNCLEAR_LABEL:
            muted.append(f"{label} (muted)")
        else:
            normal.append(label)
    rendered = [*normal, *muted]
    return "; ".join(rendered) if rendered else "none"


def _source_tag(job: dict) -> str:
    extra = job.get("extra") or {}
    source = str(extra.get("source") or "unknown")
    adapter = str(extra.get("source_adapter") or "").strip()
    if source == "direct":
        label = "direct-ATS"
    elif source == "github":
        label = "github backstop"
    else:
        label = source
    return f"{label} ({adapter})" if adapter else label


def _alumni_line(alumni: Sequence[dict]) -> str:
    if not alumni:
        return "No alumni on file"
    return "; ".join(_format_alum(record) for record in alumni)


def _format_alum(record: dict) -> str:
    name = str(record.get("name") or "(unknown name)")
    occupation = str(record.get("occupation") or "occupation not listed")
    linkedin = str(record.get("linkedin_url") or "LinkedIn not listed")
    return f"{name} - {occupation} - {linkedin}"


def _send_enabled() -> bool:
    return str(os.getenv("WATCHER_SEND_EMAIL") or "").strip().lower() in TRUTHY_ENV_VALUES


def _email_env() -> dict[str, str]:
    values = {
        "SMTP_USER": os.getenv("SMTP_USER", "").strip(),
        "SMTP_APP_PASSWORD": os.getenv("SMTP_APP_PASSWORD", "").strip(),
        "EMAIL_TO": os.getenv("EMAIL_TO", "").strip(),
    }
    values["EMAIL_FROM"] = os.getenv("EMAIL_FROM", "").strip() or values["SMTP_USER"]
    missing = [key for key in ("SMTP_USER", "SMTP_APP_PASSWORD", "EMAIL_TO") if not values[key]]
    if missing:
        raise NotifyConfigError(
            "WATCHER_SEND_EMAIL is enabled but required env var(s) are missing: "
            + ", ".join(missing)
        )
    return values
