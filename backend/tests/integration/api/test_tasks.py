"""任务 REST 接口的集成验收测试。"""

from datetime import UTC, datetime, time
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import Response
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
                    started_at=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
                    finished_at=datetime(2026, 8, 3, 0, 0, 2, tzinfo=UTC),
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
    assert snapshot.json()["event_cursor"] > 0
    assert [step["sequence"] for step in snapshot.json()["steps"]] == [1, 2]
    assert snapshot.json()["steps"][1] == {
        "id": snapshot.json()["steps"][1]["id"],
        "sequence": 2,
        "name": "second",
        "status": "pending",
        "output_summary": None,
        "error_code": None,
        "started_at": "2026-08-03T00:00:00Z",
        "finished_at": "2026-08-03T00:00:02Z",
    }
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
    assert snapshot.json()["event_cursor"] == audit_events[-1].id


@pytest.mark.asyncio
async def test_event_route_uses_query_cursor_when_eventsource_cannot_set_header(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """主动重订阅把已知游标作为查询参数传入，仍不改变 Cookie 认证边界。"""
    client, session_factory, user_id = task_client
    async with session_factory.begin() as session:
        task = TaskRunModel(
            user_id=user_id,
            kind="fake_write",
            status="queued",
            idempotency_key="query-cursor-task",
            input_payload={},
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    class CapturingStream:
        """捕获路由传给 SSE 基础设施的游标，避免测试永久流。"""

        last_event_id: int | None = None

        def response(self, *, last_event_id: int | None, **_: object) -> Response:
            """记录公开游标并返回可立即结束的测试响应。"""
            self.last_event_id = last_event_id
            return Response(status_code=204)

    stream = CapturingStream()
    transport = cast(httpx.ASGITransport, client._transport)
    transport.app.state.task_event_stream = stream

    response = await client.get(f"/api/v1/tasks/{task_id}/events?last_event_id=0")

    assert response.status_code == 204
    assert stream.last_event_id == 0


@pytest.mark.asyncio
async def test_event_route_prefers_standard_header_cursor_over_query_cursor(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """浏览器重连的标准 Header 必须覆盖主动订阅遗留的查询游标。"""
    client, session_factory, user_id = task_client
    async with session_factory.begin() as session:
        task = TaskRunModel(
            user_id=user_id,
            kind="fake_write",
            status="queued",
            idempotency_key="header-cursor-task",
            input_payload={},
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    class CapturingStream:
        """捕获路由解析的游标，确保断言覆盖真实请求边界。"""

        last_event_id: int | None = None

        def response(self, *, last_event_id: int | None, **_: object) -> Response:
            """保存传入基础设施的游标并结束测试响应。"""
            self.last_event_id = last_event_id
            return Response(status_code=204)

    stream = CapturingStream()
    transport = cast(httpx.ASGITransport, client._transport)
    transport.app.state.task_event_stream = stream

    response = await client.get(
        f"/api/v1/tasks/{task_id}/events?last_event_id=3",
        headers={"Last-Event-ID": "24"},
    )

    assert response.status_code == 204
    assert stream.last_event_id == 24


@pytest.mark.asyncio
async def test_event_route_rejects_invalid_standard_header_cursor(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """非法 Last-Event-ID Header 必须返回既定的 RFC 9457 验证问题。"""
    client, session_factory, user_id = task_client
    async with session_factory.begin() as session:
        task = TaskRunModel(
            user_id=user_id,
            kind="fake_write",
            status="queued",
            idempotency_key="invalid-header-cursor-task",
            input_payload={},
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    response = await client.get(
        f"/api/v1/tasks/{task_id}/events",
        headers={"Last-Event-ID": "not-a-cursor"},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "invalid_last_event_id"


@pytest.mark.asyncio
async def test_create_rejects_idempotency_key_longer_than_persisted_column(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """超过任务表长度的幂等键在请求边界返回 RFC 9457 验证问题。"""
    client, session_factory, _ = task_client
    csrf = client.cookies.get("ai_employee_csrf") or ""

    response = await client.post(
        "/api/v1/tasks",
        json={"kind": "fake_write", "input_payload": {}},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "x" * 256},
    )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["error_code"] == "request_validation_failed"
    async with session_factory() as session:
        assert await session.scalar(select(TaskRunModel.id)) is None


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
