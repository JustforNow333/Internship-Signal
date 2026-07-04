"""Deterministic natural-language query engine for "Ask the Dataset".

No LLM is called anywhere in this module. Questions are interpreted with
intent/keyword matching (`interpret`) and answered by filtering + ranking the
already-scored job dicts (`run_plan`). The two-step split is deliberate: a
real LLM could later replace `interpret` while everything downstream stays
identical. See the LLM INTEGRATION POINT at the bottom of this file.
"""

import re

MAX_RESULTS = 10

EXAMPLE_QUESTIONS = [
    "Which postings are best for backend experience?",
    "Show paid data science internships only",
    "Which ones look exploitative?",
    "Which companies seem like actual startups?",
    "Which ones should I apply to tonight?",
]

# ---------------------------------------------------------------------------
# Interpretation (the part an LLM could replace)
# ---------------------------------------------------------------------------

_INTENTS = [
    ("exploitative", re.compile(r"exploit|sketchy|scam|red.?flag|avoid|shady|suspicious|predatory", re.I)),
    ("apply_tonight", re.compile(r"tonight|apply now|asap|right away|immediately|urgent|due soon|closing soon", re.I)),
    ("startups", re.compile(r"start.?ups?\b", re.I)),
]

# Order matters: first regex that hits wins. "backend" is checked before the
# generic software/engineering patterns so it can carry its special handling.
_ROLE_TRIGGERS = [
    ("backend", re.compile(r"\bback.?end\b|\bapis?\b|\bserver.?side\b", re.I)),
    ("quant", re.compile(r"\bquant(itative)?\b|\btrading\b", re.I)),
    ("ml_ai", re.compile(r"\bml\b|machine learning|\ba\.?i\.?\b|artificial intelligence|deep learning", re.I)),
    ("data_science", re.compile(r"data scien|data analy|\bdata\b", re.I)),
    ("swe", re.compile(r"\bsoftware\b|\bswe\b|\bengineering\b|\bdevelop(er|ment)\b|full.?stack", re.I)),
    ("product", re.compile(r"\bproduct\b|\bpm\b", re.I)),
    ("it", re.compile(r"\bit support\b|\bhelp.?desk\b", re.I)),
]

_BEST_RE = re.compile(r"\bbest\b|\btop\b|\bgood\b|\brecommend|\bstrongest\b|\bshow\b|\blist\b|\bwhich\b|\bfind\b", re.I)
_PAID_RE = re.compile(r"\bpaid\b", re.I)          # \b stops this matching inside "unpaid"
_UNPAID_RE = re.compile(r"\bunpaid\b|\bfree labor\b", re.I)
_REMOTE_RE = re.compile(r"\bremote\b|work from home|\bwfh\b", re.I)

BACKEND_ADJACENT_TRACKS = {"backend", "full_stack", "platform_infra", "data_engineering"}

_STOPWORDS = {
    "the", "a", "an", "which", "what", "who", "show", "me", "ones", "one", "are",
    "is", "do", "does", "should", "i", "to", "for", "of", "in", "on", "only",
    "internships", "internship", "postings", "posting", "jobs", "job", "roles",
    "role", "companies", "company", "look", "like", "seem", "actual", "best",
    "good", "any", "all", "list", "find", "with", "that", "and", "or", "my",
}


def interpret(question: str) -> dict:
    """Turn a free-text question into a structured QueryPlan dict.

    This is the deterministic stand-in for an LLM. The QueryPlan contract:
      intent: exploitative | apply_tonight | startups | best_for_role | keyword | help
      role / want_backend: optional role filter
      paid_only / unpaid_only / remote_only: optional modifiers
      keywords: fallback search terms
    """
    q = (question or "").strip()
    plan = {
        "question": q,
        "intent": None,
        "role": None,
        "want_backend": False,
        "paid_only": bool(_PAID_RE.search(q)),
        "unpaid_only": bool(_UNPAID_RE.search(q)),
        "remote_only": bool(_REMOTE_RE.search(q)),
        "keywords": [],
    }
    if not q:
        plan["intent"] = "help"
        return plan

    for intent, rx in _INTENTS:
        if rx.search(q):
            plan["intent"] = intent
            break

    for role, rx in _ROLE_TRIGGERS:
        if rx.search(q):
            if role == "backend":
                plan["role"] = "swe"
                plan["want_backend"] = True
            else:
                plan["role"] = role
            break

    if plan["intent"] is None:
        if plan["role"] or plan["paid_only"] or plan["unpaid_only"] or plan["remote_only"] or _BEST_RE.search(q):
            plan["intent"] = "best_for_role"
        else:
            tokens = [t for t in re.findall(r"[a-z0-9+#.]+", q.lower()) if t not in _STOPWORDS]
            plan["keywords"] = tokens
            plan["intent"] = "keyword" if tokens else "help"
    return plan


