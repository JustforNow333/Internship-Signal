"""Stage 3 education eligibility: history and notification guarantees (AC8, AC9).

Losing education eligibility (through ``PUT /api/preferences`` or through an
ordinary import that brings or changes a posting's stored
``student_eligibility``) must behave like any other lost match: the row is
kept and only ``no_longer_matches_at`` is stamped; regaining eligibility
reactivates the same row under the existing reconciliation rules. None of
these transitions, repeated imports or an ``already_imported`` replay may
create, cancel or change notification rows; only a genuinely new eligible
opening enqueues exactly one item.

These need ``HOSTED_TEST_DATABASE_URL`` and skip without it.
"""

from __future__ import annotations

from app.hosted.models import HostedJob, UserJobMatch
from sqlalchemy import func, select
from tests.test_hosted_match_applied_lifecycle import (
    _account,
    _import,
    _job,
    _match_id,
    _notification_state,
    _patch,
    _put_preferences,
    _rows_by_id,
    _state,
    _view_ids,
)
from tests.test_hosted_match_reconciliation import (  # noqa: F401 - pytest fixtures
    catalog,
    clean_match_database,
    hosted,
    make_client,
    match_postgres_url,
)

HISTORY_FIELDS = (
    "saved_at",
    "dismissed_at",
    "applied_at",
    "matched_at",
    "match_reasons",
)


def _eligibility(reason: str | None) -> dict:
    return {
        "eligible": reason is None,
        "exclusion_reason": reason,
        "degree_level": "masters" if reason == "graduate_only" else None,
        "evidence_source": "title" if reason else None,
        "evidence": "Synthetic restriction evidence" if reason else None,
    }


def _history(state: dict) -> dict:
    return {field: state[field] for field in HISTORY_FIELDS}


def _match_count(services) -> int:
    with services.database.session_factory() as db:
        return db.scalar(select(func.count()).select_from(UserJobMatch))


def _stored_eligibility(services, index: int):
    with services.database.session_factory() as db:
        return db.scalar(
            select(HostedJob.student_eligibility).where(
                HostedJob.watcher_job_id == f"watcher-job-{index}"
            )
        )


def _assert_lost(services, match_id, before: dict, now) -> None:
    after = _state(services, match_id)
    assert after["no_longer_matches_at"] == now
    assert after["last_matched_at"] == before["last_matched_at"]
    assert _history(after) == _history(before)


def _assert_regained(services, match_id, before: dict, now) -> None:
    after = _state(services, match_id)
    assert after["no_longer_matches_at"] is None
    # Existing reactivation rule: last_matched_at advances, reasons are
    # recomputed (identical here), every other history field is untouched.
    assert after["last_matched_at"] == now
    assert _history(after) == _history(before)


