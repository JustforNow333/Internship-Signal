"""CSV ingestion pipeline: parse -> normalize -> dedupe -> analyze -> score.

`process_csv` is the CSV orchestrator the API calls. `analyze_rows` is the
shared analysis path for already-built canonical row dicts.
"""

import csv
import io
from collections import Counter
from datetime import date
from typing import Mapping

from . import config
from .classify import TECHNICAL_ROLES, classify_company, classify_role
from .dedupe import analyzed_job_ids, dedupe, job_id
from .eligibility import analyze_student_eligibility
from .normalize import build_row, infer_fields, map_headers
from .profile import load_profile
from .salary import parse_compensation
from .scoring import (
    STATIC_SCORE_CATEGORIES,
    build_static_scoring_artifact,
    score_job,
)
from .signals import (
    ProfileSkillMatcher,
    PostingAnalysisContext,
    build_profile_skill_matcher,
    count_tech_tools,
    detect_positive_signals,
    detect_red_flags,
    profile_match,
)


STATIC_ANALYSIS_ARTIFACT_SCHEMA_VERSION = 2


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
    seen_headers = set()
    for header in headers:
        if header in seen_headers:
            raise ValueError(f"Duplicate CSV header: {header or '(blank)'}")
        seen_headers.add(header)
    rows = [r for r in reader if any(_cell_has_text(v) for v in r.values())]
    return headers, rows


def _cell_has_text(value) -> bool:
    if isinstance(value, list):
        return any(str(item or "").strip() for item in value)
    return bool(str(value or "").strip())


def deduplicate_rows(
    rows: list[dict],
    *,
    include_audit_diagnostics: bool = False,
) -> tuple[list[dict], list]:
    """Deduplicate canonical rows using the existing merge/precedence policy."""

    return dedupe(
        rows,
        include_identity_diagnostics=include_audit_diagnostics,
    )


def build_static_analysis_artifact(
    row: dict,
    *,
    profile: Mapping[str, object],
    known: Mapping[str, object],
    profile_skill_matcher: ProfileSkillMatcher | None = None,
    use_analysis_context: bool = True,
) -> dict:
    """Build one JSON-safe artifact containing only date-independent work."""

    analysis_context = None
    if use_analysis_context:
        matcher = profile_skill_matcher or build_profile_skill_matcher(profile)
        analysis_context = PostingAnalysisContext.from_row(row, matcher)

    comp = parse_compensation(row.get("compensation", ""))
    role_cls = classify_role(
        row,
        analysis_context=analysis_context,
    )
    company_cls = classify_company(
        row,
        known,
        role_is_technical=role_cls["role"] in TECHNICAL_ROLES,
        analysis_context=analysis_context,
    )
    red_flags = detect_red_flags(
        row,
        comp,
        role_cls,
        company_cls,
        analysis_context=analysis_context,
    )
    positive = detect_positive_signals(
        row,
        comp,
        role_cls,
        company_cls,
        profile,
        known,
        analysis_context=analysis_context,
    )
    pmatch = profile_match(
        row,
        role_cls,
        profile,
        analysis_context=analysis_context,
    )
    tools = (
        list(analysis_context.full_technology_matches)
        if analysis_context is not None
        else count_tech_tools(
            " ".join(
                [
                    row.get("description", ""),
                    row.get("requirements", ""),
                ]
            )
        )
    )
    eligibility_analysis = analyze_student_eligibility(row)
    static_scoring = build_static_scoring_artifact(
        row,
        comp,
        role_cls,
        company_cls,
        red_flags,
        positive,
        pmatch,
        profile,
        tools,
        analysis_context=analysis_context,
        eligibility_analysis=eligibility_analysis,
    )
    return {
        "schema_version": STATIC_ANALYSIS_ARTIFACT_SCHEMA_VERSION,
        "compensation": comp,
        "role_classification": role_cls,
        "company_classification": company_cls,
        "red_flags": red_flags,
        "positive_signals": positive,
        "profile_match": pmatch,
        "technology_matches": tools,
        "eligibility_context": eligibility_analysis.context_dict(),
        "static_scoring": static_scoring,
    }


# Backward-compatible public name used by existing backend and watcher callers.
analyze_static_row = build_static_analysis_artifact


