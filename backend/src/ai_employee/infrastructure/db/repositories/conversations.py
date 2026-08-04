"""提供用户隔离的对话读取和删除仓储。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.infrastructure.db.models.briefs import ConversationModel, MessageModel


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
