"""验证 Microsoft 测试模式适配器只提供合成事实且绝不联网。"""

import base64
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest

from ai_employee.api.deps import get_connections_use_case
from ai_employee.application.ports.oauth import OAuthAuthorizationRequest
from ai_employee.application.use_cases.connections import ConsumedOAuthAttempt
from ai_employee.config import get_settings
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.integrations.microsoft.fake import FakeMicrosoftOAuthAdapter
from ai_employee.integrations.microsoft.oauth import (
    MICROSOFT_BASE_SCOPES,
    MicrosoftOAuthAdapter,
)
from ai_employee.main import create_app


@dataclass(slots=True)
class _CallbackStore:
    """为 test-mode callback 提供只保存标识的最小内存端口。"""

    attempt: ConsumedOAuthAttempt
    connection_id: UUID
    consumed: bool = False

    async def consume_attempt(self, **values: object) -> ConsumedOAuthAttempt | None:
        """模拟一次性 state 消费，不接触网络或数据库。"""
        del values
        if self.consumed:
            return None
        self.consumed = True
        return self.attempt

    async def validate_unbound_attempt_for_callback(self, **values: object) -> None:
        """核对 callback 仍属于预置用户和 attempt。"""
        assert values["attempt_id"] == self.attempt.id
        assert values["user_id"] == self.attempt.user_id

    async def ensure_connection(self, **values: object) -> UUID:
        """返回稳定合成连接 ID。"""
        del values
        return self.connection_id

    async def save_connection_tokens(self, **values: object) -> None:
        """记录 token 保存边界；测试不保存任何真实凭据。"""
        del values

    async def save_capability_state(self, **values: object) -> None:
        """接受能力状态投影，避免 callback 流程触及具体 ORM。"""
        del values


