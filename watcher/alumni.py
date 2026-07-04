"""Alumni roster loading and company matching for watcher results."""

from __future__ import annotations

import base64
import binascii
import csv
import json
import logging
import os
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Mapping, Sequence

from backend.app.dedupe import norm_company

LOGGER = logging.getLogger(__name__)

ALUMNI_CSV_PATH = Path(__file__).resolve().parent / "alumni.csv"
ALUMNI_CSV_ENV = "WATCHER_ALUMNI_CSV"
COMPANY_ALUMNI_JSON_B64_ENV = "WATCHER_COMPANY_ALUMNI_JSON_B64"
COMPANY_ALUMNI_JSON_ENV = "WATCHER_COMPANY_ALUMNI_JSON"
COMPANY_ALUMNI_JSON_PATH_ENV = "WATCHER_COMPANY_ALUMNI_JSON_PATH"
REQUIRE_ALUMNI_ENV = "WATCHER_REQUIRE_ALUMNI"
SEND_EMAIL_ENV = "WATCHER_SEND_EMAIL"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "y", "on"}
REQUIRED_COLUMNS = ("First Name", "Last Name", "Occupation", "Employer", "LinkedIn URL")
FUZZY_THRESHOLD = 0.88

# Known roster/watchlist spelling variants that exact matching misses and
# fuzzy matching should not be relied on to catch.
ALIAS_MAP = {
    norm_company("Bosch Group"): norm_company("Bosch"),
    norm_company("Robert Bosch"): norm_company("Bosch"),
    norm_company("BoschGroup"): norm_company("Bosch"),
    norm_company("Tesla Motors"): norm_company("Tesla"),
    norm_company("Google LLC"): norm_company("Google"),
    norm_company("Capital One Financial"): norm_company("Capital One"),
    norm_company("Capitol One"): norm_company("Capital One"),
    norm_company("J.P. Morgan"): norm_company("JPMorgan Chase"),
    norm_company("JP Morgan"): norm_company("JPMorgan Chase"),
    norm_company("JPMorgan"): norm_company("JPMorgan Chase"),
    norm_company("JPMC"): norm_company("JPMorgan Chase"),
    norm_company("HP Inc"): norm_company("HP"),
    norm_company("Hewlett Packard"): norm_company("HP"),
    norm_company("Hewlett-Packard"): norm_company("HP"),
    norm_company("Intuitive"): norm_company("Intuitive Surgical"),
    norm_company("ASML US"): norm_company("ASML"),
    norm_company("WhatNot"): norm_company("Whatnot"),
    norm_company("KPMG US"): norm_company("KPMG"),
    norm_company("Ernst & Young"): norm_company("EY"),
    norm_company("Ernst and Young"): norm_company("EY"),
}

AlumniRecord = dict[str, str]
AlumniIndex = dict[str, list[AlumniRecord]]


class AlumniError(ValueError):
    """Raised when the alumni roster file is missing or malformed."""


@dataclass(frozen=True)
class AlumniLoadStatus:
    """Operational status for the alumni roster used by a watcher run."""

    status: str
    path: str
    records_loaded: int = 0
    employers_indexed: int = 0
    message: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "path": self.path,
            "records_loaded": self.records_loaded,
            "employers_indexed": self.employers_indexed,
            "message": self.message,
        }


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


def load_company_alumni_json(path: str | Path) -> AlumniIndex:
    """Read a compact private company->alumni JSON map."""

    path = Path(path)
    if not path.exists():
        raise AlumniError(f"Company alumni JSON not found: {path}")
    return load_company_alumni_json_text(path.read_text(encoding="utf-8"), source=str(path))


def load_company_alumni_json_text(text: str, *, source: str = "company alumni JSON") -> AlumniIndex:
    """Convert a compact JSON map into the same AlumniIndex shape as the CSV."""

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AlumniError(f"Company alumni JSON is invalid JSON ({source}): {exc}") from exc

    if not isinstance(payload, dict):
        raise AlumniError(f"Company alumni JSON must be an object mapping company names to alumni lists ({source}).")

    index: AlumniIndex = {}
    for company_name, records in payload.items():
        if not isinstance(records, list):
            raise AlumniError(f"Company alumni JSON value for {company_name!r} must be a list.")
        fallback_employer = str(company_name or "").strip()
        key = norm_company(fallback_employer)
        for record in records:
            if not isinstance(record, dict):
                raise AlumniError(f"Company alumni JSON record for {company_name!r} must be an object.")
            alumni_record = _json_record(record, fallback_employer=fallback_employer)
            record_key = norm_company(alumni_record["employer"]) or key
            if not record_key:
                continue
            index.setdefault(record_key, []).append(alumni_record)
    return index


def alumni_index_stats(index: Mapping[str, Sequence[AlumniRecord]]) -> tuple[int, int]:
    """Return (record count, employer count) for a loaded alumni index."""

    return sum(len(records) for records in index.values()), len(index)


