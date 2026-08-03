"""任务 REST 接口的集成验收测试。"""

from datetime import time
from uuid import UUID

import httpx
import pytest

from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel, TaskStepModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.main import create_app


@pytest.fixture
async def task_client(
    database_url: str, monkeypatch: pytest.MonkeyPatch
) -> tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID]:
    """构造通过真实 Cookie+CSRF 登录的任务 API 客户端。"""
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("SESSION_COOKIE_NAME", "task_test_session")
    get_settings.cache_clear()
    session_factory = build_session_factory(database_url)
    async with session_factory.begin() as session:
        user = UserModel(
            email="tasks@example.com",
            display_name="Task owner",
            password_hash=PasswordHasher().hash("synthetic-password"),
            timezone="Asia/Shanghai",
            locale="zh-CN",
            brief_time=time(8, 0),
            is_active=True,
        )
        session.add(user)
        await session.flush()
        user_id = user.id
    app = create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": "tasks@example.com", "password": "synthetic-password"},
        )
        assert login.status_code == 200
        yield client, session_factory, user_id
    await session_factory.dispose()
    await app.state.auth_session_factory.dispose()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_task_routes_are_registered() -> None:
    """任务 API 在认证后的应用中必须具有稳定路由前缀。"""
    from ai_employee.main import create_app

    paths = {
        route.path
        for included in create_app().routes
        for route in getattr(getattr(included, "original_router", included), "routes", (included,))
        if hasattr(route, "path")
    }

    assert "/api/v1/tasks" in paths


@pytest.mark.asyncio
async def test_create_is_idempotent_and_gets_ordered_steps(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """202 创建同键复用，读取步骤按稳定 sequence 排序。"""
    client, session_factory, _ = task_client
    csrf = client.cookies.get("ai_employee_csrf") or ""
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "task-create-1"}
    first = await client.post(
        "/api/v1/tasks", json={"kind": "fake_write", "input_payload": {}}, headers=headers
    )
    second = await client.post(
        "/api/v1/tasks", json={"kind": "fake_write", "input_payload": {}}, headers=headers
    )
    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert second.json()["task_id"] == first.json()["task_id"]
    task_id = UUID(first.json()["task_id"])
    async with session_factory.begin() as session:
        session.add_all(
            (
                TaskStepModel(
                    task_id=task_id,
                    sequence=2,
                    name="second",
                    kind="test",
                    status="pending",
                    input_summary={},
                ),
                TaskStepModel(
                    task_id=task_id,
                    sequence=1,
                    name="first",
                    kind="test",
                    status="pending",
                    input_summary={},
                ),
            )
        )
    snapshot = await client.get(f"/api/v1/tasks/{task_id}")
    assert [step["sequence"] for step in snapshot.json()["steps"]] == [1, 2]


@pytest.mark.asyncio
async def test_cancel_and_retry_create_a_replacement(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """取消不重开任务，失败任务重试按键创建且复用 replacement。"""
    client, session_factory, user_id = task_client
    csrf = client.cookies.get("ai_employee_csrf") or ""
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "task-cancel-1"}
    created = await client.post(
        "/api/v1/tasks", json={"kind": "fake_write", "input_payload": {}}, headers=headers
    )
    cancelled = await client.post(
        f"/api/v1/tasks/{created.json()['task_id']}/cancel", headers={"X-CSRF-Token": csrf}
    )
    assert cancelled.json()["status"] == "cancelled"
    async with session_factory.begin() as session:
        failed = TaskRunModel(
            user_id=user_id,
            kind="fake_write",
            status="failed",
            idempotency_key="failed-task",
            input_payload={"safe": True},
        )
        session.add(failed)
        await session.flush()
        failed_id = failed.id
    retry_headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "retry-1"}
    retry = await client.post(f"/api/v1/tasks/{failed_id}/retry", headers=retry_headers)
    repeated = await client.post(f"/api/v1/tasks/{failed_id}/retry", headers=retry_headers)
    assert retry.json()["retry_of_task_id"] == str(failed_id)
    assert repeated.json()["id"] == retry.json()["id"]
