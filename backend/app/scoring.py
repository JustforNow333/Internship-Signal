"""Transparent 0-100 scoring.

total = sum(category_score * weight) over eight categories (weights in
config.SCORE_WEIGHTS). Each category returns a 0-100 score *and* a
one-line explanation; the API exposes both so the UI can show exactly
where every point came from.

Hard rules sit on top of the weighted sum:
  - any critical red flag caps the bucket at "low" and the action at "skip"
  - an expired deadline forces "skip"
"""

from datetime import date
import re

from .config import BUCKET_THRESHOLDS, SCORE_WEIGHTS
from .normalize import days_until
from .salary import hourly_mid

ACTION_LABELS = {
    "apply_now": "Apply now",
    "apply_later": "Apply later",
    "research_more": "Research more",
    "skip": "Skip",
}

SOFTWARE_FIT_TRACKS = {
    "backend",
    "full_stack",
    "frontend",
    "general_swe",
    "platform_infra",
    "data_engineering",
    "ml_ai",
    "quant_dev",
    "cloud",
    "devops",
    "embedded_software",
    "firmware",
    "sdet_qa_automation",
}
LOW_PRIORITY_FIT_TRACKS = {"it_support", "quality_test", "solutions_engineering"}
ROLE_TRACK_PRIORITY = {
    "backend": 0,
    "full_stack": 1,
    "general_swe": 2,
    "platform_infra": 3,
    "data_engineering": 4,
    "ml_ai": 5,
    "quant_dev": 6,
    "frontend": 7,
    "cloud": 8,
    "devops": 9,
    "embedded_software": 10,
    "firmware": 11,
    "sdet_qa_automation": 12,
    "it_support": 50,
    "quality_test": 51,
    "solutions_engineering": 52,
}
BACKEND_FIT_RE = re.compile(
    r"\bback[- ]?end\b|\bserver[- ]side\b|\bapis?\b|\brest(ful)?\b|"
    r"\bservices?\b|\bmicroservices?\b|\bdistributed systems?\b|"
    r"\bdatabases?\b|\bsql\b|\bpostgres(ql)?\b|\bmysql\b|\bspring\b",
    re.I,
)
PRODUCTION_CODE_RE = re.compile(r"production code|code review|ship (real )?(code|features?)|own .*service|building services?", re.I)
NEAR_PERFECT_TRACKS = {"backend", "full_stack", "data_engineering", "ml_ai", "general_swe"}
CORE_RESUME_STACK_RE = re.compile(
    r"\bpython\b|\bjava\b(?!\s*script)|\bsql\b|\bjavascript\b|\btypescript\b|"
    r"\bfastapi\b|\bflask\b|\bsqlalchemy\b|\breact\b|\bnext\.?js\b|"
    r"\bpandas\b|\bpostgres(?:ql)?\b|\bsqlite\b",
    re.I,
)
RESUME_STRONG_SKILL_PATTERNS = [
    ("Python", r"\bpython\b"),
    ("Java", r"\bjava\b(?!\s*script)"),
    ("SQL", r"\bsql\b"),
    ("JavaScript", r"\bjava\s*script\b|\bjavascript\b|\bjs\b"),
    ("TypeScript", r"\btype\s*script\b|\btypescript\b|\bts\b"),
    ("FastAPI", r"\bfastapi\b|fast api"),
    ("Flask", r"\bflask\b"),
    ("SQLAlchemy", r"\bsqlalchemy\b|sql alchemy"),
    ("Next.js", r"\bnext\.?js\b"),
    ("React", r"\breact(?:\.js)?\b"),
    ("Pandas", r"\bpandas\b"),
    ("PostgreSQL", r"\bpostgres(?:ql)?\b"),
    ("SQLite", r"\bsqlite\b"),
    ("REST/API/backend service work", r"\brest(?:ful)?\b|\bapis?\b|\bback[- ]?end\b|\bserver[- ]side\b|\bservices?\b|\bmicroservices?\b"),
    ("data ingestion/pipelines", r"\bdata ingestion\b|\betl\b|\bdata pipelines?\b|\bmarket data pipelines?\b|\bpipelines?\b"),
    ("data analytics", r"\bdata analy(tics|sis)\b|\banalytics\b"),
    ("spreadsheet/data apps", r"\bspreadsheet\b|\bdata apps?\b|\bdashboards?\b"),
    ("full-stack web apps", r"\bfull[- ]?stack\b|\bweb apps?\b"),
    ("Pytest/testing/evals", r"\bpytest\b|\btesting\b|\bunit tests?\b|\bevals?\b|\bevaluations?\b"),
]
SMALL_FIT_BONUS_PATTERNS = [
    ("Git/GitHub", r"\bgit\b|\bgithub\b", 2),
    ("OpenAI API/LLM app work", r"\bopenai api\b|\bllm\b|\blarge language model", 2),
    ("Vercel/Render deployment", r"\bvercel\b|\brender\b|\bdeploy(ment|ed)?\b", 2),
    ("finance/data app relevance", r"\bfinance\b|\bmarket data\b|\btrading data\b|\bspreadsheet\b|\bdata apps?\b", 2),
]
CPLUS_RE = re.compile(r"\bc\+\+\b|\bcpp\b", re.I)
GO_RUST_RE = re.compile(r"\bgolang\b|\brust\b|\bgo\b", re.I)
LOW_LEVEL_RE = re.compile(r"low[- ]level|kernel|driver|firmware|embedded|robotics?|hardware|electrical|mechanical|manufactur|cad\b", re.I)
OPS_HEAVY_RE = re.compile(r"\bsre\b|site reliability|on[- ]call|incident|ci/cd|terraform|kubernetes|cloud operations?|infrastructure operations?", re.I)
PRODUCT_DEV_RE = re.compile(r"product feature|user-facing feature|backend product|application|apis?|services?|platform services?", re.I)
ANALYTICS_REPORTING_RE = re.compile(r"\banalytics\b|\breporting\b|\breports?\b|\bdashboards?\b|business intelligence", re.I)
DATA_SOFTWARE_RE = re.compile(r"data engineer|pipeline|etl|software|python|sql|pandas|model|machine learning|ml\b|api", re.I)
VAGUE_TITLE_RE = re.compile(r"\btechnical intern\b|\btechnical co[- ]?op\b", re.I)
COMMERCIAL_SUPPORT_RE = re.compile(r"commercial|customer[- ]facing|customer support|technical support|solutions?|sales|pre[- ]?sales|implementation consultant", re.I)
PHD_TERM = r"(?:ph\.?\s*d\.?|phd|doctoral|doctorate)"
MASTERS_TERM = r"(?:master(?:['\u2019])?s|masters|m\.s\.|master of science)"
PHD_INTERNSHIP_RE = re.compile(
    rf"\b{PHD_TERM}(?=\W|$).{{0,60}}\b(intern(ship)?|co[- ]?op|university grad)\b|"
    rf"\b(intern(ship)?|co[- ]?op|research intern|engineer)\b.{{0,60}}\b{PHD_TERM}(?=\W|$)|"
    r"\bphd university grad\b",
    re.I,
)
MASTERS_INTERNSHIP_RE = re.compile(
    rf"\b{MASTERS_TERM}(?=\W|$).{{0,60}}\b(intern(ship)?|co[- ]?op|students?|candidates?)\b|"
    rf"\b(intern(ship)?|co[- ]?op)\b.{{0,60}}\b{MASTERS_TERM}(?=\W|$)|"
    r"\bms intern(ship)?\b",
    re.I,
)
MBA_INTERNSHIP_RE = re.compile(
    r"\bmba\b.{0,60}\b(intern(ship)?|co[- ]?op|students?|candidates?)\b|"
    r"\b(intern(ship)?|co[- ]?op)\b.{0,60}\bmba\b",
    re.I,
)
GRADUATE_INTERNSHIP_RE = re.compile(
    r"\bgraduate student intern(ship)?\b|\bgraduate intern(ship)?\b|"
    r"\bgraduate\b.{0,40}\b(intern(ship)?|co[- ]?op)\b|"
    r"\bintern(ship)?\b.{0,60}\bgraduate students?\b|"
    r"\badvanced degree intern(ship)?\b|\badvanced degree candidates?\b",
    re.I,
)
POSTDOC_RE = re.compile(r"\bpost\s*doc(?:toral)?\b|\bpostdoctoral\b", re.I)
UNDERGRAD_RE = re.compile(
    r"\bundergraduate\b|\bbachelor(?:['\u2019])?s\b|\bbachelors\b|\bbs\b|\bba\b|"
    r"\bsophomore\b|\bjunior\b|\bsenior\b",
    re.I,
)


