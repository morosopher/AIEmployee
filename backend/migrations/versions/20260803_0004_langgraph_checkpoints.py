"""创建 LangGraph PostgreSQL checkpoint 持久表。

Revision ID: 20260803_0004
Revises: 20260803_0003
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260803_0004"
down_revision: str | Sequence[str] | None = "20260803_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """以普通事务 DDL 建立与 AsyncPostgresSaver 兼容的表结构。"""
    op.create_table("checkpoint_migrations", sa.Column("v", sa.Integer(), primary_key=True))
    op.create_table(
        "checkpoints",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("parent_checkpoint_id", sa.Text()),
        sa.Column("type", sa.Text()),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False),
        sa.Column(
            "metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id"),
    )
    op.create_table(
        "checkpoint_blobs",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("blob", sa.LargeBinary()),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "channel", "version"),
    )
    op.create_table(
        "checkpoint_writes",
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), server_default="", nullable=False),
        sa.Column("checkpoint_id", sa.Text(), nullable=False),
        sa.Column("task_id", sa.Text(), nullable=False),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("type", sa.Text()),
        sa.Column("blob", sa.LargeBinary(), nullable=False),
        sa.Column("task_path", sa.Text(), server_default="", nullable=False),
        sa.PrimaryKeyConstraint("thread_id", "checkpoint_ns", "checkpoint_id", "task_id", "idx"),
    )
    op.create_index("ix_checkpoints_thread_id", "checkpoints", ["thread_id"])
    op.create_index("ix_checkpoint_blobs_thread_id", "checkpoint_blobs", ["thread_id"])
    op.create_index("ix_checkpoint_writes_thread_id", "checkpoint_writes", ["thread_id"])


def downgrade() -> None:
    """按依赖逆序移除 checkpoint 表。"""
    op.drop_index("ix_checkpoint_writes_thread_id", table_name="checkpoint_writes")
    op.drop_index("ix_checkpoint_blobs_thread_id", table_name="checkpoint_blobs")
    op.drop_index("ix_checkpoints_thread_id", table_name="checkpoints")
    op.drop_table("checkpoint_writes")
    op.drop_table("checkpoint_blobs")
    op.drop_table("checkpoints")
    op.drop_table("checkpoint_migrations")
