"""用受控会话验证 SSE 读取取消与关闭预算，不连接数据库或依赖真实等待。"""

import asyncio
from typing import Self, cast
from uuid import uuid4

import pytest

from ai_employee.api import sse
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class ControlledConnection:
    """记录关闭预算耗尽后的确切连接失效与归还。"""

    def __init__(self) -> None:
        self.invalidated = False
        self.closed = False

    async def invalidate(self) -> None:
        """模拟驱动失效完成，必须先于最终归还。"""
        self.invalidated = True

    async def close(self) -> None:
        """模拟已失效连接无需等待数据库即可归还。"""
        assert self.invalidated
        self.closed = True


class ControlledSession:
    """以事件分别阻塞查询与关闭，复现取消期间需要继续拥有资源的窗口。"""

    def __init__(self) -> None:
        self.resource = ControlledConnection()
        self.query_started = asyncio.Event()
        self.query_release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.closed = False
        self.query_cancellations = 0
        self.close_cancellations = 0

    async def connection(self) -> ControlledConnection:
        """返回本次会话唯一拥有的连接。"""
        return self.resource

    async def scalar(self, statement: object) -> int:
        """只模拟受控数据库等待；取消次数属于待验证的读取边界行为。"""
        del statement
        self.query_started.set()
        try:
            await self.query_release.wait()
        except asyncio.CancelledError:
            self.query_cancellations += 1
            raise
        return 7

    async def close(self) -> None:
        """把关闭保持在途中，验证外层重复取消不会截断资源回收。"""
        self.close_started.set()
        try:
            await self.close_release.wait()
        except asyncio.CancelledError:
            self.close_cancellations += 1
            raise
        self.closed = True

    async def __aenter__(self) -> Self:
        """兼容原有 session 上下文，使 RED 验证真实旧行为而非接口缺失。"""
        return self

    async def __aexit__(self, *args: object) -> None:
        """旧边界的关闭与新边界直接 close 使用同一受控资源。"""
        await self.close()


def make_store(session: ControlledSession) -> sse.TaskEventStore:
    """只替换数据库工厂，生产 TaskEventStore 查询和取消适配完整执行。"""
    return sse.TaskEventStore(cast(ManagedAsyncSessionMaker, lambda: session), object())


async def test_repeated_parent_cancellation_waits_for_one_owned_cleanup() -> None:
    """查询仅收到一次取消；第二次父取消不得截断关闭或提早返回。"""
    session = ControlledSession()
    task = asyncio.create_task(make_store(session).oldest_event_id(task_id=uuid4(), user_id=uuid4()))
    try:
        await session.query_started.wait()
        task.cancel()
        await session.close_started.wait()
        task.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not task.done()
        assert session.query_cancellations == 1
        assert session.close_cancellations == 0
        session.close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.closed
    finally:
        session.query_release.set()
        session.close_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_close_budget_invalidates_exact_connection_before_returning_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """零预算在下一个 await 确定触发，验证超时不丢弃仍持有的连接。"""
    monkeypatch.setattr(sse, "SSE_DATABASE_CLOSE_SECONDS", 0, raising=False)
    session = ControlledSession()
    session.query_release.set()
    task = asyncio.create_task(make_store(session).oldest_event_id(task_id=uuid4(), user_id=uuid4()))
    try:
        await session.close_started.wait()
        for _ in range(10):
            await asyncio.sleep(0)
        assert task.done()
        with pytest.raises(TimeoutError):
            await task
        assert session.close_cancellations == 1
        assert session.resource.invalidated
        assert session.resource.closed
    finally:
        session.close_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_success_waits_for_session_close_before_returning_value() -> None:
    """普通读取也必须完成会话关闭才交付结果，且不无故失效健康连接。"""
    session = ControlledSession()
    session.query_release.set()
    task = asyncio.create_task(make_store(session).oldest_event_id(task_id=uuid4(), user_id=uuid4()))
    try:
        await session.close_started.wait()
        assert not task.done()
        session.close_release.set()
        assert await task == 7
        assert session.closed
        assert not session.resource.invalidated
    finally:
        session.close_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancelled_read_preserves_cancellation_and_reports_close_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """断线后的关闭超时必须可观测，但不得把外层取消转换为普通超时错误。"""
    monkeypatch.setattr(sse, "SSE_DATABASE_CLOSE_SECONDS", 0)
    session = ControlledSession()
    task = asyncio.create_task(make_store(session).oldest_event_id(task_id=uuid4(), user_id=uuid4()))
    try:
        await session.query_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.resource.closed
        assert "task event database cleanup failed" in caplog.text
    finally:
        session.close_release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_invalidation_failure_still_attempts_close_and_remains_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失效失败也必须尝试归还；不能静默报告连接已被安全清理。"""
    monkeypatch.setattr(sse, "SSE_DATABASE_CLOSE_SECONDS", 0)
    session = ControlledSession()
    session.query_release.set()
    close_attempted = False

    async def fail_invalidate() -> None:
        raise OSError("synthetic cleanup failure")

    async def record_close() -> None:
        nonlocal close_attempted
        close_attempted = True

    monkeypatch.setattr(session.resource, "invalidate", fail_invalidate)
    monkeypatch.setattr(session.resource, "close", record_close)
    with pytest.raises(OSError, match="synthetic cleanup failure"):
        await make_store(session).oldest_event_id(task_id=uuid4(), user_id=uuid4())
    assert close_attempted