# ---------------------------------------------------------------------------
# Plan execution (stays the same even with an LLM interpreter)
# ---------------------------------------------------------------------------

def _is_paid(job):
    return job["compensation"]["kind"] in ("paid", "stipend_unspecified")


def _is_remote(job):
    return (job.get("remote_status") or "").lower() == "remote"


def _flag_counts(job):
    crit = sum(1 for f in job["red_flags"] if f["severity"] == "critical")
    major = sum(1 for f in job["red_flags"] if f["severity"] == "major")
    return crit, major


def _has_signal(job, sid):
    return any(s["id"] == sid for s in job["positive_signals"])


def _role_matches(job, plan):
    if not plan["role"]:
        return True
    rc = job["role_classification"]["role"]
    if plan["want_backend"]:
        role_track = job["role_classification"].get("role_track")
        return role_track in BACKEND_ADJACENT_TRACKS or _has_signal(job, "backend_focus")
    return rc == plan["role"]


def _apply_modifiers(jobs, plan, filters_applied):
    out = jobs
    if plan["paid_only"]:
        out = [j for j in out if _is_paid(j)]
        filters_applied.append("paid roles only")
    if plan["unpaid_only"]:
        out = [j for j in out if j["compensation"]["kind"] == "unpaid"]
        filters_applied.append("unpaid roles only")
    if plan["remote_only"]:
        out = [j for j in out if _is_remote(j)]
        filters_applied.append("remote only")
    if plan["role"]:
        out = [j for j in out if _role_matches(j, plan)]
        label = "backend-adjacent software/data roles" if plan["want_backend"] else plan["role"].replace("_", " ")
        filters_applied.append(f"role: {label}")
    return out


def _result(job, headline):
    return {
        "id": job["id"],
        "company": job["company"],
        "title": job["title"],
        "score": job["score"]["total"],
        "action_label": job["score"]["action_label"],
        "headline_reason": headline,
    }


def _first_reason(job):
    reasons = job["score"].get("reasons") or []
    return reasons[0] if reasons else job["score"]["action_label"]


