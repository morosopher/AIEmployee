"""在真实 PostgreSQL 上覆盖丢失租约后禁止提交。"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.repositories.task_retry_recovery import (
    SqlAlchemyTaskRetryRecoveryStore,
)
from ai_employee.infrastructure.db.session import build_session_factory


@pytest.mark.asyncio
async def test_worker_losing_lease_cannot_commit_terminal_result(database_url: str) -> None:
    """过期 owner 被接管后，其终态 CAS 不命中且不得覆盖新持有人。"""
    now = datetime(2026, 8, 5, tzinfo=UTC)
    task_id = uuid4()
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email=f"lease-{uuid4().hex}@example.test",
                display_name="Lease",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            session.add(
                TaskRunModel(
                    id=task_id,
                    user_id=user.id,
                    kind="daily_brief",
                    status=TaskStatus.RUNNING.value,
                    lease_owner="expired-owner",
                    lease_expires_at=now - timedelta(seconds=1),
                    idempotency_key=f"lease:{task_id}",
                    input_payload={},
                )
            )
        store = SqlAlchemyTaskExecutionStore(session_factory)
        assert (
            await store.acquire(
                task_id=task_id,
                lease_owner="new-owner",
                now=now,
                lease_expires_at=now + timedelta(seconds=30),
            )
            is not None
        )
        assert not await store.finish(
            task_id=task_id,
            lease_owner="expired-owner",
            status=TaskStatus.SUCCEEDED,
            finished_at=now,
            error_code=None,
        )
        async with session_factory() as session:
            task = await session.scalar(select(TaskRunModel).where(TaskRunModel.id == task_id))
        assert (
            task is not None
            and task.status == TaskStatus.RUNNING.value
            and task.lease_owner == "new-owner"
        )
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (TaskStatus.QUEUED, TaskStatus.RUNNING))
async def test_stale_task_with_published_outbox_gets_exactly_one_recovery_event(
    database_url: str, status: TaskStatus
) -> None:
    """已发布的旧消息不能抑制 PostgreSQL 对陈旧 QUEUED/RUNNING 任务的补投。"""
    now = datetime(2026, 8, 5, 12, 3, tzinfo=UTC)
    task_id = uuid4()
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email=f"recovery-{uuid4().hex}@example.test",
                display_name="Recovery",
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
                        status=status.value,
                        lease_owner="crashed" if status is TaskStatus.RUNNING else None,
                        lease_expires_at=now - timedelta(seconds=1)
                        if status is TaskStatus.RUNNING
                        else None,
                        idempotency_key=f"recovery:{task_id}",
                        input_payload={},
                        updated_at=now - timedelta(minutes=6),
                    ),
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=task_id,
                        deduplication_key=f"task.execute:{task_id}:initial",
                        payload={"task_id": str(task_id)},
                        published_at=now - timedelta(minutes=1),
                    ),
                )
            )
        store = SqlAlchemyTaskRetryRecoveryStore(session_factory)
        assert await store.recover_due(now=now, limit=10) == 1
        assert await store.recover_due(now=now, limit=10) == 0
        async with session_factory() as session:
            recovery_events = (
                await session.scalars(
                    select(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id == task_id,
                        OutboxEventModel.published_at.is_(None),
                    )
                )
            ).all()
        assert [event.deduplication_key for event in recovery_events] == [
            f"task.execute:{task_id}:recovery:2026-08-05T12:00:00+00:00"
        ]
    finally:
        await session_factory.dispose()
