"""编排供应商中立 OAuth、连接能力、断开与手动同步。"""

import base64
import binascii
import hashlib
import secrets
from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Protocol
from uuid import UUID, uuid4

from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.application.ports.oauth import (
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthProvider,
    OAuthProviderAdapter,
    OAuthRevocationResult,
    OAuthRevocationStatus,
    OAuthTokenSet,
)
from ai_employee.application.use_cases.tasks import CreateTaskBatchItem
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    ConnectionCapabilityDependencyConflict,
    ConnectionStatus,
    validate_capability_enable,
)
from ai_employee.domain.errors import DomainError, StateConflictError

OAUTH_ATTEMPT_TTL = timedelta(minutes=10)
OAUTH_AUTHORIZATION_FAILED_ERROR_CODE = "oauth_authorization_failed"
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
        """在当前事务内锁定目标连接，供授权代际与能力快照保持一致。"""
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
    ) -> UUID:
        """按规范账户键建立或合并连接，并确保四项能力行存在。

        同一用户的 connected 同身份 callback 只有在新 token scope 覆盖当前 scope 时才
        原样替换并保留未请求能力；新建或非 connected 连接从本次 token 事实精确重置，
        tenant/type 或 scope 单调性不满足时拒绝。
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
    ) -> None:
        """在连接仍处于同一代际时，把本次 authorizing 能力收敛为 action_required。"""
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
    ) -> None:
        """复制并冻结 adapter mapping，阻止运行时注册新供应商或替换数据流向。

        Args:
            stores: 每次调用独占的连接事务工厂。
            cipher: 对 PKCE verifier 与 OAuth token 执行记录绑定 AEAD 的端口。
            adapters: 组合根一次性提供的受支持供应商 mapping。
            clock: 返回显式 UTC 的可替换时钟。

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
    ) -> str:
        """在调用方事务内持久化授权尝试并返回适配器生成的 URL。"""
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
        return adapter.build_authorization_url(request)

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
            authorization_url = await self._persist_attempt(
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
        """发起单项能力渐进授权，并请求依赖闭包与全部当前 enabled 能力。"""
        now = _utc_now(self._clock)
        async with self._stores() as store:
            # 先锁连接再读取 enabled 快照；否则断开/关闭与授权发起可能观察到不同代际。
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
            generation = await store.set_capabilities_authorizing(
                user_id=user_id,
                connection_id=connection_id,
                capabilities=requested,
            )
            authorization_url = await self._persist_attempt(
                store=store,
                user_id=user_id,
                provider=connection.provider,
                requested_capabilities=requested,
                now=now,
                target_connection_id=connection.id,
                target_authorization_generation=generation,
            )
        return CapabilityEnableResult(authorization_url, _sorted_capabilities(requested))

    async def callback(self, *, code: str, state: str) -> UUID:
        """一次性消费 state，按尝试记录选择 adapter，并保存实际 scope 与能力状态。"""
        now = _utc_now(self._clock)
        state_hash = _oauth_state_hash(state)
        async with self._stores() as store:
            consumed = await store.consume_attempt(state_hash=state_hash, now=now)
        if consumed is None:
            raise OAuthStateRejectedError

        adapter = self._adapter_for(consumed.provider)
        verifier = self._cipher.decrypt(
            consumed.verifier,
            self._aad(consumed.user_id, consumed.id, "pkce_verifier"),
        ).decode("ascii")
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
            await self._converge_failed_progressive_authorization(consumed)
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

    async def callback_error(self, *, provider: str | OAuthProvider, state: str) -> None:
        """消费供应商拒绝回调的 state，并收敛目标渐进能力。

        OAuth provider 在用户拒绝或管理员同意缺失时不会返回授权码；若直接把错误映射为
        Problem 而不消费 state，攻击者或浏览器重试可重复触发同一一次性回调。此方法复用
        正常 callback 的原子 state 消费与失败收敛边界，调用方随后只抛出脱敏错误。

        Args:
            provider: 预期供应商，防止一个供应商的 callback 误消费另一供应商 state。
            state: 浏览器回传的一次性 OAuth state 原文。

        Raises:
            OAuthStateRejectedError: state 非法、已消费、过期或供应商不匹配。
        """
        now = _utc_now(self._clock)
        state_hash = _oauth_state_hash(state)
        normalized_provider = OAuthProvider(provider).value
        async with self._stores() as store:
            consumed = await store.consume_attempt(state_hash=state_hash, now=now)
        if consumed is None or consumed.provider != normalized_provider:
            raise OAuthStateRejectedError
        await self._converge_failed_progressive_authorization(consumed)

    async def _converge_failed_progressive_authorization(
        self,
        consumed: ConsumedOAuthAttempt,
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
                error_code=OAUTH_AUTHORIZATION_FAILED_ERROR_CODE,
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
                connection_id = await store.ensure_connection(
                    user_id=user_id,
                    provider=provider,
                    provider_account_id=account.provider_account_id,
                    provider_tenant_id=account.provider_tenant_id,
                    account_type=account.account_type,
                    account_email=account.account_email,
                    scopes=token.granted_scopes,
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
            for capability in _sorted_capabilities(requested_capabilities):
                # 回调核对必须复用领域依赖闭包；仅有粗粒度写 scope 不能证明回复读取或
                # 日程 ETag/写后核对所需的读取能力仍然存在。
                required_capabilities = validate_capability_enable(capability, frozenset())
                required_scopes = adapter.scopes_for(required_capabilities)
                enabled = required_scopes.issubset(token.granted_scopes)
                await store.save_capability_state(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=capability,
                    status=(
                        CapabilityStatus.ENABLED if enabled else CapabilityStatus.ACTION_REQUIRED
                    ),
                    actual_scopes=token.granted_scopes,
                    last_verified_at=now,
                    last_error_code=None if enabled else "connection_scope_missing",
                )
        return connection_id

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
