"""Total text sanitizers shared by every source-layer module.

These run over arbitrary URLs, error codes, failure text, and response bodies
from untrusted sources. Each one must be total: it never raises, even on a
malformed URL, and it never lets a payload, credential, or raw query string
reach a log, an exception message, or persisted diagnostics.

`html_to_text` lives here rather than in `rows.py` because it has two callers
in different layers — adapters normalizing a description field, and
`_safe_body_preview` stripping markup out of a failure preview. Keeping it here
lets this module stay the source layer's dependency-free floor.
"""

from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import urlsplit, urlunsplit

MAX_SAFE_PREVIEW_CHARS = 160


def html_to_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _safe_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return re.sub(r"[?#].*$", "", raw)
    if parsed.scheme.casefold() in {"http", "https"} and parsed.hostname:
        host = parsed.hostname
        try:
            if parsed.port:
                host = f"{host}:{parsed.port}"
        except ValueError:
            pass
        return urlunsplit((parsed.scheme.casefold(), host, parsed.path or "/", "", ""))
    return re.sub(r"[?#].*$", "", raw)


def _sanitize_fetch_message(value: object) -> str:
    message = re.sub(r"https?://[^\s]+", lambda match: _safe_url(match.group(0)), str(value or ""))
    message = re.sub(
        r"(?i)\b(?:password|passwd|token|secret|authorization|api[_-]?key|csrf)\s*[:=]\s*[^\s,;]+",
        "[secret-redacted]",
        message,
    )
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    return re.sub(r"\s+", " ", message).strip()[:320]


def _safe_error_code(value: object) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", str(value or "fetch_failure").casefold()).strip("_") or "fetch_failure"


def _safe_body_preview(text: str) -> str:
    preview = html_to_text(text[:4_096])
    preview = re.sub(r"https?://[^\s]+", "[url-redacted]", preview, flags=re.IGNORECASE)
    preview = re.sub(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b", "[email-redacted]", preview)
    preview = re.sub(
        r"(?i)\b(?:token|secret|password|passwd|authorization|api[_-]?key|csrf)\s*[:=]\s*[^\s,;]+",
        "[secret-redacted]",
        preview,
    )
    preview = re.sub(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        "[id-redacted]",
        preview,
        flags=re.IGNORECASE,
    )
    preview = re.sub(r"\b[A-Za-z0-9_-]{32,}\b", "[id-redacted]", preview)
    preview = re.sub(r"\s+", " ", preview).strip()
    return preview[:MAX_SAFE_PREVIEW_CHARS]
