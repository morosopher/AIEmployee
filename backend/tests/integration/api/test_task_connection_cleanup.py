"""以真实 PostgreSQL 锁与 SSE 断线回归连接归还，不调用供应商或输出业务载荷。

健康检查仅在本测试驱动连接上把空语句替换为只读锁屏障；取消、失效、归还和关闭仍由
真实 SQLAlchemy/asyncpg 执行。失败后的兜底仅回收本轮捕获的精确连接，不参与通过判断。
"""

import asyncio
import gc
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import event, text
from sqlalchemy.pool import AsyncAdaptedQueuePool
from sqlalchemy.util import await_only
from sse_starlette import EventSourceResponse, ServerSentEvent

from ai_employee.api import sse
from ai_employee.api.sse import TaskEventStore
from ai_employee.infrastructure.db.repositories.task_views import SqlAlchemyTaskViewStore
from ai_employee.infrastructure.db.session import build_session_factory


@pytest.mark.parametrize("operation", ["oldest", "events", "snapshot"])
@pytest.mark.parametrize("stage", ["query", "pre_ping"])
@pytest.mark.parametrize("cancellation", ["disconnect", "single_cancel"])
async def test_sse_disconnect_returns_blocked_query_connection(
    database_url: str,
    caplog: pytest.LogCaptureFixture,
    recwarn: pytest.WarningsRecorder,
    operation: str,
    stage: str,
    cancellation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """等待真实锁后取消，断线处理结束时连接、服务器事务及读取任务均已回收。

    Args:
        database_url: 正式 fixture 已验证的专用数据库，不输出原值。
        caplog: 保留终止错误，禁止用过滤日志掩盖连接问题。
        recwarn: 捕获但不忽略未归还连接的 GC 告警。
        operation: SSE 的三种有限数据库读取边界。
        stage: 在执行查询或复用连接预检查期间精确取消。
        cancellation: 真实 EventSourceResponse 断线与单次 asyncio 取消对照。
        monkeypatch: 只扩大该测试连接健康检查的真实网络等待窗口。
    """
    sessions = build_session_factory(database_url)
    blocker = build_session_factory(database_url)
    original_tasks = asyncio.all_tasks()
    pool = sessions.engine.pool
    assert isinstance(pool, AsyncAdaptedQueuePool)
    lifecycle: list[str] = []
    backend_pids: list[int] = []
    checked_out = asyncio.Event()
    disconnect = asyncio.Event()

    def observe(name: str) -> Any:
        """第三方事件签名适配只采集固定事件名与本测试驱动 PID。"""
        def listener(connection: Any, *args: Any) -> None:
            """真实 checkout 确立本测试连接身份；不保存驱动引用以免干扰 GC。"""
            del args
            lifecycle.append(name)
            if name == "checkout":
                backend_pids.append(connection.driver_connection.get_server_pid())
                checked_out.set()
        return listener

    for name in ("checkout", "checkin", "invalidate", "close", "close_detached"):
        event.listen(pool, name, observe(name))
    store = TaskEventStore(sessions, SqlAlchemyTaskViewStore(sessions))
    task_id, user_id = uuid4(), uuid4()

    async def read() -> None:
        """直接调用实际生产读取入口；同一实现用于两种取消方式。"""
        if operation == "oldest":
            await store.oldest_event_id(task_id=task_id, user_id=user_id)
        elif operation == "events":
            await store.events_after(task_id=task_id, user_id=user_id, after_id=0)
        else:
            await store.snapshot_and_max(task_id=task_id, user_id=user_id)

    async def events() -> AsyncIterator[ServerSentEvent]:
        """保留真实 SSE TaskGroup/断线取消，不替换生产资源清理。"""
        await read()
        yield ServerSentEvent(event="probe", data="done")

    async def receive() -> dict[str, str]:
        """仅在数据库锁屏障证实网络等待后发送断线。"""
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: object) -> None:
        """消费帧但不记录内容。"""
        del message

    task: asyncio.Task[None] | None = None
    try:
        if stage == "pre_ping":
            # 新连接不触发 pre_ping；预热并正常归还，下一次借出才覆盖该窗口。
            async with sessions() as warmup:
                await warmup.execute(text("SELECT 1"))
            original_fetchrow = asyncpg.Connection.fetchrow

            async def blocked_ping(connection: Any, query: str, *args: Any, **kwargs: Any) -> Any:
                """保留原驱动协议与连接池异常路径，只让健康检查等待确定的表锁。"""
                if query == ";" and connection.get_server_pid() == backend_pids[0]:
                    query = "SELECT 1 FROM audit_events LIMIT 1"
                return await original_fetchrow(connection, query, *args, **kwargs)

            monkeypatch.setattr(asyncpg.Connection, "fetchrow", blocked_ping)
        async with blocker() as lock_session:
            await lock_session.execute(text("LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE"))
            response = EventSourceResponse(events())
            task = asyncio.create_task(
                read() if cancellation == "single_cancel"
                else response({"type": "http"}, receive, send)
            )
            async with asyncio.timeout(10):
                await checked_out.wait()
                while not await lock_session.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = :pid AND NOT granted)"),
                    {"pid": backend_pids[0]},
                ):
                    await asyncio.sleep(0.01)
                if cancellation == "single_cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    disconnect.set()
                    await task

        # 归还必须在 Response/读取结束前完成，不靠 sleep 或 GC 修正池计数。
        checkedout_before_gc = pool.checkedout()
        error_count = sum(
            record.name.startswith("sqlalchemy.pool") and record.levelno >= logging.ERROR
            for record in caplog.records
        )
        # caplog traceback 会延长连接代理生命周期；清掉捕获器引用但保留错误计数和日志。
        for record in caplog.records:
            record.exc_info = None
            record.exc_text = None
        gc.collect()
        await asyncio.sleep(0)
        warning_count = sum("non-checked-in" in str(item.message) for item in recwarn)
        assert checkedout_before_gc == 0
        assert warning_count == 0
        assert error_count == 0
        assert lifecycle.count("checkin") == (2 if stage == "pre_ping" else 1)
        async with blocker() as inspection:
            assert await inspection.scalar(
                text("SELECT COUNT(*) FROM pg_stat_activity WHERE pid = :pid "
                     "AND datname = current_database() AND state LIKE 'idle in transaction%'"),
                {"pid": backend_pids[0]},
            ) == 0
        assert not [
            pending for pending in asyncio.all_tasks() - original_tasks
            if not any(frame.f_code.co_name == "_shutdown_watcher" for frame in pending.get_stack())
        ]
    finally:
        monkeypatch.undo()
        if task is not None and not task.done():
            disconnect.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await sessions.dispose()
        # RED 中服务器可能仍有事务，而 pool 已丢失资源引用；按本测试捕获的 PID 且
        # 当前 disposable 数据库精确兜底，不终止未知连接，也不把兜底后状态计作通过。
        async with blocker() as cleanup:
            for pid in set(backend_pids):
                await cleanup.execute(
                    text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                         "WHERE pid = :pid AND datname = current_database()"),
                    {"pid": pid},
                )
        await blocker.dispose()
        # 无 ASGI lifespan 的独立 Response 测试拥有本轮库级 watcher，必须自行收回。
        watchers = [
            pending for pending in asyncio.all_tasks() - original_tasks
            if any(frame.f_code.co_name == "_shutdown_watcher" for frame in pending.get_stack())
        ]
        for watcher in watchers:
            watcher.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)


