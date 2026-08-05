"""验证 Redis 丢失不影响 PostgreSQL 事实与 Outbox 恢复契约。"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from sqlalchemy import select

from ai_employee.application.use_cases.outbox import OutboxRelay
from ai_employee.application.use_cases.task_retry_recovery import RecoverScheduledTaskRetriesUseCase
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.outbox import SqlAlchemyOutboxStore
from ai_employee.infrastructure.db.session import build_session_factory


@pytest.mark.asyncio
async def test_redis_flush_recovery_delegates_to_postgresql_outbox_store() -> None:
    """恢复用例只调用耐久 store，因此 Redis flush 不会删除业务事实。"""
    calls: list[tuple[datetime, int]] = []

    class Store:
        async def recover_due(self, *, now: datetime, limit: int) -> int:
            calls.append((now, limit))
            return 1

    now = datetime(2026, 8, 5, tzinfo=UTC)
    assert await RecoverScheduledTaskRetriesUseCase(store=Store()).execute(now=now, limit=20) == 1
    assert calls == [(now, 20)]


@pytest.mark.asyncio
async def test_redis_flush_keeps_postgres_task_and_replays_unpublished_outbox(
    database_url: str, redis_url: str
) -> None:
    """真实 Redis 清库后，relay 仍从 PostgreSQL 未发布 Outbox 重投唯一任务标识。"""
    now = datetime(2026, 8, 5, tzinfo=UTC)
    task_id = uuid4()
    sessions = build_session_factory(database_url)
    redis = Redis.from_url(redis_url)
    enqueued: list[object] = []

    class Enqueuer:
        """记录 relay 边界，不将测试 payload 放入 Redis。"""

        async def enqueue(self, queued_task_id: object, **_: object) -> None:
            """保存 relay 提交的任务 ID。"""
            enqueued.append(queued_task_id)

    try:
        async with sessions.begin() as session:
            user = UserModel(
                email=f"redis-loss-{uuid4().hex}@example.test",
                display_name="Redis Loss",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            session.add_all(
                (
                    TaskRunModel(
                        id=task_id,
                        user_id=user.id,
                        kind="daily_brief",
                        status=TaskStatus.QUEUED.value,
                        idempotency_key=f"redis-loss:{task_id}",
                        input_payload={},
                    ),
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=task_id,
                        deduplication_key=f"task.execute:{task_id}:redis-loss",
                        payload={"task_id": str(task_id)},
                        available_at=now,
                    ),
                )
            )
        await redis.set("aiemployee-task20:ephemeral", "lost", ex=60)
        await redis.flushdb()
        relay = OutboxRelay(
            store=SqlAlchemyOutboxStore(sessions),
            enqueuer=Enqueuer(),
            clock=lambda: now,
            claim_ttl=timedelta(seconds=30),
            retry_base=timedelta(seconds=1),
            retry_max=timedelta(seconds=4),
        )
        assert await relay.relay_once() == 1
        async with sessions() as session:
            task = await session.get(TaskRunModel, task_id)
            event = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
            )
        assert task is not None and task.status == TaskStatus.QUEUED.value
        assert event is not None and event.published_at == now
        assert enqueued == [task_id]
    finally:
        await redis.aclose()
        await sessions.dispose()
