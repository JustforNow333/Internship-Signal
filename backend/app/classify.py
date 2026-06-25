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
from .dedupe import norm_company

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


def classify_company(row: dict, known: dict | None = None, role_is_technical: bool = False) -> dict:
    known = known or load_known_companies()
    name = row.get("company", "")
    name_norm = norm_company(name)
    context = " ".join([row.get("description", ""), row.get("requirements", ""), row.get("title", "")])

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

ROLE_PATTERNS = {
    "quant": [
        (r"\bquant(itative)?\b", 5), (r"\btrading\b", 3), (r"market[- ]mak", 3),
        (r"\balpha (research|signals?)\b", 3), (r"\bderivatives?\b", 2),
    ],
    "ml_ai": [
        (r"machine[- ]learning", 4), (r"\bml\b", 3), (r"deep learning", 4),
        (r"\bnlp\b", 3), (r"computer vision", 3), (r"\bllms?\b", 3),
        (r"\bpytorch\b|\btensorflow\b", 2), (r"\brag\b", 2), (r"fine[- ]tun", 2),
        (r"\bai\b", 2),
    ],
    "data_science": [
        (r"data scien", 5), (r"data analy", 4), (r"data engineer", 4),
        (r"\banalytics\b", 2), (r"\bstatistic", 2), (r"\bpandas\b|scikit|numpy", 2),
        (r"\ba/b test", 2), (r"business intelligence", 2), (r"\bdbt\b", 2),
        (r"\bsql\b", 1), (r"\bairflow\b", 2),
    ],
    "swe": [
        (r"software (engineer|developer|engineering)", 4), (r"\bswe\b", 4),
        (r"\bback[- ]?end\b", 4), (r"\bfront[- ]?end\b", 3), (r"full[- ]stack", 4),
        (r"\bdevops\b", 3), (r"\bmobile\b|\bios\b|\bandroid\b", 2),
        (r"\bdeveloper\b", 3), (r"\bengineering intern\b", 2), (r"\bembedded\b", 3),
        (r"\bfounding engineer\b", 4), (r"\bengineer\b", 2),
        (r"infrastructure|platform engineer|site reliability", 3),
        (r"\bapis?\b", 1), (r"\bdistributed systems?\b", 2),
    ],
    "product": [
        (r"product manage(r|ment)", 5), (r"\bpm intern\b", 4), (r"\bproduct intern\b", 3),
        (r"\broadmap\b", 1), (r"user (stories|interviews)", 1), (r"\bspecs?\b", 1),
    ],
    "it": [
        (r"\bit support\b", 5), (r"help[- ]?desk", 5), (r"\bsysadmin\b|systems? admin", 4),
        (r"network admin", 4), (r"desktop support", 4), (r"\bit intern\b", 4),
        (r"password resets?", 2), (r"\bticketing\b|\btickets\b", 1),
    ],
    "non_technical": [
        (r"data entry", 6), (r"\bmarketing\b", 4), (r"\bsales\b", 4),
        (r"business development", 4), (r"\bhr\b|human resources|recruit(ing|er)", 4),
        (r"social media", 4), (r"\bcontent\b", 2), (r"\bbrand\b", 2),
        (r"administrative|admin(istrative)? assistant", 3),
        (r"(investment|financial) analyst", 3), (r"operations intern", 3),
        (r"\baccounting\b", 3), (r"\bactivities\b", 2), (r"cold[- ]call", 3),
        (r"\bcopywrit", 3),
    ],
}

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

TECHNICAL_ROLES = {"swe", "data_science", "ml_ai", "quant"}
_TIEBREAK = ["quant", "ml_ai", "data_science", "swe", "product", "it", "non_technical"]


def classify_role(row: dict) -> dict:
    title = row.get("title", "")
    body = " ".join([row.get("requirements", ""), row.get("description", "")])

    scores = {}
    evidence = {}
    for role, patterns in ROLE_PATTERNS.items():
        total = 0
        hits = []
        for pat, weight in patterns:
            if re.search(pat, title, re.I):
                total += weight * 3
                hits.append(f'title: "{re.search(pat, title, re.I).group(0)}"')
            elif re.search(pat, body, re.I):
                total += weight
                hits.append(f'"{re.search(pat, body, re.I).group(0)}"')
        scores[role] = total
        evidence[role] = hits

    # "Data entry" is clerical work, not data science — a strong title hit
    # for it should not be diluted by an incidental "data" elsewhere.
    if re.search(r"data entry", title, re.I):
        scores["data_science"] = 0

    best = max(_TIEBREAK, key=lambda r: (scores[r], -_TIEBREAK.index(r)))
    if scores[best] < 3:
        return {"role": "unknown", "label": ROLE_LABELS["unknown"], "confidence": 0.2,
                "evidence": ["No clear role signals in the title or description."]}

    confidence = round(min(0.95, 0.3 + 0.05 * scores[best]), 2)
    return {
        "role": best,
        "label": ROLE_LABELS[best],
        "confidence": confidence,
        "evidence": evidence[best][:5],
    }
