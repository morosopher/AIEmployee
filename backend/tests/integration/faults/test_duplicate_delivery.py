"""在真实 PostgreSQL 上验证重复投递的租约与业务事实边界。"""

from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory


@pytest.mark.asyncio
async def test_duplicate_delivery_has_one_terminal_business_fact(database_url: str) -> None:
    """第二个 owner 未获 PostgreSQL 租约时不得写第二份终态审计事实。

    此测试刻意不使用内存 Store：两个执行者分别经由独立短事务竞争同一个真实
    ``UPDATE ... RETURNING`` 条件更新，证明 Redis/Taskiq 的至少一次消息不会成为
    第二个业务结果来源。
    """
    task_id = uuid4()
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session_factory = build_session_factory(database_url)
    try:
        await _create_queued_task(session_factory, task_id)
        store = SqlAlchemyTaskExecutionStore(session_factory)
        first = await store.acquire(
            task_id=task_id,
            lease_owner="first",
            now=now,
            lease_expires_at=now + timedelta(seconds=30),
        )
        duplicate = await store.acquire(
            task_id=task_id,
            lease_owner="duplicate",
            now=now,
            lease_expires_at=now + timedelta(seconds=30),
        )
        assert first is not None
        assert duplicate is None
        assert await store.finish(
            task_id=task_id,
            lease_owner="first",
            status=TaskStatus.SUCCEEDED,
            finished_at=now,
            error_code=None,
        )
        assert not await store.finish(
            task_id=task_id,
            lease_owner="duplicate",
            status=TaskStatus.SUCCEEDED,
            finished_at=now,
            error_code=None,
        )
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            terminal_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == task_id,
                    AuditEventModel.event_type == "task.succeeded",
                )
            )
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert terminal_count == 1
    finally:
        await session_factory.dispose()


async def _create_queued_task(session_factory: object, task_id: UUID) -> None:
    """创建最小真实任务行；用户归属是 TaskRun 非空外键的一部分。"""
    async with session_factory.begin() as session:  # type: ignore[union-attr]
        user = UserModel(
            email=f"duplicate-{uuid4().hex}@example.test",
            display_name="Duplicate",
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
                status=TaskStatus.QUEUED.value,
                idempotency_key=f"duplicate:{task_id}",
                input_payload={},
            )
        )
