"""允许保留任务抹除邮件正文加密三元组。

Revision ID: 20260804_0010
Revises: 20260804_0009
"""

import sqlalchemy as sa
from alembic import op

revision = "20260804_0010"
down_revision = "20260804_0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """先扩展为可空列，令已存在正文继续可读且后续保留清理可逐批执行。"""
    for name, column_type in (
        ("body_ciphertext", sa.LargeBinary()),
        ("body_nonce", sa.LargeBinary(length=12)),
        ("body_key_version", sa.Integer()),
    ):
        op.alter_column("email_messages", name, existing_type=column_type, nullable=True)


def downgrade() -> None:
    """拒绝破坏性回滚，已清除正文的数据不能伪造为非空加密材料。"""
    raise RuntimeError("retention body redaction migration cannot be downgraded safely")
