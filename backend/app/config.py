"""Central configuration for Internship Signal.

Everything tunable lives here: scoring weights, currency conversion,
known-company lists, and file paths. Lists can be overridden with JSON
files via environment variables (see .env.example).
"""

import json
import os
from pathlib import Path

from .dedupe import norm_company

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"

SAMPLE_CSV_PATH = Path(os.getenv("SAMPLE_CSV_PATH", DATA_DIR / "sample_postings.csv"))
KNOWN_COMPANIES_PATH = Path(os.getenv("KNOWN_COMPANIES_PATH", DATA_DIR / "known_companies.json"))
PROFILE_PATH = Path(os.getenv("PROFILE_PATH", DATA_DIR / "profile.json"))

CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]

# ---------------------------------------------------------------------------
# Scoring weights (must sum to 1.0 — enforced by a unit test)
# ---------------------------------------------------------------------------
SCORE_WEIGHTS = {
    "role_relevance": 0.40,
    "compensation": 0.14,
    "legitimacy": 0.08,
    "learning_value": 0.14,
    "technical_depth": 0.14,
    "effort_vs_value": 0.04,
    "location_convenience": 0.03,
    "deadline_urgency": 0.03,
}

BUCKET_THRESHOLDS = {"high": 70, "maybe": 45}  # low = anything below "maybe"

# ---------------------------------------------------------------------------
# Compensation normalization
# ---------------------------------------------------------------------------
# Rough FX rates used only to put postings on a comparable USD/hour axis.
# Precision does not matter much here; ordering does.
CURRENCY_TO_USD = {
    "USD": 1.0,
    "INR": 0.012,
    "EUR": 1.08,
    "GBP": 1.27,
    "CAD": 0.73,
}

HOURS_PER_WEEK = 40.0
HOURS_PER_MONTH = 160.0
HOURS_PER_YEAR = 2080.0
HOURS_PER_TERM = 480.0  # assumed 12-week internship at 40 h/wk


def _load_json(path: Path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return fallback


_DEFAULT_KNOWN = {
    "tech": [
        "stripe", "plaid", "ramp", "datadog", "snowflake", "notion", "figma",
        "two sigma", "jane street", "citadel", "hudson river trading",
        "hugging face", "openai", "anthropic", "databricks", "palantir",
        "cloudflare", "twilio", "mongodb", "duolingo", "vercel", "linear",
        "retool", "scale ai", "airbnb", "doordash", "robinhood",
    ],
    "non_tech": [
        "sunrise senior living",
    ],
    # Subset treated as an extra legitimacy signal. For an MVP this mirrors
    # the tech list; a real version would use size/funding data.
    "reputable": [
        "stripe", "plaid", "ramp", "datadog", "snowflake", "notion", "figma",
        "two sigma", "jane street", "citadel", "hudson river trading",
        "hugging face", "openai", "anthropic", "databricks", "palantir",
        "cloudflare", "twilio", "mongodb", "duolingo", "cornell university",
    ],
}


def load_known_companies() -> dict:
    """Known-company lists, overridable via KNOWN_COMPANIES_PATH JSON file.

    File shape: {"tech": [...], "non_tech": [...], "reputable": [...]}
    Names are matched case-insensitively against the normalized company name.
    """
    data = _load_json(KNOWN_COMPANIES_PATH, _DEFAULT_KNOWN)
    if not isinstance(data, dict):
        data = _DEFAULT_KNOWN
    out = {}
    for key in ("tech", "non_tech", "reputable"):
        values = data.get(key, _DEFAULT_KNOWN.get(key, []))
        if not isinstance(values, (list, tuple, set)):
            values = _DEFAULT_KNOWN.get(key, [])
        out[key] = {
            normalized
            for value in values
            if (normalized := norm_company(str(value)))
        }
    return out
