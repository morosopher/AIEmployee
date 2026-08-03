"""校正已升级环境的 LangGraph checkpoint 供应商迁移契约。

Revision ID: 20260803_0005
Revises: 20260803_0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260803_0005"
down_revision: str | Sequence[str] | None = "20260803_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """让历史 0004 安装也与当前 AsyncPostgresSaver 的迁移清单完全一致。

    早期 0004 使用项目自定义索引名且没有标记供应商版本；此迁移只处理该已发布
    历史，不承载业务数据变更。写入全部供应商版本后，Worker 运行期的 ``setup`` 不会
    再重复执行 DDL。索引改名以删除旧索引并创建等价规范索引实现，避免同一列重复索引。
    """
    op.execute("DROP INDEX IF EXISTS ix_checkpoints_thread_id")
    op.execute("DROP INDEX IF EXISTS ix_checkpoint_blobs_thread_id")
    op.execute("DROP INDEX IF EXISTS ix_checkpoint_writes_thread_id")
    op.execute("CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx ON checkpoints (thread_id)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx ON checkpoint_blobs (thread_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes (thread_id)"
    )
    op.execute(
        "INSERT INTO checkpoint_migrations (v) "
        "SELECT generate_series(0, 9) ON CONFLICT (v) DO NOTHING"
    )


def downgrade() -> None:
    """恢复旧索引名并移除供应商版本记录，仅供开发环境回退。"""
    op.execute("DROP INDEX IF EXISTS checkpoints_thread_id_idx")
    op.execute("DROP INDEX IF EXISTS checkpoint_blobs_thread_id_idx")
    op.execute("DROP INDEX IF EXISTS checkpoint_writes_thread_id_idx")
    op.execute("CREATE INDEX ix_checkpoints_thread_id ON checkpoints (thread_id)")
    op.execute("CREATE INDEX ix_checkpoint_blobs_thread_id ON checkpoint_blobs (thread_id)")
    op.execute("CREATE INDEX ix_checkpoint_writes_thread_id ON checkpoint_writes (thread_id)")
    op.execute("DELETE FROM checkpoint_migrations")