def load_default_alumni(
    path: str | Path | None = None,
    *,
    require: bool | None = None,
) -> tuple[AlumniIndex, AlumniLoadStatus]:
    """Load the configured alumni roster and report whether matching is active."""

    require = _env_truthy(REQUIRE_ALUMNI_ENV) if require is None else require
    try:
        json_source = _configured_company_json_source()
    except Exception as exc:
        message = f"Company alumni JSON error, alumni matching disabled: {exc}"
        status = AlumniLoadStatus("error", f"env:{COMPANY_ALUMNI_JSON_PATH_ENV}", message=message)
        if require:
            raise AlumniError(message) from exc
        LOGGER.error(message)
        return {}, status
    if json_source is not None:
        return _load_default_company_json(json_source, require=require)

    roster_path = Path(path or os.getenv(ALUMNI_CSV_ENV) or ALUMNI_CSV_PATH)
    if not roster_path.exists():
        message = "Alumni CSV missing, alumni matching disabled."
        status = AlumniLoadStatus("missing", str(roster_path), message=message)
        if require:
            raise AlumniError(f"{message} Expected file: {roster_path}")
        _log_missing_roster(status)
        return {}, status

    try:
        index = load_alumni(roster_path)
    except Exception as exc:
        message = f"Alumni CSV error, alumni matching disabled: {exc}"
        status = AlumniLoadStatus("error", str(roster_path), message=message)
        if require:
            raise AlumniError(message) from exc
        LOGGER.error(message)
        return {}, status

    records_loaded, employers_indexed = alumni_index_stats(index)
    if records_loaded == 0:
        status = AlumniLoadStatus(
            "empty",
            str(roster_path),
            records_loaded=0,
            employers_indexed=0,
            message="Alumni CSV loaded but contained no usable employer records.",
        )
        LOGGER.warning(status.message)
        return index, status

    status = AlumniLoadStatus(
        "loaded-csv",
        str(roster_path),
        records_loaded=records_loaded,
        employers_indexed=employers_indexed,
        message=f"Alumni CSV loaded: {records_loaded} records across {employers_indexed} employers.",
    )
    LOGGER.info(status.message)
    return index, status


def _configured_company_json_source() -> tuple[str, str] | None:
    raw_b64 = os.getenv(COMPANY_ALUMNI_JSON_B64_ENV)
    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise AlumniError(f"{COMPANY_ALUMNI_JSON_B64_ENV} is not valid base64-encoded UTF-8 JSON.") from exc
        return f"env:{COMPANY_ALUMNI_JSON_B64_ENV}", decoded

    raw_json = os.getenv(COMPANY_ALUMNI_JSON_ENV)
    if raw_json:
        return f"env:{COMPANY_ALUMNI_JSON_ENV}", raw_json

    json_path = os.getenv(COMPANY_ALUMNI_JSON_PATH_ENV)
    if json_path:
        path = Path(json_path)
        if not path.exists():
            raise AlumniError(f"Company alumni JSON not found: {path}")
        return str(path), path.read_text(encoding="utf-8")

    return None


def _load_default_company_json(json_source: tuple[str, str], *, require: bool) -> tuple[AlumniIndex, AlumniLoadStatus]:
    source, text = json_source
    try:
        index = load_company_alumni_json_text(text, source=source)
    except Exception as exc:
        message = f"Company alumni JSON error, alumni matching disabled: {exc}"
        status = AlumniLoadStatus("error", source, message=message)
        if require:
            raise AlumniError(message) from exc
        LOGGER.error(message)
        return {}, status

    records_loaded, employers_indexed = alumni_index_stats(index)
    if records_loaded == 0:
        status = AlumniLoadStatus(
            "empty",
            source,
            records_loaded=0,
            employers_indexed=0,
            message="Company alumni JSON loaded but contained no usable employer records.",
        )
        LOGGER.warning(status.message)
        return index, status

    status = AlumniLoadStatus(
        "loaded-json-map",
        source,
        records_loaded=records_loaded,
        employers_indexed=employers_indexed,
        message=f"Company alumni JSON loaded: {records_loaded} records across {employers_indexed} employers.",
    )
    LOGGER.info(status.message)
    return index, status


def match_alumni(company: str, index: Mapping[str, Sequence[AlumniRecord]]) -> list[AlumniRecord]:
    """Return alumni for a posting company using exact, alias, then fuzzy match."""

    return _match_alumni(company, index, allow_fuzzy=True)


def _match_alumni(
    company: str,
    index: Mapping[str, Sequence[AlumniRecord]],
    *,
    allow_fuzzy: bool,
) -> list[AlumniRecord]:
    """Return alumni for a single company string."""

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
        alias_records = _alias_records_for(alias_key, index)
        if alias_records:
            LOGGER.info("ALIAS %s -> %s", company_name, alias_key)
            return alias_records

    alias_records = _alias_records_for(key, index)
    if alias_records:
        return alias_records

    if not allow_fuzzy:
        return []

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


