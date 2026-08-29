"""Canonical row construction and date normalization."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from backend.app.normalize import CANONICAL_COLUMNS


def make_row(
    *,
    source: str,
    source_adapter: str,
    extra: dict | None = None,
    **fields: Any,
) -> dict:
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


def iso_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        except (OSError, OverflowError, ValueError):
            return ""

    raw = str(value).strip()
    if not raw:
        return ""
    try:
        normalized = raw.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return raw[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", raw) else raw
