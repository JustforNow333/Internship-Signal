"""Red flags, positive signals, and profile matching.

Each signal carries an id, a human label, a severity/strength, and the
evidence text that triggered it, so nothing is a black box. Severity for
red flags: critical (legitimacy-breaking) > major > minor.
"""

import re

from .dedupe import norm_company
from .salary import hourly_mid

# ---------------------------------------------------------------------------
# Red flag patterns
# ---------------------------------------------------------------------------

SCAM_FEE = re.compile(
    r"(training|onboarding|application|registration|placement) fee|pay (a |an )?fee|\$\d+ (fee|deposit)|send (us )?payment",
    re.I,
)
NO_INTERVIEW = re.compile(r"no interview|immediate hire|start (today|immediately) no", re.I)
OFFPLATFORM = re.compile(r"\bwhatsapp\b|\btelegram\b|text us at", re.I)
FOUNDER_PHRASES = [
    r"wear many hats", r"ground floor", r"build .{0,20}from scratch",
    r"like a (founder|co[- ]?founder)", r"no task (is )?too small",
    r"do whatever it takes", r"build (the|our) (mvp|company)",
    r"work directly with the (ceo|founders?)", r"hustle\b",
]
YEARS_REQ = re.compile(r"(\d+)\s*\+?\s*years?", re.I)
NIGHTS_WEEKENDS = re.compile(r"nights and weekends|60\+?\s*hours|evenings? and weekends? required", re.I)
GRUNT_WORK = [r"data entry", r"cold[- ]call", r"door[- ]to[- ]door", r"\bfiling\b", r"run errands", r"fetch coffee", r"answer(ing)? phones", r"\brepetitive\b", r"enter(ing)? (supplier )?(invoices|data|receipts)"]
LEARNING_WORDS = re.compile(
    r"\bmentor(ship|ing)?\b|\blearn(ing)?\b|\btraining\b|\bgrow(th)?\b|pair programming|1:1|intern (program|cohort)|workshops?",
    re.I,
)

TECH_TOOL_TERMS = [
    "python", "java", "c++", "golang", " go ", "rust", "javascript", "typescript",
    "react", "node", "flask", "django", "fastapi", "spring", "sql", "postgres",
    "postgresql", "mysql", "mongodb", "redis", "kafka", "spark", "airflow", "dbt",
    "pandas", "numpy", "scikit-learn", "pytorch", "tensorflow", "kubernetes",
    "docker", "terraform", "aws", "gcp", "azure", "linux", "git", "graphql",
    "rest", "grpc", "ros", "snowflake",
]

BACKEND_TERMS = re.compile(r"\bback[- ]?end\b|\bapis?\b|\bdatabases?\b|\bdistributed\b|\binfrastructure\b|\bpipelines?\b|\bserver[- ]side\b", re.I)
OWNERSHIP = re.compile(r"\bown(ership)? (a|an|the|your)\b|end[- ]to[- ]end|ship (a |an |your |real )?(feature|project|code|product)|your own project|lead a project", re.I)
CONVERSION = re.compile(r"return offer|full[- ]time (offer|conversion|role)|new[- ]grad pipeline|convert to full[- ]time", re.I)
STRUCTURED = re.compile(r"\b(8|10|12|16)[- ]week\b|summer (intern(ship)? )?program|structured (intern(ship)? )?program|intern cohort|cohort", re.I)

