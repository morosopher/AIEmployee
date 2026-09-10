"""验证隐私删除清理三张 checkpoint 表、保留归属和失败恢复事实。

fixture 只通过原生 saver 写合成 channel，所有 thread 均先具有真实 TaskRun/user 归属。
"""

from datetime import timedelta
from uuid import UUID

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import text, update

from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.workers.retention import RetentionCleanupWorker
from tests.integration.privacy.test_all_data_deletion import (
    BARRIER_NOW,
    BARRIER_OTHER_TASK_ID,
    BARRIER_TASK_ID,
    _barrier_lease,
    _PhaseCrashWorker,
    _seed_barrier_task,
)
from tests.integration.retention.test_m2_action_retention import seed_lifecycle_action


async def seed_checkpoint_thread(database_url: str, task_id: UUID) -> None:
    """通过共同生产 saver 写入三表；不直接制造与原生格式不一致的 JSON/blob。"""
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = BARRIER_NOW.isoformat()
    checkpoint["channel_values"] = {"synthetic": {"value": "synthetic"}}
    checkpoint["channel_versions"] = {"synthetic": "1"}
    async with postgres_checkpointer(database_url) as saver:
        saved = await saver.aput(
            {"configurable": {"thread_id": str(task_id), "checkpoint_ns": ""}},
            checkpoint,
            {"source": "input", "step": 0, "parents": {}},
            {"synthetic": "1"},
        )
        await saver.aput_writes(saved, [("synthetic", "synthetic")], "synthetic-node")


@pytest.mark.asyncio
async def test_task27e_deletion_clears_checkpoints_before_removing_task_parent(
    database_url: str,
) -> None:
    """本地任务删除阶段必须同时清理其三个 checkpoint 数据表，另一用户和全局版本不变。"""
    sessions = build_session_factory(database_url)
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        other = await seed_lifecycle_action(sessions)
        await seed_checkpoint_thread(database_url, BARRIER_OTHER_TASK_ID)
        await seed_checkpoint_thread(database_url, other.task_id)
        async with sessions() as session:
            migrations = (
                await session.execute(text("SELECT v FROM checkpoint_migrations ORDER BY v"))
            ).all()
        worker = _PhaseCrashWorker(sessions, "local_rows_deleted")
        with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
            await worker.execute(_barrier_lease())
        async with sessions() as session:
            assert await session.get(TaskRunModel, BARRIER_OTHER_TASK_ID) is None
            assert await session.get(TaskRunModel, BARRIER_TASK_ID) is not None
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE thread_id=:thread"),
                        {
                            "thread": str(BARRIER_OTHER_TASK_ID),
                        },
                    )
                    == 0
                )
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE thread_id=:thread"),
                        {
                            "thread": str(other.task_id),
                        },
                    )
                    == 1
                )
            assert (
                await session.execute(text("SELECT v FROM checkpoint_migrations ORDER BY v"))
            ).all() == migrations
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_task27e_ordinary_history_cannot_orphan_checkpoint_ownership(
    database_url: str,
) -> None:
    """普通365天任务回收也先清三表，避免全数据删除失去 orphan thread 的用户归属。"""
    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(sessions, execution_status="succeeded")
        await seed_checkpoint_thread(database_url, seed.task_id)
        async with sessions.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == seed.task_id)
                .values(
                    finished_at=BARRIER_NOW - timedelta(days=400),
                )
            )
        await RetentionCleanupWorker(
            sessions,
            checkpoint_cleaner=PostgresPrivacyCheckpointCleaner(database_url),
        ).execute(now=BARRIER_NOW, batch_size=1)
        async with sessions() as session:
            assert await session.get(TaskRunModel, seed.task_id) is None
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE thread_id=:thread"),
                        {
                            "thread": str(seed.task_id),
                        },
                    )
                    == 0
                )
    finally:
        await sessions.dispose()
