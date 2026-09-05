"""验证连接能力 API 的会话、CSRF、用户隔离与显式响应投影。"""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from sqlalchemy import func, select

from ai_employee.config import get_settings
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.session import (
    ManagedAsyncSessionMaker,
    build_session_factory,
)
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.main import create_app


@dataclass(frozen=True, slots=True)
class AuthenticatedApiClients:
    """封装两个真实 Cookie 会话及其共享测试数据库。"""

    owner: httpx.AsyncClient
    other: httpx.AsyncClient
    session_factory: ManagedAsyncSessionMaker
    owner_id: UUID
    other_id: UUID
    oauth_adapter: "FakeOAuthAdapter"


@dataclass(slots=True)
class FakeOAuthAdapter:
    """为能力 API 返回确定性授权 URL，绝不访问真实供应商。"""

    provider: str = "google"
    scope_calls: int = 0
    revoke_calls: int = 0

    def scopes_for(self, capabilities: frozenset[ConnectionCapability]) -> frozenset[str]:
        """把四种内部能力映射到只用于测试的合成 scope。"""
        self.scope_calls += 1
        return frozenset(f"scope:{capability.value}" for capability in capabilities)

    def build_authorization_url(self, request: object) -> str:
        """忽略敏感随机值，只返回固定测试域名。"""
        del request
        return "https://provider.example.test/authorize"

    async def exchange_code(self, *, code: str, verifier: str) -> object:
        """本文件不完成 callback，授权码交换不可达。"""
        del code, verifier
        raise AssertionError("exchange_code must not be called")

    async def fetch_account(self, token: object, *, expected_nonce_hash: bytes | None) -> object:
        """本文件不完成 callback，账户读取不可达。"""
        del token, expected_nonce_hash
        raise AssertionError("fetch_account must not be called")

    async def refresh(self, refresh_token: str) -> object:
        """能力 API 不刷新 token。"""
        del refresh_token
        raise AssertionError("refresh must not be called")

    async def revoke(self, token: str) -> object:
        """能力 API 不撤销 token。"""
        del token
        self.revoke_calls += 1
        raise AssertionError("revoke must not be called")


@pytest.fixture
async def authenticated_api_clients(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AuthenticatedApiClients:
    """创建注入固定 OAuth adapter 的真实双用户 API 会话。

    测试只替换供应商边界；登录、Cookie、CSRF、数据库事务和 Repository 均使用真实实现，
    因而能够证明 API 不能依赖前端隐藏资源来实现用户隔离。
    """
    master_key = tmp_path / "task8-master-key"
    master_key.write_text(
        "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=",
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:56379/0")
    monkeypatch.setenv("SESSION_COOKIE_NAME", "task8_connection_session")
    monkeypatch.setenv("APP_MASTER_KEY_FILE", str(master_key))
    get_settings.cache_clear()

    session_factory = build_session_factory(database_url)
    password = "synthetic-task8-password"
    async with session_factory.begin() as session:
        users = (
            UserModel(
                email="task8-owner@example.test",
                display_name="Task 8 Owner",
                password_hash=PasswordHasher().hash(password),
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            ),
            UserModel(
                email="task8-other@example.test",
                display_name="Task 8 Other",
                password_hash=PasswordHasher().hash(password),
                timezone="UTC",
                locale="en-US",
                brief_time=time(9, 0),
                is_active=True,
            ),
        )
        session.add_all(users)
        await session.flush()
        owner_id, other_id = users[0].id, users[1].id

    app = create_app()
    # 固定 mapping 只在组合根注入；用例没有运行时注册入口，也不会读取真实 OAuth secret。
    oauth_adapter = FakeOAuthAdapter()
    app.state.oauth_adapters = {"google": oauth_adapter}
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as owner,
        httpx.AsyncClient(transport=transport, base_url="https://testserver") as other,
    ):
        for client, email in (
            (owner, "task8-owner@example.test"),
            (other, "task8-other@example.test"),
        ):
            response = await client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": password},
            )
            assert response.status_code == 200
        yield AuthenticatedApiClients(
            owner,
            other,
            session_factory,
            owner_id,
            other_id,
            oauth_adapter,
        )

    await session_factory.dispose()
    await app.state.auth_session_factory.dispose()
    get_settings.cache_clear()


