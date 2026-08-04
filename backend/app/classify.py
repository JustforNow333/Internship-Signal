"""Company and role classification.

Company classification is deliberately layered (per the brief): employer
names alone are unreliable — "Meridian" can be an 8-person logistics-tech
startup. The layers, in order of trust:

  1. Configurable known-company lists (data/known_companies.json).
  2. Strong tech tokens in the name (Technologies, Software, AI, Labs...).
  3. Tech-stack and startup evidence in the description/requirements.
  4. A technical title prevents a non-tech verdict from name evidence alone.

Every result carries a confidence score and the evidence used, so the UI
can show *why* a verdict was reached.
"""

import re

from .config import load_known_companies
from .dedupe import norm_company, norm_title

# ---------------------------------------------------------------------------
# Company classification
# ---------------------------------------------------------------------------

NAME_TECH_TOKENS = [
    "technolog", "software", "data", "cloud", "system", "labs", "lab",
    "platform", "digital", "analytics", "cyber", "robotic", "tech",
    "network", "compute", "intelligence", "quant", "soft", "byte", "dev",
]

_AI_NAME = re.compile(r"(\b[Aa][Ii]\b|(?<=[a-z])AI\b|\.ai\b)")

TECH_CONTEXT_TERMS = [
    r"\bapis?\b", r"\bpython\b", r"\bjava\b", r"\bgolang\b", r"\brust\b",
    r"\breact\b", r"\bnode(\.js)?\b", r"\bkubernetes\b", r"\bdocker\b",
    r"\baws\b", r"\bgcp\b", r"\bazure\b", r"\bsql\b", r"\bpostgres(ql)?\b",
    r"machine learning", r"\bsaas\b", r"\bplatform\b", r"infrastructure",
    r"\bbackend\b", r"\bfront[- ]?end\b", r"\bsdk\b", r"open[- ]source",
    r"\bllms?\b", r"data pipeline", r"\betl\b", r"microservice",
    r"\bdevops\b", r"ci/cd", r"\bterraform\b", r"\bsoftware\b", r"\balgorithms?\b",
]

STARTUP_TERMS = [
    r"\bseed([- ]funded| round| stage)?\b", r"series [ab]\b", r"pre[- ]seed",
    r"\by ?combinator\b", r"\byc[- ]backed\b", r"\bfounding\b", r"early[- ]stage",
    r"\bstartup\b", r"\bstealth\b",
]
_TEAM_SIZE = re.compile(r"(\d{1,3})[- ]person team", re.I)

NON_TECH_TERMS = [
    r"\bbakery\b", r"\brestaurant\b", r"\bretail\b", r"\bboutique\b",
    r"\bsalon\b", r"staffing (agency|firm)", r"marketing agency",
    r"\blaw firm\b", r"real estate", r"\bhospitality\b", r"non[- ]?profit",
    r"senior living", r"\bcatering\b", r"event planning", r"\bwellness\b",
    r"property management", r"insurance agency",
]


def _count_matches(patterns, text):
    hits = []
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            hits.append(m.group(0).strip())
    return hits


