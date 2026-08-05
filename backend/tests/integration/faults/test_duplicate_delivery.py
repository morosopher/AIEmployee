"""在真实 PostgreSQL 上验证重复投递的租约与业务事实边界。"""

import asyncio
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask
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


@pytest.mark.asyncio
async def test_duplicate_runner_delivery_executes_counted_step_once(database_url: str) -> None:
    """两个真实 Runner 收到同一 Taskiq 投递时只有获租者执行副作用并提交终态。

    测试以受控异步门让首个 Runner 停留在实际 ``TaskExecutionStep.execute`` 内，再让
    第二个 Runner 经相同 ``DurableTaskRunner`` 路径竞争 PostgreSQL 租约。这样既验证
    Store 的条件更新，也验证 Worker 编排不会在未获租时调用可计数的工具。
    """
    task_id = uuid4()
    now = datetime(2026, 8, 5, tzinfo=UTC)
    session_factory = build_session_factory(database_url)
    started = asyncio.Event()
    release = asyncio.Event()
    step = _CountingTaskStep(started=started, release=release)
    try:
        await _create_queued_task(session_factory, task_id)
        store = SqlAlchemyTaskExecutionStore(session_factory)
        runner = DurableTaskRunner(
            store=store,
            clock=lambda: now,
            lease_duration=timedelta(seconds=30),
            task_timeout_seconds=60,
            task_step_timeout_seconds=30,
            max_transient_retries=0,
            resolve_steps=lambda _task: (step,),
        )
        first_delivery = asyncio.create_task(runner.run(task_id, lease_owner="first-worker"))
        await asyncio.wait_for(started.wait(), timeout=1)

        duplicate_delivery = await runner.run(task_id, lease_owner="duplicate-worker")
        assert duplicate_delivery is False
        assert step.calls == 1

        release.set()
        assert await first_delivery is True

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
        assert step.calls == 1
    finally:
        await session_factory.dispose()


class _CountingTaskStep:
    """阻塞一次合成工具调用，暴露重复投递是否越过 Runner 租约边界。"""

    name = "counted_fake_tool"

    def __init__(self, *, started: asyncio.Event, release: asyncio.Event) -> None:
        """保存测试同步原语；它们不代表生产任务状态。"""
        self._started = started
        self._release = release
        self.calls = 0

    async def execute(self, task: LeasedTask) -> None:
        """记录一次副作用并等待测试允许拥有租约的执行者完成。

        Args:
            task: 当前持久任务快照；本 fake 不读取业务载荷。
        """
        del task
        self.calls += 1
        self._started.set()
        await self._release.wait()


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
