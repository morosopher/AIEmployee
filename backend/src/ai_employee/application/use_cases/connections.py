"""编排供应商中立 OAuth、连接能力、断开与手动同步。"""

import base64
import binascii
import hashlib
import logging
import secrets
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Protocol, cast, get_args
from uuid import UUID, uuid4

from ai_employee.application.oauth_refresh_identity import OAuthRefreshIdentity
from ai_employee.application.ports.credential_rotation import (
    CredentialReplacedV1,
    CredentialRotationRepository,
    OAuthRefreshAuditRecord,
    RecoveryCapabilityTransition,
    RecoveryFailureCode,
    RecoveryUnsatisfiedV1,
)
from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.application.ports.oauth import (
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthPostExchangeVerificationError,
    OAuthProvider,
    OAuthProviderAdapter,
    OAuthRevocationResult,
    OAuthRevocationStatus,
    OAuthTokenSet,
)
from ai_employee.application.ports.oauth_refresh import (
    OAuthRecoveryAuthorization,
    OAuthRecoveryCandidate,
    OAuthRecoveryClaim,
    OAuthRefreshCoordinator,
    OAuthRefreshError,
    OAuthRefreshLease,
    OAuthRefreshRequest,
)
from ai_employee.application.use_cases.tasks import CreateTaskBatchItem
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    ConnectionCapabilityDependencyConflict,
    ConnectionStatus,
    validate_capability_enable,
)
from ai_employee.domain.errors import (
    DomainError,
    StateConflictError,
    TransientProviderError,
)

OAUTH_ATTEMPT_TTL = timedelta(minutes=10)
OAUTH_AUTHORIZATION_FAILED_ERROR_CODE: RecoveryFailureCode = "oauth_authorization_failed"
_AUTHORIZATION_FAILURE_LOGGER = logging.getLogger("ai_employee.oauth.authorization_failed")
_READ_CAPABILITIES = frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.CALENDAR_READ})


class Clock(Protocol):
    """定义连接流程所需的显式 UTC 时钟，测试可注入可控实现。"""

    def now(self) -> datetime:
        """返回带时区 UTC 时间。"""
        ...


@dataclass(frozen=True, slots=True)
class StoredConnection:
    """应用层可读取的连接投影，不包含 ORM 对象或凭据。"""

    id: UUID
    user_id: UUID
    provider: str
    provider_account_id: str
    provider_tenant_id: str
    account_type: str
    account_email: str
    scopes: tuple[str, ...]
    status: str
    last_error_code: str | None
    # 仅渐进授权使用的单调代际；首次连接和旧测试投影默认为零。
    authorization_generation: int = 0


@dataclass(frozen=True, slots=True)
class StoredCapability:
    """单项连接能力的规范化持久状态。"""

    capability: ConnectionCapability
    status: CapabilityStatus
    actual_scopes: tuple[str, ...]
    last_verified_at: datetime | None
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class OAuthRevokeBacklog:
    """供应商级未决撤销计数；不暴露连接、用户或任何令牌材料。"""

    provider: str
    unresolved_count: int


@dataclass(frozen=True, slots=True)
class StoredProviderCalendar:
    """供应商日历目录的显式公开投影，不包含 cursor 或原始 JSON。"""

    id: str
    name: str
    timezone: str
    is_primary: bool
    access_role: str
    can_write: bool
    provider_url: str | None


@dataclass(frozen=True, slots=True)
class ConnectionCapabilitySnapshot:
    """一个连接的能力与日历目录快照。"""

    connection_id: UUID
    provider: str
    capabilities: tuple[StoredCapability, ...]
    provider_calendars: tuple[StoredProviderCalendar, ...]


@dataclass(frozen=True, slots=True)
class ConsumedOAuthAttempt:
    """一次性 state 消费后可交给 OAuth callback 的最小事实。"""

    id: UUID
    user_id: UUID
    provider: str
    requested_capabilities: frozenset[ConnectionCapability]
    verifier: EncryptedValue
    oidc_nonce_hash: bytes | None
    target_connection_id: UUID | None = None
    target_authorization_generation: int | None = None