@dataclass(frozen=True, slots=True)
class _CallbackClock:
    """提供 callback 所需的固定 UTC 时间。"""

    current: datetime = datetime(2030, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        """返回固定时钟值。"""
        return self.current


@pytest.mark.asyncio
async def test_fake_microsoft_oauth_exchange_refresh_and_account_are_offline() -> None:
    """fake 的 callback/refresh 边界返回固定值，不构造或调用 HTTP 客户端。"""
    adapter = FakeMicrosoftOAuthAdapter(
        client_id="synthetic-client",
        redirect_uri="https://app.example.test/callback",
    )
    verifier = "synthetic-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    request = OAuthAuthorizationRequest(
        state="synthetic-state",
        code_challenge=challenge,
        requested_scopes=adapter.scopes_for(frozenset({ConnectionCapability.MAIL_READ})),
        oidc_nonce="synthetic-nonce",
    )

    assert "login.microsoftonline.com" in adapter.build_authorization_url(request)
    exchanged = await adapter.exchange_code(code="ignored", verifier=verifier)
    account = await adapter.fetch_account(
        exchanged,
        expected_nonce_hash=sha256(b"synthetic-nonce").digest(),
    )
    assert exchanged.refresh_token is not None
    refreshed = await adapter.refresh(exchanged.refresh_token)

    assert account.provider_account_id == "synthetic-tenant:synthetic-microsoft-user"
    assert account.account_email == "test-mode@microsoft.example.test"
    assert exchanged.access_token == "fake-microsoft-access"
    assert exchanged.granted_scopes == frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read"})
    assert refreshed.access_token == "fake-microsoft-refreshed-access"
    assert refreshed.granted_scopes == exchanged.granted_scopes

    calendar_verifier = "synthetic-calendar-verifier"
    calendar_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(calendar_verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    calendar_request = OAuthAuthorizationRequest(
        state="synthetic-calendar-state",
        code_challenge=calendar_challenge,
        requested_scopes=adapter.scopes_for(frozenset({ConnectionCapability.CALENDAR_READ})),
        oidc_nonce="synthetic-calendar-nonce",
    )
    adapter.build_authorization_url(calendar_request)
    calendar_token = await adapter.exchange_code(
        code="ignored-calendar",
        verifier=calendar_verifier,
    )
    assert calendar_token.granted_scopes == frozenset(
        {*MICROSOFT_BASE_SCOPES, "Calendars.Read"}
    )


@pytest.mark.asyncio
async def test_app_test_mode_composes_fake_microsoft_adapter_without_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """APP_TEST_MODE 组合根必须装配 fake，callback/refresh 不得创建真实 HTTP。"""
    master_key = tmp_path / "master-key"
    master_key.write_text(
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_TEST_MODE", "true")
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(master_key))
    get_settings.cache_clear()
    app = create_app()

    class NoHttpClient:
        """若测试模式错误创建 HTTP client，立即让测试失败。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("test mode must not construct Microsoft HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", NoHttpClient)
    try:
        use_case = get_connections_use_case(SimpleNamespace(app=app))
        adapter = use_case._adapters["microsoft"]
        assert isinstance(adapter, FakeMicrosoftOAuthAdapter)
        exchanged = await adapter.exchange_code(code="ignored", verifier="ignored")
        refreshed = await adapter.refresh("ignored-refresh")
        assert exchanged.access_token == "fake-microsoft-access"
        assert refreshed.access_token == "fake-microsoft-refreshed-access"
    finally:
        await app.state.auth_session_factory.dispose()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_app_test_mode_replaces_injected_real_microsoft_adapter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """测试模式即使显式注入真实 adapter，也必须替换为 fake 后再执行 callback/refresh。"""
    master_key = tmp_path / "master-key"
    master_key.write_text(
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_TEST_MODE", "true")
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(master_key))
    get_settings.cache_clear()
    app = create_app()
    app.state.oauth_adapters = {
        "microsoft": MicrosoftOAuthAdapter(
            "synthetic-real-client",
            "synthetic-real-secret",
            "https://app.example.test/callback",
        )
    }

    class NoHttpClient:
        """若真实 adapter 越过隔离边界构造 client，立即让测试失败。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("APP_TEST_MODE must not construct a real HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", NoHttpClient)
    try:
        use_case = get_connections_use_case(SimpleNamespace(app=app))
        adapter = use_case._adapters["microsoft"]
        assert isinstance(adapter, FakeMicrosoftOAuthAdapter)
        exchanged = await adapter.exchange_code(code="ignored", verifier="ignored")
        refreshed = await adapter.refresh("ignored-refresh")
        assert exchanged.access_token == "fake-microsoft-access"
        assert refreshed.access_token == "fake-microsoft-refreshed-access"
    finally:
        await app.state.auth_session_factory.dispose()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_test_mode_callback_with_injected_real_adapter_stays_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """完整 ConnectionsUseCase callback 在 test mode 下也不能触发真实 Microsoft HTTP。"""
    master_key = tmp_path / "master-key"
    master_key.write_text(
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_TEST_MODE", "true")
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(master_key))
    get_settings.cache_clear()
    app = create_app()
    app.state.oauth_adapters = {
        "microsoft": MicrosoftOAuthAdapter(
            "synthetic-real-client",
            "synthetic-real-secret",
            "https://app.example.test/callback",
        )
    }

    class NoHttpClient:
        """若 callback 越过 test-mode 隔离构造 HTTP client，立即失败。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise AssertionError("test-mode callback must not construct a real HTTP client")

    monkeypatch.setattr(httpx, "AsyncClient", NoHttpClient)
    try:
        use_case = get_connections_use_case(SimpleNamespace(app=app))
        user_id, attempt_id, connection_id = uuid4(), uuid4(), uuid4()
        verifier = "synthetic-callback-verifier"
        encrypted_verifier = use_case._cipher.encrypt(
            verifier.encode("ascii"),
            f"{user_id}:{attempt_id}:pkce_verifier".encode("ascii"),
        )
        store = _CallbackStore(
            attempt=ConsumedOAuthAttempt(
                id=attempt_id,
                user_id=user_id,
                provider="microsoft",
                requested_capabilities=frozenset({ConnectionCapability.MAIL_READ}),
                verifier=encrypted_verifier,
                oidc_nonce_hash=None,
            ),
            connection_id=connection_id,
        )

        @asynccontextmanager
        async def stores():
            """把 callback 的持久化边界替换为内存 fake。"""
            yield store

        use_case._stores = stores
        use_case._clock = _CallbackClock()
        state = base64.urlsafe_b64encode(b"s" * 32).rstrip(b"=").decode("ascii")
        result = await use_case.callback(code="synthetic-code", state=state)
        assert result == connection_id
    finally:
        await app.state.auth_session_factory.dispose()
        get_settings.cache_clear()