def static_analysis_artifact_is_valid(value: object) -> bool:
    """Return whether a decoded JSON value matches the static artifact schema."""

    if not isinstance(value, dict):
        return False
    if (
        type(value.get("schema_version")) is not int
        or value["schema_version"]
        != STATIC_ANALYSIS_ARTIFACT_SCHEMA_VERSION
    ):
        return False
    mapping_fields = (
        "compensation",
        "role_classification",
        "company_classification",
        "profile_match",
        "eligibility_context",
        "static_scoring",
    )
    if any(not isinstance(value.get(field), dict) for field in mapping_fields):
        return False
    if any(
        not isinstance(value.get(field), list)
        for field in ("red_flags", "positive_signals", "technology_matches")
    ):
        return False
    if not all(
        isinstance(tool, str)
        for tool in value["technology_matches"]
    ):
        return False
    if not _eligibility_context_is_valid(value["eligibility_context"]):
        return False
    if not _static_scoring_artifact_is_valid(value["static_scoring"]):
        return False
    return True


def _eligibility_context_is_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if not {
        "normalized_evidence",
        "qualification_segments",
        "restriction",
    }.issubset(value):
        return False
    normalized = value.get("normalized_evidence")
    if not isinstance(normalized, list) or not all(
        isinstance(item, str)
        for item in normalized
    ):
        return False
    qualifications = value.get("qualification_segments")
    if not isinstance(qualifications, Mapping):
        return False
    for field in ("requirements", "description"):
        segments = qualifications.get(field)
        if not isinstance(segments, list):
            return False
        if not all(
            isinstance(segment, Mapping)
            and isinstance(segment.get("text"), str)
            and type(segment.get("preferred")) is bool
            for segment in segments
        ):
            return False
    restriction = value.get("restriction")
    if restriction is not None and not (
        isinstance(restriction, Mapping)
        and all(
            isinstance(restriction.get(field), str)
            for field in (
                "exclusion_reason",
                "degree_level",
                "evidence_source",
            )
        )
    ):
        return False
    return True


def _static_scoring_artifact_is_valid(value: object) -> bool:
    if not isinstance(value, Mapping):
        return False
    if not {
        "student_eligibility",
        "categories",
        "role_ineligible_reason",
        "has_critical_red_flag",
        "major_red_flag_count",
    }.issubset(value):
        return False
    student = value.get("student_eligibility")
    if not isinstance(student, Mapping):
        return False
    required_student_types = {
        "eligible": bool,
        "degree_level": str,
        "explanation": str,
        "mandatory_language_detected": bool,
        "negation_detected": bool,
        "mixed_eligibility_detected": bool,
    }
    if any(
        type(student.get(field)) is not expected
        for field, expected in required_student_types.items()
    ):
        return False
    if not {
        "exclusion_reason",
        "evidence_source",
        "evidence",
    }.issubset(student):
        return False
    for field in ("exclusion_reason", "evidence_source", "evidence"):
        if student.get(field) is not None and not isinstance(
            student.get(field),
            str,
        ):
            return False
    categories = value.get("categories")
    if (
        not isinstance(categories, Mapping)
        or set(categories) != set(STATIC_SCORE_CATEGORIES)
    ):
        return False
    if not all(
        isinstance(categories[name], Mapping)
        and type(categories[name].get("score")) is int
        and isinstance(categories[name].get("explanation"), str)
        for name in STATIC_SCORE_CATEGORIES
    ):
        return False
    role_reason = value.get("role_ineligible_reason")
    if role_reason is not None and not isinstance(role_reason, str):
        return False
    if type(value.get("has_critical_red_flag")) is not bool:
        return False
    major_count = value.get("major_red_flag_count")
    if type(major_count) is not int or major_count < 0:
        return False
    return True


