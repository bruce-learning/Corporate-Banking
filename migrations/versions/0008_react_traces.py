"""Persist bounded ReAct action and observation summaries."""

from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chat_traces",
        sa.Column("react_steps", sa.JSON(), server_default="{}", nullable=False),
    )


def downgrade():
    op.drop_column("chat_traces", "react_steps")
