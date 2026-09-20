"""文档版本、知识分块和索引版本。"""

from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "document_versions",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("source_filename", sa.String(255), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("domain", sa.String(16), nullable=False),
        sa.Column("product", sa.String(120), nullable=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("audience", sa.String(32), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("parser_version", sa.String(32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("created_by", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("inactive_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("content_hash", "domain", "audience", name="uq_document_content_scope"),
    )
    for column in ["domain", "status", "audience", "effective_at", "expires_at"]:
        op.create_index(f"ix_document_versions_{column}", "document_versions", [column])
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.String(120), primary_key=True),
        sa.Column("document_id", sa.String(96), sa.ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("domain", sa.String(16), nullable=False),
        sa.Column("product", sa.String(120), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(255), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("embedding_json", sa.JSON(), nullable=True),
        sa.Column("embedding_model", sa.String(80), nullable=True),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk_index"),
    )
    for column in ["document_id", "domain", "active"]:
        op.create_index(f"ix_knowledge_chunks_{column}", "knowledge_chunks", [column])
    op.create_table(
        "knowledge_indexes",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("data_version", sa.String(120), unique=True, nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("embedding_model", sa.String(80), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("document_count", sa.Integer(), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_knowledge_indexes_active", "knowledge_indexes", ["active"])


def downgrade():
    op.drop_table("knowledge_indexes")
    op.drop_table("knowledge_chunks")
    op.drop_table("document_versions")