SOFTWARE_SIGNAL_TRACKS = {
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


def _flag(fid, label, severity, evidence):
    return {"id": fid, "label": label, "severity": severity, "evidence": evidence}


def _signal(sid, label, strength, evidence):
    return {"id": sid, "label": label, "strength": strength, "evidence": evidence}


def count_tech_tools(text: str):
    found = []
    lower = " " + (text or "").lower() + " "
    for term in TECH_TOOL_TERMS:
        if term.strip() in ("rest",):  # avoid matching "restaurant"
            if re.search(r"\brest(ful)?\b", lower):
                found.append("rest")
            continue
        if term == " go ":
            if " go " in lower or re.search(r"\bgolang\b", lower):
                found.append("go")
            continue
        if term in lower:
            found.append(term.strip())
    return sorted(set(found))


def detect_red_flags(row, comp, role_cls, company_cls):
    text = " ".join([row.get("description", ""), row.get("requirements", ""), row.get("compensation", "")])
    flags = []

    if SCAM_FEE.search(text):
        flags.append(_flag("scam_fee", "Asks applicants to pay a fee", "critical", SCAM_FEE.search(text).group(0)))
    if NO_INTERVIEW.search(text):
        flags.append(_flag("no_interview", "\u201cNo interview / immediate hire\u201d hiring", "major", NO_INTERVIEW.search(text).group(0)))
    if OFFPLATFORM.search(text):
        flags.append(_flag("offplatform_recruiting", "Recruiting via WhatsApp/Telegram", "major", OFFPLATFORM.search(text).group(0)))

    if comp["kind"] == "unpaid":
        flags.append(_flag("unpaid", "Unpaid position", "major", comp["raw"] or "unpaid"))
    elif comp["kind"] == "equity_only":
        flags.append(_flag("equity_only", "Equity-only compensation", "major", comp["raw"]))
    elif comp["kind"] == "commission_only":
        flags.append(_flag("commission_only", "Commission-only pay", "major", comp["raw"]))
    elif comp["kind"] in ("unknown_vague", "unknown", "stipend_unspecified"):
        flags.append(_flag("vague_comp", "Compensation unclear or unstated", "minor", comp["raw"] or "(blank)"))

    mid = hourly_mid(comp)
    if comp["kind"] == "paid" and mid is not None and mid < 7.5 and comp["confidence"] >= 0.4:
        flags.append(_flag(
            "very_low_pay", "Pay works out below US minimum wage",
            "minor", f"~${mid:.2f}/hr equivalent ({comp['raw']})",
        ))

    years = [int(m.group(1)) for m in YEARS_REQ.finditer(row.get("requirements", ""))]
    max_years = max(years) if years else 0
    if max_years >= 3:
        flags.append(_flag(
            "unrealistic_experience",
            f"Asks for {max_years}+ years of experience from an intern",
            "major", f"{max_years}+ years in requirements",
        ))

    founder_hits = [re.search(p, text, re.I).group(0) for p in FOUNDER_PHRASES if re.search(p, text, re.I)]
    if len(founder_hits) >= 2:
        flags.append(_flag(
            "founder_responsibilities",
            "Founder-level responsibilities pitched as an internship",
            "major", "; ".join(founder_hits[:3]),
        ))

    tools = count_tech_tools(row.get("requirements", ""))
    if len(tools) > 10:
        flags.append(_flag("laundry_list", f"Requirements list {len(tools)} technologies", "minor", ", ".join(tools[:12])))

    if NIGHTS_WEEKENDS.search(text):
        flags.append(_flag("excessive_hours", "Excessive hours expected", "major", NIGHTS_WEEKENDS.search(text).group(0)))

    grunt_text = row.get("title", "") + " " + text
    grunt_hits = [re.search(p, grunt_text, re.I).group(0) for p in GRUNT_WORK if re.search(p, grunt_text, re.I)]
    has_learning = bool(LEARNING_WORDS.search(text))
    if grunt_hits and not has_learning:
        flags.append(_flag("grunt_work", "Busywork with no stated learning component", "major", "; ".join(grunt_hits[:3])))
    elif row.get("description") and not has_learning and role_cls["role"] in ("non_technical", "it", "unknown"):
        flags.append(_flag("no_learning_mention", "No mention of mentorship or learning", "minor", "description"))

    return flags


def detect_positive_signals(row, comp, role_cls, company_cls, profile, known):
    text = " ".join([row.get("description", ""), row.get("requirements", "")])
    title = row.get("title", "")
    signals = []
    role_track = role_cls.get("role_track") or role_cls.get("role")
    is_software_track = role_track in SOFTWARE_SIGNAL_TRACKS

    mid = hourly_mid(comp)
    if comp["kind"] == "paid" and mid is not None:
        if mid >= 30:
            signals.append(_signal("paid_well", f"Strong pay (~${mid:.0f}/hr equivalent)", 3, comp["raw"]))
        elif mid >= profile.get("min_acceptable_hourly_usd", 15):
            signals.append(_signal("paid", f"Paid (~${mid:.0f}/hr equivalent)", 2, comp["raw"]))

    matched = matched_skills(row, profile)
    if is_software_track and len(matched) >= 2:
        signals.append(_signal("stack_match", "Tech stack overlaps your experience", 3, ", ".join(matched[:6])))
    elif is_software_track and len(matched) == 1:
        signals.append(_signal("stack_match", "Some stack overlap with your experience", 1, matched[0]))

    if OWNERSHIP.search(text):
        signals.append(_signal("ownership", "Clear project ownership", 2, OWNERSHIP.search(text).group(0)))
    if LEARNING_WORDS.search(text):
        signals.append(_signal("mentorship", "Mentorship / learning emphasized", 2, LEARNING_WORDS.search(text).group(0)))
    if CONVERSION.search(text):
        signals.append(_signal("conversion", "Return-offer / full-time pipeline", 2, CONVERSION.search(text).group(0)))
    if STRUCTURED.search(text + " " + title):
        signals.append(_signal("structured_program", "Structured internship program", 1, STRUCTURED.search(text + " " + title).group(0)))

    name_norm = norm_company(row.get("company", ""))
    if name_norm in known.get("reputable", set()):
        signals.append(_signal("reputable", "Well-known employer", 2, row.get("company", "")))

    tools = count_tech_tools(row.get("requirements", "") + " " + row.get("description", ""))
    if is_software_track and len(tools) >= 3:
        signals.append(_signal("specific_tech", "Names a concrete technical stack", 2, ", ".join(tools[:6])))

    if BACKEND_TERMS.search(title + " " + text) and role_track in {
        "backend", "full_stack", "general_swe", "platform_infra", "data_engineering", "ml_ai", "quant_dev"
    }:
        signals.append(_signal("backend_focus", "Backend / data infrastructure focus", 2, BACKEND_TERMS.search(title + " " + text).group(0)))

    if company_cls.get("is_startup"):
        signals.append(_signal("startup_env", "Startup environment (matches your interest)", 1, "; ".join(company_cls.get("evidence", [])[:1])))

    return signals


def matched_skills(row, profile):
    blob = " ".join([row.get("title", ""), row.get("requirements", ""), row.get("description", "")]).lower()
    found = []
    for skill in profile.get("skills", []):
        s = skill.lower()
        pattern = r"\b" + re.escape(s).replace(r"\ ", r"[\s-]") + r"s?\b"
        if re.search(pattern, blob):
            found.append(skill)
    return found


def profile_match(row, role_cls, profile):
    skills = matched_skills(row, profile)
    blob = " ".join([row.get("title", ""), row.get("description", "")]).lower()
    interest_map = {
        "backend": r"back[- ]?end|api|infrastructure|server",
        "data science": r"data scien|analytics|data analy",
        "ml/ai": r"machine learning|\bml\b|\bai\b|deep learning",
        "quant": r"quant|trading",
        "startup engineering": r"startup|seed|founding|early[- ]stage",
    }
    interests = [i for i in profile.get("interests", []) if re.search(interest_map.get(i, re.escape(i)), blob, re.I)]

    if skills and interests:
        summary = f"Matches your {', '.join(interests[:2])} interest and uses {len(skills)} of your skills."
    elif skills:
        summary = f"Uses {len(skills)} of your skills: {', '.join(skills[:4])}."
    elif interests:
        summary = f"Aligned with your interest in {', '.join(interests[:2])}."
    else:
        summary = "Little overlap with your stated skills and interests."
    return {"matched_skills": skills, "matched_interests": interests, "summary": summary}
