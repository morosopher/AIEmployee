"""提供用户隔离的简报查询与版本化写入适配器。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import desc, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.briefs import DailyBriefPersistenceStore
from ai_employee.domain.briefs import DailyBriefContent
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.briefs import (
    DailyBriefItemModel,
    DailyBriefModel,
    LLMInvocationModel,
)
from ai_employee.infrastructure.db.models.sources import EmailAnalysisModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.identity import lock_active_user
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyBriefRepository:
    """在调用方事务内查询和写入简报，不自行提交。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def latest(
        self, *, user_id: UUID, local_date: date | None = None
    ) -> DailyBriefModel | None:
        """返回用户范围内最新 complete/partial 版本。"""
        statement = select(DailyBriefModel).where(DailyBriefModel.user_id == user_id)
        if local_date is not None:
            statement = statement.where(DailyBriefModel.local_date == local_date)
        return await self._session.scalar(
            statement.order_by(desc(DailyBriefModel.local_date), desc(DailyBriefModel.version))
        )

    async def list_for_date(
        self, *, user_id: UUID, local_date: date
    ) -> tuple[DailyBriefModel, ...]:
        """按版本倒序返回指定本地日期的所有可见简报。"""
        return tuple(
            (
                await self._session.scalars(
                    select(DailyBriefModel)
                    .where(
                        DailyBriefModel.user_id == user_id, DailyBriefModel.local_date == local_date
                    )
                    .order_by(desc(DailyBriefModel.version))
                )
            ).all()
        )

    async def get(self, *, user_id: UUID, brief_id: UUID) -> DailyBriefModel | None:
        """按用户条件取得单个简报，避免跨用户资源探测。"""
        return await self._session.scalar(
            select(DailyBriefModel).where(
                DailyBriefModel.id == brief_id, DailyBriefModel.user_id == user_id
            )
        )

    async def items(self, *, brief_id: UUID) -> tuple[DailyBriefItemModel, ...]:
        """按持久 position 返回条目。"""
        return tuple(
            (
                await self._session.scalars(
                    select(DailyBriefItemModel)
                    .where(DailyBriefItemModel.brief_id == brief_id)
                    .order_by(DailyBriefItemModel.position)
                )
            ).all()
        )


class SqlAlchemyDailyBriefPersistenceStore:
    """在单一 PostgreSQL 事务中保存版本化简报及其所有可审计附属事实。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由 factory 管理生命周期且不在本类提交的异步 Session。"""
        self._session = session

    async def persist(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        content: DailyBriefContent,
        markdown: str,
        email_analyses: tuple[dict[str, Any], ...],
        model_invocations: tuple[dict[str, Any], ...],
    ) -> UUID:
        """原子写入简报版本、来源判断、模型审计、任务结果与完成事件。

        PostgreSQL advisory lock 只串行化同一用户同一业务日期的版本分配；唯一约束仍是
        最终防线。Graph 的最后 checkpoint 与这里是独立事务，因此先按 Task→user 重检
        active，再取得日期 advisory；屏障后不能落正文、判断、模型失败元数据或审计。
        重复 ``task_id`` 返回既有结果，因此 Worker 接管不会生成新版本。
        """
        task = await self._session.scalar(
            select(TaskRunModel)
            .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
            .with_for_update()
        )
        if task is None or not await lock_active_user(self._session, user_id=user_id):
            raise StateConflictError(
                error_code="task_state_conflict",
                message="task no longer accepts brief results",
            )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:brief_key))"),
            {"brief_key": f"daily-brief:{user_id}:{content.local_date.isoformat()}"},
        )
        existing = await self._session.scalar(
            select(DailyBriefModel.id).where(
                DailyBriefModel.user_id == user_id, DailyBriefModel.task_id == task_id
            )
        )
        if existing is not None:
            return existing
        current = await self._session.scalar(
            select(func.max(DailyBriefModel.version)).where(
                DailyBriefModel.user_id == user_id,
                DailyBriefModel.local_date == content.local_date,
            )
        )
        brief = DailyBriefModel(
            user_id=user_id,
            local_date=content.local_date,
            version=(current or 0) + 1,
            task_id=task_id,
            completeness=content.completeness,
            source_cutoff=content.source_cutoff,
            headline=content.headline,
            structured_content=content.model_dump(mode="json"),
            markdown=markdown,
            warnings=content.warnings,
            created_at=datetime.now(UTC),
        )
        self._session.add(brief)
        await self._session.flush()
        self._session.add_all(
            DailyBriefItemModel(
                brief_id=brief.id,
                position=index,
                section=item.section.value,
                priority=item.priority.value,
                title=item.title,
                body_markdown=item.body_markdown,
                source_refs=[source.model_dump(mode="json") for source in item.source_refs],
                suggested_action_kind=item.suggested_action_kind,
            )
            for index, item in enumerate(content.items)
        )
        for analysis in email_analyses:
            self._session.add(
                EmailAnalysisModel(
                    user_id=user_id,
                    thread_id=UUID(str(analysis["thread_id"])),
                    category=str(analysis["category"]),
                    urgency=str(analysis["urgency"]),
                    needs_reply=bool(analysis.get("needs_reply", False)),
                    deadline_at=analysis.get("deadline_at"),
                    confidence=float(analysis["confidence"]),
                    reason_codes=list(analysis.get("reason_codes", [])),
                    model_name=analysis.get("model_name"),
                    prompt_version=analysis.get("prompt_version"),
                    input_hash=str(analysis["input_hash"]),
                    created_at=datetime.now(UTC),
                )
            )
        for invocation in model_invocations:
            self._session.add(
                LLMInvocationModel(
                    user_id=user_id,
                    task_id=task_id,
                    step_id=None,
                    provider=str(invocation["provider"]),
                    model_name=str(invocation["model_name"]),
                    prompt_version=str(invocation["prompt_version"]),
                    input_hash=str(invocation["input_hash"]),
                    output_schema=str(invocation["output_schema"]),
                    input_tokens=int(invocation["input_tokens"]),
                    output_tokens=int(invocation["output_tokens"]),
                    estimated_cost_microusd=invocation.get("estimated_cost_microusd"),
                    latency_ms=int(invocation["latency_ms"]),
                    status=str(invocation["status"]),
                    error_code=invocation.get("error_code"),
                    created_at=datetime.now(UTC),
                )
            )
        task.result_payload = {"brief_id": str(brief.id), "completeness": content.completeness}
        self._session.add(
            AuditEventModel(
                user_id=user_id,
                task_id=task_id,
                event_type="brief.ready",
                actor_type="system",
                actor_id=None,
                event_metadata={"brief_id": str(brief.id), "completeness": content.completeness},
            )
        )
        return brief.id


class SqlAlchemyDailyBriefPersistenceStoreFactory:
    """为每次简报保存提供独立且自动提交或回滚的 SQLAlchemy 事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级 Session factory，不在构造阶段占用数据库连接。"""
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[DailyBriefPersistenceStore]:
        """将单次持久化限制为完整结果一起提交或一起回滚的事务。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyDailyBriefPersistenceStore(session)
