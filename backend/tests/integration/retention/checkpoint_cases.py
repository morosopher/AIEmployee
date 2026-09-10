"""复用真实角色 Checkpoint 并发场景，不创建数据库或改变权限。

所有调用由 test_role_permissions 的官方 disposable lifecycle 驱动。事件只暂停真实
事务边界；pg_blocking_pids 证明实际行锁等待，不用固定 sleep 推测哪一方先执行。
"""

import asyncio
from collections.abc import Awaitable
from datetime import time, timedelta
from typing import Literal
from uuid import UUID, uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import event, select, text

from ai_employee.agents.runner import _GuardedPostgresSaver, postgres_checkpointer
from ai_employee.application.use_cases.privacy import PrivacyDeletionBinding
from ai_employee.application.use_cases.task_execution import TaskLeaseMode
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel, TaskStepModel
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.repositories.task_history import TaskHistoryCleanup
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.workers.privacy import PrivacyDeletionWorker
from tests.integration.privacy.test_all_data_deletion import BARRIER_NOW, _DeletionClock
from tests.integration.privacy.test_deletion_checkpoints import seed_checkpoint_thread

CleanupKind = Literal["privacy", "history"]


async def _seed_tasks(
    app: ManagedAsyncSessionMaker, *, kind: CleanupKind
) -> tuple[UUID, UUID, UUID]:
    """app 真实创建用户和任务所有权；普通历史与删除赢家均使用固定业务时间。"""
    user_id, task_id, winner_id = uuid4(), uuid4(), uuid4()
    async with app.begin() as session:
        session.add(
            UserModel(
                id=user_id,
                email=f"{user_id}@example.test",
                display_name="Synthetic",
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            TaskRunModel(
                id=task_id,
                user_id=user_id,
                kind="daily_brief",
                status="succeeded",
                idempotency_key=str(task_id),
                input_payload={},
                started_at=BARRIER_NOW - timedelta(days=401),
                finished_at=BARRIER_NOW - timedelta(days=400),
                graph_thread_id=str(task_id),
            )
        )
        if kind == "privacy":
            session.add(
                TaskRunModel(
                    id=winner_id,
                    user_id=user_id,
                    kind="privacy.delete_all_data",
                    status="running",
                    idempotency_key=str(winner_id),
                    input_payload={"deletion_request_id": "synthetic-checkpoint-delete"},
                    started_at=BARRIER_NOW,
                    attempt_count=1,
                    lease_owner="synthetic-checkpoint-owner",
                    lease_expires_at=BARRIER_NOW + timedelta(minutes=5),
                )
            )
        await session.flush()
        session.add(
            TaskStepModel(
                task_id=task_id,
                sequence=1,
                name="synthetic",
                kind="read",
                status="succeeded",
                input_summary={},
            )
        )
    return user_id, task_id, winner_id


def _cleanup(
    retention: ManagedAsyncSessionMaker,
    cleaner: PostgresPrivacyCheckpointCleaner,
    *,
    kind: CleanupKind,
    user_id: UUID,
    winner_id: UUID,
) -> Awaitable[None]:
    """组合现有真实入口，角色职责由两个独立工厂/端口固定，不能互相替换。"""
    if kind == "history":
        return TaskHistoryCleanup(retention, cleaner).clean_user(
            user_id=user_id,
            cutoff=BARRIER_NOW - timedelta(days=365),
            batch_size=1,
        )
    return PrivacyDeletionWorker(
        retention, checkpoint_cleaner=cleaner, clock=_DeletionClock()
    ).delete_all_data(
        user_id=user_id,
        task_id=winner_id,
        request_id="synthetic-checkpoint-delete",
        lease_owner="synthetic-checkpoint-owner",
        lease_mode=TaskLeaseMode.NORMAL,
        batch_size=1,
    )


async def _wait_for_blocker(
    app: ManagedAsyncSessionMaker, *, waiter_pid: int | None = None
) -> None:
    """有界读取数据库实际阻塞关系；等待条件来自锁事实而非经过了多少秒。"""
    async with asyncio.timeout(5):
        while True:
            async with app() as session:
                waiting = await session.scalar(
                    text(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                        "AND (CAST(:pid AS integer) IS NULL OR pid=:pid) AND cardinality(pg_blocking_pids(pid)) > 0)"
                    ).bindparams(pid=waiter_pid)
                )
            if waiting:
                return
            await asyncio.sleep(0.01)


async def _checkpoint_counts(app: ManagedAsyncSessionMaker, task_id: UUID) -> tuple[int, ...]:
    """仅按规范 thread 读取三张表的计数，绝不读取 channel/blob 正文。"""
    counts: list[int] = []
    async with app() as session:
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            counts.append(
                int(
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE thread_id=:thread"),
                        {"thread": str(task_id)},
                    )
                    or 0
                )
            )
    return tuple(counts)


