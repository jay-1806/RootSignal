"""Baseline: the Phase 1-2 schema (investigations table).

Phase 1-2 created this table with `create_all`, before migrations existed. Skip it if it's
already there so existing databases upgrade in place.

Revision ID: 0001
Revises:
"""

import sqlalchemy as sa
from alembic import op

from rootsignal.db.models import JsonType

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("investigations"):
        return
    op.create_table(
        "investigations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("alert", JsonType, nullable=False),
        sa.Column("history", JsonType, nullable=False),
        sa.Column("hypotheses", JsonType, nullable=False),
        sa.Column("rca", JsonType, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_investigations_service", "investigations", ["service"])
    op.create_index("ix_investigations_state", "investigations", ["state"])


def downgrade() -> None:
    op.drop_table("investigations")
