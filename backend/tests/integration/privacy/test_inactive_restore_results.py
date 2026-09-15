"""验证恢复准备结果以Task→user同步删除屏障，再锁历史来源和缓存事件。"""

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import func, select, text

from ai_employee.application.use_cases.calendar_proposals import (
    CalendarProposalNotFoundError,
    CalendarProposalTargetSnapshot,
    CalendarRestoreEnqueueUseCase,
    CalendarRestoreSourceProjection,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.application.use_cases.tasks import CreateTaskResult
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel
from ai_employee.infrastructure.db.models.sources import CalendarEventModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
    SqlAlchemyCalendarRestoreEnqueueRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.workers.prepare_calendar_restore import PrepareCalendarRestoreTaskStep
from tests.integration.m2.test_calendar_proposal_versions import (
    ACTION_CIPHER,
    NOW,
    SOURCE_LOCAL_EVENT_ID,
    USER_ID,
    _current_provider_event,
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
    _ReaderResolver,
    _seed_restore_source,
)
from tests.integration.m2.test_restore_current_cache import SOURCE_CIPHER, _CurrentReader
from tests.integration.privacy.inactive_barrier import (
    assert_facts_unchanged,
    commit_deletion_barrier,
    database_facts,
)
from tests.integration.privacy.test_all_data_deletion import (
    _PhaseCrashWorker,
    _seed_owned_deletion_lease,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


def _restore_lease(task_id: UUID, snapshot_id: UUID) -> LeasedTask:
    """只投影既有合成任务的真实租约身份，重投沿用同一输入和owner。"""
    return LeasedTask(
        task_id=task_id,
        user_id=USER_ID,
        kind="calendar.restore.prepare",
        started_at=NOW,
        lease_owner="calendar-restore-worker",
        input_payload={
            "source_snapshot_id": str(snapshot_id),
            "creation_idempotency_key": f"calendar-restore:{task_id}",
        },
    )


async def _assert_saved_restore(sessions: ManagedAsyncSessionMaker, *, task_id: UUID) -> None:
    """从独立会话确认新提案、marker与当前事件v2密文属于同一次真实提交。"""
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(
                    CalendarChangeProposalModel,
                )
            )
            == 2
        )
        task = await session.get(TaskRunModel, task_id)
        assert task is not None and task.result_payload is not None
        proposal = await session.get(
            CalendarChangeProposalModel, UUID(str(task.result_payload["calendar_proposal_id"]))
        )
        event = await session.get(CalendarEventModel, SOURCE_LOCAL_EVENT_ID)
        assert proposal is not None and event is not None
        assert proposal.base_etag == event.etag == _current_provider_event().etag
        assert event.description_ciphertext is not None and event.description_aad_version == 2
        assert event.location_ciphertext is not None and event.location_aad_version == 2


