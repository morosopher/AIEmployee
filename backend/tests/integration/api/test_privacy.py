"""验证隐私删除 API 只创建受保护、可重放且用户隔离的耐久任务。"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.tasks import (
    SqlAlchemyTaskRepositoryFactory,
)

if TYPE_CHECKING:
    # pytest 会按目录发现 fixture；运行时导入顶层 conftest 会在多目录收集时产生歧义。
    from conftest import AuthenticatedApiClients


class RecordingTaskDispatcher:
    """替代 Redis 队列记录 API 提交，隔离路由与真实投递基础设施。

    API 的职责是提交持久事实并请求投递；本替身返回与正常 dispatcher 相同的 ``queued``
    状态，使本测试可以验证真实用例和 PostgreSQL 事务，而无需连接 Redis。
    """

    def __init__(self) -> None:
        """初始化仅保存任务标识的合成投递记录。"""
        self.dispatched_task_ids: list[UUID] = []

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """记录提交后的投递请求并模拟已排队结果。"""
        self.dispatched_task_ids.append(task_id)
        return TaskStatus.QUEUED


def _install_recording_task_creator(
    clients: AuthenticatedApiClients,
) -> RecordingTaskDispatcher:
    """以真实 Repository 和合成队列替换应用组合根中的任务创建用例。

    Args:
        clients: 已登录的 API 客户端及共享 ASGI 应用测试上下文。

    Returns:
        用于断言路由确实请求投递的记录器。
    """
    transport = cast(httpx.ASGITransport, clients.owner._transport)
    app = cast(FastAPI, transport.app)
    dispatcher = RecordingTaskDispatcher()
    app.state.create_task_use_case = CreateTaskUseCase(
        SqlAlchemyTaskRepositoryFactory(clients.session_factory), dispatcher
    )
    return dispatcher


def _csrf_headers(client: httpx.AsyncClient, idempotency_key: str) -> dict[str, str]:
    """返回满足修改请求防护边界的合成请求头。"""
    return {
        "X-CSRF-Token": client.cookies.get("ai_employee_csrf") or "",
        "Idempotency-Key": idempotency_key,
    }


@pytest.mark.asyncio
async def test_source_cache_deletion_requires_authenticated_csrf_idempotent_request(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """来源缓存删除拒绝未认证或缺失 CSRF，并按用户范围复用请求键。"""
    clients = authenticated_api_clients
    dispatcher = _install_recording_task_creator(clients)
    transport = cast(httpx.ASGITransport, clients.owner._transport)

    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as anonymous:
        unauthenticated = await anonymous.post(
            "/api/v1/privacy/source-cache-deletions",
            headers={"Idempotency-Key": "privacy-source-request"},
        )
    csrf_rejected = await clients.owner.post(
        "/api/v1/privacy/source-cache-deletions",
        headers={"Idempotency-Key": "privacy-source-request"},
    )
    missing_key = await clients.owner.post(
        "/api/v1/privacy/source-cache-deletions",
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
    )
    owner_first = await clients.owner.post(
        "/api/v1/privacy/source-cache-deletions",
        headers=_csrf_headers(clients.owner, "privacy-source-request"),
    )
    owner_retry = await clients.owner.post(
        "/api/v1/privacy/source-cache-deletions",
        headers=_csrf_headers(clients.owner, "privacy-source-request"),
    )
    other_user = await clients.other.post(
        "/api/v1/privacy/source-cache-deletions",
        headers=_csrf_headers(clients.other, "privacy-source-request"),
    )

    assert unauthenticated.status_code == 401
    assert csrf_rejected.status_code == 403
    assert csrf_rejected.json()["error_code"] == "csrf_rejected"
    assert missing_key.status_code == 422
    assert missing_key.json()["error_code"] == "idempotency_key_required"
    assert owner_first.status_code == 202
    assert owner_first.json()["status"] == "queued"
    assert owner_retry.json()["task_id"] == owner_first.json()["task_id"]
    assert other_user.status_code == 202
    assert other_user.json()["task_id"] != owner_first.json()["task_id"]

    owner_task_id = UUID(owner_first.json()["task_id"])
    other_task_id = UUID(other_user.json()["task_id"])
    async with clients.session_factory() as session:
        tasks = (
            await session.scalars(
                select(TaskRunModel)
                .where(TaskRunModel.id.in_((owner_task_id, other_task_id)))
                .order_by(TaskRunModel.user_id)
            )
        ).all()

    assert {(task.user_id, task.kind) for task in tasks} == {
        (clients.owner_id, "privacy.clear_source_cache"),
        (clients.other_id, "privacy.clear_source_cache"),
    }
    assert all(task.input_payload == {} for task in tasks)
    assert dispatcher.dispatched_task_ids.count(owner_task_id) == 2
    assert dispatcher.dispatched_task_ids.count(other_task_id) == 1


@pytest.mark.asyncio
async def test_all_data_deletion_requires_exact_confirmation_and_creates_queued_task(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """全数据删除只接受精确确认短语，并且不把确认文本写入任务输入。"""
    clients = authenticated_api_clients
    dispatcher = _install_recording_task_creator(clients)
    headers = _csrf_headers(clients.owner, "privacy-all-data-request")
    transport = cast(httpx.ASGITransport, clients.owner._transport)

    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as anonymous:
        unauthenticated = await anonymous.post(
            "/api/v1/privacy/all-data-deletions",
            headers={"Idempotency-Key": "privacy-all-data-request"},
            json={"confirmation": "DELETE ALL DATA"},
        )
    csrf_rejected = await clients.owner.post(
        "/api/v1/privacy/all-data-deletions",
        headers={"Idempotency-Key": "privacy-all-data-request"},
        json={"confirmation": "DELETE ALL DATA"},
    )
    missing_key = await clients.owner.post(
        "/api/v1/privacy/all-data-deletions",
        headers={"X-CSRF-Token": clients.owner.cookies.get("ai_employee_csrf") or ""},
        json={"confirmation": "DELETE ALL DATA"},
    )

    invalid = await clients.owner.post(
        "/api/v1/privacy/all-data-deletions",
        headers=headers,
        json={"confirmation": "DELETE ALL DATA "},
    )
    accepted = await clients.owner.post(
        "/api/v1/privacy/all-data-deletions",
        headers=headers,
        json={"confirmation": "DELETE ALL DATA"},
    )
    retry = await clients.owner.post(
        "/api/v1/privacy/all-data-deletions",
        headers=headers,
        json={"confirmation": "DELETE ALL DATA"},
    )
    other_user = await clients.other.post(
        "/api/v1/privacy/all-data-deletions",
        headers=_csrf_headers(clients.other, "privacy-all-data-request"),
        json={"confirmation": "DELETE ALL DATA"},
    )

    assert unauthenticated.status_code == 401
    assert csrf_rejected.status_code == 403
    assert csrf_rejected.json()["error_code"] == "csrf_rejected"
    assert missing_key.status_code == 422
    assert missing_key.json()["error_code"] == "idempotency_key_required"
    assert invalid.status_code == 422
    assert invalid.json()["error_code"] == "deletion_confirmation_invalid"
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "queued"
    assert retry.json()["task_id"] == accepted.json()["task_id"]
    assert other_user.status_code == 202
    assert other_user.json()["task_id"] != accepted.json()["task_id"]

    task_id = UUID(accepted.json()["task_id"])
    other_task_id = UUID(other_user.json()["task_id"])
    async with clients.session_factory() as session:
        task = await session.scalar(
            select(TaskRunModel).where(
                TaskRunModel.id == task_id,
                TaskRunModel.user_id == clients.owner_id,
            )
        )
        other_task = await session.scalar(
            select(TaskRunModel).where(
                TaskRunModel.id == other_task_id,
                TaskRunModel.user_id == clients.other_id,
            )
        )

    assert task is not None
    assert other_task is not None
    assert task.kind == "privacy.delete_all_data"
    assert set(task.input_payload) == {"deletion_request_id"}
    assert isinstance(task.input_payload["deletion_request_id"], str)
    assert (
        task.input_payload["deletion_request_id"] != other_task.input_payload["deletion_request_id"]
    )
    assert "confirmation" not in task.input_payload
    assert dispatcher.dispatched_task_ids.count(task_id) == 2
    assert dispatcher.dispatched_task_ids.count(other_task_id) == 1
