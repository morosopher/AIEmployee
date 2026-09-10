"""真实持久 Runner 的异常和租约竞争，保证 inactive 删除赢家始终可恢复。

使用真实 Task→user/CAS/authority，不把 store Fake 的保护结果当作数据库恢复证据。
超时只阻塞内存事件，所有业务时间显式注入，任何供应商路径均不参与这些测试。
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.privacy import PrivacyDeletionBinding
from ai_employee.application.use_cases.task_execution import DurableTaskRunner, TaskLeaseMode
from ai_employee.domain.errors import DomainError, StateConflictError, TransientProviderError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.privacy_checkpoints import (
    PostgresPrivacyCheckpointCleaner,
)
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.workers.privacy import PrivacyDeletionWorker
from tests.integration.privacy.test_all_data_deletion import (
    BARRIER_NOW,
    BARRIER_REQUEST_ID,
    BARRIER_TASK_ID,
    BARRIER_USER_ID,
    _barrier_lease,
    _barrier_task_facts,
    _DeletionClock,
    _PhaseCrashWorker,
    _seed_barrier_task,
)
from tests.integration.retention.checkpoint_cases import _wait_for_blocker
from tests.integration.retention.test_m2_action_retention import seed_lifecycle_action


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["total_timeout", "step_timeout", "domain", "unknown", "retry", "success", "acquire_ack"],
)
async def test_task27e_real_runner_errors_after_barrier_keep_exact_winner_recoverable(
    database_url: str,
    fault: str,
) -> None:
    """通用总/单步超时、异常、成功与acquire ACK丢失都不能终结精确inactive赢家。"""
    sessions = build_session_factory(database_url)
    reached: list[str] = []
    checkpoint_cleaner = PostgresPrivacyCheckpointCleaner(database_url)
    worker = PrivacyDeletionWorker(
        sessions, checkpoint_cleaner=checkpoint_cleaner, clock=_DeletionClock()
    )
    after_barrier = []

    async def barrier(lease) -> None:
        """先真实提交独立屏障，再冻结全部任务列，后续错误不得清理任何恢复身份。"""
        await worker._establish_barrier(
            PrivacyDeletionBinding(
                BARRIER_USER_ID,
                lease.task_id,
                BARRIER_REQUEST_ID,
                lease.lease_owner,
            ),
            lease_mode=lease.lease_mode,
        )
        after_barrier.append(await _barrier_task_facts(sessions))

    class FaultStep:
        """屏障提交后才注入故障，保证每次都是实际的 inactive Runner 分支。"""

        name = "synthetic_barrier_fault"

        async def execute(self, lease) -> None:
            await barrier(lease)
            reached.append(fault)
            if fault in {"total_timeout", "step_timeout"}:
                await asyncio.Event().wait()
            elif fault == "domain":
                raise DomainError(error_code="synthetic_deletion_failure", message="Safe failure")
            elif fault == "unknown":
                raise RuntimeError("synthetic deletion failure")
            elif fault == "retry":
                raise TransientProviderError(error_code="synthetic_retry", message="Safe failure")

    class AckLostStore(SqlAlchemyTaskExecutionStore):
        """真实 acquisition 和独立屏障都提交后才丢弃响应，触发 Runner 的安全失败端口。"""

        async def acquire(self, **kwargs):
            lease = await super().acquire(**kwargs)
            assert lease is not None
            await barrier(lease)
            raise RuntimeError("synthetic acquisition acknowledgement loss")

    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        async with sessions.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == BARRIER_TASK_ID)
                .values(
                    status="queued",
                    lease_owner=None,
                    lease_expires_at=None,
                    started_at=None,
                    attempt_count=0,
                )
            )
        runner = DurableTaskRunner(
            store=AckLostStore(sessions)
            if fault == "acquire_ack"
            else SqlAlchemyTaskExecutionStore(sessions),
            clock=lambda: BARRIER_NOW,
            lease_duration=timedelta(minutes=1),
            task_timeout_seconds=1 if fault == "total_timeout" else 10,
            task_step_timeout_seconds=1,
            max_transient_retries=3,
            resolve_steps=lambda _: (FaultStep(),),
        )
        assert (
            await runner.run(
                BARRIER_TASK_ID, lease_owner="synthetic-runner", retry_delay=timedelta(seconds=1)
            )
            is False
        )
        assert reached == ([] if fault == "acquire_ack" else [fault])
        assert len(after_barrier) == 1
        assert await _barrier_task_facts(sessions) == after_barrier[0]
        async with sessions.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == BARRIER_TASK_ID)
                .values(lease_expires_at=BARRIER_NOW - timedelta(seconds=1))
            )
        recovery = await SqlAlchemyTaskExecutionStore(sessions).acquire(
            task_id=BARRIER_TASK_ID,
            lease_owner="synthetic-recovery",
            now=BARRIER_NOW,
            lease_expires_at=BARRIER_NOW + timedelta(minutes=1),
        )
        assert (
            recovery is not None and recovery.lease_mode is TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
        )
        assert recovery.started_at == BARRIER_NOW
        await worker.delete_all_data(
            user_id=BARRIER_USER_ID,
            task_id=BARRIER_TASK_ID,
            request_id=BARRIER_REQUEST_ID,
            lease_owner=recovery.lease_owner,
            lease_mode=recovery.lease_mode,
            batch_size=1,
        )
        async with sessions() as session:
            assert await session.get(TaskRunModel, BARRIER_TASK_ID) is None
            events = (
                await session.scalars(
                    select(AuditEventModel).where(AuditEventModel.user_id == BARRIER_USER_ID)
                )
            ).all()
            assert [row.event_type for row in events] == ["privacy.deletion_completed"]
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["barrier_first", "acquire_first"])
async def test_task27e_task_acquire_and_barrier_actual_lock_race(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    order: str,
) -> None:
    """真实TaskRun acquisition与独立inactive CAS双向竞争；无Tool claim时不能伪造核对。"""
    sessions = build_session_factory(database_url)
    worker = _PhaseCrashWorker(sessions, "unclaimed_actions")
    locked, release = asyncio.Event(), asyncio.Event()
    jobs: list[asyncio.Task] = []
    acquiring: asyncio.Task | None = None
    try:
        await _seed_barrier_task(sessions, change="zero_facts", active=True, live=True)
        seed = await seed_lifecycle_action(sessions, retained=True, user_id=BARRIER_USER_ID)
        async with sessions.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == seed.task_id)
                .values(
                    status="queued",
                    lease_owner=None,
                    lease_expires_at=None,
                    started_at=None,
                    attempt_count=0,
                )
            )

        if order == "barrier_first":
            original = worker._lock_binding

            async def pause_barrier(session, binding):
                result = await original(session, binding)
                locked.set()
                await release.wait()
                return result

            monkeypatch.setattr(worker, "_lock_binding", pause_barrier)
        else:
            original_scalar = AsyncSession.scalar

            async def pause_acquire(session, *args, **kwargs):
                result = await original_scalar(session, *args, **kwargs)
                if (
                    asyncio.current_task() is acquiring
                    and isinstance(result, UserModel)
                    and not locked.is_set()
                ):
                    locked.set()
                    await release.wait()
                return result

            monkeypatch.setattr(AsyncSession, "scalar", pause_acquire)

        async def acquire():
            return await SqlAlchemyTaskExecutionStore(sessions).acquire(
                task_id=seed.task_id,
                lease_owner="synthetic-ordinary",
                now=BARRIER_NOW,
                lease_expires_at=BARRIER_NOW + timedelta(minutes=1),
            )

        async def delete():
            with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
                await worker.execute(_barrier_lease())

        async with asyncio.timeout(10):
            if order == "barrier_first":
                jobs.append(asyncio.create_task(delete()))
                await locked.wait()
                acquiring = asyncio.create_task(acquire())
                jobs.append(acquiring)
            else:
                acquiring = asyncio.create_task(acquire())
                jobs.append(acquiring)
                await locked.wait()
                jobs.append(asyncio.create_task(delete()))
            await _wait_for_blocker(sessions)
            release.set()
            results = await asyncio.gather(*jobs)
        claimed = results[1] if order == "barrier_first" else results[0]
        assert (claimed is None) is (order == "barrier_first")
        assert await acquire() is None
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            assert task is not None and task.status == "cancelled"
            assert task.error_code == "action_content_expired"
            assert (
                await session.scalar(
                    select(ToolExecutionModel.id).where(ToolExecutionModel.task_id == seed.task_id)
                )
                is None
            )
            running = (
                await session.scalars(
                    select(AuditEventModel.id).where(
                        AuditEventModel.task_id == seed.task_id,
                        AuditEventModel.event_type == "task.running",
                    )
                )
            ).all()
            assert len(running) == (1 if order == "acquire_first" else 0)
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await sessions.dispose()


@pytest.mark.asyncio
async def test_task27e_winner_lock_wait_uses_fresh_clock_after_blocker_commit(
    database_url: str,
) -> None:
    """真实等待Task锁跨过租约期限后，删除不能沿用请求开始前采样的有效时间。"""
    sessions = build_session_factory(database_url)
    clock = _DeletionClock()
    worker = PrivacyDeletionWorker(sessions, clock=clock)
    job = None
    try:
        await _seed_barrier_task(sessions, active=False, live=True)
        binding = PrivacyDeletionBinding(
            BARRIER_USER_ID, BARRIER_TASK_ID, BARRIER_REQUEST_ID, "worker-before-barrier"
        )
        async with sessions.begin() as blocking:
            await blocking.scalar(
                select(TaskRunModel).where(TaskRunModel.id == BARRIER_TASK_ID).with_for_update()
            )
            job = asyncio.create_task(worker._assert_winner(binding))
            await _wait_for_blocker(sessions)
            clock._now = BARRIER_NOW + timedelta(days=1)
        with pytest.raises(StateConflictError):
            await job
    finally:
        if job is not None:
            await asyncio.gather(job, return_exceptions=True)
        await sessions.dispose()