async def test_restore_result_holds_user_lock_before_target_read_and_commit(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """target已读active后的真实可达窗口必须串行化，而不能靠更早的active谓词。

    先认领真实删除租约，再在结果事务的第二次target读取后暂停。独立删除worker要么
    被该事务阻塞，要么先提交barrier；pg_blocking_pids提供锁等待证据，不靠睡眠猜测。
    旧实现允许barrier先提交并在其后写入业务行与marker，因此全表断言构成有效RED。
    """
    sessions = build_session_factory(database_url)
    target_read, release = asyncio.Event(), asyncio.Event()
    calls = 0
    writer_pid: int | None = None
    original = SqlAlchemyCalendarSyncRepository.get_proposal_target

    async def pause_after_target(
        repository: SqlAlchemyCalendarSyncRepository,
        *,
        user_id: UUID,
        connection_id: UUID,
        calendar_id: str,
    ) -> CalendarProposalTargetSnapshot | None:
        """前置读计划正常完成，只暂停网络已返回后的结果事务。"""
        nonlocal calls, writer_pid
        target = await original(
            repository, user_id=user_id, connection_id=connection_id, calendar_id=calendar_id
        )
        calls += 1
        if calls == 2:
            assert target is not None
            writer_pid = await repository._session.scalar(text("SELECT pg_backend_pid()"))
            target_read.set()
            await release.wait()
        return target

    monkeypatch.setattr(SqlAlchemyCalendarSyncRepository, "get_proposal_target", pause_after_target)
    pending: asyncio.Task[None] | None = None
    deletion: asyncio.Task[None] | None = None
    try:
        task_id, snapshot_id = await _seed_restore_source(sessions)
        deletion_lease = await _seed_owned_deletion_lease(
            sessions,
            user_id=USER_ID,
            request_id="synthetic-restore-result-deletion",
        )
        step = PrepareCalendarRestoreTaskStep(
            sessions,
            action_cipher=ACTION_CIPHER,
            source_cipher=SOURCE_CIPHER,
            reader_resolver=_ReaderResolver(_CurrentReader()),
            clock=lambda: NOW,
        )
        lease = _restore_lease(task_id, snapshot_id)
        pending = asyncio.create_task(step.execute(lease))
        await asyncio.wait_for(target_read.wait(), timeout=5)
        worker = _PhaseCrashWorker(sessions, "barrier")
        deletion = asyncio.create_task(worker.execute(deletion_lease))

        async def observe_serialization() -> bool:
            """只读PG等待图；返回真表示删除已明确等待当前结果事务持有的用户行锁。"""
            while True:
                assert deletion is not None and writer_pid is not None
                if deletion.done():
                    return False
                async with sessions() as observer:
                    if await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE :writer_pid = ANY(pg_blocking_pids(pid)) "
                            "AND wait_event_type = 'Lock')"
                        ),
                        {"writer_pid": writer_pid},
                    ):
                        return True

        blocked = await asyncio.wait_for(observe_serialization(), timeout=5)
        if not blocked:
            with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
                await deletion
            before = await database_facts(sessions)
            release.set()
            await asyncio.wait_for(pending, timeout=5)
            assert_facts_unchanged(before, await database_facts(sessions))
        else:
            release.set()
            await asyncio.wait_for(pending, timeout=5)
            with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
                await asyncio.wait_for(deletion, timeout=5)
            await _assert_saved_restore(sessions, task_id=task_id)
            before = await database_facts(sessions)
            await step.execute(lease)
            assert_facts_unchanged(before, await database_facts(sessions))
        assert blocked, "restore result must hold user lock before locking source/event rows"
        assert worker.visited == ["barrier"]
    finally:
        release.set()
        running = tuple(task for task in (pending, deletion) if task is not None)
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await sessions.dispose()


@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_restore_get_result_after_barrier_cannot_persist(
    database_url: str,
    inactive: bool,
) -> None:
    """保留GET阶段屏障的已有拒绝邻接，不能把更早target谓词的成功冒充新缺陷RED。"""
    sessions = build_session_factory(database_url)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_get() -> None:
        """仅暂停provider-neutral GET；没有跨网络的生产数据库事务。"""
        entered.set()
        await release.wait()

    pending: asyncio.Task[None] | None = None
    try:
        task_id, snapshot_id = await _seed_restore_source(sessions)
        step = PrepareCalendarRestoreTaskStep(
            sessions,
            action_cipher=ACTION_CIPHER,
            source_cipher=SOURCE_CIPHER,
            reader_resolver=_ReaderResolver(_CurrentReader(pause_get)),
            clock=lambda: NOW,
        )
        pending = asyncio.create_task(step.execute(_restore_lease(task_id, snapshot_id)))
        await asyncio.wait_for(entered.wait(), timeout=5)
        if inactive:
            await commit_deletion_barrier(sessions, user_id=USER_ID)
        before = await database_facts(sessions)
        release.set()
        try:
            await asyncio.wait_for(pending, timeout=5)
        except CalendarProposalNotFoundError:
            assert inactive
        except StateConflictError as error:
            assert inactive and error.error_code == "connection_capability_disabled"
        if inactive:
            assert_facts_unchanged(before, await database_facts(sessions))
        else:
            await _assert_saved_restore(sessions, task_id=task_id)
    finally:
        release.set()
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await sessions.dispose()


