"""用 SQLAlchemy 实现 Redis 延迟调度丢失后的任务重试恢复。"""

from datetime import datetime

from sqlalchemy import select

from ai_employee.application.use_cases.task_execution import utc_instant
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyTaskRetryRecoveryStore:
    """以短事务和行锁把到期重试安全恢复到既有 Outbox relay 路径。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级会话工厂，不在构造阶段占用数据库连接。"""
        self._session_factory = session_factory

    async def recover_due(self, *, now: datetime, limit: int) -> int:
        """锁定到期 RETRY_SCHEDULED 行并原子创建新的未发布执行事件。

        ``FOR UPDATE SKIP LOCKED`` 允许多个 scheduler 实例分担积压而不重复处理同一行。
        状态、恢复期限清除、审计与 Outbox 在一个事务中提交；Redis 仅在后续 relay 成功后
        承载任务 UUID。每个恢复事件的去重键使用持久到期瞬间，故崩溃重扫仍指向同一事实，
        而后续一次新的重试周期会写新的到期瞬间并可独立恢复。
        """
        now = utc_instant(now, field="now")
        async with self._session_factory.begin() as session:
            tasks = (
                await session.scalars(
                    select(TaskRunModel)
                    .where(
                        TaskRunModel.status == TaskStatus.RETRY_SCHEDULED.value,
                        TaskRunModel.retry_recovery_at.is_not(None),
                        TaskRunModel.retry_recovery_at <= now,
                    )
                    .order_by(TaskRunModel.retry_recovery_at, TaskRunModel.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for task in tasks:
                recovery_at = task.retry_recovery_at
                if recovery_at is None:
                    raise RuntimeError("locked retry task has no recovery deadline")
                task.status = TaskStatus.QUEUED.value
                task.lease_owner = None
                task.lease_expires_at = None
                task.retry_recovery_at = None
                task.updated_at = now
                session.add_all(
                    (
                        AuditEventModel(
                            user_id=task.user_id,
                            task_id=task.id,
                            event_type="task.queued",
                            actor_type="system",
                            actor_id=None,
                            event_metadata={"reason": "retry_recovery"},
                        ),
                        OutboxEventModel(
                            topic="task.execute",
                            aggregate_id=task.id,
                            deduplication_key=(
                                f"task.execute:{task.id}:retry-recovery:{recovery_at.isoformat()}"
                            ),
                            payload={"task_id": str(task.id)},
                        ),
                    )
                )
            await session.flush()
            return len(tasks)
