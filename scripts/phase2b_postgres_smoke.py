"""Phase 2B smoke test: real snapshot import into a disposable PostgreSQL database.

Creates a throwaway database, migrates it to head, builds controlled users from
companies actually present in the imported snapshot, and verifies per-user
matching, ownership isolation, reconciliation, save/dismiss durability, and
second-import idempotency. Only the disposable database is dropped; no watcher
state is written and no mail is delivered.

Example:
    python scripts/phase2b_postgres_smoke.py \\
        --admin-url postgresql://postgres@127.0.0.1:55433/postgres \\
        --snapshot watcher/collection-snapshots/collection-replay-benchmark-v2.json.gz
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from collections import Counter
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import psycopg
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from app.hosted.catalog import CompanyCatalog
from app.hosted.database import HostedDatabase, normalize_database_url
from app.hosted.job_import import JobImportService
from app.hosted.job_mapper import map_final_jobs
from app.hosted.mailer import InMemoryMailer
from app.hosted.models import (
    AuthenticationSession,
    HostedJob,
    HostedJobImportRun,
    User,
    UserCompanyWatch,
    UserJobMatch,
    UserPreference,
)
from app.hosted.security import hash_password, new_token, token_hash
from app.hosted.services import HostedServices, utc_now
from app.hosted.settings import HostedSettings
from app.hosted.snapshot_jobs import replay_snapshot_jobs, snapshot_sha256
from app.main import app

BACKEND_DIR = REPO_ROOT / "backend"
# Durable watcher state that this task must never create or modify.
WATCHER_STATE_PATHS = (
    REPO_ROOT / "watcher" / "seen.sqlite",
    REPO_ROOT / "watcher" / "analysis-cache.sqlite",
    REPO_ROOT / "watcher-data",
)

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{f' — {detail}' if detail else ''}")
    if not condition:
        failures.append(label)


def fingerprint_state() -> dict[str, str]:
    """Hash durable watcher state so the run can prove it stayed untouched."""

    result: dict[str, str] = {}
    for path in WATCHER_STATE_PATHS:
        if not path.exists():
            result[path.name] = "absent"
        elif path.is_dir():
            entries = sorted(
                f"{item.relative_to(path)}:{item.stat().st_size}"
                for item in path.rglob("*")
                if item.is_file()
            )
            result[path.name] = hashlib.sha256(
                "\n".join(entries).encode("utf-8")
            ).hexdigest()
        else:
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def psycopg_url(url: str) -> str:
    return normalize_database_url(url).replace("postgresql+psycopg://", "postgresql://", 1)


def create_database(admin_url: str, name: str) -> str:
    parsed = make_url(normalize_database_url(admin_url))
    with psycopg.connect(
        psycopg_url(parsed.set(database="postgres").render_as_string(hide_password=False)),
        autocommit=True,
    ) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return parsed.set(database=name).render_as_string(hide_password=False)


def drop_database(admin_url: str, name: str) -> None:
    parsed = make_url(normalize_database_url(admin_url))
    with psycopg.connect(
        psycopg_url(parsed.set(database="postgres").render_as_string(hide_password=False)),
        autocommit=True,
    ) as connection:
        connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def seed_user(db, email: str, *, role_ids, locations, include_remote, season, watches):
    now = utc_now()
    user = User(
        email=email,
        normalized_email=email.casefold(),
        password_hash=hash_password("smoke test password"),
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.flush()
    db.add(
        UserPreference(
            user_id=user.id,
            role_ids=list(role_ids),
            preferred_locations=list(locations),
            include_remote=include_remote,
            internship_season=season,
            alert_frequency="as_detected",
            globally_paused=False,
            created_at=now,
            updated_at=now,
        )
    )
    for company_id in watches:
        db.add(
            UserCompanyWatch(
                user_id=user.id,
                company_id=company_id,
                paused=False,
                created_at=now,
                updated_at=now,
            )
        )
    raw = new_token()
    db.add(
        AuthenticationSession(
            user_id=user.id,
            token_hash=token_hash(raw),
            created_at=now,
            expires_at=now.replace(year=now.year + 1),
            last_used_at=now,
        )
    )
    return user.id, raw


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-url", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--watchlist", default=str(REPO_ROOT / "watcher" / "watchlist.yml"))
    parser.add_argument("--expected-sha256", default="")
    args = parser.parse_args(argv)

    snapshot = Path(args.snapshot)
    print("== Phase 2B PostgreSQL smoke test ==")
    digest = snapshot_sha256(snapshot)
    print(f"snapshot: {snapshot.name}\nsha256:   {digest}")
    if args.expected_sha256:
        check("snapshot sha256 matches", digest == args.expected_sha256.strip())

    state_before = fingerprint_state()

    print("\n-- replay snapshot (network-free, no state writes) --")
    replayed = replay_snapshot_jobs(
        snapshot,
        watchlist_path=args.watchlist,
        allow_collection_config_mismatch=True,
    )
    catalog = CompanyCatalog.from_watcher_config(replayed.config)
    mapped = map_final_jobs(replayed.jobs, catalog)
    print(f"analyzed jobs: {len(replayed.jobs)}   importable: {len(mapped.jobs)}")
    if not mapped.jobs:
        print("FAIL: snapshot produced no importable jobs")
        return 1

    # Build user preferences from what the snapshot actually contains.
    open_jobs = [job for job in mapped.jobs if job.is_open]
    by_company = Counter(job.company_id for job in open_jobs)
    by_role = Counter(job.role_id for job in open_jobs)
    print(f"open importable jobs: {len(open_jobs)}")
    print(f"top companies: {by_company.most_common(5)}")
    print(f"roles: {by_role.most_common()}")
    if len(by_company) < 2 or not by_role:
        print("FAIL: snapshot lacks enough company/role variety")
        return 1

    top_company, _ = by_company.most_common(1)[0]
    top_role, _ = by_role.most_common(1)[0]
    # A company/role pair that genuinely exists, plus a disjoint role.
    pair = next(job for job in open_jobs if job.company_id == top_company)
    other_role = next(
        (role for role in by_role if role != pair.role_id),
        pair.role_id,
    )
    second_company = next(
        (company for company in by_company if company != top_company),
        top_company,
    )
    print(
        f"controlled setup: company={top_company} role={pair.role_id} "
        f"other_role={other_role} second_company={second_company}"
    )

    database_name = f"phase2b_smoke_{uuid.uuid4().hex}"
    database_url = create_database(args.admin_url, database_name)
    database = None
    previous_services = app.state.hosted_services
    try:
        print("\n-- migrate empty database to head --")
        alembic = Config(str(BACKEND_DIR / "alembic.ini"))
        alembic.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        command.upgrade(alembic, "head")
        command.check(alembic)

        database = HostedDatabase(database_url)
        with database.engine.connect() as connection:
            revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        check("migrated to Phase 2B head", revision == "20260803_0003", revision)

        mailer = InMemoryMailer()
        services = HostedServices.build(
            settings=replace(HostedSettings.from_env(), database_url=database_url),
            mailer=mailer,
            catalog=catalog,
        )
        app.state.hosted_services = services

        print("\n-- create three controlled users --")
        with services.database.session_factory.begin() as db:
            focused_id, focused_token = seed_user(
                db,
                "focused@example.com",
                role_ids=[pair.role_id],
                locations=[],
                include_remote=True,
                season="Any season",
                watches=[top_company],
            )
            role_miss_id, role_miss_token = seed_user(
                db,
                "rolemiss@example.com",
                role_ids=[other_role] if other_role != pair.role_id else ["product_management"],
                locations=[],
                include_remote=True,
                season="Any season",
                watches=[top_company],
            )
            other_company_id, other_company_token = seed_user(
                db,
                "othercompany@example.com",
                role_ids=[pair.role_id],
                locations=[],
                include_remote=True,
                season="Any season",
                watches=[second_company],
            )
        print(f"users: focused={focused_id} role_miss={role_miss_id} other={other_company_id}")

        print("\n-- first import --")
        service = JobImportService(services.database, catalog)
        first = service.import_jobs(
            replayed.jobs,
            source_fingerprint=replayed.source_fingerprint,
            source_identifier=replayed.source_identifier,
            source_type="collection_snapshot",
        )
        counters = first.counters
        print(
            f"outcome={first.outcome} received={counters.jobs_received} "
            f"inserted={counters.jobs_inserted} updated={counters.jobs_updated} "
            f"unchanged={counters.jobs_unchanged} skipped={counters.jobs_skipped} "
            f"matches_created={counters.matches_created}"
        )
        check("first import succeeded", first.outcome == "imported")

        with services.database.session_factory() as db:
            total_matches = db.scalar(select(func.count()).select_from(UserJobMatch))
            per_user = dict(
                db.execute(
                    select(UserJobMatch.user_id, func.count())
                    .group_by(UserJobMatch.user_id)
                ).all()
            )
            duplicates = db.execute(
                select(UserJobMatch.user_id, UserJobMatch.job_id, func.count())
                .group_by(UserJobMatch.user_id, UserJobMatch.job_id)
                .having(func.count() > 1)
            ).all()
        check(
            "matches_created equals inserted match rows",
            counters.matches_created == total_matches,
            f"{counters.matches_created} == {total_matches}",
        )
        check("no duplicate (user_id, job_id) rows", not duplicates)
        check("matches_created is nonnegative", counters.matches_created >= 0)
        focused_count = per_user.get(focused_id, 0)
        role_miss_count = per_user.get(role_miss_id, 0)
        other_count = per_user.get(other_company_id, 0)
        print(
            f"per-user matches: focused={focused_count} "
            f"role_miss={role_miss_count} other_company={other_count}"
        )
        check("watching user received matches", focused_count > 0)
        check(
            "users receive different results",
            len({focused_count, role_miss_count, other_count}) > 1,
        )

        print("\n-- API ownership isolation --")
        with TestClient(app) as anonymous:
            check("unauthenticated list is rejected", anonymous.get("/api/matches").status_code == 401)

        cookie_name = services.settings.session_cookie_name
        with TestClient(app) as focused_client, TestClient(app) as other_client:
            focused_client.cookies.set(cookie_name, focused_token)
            other_client.cookies.set(cookie_name, other_company_token)
            focused_page = focused_client.get("/api/matches?limit=50").json()
            check("focused user lists own matches", focused_page["total"] == focused_count)
            check("list is bounded", len(focused_page["items"]) <= 50)
            check("invalid view rejected", focused_client.get("/api/matches?view=bogus").status_code == 400)
            check("oversized limit rejected", focused_client.get("/api/matches?limit=500").status_code == 422)

            if focused_page["items"]:
                target = focused_page["items"][0]["id"]
                check(
                    "owner reads own match",
                    focused_client.get(f"/api/matches/{target}").status_code == 200,
                )
                check(
                    "other user cannot read it (404)",
                    other_client.get(f"/api/matches/{target}").status_code == 404,
                )
                check(
                    "other user cannot patch it (404)",
                    other_client.patch(
                        f"/api/matches/{target}", json={"saved": True}
                    ).status_code
                    == 404,
                )
                check(
                    "ownership-scoped patch rejects job fields",
                    focused_client.patch(
                        f"/api/matches/{target}", json={"title": "x"}
                    ).status_code
                    == 422,
                )

                print("\n-- save and dismiss --")
                saved = focused_client.patch(
                    f"/api/matches/{target}", json={"saved": True}
                ).json()
                check("save persisted", saved["saved_at"] is not None)
                second_target = (
                    focused_page["items"][1]["id"]
                    if len(focused_page["items"]) > 1
                    else target
                )
                dismissed = focused_client.patch(
                    f"/api/matches/{second_target}", json={"dismissed": True}
                ).json()
                check("dismiss persisted", dismissed["dismissed_at"] is not None)

                print("\n-- preference and watch reconciliation --")
                unwatch = focused_client.put("/api/watchlist", json={"companies": []})
                check("watchlist cleared", unwatch.status_code == 200)
                check(
                    "matches deactivate after unwatching",
                    focused_client.get("/api/matches").json()["total"] == 0,
                )
                check(
                    "history is retained",
                    focused_client.get("/api/matches?view=historical").json()["total"] > 0,
                )
                rewatch = focused_client.put(
                    "/api/watchlist",
                    json={"companies": [{"company_id": top_company, "paused": False}]},
                )
                check("watchlist restored", rewatch.status_code == 200)
                restored_total = focused_client.get("/api/matches").json()["total"]
                check(
                    "matches reactivate after rewatching",
                    restored_total == focused_count - 1,
                    f"{restored_total} active, one dismissed",
                )
                check(
                    "saved survived reconciliation",
                    focused_client.get("/api/matches?view=saved").json()["total"] >= 1,
                )
                check(
                    "dismissed survived reconciliation",
                    focused_client.get("/api/matches?view=dismissed").json()["total"] >= 1,
                )

        with services.database.session_factory() as db:
            before_rows = {
                row.id: (
                    row.matched_at,
                    row.last_matched_at,
                    row.no_longer_matches_at,
                    row.saved_at,
                    row.dismissed_at,
                    row.updated_at,
                )
                for row in db.scalars(select(UserJobMatch))
            }
            job_count_before = db.scalar(select(func.count()).select_from(HostedJob))

        print("\n-- second import of the same snapshot --")
        second = service.import_jobs(
            replayed.jobs,
            source_fingerprint=replayed.source_fingerprint,
            source_identifier=replayed.source_identifier,
            source_type="collection_snapshot",
        )
        print(
            f"outcome={second.outcome} matches_created={second.counters.matches_created}"
        )
        check("second import is already_imported", second.already_imported)
        check(
            "second import reports the first import's counter",
            second.counters.matches_created == counters.matches_created,
        )

        with services.database.session_factory() as db:
            after_rows = {
                row.id: (
                    row.matched_at,
                    row.last_matched_at,
                    row.no_longer_matches_at,
                    row.saved_at,
                    row.dismissed_at,
                    row.updated_at,
                )
                for row in db.scalars(select(UserJobMatch))
            }
            job_count_after = db.scalar(select(func.count()).select_from(HostedJob))
            run_count = db.scalar(select(func.count()).select_from(HostedJobImportRun))
        check("already_imported changed no match rows or timestamps", before_rows == after_rows)
        check("already_imported changed no jobs", job_count_before == job_count_after)
        check("only one import run exists", run_count == 1)

        check("no email delivered", not mailer.messages, f"{len(mailer.messages)} messages")

    finally:
        app.state.hosted_services = previous_services
        if database is not None:
            database.dispose()
        drop_database(args.admin_url, database_name)
        print(f"\ndropped disposable database {database_name}")

    state_after = fingerprint_state()
    check("watcher state unchanged", state_before == state_after)
    print(f"watcher state: {state_after}")

    print("\n== summary ==")
    if failures:
        for failure in failures:
            print(f"FAILED: {failure}")
        return 1
    print("all smoke-test checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