def _clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


def detect_degree_eligibility(row) -> tuple[str, bool, str | None]:
    """Return watcher degree eligibility for undergraduate-targeted internships."""

    text = " ".join([row.get("title", ""), row.get("description", ""), row.get("requirements", "")])
    if POSTDOC_RE.search(text):
        return "postdoctoral", False, "Graduate/PhD-level internship outside undergraduate target."
    if PHD_INTERNSHIP_RE.search(text):
        return "phd", False, "Graduate/PhD-level internship outside undergraduate target."
    if MASTERS_INTERNSHIP_RE.search(text):
        return "masters", False, "Graduate/PhD-level internship outside undergraduate target."
    if MBA_INTERNSHIP_RE.search(text):
        return "mba", False, "Graduate/PhD-level internship outside undergraduate target."
    if GRADUATE_INTERNSHIP_RE.search(text):
        return "graduate", False, "Graduate/PhD-level internship outside undergraduate target."
    if UNDERGRAD_RE.search(text):
        return "undergraduate", True, None
    return "unspecified", True, None


# ---------------------------------------------------------------------------
# Category scorers — each returns (score, explanation)
# ---------------------------------------------------------------------------

def _role_track_affinity(role_cls, profile):
    track = role_cls.get("role_track") or role_cls.get("role") or "unknown"
    track_affinity = profile.get("role_track_affinity", {})
    if track in track_affinity:
        return int(track_affinity.get(track) or 0)
    return int(profile.get("role_affinity", {}).get(role_cls.get("role"), 0) or 0)


