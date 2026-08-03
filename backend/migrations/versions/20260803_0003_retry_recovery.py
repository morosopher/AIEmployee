"""为 Redis 丢失后的任务重试恢复保存 PostgreSQL 到期时间。

Revision ID: 20260803_0003
Revises: 20260730_0002
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0003"
down_revision: str | Sequence[str] | None = "20260730_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """扩展任务表并建立只覆盖待恢复重试的有界扫描索引。"""
    op.add_column(
        "task_runs",
        sa.Column("retry_recovery_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_task_runs_retry_recovery_due",
        "task_runs",
        ["retry_recovery_at", "id"],
        unique=False,
        postgresql_where=sa.text("status = 'retry_scheduled' AND retry_recovery_at IS NOT NULL"),
    )
    # 升级瞬间已经在 Redis 延迟队列中的任务没有旧字段可填；同样等待策略上界后才允许
    # 恢复器接管，既让旧任务最终可恢复，也不在正常调度前制造重复投递。
    op.execute(
        "UPDATE task_runs "
        "SET retry_recovery_at = now() + interval '301 seconds' "
        "WHERE status = 'retry_scheduled' AND retry_recovery_at IS NULL"
    )


def downgrade() -> None:
    """移除本版本新增索引和字段；生产恢复应始终使用前向迁移。"""
    op.drop_index("ix_task_runs_retry_recovery_due", table_name="task_runs")
    op.drop_column("task_runs", "retry_recovery_at")