async def assert_checkpoint_race(
    *,
    app_url: str,
    retention_url: str,
    kind: CleanupKind,
    mutation: str,
    order: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证明 aput/aput_writes 的两个先后顺序及 app提交→retention提交间隙不会复活数据。"""
    app, retention = build_session_factory(app_url), build_session_factory(retention_url)
    saved_locked, resume_save, cleaned, resume_cleanup = (asyncio.Event() for _ in range(4))
    jobs: list[asyncio.Task[object]] = []
    try:
        user_id, task_id, winner_id = await _seed_tasks(app, kind=kind)
        _, other_task_id, _ = await _seed_tasks(app, kind="history")
        await seed_checkpoint_thread(app_url, task_id)
        await seed_checkpoint_thread(app_url, other_task_id)
        async with app() as session:
            migrations = (
                await session.execute(text("SELECT v FROM checkpoint_migrations ORDER BY v"))
            ).all()

        class PausingCleaner(PostgresPrivacyCheckpointCleaner):
            """在 app 原生删除已经提交后暂停，暴露两个真实连接之间最危险的间隙。"""

            async def _pause_target(self, current_id: UUID) -> None:
                """只暂停被测目标，privacy 的赢家最终 thread 不再暂停。"""
                if current_id == task_id and order == "cleanup_first":
                    cleaned.set()
                    await resume_cleanup.wait()

            async def clear_thread(
                self, *, binding: PrivacyDeletionBinding, task_id: UUID, now
            ) -> None:
                """在真实 privacy app 清理提交之后暂停。"""
                await super().clear_thread(binding=binding, task_id=task_id, now=now)
                await self._pause_target(task_id)

            async def clear_expired_thread(self, *, user_id: UUID, task_id: UUID, cutoff) -> None:
                """在真实 history app 清理提交之后暂停，外层仍持 retention Task 锁。"""
                await super().clear_expired_thread(user_id=user_id, task_id=task_id, cutoff=cutoff)
                await self._pause_target(task_id)

        original_authorize = _GuardedPostgresSaver._authorize_write

        async def pause_authorized(saver: _GuardedPostgresSaver, config: RunnableConfig) -> None:
            """先取得真实 Task→user 行锁，再让删除请求竞争同一锁。"""
            await original_authorize(saver, config)
            if order == "save_first":
                saved_locked.set()
                await resume_save.wait()

        monkeypatch.setattr(_GuardedPostgresSaver, "_authorize_write", pause_authorized)
        async with postgres_checkpointer(app_url) as saver, asyncio.timeout(15):
            prior = await saver.aget_tuple({"configurable": {"thread_id": str(task_id)}})
            assert prior is not None

            async def late_save() -> object:
                """使用屏障前取得的实际 saver/config 执行生产保存入口。"""
                if mutation == "aput_writes":
                    return await saver.aput_writes(
                        prior.config, [("synthetic", {"value": "late"})], "late-node"
                    )
                checkpoint = empty_checkpoint()
                checkpoint["id"], checkpoint["ts"] = str(uuid4()), BARRIER_NOW.isoformat()
                checkpoint["channel_values"] = {"synthetic": {"value": "late"}}
                checkpoint["channel_versions"] = {"synthetic": "2"}
                return await saver.aput(
                    prior.config,
                    checkpoint,
                    {"source": "loop", "step": 1, "parents": {}},
                    {"synthetic": "2"},
                )

            cleaner = PausingCleaner(app_url)
            if order == "save_first":
                save_job = asyncio.create_task(late_save())
                jobs.append(save_job)
                await saved_locked.wait()
                cleanup_job = asyncio.create_task(
                    _cleanup(retention, cleaner, kind=kind, user_id=user_id, winner_id=winner_id)
                )
                jobs.append(cleanup_job)
                await _wait_for_blocker(app)
                resume_save.set()
                await save_job
                await cleanup_job
            else:
                cleanup_job = asyncio.create_task(
                    _cleanup(retention, cleaner, kind=kind, user_id=user_id, winner_id=winner_id)
                )
                jobs.append(cleanup_job)
                await cleaned.wait()
                assert await _checkpoint_counts(app, task_id) == (0, 0, 0)
                async with app() as session:
                    assert await session.get(TaskRunModel, task_id) is not None
                save_job = asyncio.create_task(late_save())
                jobs.append(save_job)
                if kind == "history":
                    assert isinstance(saver, _GuardedPostgresSaver)
                    await _wait_for_blocker(
                        app, waiter_pid=saver._guard_connection.info.backend_pid
                    )
                    assert not save_job.done()
                else:
                    with pytest.raises(StateConflictError, match="Checkpoint write is unavailable"):
                        await save_job
                resume_cleanup.set()
                await cleanup_job
                if kind == "history":
                    with pytest.raises(StateConflictError, match="Checkpoint write is unavailable"):
                        await save_job
        assert await _checkpoint_counts(app, task_id) == (0, 0, 0)
        assert await _checkpoint_counts(app, other_task_id) == (1, 1, 1)
        async with app() as session:
            assert await session.get(TaskRunModel, task_id) is None
            assert (
                await session.execute(text("SELECT v FROM checkpoint_migrations ORDER BY v"))
            ).all() == migrations
    finally:
        resume_save.set()
        resume_cleanup.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await retention.dispose()
        await app.dispose()


async def assert_checkpoint_failure_resume(
    *,
    app_url: str,
    retention_url: str,
    kind: CleanupKind,
    failure_phase: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原生 app 删除失败或随后 retention 父删除失败都保留归属，同一范围可继续清理。"""
    app, retention = build_session_factory(app_url), build_session_factory(retention_url)
    try:
        user_id, task_id, winner_id = await _seed_tasks(app, kind=kind)
        await seed_checkpoint_thread(app_url, task_id)
        cleaner = PostgresPrivacyCheckpointCleaner(app_url)
        original_delete = AsyncPostgresSaver.adelete_thread

        async def fail_native(saver: AsyncPostgresSaver, thread_id: str) -> None:
            """实际三张 DELETE 执行后让同一 app 事务回滚，保留全部 checkpoint 数据。"""
            await original_delete(saver, thread_id)
            raise RuntimeError("synthetic checkpoint cleanup failure")

        def fail_parent(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            """在实际子行删除后、父行 SQL 前使 retention 事务失败；不打印 SQL 参数。"""
            del connection, cursor, parameters, context, executemany
            if statement.startswith("DELETE FROM task_runs"):
                raise RuntimeError("synthetic checkpoint cleanup failure")

        with monkeypatch.context() as patch:
            if failure_phase == "app_delete":
                patch.setattr(AsyncPostgresSaver, "adelete_thread", fail_native)
            else:
                event.listen(retention.engine.sync_engine, "before_cursor_execute", fail_parent)
            try:
                with pytest.raises(RuntimeError, match="synthetic checkpoint cleanup failure"):
                    await _cleanup(
                        retention, cleaner, kind=kind, user_id=user_id, winner_id=winner_id
                    )
            finally:
                if failure_phase != "app_delete":
                    event.remove(retention.engine.sync_engine, "before_cursor_execute", fail_parent)
        assert await _checkpoint_counts(app, task_id) == (
            (1, 1, 1) if failure_phase == "app_delete" else (0, 0, 0)
        )
        async with app() as session:
            assert await session.get(TaskRunModel, task_id) is not None
            assert (
                await session.scalar(
                    select(TaskStepModel.id).where(TaskStepModel.task_id == task_id)
                )
                is not None
            )
        if kind == "history":
            await _cleanup(retention, cleaner, kind=kind, user_id=user_id, winner_id=winner_id)
        else:
            later = BARRIER_NOW + timedelta(minutes=10)
            lease = await SqlAlchemyTaskExecutionStore(app).acquire(
                task_id=winner_id,
                lease_owner="synthetic-recovered-owner",
                now=later,
                lease_expires_at=later + timedelta(minutes=5),
            )
            assert (
                lease is not None and lease.lease_mode is TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
            )

            class RecoveryClock:
                """只为当前恢复尝试注入新的固定 UTC 时间，原始 started_at 不变。"""

                def now(self):
                    """返回已取得恢复租约的时间。"""
                    return later

            await PrivacyDeletionWorker(
                retention, checkpoint_cleaner=cleaner, clock=RecoveryClock()
            ).delete_all_data(
                user_id=user_id,
                task_id=winner_id,
                request_id="synthetic-checkpoint-delete",
                lease_owner=lease.lease_owner,
                lease_mode=lease.lease_mode,
                batch_size=1,
            )
            async with app() as session:
                assert (
                    await session.scalars(
                        select(AuditEventModel.event_type).where(
                            AuditEventModel.user_id == user_id,
                        )
                    )
                ).all() == ["privacy.deletion_completed"]
        assert await _checkpoint_counts(app, task_id) == (0, 0, 0)
        async with app() as session:
            assert await session.get(TaskRunModel, task_id) is None
    finally:
        await retention.dispose()
        await app.dispose()
