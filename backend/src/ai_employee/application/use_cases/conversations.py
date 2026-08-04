"""定义 M1 对话意图与原子消息创建用例。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from ai_employee.application.use_cases.tasks import CreateTaskResult
from ai_employee.infrastructure.db.models.briefs import ConversationModel, MessageModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

UNSUPPORTED_RESPONSE = "当前 M1 仅支持生成或查看每日简报，不会执行外部写操作或通用规划。"


def unsupported_response() -> str:
    """返回不调用模型、工具或外部提供商的固定边界回复。"""
    return UNSUPPORTED_RESPONSE


class ConversationNotFoundError(Exception):
    """表示用户范围内不存在所请求会话。"""


class CreateConversationMessageUseCase:
    """同一事务写入用户消息和 ``conversation.respond`` 任务。"""
    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """注入事务工厂；后续投递由路由复用既有 task dispatcher。"""
        self._session_factory = session_factory

    async def execute(self, *, user_id: UUID, conversation_id: UUID, content_markdown: str, client_request_id: str) -> CreateTaskResult:
        """用稳定客户端键幂等创建消息与 TaskRun，绝不跨事务留下半条消息。"""
        idempotency_key = f"conversation:{user_id}:{client_request_id}"
        async with self._session_factory.begin() as session:
            conversation = await session.scalar(select(ConversationModel).where(ConversationModel.id == conversation_id, ConversationModel.user_id == user_id).with_for_update())
            if conversation is None:
                raise ConversationNotFoundError
            existing = await session.scalar(select(TaskRunModel.id).where(TaskRunModel.user_id == user_id, TaskRunModel.idempotency_key == idempotency_key))
            if existing is not None:
                return CreateTaskResult(task_id=existing)
            task = await SqlAlchemyTaskRepository(session).create_with_outbox(user_id=user_id, kind="conversation.respond", input_payload={"conversation_id": str(conversation_id), "content": content_markdown}, idempotency_key=idempotency_key)
            conversation.updated_at = datetime.now(UTC)
            session.add(MessageModel(user_id=user_id, conversation_id=conversation_id, role="user", content_markdown=content_markdown, task_id=task.task_id, created_at=datetime.now(UTC)))
            return task
