"""Workday source adapter."""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from watcher.config import CompanyCfg
from watcher.sources.base import SourceError, SourceSchemaError, ensure_list, html_to_text, make_row, post_json, require_token

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkdayParseDiagnostics:
    raw_postings_seen: int = 0
    valid_rows_retained: int = 0
    malformed_postings_skipped: int = 0
    skip_reasons: tuple[tuple[str, int], ...] = ()


class WorkdaySource:
    name = "workday"
    page_size = 20

    def __init__(self) -> None:
        self.last_diagnostics = WorkdayParseDiagnostics()

    @staticmethod
    def endpoint(token: str, shard: str, site: str) -> str:
        return f"https://{token}.{shard}.myworkdayjobs.com/wday/cxs/{token}/{site}/jobs"

    @staticmethod
    def posting_url(token: str, shard: str, site: str, external_path: str) -> str:
        return f"https://{token}.{shard}.myworkdayjobs.com/{site}{external_path}"

    def fetch(self, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        shard = _required(company.workday_shard, "workday_shard", company)
        site = _required(company.workday_site, "workday_site", company)
        rows: list[dict] = []
        raw_postings_seen = 0
        skip_reasons: Counter[str] = Counter()
        offset = 0
        total = None
        while True:
            payload = post_json(
                self.endpoint(token, shard, site),
                {"appliedFacets": {}, "limit": self.page_size, "offset": offset, "searchText": ""},
                self.name,
            )
            postings, total_found = self._page(payload)
            raw_postings_seen += len(postings)
            page_rows, page_reasons = self._parse_postings(postings, company, token, shard, site)
            rows.extend(page_rows)
            skip_reasons.update(page_reasons)
            offset += len(postings)
            total = total_found if total_found is not None else total
            if not postings or (total is not None and offset >= total):
                break
        return self._finalize(rows, raw_postings_seen, skip_reasons, company)

    def parse(self, payload: Any, company: CompanyCfg) -> list[dict]:
        token = require_token(company, self.name)
        shard = _required(company.workday_shard, "workday_shard", company)
        site = _required(company.workday_site, "workday_site", company)
        postings, _ = self._page(payload)
        rows, skip_reasons = self._parse_postings(postings, company, token, shard, site)
        return self._finalize(rows, len(postings), skip_reasons, company)

    def _page(self, payload: Any) -> tuple[list, int | None]:
        if not isinstance(payload, dict):
            raise SourceSchemaError("workday expected a JSON object")
        postings = ensure_list(payload.get("jobPostings"), self.name, "jobPostings")
        total = payload.get("total")
        if total is not None and not isinstance(total, int):
            raise SourceSchemaError("workday expected total to be an integer")
        return postings, total

    def _parse_postings(
        self,
        postings: list,
        company: CompanyCfg,
        token: str,
        shard: str,
        site: str,
    ) -> tuple[list[dict], Counter[str]]:
        rows = []
        reasons: Counter[str] = Counter()
        for posting in postings:
            reason = _posting_skip_reason(posting)
            if reason:
                reasons[reason] += 1
                continue
            try:
                rows.append(self._parse_posting(posting, company, token, shard, site))
            except SourceSchemaError:
                reasons["posting_schema_error"] += 1
        return rows, reasons

    def _finalize(
        self,
        rows: list[dict],
        raw_postings_seen: int,
        skip_reasons: Counter[str],
        company: CompanyCfg,
    ) -> list[dict]:
        skipped = sum(skip_reasons.values())
        self.last_diagnostics = WorkdayParseDiagnostics(
            raw_postings_seen=raw_postings_seen,
            valid_rows_retained=len(rows),
            malformed_postings_skipped=skipped,
            skip_reasons=tuple(sorted(skip_reasons.items())),
        )
        if skipped:
            posting_word = "posting" if skipped == 1 else "postings"
            valid_word = "posting" if len(rows) == 1 else "postings"
            reasons = ", ".join(f"{reason}={count}" for reason, count in sorted(skip_reasons.items()))
            company_name = _safe_company_name(company.name)
            LOGGER.warning(
                "Skipped %d malformed Workday %s for %s; %d valid %s retained. Reasons: %s",
                skipped,
                posting_word,
                company_name,
                len(rows),
                valid_word,
                reasons,
            )
        if raw_postings_seen > 0 and not rows:
            raise SourceSchemaError(
                f"workday received {raw_postings_seen} posting record(s) for "
                f"{_safe_company_name(company.name)} but none were valid"
            )
        return rows

    def _parse_posting(self, posting: Any, company: CompanyCfg, token: str, shard: str, site: str) -> dict:
        if not isinstance(posting, dict):
            raise SourceSchemaError("workday expected each posting to be an object")

        title = str(posting.get("title") or "").strip()
        external_path = str(posting.get("externalPath") or "").strip()
        if not title or not external_path:
            raise SourceSchemaError("workday posting missing required title or externalPath")
        if not external_path.startswith("/"):
            external_path = f"/{external_path}"

        return make_row(
            source="direct",
            source_adapter=self.name,
            company=company.name,
            title=title,
            location=str(posting.get("locationsText") or "").strip(),
            description=html_to_text(posting.get("jobDescription")),
            source_url=self.posting_url(token, shard, site, external_path),
            date_posted=str(posting.get("postedOn") or "").strip(),
            remote_status=_remote_status(posting),
            extra={
                "source_id": _source_id(posting),
                "external_path": external_path,
                "time_type": str(posting.get("timeType") or "").strip(),
                "workday_tenant": token,
                "workday_shard": shard,
                "workday_site": site,
            },
        )


def _posting_skip_reason(posting: Any) -> str | None:
    if not isinstance(posting, dict):
        return "posting_not_object"
    title = str(posting.get("title") or "").strip()
    external_path = str(posting.get("externalPath") or "").strip()
    if not title and not external_path:
        return "missing_title_and_external_path"
    if not title:
        return "missing_title"
    if not external_path:
        return "missing_external_path"
    return None


def _safe_company_name(value: Any) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "unknown")).strip()[:120] or "unknown"


def _required(value: str, field: str, company: CompanyCfg) -> str:
    value = str(value or "").strip()
    if not value:
        raise SourceError(f"workday requires {field} for {company.name}")
    return value


def _source_id(posting: dict) -> str:
    bullet_fields = posting.get("bulletFields")
    if isinstance(bullet_fields, list) and bullet_fields:
        return str(bullet_fields[0] or "").strip()
    return ""


def _remote_status(posting: dict) -> str:
    text = " ".join(
        str(value or "").lower()
        for value in (posting.get("title"), posting.get("locationsText"))
    )
    if "remote" in text:
        return "Remote"
    if "hybrid" in text:
        return "Hybrid"
    return ""
