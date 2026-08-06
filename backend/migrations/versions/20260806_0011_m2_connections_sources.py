"""扩展 M2 连接能力、供应商日历、作用域游标与工作设置。

Revision ID: 20260806_0011
Revises: 20260804_0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260806_0011"
down_revision: str | Sequence[str] | None = "20260804_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GOOGLE_GMAIL_READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_GOOGLE_CALENDAR_READ_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
_DEFAULT_WORKING_HOURS = sa.text(
    "'{"
    '"monday":[["09:00","18:00"]],'
    '"tuesday":[["09:00","18:00"]],'
    '"wednesday":[["09:00","18:00"]],'
    '"thursday":[["09:00","18:00"]],'
    '"friday":[["09:00","18:00"]],'
    '"saturday":[],"sunday":[]'
    "}'::jsonb"
)


def _add_oauth_capability_columns() -> None:
    """扩展 OAuth 事实并提供组合归属外键可引用的唯一目标。

    Google 默认值只服务于 0011 以前的历史行和仍使用旧构造方式的 M1 调用方。显式
    Microsoft 连接必须同时携带已验证租户与规范账户类型，不能让这些兼容默认伪造身份。
    """
    op.add_column(
        "oauth_attempts",
        sa.Column("provider", sa.String(length=32), server_default="google", nullable=False),
    )
    op.add_column(
        "oauth_attempts",
        sa.Column(
            "requested_capabilities",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "oauth_attempts",
        sa.Column("oidc_nonce_hash", sa.LargeBinary(length=32), nullable=True),
    )
    op.add_column(
        "oauth_connections",
        sa.Column(
            "provider_tenant_id",
            sa.String(length=255),
            server_default="",
            nullable=False,
        ),
    )
    op.add_column(
        "oauth_connections",
        sa.Column(
            "account_type",
            sa.String(length=32),
            server_default="google",
            nullable=False,
        ),
    )
    op.create_unique_constraint(
        "uq_oauth_connections_id_user_id",
        "oauth_connections",
        ["id", "user_id"],
    )
    op.create_check_constraint(
        "ck_oauth_connections_microsoft_identity",
        "oauth_connections",
        "provider <> 'microsoft' OR ("
        "btrim(provider_tenant_id) <> '' "
        "AND account_type IN ('personal', 'work_school'))",
    )


def _create_connection_child_tables() -> None:
    """创建直接带用户归属且由组合外键阻止跨用户拼接的连接子表。"""
    op.create_table(
        "connection_capabilities",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("capability", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("actual_scopes", postgresql.JSONB(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["connection_id", "user_id"],
            ["oauth_connections.id", "oauth_connections.user_id"],
            name="fk_connection_capabilities_connection_user",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "connection_id",
            "capability",
            name="uq_connection_capabilities_user_connection_capability",
        ),
    )
    op.create_table(
        "provider_calendars",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("provider_calendar_id", sa.String(length=512), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False),
        sa.Column("access_role", sa.String(length=32), nullable=False),
        sa.Column("can_write", sa.Boolean(), nullable=False),
        sa.Column("provider_url", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["connection_id", "user_id"],
            ["oauth_connections.id", "oauth_connections.user_id"],
            name="fk_provider_calendars_connection_user",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id",
            "provider_calendar_id",
            name="uq_provider_calendars_connection_provider_calendar",
        ),
    )


def _scope_existing_cursors_and_sources() -> None:
    """把可证明的 M1 范围显式化，并让未知供应商事实保持为空或只读。"""
    # Graph deltaLink 是无规格长度上限的 opaque URL；VARCHAR(512) 会截断合法恢复位置。
    # 改为 Text 是无损放宽，已有 Gmail/Calendar 游标字节保持不变。
    op.alter_column(
        "sync_cursors",
        "cursor",
        existing_type=sa.String(length=512),
        type_=sa.Text(),
        existing_nullable=True,
    )
    op.add_column(
        "sync_cursors",
        sa.Column("scope_key", sa.String(length=512), nullable=True),
    )
    # M1 只支持单个 Gmail mailbox 和 Google primary calendar；其他 resource_kind 若意外
    # 存在会在非空约束处中止迁移，避免用猜测 scope 覆盖真实游标。
    op.execute(
        "UPDATE sync_cursors SET scope_key = CASE resource_kind "
        "WHEN 'gmail' THEN 'mailbox' WHEN 'calendar' THEN 'primary' ELSE NULL END"
    )
    op.alter_column(
        "sync_cursors",
        "scope_key",
        existing_type=sa.String(length=512),
        nullable=False,
    )
    op.drop_constraint(
        "uq_sync_cursors_connection_resource",
        "sync_cursors",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_sync_cursors_connection_resource_scope",
        "sync_cursors",
        ["connection_id", "resource_kind", "scope_key"],
    )
    # 现有 M1 OAuth repository 按旧名字执行 ``ON CONFLICT ON CONSTRAINT``；兼容约束沿用
    # 该名字但同样覆盖三列，因此不会恢复会阻断多 scope 的两列唯一语义。
    op.create_unique_constraint(
        "uq_sync_cursors_connection_resource",
        "sync_cursors",
        ["connection_id", "resource_kind", "scope_key"],
    )

    op.add_column(
        "email_messages",
        sa.Column("internet_message_id", sa.String(length=998), nullable=True),
    )
    op.add_column(
        "email_messages",
        sa.Column("provider_conversation_id", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "email_messages",
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "email_messages",
        sa.Column("mailbox_scope_key", sa.String(length=512), nullable=True),
    )
    # 0010 以前的邮件全部来自唯一 Gmail mailbox，因此该回填是已有架构事实而非推断。
    op.execute("UPDATE email_messages SET mailbox_scope_key = 'mailbox'")
    op.alter_column(
        "email_messages",
        "mailbox_scope_key",
        existing_type=sa.String(length=512),
        nullable=False,
    )

    op.add_column(
        "calendar_events",
        sa.Column("organizer", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column("attendees", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column("access_role", sa.String(length=32), nullable=True),
    )
    # 目录、用户默认值和事件必须保存同一个 opaque calendar ID；三处统一为 512，避免
    # 目录同步成功后事件因旧 M1 上限拒绝同一供应商标识符。
    op.alter_column(
        "calendar_events",
        "calendar_id",
        existing_type=sa.String(length=255),
        type_=sa.String(length=512),
        existing_nullable=False,
    )
    op.add_column(
        "calendar_events",
        sa.Column(
            "can_edit",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )


def _add_user_work_settings() -> None:
    """增加默认连接、opaque 日历 ID 和固定合成工作时间配置。"""
    op.add_column(
        "users",
        sa.Column("default_mail_connection_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("default_calendar_connection_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("default_calendar_id", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "working_hours",
            postgresql.JSONB(),
            server_default=_DEFAULT_WORKING_HOURS,
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "meeting_buffer_minutes",
            sa.SmallInteger(),
            server_default=sa.text("10"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_users_meeting_buffer_minutes",
        "users",
        "meeting_buffer_minutes BETWEEN 0 AND 120",
    )
    # 两个 guard 都包含 users.id，从数据库层证明默认连接属于同一个用户；延迟校验用于
    # 处理 users 与 oauth_connections 的循环归属创建顺序，而不放宽最终提交不变量。
    op.create_foreign_key(
        "fk_users_default_mail_connection_id_user_id",
        "users",
        "oauth_connections",
        ["default_mail_connection_id", "id"],
        ["id", "user_id"],
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        "fk_users_default_calendar_connection_id_user_id",
        "users",
        "oauth_connections",
        ["default_calendar_connection_id", "id"],
        ["id", "user_id"],
        deferrable=True,
        initially="DEFERRED",
    )


def _backfill_google_capabilities() -> None:
    """按保存的精确 Google read scope 回填四种能力，写能力始终保持关闭。"""
    op.execute(
        sa.text(
            "INSERT INTO connection_capabilities ("
            "user_id, connection_id, capability, status, actual_scopes, "
            "last_verified_at, last_error_code, id"
            ") SELECT connection.user_id, connection.id, capability.name, "
            "CASE "
            "WHEN capability.name = 'mail.read' "
            "AND connection.scopes @> CAST(:gmail_read_scope AS jsonb) THEN 'enabled' "
            "WHEN capability.name = 'calendar.read' "
            "AND connection.scopes @> CAST(:calendar_read_scope AS jsonb) THEN 'enabled' "
            "ELSE 'disabled' END, "
            "normalized.actual_scopes, NULL, NULL, gen_random_uuid() "
            "FROM oauth_connections AS connection "
            "CROSS JOIN (VALUES ('mail.read'), ('mail.send'), "
            "('calendar.read'), ('calendar.write')) AS capability(name) "
            "CROSS JOIN LATERAL ("
            "SELECT COALESCE(jsonb_agg(scope_value ORDER BY scope_value), '[]'::jsonb) "
            "AS actual_scopes FROM ("
            "SELECT DISTINCT jsonb_array_elements_text(connection.scopes) AS scope_value"
            ") AS scope_values"
            ") AS normalized "
            "WHERE connection.provider = 'google' "
            "ON CONFLICT ON CONSTRAINT "
            "uq_connection_capabilities_user_connection_capability DO NOTHING"
        ).bindparams(
            gmail_read_scope=f'["{_GOOGLE_GMAIL_READ_SCOPE}"]',
            calendar_read_scope=f'["{_GOOGLE_CALENDAR_READ_SCOPE}"]',
        )
    )


def upgrade() -> None:
    """按可引用父对象、子表、源事实和用户反向引用的顺序前向扩展。"""
    _add_oauth_capability_columns()
    _create_connection_child_tables()
    _scope_existing_cursors_and_sources()
    _add_user_work_settings()
    _backfill_google_capabilities()


def downgrade() -> None:
    """拒绝破坏性回滚，避免丢失新增能力、分作用域游标与用户工作设置事实。"""
    raise RuntimeError("M2 connection and source schema cannot be downgraded safely")
