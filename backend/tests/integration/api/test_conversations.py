"""验证 M1 对话 API、幂等消息及 worker 意图边界。"""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TypeVar
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from ai_employee.application.ports.model import ModelResponse, ModelUsage
from ai_employee.application.use_cases.conversations import UNSUPPORTED_RESPONSE
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.domain.briefs import ConversationIntent
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.infrastructure.db.models.actions import MailDraftModel, MailDraftVersionModel
from ai_employee.infrastructure.db.models.briefs import DailyBriefModel, MessageModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EmailMessageModel,
    EmailThreadModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.integrations.llm.fake import FakeModelGateway
from ai_employee.workers.conversation import ConversationTaskStep

from .conftest import AuthenticatedApiClients

TModel = TypeVar("TModel", bound=BaseModel)


class _PrepareMailDraftIntentGateway:
    """模拟模型错误选择草稿意图，验证 Worker 仍重新执行确定性解析。"""

    def __init__(self) -> None:
        self.calls: list[Sequence[dict[str, str]]] = []

    async def complete(
        self,
        *,
        model_name: str,
        prompt_version: str,
        messages: Sequence[dict[str, str]],
        response_model: type[TModel],
    ) -> ModelResponse[TModel]:
        """返回合法但不可信的 ``prepare_mail_draft`` 结构化分类。"""
        del model_name, prompt_version
        self.calls.append(messages)
        value = response_model.model_validate(
            ConversationIntent(
                intent="prepare_mail_draft",
                confidence=1,
                reason_code="synthetic_model_choice",
            ).model_dump()
        )
        return ModelResponse(value=value, usage=ModelUsage())


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
async def test_explicit_mail_draft_request_creates_only_editable_local_draft_and_link(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """明确草拟请求创建本地版本与链接，但绝不创建审批、工具执行或供应商调用。"""
    clients = authenticated_api_clients
    conversation_id = await _create_conversation(clients)
    async with clients.session_factory.begin() as session:
        connection = OAuthConnectionModel(
            user_id=clients.owner_id,
            provider="google",
            provider_account_id="synthetic-conversation-mail",
            account_email="owner@example.test",
            scopes=[],
            status="connected",
        )
        session.add(connection)
        await session.flush()
        session.add_all(
            ConnectionCapabilityModel(
                user_id=clients.owner_id,
                connection_id=connection.id,
                capability=capability.value,
                status="enabled",
                actual_scopes=[],
            )
            for capability in (
                ConnectionCapability.MAIL_READ,
                ConnectionCapability.MAIL_SEND,
            )
        )
        owner = await session.get(UserModel, clients.owner_id)
        assert owner is not None
        owner.default_mail_connection_id = connection.id
        task = TaskRunModel(
            user_id=clients.owner_id,
            kind="conversation.respond",
            status="running",
            idempotency_key="conversation-explicit-mail-draft",
            input_payload={
                "conversation_id": str(conversation_id),
                "content": 'Please prepare an email to "quoted local"@Example.Test',
            },
        )
        session.add(task)
        await session.flush()
    gateway = FakeModelGateway()
    step = ConversationTaskStep(
        clients.session_factory,
        model_gateway=gateway,
        action_cipher=ActionPayloadCipher.from_key(b"c" * 32),
        clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
    )

    await step.execute(
        LeasedTask(
            task_id=task.id,
            user_id=clients.owner_id,
            kind=task.kind,
            input_payload=task.input_payload,
            started_at=datetime.now(UTC),
        )
    )
    # Taskiq 至少一次重复投递不得创建第二封草稿或第二条 assistant 回复。
    await step.execute(
        LeasedTask(
            task_id=task.id,
            user_id=clients.owner_id,
            kind=task.kind,
            input_payload=task.input_payload,
            started_at=datetime.now(UTC),
        )
    )

    async with clients.session_factory() as session:
        draft = await session.scalar(
            select(MailDraftModel).where(MailDraftModel.user_id == clients.owner_id)
        )
        version = await session.scalar(
            select(MailDraftVersionModel).where(
                MailDraftVersionModel.user_id == clients.owner_id
            )
        )
        reply = await session.scalar(
            select(MessageModel).where(
                MessageModel.user_id == clients.owner_id,
                MessageModel.task_id == task.id,
                MessageModel.role == "assistant",
            )
        )
        approval_count = await session.scalar(
            select(func.count()).select_from(ApprovalRequestModel)
        )
        execution_count = await session.scalar(
            select(func.count()).select_from(ToolExecutionModel)
        )
    assert draft is not None and draft.status == "editing" and draft.current_version == 1
    assert version is not None and version.to_recipients == [
        {"address": '"quoted local"@example.test'}
    ]
    assert reply is not None
    assert f"/api/v1/mail/drafts/{draft.id}" in reply.content_markdown
    assert "不会发送" in reply.content_markdown
    assert approval_count == 0 and execution_count == 0
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_template",
    (
        "prepare an email reply to thread {thread_id}",
        "请准备对线程 {thread_id} 的邮件回复",
    ),
)
async def test_explicit_local_thread_reply_creates_bound_draft_once_without_write_facts(
    authenticated_api_clients: AuthenticatedApiClients,
    command_template: str,
) -> None:
    """明确本地线程 UUID 回复命令必须绑定最新消息和原连接，重放仍只有一封草稿。"""
    clients = authenticated_api_clients
    conversation_id = await _create_conversation(clients)
    # 本地 PostgreSQL UUID 并不限定版本；使用规范 UUIDv7 防止命令正则意外只接受 v1-v5。
    thread_id = UUID("00000000-0000-7000-8000-000000000701")
    message_id = uuid4()
    async with clients.session_factory.begin() as session:
        connection = OAuthConnectionModel(
            user_id=clients.owner_id,
            provider="google",
            provider_account_id="synthetic-conversation-reply",
            account_email="owner@example.test",
            scopes=[],
            status="connected",
        )
        session.add(connection)
        await session.flush()
        session.add_all(
            ConnectionCapabilityModel(
                user_id=clients.owner_id,
                connection_id=connection.id,
                capability=capability.value,
                status="enabled",
                actual_scopes=[],
            )
            for capability in (
                ConnectionCapability.MAIL_READ,
                ConnectionCapability.MAIL_SEND,
            )
        )
        owner = await session.get(UserModel, clients.owner_id)
        assert owner is not None
        owner.default_mail_connection_id = connection.id
        await session.flush()
        session.add(
            EmailThreadModel(
                id=thread_id,
                user_id=clients.owner_id,
                connection_id=connection.id,
                provider_thread_id="provider-conversation-thread",
                subject="Synthetic reply subject",
                participants=[],
                latest_message_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
                provider_url="https://provider.example.test/thread/reply",
            )
        )
        await session.flush()
        session.add(
            EmailMessageModel(
                id=message_id,
                user_id=clients.owner_id,
                connection_id=connection.id,
                thread_id=thread_id,
                provider_message_id="provider-conversation-message",
                received_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
                mailbox_scope_key="mailbox",
                sender={"email": "peer@example.test"},
                recipients=[{"email": "owner@example.test"}],
                subject="Synthetic reply subject",
                snippet="Synthetic reply snippet",
                labels=["INBOX"],
                headers={},
                provider_url="https://provider.example.test/message/reply",
            )
        )
        task = TaskRunModel(
            user_id=clients.owner_id,
            kind="conversation.respond",
            status="running",
            idempotency_key="conversation-explicit-thread-reply",
            input_payload={
                "conversation_id": str(conversation_id),
                "content": command_template.format(thread_id=thread_id),
            },
        )
        session.add(task)
        await session.flush()

    gateway = FakeModelGateway()
    step = ConversationTaskStep(
        clients.session_factory,
        model_gateway=gateway,
        action_cipher=ActionPayloadCipher.from_key(b"r" * 32),
        clock=lambda: datetime(2026, 8, 9, tzinfo=UTC),
    )
    leased = LeasedTask(
        task_id=task.id,
        user_id=clients.owner_id,
        kind=task.kind,
        input_payload=task.input_payload,
        started_at=datetime.now(UTC),
    )

    await step.execute(leased)
    await step.execute(leased)

    async with clients.session_factory() as session:
        drafts = tuple(
            (
                await session.scalars(
                    select(MailDraftModel).where(
                        MailDraftModel.user_id == clients.owner_id
                    )
                )
            ).all()
        )
        version = await session.scalar(
            select(MailDraftVersionModel).where(
                MailDraftVersionModel.user_id == clients.owner_id
            )
        )
        reply_count = await session.scalar(
            select(func.count()).select_from(MessageModel).where(
                MessageModel.user_id == clients.owner_id,
                MessageModel.task_id == task.id,
                MessageModel.role == "assistant",
            )
        )
        approval_count = await session.scalar(
            select(func.count()).select_from(ApprovalRequestModel)
        )
        execution_count = await session.scalar(
            select(func.count()).select_from(ToolExecutionModel)
        )
    assert len(drafts) == 1
    assert drafts[0].mode == "reply"
    assert drafts[0].connection_id == connection.id
    assert drafts[0].source_thread_id == "provider-conversation-thread"
    assert drafts[0].source_message_id == "provider-conversation-message"
    assert version is not None
    assert version.subject == "Re: Synthetic reply subject"
    assert version.to_recipients == [{"address": "peer@example.test"}]
    assert reply_count == 1
    assert approval_count == 0 and execution_count == 0
    assert gateway.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    (
        "Can you explain how to prepare an email?",
        "Do not prepare an email.",
        "如何准备邮件？",
        "不要创建邮件草稿",
    ),
)
async def test_mail_questions_and_negations_never_create_drafts(
    authenticated_api_clients: AuthenticatedApiClients,
    content: str,
) -> None:
    """能力询问、说明性文本和中英文否定句都必须 fail closed 为边界说明。"""
    clients = authenticated_api_clients
    conversation_id = await _create_conversation(clients)
    async with clients.session_factory.begin() as session:
        task = TaskRunModel(
            user_id=clients.owner_id,
            kind="conversation.respond",
            status="running",
            idempotency_key=f"conversation-negative-mail-{uuid4()}",
            input_payload={
                "conversation_id": str(conversation_id),
                "content": content,
            },
        )
        session.add(task)
        await session.flush()
    gateway = FakeModelGateway()
    step = ConversationTaskStep(
        clients.session_factory,
        model_gateway=gateway,
        action_cipher=ActionPayloadCipher.from_key(b"n" * 32),
    )

    await step.execute(
        LeasedTask(
            task_id=task.id,
            user_id=clients.owner_id,
            kind=task.kind,
            input_payload=task.input_payload,
            started_at=datetime.now(UTC),
        )
    )

    async with clients.session_factory() as session:
        draft_count = await session.scalar(
            select(func.count()).select_from(MailDraftModel).where(
                MailDraftModel.user_id == clients.owner_id
            )
        )
        reply = await session.scalar(
            select(MessageModel).where(
                MessageModel.user_id == clients.owner_id,
                MessageModel.task_id == task.id,
                MessageModel.role == "assistant",
            )
        )
    assert draft_count == 0
    assert reply is not None and reply.content_markdown == UNSUPPORTED_RESPONSE


