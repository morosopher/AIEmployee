"""为首次 OAuth state 增加断开后的持久失效标记。

Revision ID: 20260808_0015
Revises: 20260807_0014
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0015"
down_revision: str | Sequence[str] | None = "20260807_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加可空时间戳；旧 attempt 默认仍有效，断开事务只标记当时存在的首次 state。"""
    op.add_column(
        "oauth_attempts",
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """拒绝丢失旧 state 失效事实的破坏性回滚。"""
    raise RuntimeError("oauth attempt invalidation cannot be downgraded safely")
