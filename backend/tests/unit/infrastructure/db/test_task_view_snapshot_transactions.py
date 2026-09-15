"""验证任务命令返回的快照不会混用写事务的多个读取视图。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import NoReturn
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.task_views import TaskSnapshot
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.task_views import SqlAlchemyTaskViewStore


class _WriteSession(AsyncSession):
    """仅模拟命令事务所需的 ORM 行为，并记录事务何时已提交。"""

    def __init__(self, scalar_values: list[object | None]) -> None:
        """按调用顺序返回标量查询结果，避免该单测依赖 PostgreSQL。"""
        super().__init__()
        self._scalar_values = iter(scalar_values)

    async def scalar(self, *_: object, **__: object) -> object | None:
        """返回预设查询结果，使测试专注命令和读视图边界。"""
        return next(self._scalar_values)

    async def flush(self, *_: object, **__: object) -> None:
        """命令逻辑只需要 flush 完成，不需要真实数据库 I/O。"""

    async def execute(self, *_: object, **__: object) -> NoReturn:
        """重试状态更新不产生结果，测试不会读取其返回值。"""
        raise AssertionError("retry test must replace execute before use")

    def add(self, _: object, **__: object) -> None:
        """接收审计行，保持测试与持久化实现解耦。"""

    def add_all(self, _: object) -> None:
        """接收重试创建的审计和 Outbox 事实。"""


class _ReadSession(AsyncSession):
    """记录 PostgreSQL 一致性读取启动语句的专用 reader 会话。"""

    def __init__(self) -> None:
        """初始化尚未设置隔离级别的只读会话。"""
        super().__init__()
        self.isolation_statement: str | None = None

    async def execute(self, statement: object, **_: object) -> None:
        """记录首条隔离级别语句，不发起真实数据库 I/O。"""
        self.isolation_statement = str(statement)


class _SessionFactory:
    """按顺序提供写、读会话，明确写入提交后才允许读快照。"""

    def __init__(self, write_session: _WriteSession, read_session: _ReadSession) -> None:
        """保存命令和快照会话，禁止把二者错误合并为同一事务。"""
        self._sessions = iter((write_session, read_session))
        self.committed = False

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncSession]:
        """先提交原子写入，再允许独立 reader 固定 PostgreSQL 一致视图。"""
        session = next(self._sessions)
        yield session
        if isinstance(session, _WriteSession):
            self.committed = True


def _snapshot(task_id: UUID) -> TaskSnapshot:
    """构造仅用于身份断言的不可变 REST 快照。"""
    return TaskSnapshot(
        id=task_id,
        kind="daily_brief",
        status=TaskStatus.QUEUED,
        retry_of_task_id=None,
        input_payload={},
        error_code=None,
        event_cursor=3,
        steps=(),
    )


@pytest.mark.asyncio
async def test_cancel_reads_returned_snapshot_after_atomic_write_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消响应必须由写后独立的固定快照读取产生，不能复用 READ COMMITTED 写事务。"""
    task_id = uuid4()
    user_id = uuid4()
    write_session = _WriteSession(
        [
            TaskRunModel(
                id=task_id,
                user_id=user_id,
                kind="daily_brief",
                status=TaskStatus.QUEUED.value,
                idempotency_key="cancel-snapshot",
                input_payload={},
            ),
            # 取消事务先锁 Task 再复核 User；活动用户前提使本例仍聚焦提交后的快照边界。
            True,
        ]
    )
    read_session = _ReadSession()
    factory = _SessionFactory(write_session, read_session)
    store = SqlAlchemyTaskViewStore(factory)  # type: ignore[arg-type]
    committed_snapshot = _snapshot(task_id)

    async def read_after_commit(session: object, **_: object) -> TaskSnapshot:
        """要求提交和 ``REPEATABLE READ`` 已生效，证明快照不是写事务内读取。"""
        assert factory.committed is True
        assert session is read_session
        assert read_session.isolation_statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        return committed_snapshot

    monkeypatch.setattr(store, "_get_in_session", AsyncMock(side_effect=read_after_commit))

    result = await store.cancel(
        task_id=task_id,
        user_id=user_id,
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert result is committed_snapshot


@pytest.mark.asyncio
async def test_retry_reads_returned_snapshot_after_atomic_write_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重试响应同样必须在原子任务、审计和 Outbox 写入提交后建立读取基线。"""
    original_id = uuid4()
    replacement_id = uuid4()
    user_id = uuid4()
    write_session = _WriteSession(
        [
            TaskRunModel(
                id=original_id,
                user_id=user_id,
                kind="daily_brief",
                status=TaskStatus.FAILED.value,
                idempotency_key="failed-task",
                input_payload={},
            ),
            None,
            replacement_id,
        ]
    )
    write_session.execute = AsyncMock(return_value=None)  # type: ignore[method-assign]
    read_session = _ReadSession()
    factory = _SessionFactory(write_session, read_session)
    store = SqlAlchemyTaskViewStore(factory)  # type: ignore[arg-type]
    committed_snapshot = _snapshot(replacement_id)

    async def read_after_commit(session: object, **_: object) -> TaskSnapshot:
        """读取开始前要求提交和固定隔离级别，防止 cursor 超前于快照状态。"""
        assert factory.committed is True
        assert session is read_session
        assert read_session.isolation_statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        return committed_snapshot

    monkeypatch.setattr(store, "_get_in_session", AsyncMock(side_effect=read_after_commit))

    result = await store.retry(
        task_id=original_id,
        user_id=user_id,
        idempotency_key="retry-snapshot",
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert result is committed_snapshot


@pytest.mark.asyncio
async def test_retry_existing_replacement_reads_snapshot_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相同重试键复用 replacement 时，返回快照仍须进入独立可重复读事务。"""
    original_id = uuid4()
    replacement_id = uuid4()
    user_id = uuid4()
    write_session = _WriteSession(
        [
            TaskRunModel(
                id=original_id,
                user_id=user_id,
                kind="daily_brief",
                status=TaskStatus.FAILED.value,
                idempotency_key="failed-task",
                input_payload={},
            ),
            TaskRunModel(
                id=replacement_id,
                user_id=user_id,
                kind="daily_brief",
                status=TaskStatus.QUEUED.value,
                idempotency_key="existing-replacement",
                input_payload={},
            ),
        ]
    )
    read_session = _ReadSession()
    factory = _SessionFactory(write_session, read_session)
    store = SqlAlchemyTaskViewStore(factory)  # type: ignore[arg-type]
    committed_snapshot = _snapshot(replacement_id)

    async def read_after_commit(session: object, **_: object) -> TaskSnapshot:
        """验证已有 replacement 也不会在默认写事务中组装快照。"""
        assert factory.committed is True
        assert session is read_session
        assert read_session.isolation_statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        return committed_snapshot

    monkeypatch.setattr(store, "_get_in_session", AsyncMock(side_effect=read_after_commit))

    result = await store.retry(
        task_id=original_id,
        user_id=user_id,
        idempotency_key="retry-existing",
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert result is committed_snapshot


@pytest.mark.asyncio
async def test_retry_conflict_winner_reads_snapshot_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """唯一约束冲突后读取胜出 replacement 时，响应也必须保持固定一致性视图。"""
    original_id = uuid4()
    replacement_id = uuid4()
    user_id = uuid4()
    write_session = _WriteSession(
        [
            TaskRunModel(
                id=original_id,
                user_id=user_id,
                kind="daily_brief",
                status=TaskStatus.FAILED.value,
                idempotency_key="failed-task",
                input_payload={},
            ),
            None,
            None,
            TaskRunModel(
                id=replacement_id,
                user_id=user_id,
                kind="daily_brief",
                status=TaskStatus.QUEUED.value,
                idempotency_key="winning-replacement",
                input_payload={},
            ),
        ]
    )
    read_session = _ReadSession()
    factory = _SessionFactory(write_session, read_session)
    store = SqlAlchemyTaskViewStore(factory)  # type: ignore[arg-type]
    committed_snapshot = _snapshot(replacement_id)

    async def read_after_commit(session: object, **_: object) -> TaskSnapshot:
        """验证冲突胜出者在提交后才被用于 REST/SSE 游标基线。"""
        assert factory.committed is True
        assert session is read_session
        assert read_session.isolation_statement == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        return committed_snapshot

    monkeypatch.setattr(store, "_get_in_session", AsyncMock(side_effect=read_after_commit))

    result = await store.retry(
        task_id=original_id,
        user_id=user_id,
        idempotency_key="retry-conflict-winner",
        now=datetime(2026, 8, 3, tzinfo=UTC),
    )

    assert result is committed_snapshot
