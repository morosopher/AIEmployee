"""验证 Google OAuth 连接流程的安全参数和一次性 state 语义。"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from sqlalchemy import select

from ai_employee.application.ports.oauth import OAuthAuthorizationRequest
from ai_employee.config import get_settings
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
    SyncCursorModel,
)
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStore
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.integrations.google.oauth import (
    GOOGLE_SCOPES,
    GOOGLE_TOKEN_INFO_URL,
    GoogleOAuthAdapter,
)
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
    assert start_query["include_granted_scopes"] == ["true"]
    assert start_query["access_type"] == ["offline"]
    assert "nonce" in start_query
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post("https://oauth2.googleapis.com/token").respond(
            200,
            json={
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
                "scope": " ".join(GOOGLE_SCOPES),
                "id_token": "synthetic-id-token",
            },
        )
        mocked.get(GOOGLE_TOKEN_INFO_URL).respond(
            200,
            json={"nonce": start_query["nonce"][0]},
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
async def test_callback_marks_missing_requested_read_scope_action_required(
    oauth_context: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, FixedClock],
) -> None:
    """Google 实际只授予邮件读取时，日历读取必须进入 action_required。"""
    client, queries, _ = oauth_context
    login = await client.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": "synthetic-password"}
    )
    assert login.status_code == 200
    csrf = client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    started = await client.post("/api/v1/connections/google/start", headers={"X-CSRF-Token": csrf})
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    granted_scope = "openid email https://www.googleapis.com/auth/gmail.readonly"
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post("https://oauth2.googleapis.com/token").respond(
            200,
            json={
                "access_token": "partial-access",
                "refresh_token": "partial-refresh",
                "expires_in": 3600,
                "scope": granted_scope,
                "id_token": "partial-id-token",
            },
        )
        mocked.get(GOOGLE_TOKEN_INFO_URL).respond(200, json={"nonce": query["nonce"][0]})
        mocked.get("https://openidconnect.googleapis.com/v1/userinfo").respond(
            200, json={"sub": "partial-subject", "email": "partial@google.example"}
        )
        callback = await client.get(
            "/api/v1/connections/google/callback",
            params={"code": "partial-code", "state": query["state"][0]},
        )
    assert callback.status_code == 200

    async with queries() as session:
        rows = tuple(
            (
                await session.scalars(
                    select(ConnectionCapabilityModel).order_by(ConnectionCapabilityModel.capability)
                )
            ).all()
        )
    states = {row.capability: (row.status, set(row.actual_scopes)) for row in rows}
    assert states[ConnectionCapability.MAIL_READ.value][0] == CapabilityStatus.ENABLED.value
    assert (
        states[ConnectionCapability.CALENDAR_READ.value][0]
        == CapabilityStatus.ACTION_REQUIRED.value
    )
    assert states[ConnectionCapability.CALENDAR_READ.value][1] == set(granted_scope.split())
    assert states[ConnectionCapability.MAIL_SEND.value][0] == CapabilityStatus.DISABLED.value
    assert states[ConnectionCapability.CALENDAR_WRITE.value][0] == CapabilityStatus.DISABLED.value


@pytest.mark.asyncio
async def test_readonly_reconnect_clears_stale_enabled_write_capabilities(
    oauth_context: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, FixedClock],
) -> None:
    """断开后重新只读授权时，旧写能力不得越过本次实际 scope 继续 enabled。"""
    client, queries, _ = oauth_context
    previously_granted = frozenset(
        {
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/calendar.events",
        }
    )
    async with queries.begin() as session:
        user = await session.scalar(select(UserModel).where(UserModel.email == "owner@example.com"))
        assert user is not None
        store = SqlAlchemyConnectionStore(session)
        connection_id = await store.ensure_connection(
            user_id=user.id,
            provider="google",
            provider_account_id="reconnect-subject",
            provider_tenant_id="",
            account_type="google",
            account_email="reconnect@google.example",
            scopes=previously_granted,
        )
        for capability in ConnectionCapability:
            await store.save_capability_state(
                user_id=user.id,
                connection_id=connection_id,
                capability=capability,
                status=CapabilityStatus.ENABLED,
                actual_scopes=previously_granted,
                last_verified_at=datetime(2029, 12, 31, tzinfo=UTC),
                last_error_code=None,
            )

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "synthetic-password"},
    )
    assert login.status_code == 200
    csrf = client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    disconnected = await client.delete(
        f"/api/v1/connections/{connection_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert disconnected.status_code == 204

    started = await client.post(
        "/api/v1/connections/google/start",
        headers={"X-CSRF-Token": csrf},
    )
    assert started.status_code == 200
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    granted_scope = "openid email https://www.googleapis.com/auth/gmail.readonly"
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post("https://oauth2.googleapis.com/token").respond(
            200,
            json={
                "access_token": "reconnect-access",
                "refresh_token": "reconnect-refresh",
                "expires_in": 3600,
                "scope": granted_scope,
                "id_token": "reconnect-id-token",
            },
        )
        mocked.get(GOOGLE_TOKEN_INFO_URL).respond(200, json={"nonce": query["nonce"][0]})
        mocked.get("https://openidconnect.googleapis.com/v1/userinfo").respond(
            200,
            json={"sub": "reconnect-subject", "email": "reconnect@google.example"},
        )
        callback = await client.get(
            "/api/v1/connections/google/callback",
            params={"code": "reconnect-code", "state": query["state"][0]},
        )
    assert callback.status_code == 200

    async with queries() as session:
        connection = await session.get(OAuthConnectionModel, connection_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.connection_id == connection_id
                    )
                )
            ).all()
        )
    assert connection is not None
    assert set(connection.scopes) == set(granted_scope.split())
    states = {row.capability: row.status for row in rows}
    assert states[ConnectionCapability.MAIL_READ.value] == CapabilityStatus.ENABLED.value
    assert (
        states[ConnectionCapability.CALENDAR_READ.value] == CapabilityStatus.ACTION_REQUIRED.value
    )
    assert states[ConnectionCapability.MAIL_SEND.value] == CapabilityStatus.DISABLED.value
    assert states[ConnectionCapability.CALENDAR_WRITE.value] == CapabilityStatus.DISABLED.value


@pytest.mark.asyncio
async def test_progressive_scope_shrink_rechecks_all_previously_enabled_capabilities(
    oauth_context: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, FixedClock],
) -> None:
    """渐进回调实际 scope 缩减时，当前 enabled 并集及写依赖必须全部重新核验。"""
    client, queries, _ = oauth_context
    previously_granted = frozenset(
        {
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/calendar.readonly",
        }
    )
    enabled_capabilities = frozenset(
        {
            ConnectionCapability.MAIL_READ,
            ConnectionCapability.MAIL_SEND,
            ConnectionCapability.CALENDAR_READ,
        }
    )
    async with queries.begin() as session:
        user = await session.scalar(select(UserModel).where(UserModel.email == "owner@example.com"))
        assert user is not None
        store = SqlAlchemyConnectionStore(session)
        connection_id = await store.ensure_connection(
            user_id=user.id,
            provider="google",
            provider_account_id="progressive-shrink-subject",
            provider_tenant_id="",
            account_type="google",
            account_email="progressive-shrink@google.example",
            scopes=previously_granted,
        )
        for capability in enabled_capabilities:
            await store.save_capability_state(
                user_id=user.id,
                connection_id=connection_id,
                capability=capability,
                status=CapabilityStatus.ENABLED,
                actual_scopes=previously_granted,
                last_verified_at=datetime(2029, 12, 31, tzinfo=UTC),
                last_error_code=None,
            )

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "synthetic-password"},
    )
    assert login.status_code == 200
    csrf = client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    started = await client.post(
        f"/api/v1/connections/{connection_id}/capabilities/mail.send/enable",
        headers={"X-CSRF-Token": csrf},
    )
    assert started.status_code == 200
    query = parse_qs(urlparse(started.json()["authorization_url"]).query)
    assert set(query["scope"][0].split()) == previously_granted

    granted_scope = "openid email https://www.googleapis.com/auth/gmail.send"
    with respx.mock(assert_all_called=True) as mocked:
        mocked.post("https://oauth2.googleapis.com/token").respond(
            200,
            json={
                "access_token": "progressive-shrink-access",
                "refresh_token": "progressive-shrink-refresh",
                "expires_in": 3600,
                "scope": granted_scope,
                "id_token": "progressive-shrink-id-token",
            },
        )
        mocked.get(GOOGLE_TOKEN_INFO_URL).respond(200, json={"nonce": query["nonce"][0]})
        mocked.get("https://openidconnect.googleapis.com/v1/userinfo").respond(
            200,
            json={
                "sub": "progressive-shrink-subject",
                "email": "progressive-shrink@google.example",
            },
        )
        callback = await client.get(
            "/api/v1/connections/google/callback",
            params={"code": "progressive-shrink-code", "state": query["state"][0]},
        )
    assert callback.status_code == 200

    async with queries() as session:
        connection = await session.get(OAuthConnectionModel, connection_id)
        rows = tuple(
            (
                await session.scalars(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.connection_id == connection_id
                    )
                )
            ).all()
        )
    assert connection is not None
    assert set(connection.scopes) == set(granted_scope.split())
    by_capability = {row.capability: row for row in rows}
    for capability in enabled_capabilities:
        row = by_capability[capability.value]
        assert row.status == CapabilityStatus.ACTION_REQUIRED.value
        assert set(row.actual_scopes) == set(granted_scope.split())
        assert row.last_error_code == "connection_scope_missing"
    assert (
        by_capability[ConnectionCapability.CALENDAR_WRITE.value].status
        == CapabilityStatus.DISABLED.value
    )


@pytest.mark.asyncio
async def test_google_capability_backfill_is_idempotent_and_preserves_manual_state(
    database_url: str,
) -> None:
    """运行时回填只补缺失行，复制 scopes 且不覆盖用户后来设置的状态。"""
    sessions = build_session_factory(database_url)
    user_id = None
    connection_id = None
    scopes = [
        "openid",
        "email",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/calendar.events",
    ]
    try:
        async with sessions.begin() as session:
            user = UserModel(
                email="backfill-owner@example.test",
                display_name="Backfill Owner",
                password_hash=None,
                timezone="UTC",
                locale="en-US",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            user_id = user.id
            connection = OAuthConnectionModel(
                user_id=user.id,
                provider="google",
                provider_account_id="backfill-subject",
                provider_tenant_id="",
                account_type="google",
                account_email="backfill-owner@example.test",
                scopes=scopes,
                status="connected",
                last_error_code=None,
            )
            session.add(connection)
            await session.flush()
            connection_id = connection.id
            session.add(
                ConnectionCapabilityModel(
                    user_id=user.id,
                    connection_id=connection.id,
                    capability=ConnectionCapability.MAIL_READ.value,
                    status=CapabilityStatus.ACTION_REQUIRED.value,
                    actual_scopes=["manually-recorded"],
                    last_verified_at=None,
                    last_error_code="manual-review",
                )
            )

        async with sessions.begin() as session:
            store = SqlAlchemyConnectionStore(session)
            await store.ensure_google_capability_rows(user_id=user_id, connection_id=connection_id)
            await store.ensure_google_capability_rows(user_id=user_id, connection_id=connection_id)

        async with sessions() as session:
            rows = tuple(
                (
                    await session.scalars(
                        select(ConnectionCapabilityModel).where(
                            ConnectionCapabilityModel.connection_id == connection_id
                        )
                    )
                ).all()
            )
        assert len(rows) == 4
        by_capability = {row.capability: row for row in rows}
        assert by_capability[ConnectionCapability.MAIL_READ.value].status == (
            CapabilityStatus.ACTION_REQUIRED.value
        )
        assert by_capability[ConnectionCapability.MAIL_READ.value].actual_scopes == [
            "manually-recorded"
        ]
        assert by_capability[ConnectionCapability.CALENDAR_READ.value].status == (
            CapabilityStatus.ENABLED.value
        )
        assert by_capability[ConnectionCapability.MAIL_SEND.value].status == (
            CapabilityStatus.DISABLED.value
        )
        assert by_capability[ConnectionCapability.CALENDAR_WRITE.value].status == (
            CapabilityStatus.DISABLED.value
        )
        for row in rows:
            assert row.user_id == user_id
            if row.capability != ConnectionCapability.MAIL_READ.value:
                assert set(row.actual_scopes) == set(scopes)
    finally:
        await sessions.dispose()


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


@pytest.mark.asyncio
async def test_concurrent_callbacks_for_same_google_account_share_one_connection(
    oauth_context: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, FixedClock],
) -> None:
    """两个有效 state 并发回调同一帐号必须收敛为唯一连接、凭据和游标。"""
    client, queries, _ = oauth_context
    login = await client.post(
        "/api/v1/auth/login", json={"email": "owner@example.com", "password": "synthetic-password"}
    )
    assert login.status_code == 200
    csrf = client.cookies.get("ai_employee_csrf")
    assert csrf is not None
    starts = await asyncio.gather(
        *(
            client.post("/api/v1/connections/google/start", headers={"X-CSRF-Token": csrf})
            for _ in range(2)
        )
    )
    start_queries = [parse_qs(urlparse(item.json()["authorization_url"]).query) for item in starts]
    states = [item["state"][0] for item in start_queries]
    nonce_by_code = {
        "synthetic-code-0": start_queries[0]["nonce"][0],
        "synthetic-code-1": start_queries[1]["nonce"][0],
    }

    def token_response(request: httpx.Request) -> httpx.Response:
        """按授权码返回对应 synthetic id_token，避免并发 callback 串用 nonce。"""
        code = parse_qs(request.content.decode("utf-8"))["code"][0]
        return httpx.Response(
            200,
            json={
                "access_token": f"synthetic-access-{code[-1]}",
                "refresh_token": "synthetic-refresh",
                "expires_in": 3600,
                "scope": " ".join(GOOGLE_SCOPES),
                "id_token": f"synthetic-id-token-{code[-1]}",
            },
        )

    def token_info_response(request: httpx.Request) -> httpx.Response:
        """按 id_token 返回对应 OIDC nonce claim。"""
        id_token = request.url.params["id_token"]
        nonce = nonce_by_code[f"synthetic-code-{id_token[-1]}"]
        return httpx.Response(200, json={"nonce": nonce})

    with respx.mock(assert_all_called=True) as mocked:
        mocked.post("https://oauth2.googleapis.com/token").mock(side_effect=token_response)
        mocked.get(GOOGLE_TOKEN_INFO_URL).mock(side_effect=token_info_response)
        mocked.get("https://openidconnect.googleapis.com/v1/userinfo").respond(
            200, json={"sub": "same-subject", "email": "same@google.example"}
        )
        callbacks = await asyncio.gather(
            *(
                client.get(
                    "/api/v1/connections/google/callback",
                    params={"code": f"synthetic-code-{index}", "state": state},
                )
                for index, state in enumerate(states)
            )
        )
    assert [item.status_code for item in callbacks] == [200, 200]
    async with queries() as session:
        connections = tuple((await session.scalars(select(OAuthConnectionModel))).all())
        credentials = tuple((await session.scalars(select(EncryptedCredentialModel))).all())
        cursors = tuple((await session.scalars(select(SyncCursorModel))).all())
    assert len(connections) == 1
    assert len(credentials) == 2
    assert len(cursors) == 2


def test_google_authorization_url_uses_minimal_readonly_scopes_and_pkce() -> None:
    """授权 URL 必须使用请求中的最小只读 scope，并显式请求离线刷新令牌。

    网络回调由更高层集成测试通过 httpx mock 覆盖；本测试不访问真实 Google，
    仅验证纯 URL 构造行为不会扩大权限或省略 PKCE。
    """
    request_scopes = frozenset(
        {
            "openid",
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
        }
    )
    url = GoogleOAuthAdapter(
        "synthetic-client", "synthetic-secret", "https://example.test/callback"
    ).build_authorization_url(
        OAuthAuthorizationRequest(
            state="s" * 64,
            code_challenge="c" * 43,
            requested_scopes=request_scopes,
            oidc_nonce="synthetic-nonce",
        )
    )

    query = parse_qs(urlparse(url).query)
    assert set(query["scope"][0].split()) == request_scopes
    assert "https://www.googleapis.com/auth/calendar.readonly" not in query["scope"][0]
    assert query["include_granted_scopes"] == ["true"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["c" * 43]
    assert query["nonce"] == ["synthetic-nonce"]
