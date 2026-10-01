"""增加用户隔离的任务历史seek索引，保留0019全部权限与执行语义。

Revision ID: 20261001_0020
Revises: 20260809_0019

普通事务型索引与版本行、权限核对共同提交；不引入并发索引的部分提交状态。
"""

from alembic import op

revision: str = "20261001_0020"
down_revision: str = "20260809_0019"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    """在原迁移事务内创建三列B-tree，支持用户内稳定时间倒序分页。"""
    op.create_index("ix_task_runs_user_created_id", "task_runs", ["user_id", "created_at", "id"])


def downgrade() -> None:
    """只移除此revision新增索引；不触碰任务记录、状态或0019的AAD字段。"""
    op.drop_index("ix_task_runs_user_created_id", table_name="task_runs")