def run_plan(plan: dict, jobs: list) -> dict:
    filters_applied: list = []
    intent = plan["intent"]

    if intent == "help":
        return {
            "question": plan["question"],
            "interpretation": "I couldn't map that to anything in the dataset.",
            "filters_applied": [],
            "results": [],
            "summary_text": "Try one of the example questions — I answer with keyword rules, not magic.",
            "examples": EXAMPLE_QUESTIONS,
            "llm_note": _LLM_NOTE,
        }

    pool = _apply_modifiers(jobs, plan, filters_applied)

    if intent == "exploitative":
        cands = [j for j in pool if any(f["severity"] in ("critical", "major") for f in j["red_flags"])]
        cands.sort(key=lambda j: (-_flag_counts(j)[0], -_flag_counts(j)[1], j["score"]["total"]))
        results = []
        for j in cands[:MAX_RESULTS]:
            tops = [f["label"] for f in j["red_flags"] if f["severity"] in ("critical", "major")][:2]
            results.append(_result(j, "; ".join(tops)))
        interp = "Looking for postings with major or critical red flags (scams, unpaid work, founder-dumping, etc.)."
        summary = (
            f"{len(cands)} of {len(pool)} postings have at least one major or critical red flag."
            if cands else "Good news — nothing in the current filter set has a major or critical red flag."
        )

    elif intent == "apply_tonight":
        def urgent(j):
            d = j["score"].get("deadline_days_left")
            return j["score"]["action"] == "apply_now" or (d is not None and 0 <= d <= 7 and j["score"]["total"] >= 55)

        cands = [j for j in pool if urgent(j)]
        cands.sort(key=lambda j: (
            j["score"]["deadline_days_left"] if j["score"]["deadline_days_left"] is not None else 999,
            -j["score"]["total"],
        ))
        results = []
        for j in cands[:MAX_RESULTS]:
            d = j["score"].get("deadline_days_left")
            when = f"deadline in {d} day{'s' if d != 1 else ''}" if d is not None else "no hard deadline"
            results.append(_result(j, f"{when} — {_first_reason(j)}"))
        interp = "Finding strong postings that are urgent: recommended 'apply now' or closing within 7 days."
        summary = (
            f"{len(cands)} posting{'s' if len(cands) != 1 else ''} worth applying to tonight, sorted by deadline."
            if cands else "Nothing is both strong and urgent right now — nothing expires in the next week."
        )

    elif intent == "startups":
        def startupish(j):
            cc = j["company_classification"]
            return cc.get("is_startup") or _has_signal(j, "startup_env")

        cands = [j for j in pool if startupish(j)]
        seen, grouped = set(), []
        for j in sorted(cands, key=lambda x: -x["score"]["total"]):
            key = (j["company"] or "").strip().lower()
            if key in seen:
                continue
            seen.add(key)
            ev = j["company_classification"].get("evidence") or []
            startup_ev = next((e for e in ev if "startup" in e.lower() or "team" in e.lower() or "seed" in e.lower()), None)
            grouped.append(_result(j, startup_ev or "Startup language in the posting"))
        results = grouped[:MAX_RESULTS]
        interp = "Finding employers that look like actual startups (size/funding/startup language, not just the name)."
        summary = (
            f"{len(grouped)} distinct compan{'ies' if len(grouped) != 1 else 'y'} show startup evidence."
            if grouped else "No posting in the current filter set shows real startup evidence."
        )

    elif intent == "keyword":
        kws = plan["keywords"]
        def hits(j):
            blob = " ".join([
                j.get("company", ""), j.get("title", ""),
                j.get("description", ""), j.get("requirements", ""),
            ]).lower()
            return sum(1 for k in kws if k in blob)

        scored = [(hits(j), j) for j in pool]
        cands = [j for h, j in sorted(scored, key=lambda t: (-t[0], -t[1]["score"]["total"])) if h > 0]
        results = [_result(j, _first_reason(j)) for j in cands[:MAX_RESULTS]]
        filters_applied.append("keyword match: " + ", ".join(kws))
        interp = f"No clear intent detected — falling back to keyword search for: {', '.join(kws)}."
        summary = (
            f"{len(cands)} posting{'s' if len(cands) != 1 else ''} mention those terms."
            if cands else "No posting mentions those terms. Try one of the example questions."
        )

    else:  # best_for_role (also the generic "show me X" path)
        def rank_key(j):
            cats = j["score"]["categories"]
            base = cats["role_relevance"]["score"] + cats["technical_depth"]["score"] + 0.5 * j["score"]["total"]
            if plan["want_backend"] and _has_signal(j, "backend_focus"):
                base += 15
            return -base

        cands = sorted(pool, key=rank_key)
        results = [_result(j, _first_reason(j)) for j in cands[:MAX_RESULTS]]
        what = "backend experience" if plan["want_backend"] else (
            plan["role"].replace("_", " ") + " roles" if plan["role"] else "your profile overall"
        )
        interp = f"Ranking postings by fit for {what} (role relevance + technical depth + overall score)."
        summary = (
            f"Top {min(len(cands), MAX_RESULTS)} of {len(cands)} matching postings, best fit first."
            if cands else "Nothing matches those filters in this dataset."
        )

    return {
        "question": plan["question"],
        "interpretation": interp,
        "filters_applied": filters_applied,
        "results": results,
        "summary_text": summary,
        "llm_note": _LLM_NOTE,
    }


def ask(question: str, jobs: list) -> dict:
    """Public entry point: deterministic interpret -> run."""
    return run_plan(interpret(question), jobs)


# === LLM INTEGRATION POINT ==================================================
# To upgrade this feature with a real model, replace interpret() with a call
# that asks the LLM to emit the same QueryPlan dict, e.g.:
#
#   plan = llm_json(f"Translate this question into a QueryPlan: {question}",
#                   schema=QUERY_PLAN_SCHEMA)
#   return run_plan(plan, jobs)
#
# Keeping run_plan() as the executor keeps answers grounded in the dataset
# (the model only chooses filters/ranking; it never invents postings).

_LLM_NOTE = (
    "Answered with deterministic keyword rules — no LLM involved. "
    "An LLM could replace the question-interpretation step (see ask.py)."
)


def ask_with_llm(question: str, jobs: list) -> dict:  # pragma: no cover
    """Placeholder showing where an LLM-backed interpreter would plug in."""
    raise NotImplementedError(
        "Wire an LLM here: have it translate `question` into the QueryPlan dict "
        "that interpret() produces, then reuse run_plan() unchanged."
    )
