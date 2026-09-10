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


async def _seed_task27e_recovery_candidate(
    factory, *, index: int, shape: str = "winner", active: bool = False
) -> UUID:
    """以确定性排序身份构造恢复候选，不借当前状态推导期望去重键或 authority。"""
    now = datetime(2030, 1, 1, tzinfo=UTC)
    task_id = UUID(int=40_000 + index)
    user_id = UUID(int=10_000 + index)
    async with factory.begin() as session:
        session.add(
            UserModel(
                id=user_id,
                email=f"recovery-{index}@example.test",
                display_name="Synthetic",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=active,
            )
        )
        await session.flush()
        session.add(
            TaskRunModel(
                id=task_id,
                user_id=user_id,
                kind="daily_brief" if shape == "ordinary" else "privacy.delete_all_data",
                status="running",
                idempotency_key=f"synthetic-{index}",
                input_payload={"deletion_request_id": f"synthetic-request-{index}"},
                started_at=now - timedelta(days=2),
                attempt_count=1,
                lease_owner="expired-worker",
                lease_expires_at=now - timedelta(seconds=1),
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(minutes=10),
            )
        )
        await session.flush()
        if shape != "ordinary" and shape != "missing":
            for _ in range(2 if shape == "many" else 1):
                session.add(
                    AuditEventModel(
                        user_id=user_id,
                        task_id=task_id,
                        event_type="privacy.deletion_started",
                        actor_type="system",
                        actor_id=None,
                        event_metadata={
                            "schema_version": "privacy_deletion_started.v1",
                            "request_id": "wrong"
                            if shape == "mismatch"
                            else f"synthetic-request-{index}",
                        },
                        created_at=now - timedelta(minutes=2),
                    )
                )
        if shape == "published":
            session.add(
                OutboxEventModel(
                    topic="task.execute",
                    aggregate_id=task_id,
                    payload={"task_id": str(task_id)},
                    deduplication_key=f"task.execute:{task_id}:inactive-deletion-recovery:2030-01-01T00:00:00+00:00",
                    published_at=now,
                )
            )
    return task_id


async def _task27e_recovery_state(
    factory, task_id: UUID
) -> tuple[dict[str, object], int, list[tuple[object, ...]]]:
    """读取任务所有列与审计/Outbox，不允许扫描器隐藏任何零变更分支的状态写入。"""
    async with factory() as session:
        task = await session.get(TaskRunModel, task_id)
        assert task is not None
        facts = {
            column.name: getattr(task, column.name) for column in TaskRunModel.__table__.columns
        }
        audit_ids = (
            await session.scalars(
                select(AuditEventModel.id).where(AuditEventModel.user_id == task.user_id)
            )
        ).all()
        outbox = (
            await session.execute(
                select(OutboxEventModel.payload, OutboxEventModel.deduplication_key)
                .where(OutboxEventModel.aggregate_id == task_id)
                .order_by(OutboxEventModel.deduplication_key)
            )
        ).all()
        return facts, len(audit_ids), [tuple(row) for row in outbox]


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["ordinary", "missing", "many", "mismatch", "published"])
async def test_task27e_inactive_nonactionable_recovery_is_byte_preserving(
    database_url: str, shape: str
) -> None:
    """inactive普通/歧义/同桶已发布候选保持所有任务列与审计、Outbox逐字不变。"""
    factory = build_session_factory(database_url)
    try:
        task_id = await _seed_task27e_recovery_candidate(factory, index=1, shape=shape)
        before = await _task27e_recovery_state(factory, task_id)
        assert (
            await SqlAlchemyTaskRetryRecoveryStore(factory).recover_due(
                now=datetime(2030, 1, 1, tzinfo=UTC), limit=2
            )
            == 0
        )
        assert await _task27e_recovery_state(factory, task_id) == before
    finally:
        await factory.dispose()


