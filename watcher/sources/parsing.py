"""Shared record parsing and payload-shape validation for direct adapters."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable, Iterable, Mapping

from watcher.sources.contracts import SourceSchemaError

LOGGER = logging.getLogger(__name__)


def ensure_list(value: Any, source_name: str, field: str) -> list:
    if not isinstance(value, list):
        raise SourceSchemaError(f"{source_name} expected {field} to be a list")
    return value


def parse_records(
    records: list,
    parse_record: Callable[[Any], dict],
    *,
    source_name: str,
    company_name: str,
    include: Callable[[Any], bool] | None = None,
    diagnostics: Callable[[int, int, Iterable[str]], None] | None = None,
) -> list[dict]:
    """Retain valid direct-source records while rejecting all-malformed payloads."""

    candidates = [record for record in records if include is None or include(record)]
    rows: list[dict] = []
    malformed = 0
    schema_errors = 0
    for record in candidates:
        try:
            rows.append(parse_record(record))
        except SourceSchemaError:
            if isinstance(record, Mapping):
                schema_errors += 1
            else:
                malformed += 1
    skipped = malformed + schema_errors
    if diagnostics is not None:
        reasons = []
        if malformed:
            reasons.append("malformed_records_skipped")
        if schema_errors:
            reasons.append("schema_invalid_records_skipped")
        diagnostics(malformed, schema_errors, reasons)
    if skipped:
        safe_company = re.sub(
            r"[\x00-\x1f\x7f]+", " ", str(company_name or "unknown")
        ).strip()[:120]
        LOGGER.warning(
            "Skipped %d malformed %s record(s) for %s; %d valid record(s) retained.",
            skipped,
            source_name,
            safe_company or "unknown",
            len(rows),
        )
    if candidates and not rows:
        raise SourceSchemaError(
            f"{source_name} received {len(candidates)} posting record(s) but none were valid"
        )
    return rows


def page_fingerprint(records: list) -> str:
    """Return a bounded digest used to detect broken repeated pagination pages."""

    encoded = json.dumps(
        records, sort_keys=True, ensure_ascii=False, default=str
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