def _watcher_ineligible_reason(role_cls, profile) -> str | None:
    track = role_cls.get("role_track") or "unknown"
    if _role_track_affinity(role_cls, profile) > 0:
        return None
    label = role_cls.get("role_track_label") or role_cls.get("label") or track.replace("_", " ")
    if track == "unknown":
        return "Role lacks strong software/backend/data/ML/quant evidence for the SWE watcher."
    return f"{label} role outside the target SWE/software-adjacent watcher track."


def _watcher_action(fit_score: int, eligible: bool, expired: bool) -> str:
    if expired or not eligible or fit_score <= 0:
        return "skip"
    if fit_score >= 75:
        return "apply_now"
    if fit_score >= 45:
        return "apply_later"
    return "research_more"


def score_role_relevance(row, role_cls, pmatch, profile):
    track = role_cls.get("role_track") or role_cls.get("role") or "unknown"
    base = _role_track_affinity(role_cls, profile)
    if base <= 0:
        reason = _watcher_ineligible_reason(role_cls, profile) or "Role is outside the watcher target track."
        return 0, reason

    text = " ".join([row.get("title", ""), row.get("description", ""), row.get("requirements", "")])
    backend_hit = BACKEND_FIT_RE.search(text)
    adjacent_software_ownership = bool(
        backend_hit
        or PRODUCTION_CODE_RE.search(text)
        or re.search(r"developer tooling|automation code|platform services?|cloud APIs?|software ownership", text, re.I)
    )
    if track in {"cloud", "devops"} and not adjacent_software_ownership:
        return 0, f"{role_cls.get('role_track_label')} title lacks clear coding/backend/platform software ownership."

    if track in LOW_PRIORITY_FIT_TRACKS:
        label = role_cls.get("role_track_label") or track.replace("_", " ")
        return min(base, 20), f"{label} is visible as a low-priority adjacent track, capped at 20."

    score = float(base)
    strong_matches = _pattern_labels(RESUME_STRONG_SKILL_PATTERNS, text)
    strong_bonus = min(len(strong_matches) * 4, 16) if track in SOFTWARE_FIT_TRACKS else 0
    score += strong_bonus

    small_matches = []
    small_bonus = 0
    for label, pattern, amount in SMALL_FIT_BONUS_PATTERNS:
        if re.search(pattern, text, re.I):
            small_matches.append(label)
            small_bonus += amount
    small_bonus = min(small_bonus, 6)
    score += small_bonus

    penalties: list[str] = []
    has_core_resume_overlap = bool(CORE_RESUME_STACK_RE.search(text))
    if CPLUS_RE.search(text) and not has_core_resume_overlap:
        score -= 6
        penalties.append("C++ stack with no Python/Java/SQL/JS/TS overlap (-6)")
    if GO_RUST_RE.search(text) and not has_core_resume_overlap:
        score -= 8
        penalties.append("Go/Rust stack with no Python/Java/SQL/JS/TS overlap (-8)")
    if LOW_LEVEL_RE.search(text) or track in {"embedded_software", "firmware"}:
        score -= 10
        penalties.append("low-level/embedded/hardware-heavy context (-10)")
    if track in {"devops", "cloud"} and OPS_HEAVY_RE.search(text) and not PRODUCT_DEV_RE.search(text):
        score -= 10
        penalties.append("ops/cloud/SRE work without backend product development (-10)")
    if ANALYTICS_REPORTING_RE.search(text) and not DATA_SOFTWARE_RE.search(text):
        score -= 8
        penalties.append("analytics/reporting without data-engineering or software evidence (-8)")
    if VAGUE_TITLE_RE.search(row.get("title", "")) and len(strong_matches) < 2:
        score -= 12
        penalties.append("vague technical title with unclear software duties (-12)")
    if COMMERCIAL_SUPPORT_RE.search(text):
        score -= 15
        penalties.append("commercial/customer-facing/support orientation (-15)")

    if role_cls.get("confidence", 1) < 0.5:
        score -= 10
        penalties.append("low classification confidence (-10)")

    near_perfect_candidate = track in NEAR_PERFECT_TRACKS and len(strong_matches) >= 4
    if score > 94 and not near_perfect_candidate:
        score = 94

    score = _clamp(round(score))
    return score, _fit_explanation(role_cls, strong_matches, small_matches, penalties, score)


