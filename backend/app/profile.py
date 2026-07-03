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
        "python", "java", "sql", "javascript", "typescript", "fastapi",
        "flask", "sqlalchemy", "next.js", "react", "pandas", "openai api",
        "git", "github", "postgres", "postgresql", "sqlite", "rest",
        "rest api", "restful apis", "api", "backend apis",
        "data ingestion", "etl", "data analytics", "data analysis",
        "spreadsheet data apps", "market data pipelines",
        "full-stack web apps", "pytest", "testing", "evals",
        "numpy", "scikit-learn", "machine learning", "docker", "linux",
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
    "role_track_affinity": {
        "backend": 82,
        "full_stack": 80,
        "data_engineering": 78,
        "ml_ai": 76,
        "general_swe": 74,
        "platform_infra": 72,
        "quant_dev": 72,
        "frontend": 68,
        "cloud": 62,
        "devops": 58,
        "embedded_software": 55,
        "firmware": 50,
        "sdet_qa_automation": 50,
        "it_support": 20,
        "quality_test": 20,
        "solutions_engineering": 20,
        "product": 0,
        "customer_experience": 0,
        "electrical_hardware": 0,
        "mechanical_manufacturing": 0,
        "civil_structural": 0,
        "factory_automation": 0,
        "other_engineering": 0,
        "non_technical": 0,
        "unknown": 0,
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
            if k in {"role_affinity", "role_track_affinity"} and isinstance(v, dict):
                merged = dict(DEFAULT_PROFILE[k])
                merged.update(v)
                profile[k] = merged
            else:
                profile[k] = v
    return profile
