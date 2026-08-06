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
    delayed_retry_submission_pending: bool = False,
) -> tuple[UUID, UUID]:
    """写入 RETRY_SCHEDULED 任务及其可控的重试投递交接状态。

    Args:
        database_url: 已隔离的 PostgreSQL 测试连接。
        recovery_at: PostgreSQL 恢复扫描开始考虑该任务的时刻。
        delayed_retry_submission_pending: 为 ``True`` 时额外写入尚未发布的延迟重试
            Outbox，模拟 relay 正常但被暂时阻塞，尚未向 Redis 确认交接。

    Returns:
        新建用户与任务的稳定标识。
    """
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _user()
            task_id = uuid4()
            session.add(user)
            await session.flush()
            events = [
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
            ]
            if delayed_retry_submission_pending:
                events.append(
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=task_id,
                        deduplication_key=f"task.execute:{task_id}:retry:1",
                        payload={"task_id": str(task_id)},
                        available_at=recovery_at,
                    )
                )
            session.add_all(events)
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
        # RETRY、过期 QUEUED 与过期 RUNNING 共用统一恢复审计类型，避免消费者根据
        # 具体恢复来源分叉；去重键仍保留各路径的精确语义。
        assert [event.event_metadata for event in audit] == [{"reason": "task_recovery"}]
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
async def test_pending_durable_retry_submission_blocks_recovery_duplicate(
    database_url: str,
) -> None:
    """正常延迟 Outbox 尚未交接给 Redis 时，扫描器不能抢先补发重复事件。

    这模拟 relay 在已提交的延迟重试 Outbox 与 Redis ``enqueue`` 之间停顿。恢复事实
    必须等待该既有 Outbox 的交接结果，而不能仅依据时间阈值再创建一个可投递事件。
    """
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    _, task_id = await _create_retry_scheduled_task(
        database_url=database_url,
        recovery_at=now,
        delayed_retry_submission_pending=True,
    )
    session_factory = build_session_factory(database_url)
    try:
        recovered = await RecoverScheduledTaskRetriesUseCase(
            store=SqlAlchemyTaskRetryRecoveryStore(session_factory)
        ).execute(now=now + timedelta(hours=1), limit=10)

        assert recovered == 0
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            events = (
                await session.scalars(
                    select(OutboxEventModel)
                    .where(OutboxEventModel.aggregate_id == task_id)
                    .order_by(OutboxEventModel.created_at, OutboxEventModel.id)
                )
            ).all()
        assert task is not None
        assert task.status == TaskStatus.RETRY_SCHEDULED.value
        assert task.retry_recovery_at == now
        assert {event.deduplication_key for event in events} == {
            f"task.execute:{task_id}:initial",
            f"task.execute:{task_id}:retry:1",
        }
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_old_duplicate_message_cannot_bypass_unpublished_delayed_retry_outbox(
    database_url: str,
) -> None:
    """旧 Stream 重复消息不能把仍在等待 relay 的下一轮重试提前归队执行。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    _, task_id = await _create_retry_scheduled_task(
        database_url=database_url,
        recovery_at=now,
        delayed_retry_submission_pending=True,
    )
    session_factory = build_session_factory(database_url)
    try:
        from ai_employee.infrastructure.db.repositories.task_execution import (
            SqlAlchemyTaskExecutionStore,
        )

        store = SqlAlchemyTaskExecutionStore(session_factory)
        await store.prepare_retry(task_id=task_id, now=now + timedelta(seconds=1))
        leased = await store.acquire(
            task_id=task_id,
            lease_owner="old-stream-delivery",
            now=now + timedelta(seconds=1),
            lease_expires_at=now + timedelta(seconds=61),
        )

        assert leased is None
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            retry_event = await session.scalar(
                select(OutboxEventModel).where(
                    OutboxEventModel.aggregate_id == task_id,
                    OutboxEventModel.deduplication_key == f"task.execute:{task_id}:retry:1",
                )
            )
        assert task is not None
        assert task.status == TaskStatus.RETRY_SCHEDULED.value
        assert retry_event is not None
        assert retry_event.published_at is None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_suffix", ("initial", "retry-recovery:2030-08-01T00:00:00+00:00"))
async def test_non_delayed_outbox_publication_does_not_arm_retry_recovery_deadline(
    database_url: str,
    event_suffix: str,
) -> None:
    """初始和恢复投递都不是延迟重试，发布后不得写入恢复期限。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    _, task_id = await _create_retry_scheduled_task(
        database_url=database_url,
        recovery_at=now,
    )
    session_factory = build_session_factory(database_url)
    enqueuer = RecordingEnqueuer()
    try:
        async with session_factory.begin() as session:
            event = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
            )
            assert event is not None
            event.deduplication_key = f"task.execute:{task_id}:{event_suffix}"
            event.published_at = None

        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(
                session_factory,
                retry_recovery_delay=timedelta(seconds=30),
            ),
            enqueuer=enqueuer,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        assert await relay.relay_once(limit=10) == 1
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
        assert task is not None
        assert task.retry_recovery_at == now
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_retry_recovery_deadline_is_armed_only_after_durable_outbox_is_published(
    database_url: str,
) -> None:
    """Redis 丢失兜底期限必须以后续 enqueue 成功确认，而不是 Runner 预估为准。"""
    now = datetime(2030, 8, 1, 0, 0, tzinfo=UTC)
    _, task_id = await _create_retry_scheduled_task(
        database_url=database_url,
        recovery_at=now,
        delayed_retry_submission_pending=True,
    )
    session_factory = build_session_factory(database_url)
    enqueuer = RecordingEnqueuer()
    try:
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(
                session_factory,
                retry_recovery_delay=timedelta(seconds=30),
            ),
            enqueuer=enqueuer,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )

        assert await relay.relay_once(limit=10) == 1
        assert enqueuer.task_ids == [task_id]
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            event = await session.scalar(
                select(OutboxEventModel).where(
                    OutboxEventModel.aggregate_id == task_id,
                    OutboxEventModel.deduplication_key == f"task.execute:{task_id}:retry:1",
                )
            )
        assert task is not None
        assert task.retry_recovery_at == now + timedelta(seconds=30)
        assert event is not None
        assert event.published_at == now
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
