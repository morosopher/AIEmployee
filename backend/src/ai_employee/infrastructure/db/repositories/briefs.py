"""提供用户隔离的简报读写仓储。"""

from datetime import date
from uuid import UUID

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.infrastructure.db.models.briefs import DailyBriefItemModel, DailyBriefModel


class SqlAlchemyBriefRepository:
    """在调用方事务内查询和写入简报，不自行提交。"""
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest(self, *, user_id: UUID, local_date: date | None = None) -> DailyBriefModel | None:
        """返回用户范围内最新 complete/partial 版本。"""
        statement = select(DailyBriefModel).where(DailyBriefModel.user_id == user_id)
        if local_date is not None:
            statement = statement.where(DailyBriefModel.local_date == local_date)
        return await self._session.scalar(statement.order_by(desc(DailyBriefModel.local_date), desc(DailyBriefModel.version)))

    async def list_for_date(self, *, user_id: UUID, local_date: date) -> tuple[DailyBriefModel, ...]:
        """按版本倒序返回指定本地日期的所有可见简报。"""
        return tuple((await self._session.scalars(select(DailyBriefModel).where(DailyBriefModel.user_id == user_id, DailyBriefModel.local_date == local_date).order_by(desc(DailyBriefModel.version)))).all())

    async def get(self, *, user_id: UUID, brief_id: UUID) -> DailyBriefModel | None:
        """按用户条件取得单个简报，避免跨用户资源探测。"""
        return await self._session.scalar(select(DailyBriefModel).where(DailyBriefModel.id == brief_id, DailyBriefModel.user_id == user_id))

    async def items(self, *, brief_id: UUID) -> tuple[DailyBriefItemModel, ...]:
        """按持久 position 返回条目。"""
        return tuple((await self._session.scalars(select(DailyBriefItemModel).where(DailyBriefItemModel.brief_id == brief_id).order_by(DailyBriefItemModel.position))).all())
