"""Permanent deletion of one hosted account and everything it owns.

Deleting the ``hosted_users`` row removes every account-owned row through the
schema's ``ON DELETE CASCADE`` foreign keys: sessions, verification and reset
tokens, preferences, company watches, unsupported-company requests, job
matches, catch-up requests, and notification batches with their items and
attempts. Shared data (jobs, identities, source observations, import runs and
attempts) has no foreign key to a user and is never touched.

The caller owns the transaction, so the whole deletion commits or rolls back
as one unit.
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    HostedCatchUpRequest,
    HostedNotificationBatch,
    User,
    UserCompanyWatch,
)


def delete_account(
    db: Session, user_id: uuid.UUID, *, password_hash: str
) -> bool:
    """Delete the account if it still holds ``password_hash``.

    Returns ``False``, changing nothing, when the account is already gone or
    its password changed after the caller verified it.

    Locks are taken in the order the other writers of these rows use, so a
    deletion waits for, rather than deadlocks with, concurrent work:

    1. Company watches. An import holds ``FOR KEY SHARE`` on the watches it
       reconciles until it commits, then creates matches and notification work
       for those watchers. Waiting here means an in-flight import finishes
       first and its new rows are deleted with the account; an import that
       starts later no longer sees the watches at all.
    2. Catch-up requests, which verification and imports resolve before they
       create notification batches.
    3. Notification batches. A delivery worker holds a batch row only for the
       short prepare and record transactions; claims use ``SKIP LOCKED``.
    4. The user row itself.
    """

    db.execute(
        select(UserCompanyWatch.company_id)
        .where(UserCompanyWatch.user_id == user_id)
        .order_by(UserCompanyWatch.company_id)
        .with_for_update()
    )
    db.execute(
        select(HostedCatchUpRequest.id)
        .where(HostedCatchUpRequest.user_id == user_id)
        .order_by(HostedCatchUpRequest.id)
        .with_for_update()
    )
    db.execute(
        select(HostedNotificationBatch.id)
        .where(HostedNotificationBatch.user_id == user_id)
        .order_by(HostedNotificationBatch.id)
        .with_for_update()
    )
    user = db.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if user is None or user.password_hash != password_hash:
        return False
    db.execute(delete(User).where(User.id == user_id))
    return True
