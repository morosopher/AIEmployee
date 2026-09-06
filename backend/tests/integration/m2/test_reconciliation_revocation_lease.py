"""验证撤权与在途只读核对交错时保留有效租约和严格结果提交边界。"""

import asyncio
import base64
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest
from sqlalchemy import func, select

from ai_employee.application.ports.trusted_actions import ExecutionReference, ProviderWriteOutcome
from ai_employee.application.use_cases.connections import ConnectionsUseCase
from ai_employee.config import Settings
from ai_employee.domain.actions import ToolExecutionStatus
from ai_employee.domain.connections import ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel, MailDraftModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.connections import SqlAlchemyConnectionStoreFactory
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionReconciliationRecoveryStore,
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers.reconcile_actions import execute_reconciliation_task
from tests.integration.m2.test_capability_revocation_races import (
    _DisconnectRevokeAdapter,
    _FixedClock,
)
from tests.integration.m2.test_tool_execution_claim import (
    ACTION_CIPHER,
    NOW,
    _RecordingAdapter,
    _Seed,
    _seed_action,
    _seed_calendar_action,
)

# 使用既有 typed lifecycle 的 disposable regular 数据库；所有工厂仍由 tracker 在
# 清理前关闭，测试只调用合成 adapter，不访问真实账号或执行供应商写请求。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """只使用已完成迁移与权限准入的 regular 临时数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖 anchor fixture，避免对已准入的 regular 数据库重复迁移。"""
    del cycle5_regular_database_url
    yield


class _PausedReconciliationAdapter(_RecordingAdapter):
    """在一次只读请求已开始后暂停，让独立连接事务先完成撤权。"""

    def __init__(self) -> None:
        """初始化合成 confirmed-applied 结果、调用计数和确定性交错屏障。"""
        super().__init__()
        self.entered = asyncio.Event()
        self.release_result = asyncio.Event()

    async def reconcile(
        self, command: object, execution: ExecutionReference
    ) -> ProviderWriteOutcome:
        """保留原请求的规范结果，直到测试确认撤权事务已提交才交回 Worker。"""
        outcome = await super().reconcile(command, execution)
        self.entered.set()
        await self.release_result.wait()
        return outcome


async def _seed_reconciling_action(
    database_url: str, *, resource_kind: Literal["mail", "calendar"]
) -> _Seed:
    """复用完整冻结命令，建立有一次历史核对、等待下一轮 claim 的合成动作。

    邮件和日历使用相同的请求开始、写次数和调度形状。access/refresh 密文均真实落库，
    便于断开交错从另一个 Session 证明本地删除先于只读结果提交；合成明文不进入日志。
    """
    seed = _Seed()
    if resource_kind == "mail":
        payload_hash = await _seed_action(
            database_url,
            seed,
            existing_execution_status=ToolExecutionStatus.RECONCILING,
            request_started_at=NOW - timedelta(minutes=2),
            task_status=TaskStatus.RECONCILING,
        )
    else:
        payload_hash = await _seed_calendar_action(database_url, seed)
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            assert task is not None
            task.status = TaskStatus.RECONCILING.value
            task.error_code = "provider_write_outcome_unknown"
            task.scheduled_for = NOW - timedelta(seconds=10)
            task.lease_owner = None
            task.lease_expires_at = None
            if resource_kind == "mail":
                local = await session.get(MailDraftModel, seed.draft_id)
                execution = await session.get(ToolExecutionModel, seed.execution_id)
                assert execution is not None
            else:
                local = await session.get(CalendarChangeProposalModel, seed.draft_id)
                execution = ToolExecutionModel(
                    id=seed.execution_id,
                    task_id=seed.task_id,
                    step_id=seed.step_id,
                    tool_name="calendar.create",
                    idempotency_key=(
                        f"calendar.create:{seed.task_id}:{seed.approval_id}:1:{seed.operation_id}"
                    ),
                    operation_id=seed.operation_id,
                    request_payload_hash=payload_hash,
                    provider=seed.provider,
                    status=ToolExecutionStatus.RECONCILING.value,
                    claimed_at=NOW - timedelta(minutes=3),
                    request_started_at=NOW - timedelta(minutes=2),
                    write_attempt_count=1,
                )
                session.add(execution)
            assert local is not None
            local.status = "needs_attention"
            execution.error_code = "provider_write_outcome_unknown"
            execution.reconciliation_attempt_count = 1
            execution.last_reconciled_at = NOW - timedelta(minutes=1)
            execution.result_summary = {"kind": "unknown", "retryable": False}
            cipher = AeadCipher(b"r" * 32)
            for kind in ("access_token", "refresh_token"):
                encrypted = cipher.encrypt(
                    f"synthetic-{kind}".encode(),
                    f"{seed.user_id}:{seed.connection_id}:{kind}".encode("ascii"),
                )
                session.add(
                    EncryptedCredentialModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        credential_kind=kind,
                        ciphertext=encrypted.ciphertext,
                        nonce=encrypted.nonce,
                        key_version=encrypted.key_version,
                        token_expires_at=NOW + timedelta(hours=1),
                    )
                )
    finally:
        await sessions.dispose()
    return seed


