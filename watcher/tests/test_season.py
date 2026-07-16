from datetime import date

import pytest

from watcher.config import CompanyCfg
from watcher.season import company_season_warnings, season_status


@pytest.mark.parametrize(
    ("today", "terms", "expected"),
    [
        (date(2026, 6, 15), ("Summer 2026",), "ok"),
        (date(2026, 7, 15), ("Summer 2026",), "rollover_due"),
        (date(2026, 7, 15), ("Summer 2027",), "ok"),
        (date(2026, 7, 15), ("Fall 2026", "Summer 2027"), "ok"),
        (date(2027, 2, 1), ("Summer 2026",), "stale"),
        (date(2026, 7, 15), ("Rolling", "Various"), "unknown"),
    ],
)
def test_season_status_rules(today, terms, expected):
    assert season_status(terms, today=today) == expected


def test_season_status_uses_newest_recognized_year():
    assert season_status(
        ("Fall 2024", "Summer 2026", "Summer 2028"),
        today=date(2027, 9, 1),
    ) == "ok"


def test_company_specific_stale_override_produces_identifiable_warning():
    companies = (
        CompanyCfg(name="Inherited Co", terms=("Summer 2027",)),
        CompanyCfg(name="Stale Co", terms=("Summer 2026",)),
        CompanyCfg(name="Future Co", terms=("Summer 2028",)),
    )

    warnings = company_season_warnings(
        companies,
        ("Summer 2027",),
        today=date(2027, 7, 15),
    )

    assert warnings == ("Stale Co: stale company terms override (Summer 2026)",)
