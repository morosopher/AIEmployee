"""定义现有连接唯一自动 OAuth grant admission 与显式恢复共享 lease 边界。

自动源仅为 provider_refresh/calendar_aad_preflight。provider 调用之前必须已提交 started；
任何未知结果只能只读核对，不可被 Taskiq 或 adapter 的临时重试重新发送。
"""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from ai_employee.application.calendar_aad_digests import CredentialSnapshot
from ai_employee.application.ports.credential_rotation import (
    AutomaticRefreshSource,
    OAuthRefreshAuditRecord,
    OAuthRefreshResultV1,
    OAuthRefreshStartedV1,
)
from ai_employee.application.ports.oauth import OAuthProvider, OAuthTokenSet
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import UserActionRequiredError


class OAuthRefreshError(UserActionRequiredError):
    """稳定、不可自动重放的凭据或 fence 失败，不携带 token/供应商异常原文。"""

    def __init__(self, error_code: str = "oauth_refresh_result_unknown") -> None:
        """只接受固定稳定码；状态未知不能包装为普通 TransientProviderError。"""
        if error_code not in {
            "oauth_refresh_result_unknown",
            "oauth_credential_state_conflict",
            "oauth_refresh_locked",
        }:
            raise ValueError("OAuth refresh error code is invalid")
        super().__init__(
            error_code=error_code, message="OAuth credential refresh requires attention"
        )


@dataclass(frozen=True, slots=True)
class OAuthRefreshRequest:
    """精确绑定自动调用的用户、连接与能力，不接受供应商默认账户或原始 scope。"""

    user_id: UUID
    connection_id: UUID
    capability: ConnectionCapability
    source: AutomaticRefreshSource = "provider_refresh"
    rollout_digest_v1: str | None = None
    expected_access: CredentialSnapshot | None = field(default=None, repr=False)
    attempt_id: UUID | None = None

    def __post_init__(self) -> None:
        """阻止宽松 caller 通过非法来源、跨连接旧快照或缺少 rollout 身份绕过 admission。"""
        if (
            not isinstance(self.user_id, UUID)
            or not isinstance(self.connection_id, UUID)
            or type(self.capability) is not ConnectionCapability
        ):
            raise ValueError("OAuth refresh ownership is invalid")
        if self.source not in {"provider_refresh", "calendar_aad_preflight"}:
            raise ValueError("OAuth automatic source is invalid")
        if self.attempt_id is not None and not isinstance(self.attempt_id, UUID):
            raise ValueError("OAuth refresh attempt ID is invalid")
        if self.source == "provider_refresh" and self.rollout_digest_v1 is not None:
            raise ValueError("ordinary refresh cannot bind a rollout")
        if self.source == "calendar_aad_preflight" and (
            self.rollout_digest_v1 is None
            or self.capability is not ConnectionCapability.CALENDAR_READ
        ):
            raise ValueError("preflight refresh requires calendar rollout binding")
        if self.expected_access is not None and (
            self.expected_access.user_id != self.user_id
            or self.expected_access.connection_id != self.connection_id
            or self.expected_access.credential_kind != "access_token"
        ):
            raise ValueError("rejected access snapshot ownership is invalid")


@dataclass(frozen=True, slots=True)
class OAuthRefreshSnapshot:
    """在固定锁序下冻结 generation、完整两行与 canonical scope；仅在应用内存中使用。"""

    user_id: UUID
    connection_id: UUID
    provider: OAuthProvider
    authorization_generation: int
    scopes: frozenset[str] = field(repr=False)
    access: CredentialSnapshot = field(repr=False)
    refresh: CredentialSnapshot = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthRefreshClaim:
    """已提交 started 的一次自动 grant；旧 plaintext 只在受控内存停留。"""

    request: OAuthRefreshRequest
    snapshot: OAuthRefreshSnapshot
    started: OAuthRefreshAuditRecord
    fence: OAuthRefreshStartedV1 = field(repr=False)
    refresh_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class OAuthRefreshReady:
    """独立 current-readiness 的当前事实；返回值不能进入 API、日志或 Taskiq 载荷。"""

    snapshot: OAuthRefreshSnapshot
    access_token: str = field(repr=False)
    refreshed: bool = False
    attempt_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class OAuthRefreshClosedResult:
    """持久历史关闭证明与原事件关联，故意不包含 current credential 判断。"""

    automatic: OAuthRefreshAuditRecord
    recovery: OAuthRefreshAuditRecord | None
    result: OAuthRefreshAuditRecord
    metadata: OAuthRefreshResultV1 = field(repr=False)


class OAuthRefreshProvider(Protocol):
    """窄 provider-neutral refresh 端口；网络响应在集成层先规范化。"""

    provider: OAuthProvider

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """提供固定能力 scope 映射，不包含联系人、草稿箱或应用权限。"""
        ...

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """恰好进行一次 grant；适配器不得自动重放未知写结果。"""
        ...


class OAuthRefreshLease(Protocol):
    """在网络期间保持的 connection-scoped PostgreSQL session advisory lease。"""

    async def assert_owned(self) -> None:
        """在同一个 session 检查存活与实际锁所有权，禁止重入 acquire。"""
        ...


class OAuthRefreshCoordinator(Protocol):
    """唯一自动 grant admission，同时为 Task27B 提供不创建 automatic started 的共享 lease。"""

    async def refresh(
        self, request: OAuthRefreshRequest, provider: OAuthRefreshProvider
    ) -> OAuthRefreshReady:
        """提交 durable started、一次 provider 调用、完整 CAS；ACK 丢失时只读结果核对。"""
        ...

    async def read_current(self, request: OAuthRefreshRequest) -> OAuthRefreshReady:
        """独立校验当前凭据、能力与 identity rollback；未决 fence 阻断自动调用。"""
        ...

    async def read_result(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        attempt_id: UUID,
        recovery: bool = False,
    ) -> OAuthRefreshClosedResult | None:
        """用新 session 返回允许关闭该 automatic/recovery attempt 的严格结果联合。"""
        ...

    def explicit_recovery_lease(
        self, *, user_id: UUID, connection_id: UUID
    ) -> AbstractAsyncContextManager[OAuthRefreshLease]:
        """显式授权码恢复使用同一 connection lease，但不开始第二次自动 refresh。"""
        ...
