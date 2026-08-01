"""在真实 PostgreSQL 上验证单管理员认证、CSRF 与会话隔离边界。"""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from ai_employee.api.deps import ProblemDetails
from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.identity import UserModel, UserSessionModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.infrastructure.security.tokens import hash_token
from ai_employee.main import create_app

SESSION_COOKIE_NAME = "test_session"
CSRF_COOKIE_NAME = "ai_employee_csrf"
ADMIN_EMAIL = "owner@example.com"
ADMIN_PASSWORD = "synthetic-admin-password"


@dataclass(slots=True)
class MutableClock:
    """为认证测试提供可显式推进且始终为 UTC 的确定性时钟。"""

    current: datetime

    def now(self) -> datetime:
        """返回当前测试时间，不读取宿主机时钟。"""
        return self.current

    def advance(self, delta: timedelta) -> None:
        """按指定时长推进测试时间。"""
        self.current += delta


@dataclass(slots=True)
class CountingTokenFactory:
    """生成可复现但不在 fixture 中硬编码的 32 字节 URL-safe 测试令牌。"""

    counter: int = 0

    def __call__(self) -> str:
        """为每次调用返回不同的合成令牌。"""
        self.counter += 1
        return self.counter.to_bytes(32, "big").hex()


@dataclass(slots=True)
class AuthTestContext:
    """聚合 HTTP 客户端、数据库查询工厂与可控认证依赖。"""

    client: httpx.AsyncClient
    session_factory: ManagedAsyncSessionMaker
    clock: MutableClock


@pytest.fixture
async def auth_context(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AuthTestContext]:
    """创建使用安全 Cookie 和真实测试数据库的认证 API 上下文。"""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_COOKIE_NAME", SESSION_COOKIE_NAME)
    monkeypatch.setenv("SESSION_TTL_SECONDS", "3600")
    get_settings.cache_clear()

    clock = MutableClock(datetime(2030, 1, 1, 8, 0, tzinfo=UTC))
    app = create_app()
    # API 依赖从 app.state 读取可替换时钟与令牌工厂；测试不依赖真实时间或随机性。
    app.state.auth_clock = clock
    app.state.auth_token_factory = CountingTokenFactory()
    query_session_factory = build_session_factory(database_url)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        yield AuthTestContext(client, query_session_factory, clock)

    await query_session_factory.dispose()
    app_session_factory = getattr(app.state, "auth_session_factory", None)
    if isinstance(app_session_factory, ManagedAsyncSessionMaker):
        await app_session_factory.dispose()
    get_settings.cache_clear()


async def _seed_user(
    context: AuthTestContext,
    *,
    email: str = ADMIN_EMAIL,
    password: str = ADMIN_PASSWORD,
    is_active: bool = True,
) -> UUID:
    """插入一个带 Argon2id 密码哈希的合成用户并返回其 ID。"""
    async with context.session_factory.begin() as session:
        user = UserModel(
            email=email,
            display_name="Synthetic Owner",
            password_hash=PasswordHasher().hash(password),
            timezone="Asia/Shanghai",
            locale="zh-CN",
            brief_time=time(8, 0),
            is_active=is_active,
        )
        session.add(user)
        await session.flush()
        return user.id


async def _login(context: AuthTestContext) -> httpx.Response:
    """使用包含大小写和边界空白的邮箱调用登录，验证规范化路径。"""
    return await context.client.post(
        "/api/v1/auth/login",
        json={"email": "  OWNER@EXAMPLE.COM  ", "password": ADMIN_PASSWORD},
    )


def _set_cookie_header(response: httpx.Response, cookie_name: str) -> str:
    """从响应中提取指定 Cookie 的完整 Set-Cookie 属性。"""
    prefix = f"{cookie_name}="
    return next(
        value for value in response.headers.get_list("set-cookie") if value.startswith(prefix)
    )


def _assert_problem(response: httpx.Response, status_code: int, error_code: str) -> ProblemDetails:
    """验证认证错误遵循带稳定扩展字段的 RFC 9457 Problem Details。"""
    assert response.status_code == status_code
    assert response.headers["content-type"].startswith("application/problem+json")
    body = ProblemDetails.model_validate(response.json())
    assert body.status == status_code
    assert body.error_code == error_code
    assert len(body.trace_id) >= 16
    return body


async def _current_session(context: AuthTestContext, user_id: UUID) -> UserSessionModel:
    """按客户端持有的原始 Cookie 摘要读取当前会话。"""
    raw_session_token = context.client.cookies.get(SESSION_COOKIE_NAME)
    assert raw_session_token is not None
    async with context.session_factory() as session:
        saved = await session.scalar(
            select(UserSessionModel).where(
                UserSessionModel.user_id == user_id,
                UserSessionModel.token_hash == hash_token(raw_session_token),
            )
        )
        assert saved is not None
        return saved


