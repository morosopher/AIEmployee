"""定义供应商中立的 OAuth 连接、能力目录与规范化邮件/日历源映射。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.orm import Mapped, mapped_column

from ai_employee.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _legacy_sync_scope_key(context: DefaultExecutionContext) -> str:
    """仅为仍使用 M1 构造方式的 Gmail/Calendar 游标补上可信 scope。

    M1 的 ``gmail`` 资源始终对应唯一 mailbox，``calendar`` 始终对应 primary calendar。
    其他资源没有可安全推断的范围，必须要求未来调用方显式传入，不能以空字符串制造
    表面合法但不可恢复的游标键。

    Args:
        context: SQLAlchemy 列默认执行上下文，包含当前 INSERT 的显式参数。

    Returns:
        M1 架构可以证明的稳定 scope key。

    Raises:
        ValueError: resource_kind 不是受支持的 M1 兼容值。
    """
    resource_kind = context.get_current_parameters().get("resource_kind")
    if resource_kind == "gmail":
        return "mailbox"
    if resource_kind == "calendar":
        return "primary"
    raise ValueError("scope_key is required for non-M1 sync resources")


class OAuthAttemptModel(UUIDPrimaryKeyMixin, Base):
    """保存一次短生命周期、可原子消费的渐进 OAuth 发起事实。

    ``requested_capabilities`` 只记录本次授权意图；回调仍须以供应商实际返回 scope 更新连接
    能力。OIDC nonce 只保存固定长度摘要，原始 nonce 与 PKCE verifier 明文都不得落库。
    """

    __tablename__ = "oauth_attempts"
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="google",
        server_default=text("'google'"),
    )
    state_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    encrypted_pkce_verifier: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    requested_capabilities: Mapped[list[str]] = mapped_column(
        JSONB(),
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )
    oidc_nonce_hash: Mapped[bytes | None] = mapped_column(LargeBinary(32), nullable=True)


class OAuthConnectionModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存用户拥有的供应商帐号连接，不在本行保存任何明文 token。

    ``(id, user_id)`` 唯一键是能力、日历目录和用户默认连接组合归属外键的引用目标。
    M1 历史连接只能是 Google，因此租户空串和 ``account_type=google`` 是可信兼容默认；
    Microsoft 新连接必须由后续适配器显式写入真实租户与 ``personal``/``work_school`` 类型，
    数据库检查约束阻止显式 Microsoft 行落入 Google 兼容默认。
    """

    __tablename__ = "oauth_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "provider_account_id",
            name="uq_oauth_connections_user_provider_account",
        ),
        UniqueConstraint("id", "user_id", name="uq_oauth_connections_id_user_id"),
        CheckConstraint(
            "provider <> 'microsoft' OR ("
            "btrim(provider_tenant_id) <> '' "
            "AND account_type IN ('personal', 'work_school'))",
            name="ck_oauth_connections_microsoft_identity",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_tenant_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="",
        server_default=text("''"),
    )
    account_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="google",
        server_default=text("'google'"),
    )
    account_email: Mapped[str] = mapped_column(String(320), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ConnectionCapabilityModel(UUIDPrimaryKeyMixin, Base):
    """保存单个连接四类能力之一的本地状态与实际 scope 投影。

    能力直接记录 ``user_id``，组合外键再证明连接属于同一用户；延迟约束允许一个事务内
    按任意顺序创建连接与子记录，但跨用户错配无法提交。``actual_scopes`` 仅保存规范化 scope
    字符串，不保存 token 或供应商完整响应。
    """

    __tablename__ = "connection_capabilities"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "connection_id",
            "capability",
            name="uq_connection_capabilities_user_connection_capability",
        ),
        ForeignKeyConstraint(
            ["connection_id", "user_id"],
            ["oauth_connections.id", "oauth_connections.user_id"],
            name="fk_connection_capabilities_connection_user",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    connection_id: Mapped[UUID] = mapped_column(nullable=False)
    capability: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_scopes: Mapped[list[str]] = mapped_column(JSONB(), nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ProviderCalendarModel(UUIDPrimaryKeyMixin, Base):
    """保存连接可见日历的规范化目录与显式写权限投影。

    ``provider_calendar_id`` 是由对应连接解释的 opaque 字符串；目录行直接归属用户并通过
    组合外键绑定连接拥有者。``can_write`` 必须由同步适配器根据供应商访问角色明确投影，
    未知权限不能默认为可写。
    """

    __tablename__ = "provider_calendars"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "provider_calendar_id",
            name="uq_provider_calendars_connection_provider_calendar",
        ),
        ForeignKeyConstraint(
            ["connection_id", "user_id"],
            ["oauth_connections.id", "oauth_connections.user_id"],
            name="fk_provider_calendars_connection_user",
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    connection_id: Mapped[UUID] = mapped_column(nullable=False)
    provider_calendar_id: Mapped[str] = mapped_column(String(512), nullable=False)
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    access_role: Mapped[str] = mapped_column(String(32), nullable=False)
    can_write: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    provider_url: Mapped[str | None] = mapped_column(Text(), nullable=True)


class EncryptedCredentialModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存一类 OAuth token 的 AEAD 密文，明文只在受控内存中短暂存在。"""

    __tablename__ = "encrypted_credentials"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "credential_kind", name="uq_encrypted_credentials_connection_kind"
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("oauth_connections.id", ondelete="CASCADE"), nullable=False
    )
    credential_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer(), nullable=False)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SyncCursorModel(UUIDPrimaryKeyMixin, Base):
    """保存每个连接、资源类型与精确 scope 的可恢复供应商游标。

    同一连接可以拥有多个 mailbox/folder 或 provider calendar 游标，三列唯一键阻止不同
    范围互相覆盖。Python 默认只兼容 M1 已知的 Gmail mailbox 与 primary calendar；M2 新
    资源必须显式传入 ``scope_key``。
    """

    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "resource_kind",
            "scope_key",
            name="uq_sync_cursors_connection_resource_scope",
        ),
        # 现有 M1 OAuth repository 按旧名字执行 ON CONFLICT；兼容约束也覆盖完整三列，
        # 不会把不同 mailbox/calendar scope 错误视为冲突。
        UniqueConstraint(
            "connection_id",
            "resource_kind",
            "scope_key",
            name="uq_sync_cursors_connection_resource",
        ),
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("oauth_connections.id", ondelete="CASCADE"), nullable=False
    )
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        default=_legacy_sync_scope_key,
    )
    cursor: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class EmailThreadModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存 Gmail 线程的最小展示元数据，不保存原始 MIME。"""

    __tablename__ = "email_threads"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "provider_thread_id",
            name="uq_email_threads_connection_provider_thread",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("oauth_connections.id", ondelete="CASCADE"), nullable=False
    )
    provider_thread_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str] = mapped_column(Text(), nullable=False)
    participants: Mapped[list[dict[str, str]]] = mapped_column(JSONB(), nullable=False)
    latest_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    provider_url: Mapped[str] = mapped_column(Text(), nullable=False)
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class EmailMessageModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存规范化邮件及加密正文；附件与原始供应商响应明确不落库。

    M1 历史邮件均来自唯一 Gmail mailbox，因此 ``mailbox_scope_key`` 的 Python 兼容默认值
    可以安全设为 ``mailbox``。Internet Message-ID、供应商 conversation ID 和发送时间若
    供应商未提供则保持为空，不能由线程 ID 或接收时间伪造。
    """

    __tablename__ = "email_messages"
    __table_args__ = (
        UniqueConstraint(
            "thread_id", "provider_message_id", name="uq_email_messages_thread_provider_message"
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    thread_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_threads.id", ondelete="CASCADE"), nullable=False
    )
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    internet_message_id: Mapped[str | None] = mapped_column(String(998), nullable=True)
    provider_conversation_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mailbox_scope_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        default="mailbox",
    )
    sender: Mapped[dict[str, str]] = mapped_column(JSONB(), nullable=False)
    recipients: Mapped[list[dict[str, str]]] = mapped_column(JSONB(), nullable=False)
    subject: Mapped[str] = mapped_column(Text(), nullable=False)
    snippet: Mapped[str] = mapped_column(Text(), nullable=False)
    # 保留任务必须同时清除三元组；任何一个字段残留都可能形成可恢复的加密材料。
    body_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    body_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12), nullable=True)
    body_key_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    labels: Mapped[list[str]] = mapped_column(JSONB(), nullable=False)
    headers: Mapped[dict[str, str]] = mapped_column(JSONB(), nullable=False)
    provider_url: Mapped[str] = mapped_column(Text(), nullable=False)


class EmailAnalysisModel(UUIDPrimaryKeyMixin, Base):
    """保存确定性或模型邮件分析的可审计摘要。"""

    __tablename__ = "email_analyses"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    thread_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_threads.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    urgency: Mapped[str] = mapped_column(String(32), nullable=False)
    needs_reply: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confidence: Mapped[float] = mapped_column(nullable=False)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB(), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalendarEventModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存供应商日历事件和字段级加密的描述、地点。

    organizer、attendees 与 access role 对历史 M1 行可能未知，因此允许为空。``can_edit``
    是写入授权的只读投影，未知时必须 fail-safe 为 ``False``，后续同步只能依据供应商明确
    返回的权限提升该值。
    """

    __tablename__ = "calendar_events"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "provider_event_id",
            name="uq_calendar_events_connection_provider_event",
        ),
        Index("ix_calendar_events_connection_starts", "connection_id", "starts_at"),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("oauth_connections.id", ondelete="CASCADE"), nullable=False
    )
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    calendar_id: Mapped[str] = mapped_column(String(512), nullable=False)
    title: Mapped[str] = mapped_column(Text(), nullable=False)
    description_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    description_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12), nullable=True)
    description_key_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    location_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    location_nonce: Mapped[bytes | None] = mapped_column(LargeBinary(12), nullable=True)
    location_key_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    all_day: Mapped[bool] = mapped_column(Boolean(), nullable=False)
    transparency: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    recurring_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    organizer: Mapped[dict[str, str] | None] = mapped_column(JSONB(), nullable=True)
    attendees: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB(), nullable=True)
    access_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    can_edit: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    provider_url: Mapped[str] = mapped_column(Text(), nullable=False)
    # Google 的 ``updated`` 是供应商版本事实；最小删除墓碑可不带该字段，故必须可空。
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
