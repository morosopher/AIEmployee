"""为 Gmail 线程补齐供应商更新时间列。

Revision ID: 20260804_0009
Revises: 20260730_0004
"""

import sqlalchemy as sa
from alembic import op

revision = "20260804_0009"
down_revision = "20260730_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """扩展线程元数据列，保持已存在的同步事实不变。"""
    op.add_column("email_threads", sa.Column("provider_updated_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    """删除本迁移新增的可选供应商更新时间列。"""
    op.drop_column("email_threads", "provider_updated_at")
