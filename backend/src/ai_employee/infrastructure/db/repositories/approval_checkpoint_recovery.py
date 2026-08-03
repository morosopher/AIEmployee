"""以 PostgreSQL 恢复冻结审批到首个 LangGraph interrupt checkpoint 的交接。"""

from datetime import datetime

from sqlalchemy import exists, select, text

from ai_employee.application.use_cases.task_execution import utc_instant
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyApprovalCheckpointRecoveryStore:
    """扫描审批 checkpoint recovery anchor，并只通过 Outbox 重新投递。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存共享会话工厂，避免恢复扫描持有 Redis 或 Worker 资源。"""
        self._session_factory = session_factory

    async def recover_due(self, *, now: datetime, limit: int) -> int:
        """处理尚无 interrupt checkpoint 的 WAITING_APPROVAL 任务。

        冻结审批事务先写 ``approval_checkpoint_recovery_at``；它在初始 Outbox 已发布后仍
        保留，因此 Redis 丢失不会让审批永久不可决。若 checkpoint 已存在，本事务仅清除
        anchor；否则在没有未发布恢复消息时新增 Outbox。已发布但丢失的消息允许下轮产生
        新去重键，至少一次重投由任务租约和 checkpoint 幂等保证安全。
        """
        now = utc_instant(now, field="now")
        async with self._session_factory.begin() as session:
            tasks = list(
                (
                    await session.scalars(
                        select(TaskRunModel)
                        .where(
                            TaskRunModel.status == TaskStatus.WAITING_APPROVAL.value,
                            TaskRunModel.approval_checkpoint_recovery_at.is_not(None),
                            TaskRunModel.approval_checkpoint_recovery_at <= now,
                        )
                        .order_by(TaskRunModel.approval_checkpoint_recovery_at, TaskRunModel.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            recovered = 0
            for task in tasks:
                durable_interrupt = await session.scalar(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM checkpoint_writes AS checkpoint_write
                            INNER JOIN checkpoints AS checkpoint
                                ON checkpoint.thread_id = checkpoint_write.thread_id
                                AND checkpoint.checkpoint_ns = checkpoint_write.checkpoint_ns
                                AND checkpoint.checkpoint_id = checkpoint_write.checkpoint_id
                            WHERE checkpoint_write.thread_id = :thread_id
                                AND checkpoint_write.channel = '__interrupt__'
                        )
                        """
                    ),
                    {"thread_id": str(task.id)},
                )
                if durable_interrupt is True:
                    task.approval_checkpoint_recovery_at = None
                    continue
                unpublished_recovery = await session.scalar(
                    select(
                        exists(
                            select(OutboxEventModel.id).where(
                                OutboxEventModel.aggregate_id == task.id,
                                OutboxEventModel.topic == "task.execute",
                                OutboxEventModel.published_at.is_(None),
                                OutboxEventModel.payload["recover_approval_checkpoint"]
                                .as_boolean()
                                .is_(True),
                            )
                        )
                    )
                )
                if unpublished_recovery is True:
                    continue
                session.add(
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=task.id,
                        deduplication_key=(
                            f"task.ensure_approval_checkpoint:{task.id}:{now.isoformat()}"
                        ),
                        payload={
                            "task_id": str(task.id),
                            "recover_approval_checkpoint": True,
                        },
                        available_at=now,
                    )
                )
                recovered += 1
            return recovered
