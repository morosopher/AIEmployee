"""验证供应商中立连接能力编排的依赖闭包与授权边界。"""

import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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
    ConnectionsUseCase,
    ConsumedOAuthAttempt,
    OAuthStateRejectedError,
    StoredConnection,
    UnsupportedConnectionProviderError,
)
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    ConnectionCapabilityDependencyConflict,
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

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """返回可预测 scope，并记录用例传入的完整能力并集。"""
        self.requested_capabilities = capabilities
        return frozenset(f"scope:{capability.value}" for capability in capabilities)

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
            scopes=(),
            status="connected",
            last_error_code=None,
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
        """记录本次需要进入授权中的目标能力闭包。"""
        assert user_id == self.user_id and connection_id == self.connection_id
        self.authorizing = capabilities
        return 1

    async def disable_capability(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        capability: ConnectionCapability,
    ) -> None:
        """记录通过依赖校验后的本地关闭请求。"""
        assert user_id == self.user_id and connection_id == self.connection_id
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

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """按能力返回逐项可核对的合成 scope。"""
        self.scope_requests.append(capabilities)
        return frozenset(f"scope:{capability.value}" for capability in capabilities)

    def build_authorization_url(self, request: OAuthAuthorizationRequest) -> str:
        """callback 测试不发起新授权。"""
        del request
        raise AssertionError("build_authorization_url must not be called")

    async def exchange_code(self, *, code: str, verifier: str) -> OAuthTokenSet:
        """验证 PKCE 明文只在解密后进入 adapter。"""
        assert code == "synthetic-code"
        assert verifier == "synthetic-verifier"
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
