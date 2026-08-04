"""定义 Google OAuth 凭据与已规范化只读源数据的 ORM 映射。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ai_employee.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class OAuthAttemptModel(UUIDPrimaryKeyMixin, Base):
    """保存一次短生命周期、可原子消费的 OAuth state 与加密 PKCE verifier。"""

    __tablename__ = "oauth_attempts"
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


class OAuthConnectionModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """保存用户拥有的 Google 帐号连接，不在本行保存任何明文 token。"""

    __tablename__ = "oauth_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "provider",
            "provider_account_id",
            name="uq_oauth_connections_user_provider_account",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_email: Mapped[str] = mapped_column(String(320), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


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
    """保存每个资源的可恢复供应商游标与脱敏同步结果。"""

    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "resource_kind", name="uq_sync_cursors_connection_resource"
        ),
    )
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("oauth_connections.id", ondelete="CASCADE"), nullable=False
    )
    resource_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    cursor: Mapped[str | None] = mapped_column(String(512), nullable=True)
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
    """保存规范化邮件及加密正文；附件与原始供应商响应明确不落库。"""

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
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sender: Mapped[dict[str, str]] = mapped_column(JSONB(), nullable=False)
    recipients: Mapped[list[dict[str, str]]] = mapped_column(JSONB(), nullable=False)
    subject: Mapped[str] = mapped_column(Text(), nullable=False)
    snippet: Mapped[str] = mapped_column(Text(), nullable=False)
    body_ciphertext: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    body_nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    body_key_version: Mapped[int] = mapped_column(Integer(), nullable=False)
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
    """保存 Google Calendar 事件和字段级加密的描述、地点。"""

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
    calendar_id: Mapped[str] = mapped_column(String(255), nullable=False)
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
    provider_url: Mapped[str] = mapped_column(Text(), nullable=False)
