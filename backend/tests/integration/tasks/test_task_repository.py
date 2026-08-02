"""在真实 PostgreSQL 上验证任务创建的原子性、幂等性与用户隔离。"""

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.tasks import (
    CreateTaskResult,
    CreateTaskUseCase,
    TaskRepository,
)
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories import tasks as task_repository_module
from ai_employee.infrastructure.db.repositories.tasks import (
    SqlAlchemyTaskRepository,
    SqlAlchemyTaskRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory


class FirstSelectBarrierSession:
    """只在真实 Session 的首次 ``scalar`` 查询完成后同步两个并发调用。

    代理不伪造查询、flush 或约束结果；首次幂等 SELECT 仍由 PostgreSQL 执行，随后两个
    事务在 barrier 汇合，因而都已经观察到“尚不存在”。``Any`` 只用于忠实转发
    SQLAlchemy 第三方 Session 的泛型参数与返回边界，Repository 看到的仍是原 API。
    """

    def __init__(self, session: AsyncSession, barrier: asyncio.Barrier) -> None:
        """绑定一个独立真实 Session 及本测试共享的两方屏障。"""
        self._session = session
        self._barrier = barrier
        self._first_scalar_completed = False

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        """委托真实查询，并只在首次返回后等待另一个事务。"""
        result = await self._session.scalar(*args, **kwargs)
        if not self._first_scalar_completed:
            self._first_scalar_completed = True
            assert result is None
            await self._barrier.wait()
        return result

    def add_all(self, instances: Iterable[object]) -> None:
        """把 ORM 对象原样加入真实 Session。"""
        self._session.add_all(instances)

    async def flush(self) -> None:
        """在真实事务中执行 flush，不模拟唯一约束或错误。"""
        await self._session.flush()


class BarrierTaskRepositoryFactory:
    """为并发测试的每次调用创建独立事务和首次查询屏障代理。"""

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        barrier: asyncio.Barrier,
    ) -> None:
        """保存真实 Session factory，并记录实际创建的 Session 标识。"""
        self._session_factory = session_factory
        self._barrier = barrier
        self.session_ids: list[int] = []

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[TaskRepository]:
        """开启独立外层事务，并仅包装本次调用的首次幂等查询。"""
        async with self._session_factory.begin() as session:
            self.session_ids.append(id(session))
            proxy = FirstSelectBarrierSession(session, self._barrier)
            yield SqlAlchemyTaskRepository(cast(AsyncSession, proxy))


class CommitObservingDispatcher:
    """从新 Session 验证 Task/Outbox 已提交，再记录 dispatcher 调用。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存真实 Session factory，并初始化提交后观察记录。"""
        self._session_factory = session_factory
        self.calls: list[UUID] = []

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """只观察已提交事实，不模拟 Redis，适用于 Task 6 纯持久化测试。

        Args:
            task_id: 创建事务返回的稳定任务标识。

        Returns:
            新 Session 读取到的持久任务状态。
        """
        async with self._session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            outbox = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == task_id)
            )
        assert task is not None
        assert outbox is not None
        self.calls.append(task_id)
        return TaskStatus(task.status)


