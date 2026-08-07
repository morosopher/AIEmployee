"""绑定渐进 OAuth attempt 的目标连接并增加授权代际。

Revision ID: 20260807_0014
Revises: 20260806_0013
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260807_0014"
down_revision: str | Sequence[str] | None = "20260806_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """为已有连接安全补零，并让新的渐进授权 state 具备不可变目标边界。"""
    op.add_column(
        "oauth_connections",
        sa.Column(
            "authorization_generation",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "oauth_attempts",
        sa.Column("target_connection_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "oauth_attempts",
        sa.Column("target_authorization_generation", sa.BigInteger(), nullable=True),
    )
    op.create_check_constraint(
        "ck_oauth_connections_authorization_generation_nonnegative",
        "oauth_connections",
        "authorization_generation >= 0",
    )
    op.create_check_constraint(
        "ck_oauth_attempts_target_generation_pair",
        "oauth_attempts",
        "(target_connection_id IS NULL AND target_authorization_generation IS NULL) OR "
        "(target_connection_id IS NOT NULL AND target_authorization_generation IS NOT NULL "
        "AND target_authorization_generation > 0)",
    )
    op.create_foreign_key(
        "fk_oauth_attempts_target_connection_user",
        "oauth_attempts",
        "oauth_connections",
        ["target_connection_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    """拒绝会丢失旧 state 绑定和代际事实的破坏性回滚。"""
    raise RuntimeError("oauth attempt binding cannot be downgraded safely")
