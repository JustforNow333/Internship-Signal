"""Shared fixtures. All pipeline runs pin today=2026-06-09 (the date the
sample CSV's deadlines were written against) so results are deterministic.
"""

from datetime import date
from pathlib import Path

import pytest

from app import config
from app.classify import TECHNICAL_ROLES, classify_company, classify_role
from app.ingest import process_csv
from app.profile import load_profile
from app.salary import parse_compensation
from app.scoring import score_job
from app.signals import count_tech_tools, detect_positive_signals, detect_red_flags, profile_match

TODAY = date(2026, 6, 9)
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample_postings.csv"


@pytest.fixture(scope="session")
def sample_result():
    return process_csv(SAMPLE.read_text(encoding="utf-8"), today=TODAY)


@pytest.fixture(scope="session")
def sample_jobs(sample_result):
    return sample_result["jobs"]


def job_named(jobs, company):
    return next(j for j in jobs if j["company"] == company)


def analyze_row(row: dict, today: date = TODAY) -> dict:
    """Run the full single-row analysis chain, mirroring ingest.process_csv."""
    row = {**{"company": "", "title": "", "location": "", "compensation": "",
              "description": "", "requirements": "", "deadline": ""}, **row}
    profile = load_profile()
    known = config.load_known_companies()
    comp = parse_compensation(row.get("compensation", ""))
    role_cls = classify_role(row)
    company_cls = classify_company(row, known, role_is_technical=role_cls["role"] in TECHNICAL_ROLES)
    red = detect_red_flags(row, comp, role_cls, company_cls)
    pos = detect_positive_signals(row, comp, role_cls, company_cls, profile, known)
    pmatch = profile_match(row, role_cls, profile)
    tools = count_tech_tools(row.get("description", "") + " " + row.get("requirements", ""))
    score = score_job(row, comp, role_cls, company_cls, red, pos, pmatch, profile, tools, today=today)
    return {"comp": comp, "role": role_cls, "company": company_cls,
            "red": red, "pos": pos, "score": score}
