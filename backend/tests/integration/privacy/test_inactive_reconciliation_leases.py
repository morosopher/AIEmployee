"""验证普通只读核对的租约、恢复扫描与在途回包都服从真实删除屏障。"""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy import Select, select
from sqlalchemy.engine import ScalarResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable

from ai_employee.application.ports.trusted_actions import ExecutionReference, ProviderWriteOutcome
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.tasks import OutboxEventModel, TaskRunModel
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionReconciliationRecoveryStore,
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers.reconcile_actions import execute_reconciliation_task
from tests.integration.m2.test_reconciliation_revocation_lease import (
    _PausedReconciliationAdapter,
    _seed_reconciling_action,
    _worker_settings,
)
from tests.integration.m2.test_tool_execution_claim import (
    ACTION_CIPHER,
    NOW,
    _cycle5_database_url,  # noqa: F401
    _cycle5_migrated_database,  # noqa: F401
)
from tests.integration.privacy.inactive_barrier import (
    assert_facts_unchanged,
    commit_deletion_barrier,
    database_facts,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("cycle5_tracked_session_factories")]


@pytest.mark.parametrize("mode", ("claim", "release", "recover", "malformed", "malformed-pending"))
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_reconciliation_lease_mutations_respect_deletion_barrier(
    database_url: str,
    mode: str,
    inactive: bool,
) -> None:
    """普通lease与调度恢复不能在barrier后改写任何协调事实，含损坏lease的清理。

    release先真实claim；malformed只合成既有损坏状态。active分别验证真实租约或
    Outbox提交和重复扫描去重，inactive在真实删除Worker后比较全表并重复原入口。
    """
    seed = await _seed_reconciling_action(database_url, resource_kind="mail")
    sessions = build_session_factory(database_url)
    owner = "synthetic-inactive-reconcile-owner"
    try:
        async with sessions.begin() as session:
            if mode == "release":
                claimed = await SqlAlchemyTrustedActionRepository(
                    session,
                    ACTION_CIPHER,
                ).claim_reconciliation(
                    task_id=seed.task_id,
                    lease_owner=owner,
                    now=NOW,
                    lease_expires_at=NOW + timedelta(minutes=1),
                )
                assert claimed is not None
            elif mode.startswith("malformed"):
                task = await session.get(TaskRunModel, seed.task_id)
                assert task is not None
                task.lease_owner = owner
                task.lease_expires_at = None
                if mode == "malformed-pending":
                    session.add(
                        OutboxEventModel(
                            topic="task.execute",
                            aggregate_id=seed.task_id,
                            deduplication_key=f"synthetic-existing-reconcile:{seed.task_id}",
                            payload={"task_id": str(seed.task_id)},
                            available_at=NOW,
                        )
                    )
        if inactive:
            await commit_deletion_barrier(sessions, user_id=seed.user_id)
        before = await database_facts(sessions)

        async def submit() -> bool | int:
            """沿生产事务边界提交一次普通租约操作，返回无内容的实际处理结果。"""
            async with sessions.begin() as session:
                repository = SqlAlchemyTrustedActionRepository(session, ACTION_CIPHER)
                if mode == "claim":
                    return (
                        await repository.claim_reconciliation(
                            task_id=seed.task_id,
                            lease_owner=owner,
                            now=NOW,
                            lease_expires_at=NOW + timedelta(minutes=1),
                        )
                        is not None
                    )
                if mode == "release":
                    return await repository.release_reconciliation_lease(
                        task_id=seed.task_id,
                        lease_owner=owner,
                        now=NOW,
                    )
                return await repository.recover_due_reconciliations(now=NOW, limit=1)

        submitted = await submit()
        if inactive:
            assert_facts_unchanged(before, await database_facts(sessions))
            assert not submitted
            assert not await submit()
            assert_facts_unchanged(before, await database_facts(sessions))
        else:
            assert submitted == (0 if mode == "malformed-pending" else 1)
            async with sessions() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                assert task is not None and task.status == "reconciling"
                assert task.lease_owner == (owner if mode == "claim" else None)
                if mode == "claim":
                    assert task.lease_expires_at == NOW + timedelta(minutes=1)
                pending = tuple(
                    await session.scalars(
                        select(OutboxEventModel.id).where(
                            OutboxEventModel.aggregate_id == seed.task_id,
                            OutboxEventModel.topic == "task.execute",
                            OutboxEventModel.published_at.is_(None),
                        )
                    )
                )
                assert len(pending) == (0 if mode in {"claim", "release"} else 1)
            assert not await submit()
    finally:
        await sessions.dispose()


