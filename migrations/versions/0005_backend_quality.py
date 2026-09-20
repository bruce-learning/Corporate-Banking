"""后端质量闭环：计划追踪、导入任务、评测、Bad Case 与模型用量。"""

from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("chat_traces", sa.Column("original_query", sa.Text(), nullable=True))
    op.add_column("chat_traces", sa.Column("rewritten_query", sa.Text(), nullable=True))
    op.add_column("chat_traces", sa.Column("execution_mode", sa.String(20), server_default="fixed", nullable=False))
    op.add_column("chat_traces", sa.Column("plan_steps", sa.JSON(), server_default="[]", nullable=False))
    op.add_column("chat_traces", sa.Column("citation_valid", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("chat_traces", sa.Column("citation_coverage", sa.Float(), server_default="0", nullable=False))

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_filename", sa.String(255), nullable=False),
        sa.Column("domain", sa.String(16), nullable=False),
        sa.Column("product", sa.String(120), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("auto_publish", sa.Boolean(), nullable=False),
        sa.Column("document_id", sa.String(96), sa.ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("error_message", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_ingestion_jobs_user_id", "ingestion_jobs", ["user_id"])
    op.create_index("ix_ingestion_jobs_status", "ingestion_jobs", ["status"])

    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("dataset_version", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("limitations_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_evaluation_runs_dataset_version", "evaluation_runs", ["dataset_version"])
    op.create_index("ix_evaluation_runs_status", "evaluation_runs", ["status"])

    op.create_table(
        "bad_cases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("evaluation_run_id", sa.String(36), sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=True),
        sa.Column("case_id", sa.String(120), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("expected_json", sa.JSON(), nullable=False),
        sa.Column("actual_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("evaluation_run_id", "case_id", name="uq_bad_case_run_case"),
    )
    for column in ["evaluation_run_id", "category", "status"]:
        op.create_index(f"ix_bad_cases_{column}", "bad_cases", [column])

    op.create_table(
        "model_usage_records",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("operation", sa.String(40), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_category", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_model_usage_records_request_id", "model_usage_records", ["request_id"])
    op.create_index("ix_model_usage_records_user_id", "model_usage_records", ["user_id"])


def downgrade():
    op.drop_table("model_usage_records")
    op.drop_table("bad_cases")
    op.drop_table("evaluation_runs")
    op.drop_table("ingestion_jobs")
    for column in ["citation_coverage", "citation_valid", "plan_steps", "execution_mode", "rewritten_query", "original_query"]:
        op.drop_column("chat_traces", column)
