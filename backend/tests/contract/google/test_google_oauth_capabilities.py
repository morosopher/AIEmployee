"""Google 渐进授权适配器的脱敏供应商契约测试。"""

from hashlib import sha256
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from ai_employee.application.ports.oauth import OAuthAuthorizationRequest, OAuthTokenSet
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.integrations.google.oauth import (
    GOOGLE_BASE_SCOPES,
    GOOGLE_REVOKE_URL,
    GOOGLE_TOKEN_INFO_URL,
    GoogleOAuthAdapter,
)


def _adapter() -> GoogleOAuthAdapter:
    """构造只使用合成配置的 Google adapter，测试绝不连接真实账户。"""
    return GoogleOAuthAdapter(
        client_id="synthetic-client",
        client_secret="synthetic-secret",
        redirect_uri="https://app.example.test/callback",
    )


def test_google_scope_union_is_minimal_and_deterministic() -> None:
    """能力到 scope 的映射必须最小且不能跨数据源隐式扩大。"""
    adapter = _adapter()

    assert adapter.scopes_for(frozenset({ConnectionCapability.MAIL_READ})) == frozenset(
        {
            *GOOGLE_BASE_SCOPES,
            "https://www.googleapis.com/auth/gmail.readonly",
        }
    )
    mail_scopes = adapter.scopes_for(
        frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND})
    )
    assert "https://www.googleapis.com/auth/gmail.send" in mail_scopes
    assert "https://www.googleapis.com/auth/calendar.events" not in mail_scopes
    assert type(mail_scopes) is frozenset


def test_google_authorization_url_uses_requested_scopes_and_oidc_nonce() -> None:
    """授权 URL 必须解析出精确请求 scope、离线参数、PKCE 与 nonce。"""
    request = OAuthAuthorizationRequest(
        state="synthetic-state",
        code_challenge="synthetic-challenge",
        requested_scopes=frozenset(
            {
                "openid",
                "email",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            }
        ),
        oidc_nonce="synthetic-nonce",
    )

    query = parse_qs(urlparse(_adapter().build_authorization_url(request)).query)
    assert set(query["scope"][0].split()) == request.requested_scopes
    assert query["include_granted_scopes"] == ["true"]
    assert query["access_type"] == ["offline"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [request.state]
    assert query["nonce"] == [request.oidc_nonce]
    assert "contacts" not in query["scope"][0].lower()
    assert "draft" not in query["scope"][0].lower()


@pytest.mark.asyncio
@respx.mock
async def test_token_scope_is_normalized_and_id_token_is_retained() -> None:
    """token endpoint 的 scope 字符串应成为 frozenset，id_token 不能被丢弃。"""
    route = respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
                "scope": "email openid https://www.googleapis.com/auth/gmail.readonly",
                "id_token": "synthetic-id-token",
            },
        )
    )

    token = await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert route.called
    assert token.granted_scopes == frozenset(
        {
            "email",
            "openid",
            "https://www.googleapis.com/auth/gmail.readonly",
        }
    )
    assert token.id_token == "synthetic-id-token"


@pytest.mark.asyncio
@respx.mock
async def test_missing_token_scope_uses_token_info_boundary() -> None:
    """scope 缺失时必须通过 token-info 核验，而不是假设请求被授予。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": "synthetic-access", "expires_in": 3600},
        )
    )
    info = respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={"scope": "openid email https://www.googleapis.com/auth/calendar.readonly"},
        )
    )

    token = await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert info.called
    assert token.granted_scopes == frozenset(
        {
            "openid",
            "email",
            "https://www.googleapis.com/auth/calendar.readonly",
        }
    )


@pytest.mark.asyncio
@respx.mock
async def test_refresh_without_rotated_refresh_token_returns_none() -> None:
    """Google 刷新响应未轮换 refresh token 时，适配器必须保留 ``None`` 语义。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed-access",
                "expires_in": 3600,
                "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
            },
        )
    )

    token = await _adapter().refresh("existing-refresh")

    assert token.access_token == "refreshed-access"
    assert token.refresh_token is None


@pytest.mark.asyncio
@respx.mock
async def test_revoke_reports_revoked_only_after_google_success() -> None:
    """Google 撤销端点明确返回 2xx 后才可报告 ``REVOKED``。"""
    route = respx.post(GOOGLE_REVOKE_URL).mock(return_value=httpx.Response(204))

    result = await _adapter().revoke("synthetic-refresh")

    assert route.called
    assert result.status.value == "revoked"


@pytest.mark.asyncio
@respx.mock
async def test_token_info_http_failure_does_not_retain_access_token() -> None:
    """token-info 非成功响应的异常、请求和上下文都不得携带 access token。"""
    sensitive_access_token = "synthetic-sensitive-access-token"
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": sensitive_access_token, "expires_in": 3600},
        )
    )
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(400, json={"error": "synthetic-invalid-token"})
    )

    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert sensitive_access_token not in str(raised.value)
    assert sensitive_access_token not in str(raised.value.request.url)
    assert sensitive_access_token not in str(raised.value.response.request.url)
    assert raised.value.response.status_code == 400
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_token_info_transport_failure_does_not_retain_access_token() -> None:
    """token-info 传输异常保持原分类，但异常 request 不得保留 access token。"""
    sensitive_access_token = "synthetic-sensitive-access-token"
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={"access_token": sensitive_access_token, "expires_in": 3600},
        )
    )

    def fail_token_info(request: httpx.Request) -> httpx.Response:
        """构造携带原始请求的连接异常，复现 httpx 默认敏感 URL 传播。"""
        raise httpx.ConnectError("synthetic token-info transport failure", request=request)

    respx.get(GOOGLE_TOKEN_INFO_URL).mock(side_effect=fail_token_info)

    with pytest.raises(httpx.ConnectError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert sensitive_access_token not in str(raised.value.request.url)
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_nonce_requires_verified_token_info_claim() -> None:
    """adapter 不能通过无签名 JWT 解码伪造 OIDC nonce 绑定。"""
    nonce = "synthetic-nonce"
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "nonce": nonce,
                "sub": "synthetic-subject",
                "email": "owner@example.test",
            },
        )
    )
    respx.get("https://openidconnect.googleapis.com/v1/userinfo").mock(
        return_value=httpx.Response(
            200,
            json={"sub": "synthetic-subject", "email": "owner@example.test"},
        )
    )
    token = OAuthTokenSet(
        access_token="synthetic-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({"openid", "email"}),
        id_token="synthetic-id-token",
    )

    account = await _adapter().fetch_account(
        token,
        expected_nonce_hash=sha256(nonce.encode("utf-8")).digest(),
    )

    assert account.provider_account_id == "synthetic-subject"


@pytest.mark.asyncio
@respx.mock
async def test_malformed_scope_is_rejected_without_token_info_fallback() -> None:
    """非字符串、空白、控制字符或超长 scope 必须 fail closed。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "expires_in": 3600,
                "scope": "openid\nemail",
            },
        )
    )
    info = respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(200, json={"scope": "openid"})
    )

    with pytest.raises((TypeError, ValueError)):
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert not info.called
