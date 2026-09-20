"""请求追踪关联；保留旧记录，不删除现有数据。"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("request_records", sa.Column("request_id", sa.String(36), nullable=True))
    op.execute("UPDATE request_records SET request_id=id WHERE request_id IS NULL")
    op.alter_column("request_records", "request_id", nullable=False)
    op.create_index("ix_request_records_request_id", "request_records", ["request_id"])


def downgrade():
    op.drop_index("ix_request_records_request_id", "request_records")
    op.drop_column("request_records", "request_id")