async def test_reconciliation_recovery_filters_inactive_before_limit(database_url: str) -> None:
    """较早的inactive候选不能吞掉limit=1；活动用户的到期核对必须获得实际Outbox。"""
    inactive = await _seed_reconciling_action(database_url, resource_kind="mail")
    active = await _seed_reconciling_action(database_url, resource_kind="mail")
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            task = await session.get(TaskRunModel, inactive.task_id)
            assert task is not None
            task.scheduled_for = NOW - timedelta(minutes=1)
        await commit_deletion_barrier(sessions, user_id=inactive.user_id)
        recovery = SqlAlchemyTrustedActionReconciliationRecoveryStore(sessions)
        assert await recovery.recover_due_reconciliations(now=NOW, limit=1) == 1
        async with sessions() as session:
            recovered_ids = tuple(
                await session.scalars(
                    select(OutboxEventModel.aggregate_id).where(
                        OutboxEventModel.topic == "task.execute",
                        OutboxEventModel.aggregate_id.in_((inactive.task_id, active.task_id)),
                    )
                )
            )
        assert recovered_ids == (active.task_id,)
        assert await recovery.recover_due_reconciliations(now=NOW, limit=1) == 0
    finally:
        await sessions.dispose()


@pytest.mark.parametrize("malformed", (False, True), ids=("normal", "malformed-pending"))
async def test_recovery_rechecks_user_after_candidate_row_lock(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    malformed: bool,
) -> None:
    """在真实候选SELECT已锁Task后暂停，再提交barrier，证明SQL初筛不替代用户同步。

    hook只包第三方scalars调用，原SQL、行锁和持久结果全部真实执行；匹配Task的
    SKIP LOCKED查询后仅暂停一次。此时barrier不争用该普通Task，因此可以先提交。
    """
    seed = await _seed_reconciling_action(database_url, resource_kind="mail")
    sessions = build_session_factory(database_url)
    selected, release = asyncio.Event(), asyncio.Event()
    original = AsyncSession.scalars

    async def pause_candidates(
        session: AsyncSession,
        statement: Executable,
        *args: Any,
        **kwargs: Any,
    ) -> ScalarResult[Any]:
        """Any局限于SQLAlchemy可变重载；识别查询形状后只保留原结果并同步测试窗口。"""
        result = await original(session, statement, *args, **kwargs)
        if isinstance(statement, Select) and not selected.is_set():
            locking = statement._for_update_arg
            if (
                locking is not None
                and locking.skip_locked
                and any(
                    column.get("entity") is TaskRunModel for column in statement.column_descriptions
                )
            ):
                selected.set()
                await release.wait()
        return result

    pending: asyncio.Task[int] | None = None
    try:
        if malformed:
            async with sessions.begin() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                assert task is not None
                task.lease_owner = "synthetic-malformed-owner"
                task.lease_expires_at = None
                session.add(
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=seed.task_id,
                        deduplication_key=f"synthetic-pending-read:{seed.task_id}",
                        payload={"task_id": str(seed.task_id)},
                        available_at=NOW,
                    )
                )
        monkeypatch.setattr(AsyncSession, "scalars", pause_candidates)
        recovery = SqlAlchemyTrustedActionReconciliationRecoveryStore(sessions)
        pending = asyncio.create_task(recovery.recover_due_reconciliations(now=NOW, limit=1))
        await asyncio.wait_for(selected.wait(), timeout=5)
        await asyncio.wait_for(commit_deletion_barrier(sessions, user_id=seed.user_id), timeout=5)
        before = await database_facts(sessions)
        release.set()
        recovered = await asyncio.wait_for(pending, timeout=5)
        assert_facts_unchanged(before, await database_facts(sessions))
        assert recovered == 0
        assert await recovery.recover_due_reconciliations(now=NOW, limit=1) == 0
        assert_facts_unchanged(before, await database_facts(sessions))
    finally:
        release.set()
        if pending is not None:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await sessions.dispose()


