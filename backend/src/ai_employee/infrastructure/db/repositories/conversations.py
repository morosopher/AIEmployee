"""提供用户隔离的对话查询与原子消息创建适配器。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.conversations import (
    ConversationMessageStore,
    ConversationNotFoundError,
)
from ai_employee.application.use_cases.tasks import CreateTaskResult
from ai_employee.infrastructure.db.models.briefs import ConversationModel, MessageModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyConversationRepository:
    """所有读取和删除都带 user_id 条件，避免跨用户访问。"""
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list(self, *, user_id: UUID) -> tuple[ConversationModel, ...]:
        """按更新时间倒序列出当前用户会话。"""
        return tuple((await self._session.scalars(select(ConversationModel).where(ConversationModel.user_id == user_id).order_by(ConversationModel.updated_at.desc()))).all())

    async def get(self, *, user_id: UUID, conversation_id: UUID) -> ConversationModel | None:
        """取得当前用户会话或返回空。"""
        return await self._session.scalar(select(ConversationModel).where(ConversationModel.id == conversation_id, ConversationModel.user_id == user_id))

    async def messages(self, *, user_id: UUID, conversation_id: UUID) -> tuple[MessageModel, ...]:
        """列出会话消息，并通过消息 user_id 做二次隔离。"""
        return tuple((await self._session.scalars(select(MessageModel).where(MessageModel.conversation_id == conversation_id, MessageModel.user_id == user_id).order_by(MessageModel.created_at))).all())


class SqlAlchemyConversationMessageStore:
    """在同一事务内验证会话归属、创建用户消息、TaskRun 与 Outbox。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由 factory 提供的事务 Session，避免跨事务留下半条消息。"""
        self._session = session

    async def create_message_and_task(
        self,
        *,
        user_id: UUID,
        conversation_id: UUID,
        content_markdown: str,
        idempotency_key: str,
    ) -> CreateTaskResult:
        """用用户条件锁定会话，并以同键复用已有任务而不重复写消息。"""
        conversation = await self._session.scalar(
            select(ConversationModel)
            .where(
                ConversationModel.id == conversation_id,
                ConversationModel.user_id == user_id,
            )
            .with_for_update()
        )
        if conversation is None:
            raise ConversationNotFoundError
        existing = await self._session.scalar(
            select(TaskRunModel.id).where(
                TaskRunModel.user_id == user_id,
                TaskRunModel.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return CreateTaskResult(task_id=existing)
        task = await SqlAlchemyTaskRepository(self._session).create_with_outbox(
            user_id=user_id,
            kind="conversation.respond",
            input_payload={"conversation_id": str(conversation_id), "content": content_markdown},
            idempotency_key=idempotency_key,
        )
        conversation.updated_at = datetime.now(UTC)
        self._session.add(
            MessageModel(
                user_id=user_id,
                conversation_id=conversation_id,
                role="user",
                content_markdown=content_markdown,
                task_id=task.task_id,
                created_at=datetime.now(UTC),
            )
        )
        return task


class SqlAlchemyConversationMessageStoreFactory:
    """为每次对话消息创建提供自动提交或回滚的 SQLAlchemy 事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级 Session factory，不在构造阶段占用连接。"""
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[ConversationMessageStore]:
        """将消息、任务、审计和 Outbox 限制在同一个提交边界。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyConversationMessageStore(session)
