"""End-to-end Data Science and Quantitative Trading & Research categories.

A classified job is imported, stored under its hosted role, matched only to
users who selected that role, and notified exactly once. Existing preferences
never gain a category by themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text

from alembic import command
from app.hosted.database import alembic_config_url

from app.hosted.catalog import CompanyCatalog
from app.hosted.job_mapper import map_final_jobs
from app.hosted.models import (
    HostedJob,
    HostedNotificationItem,
    UserJobMatch,
    UserPreference,
)
from tests.test_hosted_notifications import (  # noqa: F401 - pytest fixtures
    MutableClock,
    catalog,
    clean_database,
    clock,
    create_user,
    database,
    final_job,
    import_jobs,
    notification_postgres_url,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]

QUANT_TRADER = {
    "title": "Quantitative Trader Intern",
    "role_classification": {"role": "quant", "role_track": "quant_research"},
}
QUANT_DEVELOPER = {
    "title": "Quantitative Developer Intern",
    "role_classification": {"role": "quant", "role_track": "quant_dev"},
}
DATA_SCIENTIST = {
    "title": "Data Scientist Intern",
    "role_classification": {"role": "data_science", "role_track": "data_science"},
}
DATA_ENGINEER = {
    "title": "Data Engineer Intern",
    "role_classification": {"role": "data_science", "role_track": "data_engineering"},
}


@pytest.mark.parametrize(
    ("overrides", "role_id"),
    [
        (QUANT_TRADER, "quantitative_trading_research"),
        (QUANT_DEVELOPER, "quantitative_development"),
        (DATA_SCIENTIST, "data_science"),
        (DATA_ENGINEER, "data_engineering"),
    ],
)
def test_classifier_tracks_map_to_distinct_hosted_roles(
    catalog: CompanyCatalog, overrides, role_id
) -> None:
    mapped = map_final_jobs([final_job(**overrides)], catalog)

    assert mapped.jobs[0].role_id == role_id


def _select_roles(database, user_id, role_ids):
    with database.session_factory.begin() as db:
        db.get(UserPreference, user_id).role_ids = role_ids


def _matched_titles(database, user_id):
    with database.session_factory() as db:
        return sorted(
            db.scalars(
                select(HostedJob.title)
                .join(UserJobMatch, UserJobMatch.job_id == HostedJob.id)
                .where(UserJobMatch.user_id == user_id)
            )
        )


def _notified_titles(database, user_id):
    with database.session_factory() as db:
        return sorted(
            db.scalars(
                select(HostedJob.title)
                .join(UserJobMatch, UserJobMatch.job_id == HostedJob.id)
                .join(
                    HostedNotificationItem,
                    HostedNotificationItem.user_job_match_id == UserJobMatch.id,
                )
                .where(UserJobMatch.user_id == user_id)
            )
        )


def test_each_category_matches_and_notifies_only_its_subscribers(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        trading = create_user(db, clock(), "trading@example.com")
        developer = create_user(db, clock(), "developer@example.com")
        science = create_user(db, clock(), "science@example.com")
        both = create_user(db, clock(), "both@example.com")
        neither = create_user(db, clock(), "neither@example.com")
    _select_roles(database, trading, ["quantitative_trading_research"])
    _select_roles(database, developer, ["quantitative_development"])
    _select_roles(database, science, ["data_science"])
    _select_roles(
        database, both, ["data_science", "quantitative_trading_research"]
    )
    jobs = [
        final_job(1, **QUANT_TRADER),
        final_job(2, **QUANT_DEVELOPER),
        final_job(3, **DATA_SCIENTIST),
        final_job(4, **DATA_ENGINEER),
    ]

    import_jobs(database, catalog, clock, jobs, "a")

    with database.session_factory() as db:
        roles = dict(db.execute(select(HostedJob.title, HostedJob.role_id)).all())
    assert roles["Quantitative Trader Intern"] == "quantitative_trading_research"
    assert _matched_titles(database, trading) == ["Quantitative Trader Intern"]
    assert _matched_titles(database, developer) == ["Quantitative Developer Intern"]
    assert _matched_titles(database, science) == ["Data Scientist Intern"]
    assert _matched_titles(database, both) == [
        "Data Scientist Intern",
        "Quantitative Trader Intern",
    ]
    assert _matched_titles(database, neither) == []
    assert _notified_titles(database, trading) == ["Quantitative Trader Intern"]
    assert _notified_titles(database, both) == [
        "Data Scientist Intern",
        "Quantitative Trader Intern",
    ]


def test_reimport_and_later_runs_never_duplicate_a_category_alert(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        trading = create_user(db, clock(), "trading@example.com")
    _select_roles(database, trading, ["quantitative_trading_research"])
    job = final_job(1, **QUANT_TRADER)

    first = import_jobs(database, catalog, clock, [job], "a")
    repeated = import_jobs(database, catalog, clock, [job], "a")
    clock.advance(hours=2)
    later = import_jobs(database, catalog, clock, [job], "b")

    assert first.counters.matches_created == 1
    assert repeated.already_imported
    assert later.counters.matches_created == 0
    assert _notified_titles(database, trading) == ["Quantitative Trader Intern"]


def test_existing_preferences_are_not_subscribed_to_new_categories(
    database, catalog, clock
) -> None:
    with database.session_factory.begin() as db:
        legacy = create_user(db, clock(), "legacy@example.com")
    _select_roles(database, legacy, ["quantitative_development", "data_engineering"])

    import_jobs(
        database,
        catalog,
        clock,
        [final_job(1, **QUANT_TRADER), final_job(2, **DATA_SCIENTIST)],
        "a",
    )

    with database.session_factory() as db:
        assert db.get(UserPreference, legacy).role_ids == [
            "quantitative_development",
            "data_engineering",
        ]
    assert _matched_titles(database, legacy) == []


def test_downgrade_refuses_while_jobs_or_preferences_use_the_new_role(
    database, catalog, clock, notification_postgres_url
) -> None:
    alembic = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic.set_main_option(
        "sqlalchemy.url", alembic_config_url(notification_postgres_url)
    )
    import_jobs(database, catalog, clock, [final_job(1, **QUANT_TRADER)], "a")
    with database.session_factory.begin() as db:
        user = create_user(db, clock(), "trading@example.com")
    _select_roles(database, user, ["quantitative_trading_research"])
    database.engine.dispose()
    try:
        with pytest.raises(RuntimeError, match="quantitative_trading_research"):
            command.downgrade(alembic, "20261022_0009")
        with database.engine.connect() as connection:
            assert connection.scalar(
                text("SELECT version_num FROM alembic_version")
            ) == "20261025_0010"
    finally:
        database.engine.dispose()
        command.upgrade(alembic, "head")


def test_downgrade_refuses_while_only_a_preference_uses_the_new_role(
    database, clock, notification_postgres_url
) -> None:
    alembic = Config(str(BACKEND_DIR / "alembic.ini"))
    alembic.set_main_option(
        "sqlalchemy.url", alembic_config_url(notification_postgres_url)
    )
    with database.session_factory.begin() as db:
        user = create_user(db, clock(), "trading@example.com")
    _select_roles(database, user, ["quantitative_trading_research"])
    database.engine.dispose()
    try:
        with pytest.raises(RuntimeError, match="preferences"):
            command.downgrade(alembic, "20261022_0009")
    finally:
        database.engine.dispose()
        command.upgrade(alembic, "head")
