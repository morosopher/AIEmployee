"""验证 Google OAuth 连接流程的安全参数和一次性 state 语义。"""

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import select

from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.integrations.google.oauth import GOOGLE_SCOPES, build_authorization_url
from ai_employee.main import create_app


@dataclass(slots=True)
class FixedClock:
    """向 OAuth state 测试注入不读取宿主机的显式 UTC 时间。"""

    current: datetime

    def now(self) -> datetime:
        """返回当前合成 UTC 时刻。"""
        return self.current


@pytest.fixture
async def oauth_context(
    database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, FixedClock]:
    """创建含临时 secret 文件、真实数据库和可控时钟的 OAuth API 上下文。"""
    master_key = tmp_path / "master_key"
    client_secret = tmp_path / "google_secret"
    master_key.write_text("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=", encoding="utf-8")
    client_secret.write_text("synthetic-secret", encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_COOKIE_NAME", "oauth_test_session")
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(master_key))
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET_FILE", str(client_secret))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "https://example.test/callback")
    get_settings.cache_clear()
    app = create_app()
    clock = FixedClock(datetime(2030, 1, 1, tzinfo=UTC))
    app.state.auth_clock = clock
    queries = build_session_factory(database_url)
    async with queries.begin() as session:
        session.add(
            UserModel(
                email="owner@example.com",
                display_name="Owner",
                password_hash=PasswordHasher().hash("synthetic-password"),
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
        )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield client, queries, clock
    await queries.dispose()
    await app.state.auth_session_factory.dispose()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_oauth_callback_encrypts_tokens_and_rejects_reused_state(
    oauth_context: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, FixedClock],
) -> None:
    """认证+CSRF 启动后，mock callback 只能加密保存 token 且 state 不可重用。"""
    client, queries, _ = oauth_context
    login = await client.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": "synthetic-password"}
    )
    assert login.status_code == 200
    csrf = client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    started = await client.post("/api/v1/connections/google/start", headers={"X-CSRF-Token": csrf})
    assert started.status_code == 200
    start_query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    state = start_query["state"][0]
    assert start_query["code_challenge_method"] == ["S256"]
    assert len(start_query["code_challenge"][0]) == 43
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post("https://oauth2.googleapis.com/token").respond(
            200,
            json={
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
            },
        )
        mocked.get("https://openidconnect.googleapis.com/v1/userinfo").respond(
            200, json={"sub": "synthetic-subject", "email": "owner@google.example"}
        )
        callback = await client.get(
            "/api/v1/connections/google/callback", params={"code": "synthetic-code", "state": state}
        )
    assert callback.status_code == 200
    assert (
        await client.get(
            "/api/v1/connections/google/callback", params={"code": "synthetic-code", "state": state}
        )
    ).status_code == 400
    async with queries() as session:
        connection = await session.scalar(select(OAuthConnectionModel))
        credentials = tuple((await session.scalars(select(EncryptedCredentialModel))).all())
    assert connection is not None
    assert connection.scopes == GOOGLE_SCOPES
    assert len(credentials) == 2
    assert all(
        b"synthetic-access" not in item.ciphertext and b"synthetic-refresh" not in item.ciphertext
        for item in credentials
    )
    assert not any(
        column.name in {"access_token", "refresh_token"}
        for column in EncryptedCredentialModel.__table__.columns
    )


@pytest.mark.asyncio
async def test_oauth_callback_rejects_state_after_ten_minute_expiry(
    oauth_context: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, FixedClock],
) -> None:
    """回调超过十分钟必须拒绝，且过期判断只依赖可控 UTC 时钟。"""
    client, _, clock = oauth_context
    login = await client.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": "synthetic-password"}
    )
    assert login.status_code == 200
    csrf = client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    started = await client.post("/api/v1/connections/google/start", headers={"X-CSRF-Token": csrf})
    state = parse_qs(urlparse(started.json()["authorization_url"]).query)["state"][0]

    clock.current += timedelta(minutes=10, seconds=1)

    expired = await client.get(
        "/api/v1/connections/google/callback", params={"code": "synthetic-code", "state": state}
    )
    assert expired.status_code == 400
    assert expired.json()["error_code"] == "oauth_state_rejected"


def test_google_authorization_url_uses_minimal_readonly_scopes_and_pkce() -> None:
    """授权 URL 必须固定为只读 scope，并显式请求离线刷新令牌。

    网络回调由更高层集成测试通过 httpx mock 覆盖；本测试不访问真实 Google，
    仅验证纯 URL 构造行为不会扩大权限或省略 PKCE。
    """
    url = build_authorization_url(
        client_id="synthetic-client",
        redirect_uri="https://example.test/callback",
        state="s" * 64,
        code_challenge="c" * 43,
    )

    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [" ".join(GOOGLE_SCOPES)]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["c" * 43]