@pytest.mark.asyncio
async def test_task27e_winner_recovery_preserves_running_deduplicates_and_survives_redis_loss(
    database_url: str,
) -> None:
    """并发扫描仅补identifier Outbox；Redis丢失后下一桶重补，接管后停止直到租约到期。"""
    import asyncio

    from sqlalchemy import update

    from ai_employee.infrastructure.db.repositories.task_execution import (
        SqlAlchemyTaskExecutionStore,
    )

    now = datetime(2030, 1, 1, tzinfo=UTC)
    factory = build_session_factory(database_url)
    try:
        task_id = await _seed_task27e_recovery_candidate(factory, index=1)
        before = await _task27e_recovery_state(factory, task_id)
        store = SqlAlchemyTaskRetryRecoveryStore(factory)
        assert sorted(
            await asyncio.gather(
                store.recover_due(now=now, limit=2), store.recover_due(now=now, limit=2)
            )
        ) == [0, 1]
        after = await _task27e_recovery_state(factory, task_id)
        assert after[:2] == before[:2]
        assert after[2] == [
            (
                {"task_id": str(task_id)},
                f"task.execute:{task_id}:inactive-deletion-recovery:2030-01-01T00:00:00+00:00",
            )
        ]
        assert await store.recover_due(now=now, limit=2) == 0
        enqueuer = RecordingEnqueuer()
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(factory),
            enqueuer=enqueuer,
            clock=lambda: now,
            claim_ttl=timedelta(seconds=60),
            retry_base=timedelta(seconds=5),
            retry_max=timedelta(seconds=300),
        )
        assert await relay.relay_once(limit=2) == 1
        assert enqueuer.task_ids == [task_id]
        enqueuer.task_ids.clear()  # 供应商无关 Fake queue 丢失已发布消息；PostgreSQL事实仍在。
        assert await store.recover_due(now=now + timedelta(minutes=5), limit=2) == 1
        second = await _task27e_recovery_state(factory, task_id)
        assert second[:2] == before[:2]
        assert (
            second[2][1][1]
            == f"task.execute:{task_id}:inactive-deletion-recovery:2030-01-01T00:05:00+00:00"
        )
        # 模拟acquire提交后ACK丢失，旧outbox已被发布也不能重复进入live lease。
        async with factory.begin() as session:
            await session.execute(
                update(OutboxEventModel)
                .where(OutboxEventModel.aggregate_id == task_id)
                .values(published_at=now)
            )
        assert (
            await SqlAlchemyTaskExecutionStore(factory).acquire(
                task_id=task_id,
                lease_owner="new-worker",
                now=now + timedelta(minutes=5),
                lease_expires_at=now + timedelta(minutes=20),
            )
            is not None
        )
        assert await store.recover_due(now=now + timedelta(minutes=10), limit=2) == 0
        assert await store.recover_due(now=now + timedelta(minutes=20), limit=2) == 1
    finally:
        await factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("active_prefix", [False, True])
async def test_task27e_recovery_limit_excludes_unchanged_prefix_before_winner(
    database_url: str, active_prefix: bool
) -> None:
    """超过limit的inactive零变化前缀不占名额；有限active前缀状态变更后也必达后续赢家。"""
    now = datetime(2030, 1, 1, tzinfo=UTC)
    limit = 2
    factory = build_session_factory(database_url)
    try:
        prefix_ids = []
        shapes = (
            ["ordinary"]
            if active_prefix
            else ["ordinary", "missing", "many", "mismatch", "published"]
        )
        index = 1
        for shape in shapes:
            for _ in range(limit + 1):
                prefix_ids.append(
                    await _seed_task27e_recovery_candidate(
                        factory, index=index, shape=shape, active=active_prefix
                    )
                )
                index += 1
        winner_id = await _seed_task27e_recovery_candidate(factory, index=index)
        before = [await _task27e_recovery_state(factory, task_id) for task_id in prefix_ids]
        store = SqlAlchemyTaskRetryRecoveryStore(factory)
        if active_prefix:
            assert await store.recover_due(now=now, limit=limit) == 2
            assert await store.recover_due(now=now, limit=limit) == 2
        else:
            assert await store.recover_due(now=now, limit=limit) == 1
            assert [
                await _task27e_recovery_state(factory, task_id) for task_id in prefix_ids
            ] == before
        winner = await _task27e_recovery_state(factory, winner_id)
        assert winner[0]["status"] == "running"
        assert winner[2] == [
            (
                {"task_id": str(winner_id)},
                f"task.execute:{winner_id}:inactive-deletion-recovery:2030-01-01T00:00:00+00:00",
            )
        ]
    finally:
        await factory.dispose()