def classify_company(
    row: dict,
    known: dict | None = None,
    role_is_technical: bool = False,
    *,
    analysis_context=None,
) -> dict:
    known = known or load_known_companies()
    name = row.get("company", "")
    name_norm = norm_company(name)
    context = (
        analysis_context.description_requirements_title
        if analysis_context is not None
        else " ".join(
            [
                row.get("description", ""),
                row.get("requirements", ""),
                row.get("title", ""),
            ]
        )
    )

    evidence = []
    startup_hits = _count_matches(STARTUP_TERMS, context)
    team = _TEAM_SIZE.search(context)
    if team and int(team.group(1)) <= 50:
        startup_hits.append(team.group(0))
    tech_hits = _count_matches(TECH_CONTEXT_TERMS, context)
    non_tech_hits = _count_matches(NON_TECH_TERMS, context + " " + name)

    def finish(category, confidence, extra=None):
        if extra:
            evidence.append(extra)
        return {
            "category": category,
            "confidence": round(min(confidence, 0.97), 2),
            "evidence": evidence,
            "is_startup": category == "startup",
        }

    # Layer 1: configurable known lists.
    if name_norm in known["tech"]:
        evidence.append(f'"{name}" is on the known tech-company list.')
        if startup_hits:
            evidence.append("Startup language in posting: " + ", ".join(startup_hits[:3]))
            return finish("startup", 0.95)
        return finish("tech", 0.95)
    if name_norm in known["non_tech"]:
        return finish("non_tech", 0.9, f'"{name}" is on the known non-tech list.')

    # Layer 2: strong tech tokens in the employer name.
    name_words = re.split(r"[^a-z0-9.]+", name.lower())
    token_hit = next(
        (tok for tok in NAME_TECH_TOKENS for w in name_words if w.startswith(tok)),
        None,
    )
    ai_hit = bool(_AI_NAME.search(name))
    if token_hit or ai_hit:
        label = "AI" if ai_hit and not token_hit else token_hit
        evidence.append(f'Tech indicator in company name ("{label}").')
        conf = 0.7 + (0.1 if len(tech_hits) >= 2 else 0)
        if startup_hits:
            evidence.append("Startup language: " + ", ".join(startup_hits[:3]))
            return finish("startup", conf)
        if tech_hits:
            evidence.append("Technical context: " + ", ".join(tech_hits[:4]))
        return finish("tech", conf)

    # Layer 3: description/context evidence for ambiguous names.
    if len(tech_hits) >= 3:
        evidence.append("Ambiguous name, but the posting is clearly technical: " + ", ".join(tech_hits[:5]))
        if startup_hits:
            evidence.append("Startup language: " + ", ".join(startup_hits[:3]))
            return finish("startup", 0.62 + 0.03 * min(len(tech_hits), 6))
        return finish("tech", 0.55 + 0.04 * min(len(tech_hits), 8))

    if len(non_tech_hits) >= 1 and len(tech_hits) <= 1 and not role_is_technical:
        evidence.append("Non-tech context: " + ", ".join(non_tech_hits[:3]))
        return finish("non_tech", 0.55 + 0.05 * min(len(non_tech_hits), 5))

    # Layer 3.5: explicit startup language counts even without a heavy tech
    # stack in the posting ("Series A startup", "8-person team", ...).
    if startup_hits and not non_tech_hits:
        evidence.append("Startup language: " + ", ".join(startup_hits[:3]))
        if tech_hits:
            evidence.append("Technical context: " + ", ".join(tech_hits[:4]))
        return finish("startup", 0.5 + 0.05 * min(len(startup_hits) + len(tech_hits), 6))

    # Layer 4: technical title blocks a non-tech verdict; stay unknown.
    if role_is_technical:
        return finish(
            "unknown", 0.3,
            "Employer is ambiguous, but the role itself is technical — kept for review rather than ruled out.",
        )
    return finish("unknown", 0.25, "Not enough evidence to classify this employer.")


# ---------------------------------------------------------------------------
# Role classification
# ---------------------------------------------------------------------------

ROLE_LABELS = {
    "swe": "Software engineering",
    "data_science": "Data science / analytics",
    "ml_ai": "ML / AI",
    "product": "Product",
    "quant": "Quant",
    "it": "IT",
    "non_technical": "Non-technical",
    "unknown": "Unknown",
}

ROLE_TRACK_LABELS = {
    "backend": "Backend software",
    "full_stack": "Full-stack software",
    "frontend": "Frontend software",
    "general_swe": "Software engineering",
    "platform_infra": "Platform / infrastructure software",
    "data_engineering": "Data engineering / analytics",
    "ml_ai": "ML / AI engineering",
    "quant_dev": "Quant development",
    "devops": "DevOps / developer tooling",
    "cloud": "Cloud software",
    "embedded_software": "Embedded software",
    "firmware": "Firmware",
    "sdet_qa_automation": "SDET / QA automation",
    "technical_product": "Technical product",
    "it_support": "IT support",
    "customer_experience": "Customer experience engineering",
    "solutions_engineering": "Solutions engineering",
    "electrical_hardware": "Electrical / hardware engineering",
    "mechanical_manufacturing": "Mechanical / manufacturing engineering",
    "civil_structural": "Civil / structural engineering",
    "quality_test": "Quality / test engineering",
    "factory_automation": "Factory automation engineering",
    "other_engineering": "Other engineering",
    "product": "Product",
    "non_technical": "Non-technical",
    "unknown": "Unknown",
}

