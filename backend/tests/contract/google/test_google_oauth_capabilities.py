"""Google 渐进授权适配器的脱敏供应商契约测试。"""

from hashlib import sha256
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from ai_employee.application.ports.oauth import OAuthAuthorizationRequest, OAuthTokenSet
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)
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
async def test_google_response_userinfo_email_alias_is_canonicalized() -> None:
    """Google 响应可能返回 userinfo.email 别名，但本地事实必须归一为 email。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "expires_in": 3600,
                "scope": (
                    "openid email https://www.googleapis.com/auth/userinfo.email "
                    "https://www.googleapis.com/auth/gmail.readonly"
                ),
            },
        )
    )

    token = await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert token.granted_scopes == frozenset(
        {
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
        }
    )


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

    with pytest.raises(UserActionRequiredError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert sensitive_access_token not in str(raised.value)
    assert raised.value.error_code == "google_reauthorization_required"
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

    with pytest.raises(TransientProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert sensitive_access_token not in str(raised.value)
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_google_oauth_http_and_payload_failures_are_classified_without_provider_text() -> None:
    """token 端点的 503 与 malformed JSON 必须变成稳定脱敏领域错误。"""
    sensitive_token = "synthetic-sensitive-access-token"
    token_route = respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(503, text=f"provider body {sensitive_token}")
    )
    with pytest.raises(TransientProviderError) as transient:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert token_route.called
    assert transient.value.error_code == "google_oauth_unavailable"
    assert sensitive_token not in str(transient.value)
    assert transient.value.__context__ is None

    respx.reset()
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "only-access"})
    )
    with pytest.raises(PermanentProviderError) as malformed:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert malformed.value.error_code == "google_oauth_invalid_response"
    assert malformed.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_http_status_error_from_transport_is_converted_without_request_details() -> None:
    """httpx 直接抛出的 HTTPStatusError 也必须在 adapter 边界脱敏分类。"""
    sensitive_token = "synthetic-sensitive-token"

    def raise_status(request: httpx.Request) -> httpx.Response:
        """模拟底层 transport 抛出携带请求/供应商正文的 HTTPStatusError。"""
        response = httpx.Response(
            503,
            request=request,
            text=f"provider response {sensitive_token}",
            headers={"Retry-After": "7"},
        )
        raise httpx.HTTPStatusError(
            f"provider status {sensitive_token} at {request.url}",
            request=request,
            response=response,
        )

    respx.post("https://oauth2.googleapis.com/token").mock(side_effect=raise_status)

    with pytest.raises(TransientProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    error = raised.value
    assert error.error_code == "google_oauth_unavailable"
    assert error.retry_after == 7
    assert sensitive_token not in str(error)
    assert "oauth2.googleapis.com/token" not in str(error)
    assert error.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_malformed_retry_after_does_not_escape_domain_error_boundary() -> None:
    """异常大的 Retry-After 不能让错误构造溢出为裸 ValueError。"""
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            503,
            headers={"Retry-After": "1" + ("0" * 1000)},
            text="synthetic provider body",
        )
    )

    with pytest.raises(TransientProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")

    assert raised.value.error_code == "google_oauth_unavailable"
    assert raised.value.retry_after is None
    assert raised.value.__context__ is None


@pytest.mark.asyncio
@respx.mock
async def test_google_oauth_unauthorized_and_nonce_mismatch_require_user_action() -> None:
    """401 与 OIDC nonce 不匹配都必须要求用户重新授权且不回显 token。"""
    sensitive_token = "synthetic-sensitive-access-token"
    respx.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(401, text=f"provider body {sensitive_token}")
    )
    with pytest.raises(UserActionRequiredError) as unauthorized:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert unauthorized.value.error_code == "google_reauthorization_required"
    assert sensitive_token not in str(unauthorized.value)

    nonce = "synthetic-nonce"
    respx.reset()
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(200, json={"nonce": "different-nonce"})
    )
    respx.get("https://openidconnect.googleapis.com/v1/userinfo").mock(
        return_value=httpx.Response(
            200, json={"sub": "synthetic-subject", "email": "owner@example.test"}
        )
    )
    token = OAuthTokenSet(
        access_token=sensitive_token,
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({"openid", "email"}),
        id_token="synthetic-id-token",
    )
    with pytest.raises(UserActionRequiredError) as nonce_error:
        await _adapter().fetch_account(
            token,
            expected_nonce_hash=sha256(nonce.encode("utf-8")).digest(),
        )
    assert nonce_error.value.error_code == "google_oidc_nonce_mismatch"
    assert sensitive_token not in str(nonce_error.value)
    assert nonce_error.value.__context__ is None


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
async def test_malformed_expected_nonce_hash_requires_user_action() -> None:
    """本地 nonce 摘要损坏时也必须 fail closed，而不能让 compare_digest 抛裸 TypeError。"""
    respx.get(GOOGLE_TOKEN_INFO_URL).mock(
        return_value=httpx.Response(200, json={"nonce": "synthetic-nonce"})
    )
    token = OAuthTokenSet(
        access_token="synthetic-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({"openid", "email"}),
        id_token="synthetic-id-token",
    )

    with pytest.raises(UserActionRequiredError) as raised:
        await _adapter().fetch_account(
            token,
            expected_nonce_hash="synthetic-malformed-hash",  # type: ignore[arg-type]
        )

    assert raised.value.error_code == "google_oidc_nonce_mismatch"
    assert raised.value.__context__ is None


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

    with pytest.raises(PermanentProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert raised.value.error_code == "google_oauth_invalid_response"
    assert raised.value.__context__ is None
    assert not info.called
