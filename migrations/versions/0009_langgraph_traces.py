"""Persist LangGraph orchestration backend and node trace."""

from alembic import op
import sqlalchemy as sa


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_traces",
        sa.Column("orchestration_backend", sa.String(length=24),
                  server_default="legacy", nullable=False),
    )
    op.add_column(
        "chat_traces",
        sa.Column("graph_trace", sa.JSON(), server_default="[]", nullable=False),
    )


def downgrade():
    op.drop_column("chat_traces", "graph_trace")
    op.drop_column("chat_traces", "orchestration_backend")
