"""验证供应商中立连接能力编排的依赖闭包与授权边界。"""

import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.oauth import (
    OAuthAccount,
    OAuthAuthorizationRequest,
    OAuthProvider,
    OAuthRevocationResult,
    OAuthRevocationStatus,
    OAuthTokenSet,
)
from ai_employee.application.use_cases.connections import (
    ConnectionCapabilitySnapshot,
    ConnectionsUseCase,
    ConsumedOAuthAttempt,
    OAuthAttemptInvalidatedError,
    OAuthStateRejectedError,
    StoredCapability,
    StoredConnection,
    UnsupportedConnectionProviderError,
)
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    ConnectionCapabilityDependencyConflict,
    validate_capability_disable,
)
from ai_employee.domain.errors import (
    StateConflictError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.infrastructure.security.encryption import AeadCipher

_VALID_OAUTH_STATE = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode("ascii")


@dataclass(frozen=True, slots=True)
class FixedClock:
    """为 OAuth 尝试过期时间提供不依赖宿主机的显式 UTC 时钟。"""

    current: datetime = datetime(2030, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        """返回测试冻结的 UTC 时间。"""
        return self.current


@dataclass(slots=True)
class FakeOAuthAdapter:
    """记录能力到 scope 与授权请求的供应商中立 fake。"""

    provider: OAuthProvider = OAuthProvider.GOOGLE
    requested_capabilities: frozenset[ConnectionCapability] = frozenset()
    authorization_request: object | None = None
    base_scopes: frozenset[str] = frozenset()

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """返回可预测 scope，并记录用例传入的完整能力并集。"""
        self.requested_capabilities = capabilities
        return self.base_scopes | frozenset(f"scope:{capability.value}" for capability in capabilities)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """保存类型化请求并返回不含真实供应商或凭据的合成 URL。"""
        self.authorization_request = request
        return "https://provider.example.test/authorize"

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """本测试只覆盖授权发起，交换授权码属于不可达路径。"""
        del code, verifier
        raise AssertionError("exchange_code must not be called")

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """本测试只覆盖授权发起，账户读取属于不可达路径。"""
        del token, expected_nonce_hash
        raise AssertionError("fetch_account must not be called")

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """本测试不刷新 token。"""
        del refresh_token
        raise AssertionError("refresh must not be called")

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """本测试不撤销 token。"""
        del token
        raise AssertionError("revoke must not be called")


@dataclass(slots=True)
class FakeConnectionStore:
    """提供启用能力所需的最小用户归属与状态持久化端口。"""

    user_id: UUID
    connection_id: UUID
    enabled: frozenset[ConnectionCapability] = frozenset()
    authorizing: frozenset[ConnectionCapability] = frozenset()
    disabled: ConnectionCapability | None = None
    attempt: dict[str, Any] = field(default_factory=dict)
    disconnected: bool = False
    authorization_generation: int = 0
    granted_scopes: tuple[str, ...] = ()
    capability_rows: dict[ConnectionCapability, StoredCapability] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """保留已验证 scope，并建立真实仓储在 authorizing 转换时会保留的历史字段。"""
        self.granted_scopes = tuple(f"scope:{capability.value}" for capability in self.enabled)
        self.capability_rows = {
            capability: StoredCapability(
                capability,
                CapabilityStatus.ENABLED if capability in self.enabled else CapabilityStatus.DISABLED,
                self.granted_scopes if capability in self.enabled else (),
                FixedClock().now() if capability in self.enabled else None,
                None,
            )
            for capability in ConnectionCapability
        }

    async def get_refresh_events(self, **values: Any) -> tuple[()]:
        """本组领域编排 fixture 没有 refresh 历史；真实 fence/CAS 使用数据库测试覆盖。"""
        assert values["user_id"] == self.user_id
        return ()

    async def get_connection(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> StoredConnection | None:
        """仅在用户与连接同时匹配时返回合成 Google 连接。"""
        if user_id != self.user_id or connection_id != self.connection_id:
            return None
        return StoredConnection(
            id=self.connection_id,
            user_id=self.user_id,
            provider="google",
            provider_account_id="fake-account",
            provider_tenant_id="",
            account_type="google",
            account_email="fake-account@example.test",
            scopes=self.granted_scopes,
            status="connected",
            last_error_code=None,
            authorization_generation=self.authorization_generation,
        )

    async def get_connection_for_update(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> StoredConnection | None:
        """内存 fake 与真实仓储保持同样的目标锁定端口契约。"""
        return await self.get_connection(user_id=user_id, connection_id=connection_id)

    async def get_enabled_capabilities(
        self, *, user_id: UUID, connection_id: UUID
    ) -> frozenset[ConnectionCapability] | None:
        """返回归属正确连接的当前 enabled 集合。"""
        if user_id != self.user_id or connection_id != self.connection_id:
            return None
        return self.enabled

    async def set_capabilities_authorizing(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capabilities: frozenset[ConnectionCapability],
    ) -> int:
        """模拟真实状态转换：原 enabled 并集进入 authorizing，历史 scope 不被清空。"""
        assert user_id == self.user_id and connection_id == self.connection_id
        self.authorizing = capabilities
        self.enabled = self.enabled - capabilities
        self.authorization_generation += 1
        for capability in capabilities:
            self.capability_rows[capability] = replace(
                self.capability_rows[capability], status=CapabilityStatus.AUTHORIZING
            )
        return self.authorization_generation

    async def get_capability_snapshot(
        self, *, user_id: UUID, connection_id: UUID
    ) -> ConnectionCapabilitySnapshot | None:
        """按归属返回完整状态事实，支持同一连接的连续授权尝试。"""
        if user_id != self.user_id or connection_id != self.connection_id:
            return None
        return ConnectionCapabilitySnapshot(
            self.connection_id, "google", tuple(self.capability_rows.values()), ()
        )

    async def disable_capability(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
    ) -> None:
        """记录通过依赖校验后的本地关闭请求。"""
        assert user_id == self.user_id and connection_id == self.connection_id
        validate_capability_disable(capability, self.enabled)
        self.disabled = capability

    async def create_attempt(self, **values: Any) -> UUID:
        """捕获不含明文 verifier/state 的持久化参数。"""
        self.attempt = values
        return values["attempt_id"]

    async def validate_unbound_attempt_for_callback(self, **values: Any) -> None:
        """首次 callback 的单元 fake 没有并发数据库状态，只核对用户边界。"""
        assert values["user_id"] == self.user_id

    async def disconnect(self, **values: Any) -> tuple[bool, EncryptedValue | None]:
        """记录本地断开是否在供应商 adapter 解析前发生。"""
        assert values["user_id"] == self.user_id
        assert values["connection_id"] == self.connection_id
        self.disconnected = True
        return True, None

    async def record_oauth_revoke_unresolved(self, **values: Any) -> None:
        """断开端口现在必须记录未决维护事实，且只能在本地状态关闭后调用。"""
        assert self.disconnected
        assert values["user_id"] == self.user_id
        assert values["connection_id"] == self.connection_id


@pytest.mark.asyncio
async def test_disconnect_completes_local_state_when_provider_adapter_is_unavailable() -> None:
    """未装配 adapter 时也必须完成本地断开并返回稳定 unsupported 结果。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(user_id, connection_id)

    @asynccontextmanager
    async def stores():
        """暴露本地事务是否已被调用。"""
        yield store

    use_case = ConnectionsUseCase(stores, AeadCipher(b"x" * 32), {}, FixedClock())

    result = await use_case.disconnect(user_id=user_id, connection_id=connection_id)

    assert result is not None
    assert result.status is OAuthRevocationStatus.UNSUPPORTED
    assert result.error_code == "connection_provider_unsupported"
    assert store.disconnected is True


@pytest.mark.parametrize("access_token", ("", " ", "\t"))
def test_oauth_token_set_rejects_blank_access_token(access_token: str) -> None:
    """OAuth token port 不得把空白 access token 传入加密或供应商边界。"""
    with pytest.raises(ValueError, match="access_token"):
        OAuthTokenSet(
            access_token=access_token,
            refresh_token=None,
            expires_in=3600,
            granted_scopes=frozenset({"scope:mail.read"}),
        )


@pytest.mark.parametrize("expires_in", (0, -1, True))
def test_oauth_token_set_rejects_non_positive_expiry(expires_in: object) -> None:
    """OAuth token 有效期必须是正整数，不能接受 bool 或过期值。"""
    with pytest.raises(ValueError, match="expires_in"):
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=expires_in,  # type: ignore[arg-type]
            granted_scopes=frozenset({"scope:mail.read"}),
        )


@pytest.mark.parametrize("field_name", ("access_token", "refresh_token", "id_token"))
@pytest.mark.parametrize("value", ("token\x00value", "t" * 10_000))
def test_oauth_token_set_rejects_control_and_oversized_tokens(
    field_name: str,
    value: str,
) -> None:
    """OAuth opaque token 不能携带控制字符或超出受控内存边界。"""
    values: dict[str, object] = {
        "access_token": "synthetic-access",
        "refresh_token": None,
        "expires_in": 3600,
        "granted_scopes": frozenset({"scope:mail.read"}),
        "id_token": None,
    }
    values[field_name] = value
    with pytest.raises((TypeError, ValueError), match=field_name):
        OAuthTokenSet(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("access_token", 123),
        ("refresh_token", []),
        ("id_token", object()),
        ("refresh_token", " "),
        ("id_token", "\t"),
    ),
)
def test_oauth_token_set_rejects_non_string_or_blank_tokens(
    field_name: str,
    value: object,
) -> None:
    """所有可选/必需 token 字段都必须在端口边界保持规范字符串。"""
    values: dict[str, object] = {
        "access_token": "synthetic-access",
        "refresh_token": None,
        "expires_in": 3600,
        "granted_scopes": frozenset({"scope:mail.read"}),
        "id_token": None,
    }
    values[field_name] = value
    with pytest.raises((TypeError, ValueError), match=field_name):
        OAuthTokenSet(**values)  # type: ignore[arg-type]


def test_oauth_token_set_rejects_expiry_that_can_overflow_datetime() -> None:
    """异常大的有效期必须在进入 ``datetime + timedelta`` 前被拒绝。"""
    with pytest.raises(ValueError, match="expires_in"):
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=10**30,
            granted_scopes=frozenset({"scope:mail.read"}),
        )


@pytest.mark.parametrize(
    "granted_scopes",
    (
        {"scope:mail.read"},
        ["scope:mail.read"],
        frozenset({123}),
    ),
)
def test_oauth_token_set_requires_frozen_string_scope_set(granted_scopes: object) -> None:
    """实际 scope 必须是不可变字符串集合，避免松散容器绕过逐项校验。"""
    with pytest.raises((TypeError, ValueError), match="scope"):
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=3600,
            granted_scopes=granted_scopes,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("scope", ("scope with space", "scope:\x00value", "s" * 513))
def test_oauth_token_set_rejects_malformed_scopes(scope: str) -> None:
    """scope 是供应商权限语法，不得含分隔空白、控制字符或异常长值。"""
    with pytest.raises((TypeError, ValueError), match="scope"):
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=3600,
            granted_scopes=frozenset({scope}),
        )


@pytest.mark.parametrize(
    ("provider_account_id", "account_email"),
    (
        ("", "owner@example.test"),
        (" ", "owner@example.test"),
        ("subject", ""),
        ("subject", " "),
    ),
)
def test_oauth_account_rejects_blank_identity_fields(
    provider_account_id: str,
    account_email: str,
) -> None:
    """OAuth account 不能生成空 provider key 或空邮箱事实。"""
    with pytest.raises(ValueError):
        OAuthAccount(
            provider_account_id=provider_account_id,
            account_email=account_email,
            provider_tenant_id="",
            account_type="google",
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("provider_account_id", "i" * 256),
        ("provider_account_id", "id\x00value"),
        ("provider_tenant_id", "t" * 256),
        ("provider_tenant_id", "tenant\x00value"),
        ("account_email", "not-an-email"),
        ("account_email", "mail\x00@example.test"),
        ("account_type", "a" * 33),
        ("account_type", "work\x00school"),
    ),
)
def test_oauth_account_rejects_oversized_control_or_invalid_identity(
    field_name: str,
    value: str,
) -> None:
    """连接身份字段必须适配数据库上限并拒绝控制/明显非法邮箱值。"""
    values: dict[str, str] = {
        "provider_account_id": "synthetic-subject",
        "account_email": "owner@example.test",
        "provider_tenant_id": "",
        "account_type": "google",
    }
    values[field_name] = value
    with pytest.raises((TypeError, ValueError), match=field_name.removesuffix("_id")):
        OAuthAccount(**values)


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("provider_account_id", 123),
        ("account_email", []),
        ("provider_tenant_id", object()),
        ("account_type", None),
        ("provider_tenant_id", " "),
        ("account_type", "\t"),
    ),
)
def test_oauth_account_rejects_non_string_or_blank_identity_fields(
    field_name: str,
    value: object,
) -> None:
    """除 Google 精确空 tenant 外，身份字段不得接受非字符串或空白值。"""
    values: dict[str, object] = {
        "provider_account_id": "synthetic-subject",
        "account_email": "owner@example.test",
        "provider_tenant_id": "",
        "account_type": "google",
    }
    values[field_name] = value
    with pytest.raises((TypeError, ValueError), match=field_name.removesuffix("_id")):
        OAuthAccount(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_enable_mail_send_requests_dependency_union() -> None:
    """启用 ``mail.send`` 必须同时申请其固定 ``mail.read`` 依赖。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(user_id, connection_id)

    @asynccontextmanager
    async def stores():
        """让一次用例调用共享同一个可观察 fake 事务。"""
        yield store

    adapter = FakeOAuthAdapter()
    use_case = ConnectionsUseCase(
        stores,
        AeadCipher(b"k" * 32),
        {"google": adapter},
        FixedClock(),
    )

    result = await use_case.start_capability_enable(
        user_id=user_id,
        connection_id=connection_id,
        capability=ConnectionCapability.MAIL_SEND,
    )

    assert result.authorization_url == "https://provider.example.test/authorize"
    assert result.requested_capabilities == (
        ConnectionCapability.MAIL_READ,
        ConnectionCapability.MAIL_SEND,
    )
    assert adapter.requested_capabilities == frozenset(
        {ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND}
    )
    assert store.authorizing == adapter.requested_capabilities
    assert store.attempt["provider"] == "google"
    assert store.attempt["requested_capabilities"] == adapter.requested_capabilities


@pytest.mark.asyncio
async def test_enable_includes_all_currently_enabled_capabilities() -> None:
    """渐进授权不能丢失其他数据源已启用能力，必须申请完整并集。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(
        user_id,
        connection_id,
        enabled=frozenset({ConnectionCapability.CALENDAR_READ}),
    )

    @asynccontextmanager
    async def stores():
        """复用当前 enabled 状态的单事务 fake。"""
        yield store

    adapter = FakeOAuthAdapter()
    use_case = ConnectionsUseCase(
        stores,
        AeadCipher(b"u" * 32),
        {"google": adapter},
        FixedClock(),
    )

    result = await use_case.start_capability_enable(
        user_id=user_id,
        connection_id=connection_id,
        capability=ConnectionCapability.MAIL_SEND,
    )

    assert result.requested_capabilities == (
        ConnectionCapability.CALENDAR_READ,
        ConnectionCapability.MAIL_READ,
        ConnectionCapability.MAIL_SEND,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("calendar_write_enabled", [False, True])
@pytest.mark.parametrize("request_only_base_scope", [False, True])
async def test_reauthorization_preserves_verified_capabilities_after_pending_transition(
    calendar_write_enabled: bool,
    request_only_base_scope: bool,
) -> None:
    """首次请求改变状态后再重试，仍保留先前已验证的数据源，并创建新代际。"""
    user_id, connection_id = uuid4(), uuid4()
    initial = {ConnectionCapability.CALENDAR_READ}
    if calendar_write_enabled:
        initial.add(ConnectionCapability.CALENDAR_WRITE)
    store = FakeConnectionStore(user_id, connection_id, enabled=frozenset(initial))

    @asynccontextmanager
    async def stores():
        """两个请求独立进入用例事务，但共享模拟的持久状态。"""
        yield store

    adapter = FakeOAuthAdapter(
        base_scopes=frozenset({"offline_access"}) if request_only_base_scope else frozenset()
    )
    use_case = ConnectionsUseCase(stores, AeadCipher(b"p" * 32), {"google": adapter}, FixedClock())
    await use_case.start_capability_enable(
        user_id=user_id, connection_id=connection_id, capability=ConnectionCapability.MAIL_SEND
    )
    first_attempt_id = store.attempt["attempt_id"]
    assert store.enabled == frozenset()
    result = await use_case.start_capability_enable(
        user_id=user_id, connection_id=connection_id, capability=ConnectionCapability.MAIL_SEND
    )

    expected = {
        ConnectionCapability.CALENDAR_READ,
        ConnectionCapability.MAIL_READ,
        ConnectionCapability.MAIL_SEND,
    }
    if calendar_write_enabled:
        expected.add(ConnectionCapability.CALENDAR_WRITE)
    assert frozenset(result.requested_capabilities) == frozenset(expected)
    assert store.attempt["requested_capabilities"] == frozenset(expected)
    assert store.attempt["target_authorization_generation"] == 2
    assert store.attempt["attempt_id"] != first_attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_proof",
    ["disabled", "revoked", "action_required", "never_verified", "row_scope_missing", "current_scope_missing"],
)
async def test_reauthorization_does_not_revive_unproven_or_disabled_pending_write(
    invalid_proof: str,
) -> None:
    """过去的验证时间不能单独授权；状态、行 scope 与当前凭据 scope 都必须有效。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(
        user_id,
        connection_id,
        enabled=frozenset({ConnectionCapability.CALENDAR_READ, ConnectionCapability.CALENDAR_WRITE}),
    )

    @asynccontextmanager
    async def stores():
        """复用真实转换形状，负例只改变一项历史事实。"""
        yield store

    use_case = ConnectionsUseCase(
        stores, AeadCipher(b"p" * 32), {"google": FakeOAuthAdapter()}, FixedClock()
    )
    await use_case.start_capability_enable(
        user_id=user_id, connection_id=connection_id, capability=ConnectionCapability.MAIL_SEND
    )
    previous = store.capability_rows[ConnectionCapability.CALENDAR_WRITE]
    if invalid_proof in {"disabled", "revoked", "action_required"}:
        previous = replace(previous, status=CapabilityStatus(invalid_proof))
    elif invalid_proof == "never_verified":
        previous = replace(previous, last_verified_at=None)
    elif invalid_proof == "row_scope_missing":
        previous = replace(previous, actual_scopes=("scope:calendar.read",))
    else:
        store.granted_scopes = ("scope:calendar.read",)
    store.capability_rows[ConnectionCapability.CALENDAR_WRITE] = previous

    result = await use_case.start_capability_enable(
        user_id=user_id, connection_id=connection_id, capability=ConnectionCapability.MAIL_SEND
    )
    assert result.requested_capabilities == (
        ConnectionCapability.CALENDAR_READ,
        ConnectionCapability.MAIL_READ,
        ConnectionCapability.MAIL_SEND,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("read_status", [CapabilityStatus.DISABLED, CapabilityStatus.REVOKED])
async def test_reauthorization_cannot_restore_a_pending_write_with_disabled_read_dependency(
    read_status: CapabilityStatus,
) -> None:
    """即使写 scope 有历史证明，也不得借恢复闭包重新打开已关闭的读取依赖。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(
        user_id,
        connection_id,
        enabled=frozenset({ConnectionCapability.CALENDAR_READ, ConnectionCapability.CALENDAR_WRITE}),
    )

    @asynccontextmanager
    async def stores():
        """模拟新请求前已经存在的精确本地状态。"""
        yield store

    use_case = ConnectionsUseCase(
        stores, AeadCipher(b"p" * 32), {"google": FakeOAuthAdapter()}, FixedClock()
    )
    await use_case.start_capability_enable(
        user_id=user_id, connection_id=connection_id, capability=ConnectionCapability.MAIL_SEND
    )
    store.capability_rows[ConnectionCapability.CALENDAR_READ] = replace(
        store.capability_rows[ConnectionCapability.CALENDAR_READ], status=read_status
    )
    result = await use_case.start_capability_enable(
        user_id=user_id, connection_id=connection_id, capability=ConnectionCapability.MAIL_SEND
    )
    assert result.requested_capabilities == (
        ConnectionCapability.MAIL_READ,
        ConnectionCapability.MAIL_SEND,
    )


@pytest.mark.asyncio
async def test_initial_start_accepts_only_non_empty_read_capabilities() -> None:
    """首次连接可只选一个读取数据源，但空集合或写能力必须 fail closed。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(user_id, connection_id)

    @asynccontextmanager
    async def stores():
        """捕获首次 OAuth attempt。"""
        yield store

    adapter = FakeOAuthAdapter()
    use_case = ConnectionsUseCase(
        stores,
        AeadCipher(b"r" * 32),
        {"google": adapter},
        FixedClock(),
    )

    await use_case.start(
        user_id=user_id,
        provider=OAuthProvider.GOOGLE,
        capabilities=frozenset({ConnectionCapability.MAIL_READ}),
    )
    assert store.attempt["requested_capabilities"] == frozenset({ConnectionCapability.MAIL_READ})

    with pytest.raises(ConnectionCapabilityDependencyConflict):
        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset(),
        )
    with pytest.raises(ConnectionCapabilityDependencyConflict):
        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.GOOGLE,
            capabilities=frozenset({ConnectionCapability.MAIL_SEND}),
        )


@pytest.mark.asyncio
async def test_initial_start_requires_explicit_provider_and_capabilities() -> None:
    """首次连接调用方必须显式选择供应商与读取能力，不能依赖隐式授权默认值。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(user_id, connection_id)

    @asynccontextmanager
    async def stores():
        """提供可观察的 fake，证明缺参时不会创建 OAuth attempt。"""
        yield store

    use_case = ConnectionsUseCase(
        stores,
        AeadCipher(b"e" * 32),
        {"google": FakeOAuthAdapter()},
        FixedClock(),
    )

    with pytest.raises(TypeError):
        await use_case.start(user_id=user_id, provider=OAuthProvider.GOOGLE)
    with pytest.raises(TypeError):
        await use_case.start(
            user_id=user_id,
            capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        )
    assert store.attempt == {}


@pytest.mark.asyncio
async def test_unknown_provider_is_rejected_without_persisting_attempt() -> None:
    """未由组合根固定装配的供应商不能创建 OAuth attempt。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(user_id, connection_id)

    @asynccontextmanager
    async def stores():
        """提供可观察 attempt 的 fake 事务。"""
        yield store

    use_case = ConnectionsUseCase(
        stores,
        AeadCipher(b"p" * 32),
        {"google": FakeOAuthAdapter()},
        FixedClock(),
    )

    with pytest.raises(UnsupportedConnectionProviderError):
        await use_case.start(
            user_id=user_id,
            provider=OAuthProvider.MICROSOFT,
            capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        )
    assert store.attempt == {}


@pytest.mark.asyncio
async def test_disable_read_rejects_enabled_write_dependency() -> None:
    """``mail.send`` enabled 时必须先关闭写能力，不能直接关闭 ``mail.read``。"""
    user_id, connection_id = uuid4(), uuid4()
    store = FakeConnectionStore(
        user_id,
        connection_id,
        enabled=frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND}),
    )

    @asynccontextmanager
    async def stores():
        """提供包含完整邮件依赖的 fake 状态。"""
        yield store

    use_case = ConnectionsUseCase(
        stores,
        AeadCipher(b"d" * 32),
        {"google": FakeOAuthAdapter()},
        FixedClock(),
    )

    with pytest.raises(ConnectionCapabilityDependencyConflict):
        await use_case.disable_capability(
            user_id=user_id,
            connection_id=connection_id,
            capability=ConnectionCapability.MAIL_READ,
        )
    assert store.disabled is None

    result = await use_case.disable_capability(
        user_id=user_id,
        connection_id=connection_id,
        capability=ConnectionCapability.MAIL_SEND,
    )
    assert result.status is CapabilityStatus.DISABLED
    assert store.disabled is ConnectionCapability.MAIL_SEND


@dataclass(slots=True)
class CallbackOAuthAdapter:
    """返回指定实际 scope，并记录 callback 对每项能力执行的 scope 核对集合。"""

    token: OAuthTokenSet
    provider: OAuthProvider = OAuthProvider.GOOGLE
    expected_nonce_hash: bytes | None = None
    scope_requests: list[frozenset[ConnectionCapability]] = field(default_factory=list)
    failure_stage: str | None = None
    failure: Exception | None = None
    base_scopes: frozenset[str] = frozenset()

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """按能力返回逐项可核对的合成 scope。"""
        self.scope_requests.append(capabilities)
        return self.base_scopes | frozenset(f"scope:{capability.value}" for capability in capabilities)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """callback 测试不发起新授权。"""
        del request
        raise AssertionError("build_authorization_url must not be called")

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """验证 PKCE 明文只在解密后进入 adapter。"""
        assert code == "synthetic-code"
        assert verifier == "synthetic-verifier"
        if self.failure_stage == "exchange":
            assert self.failure is not None
            raise self.failure
        return self.token

    async def fetch_account(
        self,
        token: OAuthTokenSet,
        *,
        expected_nonce_hash: bytes | None,
    ) -> OAuthAccount:
        """记录用例传入的 nonce 摘要并返回规范账户。"""
        assert token is self.token
        self.expected_nonce_hash = expected_nonce_hash
        if self.failure_stage == "fetch":
            assert self.failure is not None
            raise self.failure
        return OAuthAccount(
            provider_account_id="callback-account",
            account_email="callback-account@example.test",
            provider_tenant_id="",
            account_type="google",
        )

    async def refresh(self, refresh_token: str) -> OAuthTokenSet:
        """callback 不刷新 token。"""
        del refresh_token
        raise AssertionError("refresh must not be called")

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """callback 不撤销 token。"""
        del token
        raise AssertionError("revoke must not be called")


@dataclass(slots=True)
class CallbackConnectionStore:
    """捕获 callback 的连接、token 与逐能力状态持久化。"""

    attempt: ConsumedOAuthAttempt
    connection_id: UUID
    consumed: bool = False
    saved_scopes: frozenset[str] = frozenset()
    saved_refresh: EncryptedValue | None = None
    capability_states: dict[ConnectionCapability, tuple[CapabilityStatus, str | None]] = field(
        default_factory=dict
    )
    failed_authorization: tuple[
        UUID,
        int,
        frozenset[ConnectionCapability],
        str,
    ] | None = None

    async def get_refresh_events(self, **values: Any) -> tuple[()]:
        """普通 callback fixture 没有自动 refresh 历史，不伪造恢复事件。"""
        assert values["user_id"] == self.attempt.user_id
        return ()

    async def consume_attempt(
        self,
        *,
        state_hash: bytes,
        now: datetime,
    ) -> ConsumedOAuthAttempt | None:
        """一次返回预置 attempt，并模拟单次消费。"""
        del state_hash, now
        if self.consumed:
            return None
        self.consumed = True
        return self.attempt

    async def validate_unbound_attempt_for_callback(self, **values: Any) -> None:
        """预置 attempt 已在 fake 中消费，允许测试继续验证 token 保存语义。"""
        assert values["user_id"] == self.attempt.user_id
        assert values["attempt_id"] == self.attempt.id

    async def ensure_connection(self, **values: Any) -> UUID:
        """记录实际 granted scopes 并返回稳定连接。"""
        self.saved_scopes = values["scopes"]
        return self.connection_id

    async def validate_unfenced_identity_for_callback(self, **values: Any) -> None:
        """该普通 callback fixture 没有历史 fence，仍校验调用方限定了当前用户。"""
        assert values["user_id"] == self.attempt.user_id

    async def save_connection_tokens(self, **values: Any) -> None:
        """记录 callback 是否错误制造了 refresh 密文。"""
        self.saved_refresh = values["refresh_token"]

    async def save_capability_state(
        self,
        *,
        capability: ConnectionCapability,
        status: CapabilityStatus,
        last_error_code: str | None,
        **values: Any,
    ) -> None:
        """捕获逐能力验证结果。"""
        del values
        self.capability_states[capability] = (status, last_error_code)

    async def mark_progressive_authorization_failed(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        authorization_generation: int,
        capabilities: frozenset[ConnectionCapability],
        error_code: str,
    ) -> None:
        """记录失败收敛调用，模拟真实仓储的代际条件更新。"""
        assert user_id == self.attempt.user_id
        assert connection_id == self.connection_id
        self.failed_authorization = (
            connection_id,
            authorization_generation,
            capabilities,
            error_code,
        )


@dataclass(slots=True)
class StateProbeConnectionStore:
    """记录非法 state 是否错误进入数据库消费边界。"""

    consume_calls: int = 0

    async def consume_attempt(
        self,
        *,
        state_hash: bytes,
        now: datetime,
    ) -> ConsumedOAuthAttempt | None:
        """非法 state 不应到达此方法；若到达则记录并返回缺失。"""
        del state_hash, now
        self.consume_calls += 1
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    (
        "非ASCII状态",
        "invalid+state/value",
        "padded=",
        "white space",
        "a" * 42,
    ),
)
async def test_callback_rejects_noncanonical_state_before_store_access(state: str) -> None:
    """非 ASCII、非法字符、padding、空白或错误长度都须统一拒绝且不查询数据库。"""
    store = StateProbeConnectionStore()

    @asynccontextmanager
    async def stores():
        """暴露是否发生了非法 state 数据库查询。"""
        yield store

    use_case = ConnectionsUseCase(
        stores,
        AeadCipher(b"s" * 32),
        {"google": FakeOAuthAdapter()},
        FixedClock(),
    )

    with pytest.raises(OAuthStateRejectedError):
        await use_case.callback(code="synthetic-code", state=state)
    assert store.consume_calls == 0


