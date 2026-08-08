"""Microsoft delegated OIDC/OAuth 适配器的脱敏供应商契约测试。

所有 HTTP 响应都来自 ``.example.test`` 或被 ``respx`` 拦截的 Microsoft 固定端点；
测试不会连接真实 Microsoft 账户。RSA 测试密钥在进程内临时生成，不持久化私钥。
"""

import base64
import json
from hashlib import sha256
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_employee.application.ports.oauth import (
    OAuthAuthorizationRequest,
    OAuthRevocationStatus,
)
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import (
    PermanentProviderError,
    TransientProviderError,
    UserActionRequiredError,
)
from ai_employee.integrations.microsoft.oauth import (
    MICROSOFT_ALLOWED_SCOPES,
    MICROSOFT_AUTHORIZATION_URL,
    MICROSOFT_BASE_SCOPES,
    MICROSOFT_DISCOVERY_URL,
    MICROSOFT_GRAPH_ME_URL,
    MICROSOFT_JWKS_URL,
    MICROSOFT_TOKEN_URL,
    MicrosoftOAuthAdapter,
    classify_microsoft_callback_error,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _adapter() -> MicrosoftOAuthAdapter:
    """构造只使用合成客户端配置的 Microsoft adapter。"""
    return MicrosoftOAuthAdapter(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        redirect_uri="https://app.example.test/api/v1/connections/microsoft/callback",
    )


def _jwk_and_private_key() -> tuple[dict[str, str], rsa.RSAPrivateKey]:
    """生成短生命周期合成 RSA JWK，避免仓库保存任何私钥。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()

    def encode(number: int) -> str:
        """把 RSA 整数编码为无 padding Base64URL。"""
        raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return (
        {
            "kty": "RSA",
            "use": "sig",
            "kid": "synthetic-runtime-key",
            "alg": "RS256",
            "n": encode(public_numbers.n),
            "e": encode(public_numbers.e),
        },
        private_key,
    )


def _openid_configuration() -> dict[str, object]:
    """读取脱敏 discovery fixture，测试只允许固定 Microsoft 端点。"""
    return json.loads((FIXTURE_DIR / "openid_configuration.json").read_text(encoding="utf-8"))


def _install_oidc_routes(mocked: respx.MockRouter, private_key: rsa.RSAPrivateKey) -> None:
    """为一次账户读取安装 discovery、JWKS 和 Graph 合成响应。"""
    jwk, _ = _jwk_and_private_key()
    # 用当前运行时私钥对应的 JWK 覆盖静态占位键；fixture 本身仍保留脱敏结构。
    public_numbers = private_key.public_key().public_numbers()

    def encode(number: int) -> str:
        raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    jwk.update(
        {
            "kid": "synthetic-runtime-key",
            "n": encode(public_numbers.n),
            "e": encode(public_numbers.e),
        }
    )
    mocked.get(MICROSOFT_DISCOVERY_URL).respond(200, json=_openid_configuration())
    mocked.get(MICROSOFT_JWKS_URL).respond(200, json={"keys": [jwk]})
    mocked.get(MICROSOFT_GRAPH_ME_URL).respond(
        200,
        json={
            "id": "graph-user-123",
            "mail": "owner@example.test",
            "userPrincipalName": "owner@example.test",
        },
    )


def _id_token(
    private_key: rsa.RSAPrivateKey, *, tenant: str, nonce: str, issuer: str | None = None
) -> str:
    """签发仅用于测试的短期 OIDC ID token。"""
    return jwt.encode(
        {
            "iss": issuer or f"https://login.microsoftonline.com/{tenant}/v2.0",
            "aud": "synthetic-client-id",
            "tid": tenant,
            "sub": "oidc-subject",
            "nonce": nonce,
            "exp": 1893456000,
            "iat": 1780000000,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "synthetic-runtime-key"},
    )


def test_microsoft_scope_union_is_minimal_and_deterministic() -> None:
    """能力映射必须只请求 M2 委托 scope，禁止 Mail.ReadWrite/Contacts。"""
    adapter = _adapter()
    requested = adapter.scopes_for(
        frozenset({ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND})
    )
    assert requested == frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read", "Mail.Send"})
    assert requested.issubset(MICROSOFT_ALLOWED_SCOPES)
    assert "Mail.ReadWrite" not in requested
    assert "Contacts.Read" not in requested
    assert type(requested) is frozenset


def test_microsoft_authorization_url_uses_common_v2_pkce_nonce_and_offline_scopes() -> None:
    """授权 URL 必须使用 common v2、S256 PKCE、OIDC nonce 与离线基础 scope。"""
    request = OAuthAuthorizationRequest(
        state="synthetic-state",
        code_challenge="synthetic-challenge",
        requested_scopes=frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read", "Mail.Send"}),
        oidc_nonce="synthetic-nonce",
    )
    parsed = urlparse(_adapter().build_authorization_url(request))
    query = parse_qs(parsed.query)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == MICROSOFT_AUTHORIZATION_URL
    assert query["scope"][0].split() == [
        "openid",
        "profile",
        "email",
        "offline_access",
        "Mail.Read",
        "Mail.Send",
    ]
    assert query["response_type"] == ["code"]
    assert query["response_mode"] == ["query"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["state"] == [request.state]
    assert query["nonce"] == [request.oidc_nonce]


@pytest.mark.asyncio
@respx.mock
async def test_microsoft_token_exchange_normalizes_scope_and_id_token() -> None:
    """token endpoint 返回值应收窄为 OAuthTokenSet，保留实际 scope 与 id_token。"""
    route = respx.post(MICROSOFT_TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
                "scope": "openid profile email offline_access Mail.Read Mail.Send",
                "id_token": "synthetic-id-token",
                "token_type": "Bearer",
            },
        )
    )
    token = await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert route.called
    assert token.granted_scopes == frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read", "Mail.Send"})
    assert token.refresh_token == "synthetic-refresh"
    assert token.id_token == "synthetic-id-token"
    request = route.calls[0].request
    assert b"client_secret=synthetic-client-secret" in request.content
    assert b"code_verifier=synthetic-verifier" in request.content


@pytest.mark.asyncio
@respx.mock
async def test_microsoft_refresh_without_rotation_returns_none_refresh_token() -> None:
    """刷新响应未轮换 refresh token 时必须返回 None，由仓储保留旧密文。"""
    respx.post(MICROSOFT_TOKEN_URL).respond(
        200,
        json={
            "access_token": "synthetic-refreshed-access",
            "expires_in": 3600,
            "scope": "openid profile email offline_access Mail.Read",
        },
    )
    token = await _adapter().refresh("synthetic-existing-refresh")
    assert token.refresh_token is None
    assert token.granted_scopes == frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read"})


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503])
@respx.mock
async def test_microsoft_oauth_429_and_5xx_are_transient(status_code: int) -> None:
    """429/5xx 只返回稳定临时错误，不携带供应商正文。"""
    respx.post(MICROSOFT_TOKEN_URL).respond(
        status_code,
        headers={"Retry-After": "7"},
        text="synthetic-secret-provider-body",
    )
    with pytest.raises(TransientProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert raised.value.error_code == "microsoft_oauth_unavailable"
    assert raised.value.retry_after == 7
    assert "synthetic-secret-provider-body" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_microsoft_token_admin_consent_error_is_stable() -> None:
    """token endpoint 的 AADSTS65001 也必须映射管理员同意稳定错误。"""
    respx.post(MICROSOFT_TOKEN_URL).respond(
        400,
        json={
            "error": "invalid_grant",
            "error_codes": [65001],
            "error_description": "AADSTS65001: synthetic raw detail",
        },
    )
    with pytest.raises(UserActionRequiredError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert raised.value.error_code == "microsoft_admin_consent_required"
    assert "synthetic raw detail" not in str(raised.value)


@pytest.mark.asyncio
@respx.mock
async def test_microsoft_oidc_signature_nonce_issuer_and_graph_identity_are_verified() -> None:
    """只有签名、aud/exp/issuer/nonce 均通过后才读取 Graph 稳定身份。"""
    jwk, private_key = _jwk_and_private_key()
    del jwk
    tenant = "tenant-synthetic"
    nonce = "synthetic-nonce"
    id_token = _id_token(private_key, tenant=tenant, nonce=nonce)
    from ai_employee.application.ports.oauth import OAuthTokenSet

    oauth_token = OAuthTokenSet(
        access_token="synthetic-graph-access",
        refresh_token="synthetic-refresh",
        expires_in=3600,
        granted_scopes=frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read"}),
        id_token=id_token,
    )
    with respx.mock(assert_all_called=True) as mocked:
        _install_oidc_routes(mocked, private_key)
        account = await _adapter().fetch_account(
            oauth_token,
            expected_nonce_hash=sha256(nonce.encode()).digest(),
        )
    assert account.provider_tenant_id == tenant
    assert account.account_type == "work_school"
    assert account.provider_account_id == f"{tenant}:graph-user-123"
    assert account.account_email == "owner@example.test"


@pytest.mark.asyncio
@respx.mock
async def test_microsoft_oidc_nonce_mismatch_does_not_call_graph() -> None:
    """nonce 摘要不匹配时必须在 Graph 前失败。"""
    _, private_key = _jwk_and_private_key()
    tenant = "tenant-synthetic"
    id_token = _id_token(private_key, tenant=tenant, nonce="observed-nonce")
    from ai_employee.application.ports.oauth import OAuthTokenSet

    token = OAuthTokenSet(
        access_token="synthetic-graph-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read"}),
        id_token=id_token,
    )
    with respx.mock(assert_all_called=False) as mocked:
        _install_oidc_routes(mocked, private_key)
        with pytest.raises(UserActionRequiredError) as raised:
            await _adapter().fetch_account(token, expected_nonce_hash=sha256(b"different").digest())
        assert not any(call.request.url.path == "/v1.0/me" for call in mocked.calls)
    assert raised.value.error_code == "microsoft_oidc_nonce_mismatch"


@pytest.mark.asyncio
async def test_microsoft_oidc_wrong_audience_and_signature_fail_before_graph() -> None:
    """audience 或签名不匹配时不能把 claims 当作身份事实。"""
    _, private_key = _jwk_and_private_key()
    from ai_employee.application.ports.oauth import OAuthTokenSet

    wrong_audience = jwt.encode(
        {
            "iss": "https://login.microsoftonline.com/tenant-synthetic/v2.0",
            "aud": "another-client",
            "tid": "tenant-synthetic",
            "nonce": "nonce",
            "exp": 1893456000,
            "iat": 1780000000,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "synthetic-runtime-key"},
    )
    token = OAuthTokenSet(
        access_token="synthetic-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read"}),
        id_token=wrong_audience,
    )
    with respx.mock(assert_all_called=False) as mocked:
        _install_oidc_routes(mocked, private_key)
        with pytest.raises(UserActionRequiredError) as raised:
            await _adapter().fetch_account(token, expected_nonce_hash=sha256(b"nonce").digest())
    assert raised.value.error_code == "microsoft_oidc_signature_invalid"
    assert not any(call.request.url.path == "/v1.0/me" for call in mocked.calls)


@pytest.mark.asyncio
@respx.mock
async def test_microsoft_personal_tenant_is_normalized() -> None:
    """消费者租户应归一为 personal，身份键仍包含 tenant 与 Graph ID。"""
    _, private_key = _jwk_and_private_key()
    tenant = "9188040d-6c67-4c5b-b112-36a304b66dad"
    from ai_employee.application.ports.oauth import OAuthTokenSet

    token = OAuthTokenSet(
        access_token="synthetic-personal-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read"}),
        id_token=_id_token(private_key, tenant=tenant, nonce="personal-nonce"),
    )
    with respx.mock(assert_all_called=True) as mocked:
        _install_oidc_routes(mocked, private_key)
        account = await _adapter().fetch_account(
            token,
            expected_nonce_hash=sha256(b"personal-nonce").digest(),
        )
    assert account.account_type == "personal"
    assert account.provider_account_id == f"{tenant}:graph-user-123"


@pytest.mark.asyncio
async def test_microsoft_login_live_issuer_form_is_personal() -> None:
    """login.live.com issuer 形式也必须归一为 personal，而不依赖邮箱可变字段。"""
    _, private_key = _jwk_and_private_key()
    from ai_employee.application.ports.oauth import OAuthTokenSet

    tenant = "personal-synthetic"
    token = OAuthTokenSet(
        access_token="synthetic-live-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read"}),
        id_token=_id_token(
            private_key,
            tenant=tenant,
            nonce="live-nonce",
            issuer=f"https://login.live.com/{tenant}/v2.0",
        ),
    )
    # 此测试只验证 issuer/account classification；OIDC discovery/JWKS 仍使用脱敏响应。
    with respx.mock(assert_all_called=True) as mocked:
        _install_oidc_routes(mocked, private_key)
        account = await _adapter().fetch_account(
            token,
            expected_nonce_hash=sha256(b"live-nonce").digest(),
        )
    assert account.account_type == "personal"
    assert account.provider_tenant_id == tenant


@pytest.mark.asyncio
async def test_microsoft_oidc_discovery_is_bounded_cached() -> None:
    """同一 adapter 的 discovery/JWKS 读取应在有限 TTL 内复用，避免无限缓存。"""
    _, private_key = _jwk_and_private_key()
    from ai_employee.application.ports.oauth import OAuthTokenSet

    token = OAuthTokenSet(
        access_token="synthetic-cache-access",
        refresh_token=None,
        expires_in=3600,
        granted_scopes=frozenset({*MICROSOFT_BASE_SCOPES, "Mail.Read"}),
        id_token=_id_token(private_key, tenant="tenant-cache", nonce="cache-nonce"),
    )
    adapter = _adapter()
    with respx.mock(assert_all_called=True) as mocked:
        _install_oidc_routes(mocked, private_key)
        await adapter.fetch_account(token, expected_nonce_hash=sha256(b"cache-nonce").digest())
        await adapter.fetch_account(token, expected_nonce_hash=sha256(b"cache-nonce").digest())
        assert len(mocked.calls) == 4  # discovery + JWKS once, Graph for each account read


def test_microsoft_admin_consent_error_is_stable_and_content_free() -> None:
    """AADSTS65001/consent_required 只映射稳定错误码，不保存 raw description。"""
    error = classify_microsoft_callback_error(
        error="access_denied",
        error_description="AADSTS65001: synthetic administrator consent details",
        error_codes="65001",
    )
    assert isinstance(error, UserActionRequiredError)
    assert error.error_code == "microsoft_admin_consent_required"
    assert "synthetic administrator consent details" not in str(error)
    assert (
        classify_microsoft_callback_error(
            error="temporarily_unavailable",
            error_description="synthetic transient",
            error_codes=None,
        )
        is None
    )


@pytest.mark.asyncio
@respx.mock
async def test_microsoft_revoke_is_explicitly_unsupported_and_never_calls_broad_endpoints() -> None:
    """Microsoft delegated OAuth 没有窄 RFC7009 revoke；adapter 不得触发 Graph 广泛端点。"""
    adapter = _adapter()
    result = await adapter.revoke("synthetic-refresh-token")
    assert result.status is OAuthRevocationStatus.UNSUPPORTED
    assert result.error_code == "microsoft_token_revoke_unsupported"
    assert not respx.calls


@pytest.mark.asyncio
@respx.mock
async def test_microsoft_malformed_token_payload_is_permanent_and_redacted() -> None:
    """malformed token JSON 必须分类为稳定永久错误且不携带响应内容。"""
    respx.post(MICROSOFT_TOKEN_URL).respond(
        200,
        json={"access_token": "synthetic-access", "expires_in": "not-an-int", "scope": "Mail.Read"},
    )
    with pytest.raises(PermanentProviderError) as raised:
        await _adapter().exchange_code(code="synthetic-code", verifier="synthetic-verifier")
    assert raised.value.error_code == "microsoft_oauth_invalid_response"
    assert "not-an-int" not in str(raised.value)