SOFTWARE_ROLE_TRACKS = {
    "backend",
    "full_stack",
    "frontend",
    "general_swe",
    "platform_infra",
    "data_engineering",
    "ml_ai",
    "quant_dev",
    "devops",
    "cloud",
    "embedded_software",
    "firmware",
    "sdet_qa_automation",
    "technical_product",
}

LOW_PRIORITY_WATCHER_TRACKS = {"it_support", "quality_test", "solutions_engineering"}

ROLE_TRACK_TO_ROLE = {
    "backend": "swe",
    "full_stack": "swe",
    "frontend": "swe",
    "general_swe": "swe",
    "platform_infra": "swe",
    "devops": "swe",
    "cloud": "swe",
    "embedded_software": "swe",
    "firmware": "swe",
    "sdet_qa_automation": "swe",
    "technical_product": "product",
    "data_engineering": "data_science",
    "ml_ai": "ml_ai",
    "quant_dev": "quant",
    "product": "product",
    "it_support": "it",
    "non_technical": "non_technical",
}

TECHNICAL_ROLES = {"swe", "data_science", "ml_ai", "quant"}

CAPITAL_ONE_COMPANY_NAMES = frozenset(
    {
        norm_company("Capital One"),
        norm_company("Capital One Financial"),
    }
)
CAPITAL_ONE_GENERAL_SWE_TITLES = frozenset(
    {
        norm_title("Technology Intern"),
        norm_title("Technology Internship Program"),
        norm_title("Technology Internship Program Intern"),
    }
)
CAPITAL_ONE_SUMMER_PROGRAM_TITLE = re.compile(
    r"technology internship program summer [0-9]{4}"
)

SOFTWARE_TITLE_PATTERNS = [
    ("backend", r"\bback[- ]?end\b|\bserver[- ]side\b"),
    ("full_stack", r"\bfull[- ]?stack\b"),
    ("frontend", r"\bfront[- ]?end\b|\bui engineer\b|\bweb developer\b"),
    ("platform_infra", r"\bplatform software\b|\binfrastructure software\b|\bsite reliability\b|\bsre\b"),
    ("data_engineering", r"\bdata engineer(ing)?\b|\bdata science\b|\bdata analy(st|tics)\b|\bdata infrastructure\b|\bdata pipeline"),
    ("ml_ai", r"machine[- ]learning (engineer|engineering|intern)|\bml\b|\bml engineer\b|\bai engineer\b|\ba\.?\s*i\.?\s+integration\b|\bai[- ]integration\b|deep learning|computer vision|\bnlp\b|\bllms?\b|\bpytorch\b|\btensorflow\b"),
    ("quant_dev", r"\bquant(itative)? (developer|engineer|analyst|trading|research)|\bquantitative trading intern\b|\btrading intern\b|\bsystematic (investing|trading|research)\b|\bmarket model(l)?ing\b"),
    ("embedded_software", r"\bembedded software\b"),
    ("firmware", r"\bfirmware\b"),
    ("sdet_qa_automation", r"\bsdet\b|software qa automation|qa automation|software test automation|test automation framework"),
    ("technical_product", r"\bassociate product manager\b|\btechnical product manage(r|ment)\b|\bapm intern(ship)?\b|\bproduct development internship program\b"),
    ("general_swe", r"\bsoftware (engineer|engineering|developer|development)\b|\bswe\b|\bdeveloper intern\b|\bfounding engineer\b"),
    ("cloud", r"\bcloud developer\b|\bcloud software\b"),
    ("devops", r"\bdevops\b|developer tooling|build engineer"),
]

