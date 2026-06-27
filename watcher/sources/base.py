"""Shared source adapter interfaces and helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html import unescape
from json import JSONDecodeError
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.app.normalize import CANONICAL_COLUMNS
from watcher.config import CompanyCfg

USER_AGENT = "internship-signal-watcher/0.1"
DEFAULT_TIMEOUT_SECONDS = 20


class SourceError(Exception):
    """Base class for catchable source adapter failures."""


class SourceFetchError(SourceError):
    """Raised when a source endpoint cannot be fetched or decoded."""


class SourceSchemaError(SourceError):
    """Raised when a source response has an unexpected shape."""


class Source(Protocol):
    name: str

    def fetch(self, company: CompanyCfg) -> list[dict]:
        """Return canonical-shaped rows or raise SourceError on failure."""


def make_row(*, source: str, source_adapter: str, extra: dict | None = None, **fields: Any) -> dict:
    """Build a canonical-shaped row and attach source metadata.

    `source` is the source priority tag used later by merge/digest code
    ("direct" or "github"). `source_adapter` records the concrete adapter.
    """

    unknown = set(fields) - set(CANONICAL_COLUMNS)
    if unknown:
        raise ValueError(f"Unknown canonical fields: {', '.join(sorted(unknown))}")

    row = {column: "" for column in CANONICAL_COLUMNS}
    for key, value in fields.items():
        row[key] = "" if value is None else str(value)

    metadata = {"source": source, "source_adapter": source_adapter}
    if extra:
        metadata.update(extra)
    row["extra"] = metadata
    return row


def require_token(company: CompanyCfg, source_name: str) -> str:
    token = (company.token or "").strip()
    if not token:
        raise SourceError(f"{source_name} requires a token for {company.name}")
    return token


def fetch_json(url: str, source_name: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Any:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        raise SourceFetchError(f"{source_name} fetch failed with HTTP {exc.code}: {url}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise SourceFetchError(f"{source_name} fetch failed: {url}") from exc

    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise SourceFetchError(f"{source_name} returned invalid JSON: {url}") from exc


def post_json(
    url: str,
    payload: dict,
    source_name: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read()
    except HTTPError as exc:
        raise SourceFetchError(f"{source_name} POST failed with HTTP {exc.code}: {url}") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise SourceFetchError(f"{source_name} POST failed: {url}") from exc

    try:
        return json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise SourceFetchError(f"{source_name} returned invalid JSON: {url}") from exc


def html_to_text(value: Any) -> str:
    text = unescape(str(value or ""))
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*(p|div|li|h[1-6])\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def iso_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()

    raw = str(value).strip()
    if not raw:
        return ""
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return raw[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", raw) else raw


def ensure_list(value: Any, source_name: str, field: str) -> list:
    if not isinstance(value, list):
        raise SourceSchemaError(f"{source_name} expected {field} to be a list")
    return value