def assemble_scored_job(
    row: dict,
    artifact: Mapping[str, object],
    *,
    profile: Mapping[str, object],
    today: date | None = None,
    use_analysis_context: bool = True,
    analyzed_job_id: str | None = None,
) -> dict:
    """Recompute current scoring and assemble one final job from an artifact."""

    if not static_analysis_artifact_is_valid(artifact):
        raise ValueError("Invalid static analysis artifact")
    comp = artifact["compensation"]
    role_cls = artifact["role_classification"]
    company_cls = artifact["company_classification"]
    red_flags = artifact["red_flags"]
    positive = artifact["positive_signals"]
    pmatch = artifact["profile_match"]
    tools = artifact["technology_matches"]
    score = score_job(
        row,
        comp,
        role_cls,
        company_cls,
        red_flags,
        positive,
        pmatch,
        profile,
        tools,
        today=today,
        static_scoring=artifact["static_scoring"],
    )

    return {
        "id": analyzed_job_id or job_id(row),
        "company": row.get("company", ""),
        "title": row.get("title", ""),
        "location": row.get("location", ""),
        "remote_status": row.get("remote_status", ""),
        "internship_type": row.get("internship_type", ""),
        "source_url": row.get("source_url", ""),
        "date_posted": row.get("date_posted", ""),
        "deadline": row.get("deadline", ""),
        "deadline_days_left": score.get("deadline_days_left"),
        "degree_level": score.get("degree_level"),
        "degree_eligible": score.get("degree_eligible"),
        "degree_ineligible_reason": score.get("degree_ineligible_reason"),
        "student_eligibility": score.get("student_eligibility"),
        "eligibility_exclusion_reason": score.get(
            "eligibility_exclusion_reason"
        ),
        "eligibility_explanation": score.get("eligibility_explanation"),
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
    }


def sort_scored_jobs(jobs: list[dict]) -> list[dict]:
    """Apply the existing stable descending-total ordering in place."""

    jobs.sort(key=lambda job: -job["score"]["total"])
    return jobs


def _analyze_rows_with_report(
    rows: list[dict],
    today: date | None = None,
    *,
    include_audit_diagnostics: bool = False,
    use_analysis_context: bool = True,
) -> tuple[list[dict], list]:
    """Shared analysis engine plus dedupe report for CSV cleaning metadata."""
    today = today or date.today()
    profile = load_profile()
    profile_skill_matcher = (
        build_profile_skill_matcher(profile)
        if use_analysis_context
        else None
    )
    known = config.load_known_companies()

    unique_rows, dup_report = deduplicate_rows(
        rows,
        include_audit_diagnostics=include_audit_diagnostics,
    )

    jobs = []
    resolved_job_ids = analyzed_job_ids(unique_rows)
    for row, resolved_job_id in zip(unique_rows, resolved_job_ids):
        artifact = analyze_static_row(
            row,
            profile=profile,
            known=known,
            profile_skill_matcher=profile_skill_matcher,
            use_analysis_context=use_analysis_context,
        )
        jobs.append(
            assemble_scored_job(
                row,
                artifact,
                profile=profile,
                today=today,
                use_analysis_context=use_analysis_context,
                analyzed_job_id=resolved_job_id,
            )
        )

    sort_scored_jobs(jobs)
    return jobs, dup_report


def analyze_rows(
    rows: list[dict],
    today: date | None = None,
    *,
    include_dedupe_report: bool = False,
    include_audit_diagnostics: bool = False,
):
    """Dedupe, analyze, and score already-built canonical rows.

    By default this returns only the scored jobs. `process_csv` asks for the
    dedupe report too so its cleaning report can keep the existing shape.
    """
    jobs, dup_report = _analyze_rows_with_report(
        rows,
        today=today,
        include_audit_diagnostics=include_audit_diagnostics,
    )
    if include_dedupe_report:
        return jobs, dup_report
    return jobs


def _salary_stats_from_jobs(jobs: list[dict]) -> dict:
    salary_stats = {"parsed": 0, "unparsed": 0, "period_assumed": 0}
    for job in jobs:
        comp = job["compensation"]
        if comp["usd_hourly_min"] is not None or comp["kind"] in ("unpaid", "equity_only", "commission_only"):
            salary_stats["parsed"] += 1
        else:
            salary_stats["unparsed"] += 1
        if comp.get("period_assumed"):
            salary_stats["period_assumed"] += 1
    return salary_stats


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

    # --- analyze + score ----------------------------------------------------
    jobs, dup_report = analyze_rows(rows, today=today, include_dedupe_report=True)
    salary_stats = _salary_stats_from_jobs(jobs)

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