async def _add_session(
    context: AuthTestContext,
    user_id: UUID,
    label: str,
    *,
    created_at: datetime,
    expires_at: datetime,
    revoked_at: datetime | None = None,
) -> UUID:
    """插入只含摘要的合成会话，用于列表与用户隔离测试。"""
    async with context.session_factory.begin() as session:
        saved = UserSessionModel(
            user_id=user_id,
            token_hash=hashlib.sha256(f"session:{label}".encode()).digest(),
            csrf_hash=hashlib.sha256(f"csrf:{label}".encode()).digest(),
            created_at=created_at,
            expires_at=expires_at,
            last_seen_at=created_at,
            revoked_at=revoked_at,
        )
        session.add(saved)
        await session.flush()
        return saved.id


@pytest.mark.asyncio
async def test_login_sets_secure_cookies_persists_only_hashes_and_me_returns_user(
    auth_context: AuthTestContext,
) -> None:
    """登录应规范化邮箱、按配置 TTL 创建会话，并以安全 Cookie 返回原始令牌。"""
    user_id = await _seed_user(auth_context)

    response = await _login(auth_context)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(user_id)
    assert body["email"] == ADMIN_EMAIL
    assert body["display_name"] == "Synthetic Owner"
    assert body["timezone"] == "Asia/Shanghai"
    assert body["locale"] == "zh-CN"
    assert body["brief_time"] == "08:00:00"
    assert "password_hash" not in body

    session_cookie = _set_cookie_header(response, SESSION_COOKIE_NAME).lower()
    csrf_cookie = _set_cookie_header(response, CSRF_COOKIE_NAME).lower()
    assert "httponly" in session_cookie
    assert "secure" in session_cookie
    assert "samesite=lax" in session_cookie
    assert "path=/" in session_cookie
    assert "max-age=3600" in session_cookie
    assert "httponly" not in csrf_cookie
    assert "secure" in csrf_cookie
    assert "samesite=strict" in csrf_cookie
    assert "path=/" in csrf_cookie
    assert "max-age=3600" in csrf_cookie

    raw_session_token = auth_context.client.cookies.get(SESSION_COOKIE_NAME)
    raw_csrf_token = auth_context.client.cookies.get(CSRF_COOKIE_NAME)
    assert raw_session_token is not None
    assert raw_csrf_token is not None
    saved_session = await _current_session(auth_context, user_id)
    assert saved_session.token_hash == hash_token(raw_session_token)
    assert saved_session.csrf_hash == hash_token(raw_csrf_token)
    assert saved_session.token_hash != raw_session_token.encode()
    assert saved_session.csrf_hash != raw_csrf_token.encode()
    assert saved_session.created_at == auth_context.clock.now()
    assert saved_session.last_seen_at == auth_context.clock.now()
    assert saved_session.expires_at == auth_context.clock.now() + timedelta(seconds=3600)

    me_response = await auth_context.client.get("/api/v1/auth/me")
    assert me_response.status_code == 200
    assert me_response.json() == body


@pytest.mark.asyncio
async def test_login_rejects_unknown_wrong_password_and_inactive_user_identically(
    auth_context: AuthTestContext,
) -> None:
    """未知、错误密码或停用用户应返回同一公共错误，且都不创建会话。"""
    user_id = await _seed_user(auth_context)

    unknown_user = await auth_context.client.post(
        "/api/v1/auth/login",
        json={"email": "missing@example.com", "password": ADMIN_PASSWORD},
    )
    wrong_password = await auth_context.client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": "wrong-password"},
    )

    async with auth_context.session_factory.begin() as session:
        saved_user = await session.get(UserModel, user_id)
        assert saved_user is not None
        saved_user.is_active = False

    inactive = await _login(auth_context)
    problems = tuple(
        _assert_problem(response, 401, "invalid_credentials")
        for response in (unknown_user, wrong_password, inactive)
    )
    public_payloads = tuple(problem.model_dump(exclude={"trace_id"}) for problem in problems)
    assert public_payloads == (public_payloads[0],) * 3
    async with auth_context.session_factory() as session:
        assert (await session.scalar(select(UserSessionModel))) is None