async def _seed_connection(
    clients: AuthenticatedApiClients,
    *,
    enabled_capabilities: frozenset[ConnectionCapability] | None = None,
) -> UUID:
    """写入四项能力与一个日历目录项，所有文本均为合成数据。

    ``enabled_capabilities`` 只用于构造依赖闭包完整的合成连接，默认保持既有测试的
    ``mail.read`` 初始状态；调用方不得借此绕过应用层的能力依赖校验。
    """
    enabled = (
        frozenset({ConnectionCapability.MAIL_READ})
        if enabled_capabilities is None
        else enabled_capabilities
    )
    scopes = [
        f"scope:{capability.value}" for capability in sorted(enabled, key=lambda item: item.value)
    ]
    async with clients.session_factory.begin() as session:
        connection = OAuthConnectionModel(
            user_id=clients.owner_id,
            provider="google",
            provider_account_id="task8-provider-account",
            provider_tenant_id="",
            account_type="google",
            account_email="task8-calendar@example.test",
            scopes=scopes,
            status="connected",
            last_error_code=None,
        )
        session.add(connection)
        await session.flush()
        session.add_all(
            ConnectionCapabilityModel(
                user_id=clients.owner_id,
                connection_id=connection.id,
                capability=capability.value,
                status=("enabled" if capability in enabled else "disabled"),
                actual_scopes=([f"scope:{capability.value}"] if capability in enabled else []),
                last_verified_at=(
                    datetime(2030, 1, 1, tzinfo=UTC) if capability in enabled else None
                ),
                last_error_code=None,
            )
            for capability in ConnectionCapability
        )
        session.add(
            ProviderCalendarModel(
                user_id=clients.owner_id,
                connection_id=connection.id,
                provider_calendar_id="calendar-primary",
                name="Synthetic Team Calendar",
                timezone="Asia/Shanghai",
                is_primary=True,
                access_role="owner",
                can_write=True,
                provider_url="https://calendar.example.test/calendar-primary",
            )
        )
        return connection.id


async def _connection_disable_invariants(
    clients: AuthenticatedApiClients,
    connection_id: UUID,
) -> tuple[tuple[tuple[str, str], ...], tuple[int, int, int, int]]:
    """读取禁用请求前后必须保持不变的能力与可信动作事实。

    测试连接没有预置真实动作，因此四个全表计数足以证明拒绝路径没有偷偷创建或失效
    Task/Approval/Audit/Outbox；能力状态则按该连接和用户显式限定，避免依赖 ORM 默认排序。
    """
    async with clients.session_factory() as session:
        capabilities = tuple(
            (
                row.capability,
                row.status,
            )
            for row in (
                await session.scalars(
                    select(ConnectionCapabilityModel)
                    .where(
                        ConnectionCapabilityModel.user_id == clients.owner_id,
                        ConnectionCapabilityModel.connection_id == connection_id,
                    )
                    .order_by(ConnectionCapabilityModel.capability)
                )
            ).all()
        )
        count_values: list[int] = []
        for model in (
            TaskRunModel,
            ApprovalRequestModel,
            AuditEventModel,
            OutboxEventModel,
        ):
            count_values.append(
                int(await session.scalar(select(func.count()).select_from(model)) or 0)
            )
        counts = tuple(count_values)
    return capabilities, counts


