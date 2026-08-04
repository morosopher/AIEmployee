"""提供用户设置的事务性 SQLAlchemy 适配器。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.infrastructure.db.models.identity import UserModel


class SqlAlchemySettingsRepository:
    """在已开启事务的会话内锁定用户设置。"""
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_update(self, *, user_id: UUID) -> UserModel | None:
        """锁定指定用户，保证 PATCH 与审计同事务提交。"""
        return await self._session.scalar(select(UserModel).where(UserModel.id == user_id).with_for_update())
