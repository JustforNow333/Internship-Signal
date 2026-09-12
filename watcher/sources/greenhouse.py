"""Greenhouse source adapter."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from internship_signal.domain.identity import norm_url
from watcher.config import CompanyCfg
from watcher.sources.contracts import SourceError, SourceSchemaError, require_token
from watcher.sources.direct import SinglePayloadDirectAdapter
from watcher.sources.parsing import ensure_list
from watcher.sources.rows import iso_date, make_row
from watcher.sources.sanitize import html_to_text
from watcher.sources.transport import fetch_json


class GreenhouseSource(SinglePayloadDirectAdapter):
    name = "greenhouse"

    @staticmethod
    def endpoint(token: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        tokens = _required_tokens(company)
        if len(tokens) == 1:
            return self.parse(
                fetch_json(self.endpoint(tokens[0]), self.name),
                company,
            )

        self._begin_direct_diagnostics()
        rows: list[dict] = []
        id_index: dict[str, int] = {}
        url_index: dict[str, int] = {}
        duplicate_count = 0
        for token in tokens:
            payload = fetch_json(self.endpoint(token), self.name)
            records = self._records_from_payload(payload, company)
            board_rows = self._parse_direct_records(
                records,
                company,
                lambda record: self._parse_record(record, company),
            )
            duplicate_count += _merge_board_rows(
                rows,
                id_index,
                url_index,
                board_rows,
            )

        self._finish_direct_diagnostics(
            rows,
            duplicate_row_count=duplicate_count,
        )
        return rows

    def _records_from_payload(self, payload: Any, company: CompanyCfg) -> list:
        if not isinstance(payload, dict):
            raise SourceSchemaError("greenhouse expected a JSON object")
        return ensure_list(payload.get("jobs"), self.name, "jobs")

    def _parse_record(self, job: Any, company: CompanyCfg) -> dict:
        if not isinstance(job, dict):
            raise SourceSchemaError("greenhouse expected each job to be an object")

        title = str(job.get("title") or "").strip()
        source_url = str(job.get("absolute_url") or "").strip()
        if not title or not source_url:
            raise SourceSchemaError("greenhouse job missing required title or absolute_url")

        location = job.get("location") or {}
        if location is not None and not isinstance(location, dict):
            raise SourceSchemaError("greenhouse job location must be an object")

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=str((location or {}).get("name") or "").strip(),
            description=html_to_text(job.get("content")),
            source_url=source_url,
            date_posted=iso_date(job.get("first_published") or job.get("updated_at")),
            deadline=iso_date(job.get("application_deadline")),
            internship_type=_metadata_value(job.get("metadata"), "Role Type"),
            extra={
                "source_id": str(job.get("id") or ""),
                "source_requisition_id": str(job.get("id") or ""),
                "source_system": self.name,
                "requisition_id": str(job.get("requisition_id") or ""),
                "greenhouse_company_name": str(job.get("company_name") or ""),
                "location": location or {},
            },
        )


def _metadata_value(metadata: Any, name: str) -> str:
    if not isinstance(metadata, list):
        return ""
    for item in metadata:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").strip().lower() == name.lower():
            return str(item.get("value") or "").strip()
    return ""


def _required_tokens(company: CompanyCfg) -> tuple[str, ...]:
    primary = require_token(company, "greenhouse")
    raw_tokens = company.greenhouse_tokens
    if isinstance(raw_tokens, str):
        tokens = (raw_tokens.strip(),)
    else:
        tokens = tuple(str(token or "").strip() for token in raw_tokens)
    if not tokens:
        return (primary,)
    if (
        any(not token for token in tokens)
        or len(tokens) != len(set(tokens))
        or primary not in tokens
    ):
        raise SourceError(f"greenhouse board scope is invalid for {company.name}")
    return tokens


def _merge_board_rows(
    rows: list[dict],
    id_index: dict[str, int],
    url_index: dict[str, int],
    additions: Iterable[dict],
) -> int:
    duplicates = 0
    for row in additions:
        source_id = str(row.get("extra", {}).get("source_id") or "").strip()
        source_url = norm_url(str(row.get("source_url") or ""))
        if not source_id or not source_url:
            raise SourceSchemaError("greenhouse canonical row lacks stable identity")

        id_match = id_index.get(source_id)
        url_match = url_index.get(source_url)
        if id_match is None and url_match is None:
            index = len(rows)
            id_index[source_id] = index
            url_index[source_url] = index
            rows.append(row)
            continue
        if (
            id_match is None
            or url_match is None
            or id_match != url_match
            or not _equivalent_posting(rows[id_match], row)
        ):
            raise SourceSchemaError(
                "greenhouse returned a conflicting duplicate posting identity"
            )
        duplicates += 1
    return duplicates


def _equivalent_posting(left: dict, right: dict) -> bool:
    comparable: list[dict] = []
    for row in (left, right):
        item = dict(row)
        item.pop("source_url", None)
        extra = dict(item.get("extra") or {})
        extra.pop("greenhouse_company_name", None)
        item["extra"] = extra
        comparable.append(item)
    return comparable[0] == comparable[1]