def test_preference_changes_deactivate_and_reactivate_without_notifying(
    hosted, make_client, catalog
) -> None:
    services, mailer, clock = hosted
    client, user_id = _account(services, mailer, make_client, "owner@example.com")
    first = _import(
        services,
        catalog,
        clock,
        [
            _job(1, student_eligibility=_eligibility("graduate_only")),
            _job(2, student_eligibility=_eligibility("freshman_only")),
            _job(3, title="Data Platform Software Engineer Intern"),
        ],
        "a",
    )
    # Not specified keeps every posting, so all three are new openings.
    assert first.counters.matches_created == 3
    assert len(first.new_opening_match_ids) == 3
    grad, fresh, plain = (_match_id(services, user_id, i) for i in (1, 2, 3))
    clock.advance(minutes=5)
    _patch(client, grad, {"saved": True})
    _patch(client, grad, {"applied": True})
    _patch(client, fresh, {"saved": True})
    _patch(client, fresh, {"dismissed": True})
    before = {m: _state(services, m) for m in (grad, fresh, plain)}
    baseline = _notification_state(services)
    assert len(baseline["hosted_notification_items"]) == 3
    assert _view_ids(client, "active") == {grad, plain}

    # Undergraduate first-year: graduate_only is lost, freshman_only is kept.
    clock.advance(hours=1)
    _put_preferences(
        client, education_level="undergraduate", class_standing="first_year"
    )
    _assert_lost(services, grad, before[grad], clock())
    assert _state(services, fresh) == before[fresh]
    assert _state(services, plain) == before[plain]
    assert _view_ids(client, "active") == {plain}
    assert _view_ids(client, "historical") == {grad}
    assert _view_ids(client, "saved") == {grad, fresh}
    assert _view_ids(client, "applied") == {grad}
    assert _view_ids(client, "dismissed") == {fresh}
    assert _notification_state(services) == baseline
    lost_grad_at = clock()

    # The owner-approved sophomore correction also excludes freshman_only.
    clock.advance(hours=1)
    _put_preferences(client, class_standing="sophomore")
    _assert_lost(services, fresh, before[fresh], clock())
    assert _state(services, grad)["no_longer_matches_at"] == lost_grad_at
    assert _view_ids(client, "historical") == {grad, fresh}
    assert _view_ids(client, "dismissed") == {fresh}
    assert _view_ids(client, "saved") == {grad, fresh}
    assert _view_ids(client, "active") == {plain}
    assert _notification_state(services) == baseline

    # Back to first-year: freshman_only is regained in place.
    clock.advance(hours=1)
    _put_preferences(client, class_standing="first_year")
    _assert_regained(services, fresh, before[fresh], clock())
    # Still dismissed, so the Active view keeps hiding it.
    assert _view_ids(client, "active") == {plain}
    assert _view_ids(client, "historical") == {grad}
    assert _notification_state(services) == baseline

    # Master's excludes phd_only only, so graduate_only is regained in place.
    clock.advance(hours=1)
    _put_preferences(client, education_level="masters")
    _assert_regained(services, grad, before[grad], clock())
    assert _view_ids(client, "active") == {grad, plain}
    assert _view_ids(client, "historical") == set()
    assert _view_ids(client, "applied") == {grad}
    assert _view_ids(client, "saved") == {grad, fresh}
    assert _view_ids(client, "dismissed") == {fresh}
    assert _state(services, plain) == before[plain]

    # Rows were never deleted or duplicated, and nothing was enqueued.
    assert _match_count(services) == 3
    assert _notification_state(services) == baseline


