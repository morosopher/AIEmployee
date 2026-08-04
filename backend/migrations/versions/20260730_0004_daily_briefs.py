"""持久化每日简报、对话与用户保留设置。

Revision ID: 20260730_0004
Revises: 20260804_0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260730_0004"
down_revision: str | Sequence[str] | None = "20260804_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以向前兼容方式增加简报、对话和保留期字段。"""
    for name, default, constraint in (
        ("email_body_retention_days", "30", "ck_users_email_body_retention_days"),
        ("source_metadata_retention_days", "180", "ck_users_source_metadata_retention_days"),
        ("workspace_history_retention_days", "365", "ck_users_workspace_history_retention_days"),
    ):
        op.add_column("users", sa.Column(name, sa.SmallInteger(), server_default=default, nullable=False))
        op.create_check_constraint(constraint, "users", f"{name} BETWEEN 1 AND 3650")
    op.create_table("conversations", sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("title", sa.String(200), nullable=False), sa.Column("id", sa.Uuid(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False), sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"))
    op.create_table("daily_briefs", sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("local_date", sa.Date(), nullable=False), sa.Column("version", sa.Integer(), nullable=False), sa.Column("task_id", sa.Uuid(), nullable=False), sa.Column("completeness", sa.String(16), nullable=False), sa.Column("source_cutoff", sa.DateTime(timezone=True), nullable=False), sa.Column("headline", sa.Text(), nullable=False), sa.Column("structured_content", postgresql.JSONB(), nullable=False), sa.Column("markdown", sa.Text(), nullable=False), sa.Column("warnings", postgresql.JSONB(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("id", sa.Uuid(), nullable=False), sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"], ondelete="RESTRICT"), sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("task_id", name="uq_daily_briefs_task_id"), sa.UniqueConstraint("user_id", "local_date", "version", name="uq_daily_briefs_user_date_version"))
    op.create_table("daily_brief_items", sa.Column("brief_id", sa.Uuid(), nullable=False), sa.Column("position", sa.Integer(), nullable=False), sa.Column("section", sa.String(32), nullable=False), sa.Column("priority", sa.String(16), nullable=False), sa.Column("title", sa.Text(), nullable=False), sa.Column("body_markdown", sa.Text(), nullable=False), sa.Column("source_refs", postgresql.JSONB(), nullable=False), sa.Column("suggested_action_kind", sa.String(100), nullable=True), sa.Column("id", sa.Uuid(), nullable=False), sa.ForeignKeyConstraint(["brief_id"], ["daily_briefs.id"], ondelete="CASCADE"), sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("brief_id", "position", name="uq_daily_brief_items_brief_position"))
    op.create_table("messages", sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("conversation_id", sa.Uuid(), nullable=False), sa.Column("role", sa.String(16), nullable=False), sa.Column("content_markdown", sa.Text(), nullable=False), sa.Column("task_id", sa.Uuid(), nullable=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("id", sa.Uuid(), nullable=False), sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"))
    op.create_table("llm_invocations", sa.Column("user_id", sa.Uuid(), nullable=False), sa.Column("task_id", sa.Uuid(), nullable=False), sa.Column("step_id", sa.Uuid(), nullable=True), sa.Column("provider", sa.String(100), nullable=False), sa.Column("model_name", sa.String(100), nullable=False), sa.Column("prompt_version", sa.String(100), nullable=False), sa.Column("input_hash", sa.String(64), nullable=False), sa.Column("output_schema", sa.String(255), nullable=False), sa.Column("input_tokens", sa.Integer(), nullable=False), sa.Column("output_tokens", sa.Integer(), nullable=False), sa.Column("estimated_cost_microusd", sa.BigInteger(), nullable=True), sa.Column("latency_ms", sa.Integer(), nullable=False), sa.Column("status", sa.String(32), nullable=False), sa.Column("error_code", sa.String(100), nullable=True), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("id", sa.Uuid(), nullable=False), sa.ForeignKeyConstraint(["step_id"], ["task_steps.id"], ondelete="SET NULL"), sa.ForeignKeyConstraint(["task_id"], ["task_runs.id"], ondelete="CASCADE"), sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"), sa.PrimaryKeyConstraint("id"))


def downgrade() -> None:
    """以反向依赖顺序删除本迁移新增表及字段。"""
    for table in ("llm_invocations", "messages", "daily_brief_items", "daily_briefs", "conversations"):
        op.drop_table(table)
    for name, constraint in (("workspace_history_retention_days", "ck_users_workspace_history_retention_days"), ("source_metadata_retention_days", "ck_users_source_metadata_retention_days"), ("email_body_retention_days", "ck_users_email_body_retention_days")):
        op.drop_constraint(constraint, "users", type_="check")
        op.drop_column("users", name)
