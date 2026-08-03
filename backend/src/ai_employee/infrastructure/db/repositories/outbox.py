"""用 SQLAlchemy 实现 Outbox claim、确认与失败恢复端口。"""

from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import select, update

from ai_employee.application.use_cases.outbox import ClaimedOutboxEvent, utc_instant
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyOutboxStore:
    """用多个边界清晰的短事务实现 Outbox claim、确认与失败恢复。

    claim 事务只锁到状态和 ``available_at`` 更新提交为止，绝不在持锁期间调用 Redis。
    成功和失败分别使用新事务，并以 claim 的 ``available_at`` 充当 CAS token，防止已经
    超时并被另一 relay 重新 claim 的旧调用覆盖新结果。
    """

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        *,
        retry_recovery_delay: timedelta = timedelta(seconds=360),
    ) -> None:
        """保存进程级 Session factory 与已确认投递后的恢复等待时间。"""
        if retry_recovery_delay <= timedelta(0):
            raise ValueError("retry_recovery_delay must be positive")
        self._session_factory = session_factory
        self._retry_recovery_delay = retry_recovery_delay

    async def claim_due(
        self,
        *,
        now: datetime,
        claim_until: datetime,
        limit: int,
        task_id: UUID | None = None,
    ) -> tuple[ClaimedOutboxEvent, ...]:
        """按稳定顺序 claim 到期的未发布任务事件并先把 CREATED 归队。

        Args:
            now: 本轮扫描的 UTC 瞬间。
            claim_until: claim 提交后再次允许扫描的 UTC 瞬间。
            limit: 本事务最多锁定的行数，必须为正。
            task_id: 提交后立即投递时限定的任务；minute relay 省略。

        Returns:
            提交后可安全进行外部 I/O 的不可变事件快照。

        Raises:
            ValueError: 时间不带时区、claim 未向未来移动或 limit 非正。
        """
        now = utc_instant(now, field="now")
        claim_until = utc_instant(claim_until, field="claim_until")
        if claim_until <= now:
            raise ValueError("claim_until must be later than now")
        if limit <= 0:
            raise ValueError("limit must be positive")

        async with self._session_factory.begin() as session:
            query = (
                select(OutboxEventModel)
                .where(
                    OutboxEventModel.topic == "task.execute",
                    OutboxEventModel.published_at.is_(None),
                    OutboxEventModel.available_at <= now,
                )
                .order_by(OutboxEventModel.available_at, OutboxEventModel.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            if task_id is not None:
                query = query.where(OutboxEventModel.aggregate_id == task_id)
            events = tuple((await session.scalars(query)).all())

            claimed: list[ClaimedOutboxEvent] = []
            for event in events:
                # 状态归队与 claim 位于同一事务；enqueue 回调从新连接观察时二者都已提交。
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == event.aggregate_id,
                        TaskRunModel.status == TaskStatus.CREATED.value,
                    )
                    .values(status=TaskStatus.QUEUED.value, updated_at=now)
                )
                event.available_at = claim_until
                claimed.append(
                    ClaimedOutboxEvent(
                        event_id=event.id,
                        task_id=event.aggregate_id,
                        claim_until=claim_until,
                        attempt_count=event.attempt_count,
                        resume=cast(
                            str | None,
                            event.payload.get("resume")
                            if event.payload.get("resume") in {"approved", "rejected"}
                            else None,
                        ),
                    )
                )
            return tuple(claimed)

    async def mark_published(self, claim: ClaimedOutboxEvent, *, published_at: datetime) -> bool:
        """确认 enqueue，并在同一事务中为延迟重试启用 Redis 丢失恢复。

        只有成功 enqueue 后才把 ``retry_recovery_at`` 写入 PostgreSQL。因此 relay 被阻塞、
        Redis 调度慢或进程在确认前崩溃时，未发布 Outbox 仍是唯一待交接事实，恢复扫描不会
        抢先复制它。
        """
        published_at = utc_instant(published_at, field="published_at")
        async with self._session_factory.begin() as session:
            published_event = (
                await session.execute(
                    update(OutboxEventModel)
                    .where(
                        OutboxEventModel.id == claim.event_id,
                        OutboxEventModel.published_at.is_(None),
                        OutboxEventModel.available_at == claim.claim_until,
                    )
                    .values(published_at=published_at, last_error=None)
                    .returning(
                        OutboxEventModel.aggregate_id,
                        OutboxEventModel.deduplication_key,
                    )
                )
            ).one_or_none()
            if published_event is None:
                return False
            aggregate_id = published_event.aggregate_id
            retry_key_prefix = f"task.execute:{aggregate_id}:retry:"
            retry_attempt = published_event.deduplication_key.removeprefix(retry_key_prefix)
            if (
                not published_event.deduplication_key.startswith(retry_key_prefix)
                or not retry_attempt.isdecimal()
            ):
                return True
            await session.execute(
                update(TaskRunModel)
                .where(
                    TaskRunModel.id == aggregate_id,
                    TaskRunModel.status == TaskStatus.RETRY_SCHEDULED.value,
                )
                .values(
                    retry_recovery_at=published_at + self._retry_recovery_delay,
                    updated_at=published_at,
                )
            )
        return True

    async def mark_failed(
        self,
        claim: ClaimedOutboxEvent,
        *,
        available_at: datetime,
    ) -> bool:
        """在新短事务中记录内容无关错误码、增加次数并安排下一次扫描。

        原始异常文本可能包含 Redis URL、网络地址或第三方载荷，因此数据库只保存固定
        ``queue_enqueue_failed``，详细工程异常由上层结构化日志的脱敏策略另行处理。
        """
        available_at = utc_instant(available_at, field="available_at")
        async with self._session_factory.begin() as session:
            event_id = await session.scalar(
                update(OutboxEventModel)
                .where(
                    OutboxEventModel.id == claim.event_id,
                    OutboxEventModel.published_at.is_(None),
                    OutboxEventModel.available_at == claim.claim_until,
                )
                .values(
                    attempt_count=OutboxEventModel.attempt_count + 1,
                    last_error="queue_enqueue_failed",
                    available_at=available_at,
                )
                .returning(OutboxEventModel.id)
            )
        return event_id is not None

    async def get_task_status(self, task_id: UUID) -> TaskStatus:
        """读取提交后投递完成时的任务状态，不返回 ORM 对象。"""
        async with self._session_factory() as session:
            status = await session.scalar(
                select(TaskRunModel.status).where(TaskRunModel.id == task_id)
            )
        if status is None:
            raise RuntimeError("dispatched task no longer exists")
        return TaskStatus(status)
