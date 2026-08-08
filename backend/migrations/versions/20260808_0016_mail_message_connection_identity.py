"""把邮件 ImmutableId 身份从 thread 作用域提升为 connection 作用域。

Revision ID: 20260808_0016
Revises: 20260808_0015
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0016"
down_revision: str | Sequence[str] | None = "20260808_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _assert_connection_identity_is_unambiguous() -> None:
    """拒绝对已有连接级重复消息做任意删除或合并。

    旧约束只覆盖 ``thread_id + provider_message_id``，因此同一连接内的 immutable message
    可能因 thread 投影变化形成多行。迁移无法证明哪一行是最新供应商事实；检测到这种状态
    必须让整个 PostgreSQL DDL 事务回滚并交给人工修复，不能静默选择一行导致正文或审计
    关联丢失。错误文本固定且不包含用户、连接、消息或正文值。
    """
    duplicate_exists = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM email_messages "
                "GROUP BY connection_id, provider_message_id "
                "HAVING count(*) > 1 LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if duplicate_exists is not None:
        raise RuntimeError("mail message connection-level duplicate requires manual repair")


def upgrade() -> None:
    """无损回填 direct connection，建立归属外键并切换唯一身份约束。"""
    op.add_column(
        "email_messages",
        sa.Column("connection_id", sa.Uuid(), nullable=True),
    )
    # thread 是 0016 前消息连接归属的唯一可信来源；UPDATE 不猜测 provider 或 scope。
    op.execute(
        "UPDATE email_messages AS message "
        "SET connection_id = thread.connection_id "
        "FROM email_threads AS thread "
        "WHERE message.thread_id = thread.id"
    )
    missing_connection = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM email_messages WHERE connection_id IS NULL LIMIT 1"))
        .scalar_one_or_none()
    )
    if missing_connection is not None:
        raise RuntimeError("mail message connection backfill is incomplete")
    _assert_connection_identity_is_unambiguous()

    op.alter_column(
        "email_messages",
        "connection_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_email_messages_connection_user",
        "email_messages",
        "oauth_connections",
        ["connection_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
        deferrable=True,
        initially="DEFERRED",
    )
    # 新约束先建立，确认所有历史数据满足连接级身份后才移除旧 thread 级约束。
    op.create_unique_constraint(
        "uq_email_messages_connection_provider_message",
        "email_messages",
        ["connection_id", "provider_message_id"],
    )
    op.drop_constraint(
        "uq_email_messages_thread_provider_message",
        "email_messages",
        type_="unique",
    )


def downgrade() -> None:
    """恢复可由 thread 无损推导连接的旧结构，不删除任何邮件事实。"""
    # 连接级唯一性比旧 thread 级约束更严格，因此恢复旧约束不会要求合并或删除数据。
    op.create_unique_constraint(
        "uq_email_messages_thread_provider_message",
        "email_messages",
        ["thread_id", "provider_message_id"],
    )
    op.drop_constraint(
        "uq_email_messages_connection_provider_message",
        "email_messages",
        type_="unique",
    )
    op.drop_constraint(
        "fk_email_messages_connection_user",
        "email_messages",
        type_="foreignkey",
    )
    op.drop_column("email_messages", "connection_id")
