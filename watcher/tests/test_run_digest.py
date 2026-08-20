"""End-to-end synthetic digests rendered from a complete watcher run."""

from datetime import date

from watcher.config import CompanyCfg, WatcherConfig
from watcher.notify import render_digest
from watcher.run import run_once
from watcher.seen_store import SeenStore
from watcher.tests.run_helpers import (
    FakeDigestSender,
    FakeGithub,
    FakeSource,
    row,
)


def test_synthetic_digest_excludes_non_swe_engineering_and_ranks_backend_java(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(name="Bosch", ats="greenhouse", token="bosch"),
            CompanyCfg(name="HackerRank", ats="greenhouse", token="hackerrank"),
            CompanyCfg(name="Anduril Industries", ats="greenhouse", token="anduril"),
        )
    )
    direct_rows = {
        "Bosch": [
            row(
                "Bosch",
                "IT Internship (BackEnd, Java)",
                description="Build BackEnd services and REST APIs in Java.",
                requirements="Java, SQL, Git",
            ),
            row(
                "Bosch",
                "Cloud Developer Internship",
                description="Build cloud APIs and platform services in Python.",
                requirements="AWS, Python, Docker",
            ),
            row(
                "Bosch",
                "DevOps Engineering Intern",
                description="Own developer tooling and automation code for backend infrastructure APIs.",
                requirements="Python, Docker, Linux",
            ),
            row(
                "Bosch",
                "Mechanical Design Engineer",
                description="Design mechanical components for manufacturing.",
                requirements="CAD, fixtures, manufacturing",
            ),
            row(
                "Bosch",
                "Factory Automation Engineering Intern",
                description="Support PLCs and plant automation equipment.",
                requirements="PLC, manufacturing, electrical systems",
            ),
        ],
        "HackerRank": [
            row(
                "HackerRank",
                "Customer Experience Engineer - Intern",
                description="Help customers troubleshoot issues and answer support tickets.",
                requirements="Customer support, SQL",
            ),
        ],
        "Anduril Industries": [
            row(
                "Anduril Industries",
                "2027 Electrical Engineer Intern",
                description="Design and test electrical hardware.",
                requirements="Circuits, PCB, lab equipment",
            ),
            row(
                "Anduril Industries",
                "2027 Manufacturing Engineer Intern",
                description="Improve manufacturing processes on the factory floor.",
                requirements="Manufacturing, process engineering",
            ),
            row(
                "Anduril Industries",
                "2027 Software Engineer Intern",
                description="Build backend APIs and production services.",
                requirements="Python, Java, SQL, REST APIs",
            ),
        ],
    }

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource(direct_rows)},
            github_source=FakeGithub([]),
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 6, 9),
        )

    subject, body = render_digest(result.new_matches)

    assert subject == "Internship Watcher: 4 new SWE-intern matches"
    assert "IT Internship (BackEnd, Java)" in body
    assert "Cloud Developer Internship" in body
    assert "DevOps Engineering Intern" in body
    assert "2027 Software Engineer Intern" in body
    for excluded in (
        "Mechanical Design Engineer",
        "Factory Automation Engineering Intern",
        "Customer Experience Engineer - Intern",
        "2027 Electrical Engineer Intern",
        "2027 Manufacturing Engineer Intern",
    ):
        assert excluded not in body

    assert body.index("IT Internship (BackEnd, Java)") < body.index("Cloud Developer Internship")
    assert body.index("IT Internship (BackEnd, Java)") < body.index("DevOps Engineering Intern")