NON_SWE_TITLE_PATTERNS = [
    ("customer_experience", r"customer experience engineer|customer support engineer|technical support engineer"),
    ("solutions_engineering", r"solutions? engineer|sales engineer|forward deployed engineer|\bdigital solutions?\b|\bworkflow automation\b|\bintelligent systems?\b"),
    ("it_support", r"\bit (support|intern(ship)?)\b|help[- ]?desk|desktop support|sysadmin|systems administrator|network administrator"),
    ("electrical_hardware", r"electrical engineer|hardware engineer|\brf engineer|fpga engineer|pcb|circuit"),
    ("mechanical_manufacturing", r"naval architect|marine engineer|mechanical engineer|mechanical design engineer|manufacturing engineer|industrial engineer|industrial design engineer|physical product design|product design engineer|process engineer|aerospace engineer"),
    ("civil_structural", r"civil engineer|structural engineer"),
    ("quality_test", r"quality engineer|test engineer|validation engineer|verification engineer"),
    ("factory_automation", r"factory automation engineer|automation engineer|plc|plant automation|manufacturing automation"),
    ("product", r"product manage(r|ment)|\bpm intern\b|\bproduct intern\b|product development (co[- ]?op|intern)"),
    ("non_technical", r"commercial (co[- ]?op|intern(ship)?)"),
    ("non_technical", r"consumer insights?|consumer research|market research|survey research|data entry|\bmarketing\b|\bsales\b|business development|\bhr\b|human resources|recruit(ing|er)|social media|\bcontent\b|\bbrand\b|administrative|operations intern|accounting|activities intern|cold[- ]call|copywrit"),
]

BACKEND_CONTEXT_RE = re.compile(
    r"\bback[- ]?end\b|\bserver[- ]side\b|\bapis?\b|\brest(ful)?\b|"
    r"\bmicroservices?\b|\bservices?\b|\bdistributed systems?\b|"
    r"\bdatabases?\b|\bsql\b|\bpostgres(ql)?\b|\bmysql\b|\bspring\b",
    re.I,
)
SOFTWARE_CONTEXT_RE = re.compile(
    r"\bsoftware\b|\bdeveloper\b|\bweb app\b|\bapis?\b|\brest(ful)?\b|"
    r"\bmicroservices?\b|\bdistributed systems?\b|\bproduction code\b|"
    r"\bcode review\b|\bbuild(ing)? services?\b|\bprogramming\b|\bcoding\b|"
    r"\bplatform software\b|\binfrastructure software\b|\bdata engineer(ing)?\b|"
    r"\bml engineer\b|machine learning|\bsdet\b|qa automation|software test automation|"
    r"\bembedded software\b|\bfirmware\b",
    re.I,
)
MANUFACTURING_CONTEXT_RE = re.compile(r"manufactur|factory|plant|plc|mechanical|electrical|hardware|industrial|process", re.I)
UMBRELLA_PROGRAM_TITLE_RE = re.compile(
    r"\b(?:summer )?internship program\b|\b(?:intern|summer) rotational program\b|\ball tracks\b",
    re.I,
)
TECHNICAL_PROGRAM_TRACK_RE = re.compile(
    r"\b(?:tracks?|functions?|pathways?|business areas?)\b.{0,180}"
    r"\b(?:technology|software|engineering|data|analytics|quantitative|risk technology)\b|"
    r"\b(?:technology|software|engineering|data|analytics|quantitative|risk technology)\b"
    r".{0,180}\b(?:tracks?|functions?|pathways?|business areas?)\b",
    re.I,
)