@pytest.mark.asyncio
async def test_login_validation_error_uses_redacted_problem_details(
    auth_context: AuthTestContext,
) -> None:
    """请求 Schema 失败也必须返回稳定 Problem，且不能回显密码字段输入。"""
    sensitive_marker = "synthetic-sensitive-input-marker"

    response = await auth_context.client.post(
        "/api/v1/auth/login",
        json={"email": ADMIN_EMAIL, "password": {"value": sensitive_marker}},
    )

    _assert_problem(response, 422, "request_validation_failed")
    assert sensitive_marker not in response.text


@pytest.mark.parametrize("invalid_state", ["expired", "revoked"])
@pytest.mark.asyncio
async def test_expired_or_revoked_session_is_rejected(
    auth_context: AuthTestContext,
    invalid_state: str,
) -> None:
    """过期或已撤销会话都不能继续访问受保护资源。"""
    user_id = await _seed_user(auth_context)
    assert (await _login(auth_context)).status_code == 200
    saved_session = await _current_session(auth_context, user_id)

    async with auth_context.session_factory.begin() as session:
        mutable_session = await session.get(UserSessionModel, saved_session.id)
        assert mutable_session is not None
        if invalid_state == "expired":
            mutable_session.expires_at = auth_context.clock.now()
        else:
            mutable_session.revoked_at = auth_context.clock.now()

    response = await auth_context.client.get("/api/v1/auth/me")
    _assert_problem(response, 401, "authentication_required")


@pytest.mark.asyncio
async def test_last_seen_is_updated_no_more_than_once_every_five_minutes(
    auth_context: AuthTestContext,
) -> None:
    """活跃请求在五分钟窗口内不得制造重复数据库写入。"""
    user_id = await _seed_user(auth_context)
    assert (await _login(auth_context)).status_code == 200
    initial = await _current_session(auth_context, user_id)

    auth_context.clock.advance(timedelta(minutes=4, seconds=59))
    assert (await auth_context.client.get("/api/v1/auth/me")).status_code == 200
    before_threshold = await _current_session(auth_context, user_id)
    assert before_threshold.last_seen_at == initial.last_seen_at

    auth_context.clock.advance(timedelta(seconds=1))
    assert (await auth_context.client.get("/api/v1/auth/me")).status_code == 200
    after_threshold = await _current_session(auth_context, user_id)
    assert after_threshold.last_seen_at == auth_context.clock.now()


@pytest.mark.asyncio
async def test_logout_requires_cookie_header_and_stored_csrf_match_then_revokes_and_clears(
    auth_context: AuthTestContext,
) -> None:
    """登出必须同时验证 CSRF Cookie、Header 与数据库摘要，成功后撤销会话。"""
    user_id = await _seed_user(auth_context)
    assert (await _login(auth_context)).status_code == 200
    saved_session = await _current_session(auth_context, user_id)
    raw_csrf_token = auth_context.client.cookies.get(CSRF_COOKIE_NAME)
    assert raw_csrf_token is not None

    missing_header = await auth_context.client.post("/api/v1/auth/logout")
    _assert_problem(missing_header, 403, "csrf_rejected")

    mismatched_header = await auth_context.client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": f"{raw_csrf_token}x"}
    )
    _assert_problem(mismatched_header, 403, "csrf_rejected")

    async with auth_context.session_factory.begin() as session:
        mutable_session = await session.get(UserSessionModel, saved_session.id)
        assert mutable_session is not None
        mutable_session.csrf_hash = hashlib.sha256(b"different-stored-csrf").digest()
    stored_mismatch = await auth_context.client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": raw_csrf_token}
    )
    _assert_problem(stored_mismatch, 403, "csrf_rejected")

    async with auth_context.session_factory.begin() as session:
        mutable_session = await session.get(UserSessionModel, saved_session.id)
        assert mutable_session is not None
        mutable_session.csrf_hash = hash_token(raw_csrf_token)
    success = await auth_context.client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": raw_csrf_token}
    )
    assert success.status_code == 204
    assert auth_context.client.cookies.get(SESSION_COOKIE_NAME) is None
    assert auth_context.client.cookies.get(CSRF_COOKIE_NAME) is None
    assert "max-age=0" in _set_cookie_header(success, SESSION_COOKIE_NAME).lower()
    assert "max-age=0" in _set_cookie_header(success, CSRF_COOKIE_NAME).lower()
    async with auth_context.session_factory() as session:
        revoked = await session.get(UserSessionModel, saved_session.id)
        assert revoked is not None
        assert revoked.revoked_at == auth_context.clock.now()


