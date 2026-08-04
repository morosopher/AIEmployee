"""允许 Calendar 最小删除墓碑缺失时段和 etag。"""
import sqlalchemy as sa
from alembic import op

revision = "20260804_0008"
down_revision = "20260804_0007"
branch_labels = None
depends_on = None
def upgrade() -> None:
    """将供应商可省略的 tombstone 字段改为可空，不伪造时间。"""
    op.alter_column("calendar_events", "starts_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.alter_column("calendar_events", "ends_at", existing_type=sa.DateTime(timezone=True), nullable=True)
    op.alter_column("calendar_events", "etag", existing_type=sa.String(length=255), nullable=True)
def downgrade() -> None:
    """历史行若含最小 tombstone，不能安全恢复非空约束。"""
    raise NotImplementedError("calendar tombstone nulls make downgrade unsafe")
