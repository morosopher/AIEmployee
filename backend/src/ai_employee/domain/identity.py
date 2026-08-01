"""定义管理员身份与安全会话的纯领域值对象。"""

from dataclasses import dataclass
from datetime import datetime, time
from uuid import UUID

# 会话 TTL 下限阻止立即失效或负向生命周期，上限限制长期 Cookie 与会话暴露窗口，
# 同时避免未经配置边界的超大 timedelta 扩大 datetime 和 Cookie Max-Age 边界风险。
MIN_SESSION_TTL_SECONDS = 1
MAX_SESSION_TTL_SECONDS = 365 * 24 * 60 * 60


def normalize_email(email: str) -> str:
    """规范化管理员邮箱以形成稳定且幂等的身份键。

    Args:
        email: 来自登录或管理员创建边界的邮箱文本。

    Returns:
        去除边界空白并执行 Unicode 大小写折叠后的邮箱。
    """
    return email.strip().casefold()


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """可安全返回给 API 调用方的管理员身份资料。"""

    id: UUID
    email: str
    display_name: str
    timezone: str
    locale: str
    brief_time: time


@dataclass(frozen=True, slots=True)
class UserCredential:
    """仅供认证用例使用的用户身份、密码哈希与激活状态。"""

    identity: UserIdentity
    password_hash: str | None
    is_active: bool


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """不含原始 Token 的持久会话领域快照。"""

    id: UUID
    user_id: UUID
    csrf_hash: bytes
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    revoked_at: datetime | None

    def is_active_at(self, now: datetime) -> bool:
        """判断会话在给定 UTC 时刻是否仍可用于认证。

        Args:
            now: 由调用方注入的显式 UTC 当前时间。

        Returns:
            未撤销且过期时间严格晚于当前时间时返回 ``True``。
        """
        return self.revoked_at is None and self.expires_at > now


@dataclass(frozen=True, slots=True)
class SessionAuthenticationRecord:
    """Repository 返回的用户凭据与会话联合认证记录。"""

    user: UserCredential
    session: SessionRecord


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """认证成功后供 API 授权与 CSRF 校验使用的最小上下文。"""

    user: UserIdentity
    session: SessionRecord


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """会话管理接口可公开的非敏感生命周期元数据。"""

    id: UUID
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class ListedSession:
    """在会话摘要上标识其是否为当前请求会话。"""

    id: UUID
    created_at: datetime
    expires_at: datetime
    last_seen_at: datetime
    is_current: bool


@dataclass(frozen=True, slots=True)
class NewAdmin:
    """管理员创建用例交给持久化端口的完整且已验证身份数据。"""

    email: str
    display_name: str
    password_hash: str
    timezone: str
    locale: str
    brief_time: time