def _synthetic_user(*, email: str, display_name: str) -> UserModel:
    """构造不含真实个人资料且使用显式 UTC 偏好的测试用户。"""
    return UserModel(
        email=email,
        display_name=display_name,
        password_hash=None,
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


def _assert_utc(value: datetime) -> None:
    """确认数据库返回的是显式 UTC 时间，而不是宿主机本地时间。"""
    assert value.utcoffset() == timedelta(0)


def _postgres_constraint_name(error: IntegrityError) -> str | None:
    """从 asyncpg 原始异常链提取已命名 PostgreSQL 约束。

    SQLAlchemy 的 asyncpg 适配器保留 ``UniqueViolationError`` 为 ``orig`` 的 cause；
    ``getattr`` 只存在于这个第三方异常边界，返回值立即收窄为字符串。
    """
    if error.orig is None:
        return None
    constraint_name = getattr(error.orig.__cause__, "constraint_name", None)
    return constraint_name if isinstance(constraint_name, str) else None


@pytest.mark.asyncio
async def test_create_task_commits_task_audit_and_outbox_once(database_url: str) -> None:
    """首次调用原子写入三类事实，顺序重放同一键只返回原任务。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(
                email="task-owner@example.com",
                display_name="Task Owner",
            )
            session.add(user)
            await session.flush()
            user_id = user.id

        dispatcher = CommitObservingDispatcher(session_factory)
        use_case = CreateTaskUseCase(
            SqlAlchemyTaskRepositoryFactory(session_factory),
            dispatcher=dispatcher,
        )
        created = await use_case.execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-07-30"},
            idempotency_key="brief:user:2026-07-30:scheduled",
        )
        repeated = await use_case.execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-07-30"},
            idempotency_key="brief:user:2026-07-30:scheduled",
        )

        assert repeated.task_id == created.task_id
        assert dispatcher.calls == [created.task_id, created.task_id]

        # 必须从新 Session 读取提交后的事实，避免把同一 identity map 的未提交对象误判为成功。
        async with session_factory() as session:
            tasks = (
                await session.scalars(select(TaskRunModel).where(TaskRunModel.user_id == user_id))
            ).all()
            audits = (
                await session.scalars(
                    select(AuditEventModel).where(AuditEventModel.user_id == user_id)
                )
            ).all()
            outbox_events = (
                await session.scalars(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == created.task_id)
                )
            ).all()

        assert len(tasks) == 1
        assert isinstance(created.task_id, UUID)
        assert tasks[0].id == created.task_id
        assert tasks[0].status == TaskStatus.CREATED.value
        assert tasks[0].kind == "daily_brief"
        assert tasks[0].input_payload == {"local_date": "2026-07-30"}
        assert tasks[0].attempt_count == 0
        _assert_utc(tasks[0].created_at)
        _assert_utc(tasks[0].updated_at)

        assert len(audits) == 1
        assert audits[0].task_id == created.task_id
        assert audits[0].event_type == "task.created"
        assert audits[0].event_metadata == {
            "kind": "daily_brief",
            "status": TaskStatus.CREATED.value,
        }
        _assert_utc(audits[0].created_at)

        assert len(outbox_events) == 1
        assert outbox_events[0].topic == "task.execute"
        assert outbox_events[0].aggregate_id == created.task_id
        assert outbox_events[0].deduplication_key == (f"task.execute:{created.task_id}:initial")
        assert outbox_events[0].payload == {"task_id": str(created.task_id)}
        assert outbox_events[0].attempt_count == 0
        assert outbox_events[0].published_at is None
        _assert_utc(outbox_events[0].available_at)
        _assert_utc(outbox_events[0].created_at)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_same_idempotency_key_is_isolated_by_user(database_url: str) -> None:
    """两个用户复用同一幂等键时必须各自创建任务，不能跨用户错误复用。"""
    session_factory = build_session_factory(database_url)
    shared_key = "brief:shared-date:scheduled"
    try:
        async with session_factory.begin() as session:
            first_user = _synthetic_user(
                email="first-task-owner@example.com",
                display_name="First Task Owner",
            )
            second_user = _synthetic_user(
                email="second-task-owner@example.com",
                display_name="Second Task Owner",
            )
            session.add_all((first_user, second_user))
            await session.flush()
            first_user_id = first_user.id
            second_user_id = second_user.id

        dispatcher = CommitObservingDispatcher(session_factory)
        use_case = CreateTaskUseCase(
            SqlAlchemyTaskRepositoryFactory(session_factory),
            dispatcher=dispatcher,
        )
        first = await use_case.execute(
            user_id=first_user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-07-30"},
            idempotency_key=shared_key,
        )
        second = await use_case.execute(
            user_id=second_user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-07-30"},
            idempotency_key=shared_key,
        )

        assert first.task_id != second.task_id
        assert dispatcher.calls == [first.task_id, second.task_id]

        async with session_factory() as session:
            task_count = await session.scalar(select(func.count()).select_from(TaskRunModel))
            audit_count = await session.scalar(select(func.count()).select_from(AuditEventModel))
            outbox_count = await session.scalar(select(func.count()).select_from(OutboxEventModel))
            first_task = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.id == first.task_id,
                    TaskRunModel.user_id == first_user_id,
                )
            )
            second_task = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.id == second.task_id,
                    TaskRunModel.user_id == second_user_id,
                )
            )

        assert task_count == 2
        assert audit_count == 2
        assert outbox_count == 2
        assert first_task is not None
        assert second_task is not None
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_concurrent_same_idempotency_key_returns_one_task(database_url: str) -> None:
    """两个都先读到空值的真实事务必须返回同一任务，而不是让输家泄漏唯一约束错误。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(
                email="concurrent-task-owner@example.com",
                display_name="Concurrent Task Owner",
            )
            session.add(user)
            await session.flush()
            user_id = user.id

        repositories = BarrierTaskRepositoryFactory(session_factory, asyncio.Barrier(2))
        dispatcher = CommitObservingDispatcher(session_factory)
        use_case = CreateTaskUseCase(repositories, dispatcher=dispatcher)
        results = await asyncio.gather(
            use_case.execute(
                user_id=user_id,
                kind="daily_brief",
                input_payload={"local_date": "2026-07-30"},
                idempotency_key="brief:concurrent:2026-07-30:scheduled",
            ),
            use_case.execute(
                user_id=user_id,
                kind="daily_brief",
                input_payload={"local_date": "2026-07-30"},
                idempotency_key="brief:concurrent:2026-07-30:scheduled",
            ),
            return_exceptions=True,
        )

        result_types = sorted(type(result).__name__ for result in results)
        assert result_types == ["CreateTaskResult", "CreateTaskResult"]
        first, second = results
        assert isinstance(first, CreateTaskResult)
        assert isinstance(second, CreateTaskResult)
        assert first.task_id == second.task_id
        assert len(repositories.session_ids) == 2
        assert repositories.session_ids[0] != repositories.session_ids[1]
        assert len(dispatcher.calls) == 2
        assert set(dispatcher.calls) == {first.task_id}

        async with session_factory() as session:
            task_count = await session.scalar(select(func.count()).select_from(TaskRunModel))
            audit_count = await session.scalar(select(func.count()).select_from(AuditEventModel))
            outbox_count = await session.scalar(select(func.count()).select_from(OutboxEventModel))

        assert task_count == 1
        assert audit_count == 1
        assert outbox_count == 1
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_non_task_unique_error_rolls_back_all_new_facts(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outbox 唯一冲突必须原样抛出，且同事务内的新 TaskRun 与 AuditEvent 一并回滚。"""
    session_factory = build_session_factory(database_url)
    fixed_task_id = uuid4()
    deduplication_key = f"task.execute:{fixed_task_id}:initial"
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(
                email="rollback-task-owner@example.com",
                display_name="Rollback Task Owner",
            )
            session.add(user)
            session.add(
                OutboxEventModel(
                    topic="synthetic.preexisting",
                    aggregate_id=uuid4(),
                    deduplication_key=deduplication_key,
                    payload={"synthetic": True},
                )
            )
            await session.flush()
            user_id = user.id

        # 固定应用生成的任务 UUID，让真实 Outbox 唯一约束稳定命中；flush 与数据库不模拟。
        monkeypatch.setattr(task_repository_module, "uuid4", lambda: fixed_task_id)
        dispatcher = CommitObservingDispatcher(session_factory)
        use_case = CreateTaskUseCase(
            SqlAlchemyTaskRepositoryFactory(session_factory),
            dispatcher=dispatcher,
        )
        with pytest.raises(IntegrityError) as raised:
            await use_case.execute(
                user_id=user_id,
                kind="daily_brief",
                input_payload={"local_date": "2026-07-30"},
                idempotency_key="brief:rollback:2026-07-30:scheduled",
            )

        assert _postgres_constraint_name(raised.value) == ("uq_outbox_events_deduplication_key")
        assert dispatcher.calls == []

        async with session_factory() as session:
            task_count = await session.scalar(
                select(func.count())
                .select_from(TaskRunModel)
                .where(
                    TaskRunModel.user_id == user_id,
                    TaskRunModel.idempotency_key == "brief:rollback:2026-07-30:scheduled",
                )
            )
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.user_id == user_id,
                    AuditEventModel.task_id == fixed_task_id,
                )
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(OutboxEventModel.deduplication_key == deduplication_key)
            )

        assert task_count == 0
        assert audit_count == 0
        assert outbox_count == 1
    finally:
        await session_factory.dispose()
