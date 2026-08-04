"""执行 M1 限定对话并持久化最终 assistant 消息。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from ai_employee.agents.daily_brief.nodes import classify_conversation_intent
from ai_employee.application.use_cases.conversations import unsupported_response
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.infrastructure.db.models.briefs import DailyBriefModel, MessageModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class ConversationTaskStep:
    """只处理生成/查看简报，其他意图永远不调用工具或供应商。"""
    name = "conversation_respond"

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存数据库工厂以在短事务中读取并写入消息。"""
        self._session_factory = session_factory

    async def execute(self, task: LeasedTask) -> None:
        """按 Task15 确定性意图规则写入一个最终 assistant 消息。"""
        if task.user_id is None:
            raise ValueError("conversation.respond requires user_id")
        raw_conversation_id = task.input_payload.get("conversation_id")
        raw_content = task.input_payload.get("content")
        if not isinstance(raw_conversation_id, str) or not isinstance(raw_content, str):
            raise TypeError("conversation.respond requires conversation_id and content")
        conversation_id = UUID(raw_conversation_id)
        intent = classify_conversation_intent(raw_content)["intent"]
        async with self._session_factory.begin() as session:
            existing = await session.scalar(select(MessageModel.id).where(MessageModel.user_id == task.user_id, MessageModel.task_id == task.task_id, MessageModel.role == "assistant"))
            if existing is not None:
                return
            if intent == "show_latest_brief":
                brief = await session.scalar(select(DailyBriefModel).where(DailyBriefModel.user_id == task.user_id).order_by(DailyBriefModel.local_date.desc(), DailyBriefModel.version.desc()))
                text = brief.markdown if brief is not None else "尚无可查看的每日简报。"
            elif intent == "generate_daily_brief":
                text = "已创建每日简报任务，请在任务完成后查看结果。"
            else:
                text = unsupported_response()
            session.add(MessageModel(user_id=task.user_id, conversation_id=conversation_id, role="assistant", content_markdown=text, task_id=task.task_id, created_at=datetime.now(UTC)))


def build_conversation_task_step(*, session_factory: ManagedAsyncSessionMaker) -> ConversationTaskStep:
    """构造供 DurableTaskRunner 注册的实际会话回复节点。"""
    return ConversationTaskStep(session_factory)
