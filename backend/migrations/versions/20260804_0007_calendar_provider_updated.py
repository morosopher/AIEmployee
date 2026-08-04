"""保存 Google Calendar 供应商更新时间。"""

import sqlalchemy as sa
from alembic import op

revision = "20260804_0007"
# 必须接在既有 Google sources 迁移之后，避免与其从同一父 revision 分叉为两个 head。
down_revision = "20260804_0007_google_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """以可前向兼容的可空列保存供应商 ``updated``，不回填不可信历史值。"""
    op.add_column(
        "calendar_events",
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """仅供未部署开发环境反向移除新增字段。"""
    op.drop_column("calendar_events", "provider_updated_at")
