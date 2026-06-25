"""CSV ingestion pipeline: parse -> normalize -> dedupe -> analyze -> score.

`process_csv` is the single orchestrator the API calls. It accepts raw CSV
text and an optional fixed `today` (tests pass one for determinism) and
returns {jobs, cleaning_report, summary}.
"""

import csv
import io
from collections import Counter
from datetime import date

from . import config
from .ask import ask  # noqa: F401  (re-exported for the API layer)
from .classify import TECHNICAL_ROLES, classify_company, classify_role
from .dedupe import dedupe, job_id
from .normalize import build_row, infer_fields, map_headers
from .profile import load_profile
from .salary import parse_compensation
from .scoring import score_job
from .signals import count_tech_tools, detect_positive_signals, detect_red_flags, profile_match


def _read_csv(csv_text: str):
    """Parse CSV text into (headers, raw_rows). Sniffs the delimiter, falls
    back to comma, and tolerates BOMs/blank trailing lines."""
    text = csv_text.lstrip("\ufeff").strip("\n")
    if not text.strip():
        raise ValueError("The file is empty.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # default comma
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = reader.fieldnames or []
    rows = [r for r in reader if any((v or "").strip() for v in r.values())]
    return headers, rows


def process_csv(csv_text: str, today: date | None = None) -> dict:
    today = today or date.today()
    headers, raw_rows = _read_csv(csv_text)
    if not headers:
        raise ValueError("No header row found in the CSV.")

    mapping, column_report = map_headers(headers)
    if not any(mapping.get(h) in ("company", "title") for h in headers):
        raise ValueError(
            "Couldn't find a company or title column. "
            f"Headers seen: {', '.join(h.strip() for h in headers if h)}"
        )

    profile = load_profile()
    known = config.load_known_companies()

    # --- normalize ---------------------------------------------------------
    rows = []
    inferred_counter: Counter = Counter()
    warnings: list = []
    for i, raw in enumerate(raw_rows, start=1):
        row = build_row(raw, mapping)
        row["_row_number"] = i
        row["_inferred"] = infer_fields(row)
        for field in row["_inferred"]:
            inferred_counter[field] += 1
        if not row.get("company") and not row.get("title"):
            warnings.append(f"Row {i} has neither company nor title — kept, but it will score poorly.")
        rows.append(row)

    # --- dedupe -------------------------------------------------------------
    unique_rows, dup_report = dedupe(rows)

    # --- analyze + score ----------------------------------------------------
    jobs = []
    salary_stats = {"parsed": 0, "unparsed": 0, "period_assumed": 0}
    for row in unique_rows:
        comp = parse_compensation(row.get("compensation", ""))
        if comp["usd_hourly_min"] is not None or comp["kind"] in ("unpaid", "equity_only", "commission_only"):
            salary_stats["parsed"] += 1
        else:
            salary_stats["unparsed"] += 1
        if comp.get("period_assumed"):
            salary_stats["period_assumed"] += 1

        role_cls = classify_role(row)
        company_cls = classify_company(row, known, role_is_technical=role_cls["role"] in TECHNICAL_ROLES)
        red_flags = detect_red_flags(row, comp, role_cls, company_cls)
        positive = detect_positive_signals(row, comp, role_cls, company_cls, profile, known)
        pmatch = profile_match(row, role_cls, profile)
        tools = count_tech_tools(" ".join([row.get("description", ""), row.get("requirements", "")]))
        score = score_job(row, comp, role_cls, company_cls, red_flags, positive, pmatch, profile, tools, today=today)

        jobs.append({
            "id": job_id(row),
            "company": row.get("company", ""),
            "title": row.get("title", ""),
            "location": row.get("location", ""),
            "remote_status": row.get("remote_status", ""),
            "internship_type": row.get("internship_type", ""),
            "source_url": row.get("source_url", ""),
            "date_posted": row.get("date_posted", ""),
            "deadline": row.get("deadline", ""),
            "deadline_days_left": score.get("deadline_days_left"),
            "description": row.get("description", ""),
            "requirements": row.get("requirements", ""),
            "compensation": comp,
            "company_classification": company_cls,
            "role_classification": role_cls,
            "red_flags": red_flags,
            "positive_signals": positive,
            "profile_match": pmatch,
            "score": score,
            "inferred_fields": row.get("_inferred", []),
            "extra": row.get("extra", {}),
        })

    jobs.sort(key=lambda j: -j["score"]["total"])

    cleaning_report = {
        "rows_in": len(raw_rows),
        "rows_out": len(jobs),
        "duplicates_removed": len(dup_report),
        "duplicates": dup_report,
        "columns": column_report,
        "inferred_fields": dict(inferred_counter),
        "salary_parsing": salary_stats,
        "warnings": warnings,
    }

    return {"jobs": jobs, "cleaning_report": cleaning_report, "summary": summarize(jobs)}


def summarize(jobs: list) -> dict:
    buckets = Counter(j["score"]["bucket"] for j in jobs)
    actions = Counter(j["score"]["action"] for j in jobs)
    roles = Counter(j["role_classification"]["label"] for j in jobs)
    paid = sum(1 for j in jobs if j["compensation"]["kind"] in ("paid", "stipend_unspecified"))
    avg = round(sum(j["score"]["total"] for j in jobs) / len(jobs), 1) if jobs else 0
    return {
        "total": len(jobs),
        "buckets": {"high": buckets.get("high", 0), "maybe": buckets.get("maybe", 0), "low": buckets.get("low", 0)},
        "actions": dict(actions),
        "average_score": avg,
        "paid_count": paid,
        "paid_pct": round(100 * paid / len(jobs)) if jobs else 0,
        "role_distribution": dict(roles.most_common()),
        "top_jobs": [
            {"id": j["id"], "company": j["company"], "title": j["title"], "score": j["score"]["total"]}
            for j in jobs[:5]
        ],
    }