def test_import_eligibility_changes_keep_history_and_notify_only_new_openings(
    hosted, make_client, catalog
) -> None:
    services, mailer, clock = hosted
    client, user_id = _account(
        services,
        mailer,
        make_client,
        "owner@example.com",
        education_level="undergraduate",
        class_standing="junior",
    )
    # Unknown eligibility (no classifier verdict) is stored NULL and permissive.
    first = _import(services, catalog, clock, [_job(1), _job(2)], "a")
    assert first.counters.matches_created == 2
    assert _stored_eligibility(services, 1) is None
    match_id = _match_id(services, user_id, 1)
    other_id = _match_id(services, user_id, 2)
    clock.advance(minutes=5)
    _patch(client, match_id, {"saved": True})
    _patch(client, match_id, {"applied": True})
    before = _state(services, match_id)
    other_before = _state(services, other_id)
    baseline = _notification_state(services)
    assert len(baseline["hosted_notification_items"]) == 2

    # Eligibility arrives on the existing posting: the match is lost in place.
    clock.advance(hours=1)
    arrived = _import(
        services,
        catalog,
        clock,
        [_job(1, student_eligibility=_eligibility("graduate_only")), _job(2)],
        "b",
    )
    assert arrived.counters.matches_created == 0
    assert not arrived.new_opening_match_ids
    assert _stored_eligibility(services, 1)["exclusion_reason"] == "graduate_only"
    _assert_lost(services, match_id, before, clock())
    lost_at = clock()
    assert _state(services, other_id) == other_before
    assert _view_ids(client, "active") == {other_id}
    assert _view_ids(client, "historical") == {match_id}
    assert _view_ids(client, "saved") == {match_id}
    assert _view_ids(client, "applied") == {match_id}
    assert _view_ids(client, "dismissed") == set()
    assert _notification_state(services) == baseline

    # Replaying the same snapshot is an already_imported no-op.
    clock.advance(hours=1)
    replay = _import(
        services,
        catalog,
        clock,
        [_job(1, student_eligibility=_eligibility("graduate_only")), _job(2)],
        "b",
    )
    assert replay.already_imported is True
    assert _state(services, match_id)["no_longer_matches_at"] == lost_at
    assert _notification_state(services) == baseline

    # Eligibility changes to another reason that a junior still cannot take.
    clock.advance(hours=1)
    _import(
        services,
        catalog,
        clock,
        [_job(1, student_eligibility=_eligibility("freshman_only")), _job(2)],
        "c",
    )
    assert _stored_eligibility(services, 1)["exclusion_reason"] == "freshman_only"
    changed = _state(services, match_id)
    assert changed["no_longer_matches_at"] == lost_at
    assert _history(changed) == _history(before)
    assert _notification_state(services) == baseline

    # Eligibility is regained: the same row reactivates and nothing enqueues.
    clock.advance(hours=1)
    regained = _import(
        services,
        catalog,
        clock,
        [_job(1, student_eligibility=_eligibility(None)), _job(2)],
        "d",
    )
    assert regained.counters.matches_created == 0
    assert not regained.new_opening_match_ids
    assert _stored_eligibility(services, 1)["exclusion_reason"] is None
    _assert_regained(services, match_id, before, clock())
    assert _view_ids(client, "active") == {match_id, other_id}
    assert _view_ids(client, "historical") == set()
    assert _notification_state(services) == baseline

    # Positive control: one new eligible opening and one new incompatible one.
    clock.advance(hours=1)
    postings = [
        _job(1, student_eligibility=_eligibility(None)),
        _job(2),
        _job(3, title="Data Platform Software Engineer Intern"),
        _job(
            4,
            title="Research Software Engineer Intern",
            student_eligibility=_eligibility("phd_only"),
        ),
    ]
    opened = _import(services, catalog, clock, postings, "e")
    assert opened.counters.matches_created == 1
    new_id = _match_id(services, user_id, 3)
    assert [str(i) for i in opened.new_opening_match_ids] == [new_id]
    with services.database.session_factory() as db:
        incompatible = db.scalar(
            select(func.count())
            .select_from(UserJobMatch)
            .join(HostedJob, HostedJob.id == UserJobMatch.job_id)
            .where(HostedJob.watcher_job_id == "watcher-job-4")
        )
    assert incompatible == 0
    after = _notification_state(services)
    for table, rows in baseline.items():
        old, new = _rows_by_id(baseline, table), _rows_by_id(after, table)
        assert {key: new[key] for key in old} == old, table
    added = [
        row
        for key, row in _rows_by_id(after, "hosted_notification_items").items()
        if key not in _rows_by_id(baseline, "hosted_notification_items")
    ]
    assert [row["user_job_match_id"] for row in added] == [new_id]

    # Repeated imports of the same postings (replay and a fresh snapshot)
    # never notify again.
    clock.advance(hours=1)
    assert _import(services, catalog, clock, postings, "e").already_imported
    repeat = _import(services, catalog, clock, postings, "f")
    assert repeat.counters.matches_created == 0
    assert not repeat.new_opening_match_ids
    assert _notification_state(services) == after
    assert _match_count(services) == 3
    assert _history(_state(services, match_id)) == _history(before)


def test_preference_change_after_import_eligibility_never_notifies(
    hosted, make_client, catalog
) -> None:
    services, mailer, clock = hosted
    client, user_id = _account(
        services,
        mailer,
        make_client,
        "owner@example.com",
        education_level="undergraduate",
        class_standing="senior",
    )
    # A new opening the user cannot take creates no match and no item.
    blocked = _import(
        services,
        catalog,
        clock,
        [_job(1, student_eligibility=_eligibility("freshman_only"))],
        "a",
    )
    assert blocked.counters.matches_created == 0
    assert _match_count(services) == 0
    baseline = _notification_state(services)
    assert baseline["hosted_notification_items"] == []

    # Becoming eligible later through preferences is reconciliation, which
    # creates the row (the posting is still current) but never enqueues.
    clock.advance(hours=1)
    _put_preferences(client, class_standing="first_year")
    match_id = _match_id(services, user_id, 1)
    assert _view_ids(client, "active") == {match_id}
    assert _notification_state(services) == baseline
