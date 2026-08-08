"""Microsoft OAuth 连接 API 的合成 PostgreSQL/HTTP 集成测试。"""

import base64
import json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.integrations.microsoft.oauth import (
    MICROSOFT_DISCOVERY_URL,
    MICROSOFT_GRAPH_ME_URL,
    MICROSOFT_JWKS_URL,
    MICROSOFT_TOKEN_URL,
)
from ai_employee.main import create_app


@dataclass(slots=True)
class MicrosoftOAuthContext:
    """封装 Microsoft OAuth 集成测试所需的客户端和数据库句柄。"""

    client: httpx.AsyncClient
    queries: ManagedAsyncSessionMaker


def _signing_material() -> tuple[rsa.RSAPrivateKey, dict[str, str]]:
    """生成本测试进程内的 RSA key/JWK，不把私钥写入 fixture 或日志。"""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private_key.public_key().public_numbers()

    def encode(number: int) -> str:
        raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return private_key, {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": "integration-runtime-key",
        "n": encode(numbers.n),
        "e": encode(numbers.e),
    }


@pytest.fixture
async def microsoft_oauth_context(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> MicrosoftOAuthContext:
    """创建只使用合成 Secret 与测试 PostgreSQL 的 API 上下文。"""
    master_key = tmp_path / "master_key"
    client_secret = tmp_path / "microsoft_secret"
    google_secret = tmp_path / "google_secret"
    master_key.write_text("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=", encoding="utf-8")
    client_secret.write_text("synthetic-microsoft-secret", encoding="utf-8")
    google_secret.write_text("synthetic-google-secret", encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_COOKIE_NAME", "microsoft_oauth_test_session")
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(master_key))
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET_FILE", str(client_secret))
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "synthetic-microsoft-client")
    monkeypatch.setenv(
        "MICROSOFT_REDIRECT_URI",
        "https://app.example.test/api/v1/connections/microsoft/callback",
    )
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "synthetic-google-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET_FILE", str(google_secret))
    monkeypatch.setenv(
        "GOOGLE_REDIRECT_URI", "https://app.example.test/api/v1/connections/google/callback"
    )
    get_settings.cache_clear()
    app = create_app()
    queries = build_session_factory(database_url)
    async with queries.begin() as session:
        session.add(
            UserModel(
                email="microsoft-owner@example.test",
                display_name="Microsoft Owner",
                password_hash=PasswordHasher().hash("synthetic-password"),
                timezone="UTC",
                locale="en-US",
                brief_time=time(8, 0),
                is_active=True,
            )
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield MicrosoftOAuthContext(client, queries)
    await queries.dispose()
    await app.state.auth_session_factory.dispose()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_microsoft_start_and_callback_use_common_endpoint_and_state_once(
    microsoft_oauth_context: MicrosoftOAuthContext,
) -> None:
    """Microsoft 首次连接请求精确读取 scope，callback 成功后 state 不能重放。"""
    context = microsoft_oauth_context
    login = await context.client.post(
        "/api/v1/auth/login",
        json={"email": "microsoft-owner@example.test", "password": "synthetic-password"},
    )
    assert login.status_code == 200
    csrf = context.client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    started = await context.client.post(
        "/api/v1/connections/microsoft/start",
        headers={"X-CSRF-Token": csrf},
    )
    assert started.status_code == 200
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    assert query["scope"][0].split() == [
        "openid",
        "profile",
        "email",
        "offline_access",
        "Mail.Read",
        "Calendars.Read",
    ]

    # 本集成测试先使用 adapter 的合成 token/Graph 路径；JWT 详情在 contract 测试覆盖。
    with respx.mock(assert_all_called=False) as mocked:
        mocked.post(MICROSOFT_TOKEN_URL).respond(
            503,
            text="synthetic provider failure",
        )
        failed = await context.client.get(
            "/api/v1/connections/microsoft/callback",
            params={"code": "synthetic-code", "state": query["state"][0]},
        )
    assert failed.status_code == 503
    assert failed.json()["error_code"] == "microsoft_oauth_unavailable"
    replay = await context.client.get(
        "/api/v1/connections/microsoft/callback",
        params={"code": "synthetic-code", "state": query["state"][0]},
    )
    assert replay.status_code == 400
    assert replay.json()["error_code"] == "oauth_state_rejected"
    async with context.queries() as session:
        connections = tuple((await session.scalars(select(OAuthConnectionModel))).all())
    assert connections == ()


@pytest.mark.asyncio
async def test_microsoft_success_callback_persists_tenant_graph_identity(
    microsoft_oauth_context: MicrosoftOAuthContext,
) -> None:
    """成功 callback 必须验签后写入 tenant:Graph ID 规范键与加密凭据。"""
    context = microsoft_oauth_context
    login = await context.client.post(
        "/api/v1/auth/login",
        json={"email": "microsoft-owner@example.test", "password": "synthetic-password"},
    )
    assert login.status_code == 200
    csrf = context.client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    started = await context.client.post(
        "/api/v1/connections/microsoft/start",
        headers={"X-CSRF-Token": csrf},
    )
    assert started.status_code == 200
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    tenant = "tenant-integration"
    private_key, jwk = _signing_material()
    id_token = jwt.encode(
        {
            "iss": f"https://login.microsoftonline.com/{tenant}/v2.0",
            "aud": "synthetic-microsoft-client",
            "tid": tenant,
            "nonce": query["nonce"][0],
            "exp": 1893456000,
            "iat": 1780000000,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "integration-runtime-key"},
    )
    discovery = json.loads(
        (
            Path(__file__).parents[2]
            / "contract"
            / "microsoft"
            / "fixtures"
            / "openid_configuration.json"
        ).read_text(encoding="utf-8")
    )
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post(MICROSOFT_TOKEN_URL).respond(
            200,
            json={
                "access_token": "synthetic-microsoft-access",
                "refresh_token": "synthetic-microsoft-refresh",
                "expires_in": 3600,
                "scope": "openid profile email offline_access Mail.Read Calendars.Read",
                "id_token": id_token,
            },
        )
        mocked.get(MICROSOFT_DISCOVERY_URL).respond(200, json=discovery)
        mocked.get(MICROSOFT_JWKS_URL).respond(200, json={"keys": [jwk]})
        mocked.get(MICROSOFT_GRAPH_ME_URL).respond(
            200,
            json={
                "id": "graph-integration-user",
                "mail": "microsoft-owner@example.test",
                "userPrincipalName": "microsoft-owner@example.test",
            },
        )
        callback = await context.client.get(
            "/api/v1/connections/microsoft/callback",
            params={"code": "synthetic-microsoft-code", "state": query["state"][0]},
        )
    assert callback.status_code == 200
    async with context.queries() as session:
        connection = await session.scalar(
            select(OAuthConnectionModel).where(OAuthConnectionModel.provider == "microsoft")
        )
    assert connection is not None
    assert connection.provider_tenant_id == tenant
    assert connection.provider_account_id == f"{tenant}:graph-integration-user"
    assert connection.account_type == "work_school"
    assert set(connection.scopes) == {
        "openid",
        "profile",
        "email",
        "offline_access",
        "Mail.Read",
        "Calendars.Read",
    }


@pytest.mark.asyncio
async def test_microsoft_admin_consent_callback_never_echoes_raw_description(
    microsoft_oauth_context: MicrosoftOAuthContext,
) -> None:
    """管理员同意错误只返回稳定错误码，不回显 Microsoft description。"""
    context = microsoft_oauth_context
    login = await context.client.post(
        "/api/v1/auth/login",
        json={"email": "microsoft-owner@example.test", "password": "synthetic-password"},
    )
    assert login.status_code == 200
    csrf = context.client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    started = await context.client.post(
        "/api/v1/connections/microsoft/start",
        headers={"X-CSRF-Token": csrf},
    )
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]
    raw_description = "AADSTS65001: synthetic administrator-only detail"
    response = await context.client.get(
        "/api/v1/connections/microsoft/callback",
        params={
            "error": "access_denied",
            "error_description": raw_description,
            "error_codes": "65001",
            "state": state,
        },
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "microsoft_admin_consent_required"
    assert raw_description not in response.text
    replay = await context.client.get(
        "/api/v1/connections/microsoft/callback",
        params={
            "error": "access_denied",
            "error_codes": "65001",
            "state": state,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error_code"] == "oauth_state_rejected"


@pytest.mark.asyncio
async def test_microsoft_disconnect_does_not_call_broad_revoke_endpoints(
    microsoft_oauth_context: MicrosoftOAuthContext,
) -> None:
    """断开只清理本地事实，绝不访问广泛 sign-in/session 或 directory grant API。"""
    context = microsoft_oauth_context
    async with context.queries.begin() as session:
        user = await session.scalar(
            select(UserModel).where(UserModel.email == "microsoft-owner@example.test")
        )
        assert user is not None
        connection = OAuthConnectionModel(
            user_id=user.id,
            provider="microsoft",
            provider_account_id="tenant-synthetic:graph-user",
            provider_tenant_id="tenant-synthetic",
            account_type="work_school",
            account_email="microsoft-owner@example.test",
            scopes=["openid", "profile", "email", "offline_access", "Mail.Read"],
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()

    login = await context.client.post(
        "/api/v1/auth/login",
        json={"email": "microsoft-owner@example.test", "password": "synthetic-password"},
    )
    assert login.status_code == 200
    csrf = context.client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    with respx.mock(assert_all_called=False) as mocked:
        response = await context.client.delete(
            f"/api/v1/connections/{connection.id}",
            headers={"X-CSRF-Token": csrf},
        )
    assert response.status_code == 204
    assert not any(
        str(call.request.url).endswith(path)
        for call in mocked.calls
        for path in ("/me/revokeSignInSessions", "/oauth2PermissionGrants")
    )
