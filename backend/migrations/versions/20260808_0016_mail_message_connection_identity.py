"""扩展邮件连接身份与供应商版本列，并无损回填历史归属。

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

_BACKFILL_BATCH_SIZE = 1_000


def _column_exists(table_name: str, column_name: str) -> bool:
    """检查 expand 列是否已存在，使失败后重试不会重复执行 DDL。"""
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table_name AND column_name = :column_name"
                ")"
            ),
            {"table_name": table_name, "column_name": column_name},
        )
        .scalar()
    )


def _assert_connection_identity_is_unambiguous() -> None:
    """拒绝缺失、重复或跨用户归属异常，绝不任意删除/合并。

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
    ownership_mismatch = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM email_messages AS message "
                "JOIN email_threads AS thread ON thread.id = message.thread_id "
                "LEFT JOIN oauth_connections AS connection ON connection.id = thread.connection_id "
                "WHERE message.user_id <> thread.user_id "
                "OR connection.id IS NULL "
                "OR thread.user_id <> connection.user_id "
                "OR message.connection_id <> thread.connection_id "
                "LIMIT 1"
            )
        )
        .scalar_one_or_none()
    )
    if ownership_mismatch is not None:
        raise RuntimeError("mail message ownership mismatch requires manual repair")


def upgrade() -> None:
    """只执行 expand：增加 nullable 列、回填并 fail closed，保留全部旧约束。"""
    if not _column_exists("email_messages", "connection_id"):
        op.add_column(
            "email_messages",
            sa.Column("connection_id", sa.Uuid(), nullable=True),
        )
    if not _column_exists("email_messages", "provider_updated_at"):
        op.add_column(
            "email_messages",
            sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        )
    # 进入 AUTOCOMMIT 前 Alembic 会先提交 metadata-only nullable expand；随后每个 UPDATE
    # 都是独立事务，最多锁定一个固定批次并在下一批前释放。若重复/归属校验失败，已完成
    # 回填与 nullable 列安全保留，revision 仍停在 0015；人工修复后可重跑且不会重复加列。
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        while True:
            result = bind.execute(
                sa.text(
                    "WITH batch AS ("
                    "SELECT message.id, thread.connection_id "
                    "FROM email_messages AS message "
                    "JOIN email_threads AS thread ON thread.id = message.thread_id "
                    "WHERE message.connection_id IS NULL "
                    "ORDER BY message.id "
                    "LIMIT :batch_size "
                    "FOR UPDATE OF message SKIP LOCKED"
                    ") UPDATE email_messages AS message "
                    "SET connection_id = batch.connection_id "
                    "FROM batch WHERE message.id = batch.id "
                    "RETURNING message.id"
                ),
                {"batch_size": _BACKFILL_BATCH_SIZE},
            )
            if not result.fetchall():
                break
    missing_connection = (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM email_messages WHERE connection_id IS NULL LIMIT 1"))
        .scalar_one_or_none()
    )
    if missing_connection is not None:
        raise RuntimeError("mail message connection backfill is incomplete")
    _assert_connection_identity_is_unambiguous()


def downgrade() -> None:
    """移除 expand 列；旧约束从未删除，因此 downgrade 不触碰任何业务行。"""
    op.drop_column("email_messages", "provider_updated_at")
    op.drop_column("email_messages", "connection_id")
