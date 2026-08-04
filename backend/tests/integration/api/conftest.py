"""为 Task 16 REST 集成测试提供真实认证会话。"""

from dataclasses import dataclass
from datetime import time
from uuid import UUID

import httpx
import pytest

from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.main import create_app


@dataclass(frozen=True, slots=True)
class AuthenticatedApiClients:
    """封装两个真实登录用户及其共享的测试数据库工厂。"""

    owner: httpx.AsyncClient
    other: httpx.AsyncClient
    session_factory: ManagedAsyncSessionMaker
    owner_id: UUID
    other_id: UUID


@pytest.fixture
async def authenticated_api_clients(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> AuthenticatedApiClients:
    """创建两个独立 Cookie 会话，验证 API 授权始终以数据库用户归属为准。"""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_COOKIE_NAME", "task16_test_session")
    get_settings.cache_clear()
    session_factory = build_session_factory(database_url)
    password = "synthetic-task16-password"
    async with session_factory.begin() as session:
        users = [
            UserModel(
                email="task16-owner@example.test",
                display_name="Task 16 Owner",
                password_hash=PasswordHasher().hash(password),
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            ),
            UserModel(
                email="task16-other@example.test",
                display_name="Task 16 Other",
                password_hash=PasswordHasher().hash(password),
                timezone="UTC",
                locale="en-US",
                brief_time=time(9, 0),
                is_active=True,
            ),
        ]
        session.add_all(users)
        await session.flush()
        owner_id, other_id = users[0].id, users[1].id
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as owner,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as other,
    ):
        for client, email in ((owner, users[0].email), (other, users[1].email)):
            response = await client.post(
                "/api/v1/auth/login", json={"email": email, "password": password}
            )
            assert response.status_code == 200
        yield AuthenticatedApiClients(owner, other, session_factory, owner_id, other_id)
    await session_factory.dispose()
    await app.state.auth_session_factory.dispose()
    get_settings.cache_clear()