async def test_close_timeout_returns_connection_after_session_transaction_is_removed(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    recwarn: pytest.WarningsRecorder,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """在真实 rollback 事件处耗尽收尾预算，验证保留连接引用可完成失效归还。

    SQLAlchemy 在此事件之前已从 Session 移除事务；仅再次 session.invalidate 无法
    找回原连接。零预算在事件中的 await 确定触发，不依赖网络延迟或真实计时等待。
    """
    sessions = build_session_factory(database_url)
    pool = sessions.engine.pool
    assert isinstance(pool, AsyncAdaptedQueuePool)
    blocked = asyncio.Event()

    def pause_rollback(connection: object) -> None:
        """使用公开连接事件阻塞回滚，不替换失效／归还或驱动实现。"""
        del connection
        await_only(blocked.wait())

    event.listen(sessions.engine.sync_engine, "rollback", pause_rollback)
    monkeypatch.setattr(sse, "SSE_DATABASE_CLOSE_SECONDS", 0)
    try:
        store = TaskEventStore(sessions, SqlAlchemyTaskViewStore(sessions))
        with pytest.raises(TimeoutError):
            await store.oldest_event_id(task_id=uuid4(), user_id=uuid4())
        assert pool.checkedout() == 0
        gc.collect()
        await asyncio.sleep(0)
        assert not any("non-checked-in" in str(item.message) for item in recwarn)
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    finally:
        blocked.set()
        event.remove(sessions.engine.sync_engine, "rollback", pause_rollback)
        await sessions.dispose()
