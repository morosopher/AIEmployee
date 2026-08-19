"""任务 REST 接口的集成验收测试。"""

from collections.abc import Iterator
from datetime import UTC, datetime, time
from typing import cast
from uuid import UUID

import httpx
import pytest
from fastapi import Response
from sqlalchemy import select

from ai_employee.application.use_cases.approvals import ApprovalDecisionUseCase
from ai_employee.config import get_settings
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as ValidatedTestDatabaseUrl,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    TaskStepModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.passwords import PasswordHasher
from ai_employee.main import create_app

# 任务 API 回归使用 Cycle 5 regular head，并由 tracked fixture 在临时数据库清理前
# 释放本模块创建的异步 pool；不得为了聚焦测试放宽 Task 13 anchor 的迁移 guard。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把本模块绑定到已验证的 regular head 数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖全局迁移 fixture；共享 helper 已完成 typed lifecycle 复核。"""
    del cycle5_regular_database_url
    yield


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
    assert int(snapshot.json()["event_cursor"]) > 0
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
    assert snapshot.json()["event_cursor"] == str(audit_events[-1].id)


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
async def test_cancel_rejects_running_task_with_committed_result(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """最终结果已与业务副作用提交后，通用取消不得覆盖仍待 Runner 收尾的任务。"""
    client, session_factory, user_id = task_client
    lease_owner = "committed-result-worker"
    async with session_factory.begin() as session:
        task = TaskRunModel(
            user_id=user_id,
            kind="mail_draft.generate",
            status=TaskStatus.RUNNING.value,
            lease_owner=lease_owner,
            idempotency_key="cancel-committed-result",
            input_payload={},
            result_payload={"generation_status": "succeeded"},
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    csrf = client.cookies.get("ai_employee_csrf") or ""
    response = await client.post(
        f"/api/v1/tasks/{task_id}/cancel",
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "task_state_conflict"
    async with session_factory() as session:
        persisted = await session.get(TaskRunModel, task_id)
        cancelled_events = tuple(
            (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.task_id == task_id,
                        AuditEventModel.event_type == "task.cancelled",
                    )
                )
            ).all()
        )
    assert persisted is not None
    assert persisted.status == TaskStatus.RUNNING.value
    assert persisted.lease_owner == lease_owner
    assert persisted.result_payload == {"generation_status": "succeeded"}
    assert cancelled_events == ()


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


@pytest.mark.parametrize("payload_hash", ("é" * 64, "g" * 64, "A" * 64))
async def test_approval_api_rejects_noncanonical_hash_before_use_case(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
    monkeypatch: pytest.MonkeyPatch,
    payload_hash: str,
) -> None:
    """审批请求只接受 64 位 lowercase hex，非法输入不得进入应用用例。"""
    client, _, _ = task_client
    calls: list[object] = []

    async def record_unexpected_call(self: object, **kwargs: object) -> None:
        """若 Pydantic 边界失效则记录调用，避免测试依赖真实审批是否存在。"""
        del self
        calls.append(kwargs)

    monkeypatch.setattr(ApprovalDecisionUseCase, "execute", record_unexpected_call)
    csrf = client.cookies.get("ai_employee_csrf") or ""
    response = await client.post(
        f"/api/v1/approvals/{UUID(int=0)}/decision",
        json={"decision": "approved", "version": 1, "payload_hash": payload_hash},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 422
    assert calls == []


@pytest.mark.asyncio
async def test_approval_api_preserves_invalidated_by_edit_error_code(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """真实审批 API 必须保留需要新版本才能恢复的稳定冲突码。"""
    client, session_factory, user_id = task_client
    async with session_factory.begin() as session:
        task = TaskRunModel(
            user_id=user_id,
            kind="trusted_action",
            status=TaskStatus.WAITING_APPROVAL.value,
            idempotency_key="invalidated-approval-api",
            input_payload={},
        )
        session.add(task)
        await session.flush()
        step = TaskStepModel(
            task_id=task.id,
            sequence=1,
            name="await_approval",
            kind="trusted_action",
            status="pending",
            input_summary={},
        )
        session.add(step)
        await session.flush()
        approval = ApprovalRequestModel(
            task_id=task.id,
            step_id=step.id,
            version=1,
            action="mail.send",
            payload={},
            payload_hash="a" * 64,
            preview_markdown="",
            status=ApprovalStatus.INVALIDATED.value,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        session.add(approval)
        await session.flush()
        approval_id = approval.id

    csrf = client.cookies.get("ai_employee_csrf") or ""
    response = await client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "approved", "version": 1, "payload_hash": "a" * 64},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "approval_invalidated_by_edit"


@pytest.mark.asyncio
async def test_cross_user_approval_is_hidden_by_real_api_and_repository(
    task_client: tuple[httpx.AsyncClient, ManagedAsyncSessionMaker, UUID],
) -> None:
    """真实路由与仓储组合不得泄漏其他用户审批的存在或特殊终态。"""
    client, session_factory, _ = task_client
    async with session_factory.begin() as session:
        other_user = UserModel(
            email="other-approval-api@example.test",
            display_name="Other Approval API User",
            password_hash=PasswordHasher().hash("synthetic-password"),
            timezone="UTC",
            locale="en-US",
            brief_time=time(8),
            is_active=True,
        )
        session.add(other_user)
        await session.flush()
        task = TaskRunModel(
            user_id=other_user.id,
            kind="trusted_action",
            status=TaskStatus.WAITING_APPROVAL.value,
            idempotency_key="foreign-invalidated-approval-api",
            input_payload={},
        )
        session.add(task)
        await session.flush()
        step = TaskStepModel(
            task_id=task.id,
            sequence=1,
            name="await_approval",
            kind="trusted_action",
            status="pending",
            input_summary={},
        )
        session.add(step)
        await session.flush()
        approval = ApprovalRequestModel(
            task_id=task.id,
            step_id=step.id,
            version=1,
            action="mail.send",
            payload={},
            payload_hash="b" * 64,
            preview_markdown="",
            status=ApprovalStatus.INVALIDATED.value,
            expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        )
        session.add(approval)
        await session.flush()
        approval_id = approval.id

    csrf = client.cookies.get("ai_employee_csrf") or ""
    response = await client.post(
        f"/api/v1/approvals/{approval_id}/decision",
        json={"decision": "approved", "version": 1, "payload_hash": "b" * 64},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "approval_conflict"
    async with session_factory() as session:
        persisted = await session.get(ApprovalRequestModel, approval_id)
    assert persisted is not None and persisted.status == ApprovalStatus.INVALIDATED.value
    assert persisted.decided_at is None
