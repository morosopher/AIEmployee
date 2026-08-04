"""协调每日简报结果的版本化持久化。"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select

from ai_employee.domain.briefs import DailyBriefContent
from ai_employee.infrastructure.db.models.briefs import DailyBriefItemModel, DailyBriefModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class PersistDailyBriefUseCase:
    """在一个事务中持久化简报、条目和可审计完成事件。"""
    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """注入受调用方拥有的数据库 session factory。"""
        self._session_factory = session_factory

    async def execute(self, *, user_id: UUID, task_id: UUID, content: DailyBriefContent, markdown: str) -> UUID:
        """写入下一版本的 complete/partial 简报和所有顺序条目。

        手动刷新与重复 worker 恢复由调用方传入同一个 task_id；同一任务再次执行会复用
        已写入简报，防止至少一次投递创建第二个版本。
        """
        async with self._session_factory.begin() as session:
            existing = await session.scalar(select(DailyBriefModel.id).where(DailyBriefModel.user_id == user_id, DailyBriefModel.task_id == task_id))
            if existing is not None:
                return existing
            current = await session.scalar(select(func.max(DailyBriefModel.version)).where(DailyBriefModel.user_id == user_id, DailyBriefModel.local_date == content.local_date))
            brief = DailyBriefModel(user_id=user_id, local_date=content.local_date, version=(current or 0) + 1, task_id=task_id, completeness=content.completeness, source_cutoff=content.source_cutoff, headline=content.headline, structured_content=content.model_dump(mode="json"), markdown=markdown, warnings=content.warnings, created_at=datetime.now(UTC))
            session.add(brief)
            await session.flush()
            session.add_all(DailyBriefItemModel(brief_id=brief.id, position=index, section=item.section.value, priority=item.priority.value, title=item.title, body_markdown=item.body_markdown, source_refs=[source.model_dump(mode="json") for source in item.source_refs], suggested_action_kind=item.suggested_action_kind) for index, item in enumerate(content.items))
            session.add(AuditEventModel(user_id=user_id, task_id=task_id, event_type="brief.ready", actor_type="system", actor_id=None, event_metadata={"brief_id": str(brief.id), "completeness": content.completeness}))
            return brief.id