def test_synthetic_digest_excludes_graduate_roles_and_attaches_alumni(tmp_path):
    config = WatcherConfig(
        companies=(
            CompanyCfg(
                name="Bosch",
                ats="greenhouse",
                token="bosch",
                aliases=("Bosch Group",),
                alumni_match=("bosch group",),
            ),
            CompanyCfg(
                name="Tesla",
                ats="greenhouse",
                token="tesla",
                aliases=("Tesla Motors",),
                alumni_match=("tesla", "tesla motors"),
            ),
            CompanyCfg(name="ResearchCo", ats="greenhouse", token="researchco"),
            CompanyCfg(name="UndergradCo", ats="greenhouse", token="undergradco"),
        )
    )
    direct_rows = {
        "Bosch": [
            row(
                "Bosch",
                "IT Internship (BackEnd, Java)",
                description="Build BackEnd services and REST APIs in Java.",
                requirements="Java, SQL, Git",
            ),
            row(
                "Bosch",
                "Machine Learning Engineer PhD Intern",
                description="Build Python ML services and data pipelines.",
                requirements="Python, SQL, Pandas",
            ),
        ],
        "Tesla": [
            row(
                "Tesla",
                "Fullstack Software Engineer Intern",
                description="Build full-stack web apps with React, TypeScript, Python APIs, and SQL.",
                requirements="React, TypeScript, Python, SQL, GitHub",
            ),
            row(
                "Tesla",
                "Software Engineer Intern - Masters",
                description="Build Python backend APIs with SQL.",
                requirements="Python, SQL, REST APIs",
            ),
        ],
        "ResearchCo": [
            row(
                "ResearchCo",
                "Graduate Research Intern",
                description="Research software systems.",
                requirements="Python, SQL",
            ),
            row(
                "ResearchCo",
                "Postdoctoral Research Intern",
                description="Research ML systems.",
                requirements="Python, SQL",
            ),
        ],
        "UndergradCo": [
            row(
                "UndergradCo",
                "Undergraduate Software Engineer Intern",
                description="Build Python backend services and REST APIs.",
                requirements="Python, SQL, REST APIs, Git",
            ),
        ],
    }
    alumni_index = {
        "bosch group": [{
            "name": "Ada Bosch",
            "occupation": "Backend Engineer",
            "linkedin_url": "https://www.linkedin.com/in/fake-bosch",
            "employer": "Bosch Group",
        }],
        "tesla": [{
            "name": "Nikola Tesla",
            "occupation": "Software Engineer",
            "linkedin_url": "https://www.linkedin.com/in/fake-tesla",
            "employer": "Tesla",
        }],
    }

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource(direct_rows)},
            github_source=FakeGithub([]),
            alumni_index=alumni_index,
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 6, 9),
        )

    subject, body = render_digest(
        result.new_matches,
        alumni_summary={
            "status": result.alumni_csv_status,
            "records_loaded": result.alumni_records_loaded,
            "employers_indexed": result.alumni_employers_indexed,
        },
    )

    assert subject == "Internship Watcher: 3 new SWE-intern matches"
    assert "IT Internship (BackEnd, Java)" in body
    assert "Fullstack Software Engineer Intern" in body
    assert "Undergraduate Software Engineer Intern" in body
    assert "Ada Bosch - Backend Engineer - https://www.linkedin.com/in/fake-bosch" in body
    assert "Nikola Tesla - Software Engineer - https://www.linkedin.com/in/fake-tesla" in body
    assert "Alumni index: 2 records across 2 employers" in body
    for excluded in (
        "Machine Learning Engineer PhD Intern",
        "Software Engineer Intern - Masters",
        "Graduate Research Intern",
        "Postdoctoral Research Intern",
    ):
        assert excluded not in body


def test_mixed_digest_reserves_above_94_fit_for_near_perfect_resume_matches(tmp_path):
    config = WatcherConfig(companies=(CompanyCfg(name="FitCo", ats="greenhouse", token="fitco"),))
    direct_rows = {
        "FitCo": [
            row(
                "FitCo",
                "Backend Engineer Intern",
                description="Build Python FastAPI REST APIs with SQLAlchemy and PostgreSQL.",
                requirements="Python, FastAPI, SQLAlchemy, SQL, PostgreSQL, GitHub, Pytest",
            ),
            row(
                "FitCo",
                "Full Stack Engineer Intern",
                description="Build full-stack web apps with React, TypeScript, Next.js, Python APIs and SQL.",
                requirements="React, TypeScript, Next.js, Python, SQL, GitHub",
            ),
            row(
                "FitCo",
                "Data Engineer Intern",
                description="Build Python data ingestion pipelines and data analytics apps with Pandas.",
                requirements="Python, SQL, Pandas, Pytest",
            ),
            row(
                "FitCo",
                "Backend Java Intern",
                description="Build backend REST APIs and database-backed services.",
                requirements="Java, SQL, Git",
            ),
            row(
                "FitCo",
                "Cloud Developer Internship",
                description="Build cloud APIs and platform services in Python.",
                requirements="AWS, Python, Docker",
            ),
            row(
                "FitCo",
                "Software Engineer Intern",
                description="Build simulation infrastructure.",
                requirements="Rust, Go, C++",
            ),
        ]
    }

    with SeenStore(tmp_path / "seen.sqlite") as store:
        result = run_once(
            config,
            seen_store=store,
            direct_sources={"greenhouse": FakeSource(direct_rows)},
            github_source=FakeGithub([]),
            digest_sender=FakeDigestSender(sent=False),
            today=date(2026, 6, 9),
        )

    subject, body = render_digest(result.new_matches)
    high_fit_titles = {
        job["title"]
        for job in result.new_matches
        if job["score"]["fit_score"] > 94
    }

    assert subject == "Internship Watcher: 6 new SWE-intern matches"
    assert high_fit_titles == {
        "Backend Engineer Intern",
        "Full Stack Engineer Intern",
    }
    assert body.index("Backend Engineer Intern") < body.index("Backend Java Intern")
    assert body.index("Backend Java Intern") < body.index("Cloud Developer Internship")
