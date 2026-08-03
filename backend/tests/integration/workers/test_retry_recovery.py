"""验证 Redis 延迟重试事实丢失后可由 PostgreSQL 与 Outbox 恢复。"""

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.outbox import OutboxRelay
from ai_employee.application.use_cases.task_retry_recovery import (
    RecoverScheduledTaskRetriesUseCase,
)
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories.outbox import SqlAlchemyOutboxStore
from ai_employee.infrastructure.db.repositories.task_retry_recovery import (
    SqlAlchemyTaskRetryRecoveryStore,
)
from ai_employee.infrastructure.db.session import build_session_factory


class RecordingEnqueuer:
    """记录 Outbox relay 实际交给 Redis 边界的最小任务标识。"""

    def __init__(self) -> None:
        """初始化空的任务标识记录。"""
        self.task_ids: list[UUID] = []

    async def enqueue(self, task_id: UUID) -> None:
        """仅保存任务 UUID，证明队列不承载业务 payload。"""
        self.task_ids.append(task_id)


def _user() -> UserModel:
    """构造独立测试所需的合成活动用户。"""
    return UserModel(
        email=f"retry-recovery-{uuid4().hex}@example.com",
        display_name="Retry Recovery",
        password_hash=None,
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


async def _create_retry_scheduled_task(
    *,
    database_url: str,
    recovery_at: datetime,
) -> tuple[UUID, UUID]:
    """写入已发布初始 Outbox 的 RETRY_SCHEDULED 任务，模拟 Redis 被清空后的事实。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _user()
            task_id = uuid4()
            session.add(user)
            await session.flush()
            session.add_all(
                (
                    TaskRunModel(
                        id=task_id,
                        user_id=user.id,
                        kind="daily_brief",
                        status=TaskStatus.RETRY_SCHEDULED.value,
                        idempotency_key=f"retry-recovery:{task_id}",
                        input_payload={"local_date": "2030-08-01"},
                        error_code="provider_temporarily_unavailable",
                        retry_recovery_at=recovery_at,
                    ),
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=task_id,
                        deduplication_key=f"task.execute:{task_id}:initial",
                        payload={"task_id": str(task_id)},
                        published_at=recovery_at - timedelta(minutes=1),
                    ),
                )
            )
            return user.id, task_id
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_due_retry_is_requeued_through_new_outbox_after_redis_schedule_loss(
    database_url: str,
) -> None:
    """恢复器只在 PostgreSQL 写新事实，relay 再把唯一 task_id 投递给队列。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    user_id, task_id = await _create_retry_scheduled_task(
        database_url=database_url,
        recovery_at=now,
    )
    session_factory = build_session_factory(database_url)
    enqueuer = RecordingEnqueuer()
    try:
        recovered = await RecoverScheduledTaskRetriesUseCase(
            store=SqlAlchemyTaskRetryRecoveryStore(session_factory)
        ).execute(now=now, limit=10)
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(session_factory),
            enqueuer=enqueuer,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        assert recovered == 1
        assert await relay.relay_once(limit=10) == 1
        assert enqueuer.task_ids == [task_id]
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            events = (
                await session.scalars(
                    select(OutboxEventModel)
                    .where(OutboxEventModel.aggregate_id == task_id)
                    .order_by(OutboxEventModel.created_at, OutboxEventModel.id)
                )
            ).all()
            audit = (
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.task_id == task_id,
                        AuditEventModel.event_type == "task.queued",
                    )
                )
            ).all()
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        assert task.retry_recovery_at is None
        assert [event.deduplication_key for event in events] == [
            f"task.execute:{task_id}:initial",
            f"task.execute:{task_id}:retry-recovery:{now.isoformat()}",
        ]
        assert events[0].published_at is not None
        assert events[1].published_at == now
        assert [event.event_metadata for event in audit] == [{"reason": "retry_recovery"}]
        assert audit[0].user_id == user_id
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_retry_recovery_scan_is_idempotent_after_first_claim(database_url: str) -> None:
    """重复扫描不会改写初始事件，也不会为同一重试创建第二个 Outbox。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    _, task_id = await _create_retry_scheduled_task(database_url=database_url, recovery_at=now)
    session_factory = build_session_factory(database_url)
    try:
        use_case = RecoverScheduledTaskRetriesUseCase(
            store=SqlAlchemyTaskRetryRecoveryStore(session_factory)
        )

        assert await use_case.execute(now=now, limit=10) == 1
        assert await use_case.execute(now=now, limit=10) == 0
        async with session_factory() as session:
            events = (
                await session.scalars(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
                )
            ).all()
        assert len(events) == 2
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_not_due_retry_is_not_recovered(database_url: str) -> None:
    """恢复时间未到的任务继续等待快速 Taskiq 调度，不会被提前重复投递。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    _, task_id = await _create_retry_scheduled_task(
        database_url=database_url,
        recovery_at=now + timedelta(minutes=1),
    )
    session_factory = build_session_factory(database_url)
    try:
        recovered = await RecoverScheduledTaskRetriesUseCase(
            store=SqlAlchemyTaskRetryRecoveryStore(session_factory)
        ).execute(now=now, limit=10)

        assert recovered == 0
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            events = (
                await session.scalars(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
                )
            ).all()
        assert task is not None
        assert task.status == TaskStatus.RETRY_SCHEDULED.value
        assert task.retry_recovery_at == now + timedelta(minutes=1)
        assert len(events) == 1
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_taskiq_requeue_clears_recovery_deadline_before_fallback_scan(
    database_url: str,
) -> None:
    """正常 Redis 重投先归队时会清除恢复期限，后续扫描不能制造重复投递。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    _, task_id = await _create_retry_scheduled_task(
        database_url=database_url,
        recovery_at=now + timedelta(minutes=1),
    )
    session_factory = build_session_factory(database_url)
    try:
        from ai_employee.infrastructure.db.repositories.task_execution import (
            SqlAlchemyTaskExecutionStore,
        )

        await SqlAlchemyTaskExecutionStore(session_factory).prepare_retry(task_id=task_id, now=now)
        recovered = await RecoverScheduledTaskRetriesUseCase(
            store=SqlAlchemyTaskRetryRecoveryStore(session_factory)
        ).execute(now=now + timedelta(minutes=2), limit=10)

        assert recovered == 0
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            events = (
                await session.scalars(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
                )
            ).all()
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
        assert task.retry_recovery_at is None
        assert len(events) == 1
    finally:
        await session_factory.dispose()
