"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

# Alembic 用于建立迁移有向链的稳定标识。
revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """向前应用本版本的 Schema 变更。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """仅在明确操作决策下撤销本版本的 Schema 变更。"""
    ${downgrades if downgrades else "pass"}