def _callback_scenario(
    *,
    requested_capabilities: frozenset[ConnectionCapability],
    granted_scopes: frozenset[str],
) -> tuple[ConnectionsUseCase, CallbackConnectionStore, CallbackOAuthAdapter]:
    """构造一次真实 callback 编排所需的加密 verifier、attempt、fake adapter 与 store。"""
    user_id, attempt_id, connection_id = uuid4(), uuid4(), uuid4()
    cipher = AeadCipher(b"v" * 32)
    verifier = cipher.encrypt(
        b"synthetic-verifier",
        f"{user_id}:{attempt_id}:pkce_verifier".encode("ascii"),
    )
    store = CallbackConnectionStore(
        ConsumedOAuthAttempt(
            id=attempt_id,
            user_id=user_id,
            provider="google",
            requested_capabilities=requested_capabilities,
            verifier=verifier,
            oidc_nonce_hash=b"n" * 32,
        ),
        connection_id,
    )

    @asynccontextmanager
    async def stores():
        """让 callback 的 state 消费与结果保存共享同一可观察事务。"""
        yield store

    adapter = CallbackOAuthAdapter(
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=3600,
            granted_scopes=granted_scopes,
        )
    )
    return (
        ConnectionsUseCase(stores, cipher, {"google": adapter}, FixedClock()),
        store,
        adapter,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_capabilities", "granted_scopes", "expected_states"),
    (
        (
            frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND}),
            frozenset({"scope:mail.send"}),
            {
                ConnectionCapability.MAIL_READ: (
                    CapabilityStatus.ACTION_REQUIRED,
                    "connection_scope_missing",
                ),
                ConnectionCapability.MAIL_SEND: (
                    CapabilityStatus.ACTION_REQUIRED,
                    "connection_scope_missing",
                ),
            },
        ),
        (
            frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND}),
            frozenset({"scope:mail.read", "scope:mail.send"}),
            {
                ConnectionCapability.MAIL_READ: (CapabilityStatus.ENABLED, None),
                ConnectionCapability.MAIL_SEND: (CapabilityStatus.ENABLED, None),
            },
        ),
        (
            frozenset(
                {
                    ConnectionCapability.MAIL_READ,
                    ConnectionCapability.CALENDAR_READ,
                    ConnectionCapability.CALENDAR_WRITE,
                }
            ),
            frozenset({"scope:mail.read", "scope:calendar.write"}),
            {
                ConnectionCapability.CALENDAR_READ: (
                    CapabilityStatus.ACTION_REQUIRED,
                    "connection_scope_missing",
                ),
                ConnectionCapability.CALENDAR_WRITE: (
                    CapabilityStatus.ACTION_REQUIRED,
                    "connection_scope_missing",
                ),
                ConnectionCapability.MAIL_READ: (CapabilityStatus.ENABLED, None),
            },
        ),
    ),
)
async def test_callback_verifies_write_capabilities_with_dependency_closure(
    requested_capabilities: frozenset[ConnectionCapability],
    granted_scopes: frozenset[str],
    expected_states: dict[ConnectionCapability, tuple[CapabilityStatus, str | None]],
) -> None:
    """写能力必须同时满足自身与读取依赖，混合授权也不能逐项漏检依赖。"""
    use_case, store, adapter = _callback_scenario(
        requested_capabilities=requested_capabilities,
        granted_scopes=granted_scopes,
    )

    await use_case.callback(code="synthetic-code", state=_VALID_OAUTH_STATE)

    assert store.capability_states == expected_states
    if ConnectionCapability.MAIL_SEND in requested_capabilities:
        assert (
            frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND})
            in adapter.scope_requests
        )
    if ConnectionCapability.CALENDAR_WRITE in requested_capabilities:
        assert (
            frozenset({ConnectionCapability.CALENDAR_READ, ConnectionCapability.CALENDAR_WRITE})
            in adapter.scope_requests
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("calendar_scope_returned", [False, True])
async def test_callback_does_not_require_request_only_base_scopes_in_token_response(
    calendar_scope_returned: bool,
) -> None:
    """离线访问请求 scope 不回显时仍按资源权限启用；真正缺少日历权限则保持阻断。"""
    granted = {"openid", "scope:mail.read"}
    if calendar_scope_returned:
        granted.add("scope:calendar.read")
    use_case, store, adapter = _callback_scenario(
        requested_capabilities=frozenset(
            {ConnectionCapability.MAIL_READ, ConnectionCapability.CALENDAR_READ}
        ),
        granted_scopes=frozenset(granted),
    )
    adapter.base_scopes = frozenset({"openid", "offline_access"})

    await use_case.callback(code="synthetic-code", state=_VALID_OAUTH_STATE)

    assert store.capability_states == {
        ConnectionCapability.MAIL_READ: (CapabilityStatus.ENABLED, None),
        ConnectionCapability.CALENDAR_READ: (
            (CapabilityStatus.ENABLED, None)
            if calendar_scope_returned
            else (CapabilityStatus.ACTION_REQUIRED, "connection_scope_missing")
        ),
    }
    assert store.saved_scopes == frozenset(granted)
    assert "offline_access" not in store.saved_scopes


@pytest.mark.asyncio
async def test_callback_marks_missing_scope_action_required_and_keeps_refresh_none() -> None:
    """只有实际 scope 完整满足的能力 enabled，缺失写 scope 必须要求重新授权。"""
    user_id, attempt_id, connection_id = uuid4(), uuid4(), uuid4()
    cipher = AeadCipher(b"c" * 32)
    verifier = cipher.encrypt(
        b"synthetic-verifier",
        f"{user_id}:{attempt_id}:pkce_verifier".encode("ascii"),
    )
    nonce_hash = b"n" * 32
    attempt = ConsumedOAuthAttempt(
        id=attempt_id,
        user_id=user_id,
        provider="google",
        requested_capabilities=frozenset(
            {ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND}
        ),
        verifier=verifier,
        oidc_nonce_hash=nonce_hash,
    )
    store = CallbackConnectionStore(attempt, connection_id)

    @asynccontextmanager
    async def stores():
        """让 state 消费与结果保存共享同一可观察 fake。"""
        yield store

    adapter = CallbackOAuthAdapter(
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=3600,
            granted_scopes=frozenset({"scope:mail.read"}),
        )
    )
    use_case = ConnectionsUseCase(
        stores,
        cipher,
        {"google": adapter},
        FixedClock(),
    )

    result = await use_case.callback(code="synthetic-code", state=_VALID_OAUTH_STATE)

    assert result == connection_id
    assert adapter.expected_nonce_hash == nonce_hash
    assert store.saved_scopes == frozenset({"scope:mail.read"})
    assert store.saved_refresh is None
    assert store.capability_states == {
        ConnectionCapability.MAIL_READ: (CapabilityStatus.ENABLED, None),
        ConnectionCapability.MAIL_SEND: (
            CapabilityStatus.ACTION_REQUIRED,
            "connection_scope_missing",
        ),
    }


