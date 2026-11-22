"""Database-free checks for the Applied match schema foundation.

The PostgreSQL migration checks live in ``test_hosted_match_applied_schema``;
these run without a database so the model and revision chain are always
verified.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import DateTime
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from app.hosted.models import UserJobMatch

BACKEND_DIR = Path(__file__).resolve().parents[1]
PREVIOUS_REVISION = "20261025_0010"
APPLIED_REVISION = "20261119_0011"
INDEX_NAME = "ix_hosted_user_job_matches_user_applied"


def test_applied_revision_follows_0010_directly() -> None:
    scripts = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini")))
    # 20261121_0012 (education eligibility) now follows it as the single head;
    # see test_hosted_education_model.
    assert scripts.get_heads() == ["20261121_0012"]
    assert scripts.get_revision(APPLIED_REVISION).down_revision == PREVIOUS_REVISION
    # Exactly one new revision follows 0010.
    assert [
        revision.revision
        for revision in scripts.walk_revisions()
        if revision.down_revision == PREVIOUS_REVISION
    ] == [APPLIED_REVISION]


def test_model_declares_nullable_timezone_aware_applied_at() -> None:
    column = UserJobMatch.__table__.c.applied_at
    assert column.nullable is True
    assert isinstance(column.type, DateTime)
    assert column.type.timezone is True
    assert column.default is None
    assert column.server_default is None


def test_model_declares_the_partial_applied_index() -> None:
    [index] = [
        index
        for index in UserJobMatch.__table__.indexes
        if index.name == INDEX_NAME
    ]
    assert [column.name for column in index.columns] == ["user_id", "applied_at"]
    assert not index.unique
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert " ".join(ddl.split()) == (
        f"CREATE INDEX {INDEX_NAME} ON hosted_user_job_matches "
        "(user_id, applied_at) WHERE applied_at IS NOT NULL"
    )
