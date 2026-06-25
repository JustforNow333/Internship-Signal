"""The candidate profile that postings are matched against.

The default mirrors the brief: Cornell CS student with backend/data
experience. Override any field by editing data/profile.json (path is
configurable via PROFILE_PATH).
"""

from . import config

DEFAULT_PROFILE = {
    "name": "Cornell CS student",
    "school": "Cornell University",
    "skills": [
        "python", "flask", "sqlalchemy", "rest api", "api", "sql",
        "postgres", "postgresql", "mysql", "data ingestion", "etl",
        "pandas", "numpy", "scikit-learn", "machine learning",
        "data analysis", "git", "docker", "linux",
    ],
    "interests": ["backend", "data science", "ml/ai", "quant", "startup engineering"],
    # Base role-relevance score (0-100) per classified role.
    "role_affinity": {
        "swe": 90,
        "data_science": 90,
        "ml_ai": 95,
        "quant": 85,
        "product": 55,
        "it": 35,
        "non_technical": 10,
        "unknown": 40,
    },
    # Lowercase substrings matched against the posting location.
    "preferred_locations": ["ithaca", "new york", "nyc", "remote", "boston"],
    "min_acceptable_hourly_usd": 15,
    "goal": "Real technical experience — not unpaid busywork.",
}


def load_profile() -> dict:
    data = config._load_json(config.PROFILE_PATH, {})
    profile = dict(DEFAULT_PROFILE)
    if isinstance(data, dict):
        for k, v in data.items():
            if k == "role_affinity" and isinstance(v, dict):
                merged = dict(DEFAULT_PROFILE["role_affinity"])
                merged.update(v)
                profile[k] = merged
            else:
                profile[k] = v
    return profile