def _worker_settings(tmp_path: Path) -> Settings:
    """向真实只读 Worker 注入与冻结 fixture 匹配的合成密钥，保持写开关关闭。"""
    key_path = tmp_path / "synthetic-master-key"
    key_path.write_text(base64.b64encode(b"x" * 32).decode("ascii"), encoding="utf-8")
    return Settings(_env_file=None, app_master_key_file=key_path)


async def _revoke(
    use_case: ConnectionsUseCase,
    seed: _Seed,
    *,
    resource_kind: Literal["mail", "calendar"],
    revocation: Literal["disable", "disconnect"],
) -> None:
    """让真实连接用例在独立事务关闭精确写能力或提交本地断开。"""
    if revocation == "disconnect":
        await use_case.disconnect(user_id=seed.user_id, connection_id=seed.connection_id)
    else:
        await use_case.disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=(
                ConnectionCapability.MAIL_SEND
                if resource_kind == "mail"
                else ConnectionCapability.CALENDAR_WRITE
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_kind", ["mail", "calendar"])
@pytest.mark.parametrize("revocation", ["disable", "disconnect"])
async def test_inflight_reconciliation_result_survives_first_and_repeated_revocation(
    database_url: str,
    tmp_path: Path,
    resource_kind: Literal["mail", "calendar"],
    revocation: Literal["disable", "disconnect"],
) -> None:
    """撤权先提交后，原有效只读 owner 的结论仍原子落库；重复撤权/投递保持幂等。"""
    seed = await _seed_reconciling_action(database_url, resource_kind=resource_kind)
    sessions = build_session_factory(database_url)
    # 独立工厂确保连接撤权不会复用 Worker 的 ORM Session 或未提交身份映射。
    revocation_sessions = build_session_factory(database_url)
    adapter = _PausedReconciliationAdapter()
    registry = ProviderAdapterRegistry(
        google_mail_action=adapter if resource_kind == "mail" else None,
        google_calendar_action=adapter if resource_kind == "calendar" else None,
    )
    settings = _worker_settings(tmp_path)
    revoker = _DisconnectRevokeAdapter()
    use_case = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(revocation_sessions),
        AeadCipher(b"r" * 32),
        {"google": revoker},
        _FixedClock(),
    )
    worker = asyncio.create_task(
        execute_reconciliation_task(
            task_id=seed.task_id,
            session_factory=sessions,
            settings=settings,
            adapters=registry,
            lease_owner="inflight-read-owner",
            now=NOW,
        )
    )
    try:
        await asyncio.wait_for(adapter.entered.wait(), timeout=10)
        async with sessions() as session:
            task_before = await session.get(TaskRunModel, seed.task_id)
            execution_before = await session.get(ToolExecutionModel, seed.execution_id)
        assert task_before is not None and execution_before is not None
        assert task_before.current_step == "reconcile"
        assert task_before.lease_owner == "inflight-read-owner"
        for _ in range(2):
            await _revoke(use_case, seed, resource_kind=resource_kind, revocation=revocation)
        async with sessions() as session:
            task_revoked = await session.get(TaskRunModel, seed.task_id)
            execution_revoked = await session.get(ToolExecutionModel, seed.execution_id)
            connection = await session.get(OAuthConnectionModel, seed.connection_id)
            credential_count = await session.scalar(
                select(func.count())
                .select_from(EncryptedCredentialModel)
                .where(EncryptedCredentialModel.connection_id == seed.connection_id)
            )
            capability = await session.scalar(
                select(ConnectionCapabilityModel).where(
                    ConnectionCapabilityModel.connection_id == seed.connection_id,
                    ConnectionCapabilityModel.capability
                    == ("mail.send" if resource_kind == "mail" else "calendar.write"),
                )
            )
            pending_execute = tuple(
                await session.scalars(
                    select(OutboxEventModel.id).where(
                        OutboxEventModel.aggregate_id == seed.task_id,
                        OutboxEventModel.topic == "task.execute",
                        OutboxEventModel.published_at.is_(None),
                    )
                )
            )
        assert connection is not None and capability is not None
        assert credential_count == (0 if revocation == "disconnect" else 2)
        assert connection.status == ("disconnected" if revocation == "disconnect" else "connected")
        assert capability.status == ("revoked" if revocation == "disconnect" else "disabled")
        assert len(revoker.revoked_tokens) == (1 if revocation == "disconnect" else 0)
        # 先从独立 Session 证明撤权（及断开密文删除）已经提交，再允许原只读结果返回。
        adapter.release_result.set()
        worker_results = await asyncio.gather(worker, return_exceptions=True)
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            execution = await session.get(ToolExecutionModel, seed.execution_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            local = await session.get(
                MailDraftModel if resource_kind == "mail" else CalendarChangeProposalModel,
                seed.draft_id,
            )
            audits = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type)
                    .where(AuditEventModel.task_id == seed.task_id)
                    .order_by(AuditEventModel.id)
                )
            )
        # RED 必须落在有效结论未持久化，不能只测试租约字段或 mock 调用顺序。
        assert task is not None and task.status == "succeeded"
        assert worker_results == [True]
        assert execution is not None and execution.status == "succeeded"
        assert execution.result_summary is not None
        assert execution.result_summary["kind"] == "confirmed_applied"
        assert execution.provider_resource_id == "synthetic-resource"
        assert execution.write_attempt_count == 1 and execution.reconciliation_attempt_count == 2
        assert execution.request_started_at == execution_before.request_started_at
        assert approval is not None and approval.status == "approved"
        assert local is not None and local.status == (
            "sent" if resource_kind == "mail" else "applied"
        )
        assert local.connection_id == seed.connection_id
        assert task.lease_owner is task.lease_expires_at is task.scheduled_for is None
        assert task_revoked is not None and execution_revoked is not None
        assert (
            task_revoked.lease_owner,
            task_revoked.lease_expires_at,
            task_revoked.scheduled_for,
        ) == (
            task_before.lease_owner,
            task_before.lease_expires_at,
            task_before.scheduled_for,
        )
        assert execution_revoked.reconciliation_attempt_count == 1
        assert execution_revoked.last_reconciled_at == execution_before.last_reconciled_at
        assert execution_revoked.error_code == (
            "connection_scope_missing"
            if revocation == "disconnect"
            else "connection_capability_disabled"
        )
        assert pending_execute == ()
        assert audits.count("tool.reconciling") == 1
        assert audits.count("tool.succeeded") == audits.count("task.succeeded") == 1
        assert (
            await execute_reconciliation_task(
                task_id=seed.task_id,
                session_factory=sessions,
                settings=settings,
                adapters=registry,
                lease_owner="duplicate-read-owner",
                now=NOW,
            )
            is False
        )
        async with sessions() as session:
            repeated = await session.get(ToolExecutionModel, seed.execution_id)
            audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(AuditEventModel.task_id == seed.task_id)
            )
        assert repeated is not None and repeated.status == "succeeded"
        assert repeated.completed_at == execution.completed_at
        assert repeated.result_summary == execution.result_summary
        assert repeated.reconciliation_attempt_count == execution.reconciliation_attempt_count
        assert audit_count == len(audits)
        assert adapter.reconcile_calls == 1 and adapter.write_calls == 0
    finally:
        adapter.release_result.set()
        await asyncio.gather(worker, return_exceptions=True)
        await sessions.dispose()
        await revocation_sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_kind", ["mail", "calendar"])
