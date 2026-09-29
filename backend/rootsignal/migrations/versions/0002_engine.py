"""Phase 3: investigation context/iterations, LLM call log, incident memory (pgvector).

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

from rootsignal.db.models import EmbeddingType, JsonType

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column("investigations", sa.Column("context", JsonType, nullable=True))
    op.add_column(
        "investigations", sa.Column("iterations", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "investigations", sa.Column("reasoner", sa.String(100), nullable=False, server_default="")
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "investigation_id",
            sa.String(36),
            sa.ForeignKey("investigations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(50), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_llm_calls_investigation_id", "llm_calls", ["investigation_id"])

    op.create_table(
        "incident_memory",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "investigation_id",
            sa.String(36),
            sa.ForeignKey("investigations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("service", sa.String(100), nullable=False),
        sa.Column("category", sa.String(50), nullable=False),
        sa.Column("root_cause", sa.Text(), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("signature", sa.Text(), nullable=False),
        sa.Column("embedding", EmbeddingType, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_incident_memory_investigation_id", "incident_memory", ["investigation_id"])
    op.create_index("ix_incident_memory_service", "incident_memory", ["service"])


def downgrade() -> None:
    op.drop_table("incident_memory")
    op.drop_table("llm_calls")
    op.drop_column("investigations", "reasoner")
    op.drop_column("investigations", "iterations")
    op.drop_column("investigations", "context")