class _BarrierReadAdapter(_PausedReconciliationAdapter):
    """在真实只读端口暂停后选择成功回包或异常，让Worker执行原有cleanup事务。"""

    def __init__(self, *, fail: bool) -> None:
        """只保存合成故障选择，不改变provider调用计数或冻结命令读取。"""
        super().__init__()
        self.fail = fail

    async def reconcile(
        self,
        command: object,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """网络异常不得伪造未应用证明；父类在release前已记录唯一只读调用。"""
        outcome = await super().reconcile(command, execution)
        if self.fail:
            raise RuntimeError("synthetic reconciliation transport failure")
        return outcome


@pytest.mark.parametrize("kind", ("mail", "calendar"))
@pytest.mark.parametrize("fail", (False, True), ids=("result", "exception"))
@pytest.mark.parametrize("inactive", (False, True), ids=("active", "inactive"))
async def test_reconciliation_worker_result_and_cleanup_respect_barrier(
    database_url: str,
    tmp_path: Path,
    kind: Literal["mail", "calendar"],
    fail: bool,
    inactive: bool,
) -> None:
    """真实worker认领后在只读网络暂停；回包与异常释放都不能越过删除屏障。

    活跃对照检查成功终态或异常后的真实lease释放。inactive比较全表并重新投递同一
    worker，要求零新增只读/写调用，防止只验证直接repository而漏掉cleanup写入。
    """
    seed = await _seed_reconciling_action(database_url, resource_kind=kind)
    sessions = build_session_factory(database_url)
    adapter = _BarrierReadAdapter(fail=fail)
    registry = ProviderAdapterRegistry(
        google_mail_action=adapter if kind == "mail" else None,
        google_calendar_action=adapter if kind == "calendar" else None,
    )
    settings = _worker_settings(tmp_path)

    async def run_worker() -> bool:
        """捕获已有稳定冲突/合成transport异常，持久事实才是断言依据。"""
        try:
            return await execute_reconciliation_task(
                task_id=seed.task_id,
                session_factory=sessions,
                settings=settings,
                adapters=registry,
                lease_owner="synthetic-barrier-read-owner",
                now=NOW,
            )
        except StateConflictError as error:
            assert inactive and error.error_code == "trusted_action_unavailable"
        except RuntimeError as error:
            assert fail and str(error) == "synthetic reconciliation transport failure"
        return False

    pending = asyncio.create_task(run_worker())
    try:
        await asyncio.wait_for(adapter.entered.wait(), timeout=5)
        if inactive:
            await commit_deletion_barrier(sessions, user_id=seed.user_id)
        before = await database_facts(sessions)
        adapter.release_result.set()
        await asyncio.wait_for(pending, timeout=5)
        if inactive:
            assert_facts_unchanged(before, await database_facts(sessions))
            assert not await run_worker()
            assert_facts_unchanged(before, await database_facts(sessions))
        else:
            async with sessions() as session:
                task = await session.get(TaskRunModel, seed.task_id)
                assert task is not None
                assert task.status == ("reconciling" if fail else "succeeded")
                assert task.lease_owner is None and task.lease_expires_at is None
        assert adapter.reconcile_calls == 1 and adapter.write_calls == 0
    finally:
        adapter.release_result.set()
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await sessions.dispose()
