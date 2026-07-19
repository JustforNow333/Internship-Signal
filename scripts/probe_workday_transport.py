"""Safely compare first-page Workday transport behavior across environments."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from watcher.config import DEFAULT_WATCHLIST_PATH, CompanyCfg, load_watchlist
from watcher.sources.base import SourceFetchError, post_json_response
from watcher.sources.workday import WorkdaySource

DEFAULT_COMPANIES = (
    "Cornerstone Research",
    "Merck",
    "Capital One",
    "Salesforce",
    "Eli Lilly and Company",
)
MAX_COMPANIES = 5


def probe_company(source: WorkdaySource, company: CompanyCfg) -> dict[str, object]:
    payload = None
    metadata: dict[str, object] = {}
    error_code = "none"
    try:
        payload, metadata = source.probe_transport(company)
    except SourceFetchError as exc:
        metadata = dict(exc.response_metadata)
        error_code = exc.error_code

    diagnostics = source.last_diagnostics
    digest = str(metadata.get("body_sha256") or "")
    return {
        "company": company.name,
        "shard": company.workday_shard,
        "attempt_count": diagnostics.request_attempts,
        "final_status": metadata.get("status"),
        "content_type": str(metadata.get("content_type") or ""),
        "content_encoding": str(metadata.get("content_encoding") or ""),
        "body_kind": str(metadata.get("body_kind") or "unknown"),
        "body_length": int(metadata.get("body_bytes") or 0),
        "body_hash_prefix": digest[:12],
        "json_decoded": bool(metadata.get("json_decoded")),
        "jobs_field_present": isinstance(payload, dict) and "jobPostings" in payload,
        "error_code": error_code,
        "retries_recovered": diagnostics.retry_attempts > 0 and payload is not None,
    }


def selected_companies(watchlist: str | Path, names: tuple[str, ...]) -> list[CompanyCfg]:
    config = load_watchlist(watchlist)
    by_name = {company.name.casefold(): company for company in config.companies}
    selected = []
    for name in names:
        company = by_name.get(name.casefold())
        if company is None:
            raise ValueError(f"company not found in watchlist: {name}")
        if company.ats != "workday":
            raise ValueError(f"company is not configured for Workday: {company.name}")
        selected.append(company)
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe up to five Workday tenants without seen state, alumni, or email."
    )
    parser.add_argument("--watchlist", default=str(DEFAULT_WATCHLIST_PATH))
    parser.add_argument("--company", action="append", dest="companies")
    parser.add_argument("--json-out", help="Optional UTF-8 JSON output path.")
    args = parser.parse_args(argv)

    os.environ["WATCHER_SEND_EMAIL"] = "0"
    names = tuple(args.companies or DEFAULT_COMPANIES)
    if not 1 <= len(names) <= MAX_COMPANIES:
        parser.error(f"select between 1 and {MAX_COMPANIES} companies")

    try:
        companies = selected_companies(args.watchlist, names)
    except ValueError as exc:
        parser.error(str(exc))

    source = WorkdaySource(request_json=post_json_response)
    results = [probe_company(source, company) for company in companies]
    rendered = json.dumps(results, indent=2, sort_keys=True)
    print(rendered)
    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