@pytest.mark.parametrize("revocation", ["disable", "disconnect"])
@pytest.mark.parametrize("lease_state", ["expired", "replaced"])
async def test_revocation_cannot_authorize_stale_reconciliation_result(
    database_url: str,
    tmp_path: Path,
    resource_kind: Literal["mail", "calendar"],
    revocation: Literal["disable", "disconnect"],
    lease_state: Literal["expired", "replaced"],
) -> None:
    """过期或已被接管的旧结果在撤权前后都被拒绝，当前有效 owner 仍可提交。

    两轮 owner 均从真实 claim 边界取得；只推进数据库到期事实以模拟失去租约，不修改
    结果 CAS 或绑定。拒绝后核对计数和 UNKNOWN 保持不变，再由有效新 owner 完成只读结果。
    """
    seed = await _seed_reconciling_action(database_url, resource_kind=resource_kind)
    sessions = build_session_factory(database_url)
    revocation_sessions = build_session_factory(database_url)
    adapter = _RecordingAdapter()
    try:
        async with sessions.begin() as session:
            original = await SqlAlchemyTrustedActionRepository(
                session, ACTION_CIPHER
            ).claim_reconciliation(
                task_id=seed.task_id,
                lease_owner="stale-read-owner",
                now=NOW,
                lease_expires_at=NOW + timedelta(minutes=1),
            )
        assert original is not None
        async with revocation_sessions.begin() as session:
            task = await session.get(TaskRunModel, seed.task_id, with_for_update=True)
            database_now = await session.scalar(select(func.clock_timestamp()))
            assert task is not None and database_now is not None
            task.lease_expires_at = database_now - timedelta(seconds=1)
        replacement = None
        if lease_state == "replaced":
            async with sessions.begin() as session:
                replacement = await SqlAlchemyTrustedActionRepository(
                    session, ACTION_CIPHER
                ).claim_reconciliation(
                    task_id=seed.task_id,
                    lease_owner="replacement-read-owner",
                    now=NOW,
                    lease_expires_at=NOW + timedelta(minutes=1),
                )
            assert replacement is not None
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(revocation_sessions),
            AeadCipher(b"r" * 32),
            {"google": _DisconnectRevokeAdapter()},
            _FixedClock(),
        )
        for after_revocation in (False, True):
            if after_revocation:
                await _revoke(use_case, seed, resource_kind=resource_kind, revocation=revocation)
            # 第一次明确证明原 live owner 已经因 expiry/接管失效；第二次证明撤权没有
            # 放宽同一快照的提交授权。失败事务退出并回滚后才继续读取持久事实。
            with pytest.raises(StateConflictError) as rejected:
                async with sessions.begin() as session:
                    await SqlAlchemyTrustedActionRepository(
                        session, ACTION_CIPHER
                    ).persist_provider_outcome(
                        snapshot=original,
                        outcome=adapter.outcome,
                        completed_at=NOW,
                        from_reconciliation=True,
                        may_retry_write=False,
                        lease_owner="stale-read-owner",
                    )
            assert rejected.value.error_code == "trusted_action_unavailable"
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            execution = await session.get(ToolExecutionModel, seed.execution_id)
        assert task is not None and task.status == "reconciling"
        assert execution is not None and execution.status == "reconciling"
        assert execution.result_summary == {"kind": "unknown", "retryable": False}
        assert execution.request_started_at == original.execution.request_started_at
        assert execution.write_attempt_count == 1 and execution.reconciliation_attempt_count == 1
        assert execution.provider_resource_id is None
        if replacement is not None:
            assert task.lease_owner == "replacement-read-owner"
            assert task.lease_expires_at == NOW + timedelta(minutes=1)
            async with sessions.begin() as session:
                await SqlAlchemyTrustedActionRepository(
                    session, ACTION_CIPHER
                ).persist_provider_outcome(
                    snapshot=replacement,
                    outcome=adapter.outcome,
                    completed_at=NOW,
                    from_reconciliation=True,
                    may_retry_write=False,
                    lease_owner="replacement-read-owner",
                )
        else:
            assert task.lease_owner is None and task.lease_expires_at is None
            assert (
                await execute_reconciliation_task(
                    task_id=seed.task_id,
                    session_factory=sessions,
                    settings=_worker_settings(tmp_path),
                    adapters=ProviderAdapterRegistry(
                        google_mail_action=adapter if resource_kind == "mail" else None,
                        google_calendar_action=adapter if resource_kind == "calendar" else None,
                    ),
                    lease_owner="recovered-read-owner",
                    now=NOW,
                )
                is True
            )
        async with sessions() as session:
            execution = await session.get(ToolExecutionModel, seed.execution_id)
        assert execution is not None and execution.status == "succeeded"
        assert execution.write_attempt_count == 1 and execution.reconciliation_attempt_count == 2
        assert adapter.write_calls == 0
    finally:
        await sessions.dispose()
        await revocation_sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("resource_kind", ["mail", "calendar"])
