"""Red flags, positive signals, and profile matching.

Each signal carries an id, a human label, a severity/strength, and the
evidence text that triggered it, so nothing is a black box. Severity for
red flags: critical (legitimacy-breaking) > major > minor.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

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


def _technology_pattern(term: str) -> re.Pattern[str] | None:
    if term == " go ":
        return None
    if term.strip() == "rest":
        return re.compile(r"\brest(ful)?\b")
    normalized = term.strip()
    right_boundary = r"(?![a-z0-9])" if normalized[-1].isalnum() else ""
    return re.compile(
        rf"(?<![a-z0-9]){re.escape(normalized)}{right_boundary}"
    )


_TECH_TOOL_PATTERNS = tuple(
    (term, _technology_pattern(term))
    for term in TECH_TOOL_TERMS
)
_GOLANG_PATTERN = re.compile(r"\bgolang\b")

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


def _count_tech_tools_lower(lower_text: str) -> list[str]:
    found = []
    lower = " " + lower_text + " "
    for term, pattern in _TECH_TOOL_PATTERNS:
        if term.strip() in ("rest",):  # avoid matching "restaurant"
            if pattern.search(lower):
                found.append("rest")
            continue
        if term == " go ":
            if " go " in lower or _GOLANG_PATTERN.search(lower):
                found.append("go")
            continue
        normalized = term.strip()
        if pattern.search(lower):
            found.append(normalized)
    return sorted(set(found))


def count_tech_tools(text: str):
    return _count_tech_tools_lower((text or "").lower())


@dataclass(frozen=True, slots=True)
class ProfileSkillMatcher:
    """Profile skills paired with regexes compiled once for the loaded profile."""

    patterns: tuple[tuple[str, re.Pattern[str]], ...]

    def match(self, lowercase_text: str) -> tuple[str, ...]:
        return tuple(
            skill
            for skill, pattern in self.patterns
            if pattern.search(lowercase_text)
        )


@lru_cache(maxsize=16)
def _profile_skill_matcher(
    skills: tuple[str, ...],
) -> ProfileSkillMatcher:
    patterns = []
    for skill in skills:
        lowered = skill.lower()
        pattern = (
            r"\b"
            + re.escape(lowered).replace(r"\ ", r"[\s-]")
            + r"s?\b"
        )
        patterns.append((skill, re.compile(pattern)))
    return ProfileSkillMatcher(tuple(patterns))


def build_profile_skill_matcher(profile) -> ProfileSkillMatcher:
    """Return the cached compiled matcher for a loaded profile."""

    return _profile_skill_matcher(tuple(profile.get("skills", [])))


@dataclass(frozen=True, slots=True)
class PostingAnalysisContext:
    """Text variants and expensive matches shared by one posting analysis."""

    title: str
    description: str
    requirements: str
    compensation: str
    title_lower: str
    description_lower: str
    requirements_lower: str
    compensation_lower: str
    description_requirements: str
    description_requirements_lower: str
    requirements_description: str
    requirements_description_lower: str
    title_description_requirements: str
    title_description_requirements_lower: str
    title_requirements_description: str
    title_requirements_description_lower: str
    title_description: str
    title_description_lower: str
    description_requirements_compensation: str
    description_requirements_compensation_lower: str
    title_description_requirements_compensation: str
    description_requirements_title: str
    requirements_technology_matches: tuple[str, ...]
    full_technology_matches: tuple[str, ...]
    matched_profile_skills: tuple[str, ...]

    @classmethod
    def from_row(
        cls,
        row,
        profile_skill_matcher: ProfileSkillMatcher | None = None,
    ) -> "PostingAnalysisContext":
        title = row.get("title", "")
        description = row.get("description", "")
        requirements = row.get("requirements", "")
        compensation = row.get("compensation", "")

        title_lower = title.lower()
        description_lower = description.lower()
        requirements_lower = requirements.lower()
        compensation_lower = compensation.lower()

        description_requirements = " ".join([description, requirements])
        requirements_description = " ".join([requirements, description])
        title_description_requirements = " ".join(
            [title, description, requirements]
        )
        title_requirements_description = " ".join(
            [title, requirements, description]
        )
        title_description = " ".join([title, description])
        description_requirements_compensation = " ".join(
            [description, requirements, compensation]
        )

        description_requirements_lower = " ".join(
            [description_lower, requirements_lower]
        )
        requirements_description_lower = " ".join(
            [requirements_lower, description_lower]
        )
        title_description_requirements_lower = " ".join(
            [title_lower, description_lower, requirements_lower]
        )
        title_requirements_description_lower = " ".join(
            [title_lower, requirements_lower, description_lower]
        )
        title_description_lower = " ".join([title_lower, description_lower])
        description_requirements_compensation_lower = " ".join(
            [description_lower, requirements_lower, compensation_lower]
        )

        return cls(
            title=title,
            description=description,
            requirements=requirements,
            compensation=compensation,
            title_lower=title_lower,
            description_lower=description_lower,
            requirements_lower=requirements_lower,
            compensation_lower=compensation_lower,
            description_requirements=description_requirements,
            description_requirements_lower=description_requirements_lower,
            requirements_description=requirements_description,
            requirements_description_lower=requirements_description_lower,
            title_description_requirements=title_description_requirements,
            title_description_requirements_lower=(
                title_description_requirements_lower
            ),
            title_requirements_description=title_requirements_description,
            title_requirements_description_lower=(
                title_requirements_description_lower
            ),
            title_description=title_description,
            title_description_lower=title_description_lower,
            description_requirements_compensation=(
                description_requirements_compensation
            ),
            description_requirements_compensation_lower=(
                description_requirements_compensation_lower
            ),
            title_description_requirements_compensation=(
                title + " " + description_requirements_compensation
            ),
            description_requirements_title=(
                description_requirements + " " + title
            ),
            requirements_technology_matches=tuple(
                _count_tech_tools_lower(requirements_lower)
            ),
            full_technology_matches=tuple(
                _count_tech_tools_lower(description_requirements_lower)
            ),
            matched_profile_skills=(
                profile_skill_matcher.match(
                    title_requirements_description_lower
                )
                if profile_skill_matcher is not None
                else ()
            ),
        )


def detect_red_flags(
    row,
    comp,
    role_cls,
    company_cls,
    *,
    analysis_context: PostingAnalysisContext | None = None,
):
    text = (
        analysis_context.description_requirements_compensation
        if analysis_context is not None
        else " ".join(
            [
                row.get("description", ""),
                row.get("requirements", ""),
                row.get("compensation", ""),
            ]
        )
    )
    flags = []

    scam_fee_match = SCAM_FEE.search(text)
    if scam_fee_match:
        flags.append(_flag("scam_fee", "Asks applicants to pay a fee", "critical", scam_fee_match.group(0)))
    no_interview_match = NO_INTERVIEW.search(text)
    if no_interview_match:
        flags.append(_flag("no_interview", "\u201cNo interview / immediate hire\u201d hiring", "major", no_interview_match.group(0)))
    offplatform_match = OFFPLATFORM.search(text)
    if offplatform_match:
        flags.append(_flag("offplatform_recruiting", "Recruiting via WhatsApp/Telegram", "major", offplatform_match.group(0)))

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

    founder_hits = []
    for pattern in FOUNDER_PHRASES:
        match = re.search(pattern, text, re.I)
        if match:
            founder_hits.append(match.group(0))
    if len(founder_hits) >= 2:
        flags.append(_flag(
            "founder_responsibilities",
            "Founder-level responsibilities pitched as an internship",
            "major", "; ".join(founder_hits[:3]),
        ))

    tools = (
        list(analysis_context.requirements_technology_matches)
        if analysis_context is not None
        else count_tech_tools(row.get("requirements", ""))
    )
    if len(tools) > 10:
        flags.append(_flag("laundry_list", f"Requirements list {len(tools)} technologies", "minor", ", ".join(tools[:12])))

    excessive_hours_match = NIGHTS_WEEKENDS.search(text)
    if excessive_hours_match:
        flags.append(_flag("excessive_hours", "Excessive hours expected", "major", excessive_hours_match.group(0)))

    grunt_text = (
        analysis_context.title_description_requirements_compensation
        if analysis_context is not None
        else row.get("title", "") + " " + text
    )
    grunt_hits = []
    for pattern in GRUNT_WORK:
        match = re.search(pattern, grunt_text, re.I)
        if match:
            grunt_hits.append(match.group(0))
    has_learning = bool(LEARNING_WORDS.search(text))
    if grunt_hits and not has_learning:
        flags.append(_flag("grunt_work", "Busywork with no stated learning component", "major", "; ".join(grunt_hits[:3])))
    elif row.get("description") and not has_learning and role_cls["role"] in ("non_technical", "it", "unknown"):
        flags.append(_flag("no_learning_mention", "No mention of mentorship or learning", "minor", "description"))

    return flags


def detect_positive_signals(
    row,
    comp,
    role_cls,
    company_cls,
    profile,
    known,
    *,
    analysis_context: PostingAnalysisContext | None = None,
):
    text = (
        analysis_context.description_requirements
        if analysis_context is not None
        else " ".join(
            [row.get("description", ""), row.get("requirements", "")]
        )
    )
    title = (
        analysis_context.title
        if analysis_context is not None
        else row.get("title", "")
    )
    signals = []
    role_track = role_cls.get("role_track") or role_cls.get("role")
    is_software_track = role_track in SOFTWARE_SIGNAL_TRACKS

    mid = hourly_mid(comp)
    if comp["kind"] == "paid" and mid is not None:
        if mid >= 30:
            signals.append(_signal("paid_well", f"Strong pay (~${mid:.0f}/hr equivalent)", 3, comp["raw"]))
        elif mid >= profile.get("min_acceptable_hourly_usd", 15):
            signals.append(_signal("paid", f"Paid (~${mid:.0f}/hr equivalent)", 2, comp["raw"]))

    matched = matched_skills(
        row,
        profile,
        analysis_context=analysis_context,
    )
    if is_software_track and len(matched) >= 2:
        signals.append(_signal("stack_match", "Tech stack overlaps your experience", 3, ", ".join(matched[:6])))
    elif is_software_track and len(matched) == 1:
        signals.append(_signal("stack_match", "Some stack overlap with your experience", 1, matched[0]))

    ownership_match = OWNERSHIP.search(text)
    if ownership_match:
        signals.append(_signal("ownership", "Clear project ownership", 2, ownership_match.group(0)))
    learning_match = LEARNING_WORDS.search(text)
    if learning_match:
        signals.append(_signal("mentorship", "Mentorship / learning emphasized", 2, learning_match.group(0)))
    conversion_match = CONVERSION.search(text)
    if conversion_match:
        signals.append(_signal("conversion", "Return-offer / full-time pipeline", 2, conversion_match.group(0)))
    structured_text = (
        analysis_context.description_requirements_title
        if analysis_context is not None
        else text + " " + title
    )
    structured_match = STRUCTURED.search(structured_text)
    if structured_match:
        signals.append(_signal("structured_program", "Structured internship program", 1, structured_match.group(0)))

    name_norm = norm_company(row.get("company", ""))
    if name_norm in known.get("reputable", set()):
        signals.append(_signal("reputable", "Well-known employer", 2, row.get("company", "")))

    tools = (
        list(analysis_context.full_technology_matches)
        if analysis_context is not None
        else count_tech_tools(
            row.get("requirements", "")
            + " "
            + row.get("description", "")
        )
    )
    if is_software_track and len(tools) >= 3:
        signals.append(_signal("specific_tech", "Names a concrete technical stack", 2, ", ".join(tools[:6])))

    backend_text = (
        analysis_context.title_description_requirements
        if analysis_context is not None
        else title + " " + text
    )
    backend_match = BACKEND_TERMS.search(backend_text)
    if backend_match and role_track in {
        "backend", "full_stack", "general_swe", "platform_infra", "data_engineering", "ml_ai", "quant_dev"
    }:
        signals.append(_signal("backend_focus", "Backend / data infrastructure focus", 2, backend_match.group(0)))

    if company_cls.get("is_startup"):
        signals.append(_signal("startup_env", "Startup environment (matches your interest)", 1, "; ".join(company_cls.get("evidence", [])[:1])))

    return signals


def matched_skills(
    row,
    profile,
    *,
    analysis_context: PostingAnalysisContext | None = None,
    profile_skill_matcher: ProfileSkillMatcher | None = None,
):
    if analysis_context is not None:
        return list(analysis_context.matched_profile_skills)
    blob = " ".join(
        [
            row.get("title", ""),
            row.get("requirements", ""),
            row.get("description", ""),
        ]
    ).lower()
    matcher = profile_skill_matcher or build_profile_skill_matcher(profile)
    return list(matcher.match(blob))


def profile_match(
    row,
    role_cls,
    profile,
    *,
    analysis_context: PostingAnalysisContext | None = None,
):
    skills = matched_skills(
        row,
        profile,
        analysis_context=analysis_context,
    )
    blob = (
        analysis_context.title_description_lower
        if analysis_context is not None
        else " ".join(
            [row.get("title", ""), row.get("description", "")]
        ).lower()
    )
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