# Bounded title variants recovered from the Phase 2A invalid-role audit. These
# phrases name a concrete technical discipline or activity; generic words such
# as "engineering", "technology", "data", "AI", and "systems" remain
# insufficient on their own.
SCOPED_ROLE_TITLE_PATTERNS = [
    (
        "full_stack",
        r"\bjava web develop(?:er|ing)\b|entwicklung (?:einer )?web[- ]app",
    ),
    (
        "general_swe",
        r"\bit[- ]entwicklung\b|datenanalyse und softwaremonitoring",
    ),
    ("devops", r"\bpowershell (?:development |automation )?intern(?:ship)?\b"),
    (
        "sdet_qa_automation",
        r"\bautomation tester\b.*\bselenium\b|"
        r"\bselenium\b.*\bautomation test",
    ),
    (
        "ml_ai",
        r"\b(?:intern(?:ship)?\W{0,12}ai research|"
        r"ai research\W{0,12}intern(?:ship)?)\b",
    ),
    (
        "ml_ai",
        r"\bdata and ai intern(?:ship)?\b|"
        r"\bai (?:&|and) automation development\b",
    ),
    (
        "ml_ai",
        r"d[ée]veloppeur ia et automatisation|data et intelligence artificielle",
    ),
    (
        "ml_ai",
        r"\bai(?:[- ]based)? (?:hvac )?research\b|\bai[- ]based arc detection\b",
    ),
    (
        "ml_ai",
        r"agentic ai|agentischer ki|generative ki|\bki[- ]entwicklung\b|"
        r"\bdata\s*&\s*ai solutions\b",
    ),
    ("ml_ai", r"world[- ]action model|\bvla\b.{0,50}autonomous driving"),
    ("electrical_hardware", r"\bhardware penetration testing\b"),
    ("quant_dev", r"\bquant and portfolio analytics\b"),
    (
        "technical_product",
        r"\bit product business analyst\s*/\s*product owner assistant\b",
    ),
    (
        "it_support",
        r"\bit (?:global |infrastructure )?support intern(?:ship)?\b|"
        r"\bit admin intern(?:ship)?\b",
    ),
    (
        "mechanical_manufacturing",
        r"\binternship\b.{0,100}\bengineering in manufacturing\b",
    ),
    (
        "mechanical_manufacturing",
        r"\binternship\b.{0,50}\bengineering mechanics and hydraulic\b",
    ),
    ("other_engineering", r"\binternship\b.{0,50}\br&d engineering design\b"),
    (
        "other_engineering",
        r"\bintern(?:ship)?\b.{0,70}\btechnical engineering "
        r"(?:responsibility|function)\b",
    ),
    ("other_engineering", r"\bintern,? engineering\b"),
    (
        "general_swe",
        r"\bsoftwareentwicklung\b|\bszoftverfejleszt[őo]\b|"
        r"ingenier[ií]a inform[aá]tica",
    ),
    (
        "electrical_hardware",
        r"measurement technology for consumer sensors|"
        r"hardware[- ]kalibrierung.{0,40}mems|zuverl[aä]ssigkeit von mems|"
        r"fem simulation.{0,60}elektrischer antriebe",
    ),
    (
        "other_engineering",
        r"fertigungsverfahren f[üu]r mems|simulationsmodelle f[üu]r mems|"
        r"versuchsdurchf[üu]hrung und datenanalyse f[üu]r mems",
    ),
    (
        "other_engineering",
        r"\brobotics system development\b|entwicklung robotischer systeme|"
        r"humanoide roboter|\b3d[- ]cfd simulation\b",
    ),
    ("it_support", r"\binternship in information system and technology\b"),
    ("technical_product", r"digital experience product ownership"),
]


def _hits(patterns, text: str) -> list[tuple[str, str]]:
    found = []
    for track, pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            found.append((track, match.group(0).strip()))
    return found


def _software_context_evidence(
    title: str,
    description: str,
    requirements: str,
    *,
    full: str | None = None,
) -> list[str]:
    full = full if full is not None else " ".join(
        [title, description, requirements]
    )
    evidence = []
    for match in SOFTWARE_CONTEXT_RE.finditer(full):
        evidence.append(match.group(0).strip())
        if len(evidence) >= 5:
            break
    if re.search(r"\bjava\b", full, re.I) and BACKEND_CONTEXT_RE.search(full):
        evidence.append("Java with backend/API context")
    if re.search(r"\bpython\b", full, re.I) and re.search(r"back[- ]?end|api|data|machine learning|ml|pipeline|service", full, re.I):
        evidence.append("Python with software/data context")
    if re.search(r"\bsql\b", full, re.I) and re.search(r"back[- ]?end|data engineer|pipeline|database|service", full, re.I):
        evidence.append("SQL with backend/data context")
    if re.search(r"\b(aws|gcp|azure|cloud)\b", full, re.I) and re.search(r"code|api|service|platform|developer|software|automation framework", full, re.I):
        evidence.append("cloud with software/platform context")
    return list(dict.fromkeys(evidence))


def has_strong_software_context(
    title: str,
    description: str = "",
    requirements: str = "",
    *,
    full: str | None = None,
) -> bool:
    """Return true only for explicit software context, not generic tools alone."""

    full = full if full is not None else " ".join(
        [title, description, requirements]
    )
    if SOFTWARE_CONTEXT_RE.search(full):
        return True
    if re.search(r"\b(java|python|sql)\b", full, re.I) and BACKEND_CONTEXT_RE.search(full):
        return True
    if re.search(r"\b(aws|gcp|azure|cloud)\b", full, re.I) and re.search(r"code|api|service|platform|developer|software", full, re.I):
        return True
    return False