@pytest.mark.asyncio
async def test_model_selected_mail_draft_intent_is_rejected_without_explicit_command(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """模型只能分类歧义文本，不能凭结构化输出选择收件人、账户或线程并创建动作。"""
    clients = authenticated_api_clients
    conversation_id = await _create_conversation(clients)
    async with clients.session_factory.begin() as session:
        task = TaskRunModel(
            user_id=clients.owner_id,
            kind="conversation.respond",
            status="running",
            idempotency_key="conversation-model-cannot-create-draft",
            input_payload={
                "conversation_id": str(conversation_id),
                "content": "Could you outline what this assistant can do?",
            },
        )
        session.add(task)
        await session.flush()
    gateway = _PrepareMailDraftIntentGateway()
    step = ConversationTaskStep(clients.session_factory, model_gateway=gateway)

    await step.execute(
        LeasedTask(
            task_id=task.id,
            user_id=clients.owner_id,
            kind=task.kind,
            input_payload=task.input_payload,
            started_at=datetime.now(UTC),
        )
    )

    async with clients.session_factory() as session:
        draft_count = await session.scalar(
            select(func.count()).select_from(MailDraftModel).where(
                MailDraftModel.user_id == clients.owner_id
            )
        )
        reply = await session.scalar(
            select(MessageModel).where(
                MessageModel.user_id == clients.owner_id,
                MessageModel.task_id == task.id,
                MessageModel.role == "assistant",
            )
        )
    assert len(gateway.calls) == 1
    assert draft_count == 0
    assert reply is not None and reply.content_markdown == UNSUPPORTED_RESPONSE


@pytest.mark.asyncio
async def test_ambiguous_mail_chat_explains_capabilities_without_creating_action(
    authenticated_api_clients: AuthenticatedApiClients,
) -> None:
    """未明确要求草稿的邮件闲聊继续解释边界，不创建任何本地或真实动作。"""
    clients = authenticated_api_clients
    conversation_id = await _create_conversation(clients)
    async with clients.session_factory.begin() as session:
        task = TaskRunModel(
            user_id=clients.owner_id,
            kind="conversation.respond",
            status="running",
            idempotency_key="conversation-ambiguous-mail-help",
            input_payload={
                "conversation_id": str(conversation_id),
                "content": "Can you help me with email?",
            },
        )
        session.add(task)
        await session.flush()
    gateway = FakeModelGateway()
    step = ConversationTaskStep(clients.session_factory, model_gateway=gateway)

    await step.execute(
        LeasedTask(
            task_id=task.id,
            user_id=clients.owner_id,
            kind=task.kind,
            input_payload=task.input_payload,
            started_at=datetime.now(UTC),
        )
    )

    async with clients.session_factory() as session:
        reply = await session.scalar(
            select(MessageModel).where(
                MessageModel.user_id == clients.owner_id,
                MessageModel.task_id == task.id,
                MessageModel.role == "assistant",
            )
        )
        draft_count = await session.scalar(select(func.count()).select_from(MailDraftModel))
        approval_count = await session.scalar(
            select(func.count()).select_from(ApprovalRequestModel)
        )
        execution_count = await session.scalar(
            select(func.count()).select_from(ToolExecutionModel)
        )
    assert reply is not None and reply.content_markdown == UNSUPPORTED_RESPONSE
    assert draft_count == 0 and approval_count == 0 and execution_count == 0
    assert gateway.calls == []


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