class ConnectionStore(Protocol):
    """限定连接用例可使用的持久化操作，并要求每项查询显式带用户。"""

    async def create_attempt(
        self,
        *,
        attempt_id: UUID,
        user_id: UUID,
        provider: str,
        state_hash: bytes,
        verifier: EncryptedValue,
        requested_capabilities: frozenset[ConnectionCapability],
        oidc_nonce_hash: bytes | None,
        expires_at: datetime,
        created_at: datetime,
        target_connection_id: UUID | None = None,
        target_authorization_generation: int | None = None,
    ) -> UUID:
        """保存不含明文 state、verifier 或 nonce 的 OAuth 尝试。"""
        ...

    async def consume_attempt(
        self,
        *,
        state_hash: bytes,
        now: datetime,
    ) -> ConsumedOAuthAttempt | None:
        """锁定并一次性消费有效 state。"""
        ...

    async def record_authorization_failed(
        self,
        *,
        user_id: UUID,
        attempt_id: UUID,
        provider: str,
        occurred_at: datetime,
        error_code: RecoveryFailureCode = OAUTH_AUTHORIZATION_FAILED_ERROR_CODE,
    ) -> None:
        """在消费 state 的同一事务追加固定失败事实，不接受供应商原始错误或描述。"""
        ...

    async def get_refresh_events(
        self, *, user_id: UUID, connection_id: UUID
    ) -> tuple[OAuthRefreshAuditRecord, ...]:
        """读取共享 refresh 协议事实，未装配恢复服务的旧调用方也不能忽略已有 fence。"""
        ...

    def credential_rotation(
        self, *, cipher: Encryption, identity: OAuthRefreshIdentity
    ) -> CredentialRotationRepository:
        """返回绑定当前同一短事务的唯一 credential CAS writer，不拥有独立 session。"""
        ...

    async def validate_unbound_attempt_for_callback(
        self,
        *,
        attempt_id: UUID,
        user_id: UUID,
        provider: str,
    ) -> None:
        """在保存首次 OAuth 结果前重新锁定并核对持久失效事实。"""
        ...

    async def get_connection_for_update(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> StoredConnection | None:
        """在当前事务先同步所属 user 再锁定目标连接，保持能力/代际及后续外键写入锁序。"""
        ...

    async def ensure_connection(
        self,
        *,
        user_id: UUID,
        provider: str,
        provider_account_id: str,
        provider_tenant_id: str,
        account_type: str,
        account_email: str,
        scopes: frozenset[str],
        identity_key_version: int | None = None,
    ) -> UUID:
        """按规范账户键建立或合并连接，并确保四项能力行存在。

        同一用户的 connected 同身份 callback 只有在新 token scope 覆盖当前 scope 时才
        原样替换并保留未请求能力；新建或非 connected 连接从本次 token 事实精确重置，
        tenant/type 或 scope 单调性不满足时拒绝。
        """
        ...

    async def validate_unfenced_identity_for_callback(
        self,
        *,
        user_id: UUID,
        provider: str,
        provider_account_id: str,
        identity_key_version: int,
    ) -> None:
        """在无目标 callback 进入合并流程前锁定规范身份并检查自动刷新 fence。

        Args:
            user_id: 从已消费 OAuthAttempt 取得的归属用户。
            provider: 当前 attempt 绑定的规范供应商。
            provider_account_id: adapter 验证后的稳定账户键。
            identity_key_version: 当前共享身份服务的固定根密钥版本。

        Raises:
            OAuthRefreshError: 身份命中有未关闭 fence 的 connected 连接，或审计不合法。
        """
        ...

    async def update_connection_scopes(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        scopes: frozenset[str],
    ) -> None:
        """更新已绑定渐进连接的规范化实际 scope，并再次核对用户归属。"""
        ...

    async def ensure_google_capability_rows(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> None:
        """为旧 Google 连接幂等补齐能力行，不覆盖已有人工状态。"""
        ...

    async def save_connection_tokens(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        access_token: EncryptedValue,
        refresh_token: EncryptedValue | None,
        expires_at: datetime,
    ) -> None:
        """仅为当前用户 connected 连接保存 token；refresh 为 ``None`` 时保留既有密文。"""
        ...

    async def save_capability_state(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
        status: CapabilityStatus,
        actual_scopes: frozenset[str],
        last_verified_at: datetime | None,
        last_error_code: str | None,
    ) -> None:
        """以替换语义保存一项能力的实际 scope 与状态。"""
        ...

    async def set_capabilities_authorizing(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capabilities: frozenset[ConnectionCapability],
    ) -> int:
        """把本次授权并集置为 authorizing，并原子递增授权代际。"""
        ...

    async def mark_progressive_authorization_failed(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        authorization_generation: int,
        capabilities: frozenset[ConnectionCapability],
        error_code: str,
    ) -> RecoveryCapabilityTransition:
        """同代际只更新本次 authorizing 状态；返回真实 action_required 或 stale_target_noop。"""
        ...

    async def ensure_bound_connection_for_callback(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        authorization_generation: int,
        provider: str,
        provider_account_id: str,
        provider_tenant_id: str,
        account_type: str,
        identity_key_version: int | None = None,
    ) -> UUID:
        """锁定并核对渐进 OAuth 的冻结目标，失败时不得产生任何 token 写入。"""
        ...

    async def disable_capability(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
    ) -> None:
        """仅本地关闭一项能力；Task 25 再补未认领动作失效。"""
        ...

    async def get_enabled_capabilities(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> frozenset[ConnectionCapability] | None:
        """返回当前 enabled 集合；连接不存在或跨用户时返回 ``None``。"""
        ...

    async def get_capability_snapshot(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> ConnectionCapabilitySnapshot | None:
        """返回用户范围能力/日历投影。"""
        ...

    async def list_connections(self, *, user_id: UUID) -> tuple[StoredConnection, ...]:
        """列出当前用户连接。"""
        ...

    async def get_connection(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> StoredConnection | None:
        """按用户与连接主键读取一行。"""
        ...

    async def disconnect(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        invalidated_at: datetime,
    ) -> tuple[bool, EncryptedValue | None]:
        """标记首次 state 失效、删除本地凭据并返回待尽力撤销的 refresh token 密文。"""
        ...

    async def record_oauth_revoke_unresolved(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        provider: str,
        error_code: str,
        occurred_at: datetime,
    ) -> None:
        """在本地凭据已删除后追加无内容的供应商撤销未决事实。"""
        ...

    async def get_oauth_revoke_backlog(self) -> tuple[OAuthRevokeBacklog, ...]:
        """聚合仍未被操作者补救的撤销维护事实，不创建网络重试任务。"""
        ...

    async def record_oauth_revoke_remediation(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        unresolved_event_id: int,
        occurred_at: datetime,
    ) -> bool:
        """追加当前用户对精确未决撤销的补救事实；跨用户、缺失或重复确认返回 False。"""
        ...


class ConnectionStoreFactory(Protocol):
    """为一次连接用例提供事务边界，不让路由或用例接触 ORM。"""

    def __call__(self) -> AbstractAsyncContextManager[ConnectionStore]:
        """创建自动提交或回滚的存储上下文。"""
        ...


class OAuthRevokeMaintenanceUseCase:
    """为 Scheduler 和操作者提供无令牌的撤销维护能力。

    本用例没有 OAuth adapter 或 Encryption 依赖。扫描只能计数，人工补救只能追加当前
    用户对精确审计事件的确认；无法构造恢复 token 或再次调用 revoke 的任务。
    """

    def __init__(self, stores: ConnectionStoreFactory, clock: Clock) -> None:
        """保存短事务工厂与事实时间来源。"""
        self._stores = stores
        self._clock = clock

    async def scan(self) -> tuple[OAuthRevokeBacklog, ...]:
        """返回各供应商仍未处理的撤销数量，供周期告警使用。"""
        async with self._stores() as store:
            return await store.get_oauth_revoke_backlog()

    async def record_remediation(
        self, *, user_id: UUID, connection_id: UUID, unresolved_event_id: int
    ) -> bool:
        """记录操作者已在供应商侧完成补救的确认，不接受自由文本或外部凭据。

        Args:
            user_id: 当前已认证操作者，同时作为连接归属条件。
            connection_id: 需要补救的精确连接。
            unresolved_event_id: ``oauth.revoke_unresolved`` 的正整数审计游标。

        Returns:
            首次追加确认时为 True；无权限、事实不存在或已确认时为 False。
        """
        if type(unresolved_event_id) is not int or unresolved_event_id <= 0:
            raise ValueError("unresolved_event_id must be positive")
        async with self._stores() as store:
            return await store.record_oauth_revoke_remediation(
                user_id=user_id,
                connection_id=connection_id,
                unresolved_event_id=unresolved_event_id,
                occurred_at=_utc_now(self._clock),
            )


class TaskCreator(Protocol):
    """定义手动同步创建两个可恢复任务的最小端口。"""

    async def execute_many(
        self,
        *,
        user_id: UUID,
        items: tuple[CreateTaskBatchItem, ...],
    ) -> tuple["CreatedTask", ...]:
        """原子创建邮件和日历同步任务。"""
        ...


class CreatedTask(Protocol):
    """定义同步接口需要读取的已创建任务标识。"""

    @property
    def task_id(self) -> UUID:
        """返回任务创建后可安全公开的稳定 UUID。"""
        ...


@dataclass(frozen=True, slots=True)
class ConnectionSummary:
    """API 可以公开的连接元数据，刻意排除 token、nonce 和供应商错误细节。"""

    id: UUID
    provider: str
    account_email: str
    scopes: tuple[str, ...]
    status: str
    last_error_code: str | None


@dataclass(frozen=True, slots=True)
class OAuthStartResult:
    """返回浏览器重定向用 URL，不单独公开一次性 state。"""

    authorization_url: str


@dataclass(frozen=True, slots=True)
class CapabilityEnableResult:
    """返回渐进授权 URL 与确定性排序后的完整能力并集。"""

    authorization_url: str
    requested_capabilities: tuple[ConnectionCapability, ...]


@dataclass(frozen=True, slots=True)
class CapabilityDisableResult:
    """返回本地关闭后的精确能力状态。"""

    capability: ConnectionCapability
    status: CapabilityStatus


@dataclass(frozen=True, slots=True)
class ManualSyncResult:
    """包含邮件与 Calendar 各一个稳定任务标识的异步接受结果。"""

    gmail_task_id: UUID
    calendar_task_id: UUID


class OAuthStateRejectedError(Exception):
    """表示 state 格式非法、不存在、已消费或已过期，统一映射为不泄露细节的 400。"""


class ConnectionNotFoundError(Exception):
    """表示指定连接不存在或不属于当前认证用户。"""


class ConnectionIdentityConflictError(StateConflictError):
    """表示同一规范供应商账户键试图改绑不可变 tenant 或账户类型。"""

    def __init__(self) -> None:
        """返回不含供应商 ID、租户或邮箱的稳定冲突错误。"""
        super().__init__(
            error_code="connection_identity_conflict",
            message="connection identity conflicts with the normalized provider account key",
        )


class ConnectionCredentialOwnershipError(StateConflictError):
    """表示 token 写入目标不是当前用户仍处于 connected 的连接。"""

    def __init__(self) -> None:
        """返回不区分跨用户、缺失或断开状态的稳定安全错误。"""
        super().__init__(
            error_code="connection_credential_ownership_conflict",
            message="connection credential ownership cannot be verified",
        )


class UnsupportedConnectionProviderError(StateConflictError):
    """表示调用方请求了组合根未固定装配的供应商。"""

    def __init__(self) -> None:
        """使用不回显原始 provider 的稳定错误码 fail closed。"""
        super().__init__(
            error_code="connection_provider_unsupported",
            message="connection provider is not supported",
        )


class OAuthAttemptInvalidatedError(StateConflictError):
    """表示渐进 OAuth state 已被关闭、断开或新授权代际安全失效。"""

    def __init__(self) -> None:
        """不回显连接、账户或供应商身份，统一收敛为稳定冲突码。"""
        super().__init__(
            error_code="oauth_attempt_invalidated",
            message="oauth authorization attempt is no longer valid",
        )


class OAuthAuthorizationScopeConflictError(StateConflictError):
    """表示 targetless callback 的 token scope 无法覆盖当前 connected 权限事实。"""

    def __init__(self) -> None:
        """返回不包含账户、scope 或 token 的稳定冲突错误。"""
        super().__init__(
            error_code="oauth_authorization_scope_conflict",
            message="oauth authorization scopes conflict with the connected account",
        )


def _oauth_state_hash(state: str) -> bytes:
    """严格校验本应用生成的无 padding Base64URL state 并返回其摘要。

    非 ASCII、非法字符、padding、错误长度或非规范编码都在访问数据库前统一收敛为
    ``OAuthStateRejectedError``，避免编码异常变成 500，也避免把任意字符串当作 OAuth
    CSRF 事实继续消费。
    """
    try:
        encoded = state.encode("ascii")
    except UnicodeEncodeError:
        raise OAuthStateRejectedError from None
    if len(encoded) != 43:
        raise OAuthStateRejectedError
    try:
        decoded = base64.b64decode(
            encoded + b"=" * (-len(encoded) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError):
        raise OAuthStateRejectedError from None
    if len(decoded) != 32 or _b64url(decoded) != state:
        raise OAuthStateRejectedError
    return hashlib.sha256(encoded).digest()


def _utc_now(clock: Clock) -> datetime:
    """读取并验证时钟返回显式 UTC，阻止本地时区进入 OAuth 过期判断。"""
    now = clock.now()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise ValueError("connection clock must return an explicit UTC datetime")
    return now.astimezone(UTC)


def _b64url(value: bytes) -> str:
    """按 PKCE/OIDC 规范输出无填充 Base64URL 文本。"""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _sorted_capabilities(
    capabilities: frozenset[ConnectionCapability],
) -> tuple[ConnectionCapability, ...]:
    """按稳定枚举值排序能力，保证 API、JSON 与测试输出确定。"""
    return tuple(sorted(capabilities, key=lambda item: item.value))


class ConnectionsUseCase:
    """协调固定 OAuth adapter mapping、AEAD、能力规则与数据库事务。"""

    def __init__(
        self,
        stores: ConnectionStoreFactory,
        cipher: Encryption,
        adapters: Mapping[str, OAuthProviderAdapter],
        clock: Clock,
        *,
        identity: OAuthRefreshIdentity | None = None,
        coordinator: OAuthRefreshCoordinator | None = None,
    ) -> None:
        """复制并冻结 adapter mapping，阻止运行时注册新供应商或替换数据流向。

        Args:
            stores: 每次调用独占的连接事务工厂。
            cipher: 对 PKCE verifier 与 OAuth token 执行记录绑定 AEAD 的端口。
            adapters: 组合根一次性提供的受支持供应商 mapping。
            clock: 返回显式 UTC 的可替换时钟。
            identity: 与 cipher 同次 Secret 构造的固定 keyed identity；有 refresh 历史时必需。
            coordinator: Task27A 的共享 lease/只读 result 联合端口，不创建第二个 OAuth 服务。

        Raises:
            ValueError: mapping 含未知供应商、键值不一致或重复规范化键。
        """
        normalized: dict[str, OAuthProviderAdapter] = {}
        for raw_provider, adapter in adapters.items():
            try:
                provider = OAuthProvider(raw_provider).value
                adapter_provider = OAuthProvider(adapter.provider).value
            except ValueError as error:
                raise ValueError(
                    "OAuth adapter mapping contains an unsupported provider"
                ) from error
            if provider != adapter_provider or provider in normalized:
                raise ValueError("OAuth adapter mapping key does not match adapter provider")
            normalized[provider] = adapter
        self._stores = stores
        self._cipher = cipher
        self._adapters = MappingProxyType(normalized)
        self._clock = clock
        if (identity is None) != (coordinator is None) or (
            identity is not None and identity.key_version != cipher.key_version
        ):
            raise ValueError("OAuth recovery services require the same cipher identity version")
        self._identity = identity
        self._coordinator = coordinator

    def _rotation(self, store: ConnectionStore) -> CredentialRotationRepository:
        """取得当前短事务内的唯一 writer；缺少组合根注入时有 fence 必须失败关闭。"""
        if self._identity is None or self._coordinator is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        return store.credential_rotation(cipher=self._cipher, identity=self._identity)

    async def _recovery_authorization(
        self,
        store: ConnectionStore,
        consumed: ConsumedOAuthAttempt,
    ) -> OAuthRecoveryAuthorization | None:
        """消费阶段按 attempt 查询恢复关系；旧普通 callback 遇到新 fence 也不得继续保存。"""
        if consumed.target_connection_id is None:
            return None
        records = await store.get_refresh_events(
            user_id=consumed.user_id, connection_id=consumed.target_connection_id
        )
        if not records:
            return None
        rotation = self._rotation(store)
        authorization = await rotation.recovery_authorization(
            user_id=consumed.user_id,
            connection_id=consumed.target_connection_id,
            attempt_id=consumed.id,
        )
        if (
            authorization is None
            and await rotation.recovery_candidate(
                user_id=consumed.user_id,
                connection_id=consumed.target_connection_id,
            )
            is not None
        ):
            raise OAuthRefreshError()
        if (
            authorization is not None
            and authorization.metadata.target_generation != consumed.target_authorization_generation
        ):
            raise OAuthRefreshError("oauth_credential_state_conflict")
        return authorization

    def _adapter_for(self, provider: str | OAuthProvider) -> OAuthProviderAdapter:
        """解析固定 adapter；未知或未装配供应商始终拒绝。"""
        try:
            normalized = OAuthProvider(provider).value
        except ValueError:
            raise UnsupportedConnectionProviderError from None
        adapter = self._adapters.get(normalized)
        if adapter is None:
            raise UnsupportedConnectionProviderError
        return adapter

    @staticmethod
    def _aad(user_id: UUID, record_id: UUID, secret_kind: str) -> bytes:
        """生成固定 ``user_id:record_id:secret_kind`` AAD，禁止跨记录替换密文。"""
        return f"{user_id}:{record_id}:{secret_kind}".encode("ascii")

    def _build_attempt(
        self,
        *,
        provider: str | OAuthProvider,
        requested_capabilities: frozenset[ConnectionCapability],
    ) -> tuple[
        UUID,
        str,
        bytes,
        bytes,
        OAuthAuthorizationRequest,
        OAuthProviderAdapter,
    ]:
        """生成一次授权的随机值、摘要、密文与类型化适配器请求。

        明文 state 只进入 URL；verifier 只进入 AEAD；OIDC nonce 原文只进入适配器请求，
        数据库分别保存固定长度 state/nonce 摘要，避免日志或通用响应接触这些值。
        """
        adapter = self._adapter_for(provider)
        state = _b64url(secrets.token_bytes(32))
        verifier = _b64url(secrets.token_bytes(64))
        oidc_nonce = _b64url(secrets.token_bytes(32))
        attempt_id = uuid4()
        requested_scopes = adapter.scopes_for(requested_capabilities)
        request = OAuthAuthorizationRequest(
            state=state,
            code_challenge=_b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
            requested_scopes=requested_scopes,
            oidc_nonce=oidc_nonce,
        )
        return (
            attempt_id,
            verifier,
            hashlib.sha256(state.encode("ascii")).digest(),
            hashlib.sha256(oidc_nonce.encode("ascii")).digest(),
            request,
            adapter,
        )

    async def _persist_attempt(
        self,
        *,
        store: ConnectionStore,
        user_id: UUID,
        provider: str | OAuthProvider,
        requested_capabilities: frozenset[ConnectionCapability],
        now: datetime,
        target_connection_id: UUID | None = None,
        target_authorization_generation: int | None = None,
    ) -> tuple[str, UUID]:
        """在调用方事务内持久化授权尝试，返回 URL 与恢复关联需要的原 attempt ID。"""
        (
            attempt_id,
            verifier,
            state_hash,
            oidc_nonce_hash,
            request,
            adapter,
        ) = self._build_attempt(
            provider=provider,
            requested_capabilities=requested_capabilities,
        )
        verifier_value = self._cipher.encrypt(
            verifier.encode("ascii"),
            self._aad(user_id, attempt_id, "pkce_verifier"),
        )
        await store.create_attempt(
            attempt_id=attempt_id,
            user_id=user_id,
            provider=OAuthProvider(provider).value,
            state_hash=state_hash,
            verifier=verifier_value,
            requested_capabilities=requested_capabilities,
            oidc_nonce_hash=oidc_nonce_hash,
            expires_at=now + OAUTH_ATTEMPT_TTL,
            created_at=now,
            target_connection_id=target_connection_id,
            target_authorization_generation=target_authorization_generation,
        )
        return adapter.build_authorization_url(request), attempt_id

    async def start(
        self,
        *,
        user_id: UUID,
        provider: str | OAuthProvider,
        capabilities: frozenset[ConnectionCapability],
    ) -> OAuthStartResult:
        """为调用方显式选择的非空读取能力集合发起首次 OAuth。"""
        if not capabilities or not capabilities.issubset(_READ_CAPABILITIES):
            raise ConnectionCapabilityDependencyConflict
        now = _utc_now(self._clock)
        async with self._stores() as store:
            authorization_url, _ = await self._persist_attempt(
                store=store,
                user_id=user_id,
                provider=provider,
                requested_capabilities=frozenset(capabilities),
                now=now,
            )
        return OAuthStartResult(authorization_url)

    async def start_capability_enable(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
    ) -> CapabilityEnableResult:
        """发起单项能力渐进授权，并在存在原 fence 时原子建立显式恢复关系。

        Args:
            user_id: 当前已认证用户，所有连接和凭据读取均限定该归属。
            connection_id: 用户选择的既有 connected 连接。
            capability: 本次请求的单项能力；请求范围包含依赖闭包和当前 enabled 能力。

        Returns:
            授权 URL 与实际请求的规范能力集合。S→T、OAuthAttempt 和恢复 started
            在同一事务提交；普通无 fence 的渐进授权继续沿用既有流程。

        Raises:
            ConnectionNotFoundError: 目标不存在、跨用户或已经断开。
            OAuthRefreshError: 未决 fence 不唯一、固定旧身份不符或当前凭据无效。
        """
        now = _utc_now(self._clock)
        async with self._stores() as store:
            # 仓储先按 user→connection 同步再读取 enabled 快照；后续 attempt/恢复审计
            # 的 user 外键不能与可信执行 user→connection 反序，代际快照仍在同一事务。
            connection = await store.get_connection_for_update(
                user_id=user_id,
                connection_id=connection_id,
            )
            enabled = await store.get_enabled_capabilities(
                user_id=user_id,
                connection_id=connection_id,
            )
            if (
                connection is None
                or enabled is None
                or connection.status != ConnectionStatus.CONNECTED.value
            ):
                raise ConnectionNotFoundError
            requested = validate_capability_enable(capability, enabled)
            candidate: OAuthRecoveryCandidate | None = None
            if await store.get_refresh_events(user_id=user_id, connection_id=connection_id):
                candidate = await self._rotation(store).recovery_candidate(
                    user_id=user_id,
                    connection_id=connection_id,
                )
            generation = await store.set_capabilities_authorizing(
                user_id=user_id,
                connection_id=connection_id,
                capabilities=requested,
            )
            authorization_url, attempt_id = await self._persist_attempt(
                store=store,
                user_id=user_id,
                provider=connection.provider,
                requested_capabilities=requested,
                now=now,
                target_connection_id=connection.id,
                target_authorization_generation=generation,
            )
            if candidate is not None:
                await self._rotation(store).start_recovery(
                    candidate,
                    attempt_id=attempt_id,
                    target_generation=generation,
                    occurred_at=now,
                )
        return CapabilityEnableResult(authorization_url, _sorted_capabilities(requested))

    async def callback(
        self,
        *,
        code: str,
        state: str,
        provider: str | OAuthProvider | None = None,
    ) -> UUID:
        """持久消费一次 state，再按普通授权或显式恢复执行一次 code exchange。

        Args:
            code: 路由已验证的非空供应商授权码，只在内存使用且不能重试。
            state: 规范 Base64URL 一次性 state，持久层只使用其摘要。
            provider: API 路由绑定的供应商；兼容既有内部调用时可省略。

        Returns:
            当前凭据满足 requested 能力的原连接 ID；恢复响应必须先完成双行 CAS、
            scope/能力/replacement 原子提交，再独立读取当前就绪状态。

        Raises:
            OAuthStateRejectedError: state 非法、已消费、过期或供应商不匹配。
            OAuthRefreshError: 恢复未满足、CAS/lease 失败或只能只读核对的未知结果。
            DomainError: 已安全分类的供应商拒绝或本地授权冲突。
        """
        now = _utc_now(self._clock)
        state_hash = _oauth_state_hash(state)
        async with self._stores() as store:
            consumed = await store.consume_attempt(state_hash=state_hash, now=now)
            if consumed is None or (
                provider is not None and consumed.provider != OAuthProvider(provider).value
            ):
                raise OAuthStateRejectedError
            recovery = await self._recovery_authorization(store, consumed)

        adapter = self._adapter_for(consumed.provider)
        verifier = self._cipher.decrypt(
            consumed.verifier,
            self._aad(consumed.user_id, consumed.id, "pkce_verifier"),
        ).decode("ascii")
        if recovery is not None:
            return await self._callback_recovery(
                consumed, recovery, adapter, code=code, verifier=verifier
            )
        try:
            token = await adapter.exchange_code(code=code, verifier=verifier)
            account = await adapter.fetch_account(
                token,
                expected_nonce_hash=consumed.oidc_nonce_hash,
            )
        except DomainError as error:
            if isinstance(error, StateConflictError):
                # StateConflict/OAuthAttemptInvalidated 是本地状态事实，不是供应商授权
                # 失败；绝不能把新一轮授权或断开竞争误记为本次 action_required。
                raise
            # 网络交换、token-info、userinfo 或 nonce 校验失败后，只有 target-bound
            # 渐进授权需要收敛状态；targetless 首次失败不能创建连接，也没有本地能力行可更新。
            await self._converge_failed_progressive_authorization(
                consumed, error_code=self._safe_failure_code(error.error_code)
            )
            raise
        return await self._save_tokens_and_capabilities(
            user_id=consumed.user_id,
            attempt_id=consumed.id,
            provider=consumed.provider,
            requested_capabilities=consumed.requested_capabilities,
            target_connection_id=consumed.target_connection_id,
            target_authorization_generation=consumed.target_authorization_generation,
            account=account,
            token=token,
            now=now,
            adapter=adapter,
        )

    async def callback_error(
        self,
        *,
        provider: str | OAuthProvider,
        state: str,
        error_code: RecoveryFailureCode = "oauth_authorization_failed",
    ) -> None:
        """消费供应商拒绝回调的 state，并收敛目标渐进能力。

        OAuth provider 在用户拒绝或管理员同意缺失时不会返回授权码；若直接把错误映射为
        Problem 而不消费 state，攻击者或浏览器重试可重复触发同一一次性回调。此方法复用
        正常 callback 的 state 绑定。普通错误的消费、能力收敛与审计仍原子提交；恢复
        错误先原子提交消费与普通失败审计，再在另一短事务提交能力/unsatisfied。结果
        事务回滚或 ACK 丢失不能重新使用 state；本路径没有 provider/code 调用。

        Args:
            provider: 预期供应商，防止一个供应商的 callback 误消费另一供应商 state。
            state: 浏览器回传的一次性 OAuth state 原文。
            error_code: API 已安全分类的固定码，raw error/description/codes 不得进入本端口。

        Raises:
            OAuthStateRejectedError: state 非法、已消费、过期或供应商不匹配。
        """
        now = _utc_now(self._clock)
        if error_code not in get_args(RecoveryFailureCode):
            raise ValueError("OAuth authorization error code is invalid")
        state_hash = _oauth_state_hash(state)
        normalized_provider = OAuthProvider(provider).value
        async with self._stores() as store:
            consumed = await store.consume_attempt(state_hash=state_hash, now=now)
            if consumed is None or consumed.provider != normalized_provider:
                # 检查必须在事务内，错误供应商不能提前提交另一 callback 的 state 消费。
                raise OAuthStateRejectedError
            recovery = await self._recovery_authorization(store, consumed)
            if (
                recovery is None
                and consumed.target_connection_id is not None
                and consumed.target_authorization_generation is not None
            ):
                await store.mark_progressive_authorization_failed(
                    user_id=consumed.user_id,
                    connection_id=consumed.target_connection_id,
                    authorization_generation=consumed.target_authorization_generation,
                    capabilities=consumed.requested_capabilities,
                    error_code=error_code,
                )
            await store.record_authorization_failed(
                user_id=consumed.user_id,
                attempt_id=consumed.id,
                provider=normalized_provider,
                occurred_at=now,
                error_code=error_code,
            )
        if recovery is not None:
            await self._persist_recovery_unsatisfied(consumed, recovery, error_code=error_code)
        _AUTHORIZATION_FAILURE_LOGGER.info(
            "OAuth authorization failed",
            extra={
                "provider": normalized_provider,
                "error_code": error_code,
            },
        )

    async def _converge_failed_progressive_authorization(
        self,
        consumed: ConsumedOAuthAttempt,
        *,
        error_code: RecoveryFailureCode = "oauth_authorization_failed",
    ) -> None:
        """按冻结连接与授权代际安全收敛失败的渐进能力。

        失败 callback 与新授权、能力关闭或断开可能并发；仓储在连接行锁下再次核对
        ``authorization_generation`` 与 ``connected`` 状态，过时代际只能 no-op，不能把
        新授权重新覆盖为旧错误。targetless 首次 OAuth 没有连接目标，直接返回。
        """
        if (
            consumed.target_connection_id is None
            or consumed.target_authorization_generation is None
        ):
            return
        async with self._stores() as store:
            await store.mark_progressive_authorization_failed(
                user_id=consumed.user_id,
                connection_id=consumed.target_connection_id,
                authorization_generation=consumed.target_authorization_generation,
                capabilities=consumed.requested_capabilities,
                error_code=error_code,
            )

    @staticmethod
    def _safe_failure_code(error_code: str) -> RecoveryFailureCode:
        """保留既有安全分类；供应商未知内部码只能收敛为固定通用授权失败。"""
        return (
            cast(RecoveryFailureCode, error_code)
            if error_code in get_args(RecoveryFailureCode)
            else "oauth_authorization_failed"
        )

    async def _persist_recovery_unsatisfied(
        self,
        consumed: ConsumedOAuthAttempt,
        authorization: OAuthRecoveryAuthorization,
        *,
        error_code: RecoveryFailureCode,
        lease: OAuthRefreshLease | None = None,
    ) -> RecoveryUnsatisfiedV1:
        """同事务收敛 requested 能力/追加 unsatisfied；未知提交只用新 session 的 union 核对。

        error callback 没有网络，直接使用事务行锁和两个共享 audit mutex；code callback
        还必须在写前/提交前证明已有 shared session lease。任何结果回滚都不能回滚先前
        已提交的 state 消费，更不能补写猜测结果或发送第二次 code。
        """
        coordinator = self._coordinator
        if coordinator is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        try:
            if lease is not None:
                await lease.assert_owned()
            async with self._stores() as store:
                transition = await store.mark_progressive_authorization_failed(
                    user_id=consumed.user_id,
                    connection_id=authorization.connection_id,
                    authorization_generation=authorization.metadata.target_generation,
                    capabilities=consumed.requested_capabilities,
                    error_code=error_code,
                )
                result = await self._rotation(store).unsatisfied(
                    authorization,
                    requested_capabilities=consumed.requested_capabilities,
                    capability_transition=transition,
                    error_code=error_code,
                    completed_at=_utc_now(self._clock),
                )
                if lease is not None:
                    await lease.assert_owned()
            return result
        except Exception as error:  # state 已消费；commit 异常只核对 append-only 结果。
            closed = await coordinator.read_result(
                user_id=consumed.user_id,
                connection_id=authorization.connection_id,
                attempt_id=consumed.id,
                recovery=True,
            )
            if closed is None:
                if isinstance(error, OAuthRefreshError) and error.error_code in {
                    "oauth_refresh_claim_locked",
                    "oauth_refresh_claim_lost",
                    "oauth_credential_state_conflict",
                }:
                    raise error from None
                raise OAuthRefreshError() from None
            if not isinstance(closed.metadata, RecoveryUnsatisfiedV1):
                raise OAuthRefreshError() from None
            # 历史关闭不依赖 current T；只读取当前能力投影，不把后来合法授权当成 rollback。
            async with self._stores() as store:
                await store.get_capability_snapshot(
                    user_id=consumed.user_id, connection_id=authorization.connection_id
                )
            return closed.metadata

    async def _callback_recovery(
        self,
        consumed: ConsumedOAuthAttempt,
        authorization: OAuthRecoveryAuthorization,
        adapter: OAuthProviderAdapter,
        *,
        code: str,
        verifier: str,
    ) -> UUID:
        """一次 code exchange 贯穿共享连接 lease，结果只能由真实事务证明。

        Args:
            consumed: 已在前一短事务持久消费的 OAuthAttempt。
            authorization: 共享 parser 验证的原 fence、本次恢复 started 与 F/S/T。
            adapter: 组合根固定的当前供应商 adapter，不允许切换账户或供应商。
            code: 本次唯一授权码；任何未知结果、回滚或丢锁都不允许重发。
            verifier: 已用精确 attempt AAD 解密的 PKCE verifier，只在内存使用。

        Returns:
            replacement 已提交且当前 requested 能力可用的原连接 ID。

        Raises:
            OAuthRefreshError: lease 竞争/丢失、凭据冲突、恢复未满足或结果仍无法核实。
            DomainError: 目标 T 过时、供应商已知拒绝或 token 响应后的只读验证失败。

        网络前短事务冻结当前完整双行；网络后禁止重新冻结。未知 exchange、CAS miss、
        lease loss 与 commit ACK loss 都只读核对共享 result union，不猜写 unsatisfied。
        """
        coordinator = self._coordinator
        if coordinator is None:
            raise OAuthRefreshError("oauth_credential_state_conflict")
        failure: DomainError | None = None
        try:
            async with coordinator.explicit_recovery_lease(
                user_id=consumed.user_id, connection_id=authorization.connection_id
            ) as lease:
                await lease.assert_owned()
                async with self._stores() as store:
                    connection = await store.get_connection_for_update(
                        user_id=consumed.user_id, connection_id=authorization.connection_id
                    )
                    stale = (
                        connection is None
                        or connection.status != "connected"
                        or connection.authorization_generation
                        != authorization.metadata.target_generation
                    )
                    claim = (
                        None
                        if stale
                        else await self._rotation(store).freeze_recovery(authorization)
                    )
                if stale or claim is None:
                    await self._persist_recovery_unsatisfied(
                        consumed,
                        authorization,
                        error_code="oauth_authorization_failed",
                        lease=lease,
                    )
                    failure = OAuthAttemptInvalidatedError()
                else:
                    await lease.assert_owned()
                    exchange_completed = False
                    try:
                        token = await adapter.exchange_code(code=code, verifier=verifier)
                        exchange_completed = True
                        await lease.assert_owned()
                        account = await adapter.fetch_account(
                            token, expected_nonce_hash=consumed.oidc_nonce_hash
                        )
                        await lease.assert_owned()
                    except DomainError as error:
                        # adapter 把 timeout/request-failed/不确定 5xx 都归入 Transient；
                        # 尚未拿到 token 响应时无法证明 exchange 结果，不能猜写 unsatisfied。
                        # adapter 内部的 post-exchange scope 核验与外部只读账户验证
                        # 都不再有授权码结果歧义；它们的已知失败可以原子收敛能力。
                        if isinstance(error, (StateConflictError, OAuthRefreshError)) or (
                            isinstance(error, TransientProviderError)
                            and not exchange_completed
                            and not isinstance(error, OAuthPostExchangeVerificationError)
                        ):
                            raise
                        await self._persist_recovery_unsatisfied(
                            consumed,
                            authorization,
                            error_code=self._safe_failure_code(error.error_code),
                            lease=lease,
                        )
                        failure = error
                    else:
                        if connection is None:
                            raise OAuthRefreshError("oauth_credential_state_conflict")
                        valid = self._recovery_response_valid(
                            claim,
                            connection,
                            account,
                            token,
                            adapter,
                            consumed.requested_capabilities,
                        )
                        if not valid:
                            await self._persist_recovery_unsatisfied(
                                consumed,
                                authorization,
                                error_code="oauth_refresh_recovery_unsatisfied",
                                lease=lease,
                            )
                            failure = OAuthRefreshError("oauth_refresh_recovery_unsatisfied")
                        else:
                            async with self._stores() as store:
                                await lease.assert_owned()
                                await self._rotation(store).replace(
                                    claim, token, completed_at=_utc_now(self._clock)
                                )
                                await store.update_connection_scopes(
                                    user_id=consumed.user_id,
                                    connection_id=authorization.connection_id,
                                    scopes=token.granted_scopes,
                                )
                                await self._save_actual_capabilities(
                                    store,
                                    user_id=consumed.user_id,
                                    connection_id=authorization.connection_id,
                                    requested_capabilities=consumed.requested_capabilities,
                                    adapter=adapter,
                                    token=token,
                                    now=_utc_now(self._clock),
                                )
                                await lease.assert_owned()
        except Exception as error:  # started/state 已持久；commit ACK 丢失只读核对。
            closed = await coordinator.read_result(
                user_id=consumed.user_id,
                connection_id=authorization.connection_id,
                attempt_id=consumed.id,
                recovery=True,
            )
            if closed is None:
                if isinstance(error, OAuthRefreshError) and error.error_code in {
                    "oauth_refresh_claim_locked",
                    "oauth_refresh_claim_lost",
                    "oauth_credential_state_conflict",
                }:
                    # 新 session 的合法历史结果优先；不存在结果时不能抹掉明确 lease/CAS 原因。
                    raise error from None
                raise OAuthRefreshError() from None
            if isinstance(closed.metadata, RecoveryUnsatisfiedV1):
                raise OAuthRefreshError("oauth_refresh_recovery_unsatisfied") from None
            if not isinstance(closed.metadata, CredentialReplacedV1):
                raise OAuthRefreshError("oauth_credential_state_conflict") from None
        if failure is not None:
            raise failure
        # append-only closure 与 current readiness 分开；更晚合法 generation/refresh 不重开本次。
        for capability in _sorted_capabilities(consumed.requested_capabilities):
            await coordinator.read_current(
                OAuthRefreshRequest(
                    user_id=consumed.user_id,
                    connection_id=authorization.connection_id,
                    capability=capability,
                )
            )
        return authorization.connection_id

    @staticmethod
    def _recovery_response_valid(
        claim: OAuthRecoveryClaim,
        connection: StoredConnection,
        account: OAuthAccount,
        token: OAuthTokenSet,
        adapter: OAuthProviderAdapter,
        requested_capabilities: frozenset[ConnectionCapability],
    ) -> bool:
        """在受控内存校验规范响应，并以 compare_digest 比较 refresh 明文。

        Args:
            claim: 网络前冻结的当前双行和旧 refresh 明文。
            connection: 同次冻结的规范账户身份，不接受响应更换目标。
            account: adapter 规范化的账户，仍再次验证其边界。
            token: adapter 规范化的响应，仍再次验证非空文本、scope 与有界 expiry。
            adapter: 用于计算 requested 能力的固定供应商 scope 映射。
            requested_capabilities: OAuthAttempt 实际冻结的能力请求。

        Returns:
            仅在身份不变、scope 覆盖旧事实和请求、明确返回不同非空 refresh 时为 True。
        """
        if type(account) is not OAuthAccount or type(token) is not OAuthTokenSet:
            return False
        try:
            account.__post_init__()
            token.__post_init__()
        except (ValueError, TypeError):
            return False
        return (
            (account.provider_account_id, account.provider_tenant_id, account.account_type)
            == (
                connection.provider_account_id,
                connection.provider_tenant_id,
                connection.account_type,
            )
            and claim.snapshot.scopes.issubset(token.granted_scopes)
            and adapter.scopes_for(requested_capabilities).issubset(token.granted_scopes)
            and token.refresh_token is not None
            and not secrets.compare_digest(
                claim.refresh_plaintext, token.refresh_token.encode("utf-8")
            )
        )

    async def _save_tokens_and_capabilities(
        self,
        *,
        user_id: UUID,
        attempt_id: UUID,
        provider: str,
        requested_capabilities: frozenset[ConnectionCapability],
        target_connection_id: UUID | None,
        target_authorization_generation: int | None,
        account: OAuthAccount,
        token: OAuthTokenSet,
        now: datetime,
        adapter: OAuthProviderAdapter,
    ) -> UUID:
        """保存规范账户、token 与逐能力实际 scope 验证结果。"""
        async with self._stores() as store:
            if target_connection_id is None:
                # 首次 OAuth 没有既有连接目标，沿用规范账户键 upsert。
                if target_authorization_generation is not None:
                    raise OAuthAttemptInvalidatedError
                # 网络交换在事务外执行；这里必须重新锁定 attempt，防止断开事务在此期间
                # 提交后仍让旧 callback 通过规范账户 upsert 复活断开的连接。
                await store.validate_unbound_attempt_for_callback(
                    attempt_id=attempt_id,
                    user_id=user_id,
                    provider=provider,
                )
                # 已有 fenced 身份必须在进入任何 bootstrap/合并写入方法前拒绝；
                # 仓储在唯一键竞争后仍会复查，覆盖这里尚不存在连接的插入竞态。
                await store.validate_unfenced_identity_for_callback(
                    user_id=user_id,
                    provider=provider,
                    provider_account_id=account.provider_account_id,
                    identity_key_version=self._cipher.key_version,
                )
                connection_id = await store.ensure_connection(
                    user_id=user_id,
                    provider=provider,
                    provider_account_id=account.provider_account_id,
                    provider_tenant_id=account.provider_tenant_id,
                    account_type=account.account_type,
                    account_email=account.account_email,
                    scopes=token.granted_scopes,
                    identity_key_version=self._cipher.key_version,
                )
            else:
                if target_authorization_generation is None:
                    raise OAuthAttemptInvalidatedError
                connection_id = await store.ensure_bound_connection_for_callback(
                    user_id=user_id,
                    connection_id=target_connection_id,
                    authorization_generation=target_authorization_generation,
                    provider=provider,
                    provider_account_id=account.provider_account_id,
                    provider_tenant_id=account.provider_tenant_id,
                    account_type=account.account_type,
                    identity_key_version=self._cipher.key_version,
                )
                # 渐进 callback 的目标连接已通过代际与身份锁定；此处才替换实际 scope，
                # 避免旧 state 在网络调用期间覆盖更新后的连接权限事实。
                await store.update_connection_scopes(
                    user_id=user_id,
                    connection_id=connection_id,
                    scopes=token.granted_scopes,
                )
            access = self._cipher.encrypt(
                token.access_token.encode("utf-8"),
                self._aad(user_id, connection_id, "access_token"),
            )
            refresh = (
                self._cipher.encrypt(
                    token.refresh_token.encode("utf-8"),
                    self._aad(user_id, connection_id, "refresh_token"),
                )
                if token.refresh_token is not None
                else None
            )
            await store.save_connection_tokens(
                user_id=user_id,
                connection_id=connection_id,
                access_token=access,
                refresh_token=refresh,
                expires_at=now + timedelta(seconds=token.expires_in),
            )
            await self._save_actual_capabilities(
                store,
                user_id=user_id,
                connection_id=connection_id,
                requested_capabilities=requested_capabilities,
                adapter=adapter,
                token=token,
                now=now,
            )
        return connection_id

    async def _save_actual_capabilities(
        self,
        store: ConnectionStore,
        *,
        user_id: UUID,
        connection_id: UUID,
        requested_capabilities: frozenset[ConnectionCapability],
        adapter: OAuthProviderAdapter,
        token: OAuthTokenSet,
        now: datetime,
    ) -> None:
        """普通与恢复 callback 共用 scope/依赖闭包映射，所有写入仍属于调用方同一短事务。"""
        for capability in _sorted_capabilities(requested_capabilities):
            # 写 scope 不能单独证明回复读取、ETag 或结果核对所依赖的读取权限。
            required_capabilities = validate_capability_enable(capability, frozenset())
            enabled = adapter.scopes_for(required_capabilities).issubset(token.granted_scopes)
            await store.save_capability_state(
                user_id=user_id,
                connection_id=connection_id,
                capability=capability,
                status=CapabilityStatus.ENABLED if enabled else CapabilityStatus.ACTION_REQUIRED,
                actual_scopes=token.granted_scopes,
                last_verified_at=now,
                last_error_code=None if enabled else "connection_scope_missing",
            )

    async def list(self, *, user_id: UUID) -> tuple[ConnectionSummary, ...]:
        """返回当前用户的连接列表，绝不加载或公开 credential 行。"""
        async with self._stores() as store:
            rows = await store.list_connections(user_id=user_id)
        return tuple(
            ConnectionSummary(
                row.id,
                row.provider,
                row.account_email,
                row.scopes,
                row.status,
                row.last_error_code,
            )
            for row in rows
        )

    async def get_capabilities(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> ConnectionCapabilitySnapshot:
        """返回当前用户连接的能力与日历目录；跨用户与缺失统一为 404 语义。"""
        async with self._stores() as store:
            snapshot = await store.get_capability_snapshot(
                user_id=user_id,
                connection_id=connection_id,
            )
        if snapshot is None:
            raise ConnectionNotFoundError
        return snapshot

    async def verify_google_capabilities(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> None:
        """显式执行一次 Google 历史能力回填/核验，供启动维护任务调用。

        Repository 方法本身以 ``ON CONFLICT DO NOTHING`` 保证幂等；用例仍把用户与连接
        作为必需参数传入，避免维护调用绕过跨用户隔离。
        """
        async with self._stores() as store:
            await store.ensure_google_capability_rows(
                user_id=user_id,
                connection_id=connection_id,
            )

    async def disable_capability(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
    ) -> CapabilityDisableResult:
        """委托仓储在固定可信锁序内校验依赖并原子关闭能力。

        Task 19 规定动作撤权沿 ``TaskRun → ApprovalRequest → ToolExecution → 本地动作 →
        Connection → capability`` 顺序取得行锁；enabled 快照与依赖校验必须发生在同一事务、
        同一连接锁之后。这样渐进 OAuth callback 不能在快照与关闭之间提交写能力，且 claim
        与撤权不会形成 Connection→Task 的反向等待。仓储抛出的依赖冲突会回滚此前扫描的
        生命周期写入，路由仍只看到稳定领域错误。
        """
        async with self._stores() as store:
            try:
                await store.disable_capability(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=capability,
                )
            except ConnectionCredentialOwnershipError:
                # 仓储在扫描候选后锁定连接；缺失、跨用户和已删除连接都不能暴露
                # ownership 细节，统一保持既有 404 资源隐藏契约。
                raise ConnectionNotFoundError from None
        return CapabilityDisableResult(capability, CapabilityStatus.DISABLED)

    async def disconnect(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> OAuthRevocationResult | None:
        """先删除本地密文并断开，再调用固定 adapter 撤销远端 refresh token。

        供应商网络/分类异常会继续向上传递，不能伪装为撤销成功；即使远端失败，本地提交也
        不会回滚恢复凭据。供应商明确不支持窄撤销时返回 ``UNSUPPORTED`` 供后续审计使用。
        """
        now = _utc_now(self._clock)
        raw_refresh: str | None = None
        async with self._stores() as store:
            connection = await store.get_connection(
                user_id=user_id,
                connection_id=connection_id,
            )
            if connection is None:
                raise ConnectionNotFoundError
            found, refresh = await store.disconnect(
                user_id=user_id,
                connection_id=connection_id,
                invalidated_at=now,
            )
            if refresh is not None:
                # refresh token 必须在本地删除事务仍持有精确连接事实时解密到受控内存；
                # 随后先提交密文删除，再允许任何供应商网络调用。这样提交后即使进程崩溃，
                # 也不会为“稍后重试撤销”把凭据重新持久化或塞入队列。
                raw_refresh = self._cipher.decrypt(
                    refresh,
                    self._aad(user_id, connection_id, "refresh_token"),
                ).decode("utf-8")
        if not found:
            raise ConnectionNotFoundError

        # 本地断开事务已经提交；adapter 缺失或后续网络失败都不能让凭据复活。
        try:
            adapter = self._adapter_for(connection.provider)
        except UnsupportedConnectionProviderError as error:
            async with self._stores() as store:
                await store.record_oauth_revoke_unresolved(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider=connection.provider,
                    error_code=error.error_code,
                    occurred_at=_utc_now(self._clock),
                )
            return OAuthRevocationResult(
                OAuthRevocationStatus.UNSUPPORTED,
                error.error_code,
            )
        if raw_refresh is None:
            return None
        try:
            result = await adapter.revoke(raw_refresh)
        except DomainError as error:
            # 供应商错误码已经在 adapter 边界完成脱敏；只把稳定 code 写入独立短事务，
            # 不保存异常文本、请求正文或仍在受控内存中的 token。原始领域错误仍按端口
            # 语义向上抛出，调用方不能把失败伪装成撤销成功。
            async with self._stores() as store:
                await store.record_oauth_revoke_unresolved(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider=connection.provider,
                    error_code=error.error_code,
                    occurred_at=_utc_now(self._clock),
                )
            raise
        except Exception:
            # 适配器若在未来遗漏分类，仍只记录固定机器码；绝不把未知异常的字符串
            # 或 traceback 放入审计，且不创建任何能够重新取得已删除 token 的工作。
            async with self._stores() as store:
                await store.record_oauth_revoke_unresolved(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider=connection.provider,
                    error_code="oauth_revoke_failed",
                    occurred_at=_utc_now(self._clock),
                )
            raise
        if result.status is OAuthRevocationStatus.UNSUPPORTED:
            # 没有安全窄撤销端点与网络失败同样需要可观测的未决事实；token 已在本地
            # 事务中销毁，因此只追加审计，不排入 retry/outbox。
            async with self._stores() as store:
                await store.record_oauth_revoke_unresolved(
                    user_id=user_id,
                    connection_id=connection_id,
                    provider=connection.provider,
                    error_code=result.error_code or "oauth_revoke_unsupported",
                    occurred_at=_utc_now(self._clock),
                )
        return result

    async def start_manual_sync(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        idempotency_key: str,
        tasks: TaskCreator,
    ) -> ManualSyncResult:
        """为已连接且归属当前用户的帐号幂等创建邮件、日历两项异步任务。

        新任务必须使用供应商中立的 ``sync_mail``，并显式绑定 mailbox 与 calendar
        directory 两个目录 owner；幂等键仍保留 M1 的 ``gmail`` 后缀，使升级前后重复提交
        收敛到既有任务。旧 ``sync_gmail`` kind 和缺少 calendar scope 的任务只由 Worker
        读取已持久化记录，不再从当前 API 创建。
        """
        async with self._stores() as store:
            connection = await store.get_connection(
                user_id=user_id,
                connection_id=connection_id,
            )
        if connection is None or connection.status != ConnectionStatus.CONNECTED.value:
            raise ConnectionNotFoundError
        created = await tasks.execute_many(
            user_id=user_id,
            items=(
                CreateTaskBatchItem(
                    "sync_mail",
                    {
                        "connection_id": str(connection_id),
                        "scope_key": "mailbox",
                    },
                    f"{idempotency_key}:gmail",
                ),
                CreateTaskBatchItem(
                    "sync_calendar",
                    {
                        "connection_id": str(connection_id),
                        "scope_key": "directory",
                    },
                    f"{idempotency_key}:calendar",
                ),
            ),
        )
        return ManualSyncResult(created[0].task_id, created[1].task_id)


class GoogleConnectionsUseCase(ConnectionsUseCase):
    """保留 M1 导入名的兼容壳；当前 API 已改由组合根注入通用用例。

    旧单元测试只通过该名字调用供应商中立的手动同步路径，因此兼容构造函数接受历史参数但
    不把旧 Google 客户端重新引入 Application。OAuth 路由必须使用 ``ConnectionsUseCase``
    与组合层 adapter，不能调用本兼容壳发起授权。
    """

    def __init__(
        self,
        stores: ConnectionStoreFactory,
        cipher: Encryption,
        oauth: object,
        clock: Clock,
        client_id: str,
        redirect_uri: str,
    ) -> None:
        """接受历史签名并丢弃供应商参数，仅维持非 OAuth M1 调用方。"""
        del oauth, client_id, redirect_uri
        super().__init__(stores, cipher, {}, clock)
