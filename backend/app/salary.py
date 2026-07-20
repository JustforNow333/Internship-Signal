"""Messy compensation string -> structured, comparable data.

Handles: "$25/hr", "$4,000 monthly", "$4k/month", "₹1.5L - ₹2.4L", "80k",
"25-30/hour", "$3,000 for the summer", "unpaid", "equity only",
"competitive", "stipend provided", blanks.

Output is a dict with:
  raw, kind, currency, period, period_assumed, amount_min, amount_max,
  usd_hourly_min, usd_hourly_max, confidence (0-1), notes (list of strings)

kind is one of: paid, unpaid, equity_only, commission_only,
stipend_unspecified, unknown_vague, unknown.

Every assumption (currency, pay period) is written into `notes` and lowers
`confidence`, so the UI can be honest about what was guessed.
"""

import re

from .config import (
    CURRENCY_TO_USD,
    HOURS_PER_MONTH,
    HOURS_PER_TERM,
    HOURS_PER_WEEK,
    HOURS_PER_YEAR,
)

UNPAID_PAT = re.compile(
    r"\b(unpaid|no (pay|compensation|salary|stipend)|volunteer|for (college )?credit|credit[- ]only|academic credit)\b",
    re.I,
)
NEGATED_UNPAID_PAT = re.compile(r"\b(?:not|isn't|is not)\s+(?:an?\s+)?unpaid\b", re.I)
EQUITY_ONLY_PAT = re.compile(r"\bequity([\s-]*(only|based|compensation))?\b", re.I)
EQUITY_ONLY_STRICT = re.compile(r"equity[\s-]*only|only equity|equity in lieu|paid in equity|equity[\s-]*based", re.I)
COMMISSION_PAT = re.compile(r"commission[\s-]*(only|based)|100% commission", re.I)
VAGUE_PAT = re.compile(r"\b(competitive|negotiable|doe|tbd|market rate|commensurate|depends on experience)\b", re.I)
STIPEND_PAT = re.compile(r"\bstipend( provided| available| offered)?\b", re.I)

_MULTIPLIERS = {"k": 1_000.0, "m": 1_000_000.0, "l": 100_000.0, "lakh": 100_000.0, "lakhs": 100_000.0, "lpa": 100_000.0}

_AMOUNT_RE = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*(lpa|lakhs|lakh|k|m|l)?(?![a-z0-9])",
    re.I,
)

_PERIOD_PATTERNS = [
    ("hourly", re.compile(r"/\s*h(ou)?r\b|per\s+hour|hourly|an\s+hour|/\s*h\b", re.I)),
    ("weekly", re.compile(r"/\s*w(ee)?k\b|per\s+week|weekly|a\s+week", re.I)),
    ("monthly", re.compile(r"/\s*mo(nth)?\b|per\s+month|monthly|a\s+month|/\s*m\b", re.I)),
    ("annual", re.compile(r"/\s*y(ea)?r\b|per\s+year|yearly|annual(ly)?|per\s+annum|\bp\.?a\.?\b|\blpa\b", re.I)),
    ("term", re.compile(r"for\s+the\s+(summer|term|semester|program|internship)|\btotal\b|\blump\s*sum\b", re.I)),
]

_HOURS_PER = {
    "hourly": 1.0,
    "weekly": HOURS_PER_WEEK,
    "monthly": HOURS_PER_MONTH,
    "annual": HOURS_PER_YEAR,
    "term": HOURS_PER_TERM,
}


def _detect_currency(s: str):
    if "₹" in s or re.search(r"\b(inr|rs\.?|rupees?)\b", s, re.I):
        return "INR", False
    if "€" in s or re.search(r"\beur\b", s, re.I):
        return "EUR", False
    if "£" in s or re.search(r"\bgbp\b", s, re.I):
        return "GBP", False
    if re.search(r"\bcad\b|c\$", s, re.I):
        return "CAD", False
    if "$" in s or re.search(r"\busd\b", s, re.I):
        return "USD", False
    return "USD", True  # assumed


def _detect_period(s: str):
    for name, pat in _PERIOD_PATTERNS:
        if pat.search(s):
            return name, False
    return None, True


def _extract_amounts(s: str):
    """Return [(value, had_multiplier)] of pay amounts, skipping hour counts.

    "40 hrs/week" must not read as $40; "401(k)" must not read as $401.
    """
    cleaned = re.sub(r"401\s*\(?k\)?", " ", s, flags=re.I)
    amounts = []
    for m in _AMOUNT_RE.finditer(cleaned):
        tail = cleaned[m.end(): m.end() + 12].lower()
        if re.match(r"\s*%", tail):
            continue  # an equity/bonus percentage, not a cash amount
        if re.match(r"\s*(hours?|hrs?)\b", tail) and "$" not in cleaned[max(0, m.start() - 2): m.start()]:
            continue  # an hours-per-week figure, not money
        value = float(m.group(1).replace(",", ""))
        suffix = (m.group(2) or "").lower()
        if suffix:
            value *= _MULTIPLIERS[suffix]
        amounts.append((value, bool(suffix)))
    return amounts


