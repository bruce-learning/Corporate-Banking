"""Lightweight Skills audit and three-layer memory trace."""

from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_traces", sa.Column("skills_used", sa.JSON(), server_default="{}", nullable=False))
    op.add_column("chat_traces", sa.Column("memory_context", sa.JSON(), server_default="{}", nullable=False))


def downgrade():
    op.drop_column("chat_traces", "memory_context")
    op.drop_column("chat_traces", "skills_used")
