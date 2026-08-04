"""执行 M1 限定对话并持久化最终 assistant 消息。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select

from ai_employee.agents.daily_brief.nodes import (
    classify_ambiguous_conversation_intent,
    classify_conversation_intent,
)
from ai_employee.application.ports.model import ModelGateway
from ai_employee.application.use_cases.conversations import unsupported_response
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.config import Settings
from ai_employee.infrastructure.db.models.briefs import (
    DailyBriefModel,
    LLMInvocationModel,
    MessageModel,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.integrations.llm.fake import FakeModelGateway, build_model_gateway


class ConversationTaskStep:
    """只处理生成/查看简报，其他意图永远不调用工具或供应商。"""
    name = "conversation_respond"

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        *,
        model_gateway: ModelGateway | None = None,
        model_name: str = "fake",
        model_redaction_patterns: tuple[str, ...] = (),
    ) -> None:
        """保存数据库工厂以在短事务中读取并写入消息。"""
        self._session_factory = session_factory
        # 直接构造仅供单测与本地编排使用，必须保持无网络；生产 factory 显式注入配置网关。
        self._model_gateway = model_gateway or FakeModelGateway()
        self._model_name = model_name
        self._model_redaction_patterns = model_redaction_patterns

    async def execute(self, task: LeasedTask) -> None:
        """按 Task15 确定性意图规则写入一个最终 assistant 消息。"""
        if task.user_id is None:
            raise ValueError("conversation.respond requires user_id")
        raw_conversation_id = task.input_payload.get("conversation_id")
        raw_content = task.input_payload.get("content")
        if not isinstance(raw_conversation_id, str) or not isinstance(raw_content, str):
            raise TypeError("conversation.respond requires conversation_id and content")
        conversation_id = UUID(raw_conversation_id)
        # 至少一次重复投递先在数据库短路，绝不在已完成任务上再次调用模型。
        async with self._session_factory() as session:
            existing = await session.scalar(
                select(MessageModel.id).where(
                    MessageModel.user_id == task.user_id,
                    MessageModel.task_id == task.task_id,
                    MessageModel.role == "assistant",
                )
            )
        if existing is not None:
            return
        deterministic = classify_conversation_intent(raw_content)
        intent = deterministic["intent"]
        invocation_metadata: list[dict[str, Any]] = []
        if deterministic["reason_code"] == "ambiguous_request":
            intent = (
                await classify_ambiguous_conversation_intent(
                    raw_content,
                    model_gateway=self._model_gateway,
                    model_name=self._model_name,
                    configured_patterns=self._model_redaction_patterns,
                    invocation_metadata=invocation_metadata,
                )
            ).intent
        async with self._session_factory.begin() as session:
            existing = await session.scalar(select(MessageModel.id).where(MessageModel.user_id == task.user_id, MessageModel.task_id == task.task_id, MessageModel.role == "assistant"))
            if existing is not None:
                return
            if intent == "show_latest_brief":
                brief = await session.scalar(select(DailyBriefModel).where(DailyBriefModel.user_id == task.user_id).order_by(DailyBriefModel.local_date.desc(), DailyBriefModel.version.desc()))
                text = brief.markdown if brief is not None else "尚无可查看的每日简报。"
            elif intent == "generate_daily_brief":
                generated = await SqlAlchemyTaskRepository(session).create_with_outbox(
                    user_id=task.user_id,
                    kind="daily_brief",
                    input_payload={"schedule_kind": "conversation"},
                    idempotency_key=f"daily_brief:{task.user_id}:conversation:{task.task_id}",
                )
                text = f"已创建每日简报任务：`{generated.task_id}`。"
            else:
                text = unsupported_response()
            session.add_all(
                LLMInvocationModel(
                    user_id=task.user_id,
                    task_id=task.task_id,
                    step_id=None,
                    provider=str(metadata["provider"]),
                    model_name=str(metadata["model_name"]),
                    prompt_version=str(metadata["prompt_version"]),
                    input_hash=str(metadata["input_hash"]),
                    output_schema=str(metadata["output_schema"]),
                    input_tokens=int(metadata["input_tokens"]),
                    output_tokens=int(metadata["output_tokens"]),
                    estimated_cost_microusd=metadata["estimated_cost_microusd"],
                    latency_ms=int(metadata["latency_ms"]),
                    status=str(metadata["status"]),
                    error_code=metadata["error_code"],
                    created_at=datetime.now(UTC),
                )
                for metadata in invocation_metadata
            )
            session.add(MessageModel(user_id=task.user_id, conversation_id=conversation_id, role="assistant", content_markdown=text, task_id=task.task_id, created_at=datetime.now(UTC)))


def build_conversation_task_step(
    *, session_factory: ManagedAsyncSessionMaker, settings: Settings | None = None
) -> ConversationTaskStep:
    """构造供 DurableTaskRunner 注册的实际会话回复节点。"""
    if settings is None:
        return ConversationTaskStep(session_factory)
    return ConversationTaskStep(
        session_factory,
        model_gateway=build_model_gateway(settings),
        model_name=settings.model_name,
        model_redaction_patterns=tuple(settings.model_redaction_patterns),
    )
