"""以 PostgreSQL 实现任务 API 所需的用户隔离读写视图。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from ai_employee.application.use_cases.task_views import TaskSnapshot, TaskStepSnapshot
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import TaskStatus, transition_task
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyTaskViewStore:
    """把任务状态变更、审计与 Outbox 保持在单一短事务内。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存 API 进程共享的 Session factory。"""
        self._session_factory = session_factory

    @staticmethod
    def _snapshot(task: TaskRunModel, steps: tuple[TaskStepModel, ...]) -> TaskSnapshot:
        """显式白名单映射 ORM，阻止 API 暴露租约或内部图字段。"""
        return TaskSnapshot(
            id=task.id,
            kind=task.kind,
            status=TaskStatus(task.status),
            retry_of_task_id=task.retry_of_task_id,
            input_payload=task.input_payload,
            error_code=task.error_code,
            steps=tuple(
                TaskStepSnapshot(
                    id=step.id,
                    sequence=step.sequence,
                    name=step.name,
                    status=step.status,
                    output_summary=step.output_summary,
                    error_code=step.error_code,
                )
                for step in steps
            ),
        )

    async def _get_in_session(
        self, session: object, *, task_id: UUID, user_id: UUID
    ) -> TaskSnapshot | None:
        """在调用方事务中读取排序步骤；私有方法避免跨事务 ORM 泄漏。"""
        from sqlalchemy.ext.asyncio import AsyncSession

        typed_session = session if isinstance(session, AsyncSession) else None
        if typed_session is None:
            raise RuntimeError("invalid session")
        task = await typed_session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
        )
        if task is None:
            return None
        steps = tuple(
            (
                await typed_session.scalars(
                    select(TaskStepModel)
                    .where(TaskStepModel.task_id == task_id)
                    .order_by(TaskStepModel.sequence)
                )
            ).all()
        )
        return self._snapshot(task, steps)

    async def get(self, *, task_id: UUID, user_id: UUID) -> TaskSnapshot | None:
        """读取用户拥有的任务及稳定排序时间线。"""
        async with self._session_factory() as session:
            return await self._get_in_session(session, task_id=task_id, user_id=user_id)

    async def cancel(self, *, task_id: UUID, user_id: UUID, now: datetime) -> TaskSnapshot | None:
        """锁定任务，验证状态机后追加可重放的取消审计事件。"""
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
                .with_for_update()
            )
            if task is None:
                return None
            transition_task(TaskStatus(task.status), TaskStatus.CANCELLED)
            task.status = TaskStatus.CANCELLED.value
            task.finished_at = now
            task.lease_owner = None
            task.lease_expires_at = None
            session.add(
                AuditEventModel(
                    user_id=user_id,
                    task_id=task_id,
                    event_type="task.cancelled",
                    actor_type="user",
                    actor_id=str(user_id),
                    event_metadata={},
                )
            )
            await session.flush()
            return await self._get_in_session(session, task_id=task_id, user_id=user_id)

    async def retry(
        self, *, task_id: UUID, user_id: UUID, idempotency_key: str, now: datetime
    ) -> TaskSnapshot | None:
        """通过用户范围幂等键从失败任务复制输入并创建新的 Outbox 事实。"""
        async with self._session_factory.begin() as session:
            original = await session.scalar(
                select(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
                .with_for_update()
            )
            if original is None:
                return None
            if original.status != TaskStatus.FAILED.value:
                raise StateConflictError(
                    error_code="task_state_conflict", message="task is not failed"
                )
            existing = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.user_id == user_id, TaskRunModel.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return await self._get_in_session(session, task_id=existing.id, user_id=user_id)
            replacement_id = uuid4()
            inserted = await session.scalar(
                insert(TaskRunModel)
                .values(
                    id=replacement_id,
                    user_id=user_id,
                    retry_of_task_id=task_id,
                    kind=original.kind,
                    status=TaskStatus.QUEUED.value,
                    idempotency_key=idempotency_key,
                    input_payload=original.input_payload,
                )
                .on_conflict_do_nothing(constraint="uq_task_runs_user_id_idempotency_key")
                .returning(TaskRunModel.id)
            )
            if inserted is None:
                existing = await session.scalar(
                    select(TaskRunModel).where(
                        TaskRunModel.user_id == user_id,
                        TaskRunModel.idempotency_key == idempotency_key,
                    )
                )
                if existing is None:
                    raise RuntimeError("retry idempotency winner is not visible")
                return await self._get_in_session(session, task_id=existing.id, user_id=user_id)
            session.add_all(
                (
                    AuditEventModel(
                        user_id=user_id,
                        task_id=replacement_id,
                        event_type="task.created",
                        actor_type="user",
                        actor_id=str(user_id),
                        event_metadata={"retry_of_task_id": str(task_id)},
                    ),
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=replacement_id,
                        deduplication_key=f"task.execute:{replacement_id}:initial",
                        payload={"task_id": str(replacement_id)},
                        available_at=now,
                    ),
                )
            )
            await session.flush()
            return await self._get_in_session(session, task_id=replacement_id, user_id=user_id)


class PostgresQueuedTaskDispatcher:
    """在 Outbox relay 接管前仅持久化初始 ``QUEUED`` 状态的 API 适配器。

    API 进程不直接访问 Redis；未发布 Outbox 仍是唯一可恢复的投递事实，独立 relay 会负责
    把标识交给队列。该轻量状态推进让 202 响应准确反映任务已可被 relay 消费。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存共享 Session factory。"""
        self._session_factory = session_factory

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """原子把新建任务归队，不执行任何队列网络 I/O。"""
        async with self._session_factory.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.status == TaskStatus.CREATED.value)
                .values(status=TaskStatus.QUEUED.value)
            )
            status = await session.scalar(
                select(TaskRunModel.status).where(TaskRunModel.id == task_id)
            )
        if status is None:
            raise RuntimeError("created task disappeared")
        return TaskStatus(status)