def _pattern_labels(patterns, text: str) -> list[str]:
    labels = []
    for label, pattern in patterns:
        if re.search(pattern, text, re.I):
            labels.append(label)
    return labels


def _fit_explanation(role_cls, strong_matches: list[str], small_matches: list[str], penalties: list[str], score: int) -> str:
    track_label = role_cls.get("role_track_label") or role_cls.get("label") or "Eligible role"
    if strong_matches:
        top = ", ".join(strong_matches[:5])
        prefix = f"{track_label} with resume overlap in {top}"
        if len(strong_matches) > 5:
            prefix += f", plus {len(strong_matches) - 5} more"
    else:
        prefix = f"{track_label}, but limited direct resume stack overlap"

    details = []
    if small_matches:
        details.append("smaller positives: " + ", ".join(small_matches[:3]))
    if penalties:
        details.append("penalties: " + "; ".join(penalties[:3]))
    if score >= 95:
        details.append("near-perfect fit requires multiple direct resume matches")
    return prefix + ("; " + "; ".join(details) if details else "") + "."


def score_compensation(comp, profile):
    kind = comp["kind"]
    if kind == "unpaid":
        return 0, "Unpaid."
    if kind == "equity_only":
        return 5, "Equity only — no cash compensation."
    if kind == "commission_only":
        return 10, "Commission only — no guaranteed pay."
    if kind == "stipend_unspecified":
        return 50, "Stipend mentioned but amount unknown."
    if kind in ("unknown_vague", "unknown"):
        return 45, "Compensation not stated — scored neutral-low."

    mid = hourly_mid(comp)
    if mid is None:
        return 45, "Amount could not be normalized."
    bands = [(40, 100), (35, 95), (30, 90), (25, 80), (20, 70), (15, 55), (10, 40), (5, 25), (0.01, 10)]
    score = next((s for threshold, s in bands if mid >= threshold), 0)
    expl = f"≈${mid:.2f}/hr (USD equivalent)."
    if comp["period_assumed"]:
        expl += " Pay period was assumed — verify before relying on this."
    if mid < profile.get("min_acceptable_hourly_usd", 15):
        expl += " Below your stated minimum."
    return score, expl


def score_legitimacy(red_flags, company_cls, comp, positive):
    score = 70
    parts = []
    sev_cost = {"critical": 30, "major": 12, "minor": 4}
    for f in red_flags:
        score -= sev_cost[f["severity"]]
        parts.append(f"-{sev_cost[f['severity']]} {f['label'].lower()}")
    if any(s["id"] == "reputable" for s in positive):
        score += 12
        parts.append("+12 well-known employer")
    if company_cls["category"] in ("tech", "startup"):
        score += 6
        parts.append("+6 classified as a real tech employer")
    elif company_cls["category"] == "unknown":
        score -= 5
        parts.append("-5 employer could not be verified")
    if comp["kind"] == "paid" and comp["confidence"] >= 0.5:
        score += 6
        parts.append("+6 concrete pay stated")
    expl = "Base 70; " + ", ".join(parts) + "." if parts else "Base 70 with no adjustments."
    return _clamp(score), expl


def score_learning(red_flags, positive):
    score = 50
    parts = []
    gains = {"mentorship": 20, "ownership": 15, "conversion": 10, "structured_program": 8}
    for s in positive:
        if s["id"] in gains:
            score += gains[s["id"]]
            parts.append(f"+{gains[s['id']]} {s['label'].lower()}")
    flag_costs = {"grunt_work": 25, "no_learning_mention": 12, "founder_responsibilities": 10}
    for f in red_flags:
        if f["id"] in flag_costs:
            score -= flag_costs[f["id"]]
            parts.append(f"-{flag_costs[f['id']]} {f['label'].lower()}")
    expl = "Base 50; " + ", ".join(parts) + "." if parts else "Base 50 — posting says little about learning either way."
    return _clamp(score), expl


def score_technical_depth(row, role_cls, tools):
    n = len(tools)
    if n == 0:
        score = 30
    elif n <= 2:
        score = 55
    elif n <= 5:
        score = 75
    elif n <= 8:
        score = 88
    else:
        score = 95
    expl = f"{n} concrete technolog{'y' if n == 1 else 'ies'} named"
    if tools:
        expl += f" ({', '.join(tools[:5])})"
    track = role_cls.get("role_track") or role_cls.get("role")
    if track not in SOFTWARE_FIT_TRACKS:
        score = min(score, 35)
        expl += "; capped — role itself is not a software-track role"
    return score, expl + "."


HOOPS = [
    (r"cover letter", "cover letter", 8),
    (r"take[- ]home|coding challenge|assessment", "take-home/assessment", 8),
    (r"video (submission|introduction|essay)", "video submission", 10),
    (r"\bessays?\b", "essay questions", 8),
    (r"\btranscript\b", "transcript", 4),
    (r"(two|three|2|3) references", "references", 5),
]


def score_effort(row):
    import re
    text = " ".join([row.get("description", ""), row.get("requirements", "")])
    score = 70
    parts = []
    for pat, label, cost in HOOPS:
        if re.search(pat, text, re.I):
            score -= cost
            parts.append(f"-{cost} {label}")
    if re.search(r"easy apply|apply with (your )?resume|resume only", text, re.I):
        score += 12
        parts.append("+12 resume-only application")
    expl = "Base 70; " + ", ".join(parts) + "." if parts else "Base 70 — no unusual application hoops mentioned."
    return _clamp(score), expl


