"""任务 REST 接口的集成验收测试。"""

from datetime import time
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from ai_employee.config import get_settings
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel, TaskStepModel
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
    async with session_factory() as session:
        audit_events = (
            await session.scalars(
                select(AuditEventModel)
                .where(AuditEventModel.task_id == task_id)
                .order_by(AuditEventModel.id)
            )
        ).all()

    assert [(event.event_type, event.event_metadata) for event in audit_events] == [
        ("task.created", {"kind": "fake_write", "status": "created"}),
        ("task.queued", {"status": "queued"}),
    ]


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
    retry_id = UUID(retry.json()["id"])
    async with session_factory() as session:
        audit_events = (
            await session.scalars(
                select(AuditEventModel)
                .where(AuditEventModel.task_id == retry_id)
                .order_by(AuditEventModel.id)
            )
        ).all()

    assert [(event.event_type, event.event_metadata) for event in audit_events] == [
        (
            "task.created",
            {"retry_of_task_id": str(failed_id), "status": "created"},
        ),
        ("task.queued", {"status": "queued"}),
    ]


@pytest.mark.asyncio
async def test_retry_idempotency_key_is_scoped_to_the_original_failed_task(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """同一用户复用重试键时，其他失败任务不得返回无关 replacement。"""
    client, session_factory, user_id = task_client
    async with session_factory.begin() as session:
        first_failed = TaskRunModel(
            user_id=user_id,
            kind="fake_write",
            status="failed",
            idempotency_key="first-failed-task",
            input_payload={},
        )
        second_failed = TaskRunModel(
            user_id=user_id,
            kind="fake_write",
            status="failed",
            idempotency_key="second-failed-task",
            input_payload={},
        )
        session.add_all((first_failed, second_failed))
        await session.flush()
        first_failed_id = first_failed.id
        second_failed_id = second_failed.id

    csrf = client.cookies.get("ai_employee_csrf") or ""
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": "shared-retry-key"}
    first_retry = await client.post(f"/api/v1/tasks/{first_failed_id}/retry", headers=headers)
    second_retry = await client.post(f"/api/v1/tasks/{second_failed_id}/retry", headers=headers)

    assert first_retry.status_code == 200
    assert second_retry.status_code == 200
    assert first_retry.json()["retry_of_task_id"] == str(first_failed_id)
    assert second_retry.json()["retry_of_task_id"] == str(second_failed_id)
    assert first_retry.json()["id"] != second_retry.json()["id"]


@pytest.mark.asyncio
async def test_cross_user_task_is_hidden_as_not_found(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """另一个用户拥有的任务必须与不存在任务同样返回 404。"""
    client, session_factory, _ = task_client
    async with session_factory.begin() as session:
        other_user = UserModel(
            email="other-tasks@example.com",
            display_name="Other task owner",
            password_hash=PasswordHasher().hash("synthetic-password"),
            timezone="Asia/Shanghai",
            locale="zh-CN",
            brief_time=time(8, 0),
            is_active=True,
        )
        session.add(other_user)
        await session.flush()
        foreign_task = TaskRunModel(
            user_id=other_user.id,
            kind="fake_write",
            status="queued",
            idempotency_key="foreign-task",
            input_payload={},
        )
        session.add(foreign_task)
        await session.flush()
        foreign_task_id = foreign_task.id

    response = await client.get(f"/api/v1/tasks/{foreign_task_id}")

    assert response.status_code == 404
    assert response.json()["error_code"] == "task_not_found"


@pytest.mark.asyncio
async def test_task_and_approval_mutations_require_csrf(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """任务创建和审批决定均拒绝缺失 CSRF Header 的已登录 Cookie 请求。"""
    client, _, _ = task_client
    task_response = await client.post(
        "/api/v1/tasks",
        json={"kind": "fake_write", "input_payload": {}},
        headers={"Idempotency-Key": "csrf-task"},
    )
    approval_response = await client.post(
        f"/api/v1/approvals/{UUID(int=0)}/decision",
        json={"decision": "approved", "version": 1, "payload_hash": "a" * 64},
    )

    assert task_response.status_code == 403
    assert approval_response.status_code == 403
    assert task_response.json()["error_code"] == "csrf_rejected"
    assert approval_response.json()["error_code"] == "csrf_rejected"
