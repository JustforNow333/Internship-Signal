"""Alumni roster loading and company matching for watcher results."""

from __future__ import annotations

import csv
import logging
from difflib import SequenceMatcher
from pathlib import Path
from typing import Mapping, Sequence

from backend.app.dedupe import norm_company

LOGGER = logging.getLogger(__name__)

ALUMNI_CSV_PATH = Path(__file__).resolve().parent / "alumni.csv"
REQUIRED_COLUMNS = ("First Name", "Last Name", "Occupation", "Employer", "LinkedIn URL")
FUZZY_THRESHOLD = 0.88

# Known roster/watchlist spelling variants that exact matching misses and
# fuzzy matching should not be relied on to catch.
ALIAS_MAP = {
    norm_company("Capital One Financial"): norm_company("Capital One"),
    norm_company("Capitol One"): norm_company("Capital One"),
}

AlumniRecord = dict[str, str]
AlumniIndex = dict[str, list[AlumniRecord]]


class AlumniError(ValueError):
    """Raised when the alumni roster file is missing or malformed."""


def load_alumni(path: str | Path = ALUMNI_CSV_PATH) -> AlumniIndex:
    """Read the alumni CSV once and index records by normalized employer."""

    path = Path(path)
    if not path.exists():
        raise AlumniError(f"Alumni CSV not found: {path}")

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = tuple(reader.fieldnames or ())
        missing = [column for column in REQUIRED_COLUMNS if column not in headers]
        if missing:
            raise AlumniError(
                f"Alumni CSV missing required column(s): {', '.join(missing)}. "
                f"Found columns: {', '.join(headers) if headers else '(none)'}"
            )

        index: AlumniIndex = {}
        for row in reader:
            employer = str(row.get("Employer") or "").strip()
            if not employer:
                continue
            key = norm_company(employer)
            if not key:
                continue
            index.setdefault(key, []).append(_record(row))
    return index


def match_alumni(company: str, index: Mapping[str, Sequence[AlumniRecord]]) -> list[AlumniRecord]:
    """Return alumni for a posting company using exact, alias, then fuzzy match."""

    company_name = str(company or "").strip()
    key = norm_company(company_name)
    if not key:
        return []

    exact = index.get(key)
    if exact is not None:
        return [*list(exact), *_alias_records_for(key, index)]

    alias_key = ALIAS_MAP.get(key)
    if alias_key:
        alias_records = index.get(alias_key)
        if alias_records is not None:
            LOGGER.info("ALIAS %s -> %s", company_name, alias_key)
            return list(alias_records)

    alias_records = _alias_records_for(key, index)
    if alias_records:
        return alias_records

    fuzzy_matches = []
    for employer_key in sorted(index):
        ratio = SequenceMatcher(None, key, employer_key).ratio()
        if ratio >= FUZZY_THRESHOLD:
            records = list(index[employer_key])
            employer = records[0]["employer"] if records else employer_key
            LOGGER.info("FUZZY %s ~ %s (ratio=%.2f)", company_name, employer, ratio)
            fuzzy_matches.append((ratio, employer_key, records))

    if not fuzzy_matches:
        return []
    fuzzy_matches.sort(key=lambda item: (-item[0], item[1]))
    return list(fuzzy_matches[0][2])


def _alias_records_for(canonical_key: str, index: Mapping[str, Sequence[AlumniRecord]]) -> list[AlumniRecord]:
    records: list[AlumniRecord] = []
    for alias_key, target_key in sorted(ALIAS_MAP.items()):
        if target_key != canonical_key:
            continue
        alias_records = index.get(alias_key)
        if alias_records is None:
            continue
        LOGGER.info("ALIAS %s -> %s", _alias_label(alias_key, alias_records), canonical_key)
        records.extend(alias_records)
    return records


def _alias_label(alias_key: str, records: Sequence[AlumniRecord]) -> str:
    for record in records:
        employer = str(record.get("employer") or "").strip()
        if employer:
            return employer
    return alias_key


def attach_alumni(jobs: Sequence[dict], index: Mapping[str, Sequence[AlumniRecord]]) -> list[dict]:
    """Attach an `alumni` list to each job without filtering or changing fields."""

    annotated = []
    for job in jobs:
        next_job = dict(job)
        next_job["alumni"] = match_alumni(str(job.get("company") or ""), index)
        annotated.append(next_job)
    return annotated


def load_default_alumni_index() -> AlumniIndex:
    """Load the default roster when present; otherwise keep the join additive."""

    if not ALUMNI_CSV_PATH.exists():
        LOGGER.warning("Alumni CSV not found; continuing with empty alumni index: %s", ALUMNI_CSV_PATH)
        return {}
    return load_alumni(ALUMNI_CSV_PATH)


def _record(row: Mapping[str, str]) -> AlumniRecord:
    first = str(row.get("First Name") or "").strip()
    last = str(row.get("Last Name") or "").strip()
    return {
        "name": " ".join(part for part in (first, last) if part),
        "occupation": str(row.get("Occupation") or "").strip(),
        "linkedin_url": str(row.get("LinkedIn URL") or "").strip(),
        "employer": str(row.get("Employer") or "").strip(),
    }