def _finish_role(track: str, confidence: float, evidence: list[str], software_evidence: list[str], non_swe_evidence: list[str]) -> dict:
    role = ROLE_TRACK_TO_ROLE.get(track, "unknown")
    return {
        "role": role,
        "label": ROLE_LABELS[role],
        "confidence": round(min(confidence, 0.95), 2),
        "evidence": evidence[:5] or ["No clear role signals in the title or description."],
        "role_track": track,
        "role_track_label": ROLE_TRACK_LABELS[track],
        "software_evidence": software_evidence[:6],
        "non_swe_evidence": non_swe_evidence[:6],
    }


def classify_role(row: dict, *, analysis_context=None) -> dict:
    if analysis_context is None:
        title = row.get("title", "")
        description = row.get("description", "")
        requirements = row.get("requirements", "")
        body = " ".join([description, requirements])
        full = " ".join([title, body])
    else:
        title = analysis_context.title
        description = analysis_context.description
        requirements = analysis_context.requirements
        body = analysis_context.description_requirements
        full = analysis_context.title_description_requirements

    title_software_hits = _hits(SOFTWARE_TITLE_PATTERNS, title)
    body_software_hits = _hits(SOFTWARE_TITLE_PATTERNS, body)
    title_non_swe_hits = _hits(NON_SWE_TITLE_PATTERNS, title)
    body_non_swe_hits = _hits(NON_SWE_TITLE_PATTERNS, body)
    software_evidence = _software_context_evidence(
        title,
        description,
        requirements,
        full=full,
    )
    non_swe_evidence = [f"{track}: {hit}" for track, hit in [*title_non_swe_hits, *body_non_swe_hits]]
    strong_software = has_strong_software_context(
        title,
        description,
        requirements,
        full=full,
    )

    normalized_title = norm_title(title)
    if (
        norm_company(row.get("company", "")) in CAPITAL_ONE_COMPANY_NAMES
        and (
            normalized_title in CAPITAL_ONE_GENERAL_SWE_TITLES
            or CAPITAL_ONE_SUMMER_PROGRAM_TITLE.fullmatch(normalized_title)
        )
    ):
        return _finish_role(
            "general_swe",
            0.82,
            [f'Capital One technology internship title: "{title}"'],
            [title],
            non_swe_evidence,
        )

    # Clerical "data entry" must not be diluted by incidental data words.
    if re.search(r"data entry", title, re.I):
        return _finish_role(
            "non_technical",
            0.9,
            ['title: "data entry"'],
            software_evidence,
            non_swe_evidence or ["data entry"],
        )

    embedded_intern = re.search(r"\bembedded intern(?:ship)?\b", title, re.I)
    if embedded_intern:
        embedded_software = re.search(
            r"\bfirmware\b|\bembedded software\b|\bsoftware development\b|"
            r"\bprogramming\b|\bcoding\b|\bc\+\+\b",
            full,
            re.I,
        )
        track = "embedded_software" if embedded_software else "electrical_hardware"
        return _finish_role(
            track,
            0.78,
            [f'scoped technical title: "{embedded_intern.group(0)}"'],
            software_evidence,
            non_swe_evidence,
        )

    scoped_title_hits = _hits(SCOPED_ROLE_TITLE_PATTERNS, title)
    if scoped_title_hits:
        track, hit = scoped_title_hits[0]
        return _finish_role(
            track,
            0.78,
            [f'scoped technical title: "{hit}"'],
            software_evidence or ([hit] if track in SOFTWARE_ROLE_TRACKS else []),
            non_swe_evidence,
        )

    umbrella_program_match = UMBRELLA_PROGRAM_TITLE_RE.search(title)
    technical_program_match = (
        TECHNICAL_PROGRAM_TRACK_RE.search(body)
        if umbrella_program_match
        else None
    )
    if umbrella_program_match and technical_program_match:
        return _finish_role(
            "general_swe",
            0.64,
            [
                "explicit technical program track: "
                f'"{technical_program_match.group(0).strip()}"'
            ],
            software_evidence or ["explicit technology/analytics/engineering program track"],
            non_swe_evidence,
        )

    if title_software_hits:
        track, hit = title_software_hits[0]
        evidence = [f'title: "{hit}"']
        if body_software_hits:
            evidence.append(f'"{body_software_hits[0][1]}"')
        return _finish_role(track, 0.82 + (0.05 if software_evidence else 0), evidence, software_evidence or [hit], non_swe_evidence)

    if title_non_swe_hits:
        track, hit = title_non_swe_hits[0]
        can_rescue_business_title = track == "product" or (
            track == "non_technical" and re.search(r"commercial (co[- ]?op|intern(ship)?)", title, re.I)
        )
        if can_rescue_business_title and strong_software and re.search(
            r"software|developer|back[- ]?end|front[- ]?end|full[- ]?stack|apis?|production code|programming|coding",
            full,
            re.I,
        ):
            rescue_track = "backend" if BACKEND_CONTEXT_RE.search(full) else "full_stack" if re.search(r"full[- ]?stack|react|typescript|next\.?js", full, re.I) else "general_swe"
            return _finish_role(
                rescue_track,
                0.7,
                [f'non-SWE title signal "{hit}" overridden by clear software duties'],
                software_evidence,
                non_swe_evidence,
            )
        # Non-SWE engineering can be rescued only by explicit software/firmware
        # context, never by "engineer", generic Python/Linux/cloud, or prestige.
        if track == "quality_test" and re.search(r"\bsdet\b|qa automation|software test automation|automated testing framework", full, re.I):
            return _finish_role(
                "sdet_qa_automation",
                0.82,
                [f'title: "{hit}"', "software QA automation context"],
                software_evidence or ["software QA automation"],
                non_swe_evidence,
            )
        if track == "factory_automation" and strong_software and not MANUFACTURING_CONTEXT_RE.search(title):
            return _finish_role(
                "platform_infra",
                0.72,
                [f'title: "{hit}"', "software automation context"],
                software_evidence,
                non_swe_evidence,
            )
        if track in {"factory_automation", "quality_test"}:
            if re.search(r"embedded software|firmware|software engineer|software developer|back[- ]?end|full[- ]?stack|platform software|infrastructure software", full, re.I):
                rescue_track = "firmware" if re.search(r"\bfirmware\b", full, re.I) else "embedded_software" if re.search(r"embedded software", full, re.I) else "general_swe"
                return _finish_role(
                    rescue_track,
                    0.74,
                    [f'non-SWE title signal "{hit}" overridden by explicit software context'],
                    software_evidence,
                    non_swe_evidence,
                )
        return _finish_role(
            track,
            0.82,
            [f'title: "{hit}"'],
            software_evidence,
            non_swe_evidence or [hit],
        )

    if re.search(r"\bcloud\b", title, re.I):
        track = "cloud" if strong_software else "other_engineering"
        evidence = ['title: "cloud"', "software/platform context"] if strong_software else ['title: "cloud" without software ownership context']
        return _finish_role(track, 0.68, evidence, software_evidence, non_swe_evidence)

    if re.search(r"\bdevops\b", title, re.I):
        track = "devops" if strong_software else "other_engineering"
        evidence = ['title: "DevOps"', "developer tooling/software context"] if strong_software else ['title: "DevOps" without software ownership context']
        return _finish_role(track, 0.68, evidence, software_evidence, non_swe_evidence)

    if re.search(r"\bembedded engineer", title, re.I):
        track = "embedded_software" if re.search(r"firmware|embedded software|software|production code|programming|coding", full, re.I) else "electrical_hardware"
        evidence = ['title: "embedded engineer"']
        return _finish_role(track, 0.7, evidence, software_evidence, non_swe_evidence)

    if re.search(r"\b(engineer|engineering intern)\b", title, re.I):
        if strong_software:
            track = "backend" if BACKEND_CONTEXT_RE.search(full) else "general_swe"
            return _finish_role(track, 0.68, ['generic engineer title with strong software context'], software_evidence, non_swe_evidence)
        return _finish_role(
            "other_engineering",
            0.66,
            ['generic engineer title without strong software context'],
            software_evidence,
            non_swe_evidence,
        )

    if body_software_hits or strong_software:
        track = body_software_hits[0][0] if body_software_hits else "general_swe"
        return _finish_role(track, 0.58, [f'"{body_software_hits[0][1]}"'] if body_software_hits else ["strong software context"], software_evidence, non_swe_evidence)

    if body_non_swe_hits:
        track, hit = body_non_swe_hits[0]
        return _finish_role(track, 0.55, [f'"{hit}"'], software_evidence, non_swe_evidence)

    return _finish_role("unknown", 0.2, ["No clear role signals in the title or description."], software_evidence, non_swe_evidence)