async def test_restore_enqueue_and_result_keep_user_before_source_lock_order(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实enqueue应用边界与在途GET结果交错，不能形成source→user和user→source环。

    先让既有Worker完成读计划进入GET，再暂停新请求已锁定的source投影。恢复GET后，
    PostgreSQL必须观测到结果事务等待enqueue；释放新请求后，两者都应成功，不容许
    用死锁回滚任意一方来满足持久化原子性。只替换提交后的队列投递端口，不发送消息。
    """
    sessions = build_session_factory(database_url)
    network, release_network = asyncio.Event(), asyncio.Event()
    projected, release_enqueue = asyncio.Event(), asyncio.Event()
    enqueue_pid: int | None = None
    original = SqlAlchemyCalendarProposalRepository.get_restore_source_projection

    async def pause_get() -> None:
        """确保首读事务已提交，让新enqueue与后续结果事务形成精确锁交错。"""
        network.set()
        await release_network.wait()

    async def pause_projection(
        repository: SqlAlchemyCalendarProposalRepository,
        *,
        user_id: UUID,
        source_snapshot_id: UUID,
    ) -> CalendarRestoreSourceProjection | None:
        """保存真实source行锁的会话标识，等待测试通过PG确认另一事务正在等待它。"""
        nonlocal enqueue_pid
        value = await original(repository, user_id=user_id, source_snapshot_id=source_snapshot_id)
        assert value is not None
        enqueue_pid = await repository._session.scalar(text("SELECT pg_backend_pid()"))
        projected.set()
        await release_enqueue.wait()
        return value

    class LocalDispatcher:
        """保持数据库已创建状态；队列投递不属于本测试的锁排序边界。"""

        async def dispatch(self, task_id: UUID) -> TaskStatus:
            """只返回已存在的本地状态，不修改任务、不调用Redis或供应商。"""
            del task_id
            return TaskStatus.CREATED

    monkeypatch.setattr(
        SqlAlchemyCalendarProposalRepository, "get_restore_source_projection", pause_projection
    )
    pending: asyncio.Task[None] | None = None
    enqueue: asyncio.Task[CreateTaskResult] | None = None
    try:
        task_id, snapshot_id = await _seed_restore_source(sessions)
        step = PrepareCalendarRestoreTaskStep(
            sessions,
            action_cipher=ACTION_CIPHER,
            source_cipher=SOURCE_CIPHER,
            reader_resolver=_ReaderResolver(_CurrentReader(pause_get)),
            clock=lambda: NOW,
        )
        pending = asyncio.create_task(step.execute(_restore_lease(task_id, snapshot_id)))
        await asyncio.wait_for(network.wait(), timeout=5)
        use_case = CalendarRestoreEnqueueUseCase(
            SqlAlchemyCalendarRestoreEnqueueRepositoryFactory(sessions, ACTION_CIPHER),
            LocalDispatcher(),
            lambda: NOW,
        )
        enqueue = asyncio.create_task(
            use_case.execute(
                user_id=USER_ID,
                event_id=SOURCE_LOCAL_EVENT_ID,
                source_snapshot_id=snapshot_id,
                creation_idempotency_key="synthetic-concurrent-restore-enqueue",
            )
        )
        await asyncio.wait_for(projected.wait(), timeout=5)
        release_network.set()

        async def wait_for_database_lock() -> None:
            """等待真实阻塞图出现；没有基于固定sleep推断竞争状态。"""
            while True:
                async with sessions() as observer:
                    if await observer.scalar(
                        text(
                            "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                            "WHERE :enqueue_pid = ANY(pg_blocking_pids(pid)) "
                            "AND wait_event_type = 'Lock')"
                        ),
                        {"enqueue_pid": enqueue_pid},
                    ):
                        return

        await asyncio.wait_for(wait_for_database_lock(), timeout=5)
        release_enqueue.set()
        results = await asyncio.wait_for(
            asyncio.gather(pending, enqueue, return_exceptions=True),
            timeout=8,
        )
        # 不让pytest展开SQL异常及参数；失败只报告异常类型和SQLSTATE，原会话仍由finally释放。
        failures = [
            (type(result).__name__, getattr(getattr(result, "orig", None), "sqlstate", None))
            for result in results
            if isinstance(result, BaseException)
        ]
        assert not failures, f"concurrent restore operations failed: {failures}"
        await _assert_saved_restore(sessions, task_id=task_id)
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(TaskRunModel)) == 2
    finally:
        release_enqueue.set()
        release_network.set()
        running = tuple(task for task in (pending, enqueue) if task is not None)
        for task in running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        await sessions.dispose()