def score_location(row, profile):
    remote = (row.get("remote_status") or "").lower()
    loc = (row.get("location") or "").lower()
    preferred = any(p in loc for p in profile.get("preferred_locations", []))
    if "remote" in remote or loc.strip() == "remote":
        return 100, "Remote — works from anywhere."
    if "hybrid" in remote:
        return (85, f"Hybrid in a preferred area ({row.get('location')}).") if preferred else (60, f"Hybrid in {row.get('location') or 'an unstated location'}.")
    if preferred:
        return 75, f"On-site in a preferred area ({row.get('location')})."
    if not loc:
        return 60, "Location not stated."
    if any(c in loc for c in ("india", "bengaluru", "bangalore", "london", "berlin", "toronto", "singapore")):
        return 30, f"On-site outside the US ({row.get('location')})."
    return 50, f"On-site in {row.get('location')} — outside your preferred areas."


def score_deadline(row, today):
    days = days_until(row.get("deadline", ""), today)
    if days is None:
        return 70, "No deadline / rolling — apply when ready.", None
    if days < 0:
        return 0, f"Deadline passed {-days} day{'s' if days != -1 else ''} ago.", days
    if days <= 2:
        return 55, f"Closes in {days} day{'s' if days != 1 else ''} — tight but doable tonight.", days
    if days <= 14:
        return 85, f"Closes in {days} days — act soon.", days
    if days <= 45:
        return 100, f"{days} days left — comfortable window.", days
    return 75, f"{days} days left — no urgency.", days


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def score_job(row, comp, role_cls, company_cls, red_flags, positive, pmatch, profile, tools, today=None):
    today = today or date.today()
    degree_level, degree_eligible, degree_ineligible_reason = detect_degree_eligibility(row)

    cat = {}
    cat["role_relevance"] = score_role_relevance(row, role_cls, pmatch, profile)
    cat["compensation"] = score_compensation(comp, profile)
    cat["legitimacy"] = score_legitimacy(red_flags, company_cls, comp, positive)
    cat["learning_value"] = score_learning(red_flags, positive)
    cat["technical_depth"] = score_technical_depth(row, role_cls, tools)
    cat["effort_vs_value"] = score_effort(row)
    cat["location_convenience"] = score_location(row, profile)
    dl_score, dl_expl, days_left = score_deadline(row, today)
    cat["deadline_urgency"] = (dl_score, dl_expl)

    categories = {
        name: {"score": int(round(s)), "weight": SCORE_WEIGHTS[name], "explanation": e}
        for name, (s, e) in cat.items()
    }
    total = round(sum(c["score"] * c["weight"] for c in categories.values()))
    fit_score = categories["role_relevance"]["score"]
    if not degree_eligible:
        fit_score = 0
    watcher_eligible = fit_score > 0 and degree_eligible
    fit_explanation = categories["role_relevance"]["explanation"]
    watcher_ineligible_reason = (
        degree_ineligible_reason
        if not degree_eligible
        else _watcher_ineligible_reason(role_cls, profile) or (fit_explanation if not watcher_eligible else None)
    )

    has_critical = any(f["severity"] == "critical" for f in red_flags)
    majors = sum(1 for f in red_flags if f["severity"] == "major")
    expired = days_left is not None and days_left < 0

    if not watcher_eligible:
        total = min(total, BUCKET_THRESHOLDS["maybe"] - 1)
    if has_critical:
        total = min(total, BUCKET_THRESHOLDS["maybe"] - 5)
    elif majors >= 3:
        # Three independent major red flags is a pattern, not bad luck.
        total = min(total, BUCKET_THRESHOLDS["maybe"] - 1)

    bucket = "high" if total >= BUCKET_THRESHOLDS["high"] else "maybe" if total >= BUCKET_THRESHOLDS["maybe"] else "low"
    if not watcher_eligible or has_critical or majors >= 3:
        bucket = "low"

    if not watcher_eligible:
        action = "skip"
    elif has_critical:
        action = "skip"
    elif majors >= 3:
        action = "skip"
    elif expired:
        action = "skip"
    elif total >= BUCKET_THRESHOLDS["high"] and majors == 0:
        action = "apply_now"
    elif total >= BUCKET_THRESHOLDS["high"]:
        action = "research_more"
    elif total >= 60 and days_left is not None and 0 <= days_left <= 7 and majors == 0:
        action = "apply_now"
    elif role_cls.get("confidence", 1) < 0.6 and total < BUCKET_THRESHOLDS["high"]:
        action = "research_more"
    elif total >= 55:
        action = "apply_later"
    elif total >= BUCKET_THRESHOLDS["maybe"]:
        action = "research_more"
    else:
        action = "skip"

    watcher_action = _watcher_action(fit_score, watcher_eligible, expired)
    reasons = _top_reasons(positive, categories, fit_explanation=fit_explanation, watcher_eligible=watcher_eligible)
    concerns = _top_concerns(red_flags, categories, expired)
    if watcher_ineligible_reason and watcher_ineligible_reason not in concerns:
        concerns.insert(0, watcher_ineligible_reason)
        concerns = concerns[:3]

    explanation = (
        f"Weighted total of eight categories: {total}/100. Strongest: "
        f"{_extreme(categories, best=True)}. Weakest: {_extreme(categories, best=False)}."
        + (" A critical red flag caps this posting in the low bucket." if has_critical
           else " Three or more major red flags cap this posting in the low bucket." if majors >= 3 else "")
    )

    return {
        "total": int(total),
        "fit_score": int(fit_score if watcher_eligible else 0),
        "watcher_eligible": bool(watcher_eligible),
        "watcher_ineligible_reason": None if watcher_eligible else watcher_ineligible_reason,
        "fit_explanation": fit_explanation,
        "role_track": role_cls.get("role_track", "unknown"),
        "degree_level": degree_level,
        "degree_eligible": bool(degree_eligible),
        "degree_ineligible_reason": degree_ineligible_reason,
        "watcher_action": watcher_action,
        "watcher_action_label": ACTION_LABELS[watcher_action],
        "bucket": bucket,
        "action": action,
        "action_label": ACTION_LABELS[action],
        "categories": categories,
        "reasons": reasons,
        "concerns": concerns,
        "explanation": explanation,
        "deadline_days_left": days_left,
    }