@pytest.mark.asyncio
async def test_capabilities_are_user_scoped_typed_and_not_cached(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """GET 只返回当前用户的显式能力/日历字段，并禁止缓存账户名称。"""
    clients = authenticated_api_clients
    connection_id = await _seed_connection(clients)

    owner_response = await clients.owner.get(f"/api/v1/connections/{connection_id}/capabilities")
    foreign_response = await clients.other.get(f"/api/v1/connections/{connection_id}/capabilities")

    assert owner_response.status_code == 200
    assert owner_response.headers["Cache-Control"] == "no-store"
    assert foreign_response.status_code == 404
    assert foreign_response.json()["error_code"] == "connection_not_found"

    payload = owner_response.json()
    assert payload["connection_id"] == str(connection_id)
    assert payload["provider"] == "google"
    assert [item["capability"] for item in payload["capabilities"]] == [
        "calendar.read",
        "calendar.write",
        "mail.read",
        "mail.send",
    ]
    assert payload["provider_calendars"] == [
        {
            "id": "calendar-primary",
            "name": "Synthetic Team Calendar",
            "timezone": "Asia/Shanghai",
            "is_primary": True,
            "access_role": "owner",
            "can_write": True,
            "provider_url": "https://calendar.example.test/calendar-primary",
        }
    ]
    assert set(payload["provider_calendars"][0]) == {
        "id",
        "name",
        "timezone",
        "is_primary",
        "access_role",
        "can_write",
        "provider_url",
    }


@pytest.mark.asyncio
async def test_enable_mail_send_requires_csrf_and_hides_foreign_connections(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """修改请求须通过双提交 CSRF，且跨用户连接在用例层仍表现为 404。"""
    clients = authenticated_api_clients
    connection_id = await _seed_connection(clients)
    path = f"/api/v1/connections/{connection_id}/capabilities/mail.send/enable"

    missing = await clients.owner.post(path)
    wrong = await clients.owner.post(path, headers={"X-CSRF-Token": "wrong-token"})
    foreign = await clients.other.post(
        path,
        headers={"X-CSRF-Token": clients.other.cookies.get("ai_employee_csrf") or ""},
    )
    allowed = await clients.owner.post(
        path,
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
    )

    assert missing.status_code == 403 and missing.json()["error_code"] == "csrf_rejected"
    assert wrong.status_code == 403 and wrong.json()["error_code"] == "csrf_rejected"
    assert foreign.status_code == 404
    assert allowed.status_code == 200
    assert allowed.json() == {
        "authorization_url": "https://provider.example.test/authorize",
        "requested_capabilities": ["mail.read", "mail.send"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("read_capability", "write_capability"),
    (
        (ConnectionCapability.MAIL_READ, ConnectionCapability.MAIL_SEND),
        (ConnectionCapability.CALENDAR_READ, ConnectionCapability.CALENDAR_WRITE),
    ),
)
async def test_disable_read_capability_rejects_enabled_write_dependency_without_mutation(
    authenticated_api_clients: AuthenticatedApiClients,
    read_capability: ConnectionCapability,
    write_capability: ConnectionCapability,
) -> None:
    """只读能力被写能力依赖时返回稳定 409，且不触发任何持久化或供应商调用。

    第二段先关闭写能力，再关闭读取能力，作为同一 HTTP 契约中的正例；这也证明拒绝
    路径没有把 capability/action/task/audit/outbox 的事实部分提交后再回滚不完整。
    """
    clients = authenticated_api_clients
    connection_id = await _seed_connection(
        clients,
        enabled_capabilities=frozenset({read_capability, write_capability}),
    )
    path = f"/api/v1/connections/{connection_id}/capabilities/{read_capability.value}/disable"
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    before = await _connection_disable_invariants(clients, connection_id)
    provider_calls_before = (clients.oauth_adapter.scope_calls, clients.oauth_adapter.revoke_calls)

    rejected = await clients.owner.post(path, headers={"X-CSRF-Token": csrf})

    assert rejected.status_code == 409
    assert rejected.json()["error_code"] == "connection_capability_dependency_conflict"
    assert rejected.json()["detail"] == "The request conflicts with current state."
    assert await _connection_disable_invariants(clients, connection_id) == before
    assert (clients.oauth_adapter.scope_calls, clients.oauth_adapter.revoke_calls) == (
        provider_calls_before
    )

    disable_write = await clients.owner.post(
        f"/api/v1/connections/{connection_id}/capabilities/{write_capability.value}/disable",
        headers={"X-CSRF-Token": csrf},
    )
    disable_read = await clients.owner.post(path, headers={"X-CSRF-Token": csrf})

    assert disable_write.status_code == 200
    assert disable_write.json() == {
        "capability": write_capability.value,
        "status": "disabled",
    }
    assert disable_read.status_code == 200
    assert disable_read.json() == {
        "capability": read_capability.value,
        "status": "disabled",
    }
    final_capabilities, _ = await _connection_disable_invariants(clients, connection_id)
    assert dict(final_capabilities)[read_capability.value] == "disabled"
    assert dict(final_capabilities)[write_capability.value] == "disabled"
    assert (clients.oauth_adapter.scope_calls, clients.oauth_adapter.revoke_calls) == (
        provider_calls_before
    )


@pytest.mark.asyncio
async def test_callback_maps_non_ascii_state_to_content_free_rejection(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """Unicode state 不能触发 500，响应只能包含稳定 ``oauth_state_rejected`` 契约。"""
    invalid_state = "非ASCII状态"

    response = await authenticated_api_clients.owner.get(
        "/api/v1/connections/google/callback",
        params={"code": "synthetic-code", "state": invalid_state},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["error_code"] == "oauth_state_rejected"
    assert payload["detail"] == "The OAuth state is invalid or expired."
    assert invalid_state not in response.text