def attach_alumni(
    jobs: Sequence[dict],
    index: Mapping[str, Sequence[AlumniRecord]],
    companies: Sequence[object] = (),
) -> list[dict]:
    """Attach an `alumni` list to each job without filtering or changing fields."""

    watchlist = _watchlist_lookup(companies)
    annotated = []
    for job in jobs:
        next_job = dict(job)
        next_job["alumni"] = match_alumni_for_watchlist_company(
            str(job.get("company") or ""),
            index,
            watchlist,
        )
        annotated.append(next_job)
    return annotated


def match_alumni_for_watchlist_company(
    company: str,
    index: Mapping[str, Sequence[AlumniRecord]],
    watchlist: Mapping[str, object],
) -> list[AlumniRecord]:
    """Match alumni using the posting company plus watchlist aliases."""

    matches: list[AlumniRecord] = []
    _extend_unique(matches, _match_alumni(company, index, allow_fuzzy=False))

    cfg = watchlist.get(norm_company(company))
    if cfg is not None:
        for alias in _watchlist_alumni_names(cfg):
            alias_matches = _match_alumni(alias, index, allow_fuzzy=False)
            if alias_matches:
                LOGGER.info("WATCHLIST_ALIAS %s -> %s", company, alias)
                _extend_unique(matches, alias_matches)

    if matches:
        return matches
    _extend_unique(matches, _match_alumni(company, index, allow_fuzzy=True))
    return matches


def _watchlist_lookup(companies: Sequence[object]) -> dict[str, object]:
    lookup: dict[str, object] = {}
    for company in companies:
        for name in _watchlist_alumni_names(company):
            key = norm_company(name)
            if key:
                lookup.setdefault(key, company)
    return lookup


def _watchlist_alumni_names(company: object) -> tuple[str, ...]:
    values = (
        str(getattr(company, "name", "") or ""),
        *(str(value or "") for value in getattr(company, "aliases", ()) or ()),
        *(str(value or "") for value in getattr(company, "alumni_match", ()) or ()),
    )
    return tuple(value.strip() for value in values if value.strip())


def _extend_unique(target: list[AlumniRecord], records: Sequence[AlumniRecord]) -> None:
    seen = {_record_key(record) for record in target}
    for record in records:
        key = _record_key(record)
        if key in seen:
            continue
        target.append(record)
        seen.add(key)


def _record_key(record: Mapping[str, str]) -> tuple[str, str, str, str]:
    return (
        str(record.get("name") or ""),
        str(record.get("occupation") or ""),
        str(record.get("linkedin_url") or ""),
        str(record.get("employer") or ""),
    )


def load_default_alumni_index() -> AlumniIndex:
    """Load the default roster when present; otherwise keep the join additive."""

    index, _status = load_default_alumni()
    return index


def status_for_injected_index(index: Mapping[str, Sequence[AlumniRecord]]) -> AlumniLoadStatus:
    """Build a status object for tests or callers that inject an alumni index."""

    records_loaded, employers_indexed = alumni_index_stats(index)
    status = "loaded" if records_loaded else "empty"
    message = (
        f"Alumni index injected: {records_loaded} records across {employers_indexed} employers."
        if records_loaded
        else "Alumni index injected but empty."
    )
    return AlumniLoadStatus(
        status,
        "(injected)",
        records_loaded=records_loaded,
        employers_indexed=employers_indexed,
        message=message,
    )


def _env_truthy(name: str) -> bool:
    return str(os.getenv(name) or "").strip().lower() in TRUTHY_ENV_VALUES


def _log_missing_roster(status: AlumniLoadStatus) -> None:
    message = f"{status.message} Expected file: {status.path}"
    if _env_truthy(SEND_EMAIL_ENV):
        LOGGER.error(message)
    else:
        LOGGER.warning(message)


def _record(row: Mapping[str, str]) -> AlumniRecord:
    first = str(row.get("First Name") or "").strip()
    last = str(row.get("Last Name") or "").strip()
    return {
        "name": " ".join(part for part in (first, last) if part),
        "occupation": str(row.get("Occupation") or "").strip(),
        "linkedin_url": str(row.get("LinkedIn URL") or "").strip(),
        "employer": str(row.get("Employer") or "").strip(),
    }


def _json_record(row: Mapping[str, object], *, fallback_employer: str) -> AlumniRecord:
    return {
        "name": str(row.get("name") or "").strip(),
        "occupation": str(row.get("occupation") or "").strip(),
        "linkedin_url": str(row.get("linkedin_url") or "").strip(),
        "employer": str(row.get("employer") or fallback_employer or "").strip(),
    }