@pytest.mark.asyncio
async def test_progressive_callback_failure_converges_authorizing_capabilities() -> None:
    """渐进 callback 在供应商失败后保留原错误并收敛本次能力状态。"""
    user_id, attempt_id, connection_id = uuid4(), uuid4(), uuid4()
    cipher = AeadCipher(b"f" * 32)
    verifier = cipher.encrypt(
        b"synthetic-verifier",
        f"{user_id}:{attempt_id}:pkce_verifier".encode("ascii"),
    )
    requested = frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND})
    attempt = ConsumedOAuthAttempt(
        id=attempt_id,
        user_id=user_id,
        provider="google",
        requested_capabilities=requested,
        verifier=verifier,
        oidc_nonce_hash=b"n" * 32,
        target_connection_id=connection_id,
        target_authorization_generation=7,
    )
    store = CallbackConnectionStore(attempt, connection_id)

    @asynccontextmanager
    async def stores():
        """让失败收敛事务可被 fake 记录。"""
        yield store

    failure = TransientProviderError(
        error_code="google_oauth_timeout",
        message="safe provider failure",
    )
    adapter = CallbackOAuthAdapter(
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=3600,
            granted_scopes=frozenset({"scope:mail.read"}),
        ),
        failure_stage="exchange",
        failure=failure,
    )
    use_case = ConnectionsUseCase(stores, cipher, {"google": adapter}, FixedClock())

    with pytest.raises(TransientProviderError) as raised:
        await use_case.callback(code="synthetic-code", state=_VALID_OAUTH_STATE)

    assert raised.value is failure
    assert store.failed_authorization == (
        connection_id,
        7,
        requested,
        "oauth_authorization_failed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stable_code",
    (
        "google_reauthorization_required",
        "microsoft_admin_consent_required",
        "microsoft_reauthorization_required",
    ),
)
async def test_progressive_known_failure_preserves_safe_classification(stable_code: str) -> None:
    """已由适配器分类的交互/同意失败必须同步持久状态，不能被通用错误抹平。"""
    user_id, attempt_id, connection_id = uuid4(), uuid4(), uuid4()
    cipher = AeadCipher(b"f" * 32)
    provider = "microsoft" if stable_code.startswith("microsoft_") else "google"
    requested = frozenset({ConnectionCapability.MAIL_READ})
    store = CallbackConnectionStore(
        ConsumedOAuthAttempt(
            id=attempt_id,
            user_id=user_id,
            provider=provider,
            requested_capabilities=requested,
            verifier=cipher.encrypt(
                b"synthetic-verifier", f"{user_id}:{attempt_id}:pkce_verifier".encode("ascii")
            ),
            oidc_nonce_hash=None,
            target_connection_id=connection_id,
            target_authorization_generation=7,
        ),
        connection_id,
    )

    @asynccontextmanager
    async def stores():
        """只替代持久端口；实际分类与用例异常路径保持真实。"""
        yield store

    failure = UserActionRequiredError(error_code=stable_code, message="Safe synthetic failure")
    adapter = CallbackOAuthAdapter(
        OAuthTokenSet("synthetic-access", None, 3600, frozenset({"scope:mail.read"})),
        provider=OAuthProvider(provider),
        failure_stage="exchange",
        failure=failure,
    )
    use_case = ConnectionsUseCase(stores, cipher, {provider: adapter}, FixedClock())

    with pytest.raises(UserActionRequiredError) as raised:
        await use_case.callback(code="synthetic-code", state=_VALID_OAUTH_STATE)

    assert raised.value is failure
    assert store.failed_authorization == (connection_id, 7, requested, stable_code)
    assert store.saved_scopes == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    (
        StateConflictError(
            error_code="synthetic_state_conflict",
            message="synthetic state conflict",
        ),
        OAuthAttemptInvalidatedError(),
    ),
)
async def test_callback_does_not_converge_state_conflicts_from_adapter(
    failure: StateConflictError,
) -> None:
    """adapter 边界的状态冲突必须原样传播，不能伪装成授权失败。"""
    user_id, attempt_id, connection_id = uuid4(), uuid4(), uuid4()
    cipher = AeadCipher(b"g" * 32)
    verifier = cipher.encrypt(
        b"synthetic-verifier",
        f"{user_id}:{attempt_id}:pkce_verifier".encode("ascii"),
    )
    attempt = ConsumedOAuthAttempt(
        id=attempt_id,
        user_id=user_id,
        provider="google",
        requested_capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        verifier=verifier,
        oidc_nonce_hash=b"n" * 32,
        target_connection_id=connection_id,
        target_authorization_generation=8,
    )
    store = CallbackConnectionStore(attempt, connection_id)

    @asynccontextmanager
    async def stores():
        """让 callback 的补偿调用可被断言。"""
        yield store

    adapter = CallbackOAuthAdapter(
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=3600,
            granted_scopes=frozenset({"scope:mail.read"}),
        ),
        failure_stage="exchange",
        failure=failure,
    )
    use_case = ConnectionsUseCase(stores, cipher, {"google": adapter}, FixedClock())

    with pytest.raises(StateConflictError) as raised:
        await use_case.callback(code="synthetic-code", state=_VALID_OAUTH_STATE)

    assert raised.value is failure
    assert store.failed_authorization is None


