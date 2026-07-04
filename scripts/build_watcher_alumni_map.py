#!/usr/bin/env python3
"""Build a compact private alumni JSON map for the watcher.

The output intentionally contains only alumni attached to companies in
watcher/watchlist.yml, so it can be stored as a smaller GitHub Actions secret.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.app.dedupe import norm_company  # noqa: E402
from watcher.alumni import ALIAS_MAP, FUZZY_THRESHOLD, REQUIRED_COLUMNS  # noqa: E402
from watcher.config import CompanyCfg, load_watchlist  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build compact watcher alumni JSON from a private CSV.")
    parser.add_argument("--csv", required=True, help="Path to the private full alumni CSV.")
    parser.add_argument("--watchlist", default="watcher/watchlist.yml", help="Path to watcher/watchlist.yml.")
    parser.add_argument("--out", required=True, help="Output path for the compact private JSON map.")
    args = parser.parse_args(argv)

    csv_path = Path(args.csv)
    watchlist_path = Path(args.watchlist)
    out_path = Path(args.out)

    config = load_watchlist(watchlist_path)
    lookup, display_names = _watchlist_lookup(config.companies)
    output: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen: set[tuple[str, str, str, str, str]] = set()

    for row in _read_csv(csv_path):
        employer = str(row.get("Employer") or "").strip()
        employer_key = norm_company(employer)
        if not employer_key:
            continue

        company_key = _match_company_key(employer_key, lookup)
        if company_key is None:
            continue

        record = _record(row)
        dedupe_key = (
            company_key,
            record["name"],
            record["occupation"],
            record["linkedin_url"],
            record["employer"],
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        output[company_key].append(record)

    ordered = {key: output[key] for key in sorted(output)}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    record_count = sum(len(records) for records in ordered.values())
    matched_companies = [display_names.get(key, key) for key in sorted(ordered)]
    print(f"Wrote {record_count} alumni record(s).")
    print(f"Companies with alumni: {len(ordered)}.")
    print(f"Watchlist companies checked: {len(config.companies)}.")
    if matched_companies:
        preview = ", ".join(matched_companies[:20])
        suffix = f", plus {len(matched_companies) - 20} more" if len(matched_companies) > 20 else ""
        print(f"Matched companies: {preview}{suffix}")
    else:
        print("Matched companies: none")

    return 0


def _read_csv(path: Path) -> list[Mapping[str, str]]:
    if not path.exists():
        raise SystemExit(f"Alumni CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = tuple(reader.fieldnames or ())
        missing = [column for column in REQUIRED_COLUMNS if column not in headers]
        if missing:
            raise SystemExit(
                "Alumni CSV missing required column(s): "
                + ", ".join(missing)
                + f". Found columns: {', '.join(headers) if headers else '(none)'}"
            )
        return list(reader)


def _watchlist_lookup(companies: tuple[CompanyCfg, ...]) -> tuple[dict[str, str], dict[str, str]]:
    lookup: dict[str, str] = {}
    display_names: dict[str, str] = {}

    for company in companies:
        company_key = norm_company(company.name)
        if not company_key:
            continue
        display_names[company_key] = company.name
        for key in _candidate_keys(company):
            lookup.setdefault(key, company_key)

    # Mirror the runtime alias map in both directions where it touches a
    # watched company. This catches cases like Bosch Group -> Bosch.
    for alias_key, target_key in ALIAS_MAP.items():
        if target_key in lookup:
            lookup.setdefault(alias_key, lookup[target_key])
        if alias_key in lookup:
            lookup.setdefault(target_key, lookup[alias_key])

    return lookup, display_names


def _candidate_keys(company: CompanyCfg) -> set[str]:
    names = (
        company.name,
        *(company.aliases or ()),
        *(company.alumni_match or ()),
    )
    return {key for name in names if (key := norm_company(str(name)))}


def _match_company_key(employer_key: str, lookup: Mapping[str, str]) -> str | None:
    if employer_key in lookup:
        return lookup[employer_key]

    alias_target = ALIAS_MAP.get(employer_key)
    if alias_target and alias_target in lookup:
        return lookup[alias_target]

    best_ratio = 0.0
    best_company_key = None
    for candidate_key, company_key in lookup.items():
        ratio = SequenceMatcher(None, employer_key, candidate_key).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_company_key = company_key
    return best_company_key if best_ratio >= FUZZY_THRESHOLD else None


def _record(row: Mapping[str, str]) -> dict[str, str]:
    first = str(row.get("First Name") or "").strip()
    last = str(row.get("Last Name") or "").strip()
    return {
        "name": " ".join(part for part in (first, last) if part),
        "occupation": str(row.get("Occupation") or "").strip(),
        "linkedin_url": str(row.get("LinkedIn URL") or "").strip(),
        "employer": str(row.get("Employer") or "").strip(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