@pytest.mark.asyncio
async def test_session_list_contains_only_active_owned_sessions_without_hashes(
    auth_context: AuthTestContext,
) -> None:
    """会话列表只公开当前用户未过期、未撤销的元数据。"""
    user_id = await _seed_user(auth_context)
    assert (await _login(auth_context)).status_code == 200
    current = await _current_session(auth_context, user_id)
    now = auth_context.clock.now()
    active_id = await _add_session(
        auth_context,
        user_id,
        "active",
        created_at=now - timedelta(minutes=10),
        expires_at=now + timedelta(hours=2),
    )
    await _add_session(
        auth_context,
        user_id,
        "expired",
        created_at=now - timedelta(hours=2),
        expires_at=now,
    )
    await _add_session(
        auth_context,
        user_id,
        "revoked",
        created_at=now - timedelta(minutes=20),
        expires_at=now + timedelta(hours=2),
        revoked_at=now - timedelta(minutes=1),
    )

    response = await auth_context.client.get("/api/v1/auth/sessions")

    assert response.status_code == 200
    items = response.json()
    assert {item["id"] for item in items} == {str(current.id), str(active_id)}
    assert sum(item["is_current"] for item in items) == 1
    for item in items:
        assert set(item) == {"id", "created_at", "last_seen_at", "expires_at", "is_current"}
        assert "token_hash" not in item
        assert "csrf_hash" not in item


@pytest.mark.asyncio
async def test_revoke_owned_non_current_session_preserves_current_cookies(
    auth_context: AuthTestContext,
) -> None:
    """撤销本人其他会话应保留当前浏览器认证状态。"""
    user_id = await _seed_user(auth_context)
    assert (await _login(auth_context)).status_code == 200
    csrf = auth_context.client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None
    other_session_id = await _add_session(
        auth_context,
        user_id,
        "owned-other",
        created_at=auth_context.clock.now(),
        expires_at=auth_context.clock.now() + timedelta(hours=2),
    )

    response = await auth_context.client.delete(
        f"/api/v1/auth/sessions/{other_session_id}", headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 204
    assert auth_context.client.cookies.get(SESSION_COOKIE_NAME) is not None
    assert auth_context.client.cookies.get(CSRF_COOKIE_NAME) is not None
    async with auth_context.session_factory() as session:
        revoked = await session.get(UserSessionModel, other_session_id)
        assert revoked is not None
        assert revoked.revoked_at == auth_context.clock.now()


@pytest.mark.asyncio
async def test_cross_user_revoke_is_indistinguishable_from_missing_and_does_not_mutate(
    auth_context: AuthTestContext,
) -> None:
    """跨用户撤销必须以相同 404 拒绝，且不能泄露目标是否存在。"""
    owner_id = await _seed_user(auth_context)
    other_user_id = await _seed_user(
        auth_context,
        email="other@example.com",
        password="another-synthetic-password",
    )
    assert (await _login(auth_context)).status_code == 200
    csrf = auth_context.client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None
    other_session_id = await _add_session(
        auth_context,
        other_user_id,
        "cross-user",
        created_at=auth_context.clock.now(),
        expires_at=auth_context.clock.now() + timedelta(hours=2),
    )

    cross_user = await auth_context.client.delete(
        f"/api/v1/auth/sessions/{other_session_id}", headers={"X-CSRF-Token": csrf}
    )
    missing = await auth_context.client.delete(
        f"/api/v1/auth/sessions/{UUID(int=0)}", headers={"X-CSRF-Token": csrf}
    )

    cross_problem = _assert_problem(cross_user, 404, "session_not_found")
    missing_problem = _assert_problem(missing, 404, "session_not_found")
    assert cross_problem.title == missing_problem.title
    assert cross_problem.detail == missing_problem.detail
    async with auth_context.session_factory() as session:
        untouched = await session.get(UserSessionModel, other_session_id)
        assert untouched is not None
        assert untouched.user_id == other_user_id
        assert untouched.user_id != owner_id
        assert untouched.revoked_at is None


@pytest.mark.asyncio
async def test_revoking_current_session_clears_cookies_and_authentication(
    auth_context: AuthTestContext,
) -> None:
    """撤销当前会话应清 Cookie，并使后续请求立即失去认证。"""
    user_id = await _seed_user(auth_context)
    assert (await _login(auth_context)).status_code == 200
    current = await _current_session(auth_context, user_id)
    csrf = auth_context.client.cookies.get(CSRF_COOKIE_NAME)
    assert csrf is not None

    response = await auth_context.client.delete(
        f"/api/v1/auth/sessions/{current.id}", headers={"X-CSRF-Token": csrf}
    )

    assert response.status_code == 204
    assert auth_context.client.cookies.get(SESSION_COOKIE_NAME) is None
    assert auth_context.client.cookies.get(CSRF_COOKIE_NAME) is None
    _assert_problem(
        await auth_context.client.get("/api/v1/auth/me"),
        401,
        "authentication_required",
    )
