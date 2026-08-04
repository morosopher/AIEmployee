"""定义管理员用户及其安全会话的数据库映射。"""

from datetime import datetime, time
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column

from ai_employee.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class UserModel(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """持久化单管理员用户的身份资料与简报偏好。

    本表只保存认证所需的密码哈希，不保存明文密码。``timezone`` 必须是由应用边界验证过的
    IANA 时区名称；``brief_time`` 表示该时区内的墙上时间，不携带宿主机时区推断。
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    locale: Mapped[str] = mapped_column(String(16), nullable=False, default="zh-CN")
    brief_time: Mapped[time] = mapped_column(Time(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    email_body_retention_days: Mapped[int] = mapped_column(SmallInteger(), nullable=False, default=30)
    source_metadata_retention_days: Mapped[int] = mapped_column(SmallInteger(), nullable=False, default=180)
    workspace_history_retention_days: Mapped[int] = mapped_column(SmallInteger(), nullable=False, default=365)

    __table_args__ = (
        CheckConstraint("email_body_retention_days BETWEEN 1 AND 3650", name="ck_users_email_body_retention_days"),
        CheckConstraint("source_metadata_retention_days BETWEEN 1 AND 3650", name="ck_users_source_metadata_retention_days"),
        CheckConstraint("workspace_history_retention_days BETWEEN 1 AND 3650", name="ck_users_workspace_history_retention_days"),
    )


class UserSessionModel(UUIDPrimaryKeyMixin, Base):
    """持久化不可逆会话令牌摘要及会话生命周期。

    ``token_hash`` 与 ``csrf_hash`` 固定为 32 字节摘要，数据库中不落原始 Cookie 或 CSRF
    Token。删除用户时由外键级联清除会话；过期索引支持按用户快速查找和回收会话。
    所有时间字段都必须以带时区 UTC 值写入。
    """

    __tablename__ = "user_sessions"
    __table_args__ = (
        CheckConstraint(
            "octet_length(token_hash) = 32",
            name="ck_user_sessions_token_hash_octet_length_32",
        ),
        CheckConstraint(
            "octet_length(csrf_hash) = 32",
            name="ck_user_sessions_csrf_hash_octet_length_32",
        ),
        Index("ix_user_sessions_user_expires", "user_id", "expires_at"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False, unique=True)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