@pytest.mark.parametrize("revocation", ["disable", "disconnect"])
async def test_preserved_read_lease_recovers_after_worker_crash_and_expiry(
    database_url: str,
    tmp_path: Path,
    resource_kind: Literal["mail", "calendar"],
    revocation: Literal["disable", "disconnect"],
) -> None:
    """撤权保留的只读 owner 崩溃后，既有 PostgreSQL 到期恢复仍只投递一轮核对。"""
    seed = await _seed_reconciling_action(database_url, resource_kind=resource_kind)
    sessions = build_session_factory(database_url)
    revocation_sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            snapshot = await SqlAlchemyTrustedActionRepository(
                session, ACTION_CIPHER
            ).claim_reconciliation(
                task_id=seed.task_id,
                lease_owner="crashed-read-owner",
                now=NOW,
                lease_expires_at=NOW + timedelta(minutes=1),
            )
        assert snapshot is not None
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(revocation_sessions),
            AeadCipher(b"r" * 32),
            {"google": _DisconnectRevokeAdapter()},
            _FixedClock(),
        )
        await _revoke(use_case, seed, resource_kind=resource_kind, revocation=revocation)
        recovery = SqlAlchemyTrustedActionReconciliationRecoveryStore(sessions)
        await recovery.recover_due_reconciliations(now=NOW, limit=100)
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            active_pending = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "task.execute",
                    OutboxEventModel.published_at.is_(None),
                )
            )
        assert task is not None and task.lease_owner == "crashed-read-owner"
        assert task.lease_expires_at == NOW + timedelta(minutes=1)
        assert task.scheduled_for == NOW - timedelta(seconds=10)
        assert active_pending == 0
        # 模拟进程被终止而没有机会执行 release；只让数据库租约过期，不主动创建消息，
        # 从而证明原 PostgreSQL recovery 能接管保留的 lease，且无需等待真实分钟流逝。
        async with revocation_sessions.begin() as session:
            task = await session.get(TaskRunModel, seed.task_id, with_for_update=True)
            database_now = await session.scalar(select(func.clock_timestamp()))
            assert task is not None and database_now is not None
            task.lease_expires_at = database_now - timedelta(seconds=1)
        await recovery.recover_due_reconciliations(now=NOW, limit=100)
        await recovery.recover_due_reconciliations(now=NOW, limit=100)
        async with sessions() as session:
            pending = tuple(
                await session.scalars(
                    select(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id == seed.task_id,
                        OutboxEventModel.topic == "task.execute",
                        OutboxEventModel.published_at.is_(None),
                    )
                )
            )
        assert len(pending) == 1
        assert pending[0].deduplication_key.startswith(
            f"task.execute:{seed.task_id}:reconcile-recovery:1:"
        )
        assert pending[0].available_at == NOW - timedelta(seconds=10)
        adapter = _RecordingAdapter()
        assert (
            await execute_reconciliation_task(
                task_id=seed.task_id,
                session_factory=sessions,
                settings=_worker_settings(tmp_path),
                adapters=ProviderAdapterRegistry(
                    google_mail_action=adapter if resource_kind == "mail" else None,
                    google_calendar_action=adapter if resource_kind == "calendar" else None,
                ),
                lease_owner="post-crash-read-owner",
                now=NOW,
            )
            is True
        )
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            execution = await session.get(ToolExecutionModel, seed.execution_id)
        assert task is not None and task.status == "succeeded"
        assert task.lease_owner is task.lease_expires_at is task.scheduled_for is None
        assert execution is not None and execution.status == "succeeded"
        assert execution.write_attempt_count == 1 and execution.reconciliation_attempt_count == 2
        assert adapter.reconcile_calls == 1 and adapter.write_calls == 0
    finally:
        await sessions.dispose()
        await revocation_sessions.dispose()
