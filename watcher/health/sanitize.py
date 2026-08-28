"""Total sanitizers, bounds, and UTC helpers. Nothing here may raise."""

from __future__ import annotations

import re
from datetime import datetime
from datetime import timezone
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from watcher.text_safety import safe_text

MAX_ERROR_LENGTH = 320


MAX_FEED_LABEL_LENGTH = 180


MAX_DIAGNOSTIC_COUNT = 1_000_000_000


MAX_REASON_CODES = 12


def sanitize_feed_label(value: object) -> str:
    raw = safe_text(value).strip()
    if not raw:
        return "injected"
    # A malformed authority (bad IPv6 bracket, out-of-range port) must never
    # raise out of a sanitizer: sanitize_error() runs over arbitrary failure
    # text, so one bad URL would otherwise abort the whole run.
    try:
        parsed = urlsplit(raw)
    except ValueError:
        parsed = None
    if parsed is not None and parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
        host = parsed.hostname
        try:
            if parsed.port:
                host = f"{host}:{parsed.port}"
        except ValueError:
            pass
        raw = urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", "", ""))
    else:
        raw = re.sub(r"[?#].*$", "", raw)
    raw = re.sub(r"[\x00-\x1f\x7f]+", " ", raw)
    return raw[:MAX_FEED_LABEL_LENGTH]


def _sanitize_url_match(match: re.Match) -> str:
    raw = match.group(0)
    suffix = ""
    while raw and raw[-1] in ".,;:)":
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    return sanitize_feed_label(raw) + suffix


def sanitize_error(value: object) -> str:
    message = safe_text(value)
    message = re.sub(
        r"https?://[^\s]+",
        _sanitize_url_match,
        message,
    )
    message = re.sub(
        r"(?i)\b([a-z0-9_-]*(?:password|passwd|token|secret|api[_-]?key|authorization))\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    message = re.sub(r"\s+", " ", message).strip()
    return message[:MAX_ERROR_LENGTH]


def safe_token(value: object) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", safe_text(value).strip().casefold()).strip("_")


def safe_error_kind(value: object) -> str:
    """Normalize broad/subtype error kinds while preserving one slash."""

    parts = [safe_token(part) for part in safe_text(value).split("/", 1)]
    return "/".join(part for part in parts if part)[:96]


def safe_run_id(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.:-]+", "-", safe_text(value).strip())[:96] or "unknown"


def sanitize_plain(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", safe_text(value)).strip()[:180]


def utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return utc_datetime(value).isoformat()


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    return utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _bounded_optional_count(value: object) -> int | None:
    if value is None:
        return None
    try:
        return min(MAX_DIAGNOSTIC_COUNT, max(0, int(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _bounded_reason_codes(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        code = safe_token(value)[:80]
        if code and code not in result:
            result.append(code)
        if len(result) >= MAX_REASON_CODES:
            break
    return tuple(result)