@pytest.mark.asyncio
async def test_task27e_inactive_winner_real_redis_loss_replays_identifier_only_outbox(
    database_url: str,
    empty_redis,
) -> None:
    """真实 Taskiq/Redis 先接收再丢ACK；重放与整库丢失后的下一桶都只运输 task_id。"""
    import json

    from redis.asyncio import Redis
    from taskiq_redis import RedisStreamBroker

    from ai_employee.application.use_cases.task_execution import TaskLeaseMode
    from ai_employee.infrastructure.db.repositories.task_execution import (
        SqlAlchemyTaskExecutionStore,
    )
    from ai_employee.infrastructure.queue.enqueue import TaskiqTaskEnqueuer

    class LostPublishAckStore(SqlAlchemyOutboxStore):
        """只在真实 Redis 入队后丢弃本次确认，保留已提交的 Outbox claim 供到期接管。"""

        lose_ack = False

        async def mark_published(self, claim, *, published_at):
            if self.lose_ack:
                self.lose_ack = False
                raise RuntimeError("synthetic publish acknowledgement loss")
            return await super().mark_published(claim, published_at=published_at)

    now = datetime(2030, 1, 1, tzinfo=UTC)
    factory = build_session_factory(database_url)
    client = Redis.from_url(str(empty_redis), decode_responses=False)
    # 仅创建测试传输实例；生产相同的 Taskiq sender→enqueuer 编码保持不变，不执行任务体。
    broker = RedisStreamBroker(
        url=str(empty_redis),
        queue_name="ai_employee_tasks",
        consumer_group_name="ai_employee_workers",
        consumer_id="0-0",
    )

    @broker.task(task_name="ai_employee.workers.execute_task:execute_task")
    async def transport_only(task_id: str) -> None:
        """此测试只核对传输，任何意外 Worker 执行都明确失败。"""
        del task_id
        raise AssertionError("transport-only task must not run")

    try:
        task_id = await _seed_task27e_recovery_candidate(factory, index=1)
        before = await _task27e_recovery_state(factory, task_id)
        recovery = SqlAlchemyTaskRetryRecoveryStore(factory)
        outbox = LostPublishAckStore(factory)
        relay = OutboxRelay(
            store=outbox,
            enqueuer=TaskiqTaskEnqueuer(transport_only.kiq),
            clock=lambda: now,
            claim_ttl=timedelta(seconds=30),
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=10),
        )
        for bucket in range(2):
            now = datetime(2030, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * bucket)
            assert await recovery.recover_due(now=now, limit=1) == 1
            outbox.lose_ack = True
            with pytest.raises(RuntimeError, match="synthetic publish acknowledgement loss"):
                await relay.relay_once(limit=1)
            assert await client.xlen("ai_employee_tasks") == 1
            now += timedelta(seconds=31)
            assert await relay.relay_once(limit=1) == 1
            entries = await client.xrange("ai_employee_tasks")
            assert len(entries) == 2
            for _, fields in entries:
                message = json.loads(fields[b"data"])
                assert message["task_name"] == "ai_employee.workers.execute_task:execute_task"
                assert message["args"] == [str(task_id)] and message["kwargs"] == {}
                assert b"deletion_request_id" not in fields[b"data"]
            after = await _task27e_recovery_state(factory, task_id)
            assert after[:2] == before[:2]
            assert all(payload == {"task_id": str(task_id)} for payload, _ in after[2])
            assert await relay.relay_once(limit=1) == 0
            # fail-closed fixture 只允许 loopback 的保留 DB15；真正抹掉已发布队列再由PG恢复。
            await client.flushdb()
            assert await client.xlen("ai_employee_tasks") == 0

        lease_store = SqlAlchemyTaskExecutionStore(factory)
        lease = await lease_store.acquire(
            task_id=task_id,
            lease_owner="synthetic-after-loss",
            now=now,
            lease_expires_at=now + timedelta(minutes=5),
        )
        assert lease is not None and lease.lease_mode is TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
        assert (
            await lease_store.acquire(
                task_id=task_id,
                lease_owner="synthetic-duplicate",
                now=now,
                lease_expires_at=now + timedelta(minutes=5),
            )
            is None
        )
        assert await recovery.recover_due(now=now + timedelta(minutes=1), limit=1) == 0
        assert await recovery.recover_due(now=now + timedelta(minutes=5), limit=1) == 1
    finally:
        await broker.shutdown()
        await client.aclose()
        await factory.dispose()
