"""将遗留 Gmail 同步游标资源名迁移为供应商无关的 mail。

Revision ID: 20260806_0013
Revises: 20260806_0012
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260806_0013"
down_revision: str | Sequence[str] | None = "20260806_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只重命名遗留 Gmail 资源种类，原样保留作用域与 opaque cursor。"""
    # 0011 已把游标唯一键扩展到 scope_key；这里不触碰 scope 或 cursor，避免丢失同步位置。
    op.execute(
        sa.text("UPDATE sync_cursors SET resource_kind = 'mail' WHERE resource_kind = 'gmail'")
    )


def downgrade() -> None:
    """拒绝无法区分 Gmail 与 Microsoft mail 游标的破坏性回滚。"""
    raise RuntimeError("provider-neutral mail cursors cannot be downgraded safely")
