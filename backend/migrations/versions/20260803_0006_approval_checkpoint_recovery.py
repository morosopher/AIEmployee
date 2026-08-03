"""为冻结审批到首个 checkpoint 的交接增加 PostgreSQL 恢复 anchor。

Revision ID: 20260803_0006
Revises: 20260803_0005
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0006"
down_revision: str | Sequence[str] | None = "20260803_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展任务行，使 Redis 丢失时仍可扫描恢复审批 interrupt。"""
    op.add_column(
        "task_runs",
        sa.Column("approval_checkpoint_recovery_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_task_runs_approval_checkpoint_recovery",
        "task_runs",
        ["approval_checkpoint_recovery_at", "id"],
        postgresql_where=sa.text("approval_checkpoint_recovery_at IS NOT NULL"),
    )


def downgrade() -> None:
    """仅供开发环境回退，移除尚未写入业务语义的恢复 anchor。"""
    op.drop_index("ix_task_runs_approval_checkpoint_recovery", table_name="task_runs")
    op.drop_column("task_runs", "approval_checkpoint_recovery_at")
