"""验证 Microsoft 测试模式适配器只提供合成事实且绝不联网。"""

import base64
import hashlib
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ai_employee.api.deps import get_connections_use_case
from ai_employee.application.ports.oauth import OAuthAuthorizationRequest
from ai_employee.config import get_settings
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.integrations.microsoft.fake import FakeMicrosoftOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MICROSOFT_BASE_SCOPES
from ai_employee.main import create_app


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
