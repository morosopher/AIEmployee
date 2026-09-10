"""在实际 ToolExecution claim/request-start 事务上验证删除屏障的双向锁竞争。

只暂停生产方法已经取得锁的边界；精确 backend PID 与 pg_blocking_pids 证明真实等待。
所有审批、命令和账户均为合成 fixture；保留原锁序、事务工厂及授权策略，不派发网络写入。
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.trusted_actions import (
    ProviderWriteOutcome,
    RequestStartAuthorizer,
    RequestStartDisposition,
    RequestStartResult,
    TrustedActionDispatchSnapshot,
    TrustedActionExecutionSnapshot,
)
from ai_employee.application.use_cases.privacy import (
    PrivacyDeletionBinding,
    PrivacyReconciliationTarget,
)
from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.application.use_cases.trusted_actions import TrustedActionUserInactive
from ai_employee.domain.actions import ProviderWriteOutcomeKind, ToolExecutionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.actions import MailDraftVersionModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.privacy_reconciliation import (
    PRIVACY_RECONCILIATION_STARTED,
    SqlAlchemyPrivacyReconciliationStore,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.privacy import PrivacyProviderReader
from tests.integration.m2 import test_tool_execution_claim as claim_cases
from tests.integration.m2.test_tool_execution_claim import (
    ACTION_CIPHER,
    NOW,
    _load_dispatch_snapshot_for_test,
    _RecordingAdapter,
    _Seed,
    _seed_action,
    _workflow,
)
from tests.integration.privacy.test_all_data_deletion import (
    _barrier_lease,
    _DeletionClock,
    _PhaseCrashWorker,
)
from tests.integration.retention.checkpoint_cases import _wait_for_blocker

# 复用已交付 claim 测试的 official regular database 准入及连接池登记。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")
_cycle5_database_url = claim_cases._cycle5_database_url
_cycle5_migrated_database = claim_cases._cycle5_migrated_database


async def _seed_delete_for_action(sessions: ManagedAsyncSessionMaker, seed: _Seed) -> LeasedTask:
    """补齐同用户的真实删除任务与邮件版本，使后续隐私核对能验证完整持久绑定。"""
    lease = replace(_barrier_lease(), task_id=uuid4(), user_id=seed.user_id, started_at=NOW)
    async with sessions.begin() as session:
        session.add(
            TaskRunModel(
                id=lease.task_id,
                user_id=lease.user_id,
                kind=lease.kind,
                status="running",
                idempotency_key=f"privacy-race:{lease.task_id}",
                input_payload=lease.input_payload,
                started_at=lease.started_at,
                attempt_count=1,
                lease_owner=lease.lease_owner,
                lease_expires_at=NOW + timedelta(minutes=5),
            )
        )
        session.add(
            MailDraftVersionModel(
                user_id=seed.user_id,
                draft_id=seed.draft_id,
                version=1,
                to_recipients=[{"email": "recipient@example.test"}],
                cc_recipients=[],
                bcc_recipients=[],
                subject="Synthetic subject",
                body_ciphertext=b"synthetic-content",
                body_nonce=bytes(range(12)),
                body_key_version=1,
                created_at=NOW,
            )
        )
    return lease


async def _backend_pid(session: AsyncSession) -> int:
    """只读取当前事务会话标识，不改变锁或返回连接参数。"""
    pid = await session.scalar(select(func.pg_backend_pid()))
    assert isinstance(pid, int)
    return pid


async def _prove_exact_blocker(
    sessions: ManagedAsyncSessionMaker, *, holder_pid: int, waiter_pid: int
) -> None:
    """先复用有界等待，再确认阻塞者正是本轮持锁事务，避免把无关锁当作证据。"""
    await _wait_for_blocker(sessions, waiter_pid=waiter_pid)
    async with sessions() as session:
        blockers = await session.scalar(
            text("SELECT pg_blocking_pids(:waiter_pid)").bindparams(waiter_pid=waiter_pid)
        )
    assert holder_pid != waiter_pid and holder_pid in blockers


async def _assert_barrier(sessions: ManagedAsyncSessionMaker, lease: LeasedTask) -> None:
    """核对真实 CAS 与唯一 started 的外层归属和精确 metadata，不只检查 inactive 布尔值。"""
    async with sessions() as session:
        user = await session.get(UserModel, lease.user_id)
        assert user is not None and not user.is_active
        events = (
            await session.scalars(
                select(AuditEventModel).where(
                    AuditEventModel.user_id == lease.user_id,
                    AuditEventModel.event_type == "privacy.deletion_started",
                )
            )
        ).all()
        assert len(events) == 1 and events[0].task_id == lease.task_id
        assert events[0].event_metadata == {
            "schema_version": "privacy_deletion_started.v1",
            "request_id": lease.input_payload["deletion_request_id"],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier_first", [True, False], ids=["barrier-first", "claim-first"])
async def test_task27e_tool_claim_and_barrier_actual_lock_race(
    database_url: str, monkeypatch: pytest.MonkeyPatch, barrier_first: bool
) -> None:
    """首次 claim 与实际 CAS 在两种顺序均确实等锁；屏障后重投不解密、不调用、不伪造结果。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    sessions = build_session_factory(database_url)
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    worker = _PhaseCrashWorker(sessions, "barrier")
    worker._clock = _DeletionClock(NOW)
    held, release, waiter_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    holder_pids: list[int] = []
    waiter_pids: list[int] = []
    decrypt_calls: list[UUID] = []
    jobs: list[asyncio.Task[None]] = []
    claim_rejected: list[bool] = []
    original_binding = worker._lock_binding
    original_lock = SqlAlchemyTrustedActionRepository.lock_execution
    original_claim = SqlAlchemyTrustedActionRepository.create_tool_claim
    original_load = SqlAlchemyTrustedActionRepository.load_command

    async def barrier_lock(
        session: AsyncSession, binding: PrivacyDeletionBinding
    ) -> tuple[TaskRunModel, UserModel]:
        """只暂停原 barrier 用户锁，或登记它即将等待的真实会话。"""
        if not barrier_first:
            waiter_pids.append(await _backend_pid(session))
            waiter_ready.set()
        result = await original_binding(session, binding)
        if barrier_first:
            holder_pids.append(await _backend_pid(session))
            held.set()
            await release.wait()
        return result

    async def claim_lock(
        repository: SqlAlchemyTrustedActionRepository,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
    ) -> TrustedActionExecutionSnapshot | None:
        """在真正的 claim 事务进入原锁序之前登记 waiter；不替换任何查询或授权结果。"""
        if barrier_first and not waiter_ready.is_set():
            waiter_pids.append(await _backend_pid(repository._session))
            waiter_ready.set()
        return await original_lock(
            repository, task_id=task_id, approval_id=approval_id, operation_id=operation_id
        )

    async def insert_claim(
        repository: SqlAlchemyTrustedActionRepository,
        *,
        snapshot: TrustedActionExecutionSnapshot,
        execution_id: UUID,
        idempotency_key: str,
        claimed_at: datetime,
    ) -> None:
        """让真实 INSERT/flush 完成后仍持原事务锁，删除只能等到 claim 提交。"""
        await original_claim(
            repository,
            snapshot=snapshot,
            execution_id=execution_id,
            idempotency_key=idempotency_key,
            claimed_at=claimed_at,
        )
        if not barrier_first:
            holder_pids.append(await _backend_pid(repository._session))
            held.set()
            await release.wait()

    async def observe_decryption(
        repository: SqlAlchemyTrustedActionRepository, *, user_id: UUID, approval_id: UUID
    ) -> dict[str, object] | None:
        """保留原 AEAD 读取，零调用断言才能证明 preparation 的真实拒绝顺序。"""
        decrypt_calls.append(approval_id)
        return await original_load(repository, user_id=user_id, approval_id=approval_id)

    async def claim() -> None:
        """执行完整生产 claim 用例，不绕过其应用策略。"""
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )

    async def first_claim() -> None:
        """只记录预期 inactive 分支，其他异常继续使本轮失败。"""
        try:
            await claim()
        except TrustedActionUserInactive:
            claim_rejected.append(True)

    try:
        lease = await _seed_delete_for_action(sessions, seed)
        monkeypatch.setattr(worker, "_lock_binding", barrier_lock)
        monkeypatch.setattr(SqlAlchemyTrustedActionRepository, "lock_execution", claim_lock)
        monkeypatch.setattr(SqlAlchemyTrustedActionRepository, "create_tool_claim", insert_claim)
        monkeypatch.setattr(SqlAlchemyTrustedActionRepository, "load_command", observe_decryption)

        async def delete() -> None:
            """生产 CAS 提交后停在已提交屏障，保留 claim/preparation 中间态供独立读取。"""
            with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
                await worker.execute(lease)

        async with asyncio.timeout(15):
            jobs.append(asyncio.create_task(delete() if barrier_first else first_claim()))
            await held.wait()
            jobs.append(asyncio.create_task(first_claim() if barrier_first else delete()))
            await waiter_ready.wait()
            await _prove_exact_blocker(
                sessions, holder_pid=holder_pids[0], waiter_pid=waiter_pids[0]
            )
            assert all(not job.done() for job in jobs)
            release.set()
            await asyncio.gather(*jobs)
        assert claim_rejected == ([True] if barrier_first else [])
        await _assert_barrier(sessions, lease)
        for _ in range(2):
            with pytest.raises(TrustedActionUserInactive):
                await claim()
            with pytest.raises(StateConflictError):
                await workflow.execute_or_reconcile(
                    task_id=seed.task_id,
                    approval_id=seed.approval_id,
                    operation_id=seed.operation_id,
                    expected_payload_hash=payload_hash,
                    lease_owner=seed.owner,
                )
        async with sessions() as session:
            executions = (
                await session.scalars(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                )
            ).all()
            assert len(executions) == (0 if barrier_first else 1)
            if executions:
                execution = executions[0]
                assert execution.status == "claimed" and execution.claimed_at is not None
                assert execution.request_started_at is None and execution.write_attempt_count == 0
                assert execution.result_summary is None and execution.error_code is None
            task = await session.get(TaskRunModel, seed.task_id)
            assert task is not None and task.status == "running" and task.error_code is None
        assert decrypt_calls == []
        assert adapter.write_calls == adapter.reconcile_calls == 0
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await workflow.dispose()
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier_first", [True, False], ids=["barrier-first", "start-first"])
async def test_task27e_request_start_and_barrier_actual_lock_race(
    database_url: str, monkeypatch: pytest.MonkeyPatch, barrier_first: bool
) -> None:
    """真实 request-start 提交与 CAS 双向阻塞；开始事实一旦存在，只能按隐私 UNKNOWN 协议收敛。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    sessions = build_session_factory(database_url)
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    worker = _PhaseCrashWorker(sessions, "reconciliation")
    worker._clock = _DeletionClock(NOW)
    held, release, waiter_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    barrier_committed, continue_deletion = asyncio.Event(), asyncio.Event()
    holder_pids: list[int] = []
    waiter_pids: list[int] = []
    starts: list[RequestStartResult] = []
    decrypt_calls: list[UUID] = []
    reads: list[ProviderWriteOutcomeKind] = []
    network_calls: list[str] = []
    jobs: list[asyncio.Task[None]] = []
    original_binding = worker._lock_binding
    original_phase = worker._after_deletion_phase
    original_start = SqlAlchemyTrustedActionRepository.mark_request_started
    original_load = SqlAlchemyTrustedActionRepository.load_command
    original_read = PrivacyProviderReader.reconcile

    async def barrier_lock(
        session: AsyncSession, binding: PrivacyDeletionBinding
    ) -> tuple[TaskRunModel, UserModel]:
        """登记等待中的删除会话，或暂停已经持有用户锁的原 CAS 事务。"""
        if not barrier_first:
            waiter_pids.append(await _backend_pid(session))
            waiter_ready.set()
        result = await original_binding(session, binding)
        if barrier_first:
            holder_pids.append(await _backend_pid(session))
            held.set()
            await release.wait()
        return result

    async def after_phase(*, phase: str) -> None:
        """在同一次合法删除租约内检查已提交屏障，再继续真实清理及一次只读资格。"""
        if phase == "barrier":
            barrier_committed.set()
            await continue_deletion.wait()
        await original_phase(phase=phase)

    async def request_start(
        repository: SqlAlchemyTrustedActionRepository,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
        authorize: RequestStartAuthorizer,
    ) -> RequestStartResult:
        """不替换 start 结果；在真实持锁写入后暂停，或登记即将阻塞的原事务。"""
        if barrier_first:
            waiter_pids.append(await _backend_pid(repository._session))
            waiter_ready.set()
        result = await original_start(
            repository, snapshot=snapshot, lease_owner=lease_owner, authorize=authorize
        )
        if not barrier_first:
            holder_pids.append(await _backend_pid(repository._session))
            held.set()
            await release.wait()
        return result

    async def observe_decryption(
        repository: SqlAlchemyTrustedActionRepository, *, user_id: UUID, approval_id: UUID
    ) -> dict[str, object] | None:
        """后续重投仍执行原 preparation gate，任何命令读取都会被计数。"""
        decrypt_calls.append(approval_id)
        return await original_load(repository, user_id=user_id, approval_id=approval_id)

    async def observe_read(
        reader: PrivacyProviderReader,
        *,
        binding: PrivacyDeletionBinding,
        target: PrivacyReconciliationTarget,
    ) -> ProviderWriteOutcome:
        """调用实际无命令 Reader；记录结果枚举，绝不伪造 applied/not-applied。"""
        outcome = await original_read(reader, binding=binding, target=target)
        reads.append(outcome.kind)
        return outcome

    def forbid_network(request: httpx.Request) -> httpx.Response:
        """没有供应商结果定位的 request-start 只能零网络 UNKNOWN，不能猜测资源。"""
        network_calls.append(request.method)
        raise AssertionError("provider network must not be called")

    try:
        lease = await _seed_delete_for_action(sessions, seed)
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        snapshot = await _load_dispatch_snapshot_for_test(database_url, seed)
        assert snapshot.execution.status is ToolExecutionStatus.CLAIMED
        worker._privacy_reader = PrivacyProviderReader(
            store=SqlAlchemyPrivacyReconciliationStore(sessions, clock=worker._clock),
            cipher=AeadCipher(bytes(range(32))),
            clock=worker._clock,
            transport=httpx.MockTransport(forbid_network),
        )
        monkeypatch.setattr(worker, "_lock_binding", barrier_lock)
        monkeypatch.setattr(worker, "_after_deletion_phase", after_phase)
        monkeypatch.setattr(
            SqlAlchemyTrustedActionRepository, "mark_request_started", request_start
        )
        monkeypatch.setattr(SqlAlchemyTrustedActionRepository, "load_command", observe_decryption)
        monkeypatch.setattr(PrivacyProviderReader, "reconcile", observe_read)

        async def start() -> None:
            """使用原事务工厂和真实策略提交开始事实，模拟提交后尚未调用供应商的边界。"""
            transactions = SqlAlchemyTrustedActionRepositoryFactory(sessions, ACTION_CIPHER)
            async with transactions() as transaction:
                result = await transaction.mark_request_started(
                    snapshot=snapshot,
                    lease_owner=seed.owner,
                    authorize=workflow._request_start_authorization_error,
                )
            starts.append(result)

        async def delete() -> None:
            """同一次 Worker 在核对阶段提交后停止，保留未知执行事实用于读取验收。"""
            with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
                await worker.execute(lease)

        async with asyncio.timeout(15):
            jobs.append(asyncio.create_task(delete() if barrier_first else start()))
            await held.wait()
            jobs.append(asyncio.create_task(start() if barrier_first else delete()))
            await waiter_ready.wait()
            await _prove_exact_blocker(
                sessions, holder_pid=holder_pids[0], waiter_pid=waiter_pids[0]
            )
            assert all(not job.done() for job in jobs)
            release.set()
            await jobs[1 if barrier_first else 0]
            await barrier_committed.wait()
            await _assert_barrier(sessions, lease)
            assert starts[0].disposition is (
                RequestStartDisposition.USER_INACTIVE
                if barrier_first
                else RequestStartDisposition.STARTED
            )
            before_cleanup = await _load_dispatch_snapshot_for_test(database_url, seed)
            assert before_cleanup.execution.status is (
                ToolExecutionStatus.CLAIMED if barrier_first else ToolExecutionStatus.EXECUTING
            )
            started_at = before_cleanup.execution.request_started_at
            assert (started_at is None) is barrier_first
            assert before_cleanup.execution.write_attempt_count == (0 if barrier_first else 1)
            for _ in range(2):
                with pytest.raises(TrustedActionUserInactive):
                    await workflow.execute_or_reconcile(
                        task_id=seed.task_id,
                        approval_id=seed.approval_id,
                        operation_id=seed.operation_id,
                        expected_payload_hash=payload_hash,
                        lease_owner=seed.owner,
                    )
            continue_deletion.set()
            await asyncio.gather(*jobs)
        async with sessions() as session:
            execution = await session.get(ToolExecutionModel, snapshot.execution.execution_id)
            task = await session.get(TaskRunModel, seed.task_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            assert execution is not None and task is not None and approval is not None
            assert execution.status == task.status == "needs_attention"
            assert execution.error_code == task.error_code == PRIVACY_RECONCILIATION_STARTED
            assert execution.request_started_at == started_at
            assert execution.write_attempt_count == (0 if barrier_first else 1)
            assert execution.reconciliation_attempt_count == 0
            assert execution.result_summary is None and execution.manual_resolution is None
            assert execution.completed_at is None
            # 当前观察点在只读核对之后、local_rows_deleted 之前；已认领命令由后一阶段
            # 删除，此时只能保留原绑定且绝不解密，不能把提前清空当作协议要求。
            assert approval.payload_hash == payload_hash
            assert all(
                value is not None
                for value in (
                    approval.payload_ciphertext,
                    approval.payload_nonce,
                    approval.payload_key_version,
                )
            )
        assert reads == [ProviderWriteOutcomeKind.UNKNOWN]
        assert decrypt_calls == network_calls == []
        assert adapter.write_calls == adapter.reconcile_calls == 0
    finally:
        release.set()
        continue_deletion.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await workflow.dispose()
        await sessions.dispose()