def _extreme(categories, best=True):
    name, data = (max if best else min)(categories.items(), key=lambda kv: kv[1]["score"])
    return f"{name.replace('_', ' ')} ({data['score']})"


def _top_reasons(positive, categories, *, fit_explanation=None, watcher_eligible=True):
    reasons = []
    if watcher_eligible and fit_explanation:
        reasons.append(f"Role fit: {fit_explanation}")
    reasons.extend(s["label"] for s in sorted(positive, key=lambda s: -s["strength"]))
    for name, data in sorted(categories.items(), key=lambda kv: -kv[1]["score"]):
        if data["score"] >= 85 and len(reasons) < 6:
            reasons.append(f"Strong {name.replace('_', ' ')}: {data['explanation']}")
    seen, out = set(), []
    for r in reasons:
        if r not in seen:
            out.append(r)
            seen.add(r)
        if len(out) == 3:
            break
    return out or ["No standout strengths found."]


def _top_concerns(red_flags, categories, expired):
    order = {"critical": 0, "major": 1, "minor": 2}
    concerns = [f["label"] for f in sorted(red_flags, key=lambda f: order[f["severity"]])]
    if expired and not any("Deadline passed" in c for c in concerns):
        concerns.insert(0, "Deadline has already passed")
    for name, data in sorted(categories.items(), key=lambda kv: kv[1]["score"]):
        if data["score"] <= 40 and len(concerns) < 6:
            concerns.append(f"Weak {name.replace('_', ' ')}: {data['explanation']}")
    seen, out = set(), []
    for c in concerns:
        if c not in seen:
            out.append(c)
            seen.add(c)
        if len(out) == 3:
            break
    return out or ["No major concerns detected."]
