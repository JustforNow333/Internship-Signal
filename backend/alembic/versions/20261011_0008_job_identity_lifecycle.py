"""Add schema for stable job identity and source-evidence lifecycle.

Revision ID: 20261011_0008
Revises: 20260926_0007

Schema only: nothing reads or writes these structures yet, and no existing
row is backfilled.

``hosted_job_identities`` holds the source-scoped identity keys of each job
(requisition, posting-specific URL, or backstop listing). A key belongs to at
most one job per company and kind, and ``identity_digest`` must be the
SHA-256 hex digest of ``identity_key`` so uniqueness never depends on long
keys fitting in a btree entry.

``hosted_source_observations`` records, per import run, what each source
attempt reported about its own completeness: the evidence a future closure
rule needs before treating a missing posting as removed.

``hosted_jobs`` gains nullable or defaulted columns only. Adding them is a
catalog-only change; the new CHECK and FOREIGN KEY constraints are added NOT
VALID and validated separately, so writers are blocked only for the catalog
updates and never for a table scan. ``lock_timeout`` makes the migration fail
fast instead of queueing every reader behind a long-running import.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261011_0008"
down_revision: str | None = "20260926_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CLOSURE_REASONS = "'source_closed', 'deadline_passed', 'absent_from_source', 'superseded'"

# (name, expression) pairs added to hosted_jobs NOT VALID, then validated.
_JOB_CHECKS = (
    (
        "ck_hosted_jobs_absent_complete_runs_nonnegative",
        "absent_complete_runs >= 0",
    ),
    (
        "ck_hosted_jobs_closure_reason",
        f"closure_reason IS NULL OR closure_reason IN ({CLOSURE_REASONS})",
    ),
    (
        "ck_hosted_jobs_closure_reason_requires_closed",
        "closure_reason IS NULL OR NOT is_open",
    ),
    (
        "ck_hosted_jobs_superseded_state",
        "superseded_by_job_id IS NULL OR (superseded_by_job_id <> id "
        "AND NOT is_open AND closure_reason = 'superseded')",
    ),
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '5min'")

    op.add_column("hosted_jobs", sa.Column("source_key", sa.String(length=255)))
    op.add_column(
        "hosted_jobs",
        sa.Column("last_direct_seen_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "hosted_jobs",
        sa.Column(
            "absent_complete_runs",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("hosted_jobs", sa.Column("closure_reason", sa.String(length=32)))
    op.add_column("hosted_jobs", sa.Column("superseded_by_job_id", sa.Uuid()))

    for name, expression in _JOB_CHECKS:
        op.execute(
            f"ALTER TABLE hosted_jobs ADD CONSTRAINT {name} "
            f"CHECK ({expression}) NOT VALID"
        )
    op.execute(
        "ALTER TABLE hosted_jobs ADD CONSTRAINT fk_hosted_jobs_superseded_by_job "
        "FOREIGN KEY (superseded_by_job_id) REFERENCES hosted_jobs (id) "
        "ON DELETE RESTRICT NOT VALID"
    )
    for name, _expression in _JOB_CHECKS:
        op.execute(f"ALTER TABLE hosted_jobs VALIDATE CONSTRAINT {name}")
    op.execute(
        "ALTER TABLE hosted_jobs VALIDATE CONSTRAINT fk_hosted_jobs_superseded_by_job"
    )
    op.create_index(
        "ix_hosted_jobs_superseded_by_job_id",
        "hosted_jobs",
        ["superseded_by_job_id"],
        postgresql_where=sa.text("superseded_by_job_id IS NOT NULL"),
    )
    op.create_index(
        "ix_hosted_jobs_source_key",
        "hosted_jobs",
        ["source_key"],
        postgresql_where=sa.text("source_key IS NOT NULL"),
    )

    op.create_table(
        "hosted_job_identities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("identity_key", sa.Text(), nullable=False),
        sa.Column("identity_digest", sa.String(length=64), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('requisition', 'url', 'listing')",
            name="kind",
        ),
        sa.CheckConstraint(
            "length(identity_key) BETWEEN 1 AND 4096",
            name="identity_key_length",
        ),
        sa.CheckConstraint(
            "identity_digest = encode(sha256(convert_to(identity_key, 'UTF8')), 'hex')",
            name="identity_digest",
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at",
            name="seen_timestamps",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["hosted_jobs.id"],
            name="fk_hosted_job_identities_job",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_job_identities"),
        sa.UniqueConstraint(
            "company_id",
            "kind",
            "identity_digest",
            name="uq_hosted_job_identities_company_kind_digest",
        ),
    )
    op.create_index(
        "ix_hosted_job_identities_job_id", "hosted_job_identities", ["job_id"]
    )

    op.create_table(
        "hosted_source_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("import_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.String(length=255), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("company_name", sa.String(length=200), nullable=True),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempted", sa.Boolean(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=True),
        sa.Column("complete", sa.Boolean(), nullable=True),
        sa.Column("incomplete", sa.Boolean(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=True),
        sa.Column("degraded", sa.Boolean(), nullable=True),
        sa.Column("rows_returned", sa.Integer(), nullable=True),
        sa.Column("identity_count", sa.Integer(), nullable=True),
        sa.Column("config_fingerprint", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "source_kind IN ('direct', 'github_feed')",
            name="source_kind",
        ),
        sa.CheckConstraint(
            "attempted OR (succeeded IS NULL AND complete IS NULL "
            "AND rows_returned IS NULL)",
            name="outcome_requires_attempt",
        ),
        sa.CheckConstraint(
            "complete IS NOT TRUE OR succeeded IS TRUE",
            name="complete_requires_success",
        ),
        sa.CheckConstraint(
            "rows_returned IS NULL OR rows_returned >= 0",
            name="rows_returned_nonnegative",
        ),
        sa.CheckConstraint(
            "identity_count IS NULL OR identity_count >= 0",
            name="identity_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["hosted_job_import_runs.id"],
            name="fk_hosted_source_obs_run",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_hosted_source_observations"),
        sa.UniqueConstraint(
            "import_run_id",
            "source_key",
            name="uq_hosted_source_observations_run_source",
        ),
    )
    op.create_index(
        "ix_hosted_source_observations_source_observed",
        "hosted_source_observations",
        ["source_key", "observed_at"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index(
        "ix_hosted_source_observations_source_observed",
        table_name="hosted_source_observations",
    )
    op.drop_table("hosted_source_observations")
    op.drop_index(
        "ix_hosted_job_identities_job_id", table_name="hosted_job_identities"
    )
    op.drop_table("hosted_job_identities")
    op.drop_index("ix_hosted_jobs_source_key", table_name="hosted_jobs")
    op.drop_index("ix_hosted_jobs_superseded_by_job_id", table_name="hosted_jobs")
    # Raw SQL: these names are already final, and the ORM naming convention
    # would otherwise prefix the check-constraint names a second time.
    op.execute(
        "ALTER TABLE hosted_jobs DROP CONSTRAINT fk_hosted_jobs_superseded_by_job"
    )
    for name, _expression in reversed(_JOB_CHECKS):
        op.execute(f"ALTER TABLE hosted_jobs DROP CONSTRAINT {name}")
    op.drop_column("hosted_jobs", "superseded_by_job_id")
    op.drop_column("hosted_jobs", "closure_reason")
    op.drop_column("hosted_jobs", "absent_complete_runs")
    op.drop_column("hosted_jobs", "last_direct_seen_at")
    op.drop_column("hosted_jobs", "source_key")
