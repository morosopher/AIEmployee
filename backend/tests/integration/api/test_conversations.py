"""验证 M1 对话 API、幂等消息及 worker 意图边界。"""

from datetime import UTC, datetime
from uuid import UUID

import pytest
from conftest import AuthenticatedApiClients
from sqlalchemy import func, select

from ai_employee.application.use_cases.conversations import UNSUPPORTED_RESPONSE
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.infrastructure.db.models.briefs import DailyBriefModel, MessageModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.conversation import ConversationTaskStep


async def _create_conversation(clients: AuthenticatedApiClients) -> UUID:
    """经 CSRF 保护的真实 API 创建用户自己的空会话。"""
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    response = await clients.owner.post("/api/v1/conversations", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 201
    return UUID(response.json()["id"])


@pytest.mark.asyncio
async def test_conversation_writes_require_csrf_and_client_id_is_atomic(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """创建和消息写入拒绝缺失 CSRF，同一 client id 只留下一个消息和任务。"""
    clients = authenticated_api_clients
    denied = await clients.owner.post("/api/v1/conversations")
    assert denied.status_code == 403 and denied.json()["error_code"] == "csrf_rejected"
    conversation_id = await _create_conversation(clients)
    payload = {"content_markdown": "生成今天简报", "client_request_id": "atomic-request-1"}
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    first = await clients.owner.post(f"/api/v1/conversations/{conversation_id}/messages", json=payload, headers={"X-CSRF-Token": csrf})
    second = await clients.owner.post(f"/api/v1/conversations/{conversation_id}/messages", json=payload, headers={"X-CSRF-Token": csrf})
    assert first.status_code == 202 and second.json() == first.json()
    async with clients.session_factory() as session:
        message_count = await session.scalar(select(func.count()).select_from(MessageModel).where(MessageModel.conversation_id == conversation_id))
        task_count = await session.scalar(select(func.count()).select_from(TaskRunModel).where(TaskRunModel.user_id == clients.owner_id, TaskRunModel.idempotency_key == f"conversation:{clients.owner_id}:atomic-request-1"))
    assert message_count == 1
    assert task_count == 1


@pytest.mark.asyncio
async def test_worker_handles_supported_and_unsupported_intents_without_provider_or_tool(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """生成、读取最新简报与范围外请求均走确定性 worker，不引入供应商或写工具。"""
    clients = authenticated_api_clients
    conversation_id = await _create_conversation(clients)
    async with clients.session_factory.begin() as session:
        tasks = []
        for index, content in enumerate(("生成今日简报", "查看今日简报", "帮我发送邮件")):
            task = TaskRunModel(user_id=clients.owner_id, kind="conversation.respond", status="running", idempotency_key=f"conversation-worker-{index}", input_payload={"conversation_id": str(conversation_id), "content": content})
            tasks.append(task)
            session.add(task)
        await session.flush()
        latest_task = TaskRunModel(user_id=clients.owner_id, kind="daily_brief", status="succeeded", idempotency_key="latest-brief-task", input_payload={})
        session.add(latest_task)
        await session.flush()
        session.add(DailyBriefModel(user_id=clients.owner_id, local_date=datetime(2026, 8, 4, tzinfo=UTC).date(), version=1, task_id=latest_task.id, completeness="complete", source_cutoff=datetime.now(UTC), headline="Latest", structured_content={}, markdown="# Latest synthetic brief", warnings=[], created_at=datetime.now(UTC)))
    step = ConversationTaskStep(clients.session_factory)
    for task in tasks:
        await step.execute(LeasedTask(task_id=task.id, user_id=clients.owner_id, kind=task.kind, input_payload=task.input_payload, started_at=datetime.now(UTC)))
    async with clients.session_factory() as session:
        replies = (await session.scalars(select(MessageModel).where(MessageModel.conversation_id == conversation_id, MessageModel.role == "assistant").order_by(MessageModel.created_at))).all()
        generated_count = await session.scalar(select(func.count()).select_from(TaskRunModel).where(TaskRunModel.kind == "daily_brief"))
    assert len(replies) == 3
    assert replies[0].content_markdown.startswith("已创建每日简报任务：")
    assert replies[1].content_markdown == "# Latest synthetic brief"
    assert replies[2].content_markdown == UNSUPPORTED_RESPONSE
    assert generated_count == 2


@pytest.mark.asyncio
async def test_replayed_ambiguous_conversation_skips_model_and_duplicate_reply(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """同一会话任务重放必须在网关调用前短路，且只保留一条 assistant 回复。"""
    clients = authenticated_api_clients
    conversation_id = await _create_conversation(clients)
    async with clients.session_factory.begin() as session:
        task = TaskRunModel(
            user_id=clients.owner_id,
            kind="conversation.respond",
            status="running",
            idempotency_key="conversation-ambiguous-replay",
            input_payload={"conversation_id": str(conversation_id), "content": "what should I focus on"},
        )
        session.add(task)
        await session.flush()
    gateway = FakeModelGateway()
    step = ConversationTaskStep(clients.session_factory, model_gateway=gateway)
    leased = LeasedTask(task_id=task.id, user_id=clients.owner_id, kind=task.kind, input_payload=task.input_payload, started_at=datetime.now(UTC))
    await step.execute(leased)
    calls_after_first = len(gateway.calls)
    await step.execute(leased)
    async with clients.session_factory() as session:
        replies = (await session.scalars(select(MessageModel).where(MessageModel.task_id == task.id, MessageModel.role == "assistant"))).all()
    assert calls_after_first == 1
    assert len(gateway.calls) == calls_after_first
    assert len(replies) == 1


@pytest.mark.asyncio
async def test_delete_is_owned_audited_without_content_and_foreign_resources_are_hidden(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """删除仅允许所有者，审计只存会话 ID，其他用户不能读取或删除。"""
    clients = authenticated_api_clients
    conversation_id = await _create_conversation(clients)
    csrf = clients.owner.cookies.get("ai_employee_csrf") or ""
    foreign_get = await clients.other.get(f"/api/v1/conversations/{conversation_id}")
    foreign_delete = await clients.other.delete(f"/api/v1/conversations/{conversation_id}", headers={"X-CSRF-Token": clients.other.cookies.get("ai_employee_csrf") or ""})
    deleted = await clients.owner.delete(f"/api/v1/conversations/{conversation_id}", headers={"X-CSRF-Token": csrf})
    assert foreign_get.status_code == 404 and foreign_delete.status_code == 404
    assert deleted.status_code == 204
    async with clients.session_factory() as session:
        event = await session.scalar(select(AuditEventModel).where(AuditEventModel.event_type == "conversation.deleted"))
    assert event is not None and event.event_metadata == {"conversation_id": str(conversation_id)}