@pytest.mark.asyncio
async def test_callback_does_not_converge_when_adapter_lookup_fails() -> None:
    """未知或未装配 provider 在 lookup 阶段失败时不应触发渐进状态补偿。"""
    user_id, attempt_id, connection_id = uuid4(), uuid4(), uuid4()
    cipher = AeadCipher(b"h" * 32)
    verifier = cipher.encrypt(
        b"synthetic-verifier",
        f"{user_id}:{attempt_id}:pkce_verifier".encode("ascii"),
    )
    attempt = ConsumedOAuthAttempt(
        id=attempt_id,
        user_id=user_id,
        provider="microsoft",
        requested_capabilities=frozenset({ConnectionCapability.MAIL_READ}),
        verifier=verifier,
        oidc_nonce_hash=b"n" * 32,
        target_connection_id=connection_id,
        target_authorization_generation=9,
    )
    store = CallbackConnectionStore(attempt, connection_id)

    @asynccontextmanager
    async def stores():
        """让 callback 的补偿调用可被断言。"""
        yield store

    use_case = ConnectionsUseCase(stores, cipher, {"google": CallbackOAuthAdapter(
        OAuthTokenSet(
            access_token="synthetic-access",
            refresh_token=None,
            expires_in=3600,
            granted_scopes=frozenset({"scope:mail.read"}),
        )
    )}, FixedClock())

    with pytest.raises(UnsupportedConnectionProviderError):
        await use_case.callback(code="synthetic-code", state=_VALID_OAUTH_STATE)

    assert store.failed_authorization is None
