"""Normalize hosted check-constraint names to the SQLAlchemy convention.

Revision ID: 20260919_0006
Revises: 20260919_0005

Revisions 0001-0004 declared their check constraints with names that were
already prefixed, for example ``name="ck_hosted_jobs_role_id"``. The shared
``NAMING_CONVENTION`` in ``app.hosted.database`` then applied
``ck_%(table_name)s_%(constraint_name)s`` on top, so PostgreSQL received
``ck_hosted_jobs_ck_hosted_jobs_role_id``, truncated to 63 characters with a
deterministic suffix where it was too long. The ORM models declare the same
constraints with short logical names (``name="role_id"``), which render as
``ck_hosted_jobs_role_id``, so ``alembic check`` reported a permanent
add/remove pair for all 27 check constraints and no drift could be detected.

This revision renames the physical constraints to the canonical names. It does
not change any constraint expression, so enforcement is untouched and the
rename is a catalog-only operation. The already-applied revisions are left
exactly as they are, which keeps deployed databases and freshly built ones
converging on the same final schema.

Future migrations should pass the short logical name and let the convention add
the prefix once.

The pairs below were derived by diffing ``pg_get_constraintdef`` between a
database built by ``alembic upgrade head`` and one built by
``Base.metadata.create_all``, so no name is guessed.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260919_0006"
down_revision: str | None = "20260919_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, name produced by revisions 0001-0004, name the ORM metadata expects)
RENAMES: tuple[tuple[str, str, str], ...] = (
    (
        "hosted_job_import_attempts",
        "ck_hosted_job_import_attempts_ck_hosted_job_import_atte_3e3c",
        "ck_hosted_job_import_attempts_attempt_number_positive",
    ),
    (
        "hosted_job_import_attempts",
        "ck_hosted_job_import_attempts_ck_hosted_job_import_atte_3f1f",
        "ck_hosted_job_import_attempts_completion_state",
    ),
    (
        "hosted_job_import_attempts",
        "ck_hosted_job_import_attempts_ck_hosted_job_import_atte_fdb5",
        "ck_hosted_job_import_attempts_status",
    ),
    (
        "hosted_job_import_runs",
        "ck_hosted_job_import_runs_ck_hosted_job_import_runs_com_f9c5",
        "ck_hosted_job_import_runs_completion_state",
    ),
    (
        "hosted_job_import_runs",
        "ck_hosted_job_import_runs_ck_hosted_job_import_runs_job_e706",
        "ck_hosted_job_import_runs_jobs_inserted_nonnegative",
    ),
    (
        "hosted_job_import_runs",
        "ck_hosted_job_import_runs_ck_hosted_job_import_runs_job_df61",
        "ck_hosted_job_import_runs_jobs_received_nonnegative",
    ),
    (
        "hosted_job_import_runs",
        "ck_hosted_job_import_runs_ck_hosted_job_import_runs_job_1a46",
        "ck_hosted_job_import_runs_jobs_skipped_nonnegative",
    ),
    (
        "hosted_job_import_runs",
        "ck_hosted_job_import_runs_ck_hosted_job_import_runs_job_64e5",
        "ck_hosted_job_import_runs_jobs_unchanged_nonnegative",
    ),
    (
        "hosted_job_import_runs",
        "ck_hosted_job_import_runs_ck_hosted_job_import_runs_job_2c8d",
        "ck_hosted_job_import_runs_jobs_updated_nonnegative",
    ),
    (
        "hosted_job_import_runs",
        "ck_hosted_job_import_runs_ck_hosted_job_import_runs_mat_bc70",
        "ck_hosted_job_import_runs_matches_created_nonnegative",
    ),
    (
        "hosted_job_import_runs",
        "ck_hosted_job_import_runs_ck_hosted_job_import_runs_status",
        "ck_hosted_job_import_runs_status",
    ),
    (
        "hosted_jobs",
        "ck_hosted_jobs_ck_hosted_jobs_closed_timestamp",
        "ck_hosted_jobs_closed_timestamp",
    ),
    (
        "hosted_jobs",
        "ck_hosted_jobs_ck_hosted_jobs_role_id",
        "ck_hosted_jobs_role_id",
    ),
    (
        "hosted_jobs",
        "ck_hosted_jobs_ck_hosted_jobs_seen_timestamps",
        "ck_hosted_jobs_seen_timestamps",
    ),
    (
        "hosted_notification_attempts",
        "ck_hosted_notification_attempts_ck_hosted_notification__2fa1",
        "ck_hosted_notification_attempts_attempt_number_positive",
    ),
    (
        "hosted_notification_attempts",
        "ck_hosted_notification_attempts_ck_hosted_notification__cb1a",
        "ck_hosted_notification_attempts_lifecycle",
    ),
    (
        "hosted_notification_attempts",
        "ck_hosted_notification_attempts_ck_hosted_notification__6444",
        "ck_hosted_notification_attempts_outcome",
    ),
    (
        "hosted_notification_batches",
        "ck_hosted_notification_batches_ck_hosted_notification_b_ff13",
        "ck_hosted_notification_batches_attempt_count_nonnegative",
    ),
    (
        "hosted_notification_batches",
        "ck_hosted_notification_batches_ck_hosted_notification_b_6244",
        "ck_hosted_notification_batches_frequency",
    ),
    (
        "hosted_notification_batches",
        "ck_hosted_notification_batches_ck_hosted_notification_b_e54e",
        "ck_hosted_notification_batches_lifecycle",
    ),
    (
        "hosted_notification_batches",
        "ck_hosted_notification_batches_ck_hosted_notification_b_44a2",
        "ck_hosted_notification_batches_source_import_frequency",
    ),
    (
        "hosted_notification_batches",
        "ck_hosted_notification_batches_ck_hosted_notification_b_6762",
        "ck_hosted_notification_batches_status",
    ),
    (
        "hosted_notification_items",
        "ck_hosted_notification_items_ck_hosted_notification_ite_aba3",
        "ck_hosted_notification_items_lifecycle",
    ),
    (
        "hosted_notification_items",
        "ck_hosted_notification_items_ck_hosted_notification_ite_5913",
        "ck_hosted_notification_items_status",
    ),
    (
        "hosted_unsupported_company_requests",
        "ck_hosted_unsupported_company_requests_ck_hosted_unsupp_c393",
        "ck_hosted_unsupported_company_requests_status",
    ),
    (
        "hosted_user_job_matches",
        "ck_hosted_user_job_matches_ck_hosted_user_job_matches_m_c5e6",
        "ck_hosted_user_job_matches_match_timestamps",
    ),
    (
        "hosted_user_preferences",
        "ck_hosted_user_preferences_ck_hosted_user_preferences_a_2f17",
        "ck_hosted_user_preferences_alert_frequency",
    ),
)


def _rename(table: str, old: str, new: str) -> None:
    """Rename one constraint, tolerating a database already in the new shape.

    Every identifier comes from the static table above; nothing here is
    interpolated from data.
    """

    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = '{old}'
                  AND conrelid = 'public.{table}'::regclass
            ) THEN
                ALTER TABLE public.{table}
                    RENAME CONSTRAINT "{old}" TO "{new}";
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    for table, old, new in RENAMES:
        _rename(table, old, new)


def downgrade() -> None:
    for table, old, new in RENAMES:
        _rename(table, new, old)