def parse_compensation(raw) -> dict:
    raw = (raw or "").strip()
    result = {
        "raw": raw,
        "kind": "unknown",
        "currency": None,
        "period": None,
        "period_assumed": False,
        "amount_min": None,
        "amount_max": None,
        "usd_hourly_min": None,
        "usd_hourly_max": None,
        "confidence": 0.0,
        "notes": [],
    }
    if not raw:
        result["notes"].append("No compensation listed.")
        return result

    unpaid_text = NEGATED_UNPAID_PAT.sub("", raw)
    if UNPAID_PAT.search(unpaid_text):
        result["kind"] = "unpaid"
        result["usd_hourly_min"] = result["usd_hourly_max"] = 0.0
        result["confidence"] = 0.95
        if re.search(r"credit", raw, re.I):
            result["notes"].append("Offers academic credit instead of pay.")
        return result

    if COMMISSION_PAT.search(raw):
        result["kind"] = "commission_only"
        result["confidence"] = 0.9
        result["notes"].append("Commission-only: no guaranteed pay.")
        return result

    amounts = _extract_amounts(raw)

    if not amounts:
        if EQUITY_ONLY_STRICT.search(raw):
            result["kind"] = "equity_only"
            result["confidence"] = 0.9
            result["notes"].append("Equity only: no cash compensation stated.")
            return result
        if VAGUE_PAT.search(raw):
            result["kind"] = "unknown_vague"
            result["confidence"] = 0.6
            result["notes"].append(f'Vague compensation language: "{raw}".')
            return result
        if STIPEND_PAT.search(raw):
            result["kind"] = "stipend_unspecified"
            result["confidence"] = 0.5
            result["notes"].append("A stipend is mentioned but no amount is given.")
            return result
        if EQUITY_ONLY_PAT.search(raw):
            result["kind"] = "equity_only"
            result["confidence"] = 0.9
            result["notes"].append("Equity only: no cash compensation stated.")
            return result
        result["notes"].append(f'Could not parse "{raw}".')
        return result

    # If equity-only language appears *with* a number it usually still means
    # no cash ("equity only, 0.5%") — treat strict matches as equity_only.
    if EQUITY_ONLY_STRICT.search(raw):
        result["kind"] = "equity_only"
        result["confidence"] = 0.85
        result["notes"].append("Equity only: percentage stated, no cash compensation.")
        return result

    currency, currency_assumed = _detect_currency(raw)
    period, period_assumed = _detect_period(raw)

    values = [v for v, _ in amounts]
    # Share-multiplier heuristic: "$80-90k" parses as (80, None), (90000, k).
    if len(amounts) == 2 and amounts[1][1] and not amounts[0][1] and values[0] < 1000 <= values[1]:
        ratio = values[1] / values[0] if values[0] else 0
        for mult in (1_000.0, 100_000.0):
            if 0.5 <= ratio / mult <= 2.0:
                values[0] *= mult
                result["notes"].append("Assumed both ends of the range share the same unit (e.g. 80-90k).")
                break

    amount_min, amount_max = min(values), max(values)

    if period is None:
        # Infer from magnitude; this is a guess and is labeled as such.
        if currency == "INR" and amount_min >= 100_000:
            # Indian salaries quoted in lakhs without a period almost always
            # follow the LPA (per-annum) convention.
            period = "annual"
        else:
            probe = amount_min * CURRENCY_TO_USD.get(currency, 1.0)
            if probe <= 200:
                period = "hourly"
            elif probe < 10_000:
                period = "monthly"
            else:
                period = "annual"
        period_assumed = True
        result["notes"].append(f"No pay period stated — assumed {period} from the amount's size.")

    if currency_assumed and currency == "USD":
        result["notes"].append("No currency symbol — assumed USD.")

    fx = CURRENCY_TO_USD.get(currency, 1.0)
    hours = _HOURS_PER[period]
    result.update(
        kind="paid",
        currency=currency,
        period=period,
        period_assumed=period_assumed,
        amount_min=amount_min,
        amount_max=amount_max,
        usd_hourly_min=round(amount_min * fx / hours, 2),
        usd_hourly_max=round(amount_max * fx / hours, 2),
    )
    confidence = 0.9
    if period_assumed:
        confidence -= 0.35
    if currency_assumed:
        confidence -= 0.1
    result["confidence"] = round(max(confidence, 0.2), 2)

    if currency == "INR" and period == "annual" and period_assumed:
        result["notes"].append(
            "Lakh amounts without a stated period are read as per-annum (LPA convention); "
            "if this is a monthly stipend the real rate is ~12x higher."
        )
    return result


def hourly_mid(comp: dict):
    lo, hi = comp.get("usd_hourly_min"), comp.get("usd_hourly_max")
    if lo is None or hi is None:
        return None
    return (lo + hi) / 2.0
