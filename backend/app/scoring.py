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

from .config import BUCKET_THRESHOLDS, SCORE_WEIGHTS
from .normalize import days_until
from .salary import hourly_mid

ACTION_LABELS = {
    "apply_now": "Apply now",
    "apply_later": "Apply later",
    "research_more": "Research more",
    "skip": "Skip",
}


def _clamp(x, lo=0, hi=100):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# Category scorers — each returns (score, explanation)
# ---------------------------------------------------------------------------

def score_role_relevance(role_cls, pmatch, profile):
    base = profile["role_affinity"].get(role_cls["role"], 40)
    bonus = min(len(pmatch["matched_skills"]) * 3, 12)
    score = _clamp(base + bonus)
    expl = f"{role_cls['label']} role (base {base} for your interests)"
    if bonus:
        expl += f", +{bonus} for {len(pmatch['matched_skills'])} matched skills"
    if role_cls["confidence"] < 0.5:
        score = _clamp(score - 10)
        expl += "; low classification confidence (-10)"
    return score, expl + "."


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
    if role_cls["role"] in ("non_technical", "it"):
        score = min(score, 35)
        expl += "; capped — role itself is not engineering-track"
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

    cat = {}
    cat["role_relevance"] = score_role_relevance(role_cls, pmatch, profile)
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

    has_critical = any(f["severity"] == "critical" for f in red_flags)
    majors = sum(1 for f in red_flags if f["severity"] == "major")
    expired = days_left is not None and days_left < 0

    if has_critical:
        total = min(total, BUCKET_THRESHOLDS["maybe"] - 5)
    elif majors >= 3:
        # Three independent major red flags is a pattern, not bad luck.
        total = min(total, BUCKET_THRESHOLDS["maybe"] - 1)

    bucket = "high" if total >= BUCKET_THRESHOLDS["high"] else "maybe" if total >= BUCKET_THRESHOLDS["maybe"] else "low"
    if has_critical or majors >= 3:
        bucket = "low"

    if has_critical:
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
    elif total >= 55:
        action = "apply_later"
    elif total >= BUCKET_THRESHOLDS["maybe"]:
        action = "research_more"
    else:
        action = "skip"

    reasons = _top_reasons(positive, categories)
    concerns = _top_concerns(red_flags, categories, expired)

    explanation = (
        f"Weighted total of eight categories: {total}/100. Strongest: "
        f"{_extreme(categories, best=True)}. Weakest: {_extreme(categories, best=False)}."
        + (" A critical red flag caps this posting in the low bucket." if has_critical
           else " Three or more major red flags cap this posting in the low bucket." if majors >= 3 else "")
    )

    return {
        "total": int(total),
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


def _top_reasons(positive, categories):
    reasons = [s["label"] for s in sorted(positive, key=lambda s: -s["strength"])]
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
