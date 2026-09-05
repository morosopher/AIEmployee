"""验证连接撤权与可信动作认领共享 PostgreSQL 原子边界。"""

import asyncio
import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.oauth import OAuthRevocationResult, OAuthRevocationStatus
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ProviderWriteOutcome,
)
from ai_employee.application.use_cases.connections import (
    ConnectionNotFoundError,
    ConnectionsUseCase,
)
from ai_employee.application.use_cases.trusted_actions import TrustedActionExecutionUseCase
from ai_employee.config import Settings
from ai_employee.domain.actions import MailDraftStatus, ToolExecutionStatus
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    canonical_provider_identity_key,
)
from ai_employee.domain.errors import StateConflictError, TransientProviderError
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.database_url import (
    TestDatabaseUrl as ValidatedTestDatabaseUrl,
)
from ai_employee.infrastructure.db.models.actions import CalendarChangeProposalModel, MailDraftModel
from ai_employee.infrastructure.db.models.identity import UserModel
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
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.connections import (
    SqlAlchemyConnectionStore,
    SqlAlchemyConnectionStoreFactory,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.google.oauth import GOOGLE_REVOKE_URL, GoogleOAuthAdapter
from ai_employee.integrations.microsoft.oauth import MicrosoftOAuthAdapter
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers.reconcile_actions import execute_reconciliation_task

NOW = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)

# 本文件需要 PostgreSQL 真行锁；沿用 Cycle 5 官方 disposable regular 数据库，并让
# tracker 在清理数据库前先关闭测试创建的全部连接池。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """只使用已完成 typed lifecycle 与 grants 校验的 regular head 数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖 anchor 迁移 fixture；disposable helper 已完成准入。"""
    del cycle5_regular_database_url
    yield


@dataclass(frozen=True, slots=True)
class _FixedClock:
    """让 API 用例与数据库断言共享一个确定 UTC 时刻。"""

    def now(self) -> datetime:
        """返回冻结时刻。"""
        return NOW


@dataclass(frozen=True, slots=True)
class _ActionSeed:
    """保存一个合成邮件动作的稳定关系标识。"""

    user_id: UUID
    connection_id: UUID
    draft_id: UUID
    task_id: UUID
    step_id: UUID
    approval_id: UUID
    operation_id: UUID


@dataclass(slots=True)
class _ClaimAdapter:
    """仅用于 claim 竞态的合成 adapter；任何分支都不得触达 provider。"""

    provider: str = "google"
    write_calls: int = 0

    def validate_for_approval(self, command: object) -> ApprovalPreflightResult:
        """返回空预检结果；本组测试不提交新审批。"""
        del command
        return ApprovalPreflightResult()

    async def execute(self, command: object) -> ProviderWriteOutcome:
        """若意外进入真实写分支立即失败，保护零 provider-write 断言。"""
        del command
        self.write_calls += 1
        raise AssertionError("provider execute must not run during claim race")

    async def reconcile(self, command: object, execution: object) -> ProviderWriteOutcome:
        """竞态测试不执行只读核对。"""
        del command, execution
        raise AssertionError("provider reconcile must not run during claim race")


@dataclass(slots=True)
class _DisconnectRevokeAdapter:
    """仅记录断开时的窄 revoke；不会执行邮件或日历写请求。"""

    provider: str = "google"
    revoked_tokens: list[str] = field(default_factory=list)

    async def revoke(self, token: str) -> OAuthRevocationResult:
        """记录一次已提交本地删除后的 revoke，并返回确定成功。"""
        self.revoked_tokens.append(token)
        return OAuthRevocationResult(OAuthRevocationStatus.REVOKED)


async def _seed_mail_action(
    database_url: str,
    *,
    approval_status: ApprovalStatus = ApprovalStatus.PENDING,
    task_status: TaskStatus = TaskStatus.WAITING_APPROVAL,
) -> _ActionSeed:
    """建立未认领审批、awaiting draft 与未发布初始投递事实。"""
    seed = _ActionSeed(*(uuid4() for _ in range(7)))
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            session.add(
                UserModel(
                    id=seed.user_id,
                    # 一个竞态用例可同时建立多个用户；合成登录邮箱也必须满足全局唯一约束。
                    email=f"revocation-{seed.user_id.hex}@example.test",
                    display_name="Synthetic Owner",
                    password_hash=None,
                    timezone="UTC",
                    locale="zh-CN",
                    brief_time=time(8, 0),
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                OAuthConnectionModel(
                    id=seed.connection_id,
                    user_id=seed.user_id,
                    provider="google",
                    provider_account_id="synthetic-revocation-account",
                    provider_tenant_id="",
                    account_type="google",
                    account_email="revocation-owner@example.test",
                    scopes=["scope:mail.read", "scope:mail.send"],
                    status="connected",
                )
            )
            await session.flush()
            session.add_all(
                (
                    ConnectionCapabilityModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        capability=ConnectionCapability.MAIL_READ.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=["scope:mail.read"],
                    ),
                    ConnectionCapabilityModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        capability=ConnectionCapability.MAIL_SEND.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=["scope:mail.send"],
                    ),
                    MailDraftModel(
                        id=seed.draft_id,
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        creation_idempotency_key=f"draft:{seed.draft_id}",
                        creation_payload_hash="d" * 64,
                        mode="new",
                        current_version=1,
                        status=MailDraftStatus.AWAITING_APPROVAL.value,
                        retain_until=NOW + timedelta(days=30),
                    ),
                    TaskRunModel(
                        id=seed.task_id,
                        user_id=seed.user_id,
                        kind="trusted_action",
                        status=task_status.value,
                        idempotency_key=f"task:{seed.task_id}",
                        input_payload={
                            "approval_id": str(seed.approval_id),
                            "operation_id": str(seed.operation_id),
                        },
                        graph_thread_id=str(seed.task_id),
                        lease_owner=("claim-owner" if task_status is TaskStatus.RUNNING else None),
                        lease_expires_at=(
                            NOW + timedelta(minutes=5)
                            if task_status is TaskStatus.RUNNING
                            else None
                        ),
                        started_at=(
                            NOW - timedelta(seconds=1)
                            if task_status is TaskStatus.RUNNING
                            else None
                        ),
                        scheduled_for=NOW + timedelta(minutes=2),
                        retry_recovery_at=NOW + timedelta(minutes=3),
                        approval_checkpoint_recovery_at=NOW + timedelta(minutes=1),
                    ),
                )
            )
            await session.flush()
            session.add(
                TaskStepModel(
                    id=seed.step_id,
                    task_id=seed.task_id,
                    sequence=1,
                    name="await_approval",
                    kind="trusted_action",
                    status="running",
                    input_summary={"action": "mail.send", "proposal_version": 1},
                )
            )
            await session.flush()
            session.add_all(
                (
                    ApprovalRequestModel(
                        id=seed.approval_id,
                        task_id=seed.task_id,
                        step_id=seed.step_id,
                        version=1,
                        action="mail.send",
                        payload={"storage": "encrypted", "schema_version": "mail_send.v1"},
                        schema_version="mail_send.v1",
                        risk_level="high",
                        payload_hash="a" * 64,
                        proposal_kind="mail_draft",
                        proposal_id=seed.draft_id,
                        proposal_version=1,
                        preview_markdown="",
                        status=approval_status.value,
                        expires_at=NOW + timedelta(minutes=5),
                        approved_execution_deadline_at=(
                            NOW + timedelta(minutes=5)
                            if approval_status is ApprovalStatus.APPROVED
                            else None
                        ),
                        decided_at=(
                            NOW - timedelta(minutes=1)
                            if approval_status is ApprovalStatus.APPROVED
                            else None
                        ),
                        decided_by_user_id=(
                            seed.user_id if approval_status is ApprovalStatus.APPROVED else None
                        ),
                    ),
                    AuditEventModel(
                        user_id=seed.user_id,
                        task_id=seed.task_id,
                        event_type="approval.requested",
                        actor_type="user",
                        actor_id=str(seed.user_id),
                        event_metadata={},
                    ),
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=seed.task_id,
                        deduplication_key=f"task.execute:{seed.task_id}:initial",
                        payload={"task_id": str(seed.task_id)},
                        available_at=NOW,
                    ),
                )
            )
    finally:
        await sessions.dispose()
    return seed


async def _install_execution_state(
    database_url: str,
    seed: _ActionSeed,
    *,
    execution_status: ToolExecutionStatus,
    task_status: TaskStatus,
    draft_status: MailDraftStatus,
    approval_status: ApprovalStatus,
    request_started_at: datetime | None,
    write_attempt_count: int,
    result_summary: dict[str, object] | None,
) -> None:
    """把合成动作推进到指定已认领状态，供撤权边界 characterization 复用。"""
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            assert task is not None and approval is not None and draft is not None
            task.status = task_status.value
            task.lease_owner = "claim-owner" if task_status is TaskStatus.RUNNING else None
            task.lease_expires_at = (
                NOW + timedelta(minutes=5) if task_status is TaskStatus.RUNNING else None
            )
            task.scheduled_for = (
                NOW if task_status in {TaskStatus.RECONCILING, TaskStatus.RETRY_SCHEDULED} else None
            )
            task.retry_recovery_at = None
            task.approval_checkpoint_recovery_at = None
            task.finished_at = None
            approval.status = approval_status.value
            draft.status = draft_status.value
            session.add(
                ToolExecutionModel(
                    id=seed.operation_id,
                    task_id=seed.task_id,
                    step_id=seed.step_id,
                    tool_name="mail.send",
                    idempotency_key=f"mail.send:{seed.task_id}:{seed.approval_id}:1:{seed.operation_id}",
                    operation_id=seed.operation_id,
                    request_payload_hash="a" * 64,
                    provider="google",
                    status=execution_status.value,
                    claimed_at=NOW,
                    request_started_at=request_started_at,
                    write_attempt_count=write_attempt_count,
                    reconciliation_attempt_count=0,
                    result_summary=result_summary,
                )
            )
    finally:
        await sessions.dispose()


def _claim_settings() -> Settings:
    """构造只允许合成连接身份的真实写门禁。"""
    return Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=True,
        google_writes_enabled=True,
        write_test_account_allowlist=[
            canonical_provider_identity_key("google", "", "synthetic-revocation-account")
        ],
    )


def _claim_workflow(
    database_url: str,
    adapter: _ClaimAdapter,
) -> TrustedActionExecutionUseCase:
    """组装只执行 claim 事务的可信动作用例。"""
    sessions = build_session_factory(database_url)
    return TrustedActionExecutionUseCase(
        transactions=SqlAlchemyTrustedActionRepositoryFactory(
            sessions,
            ActionPayloadCipher.from_key(b"x" * 32),
        ),
        adapters=ProviderAdapterRegistry(google_mail_action=adapter),
        write_policy=_claim_settings(),
        clock=lambda: NOW,
        dispose=sessions.dispose,
    )


async def _postgres_backend_pid(session: AsyncSession) -> int:
    """读取事务 backend PID，供竞态测试证明等待发生在 PostgreSQL 行锁上。"""
    value = await session.scalar(text("SELECT pg_backend_pid()"))
    assert isinstance(value, int)
    return value


async def _wait_for_postgres_blockers(
    sessions: ManagedAsyncSessionMaker,
    *,
    waiting_pid: int,
) -> tuple[int, ...]:
    """轮询真实阻塞关系，不以固定墙钟 sleep 猜测事务先后。"""
    async with asyncio.timeout(5):
        async with sessions() as observer:
            while True:
                blockers = await observer.scalar(
                    text("SELECT pg_blocking_pids(:waiting_pid)"),
                    {"waiting_pid": waiting_pid},
                )
                normalized = tuple(int(pid) for pid in blockers or ())
                if normalized:
                    return normalized
                await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_disable_write_capability_invalidates_pending_approval_atomically(
    database_url: str,
) -> None:
    """只改能力行会漏掉审批、任务、本地对象及生命周期 Outbox。"""
    seed = await _seed_mail_action(database_url)
    sessions = build_session_factory(database_url)
    try:
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            AeadCipher(b"r" * 32),
            {},
            _FixedClock(),
        )
        await use_case.disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )

        async with sessions() as session:
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            audit_types = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type)
                    .where(AuditEventModel.task_id == seed.task_id)
                    .order_by(AuditEventModel.id)
                )
            )
            outbox_topics = tuple(
                await session.scalars(
                    select(OutboxEventModel.topic)
                    .where(OutboxEventModel.aggregate_id == seed.task_id)
                    .order_by(OutboxEventModel.topic)
                )
            )
        assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
        assert task is not None and task.status == TaskStatus.CANCELLED.value
        assert task.lease_owner is task.lease_expires_at is task.scheduled_for is None
        assert task.retry_recovery_at is None
        assert task.approval_checkpoint_recovery_at is None
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert audit_types == ("approval.requested", "approval.invalidated", "task.cancelled")
        assert outbox_topics == ("approval.invalidated", "task.cancelled")
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_disable_write_capability_cancels_queued_action_once_on_repeated_delivery(
    database_url: str,
) -> None:
    """approved queued 动作尚无 claim 时必须取消，重复关闭不得复制审计或 Outbox。"""
    seed = await _seed_mail_action(
        database_url,
        approval_status=ApprovalStatus.APPROVED,
        task_status=TaskStatus.QUEUED,
    )
    sessions = build_session_factory(database_url)
    try:
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            AeadCipher(b"r" * 32),
            {},
            _FixedClock(),
        )
        for _ in range(2):
            await use_case.disable_capability(
                user_id=seed.user_id,
                connection_id=seed.connection_id,
                capability=ConnectionCapability.MAIL_SEND,
            )

        async with sessions() as session:
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            audit_types = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type)
                    .where(
                        AuditEventModel.task_id == seed.task_id,
                        AuditEventModel.event_type.in_(("approval.invalidated", "task.cancelled")),
                    )
                    .order_by(AuditEventModel.event_type)
                )
            )
            outbox_topics = tuple(
                await session.scalars(
                    select(OutboxEventModel.topic)
                    .where(OutboxEventModel.aggregate_id == seed.task_id)
                    .order_by(OutboxEventModel.topic)
                )
            )
        assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
        assert task is not None and task.status == TaskStatus.CANCELLED.value
        assert (
            task.lease_owner
            is task.lease_expires_at
            is task.scheduled_for
            is task.retry_recovery_at
            is task.approval_checkpoint_recovery_at
            is None
        )
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert audit_types == ("approval.invalidated", "task.cancelled")
        assert outbox_topics == ("approval.invalidated", "task.cancelled")
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_disable_wins_claim_race_without_creating_tool_execution(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """撤权事务先持有 Connection 锁时，等待中的 claim 只能观察到取消事实。"""
    seed = await _seed_mail_action(
        database_url,
        approval_status=ApprovalStatus.APPROVED,
        task_status=TaskStatus.RUNNING,
    )
    disable_locked = asyncio.Event()
    claim_entered = asyncio.Event()
    release_disable = asyncio.Event()
    loop = asyncio.get_running_loop()
    disable_pid_ready: asyncio.Future[int] = loop.create_future()
    claim_pid_ready: asyncio.Future[int] = loop.create_future()
    original_disable = SqlAlchemyConnectionStore.disable_capability
    original_lock_execution = SqlAlchemyTrustedActionRepository.lock_execution

    async def pause_disable(self: SqlAlchemyConnectionStore, **values: Any) -> None:
        """在能力与动作状态已写入、事务仍持锁时暂停。"""
        await original_disable(self, **values)
        disable_pid_ready.set_result(await _postgres_backend_pid(self._session))
        disable_locked.set()
        await release_disable.wait()

    async def observe_claim_entry(
        self: SqlAlchemyTrustedActionRepository,
        **values: Any,
    ) -> Any:
        """确认 claim 已进入独立数据库会话，再释放撤权事务。"""
        claim_pid_ready.set_result(await _postgres_backend_pid(self._session))
        claim_entered.set()
        return await original_lock_execution(self, **values)

    monkeypatch.setattr(SqlAlchemyConnectionStore, "disable_capability", pause_disable)
    monkeypatch.setattr(
        SqlAlchemyTrustedActionRepository,
        "lock_execution",
        observe_claim_entry,
    )
    disable_sessions = build_session_factory(database_url)
    disable_use_case = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(disable_sessions),
        AeadCipher(b"r" * 32),
        {},
        _FixedClock(),
    )
    adapter = _ClaimAdapter()
    workflow = _claim_workflow(database_url, adapter)
    disable_task = asyncio.create_task(
        disable_use_case.disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )
    )
    claim_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(disable_locked.wait(), timeout=2)
        claim_task = asyncio.create_task(
            workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash="a" * 64,
                lease_owner="claim-owner",
            )
        )
        disable_pid = await asyncio.wait_for(asyncio.shield(disable_pid_ready), timeout=2)
        claim_pid = await asyncio.wait_for(asyncio.shield(claim_pid_ready), timeout=2)
        blockers = await _wait_for_postgres_blockers(disable_sessions, waiting_pid=claim_pid)
        assert disable_pid in blockers
        await asyncio.wait_for(claim_entered.wait(), timeout=2)
        release_disable.set()
        with pytest.raises(StateConflictError):
            await claim_task
        await disable_task
    finally:
        release_disable.set()
        if claim_task is not None and not claim_task.done():
            claim_task.cancel()
            await asyncio.gather(claim_task, return_exceptions=True)
        if not disable_task.done():
            disable_task.cancel()
            await asyncio.gather(disable_task, return_exceptions=True)
        await workflow.dispose()
        await disable_sessions.dispose()

    sessions = build_session_factory(database_url)
    try:
        async with sessions() as session:
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await sessions.dispose()
    assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
    assert task is not None and task.status == TaskStatus.CANCELLED.value
    assert execution_count == 0
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_claim_wins_disable_race_moves_existing_execution_to_reconciliation(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claim 先提交 ToolExecution 后，撤权不得伪装取消而应移交只读核对。"""
    seed = await _seed_mail_action(
        database_url,
        approval_status=ApprovalStatus.APPROVED,
        task_status=TaskStatus.RUNNING,
    )
    claim_created = asyncio.Event()
    release_claim = asyncio.Event()
    disable_entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    claim_pid_ready: asyncio.Future[int] = loop.create_future()
    disable_pid_ready: asyncio.Future[int] = loop.create_future()
    original_create_claim = SqlAlchemyTrustedActionRepository.create_tool_claim
    from ai_employee.infrastructure.db.repositories import connections as connections_repository

    original_invalidate = connections_repository.invalidate_unclaimed_actions_for_connection

    async def pause_claim(
        self: SqlAlchemyTrustedActionRepository,
        **values: Any,
    ) -> None:
        """在 ToolExecution 已插入但事务尚未提交时暂停 claim。"""
        await original_create_claim(self, **values)
        claim_pid_ready.set_result(await _postgres_backend_pid(self._session))
        claim_created.set()
        await release_claim.wait()

    async def observe_disable(*args: Any, **kwargs: Any) -> int:
        """确认撤权扫描已进入独立会话并随后等待 Task 锁。"""
        disable_pid_ready.set_result(await _postgres_backend_pid(args[0]))
        disable_entered.set()
        return await original_invalidate(*args, **kwargs)

    monkeypatch.setattr(
        SqlAlchemyTrustedActionRepository,
        "create_tool_claim",
        pause_claim,
    )
    monkeypatch.setattr(
        connections_repository,
        "invalidate_unclaimed_actions_for_connection",
        observe_disable,
    )
    disable_sessions = build_session_factory(database_url)
    disable_use_case = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(disable_sessions),
        AeadCipher(b"r" * 32),
        {},
        _FixedClock(),
    )
    adapter = _ClaimAdapter()
    workflow = _claim_workflow(database_url, adapter)
    claim_task = asyncio.create_task(
        workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash="a" * 64,
            lease_owner="claim-owner",
        )
    )
    disable_task: asyncio.Task[object] | None = None
    try:
        await asyncio.wait_for(claim_created.wait(), timeout=2)
        disable_task = asyncio.create_task(
            disable_use_case.disable_capability(
                user_id=seed.user_id,
                connection_id=seed.connection_id,
                capability=ConnectionCapability.MAIL_SEND,
            )
        )
        await asyncio.wait_for(disable_entered.wait(), timeout=2)
        claim_pid = await asyncio.wait_for(asyncio.shield(claim_pid_ready), timeout=2)
        disable_pid = await asyncio.wait_for(asyncio.shield(disable_pid_ready), timeout=2)
        blockers = await _wait_for_postgres_blockers(disable_sessions, waiting_pid=disable_pid)
        assert claim_pid in blockers
        release_claim.set()
        await claim_task
        await disable_task
    finally:
        release_claim.set()
        if not claim_task.done():
            claim_task.cancel()
            await asyncio.gather(claim_task, return_exceptions=True)
        if disable_task is not None and not disable_task.done():
            disable_task.cancel()
            await asyncio.gather(disable_task, return_exceptions=True)
        await workflow.dispose()
        await disable_sessions.dispose()

    sessions = build_session_factory(database_url)
    try:
        async with sessions() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
    finally:
        await sessions.dispose()
    assert execution is not None and execution.status == ToolExecutionStatus.RECONCILING.value
    assert task is not None and task.status == TaskStatus.RECONCILING.value
    assert draft is not None and draft.status == MailDraftStatus.NEEDS_ATTENTION.value
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_disable_claimed_before_request_start_preserves_reconciliation_without_provider(
    database_url: str,
) -> None:
    """未提交 request-start 的既有 claim 仍保留审批并转入只读核对，不调用 provider。"""
    seed = await _seed_mail_action(
        database_url,
        approval_status=ApprovalStatus.APPROVED,
        task_status=TaskStatus.RUNNING,
    )
    adapter = _ClaimAdapter()
    workflow = _claim_workflow(database_url, adapter)
    sessions = build_session_factory(database_url)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash="a" * 64,
            lease_owner="claim-owner",
        )
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            AeadCipher(b"r" * 32),
            {},
            _FixedClock(),
        )
        await use_case.disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )

        async with sessions() as session:
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            audits = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type)
                    .where(AuditEventModel.task_id == seed.task_id)
                    .order_by(AuditEventModel.id)
                )
            )
            outbox_topics = tuple(
                await session.scalars(
                    select(OutboxEventModel.topic)
                    .where(OutboxEventModel.aggregate_id == seed.task_id)
                    .order_by(OutboxEventModel.id)
                )
            )
    finally:
        await workflow.dispose()
        await sessions.dispose()

    assert approval is not None and approval.status == ApprovalStatus.APPROVED.value
    assert task is not None and task.status == TaskStatus.RECONCILING.value
    assert draft is not None and draft.status == MailDraftStatus.NEEDS_ATTENTION.value
    assert execution is not None
    assert execution.status == ToolExecutionStatus.RECONCILING.value
    assert execution.request_started_at is None
    assert execution.write_attempt_count == 0
    assert audits.count("approval.invalidated") == 0
    assert audits.count("task.failed") == 0
    assert audits.count("tool.reconciling") == 1
    assert outbox_topics.count("approval.invalidated") == 0
    assert outbox_topics.count("tool.reconciling") == 1
    assert outbox_topics.count("task.execute") == 1
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_pre_request_reconciliation_converges_without_provider_and_preserves_approval(
    database_url: str,
) -> None:
    """撤权后的零请求 claim 必须安全收敛而不能留下永远无法认领的核对任务。

    该回归保护 ``request_started_at IS NULL`` 分支：审批及冻结命令身份仍保持原样，
    但已由撤权屏障转入 ``reconciling`` 的动作可以在专用核对入口内确认未应用，且不
    触达任何 provider。重复投递必须观察终态并成为 no-op。
    """
    seed = await _seed_mail_action(
        database_url,
        approval_status=ApprovalStatus.APPROVED,
        task_status=TaskStatus.RUNNING,
    )
    adapter = _ClaimAdapter()
    workflow = _claim_workflow(database_url, adapter)
    sessions = build_session_factory(database_url)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash="a" * 64,
            lease_owner="claim-owner",
        )
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            AeadCipher(b"r" * 32),
            {},
            _FixedClock(),
        )
        await use_case.disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )

        async with sessions.begin() as session:
            repository = SqlAlchemyTrustedActionRepository(
                session,
                ActionPayloadCipher.from_key(b"x" * 32),
            )
            assert (
                await repository.converge_pre_request_reconciliation(
                    task_id=seed.task_id,
                )
                is True
            )
            # 终态后重复队列投递不能追加第二组审计或改变冻结审批。
            assert (
                await repository.converge_pre_request_reconciliation(
                    task_id=seed.task_id,
                )
                is False
            )

        async with sessions() as session:
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            audits = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type)
                    .where(AuditEventModel.task_id == seed.task_id)
                    .order_by(AuditEventModel.id)
                )
            )
            outbox_topics = tuple(
                await session.scalars(
                    select(OutboxEventModel.topic)
                    .where(OutboxEventModel.aggregate_id == seed.task_id)
                    .order_by(OutboxEventModel.id)
                )
            )
    finally:
        await workflow.dispose()
        await sessions.dispose()

    assert approval is not None and approval.status == ApprovalStatus.APPROVED.value
    assert task is not None and task.status == TaskStatus.FAILED.value
    assert task.error_code == "connection_capability_disabled"
    assert task.lease_owner is None and task.lease_expires_at is None
    assert task.scheduled_for is None and task.finished_at is not None
    assert draft is not None and draft.status == MailDraftStatus.EDITING.value
    assert execution is not None
    assert execution.status == ToolExecutionStatus.CONFIRMED_FAILED.value
    assert execution.request_started_at is None
    assert execution.write_attempt_count == 0
    assert execution.result_summary == {
        "kind": "confirmed_not_applied",
        "retryable": False,
    }
    assert audits.count("approval.invalidated") == 0
    assert audits.count("task.failed") == 1
    assert audits.count("tool.confirmed_failed") == 1
    assert outbox_topics.count("tool.confirmed_failed") == 1
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_disable_started_execution_preserves_immutable_command_and_only_reconciles(
    database_url: str,
) -> None:
    """request-start 已提交后撤权只能移交核对，不能失效审批或创建新的写入。"""
    seed = await _seed_mail_action(
        database_url,
        approval_status=ApprovalStatus.APPROVED,
        task_status=TaskStatus.RUNNING,
    )
    await _install_execution_state(
        database_url,
        seed,
        execution_status=ToolExecutionStatus.EXECUTING,
        task_status=TaskStatus.RUNNING,
        draft_status=MailDraftStatus.EXECUTING,
        approval_status=ApprovalStatus.APPROVED,
        request_started_at=NOW - timedelta(seconds=1),
        write_attempt_count=1,
        result_summary=None,
    )
    sessions = build_session_factory(database_url)
    try:
        async with sessions() as session:
            before = await session.get(ToolExecutionModel, seed.operation_id)
            approval_before = await session.get(ApprovalRequestModel, seed.approval_id)
            assert before is not None and approval_before is not None
            frozen_fields = (
                before.id,
                before.operation_id,
                before.request_payload_hash,
                before.provider,
                before.idempotency_key,
                approval_before.payload_hash,
            )
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            AeadCipher(b"r" * 32),
            {},
            _FixedClock(),
        )
        await use_case.disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )
        async with sessions() as session:
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            execution = await session.get(ToolExecutionModel, seed.operation_id)
            audits = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type)
                    .where(AuditEventModel.task_id == seed.task_id)
                    .order_by(AuditEventModel.id)
                )
            )
            outbox_topics = tuple(
                await session.scalars(
                    select(OutboxEventModel.topic)
                    .where(OutboxEventModel.aggregate_id == seed.task_id)
                    .order_by(OutboxEventModel.id)
                )
            )
    finally:
        await sessions.dispose()

    assert approval is not None and approval.status == ApprovalStatus.APPROVED.value
    assert task is not None and task.status == TaskStatus.RECONCILING.value
    assert draft is not None and draft.status == MailDraftStatus.NEEDS_ATTENTION.value
    assert execution is not None and execution.status == ToolExecutionStatus.RECONCILING.value
    assert execution.request_started_at == NOW - timedelta(seconds=1)
    assert execution.write_attempt_count == 1
    assert (
        execution.id,
        execution.operation_id,
        execution.request_payload_hash,
        execution.provider,
        execution.idempotency_key,
        approval.payload_hash,
    ) == frozen_fields
    assert audits.count("tool.reconciling") == 1
    assert audits.count("approval.invalidated") == 0
    assert audits.count("task.cancelled") == 0
    assert outbox_topics.count("tool.reconciling") == 1
    assert outbox_topics.count("task.execute") == 1


@pytest.mark.asyncio
@respx.mock
async def test_disconnect_commits_and_invalidates_before_google_revoke_failure(
    database_url: str,
) -> None:
    """Google revoke 返回 5xx 时，本地凭据与未认领动作先提交且只留下无内容审计。"""
    seed = await _seed_mail_action(database_url)
    cipher = AeadCipher(b"r" * 32)
    sessions = build_session_factory(database_url)
    refresh_token = "synthetic-refresh-token-for-revoke"
    access_token = "synthetic-access-token-for-revoke"
    try:
        # 先写入真实加密凭据；断开实现必须在同一连接归属下读取并删除，测试只保存
        # 密文快照用于证明提交后的数据库不再含有可恢复材料。
        async with sessions.begin() as session:
            refresh_encrypted = cipher.encrypt(
                refresh_token.encode("utf-8"),
                f"{seed.user_id}:{seed.connection_id}:refresh_token".encode("ascii"),
            )
            access_encrypted = cipher.encrypt(
                access_token.encode("utf-8"),
                f"{seed.user_id}:{seed.connection_id}:access_token".encode("ascii"),
            )
            session.add_all(
                (
                    EncryptedCredentialModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        credential_kind="refresh_token",
                        ciphertext=refresh_encrypted.ciphertext,
                        nonce=refresh_encrypted.nonce,
                        key_version=refresh_encrypted.key_version,
                        token_expires_at=NOW + timedelta(hours=1),
                    ),
                    EncryptedCredentialModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        credential_kind="access_token",
                        ciphertext=access_encrypted.ciphertext,
                        nonce=access_encrypted.nonce,
                        key_version=access_encrypted.key_version,
                        token_expires_at=NOW + timedelta(hours=1),
                    ),
                )
            )

        provider_entered = asyncio.Event()
        release_provider = asyncio.Event()

        async def return_failure(request: httpx.Request) -> httpx.Response:
            """在独立数据库会话观察提交前暂停 provider 响应。"""
            del request
            provider_entered.set()
            await release_provider.wait()
            return httpx.Response(
                500,
                json={"error": "synthetic-provider-revoke-failure"},
            )

        route = respx.post(GOOGLE_REVOKE_URL).mock(side_effect=return_failure)
        adapter = GoogleOAuthAdapter(
            client_id="synthetic-client",
            client_secret="synthetic-client-secret",
            redirect_uri="https://app.example.test/oauth/callback",
        )
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            cipher,
            {"google": adapter},
            _FixedClock(),
        )
        disconnect_task = asyncio.create_task(
            use_case.disconnect(
                user_id=seed.user_id,
                connection_id=seed.connection_id,
            )
        )
        try:
            # provider callback 尚未放行；若本地事务没有先提交，下面独立 session 不会看到
            # 断开事实，从而把“网络调用先于本地提交”的回归稳定暴露出来。
            await asyncio.wait_for(provider_entered.wait(), timeout=5)
            async with sessions() as session:
                connection = await session.get(OAuthConnectionModel, seed.connection_id)
                credentials = tuple(
                    await session.scalars(
                        select(EncryptedCredentialModel).where(
                            EncryptedCredentialModel.connection_id == seed.connection_id
                        )
                    )
                )
                capabilities = tuple(
                    await session.scalars(
                        select(ConnectionCapabilityModel.status).where(
                            ConnectionCapabilityModel.connection_id == seed.connection_id
                        )
                    )
                )
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                task = await session.get(TaskRunModel, seed.task_id)
                draft = await session.get(MailDraftModel, seed.draft_id)
            assert connection is not None and connection.status == "disconnected"
            assert credentials == ()
            assert set(capabilities) == {CapabilityStatus.REVOKED.value}
            assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
            assert task is not None and task.status == TaskStatus.CANCELLED.value
            assert draft is not None and draft.status == MailDraftStatus.EDITING.value

            release_provider.set()
            with pytest.raises(TransientProviderError) as raised:
                await disconnect_task
        finally:
            release_provider.set()
            if not disconnect_task.done():
                disconnect_task.cancel()
                await asyncio.gather(disconnect_task, return_exceptions=True)

        assert route.call_count == 1
        assert raised.value.error_code == "google_oauth_unavailable"
        assert refresh_token not in str(raised.value)
        assert access_token not in str(raised.value)

        async with sessions() as session:
            unresolved = tuple(
                await session.scalars(
                    select(AuditEventModel)
                    .where(
                        AuditEventModel.event_type == "oauth.revoke_unresolved",
                        AuditEventModel.user_id == seed.user_id,
                    )
                    .order_by(AuditEventModel.id)
                )
            )
            outbox = tuple(
                await session.scalars(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == seed.task_id)
                )
            )
            credentials_after = tuple(
                await session.scalars(
                    select(EncryptedCredentialModel).where(
                        EncryptedCredentialModel.connection_id == seed.connection_id
                    )
                )
            )
        assert len(unresolved) == 1
        event = unresolved[0]
        assert event.task_id is None
        assert event.created_at.tzinfo is not None
        assert event.event_metadata == {
            "provider": "google",
            "error_code": "google_oauth_unavailable",
            "connection_id": str(seed.connection_id),
        }
        serialized_audit = json.dumps(event.event_metadata, sort_keys=True)
        assert refresh_token not in serialized_audit
        assert access_token not in serialized_audit
        assert credentials_after == ()
        assert {item.topic for item in outbox} == {
            "approval.invalidated",
            "task.cancelled",
        }
        assert all(
            refresh_token not in json.dumps(item.payload, sort_keys=True)
            and access_token not in json.dumps(item.payload, sort_keys=True)
            for item in outbox
        )
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@respx.mock
async def test_disconnect_records_unresolved_when_microsoft_revoke_is_unsupported(
    database_url: str,
) -> None:
    """Microsoft 没有窄撤销端点时也只记录无 token 的维护事实，不创建重试工作。"""
    seed = await _seed_mail_action(database_url)
    cipher = AeadCipher(b"r" * 32)
    sessions = build_session_factory(database_url)
    refresh_token = "synthetic-microsoft-refresh-token"
    try:
        async with sessions.begin() as session:
            # 复用相同的未认领邮件动作，只把其连接身份切换为满足数据库检查约束的
            # 合成 Microsoft work/school 账户；不引入第二套动作 seed 逻辑。
            connection = await session.get(OAuthConnectionModel, seed.connection_id)
            assert connection is not None
            connection.provider = "microsoft"
            connection.provider_tenant_id = "synthetic-tenant"
            connection.account_type = "work_school"
            connection.provider_account_id = "synthetic-tenant:synthetic-account"
            encrypted = cipher.encrypt(
                refresh_token.encode("utf-8"),
                f"{seed.user_id}:{seed.connection_id}:refresh_token".encode("ascii"),
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    credential_kind="refresh_token",
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=NOW + timedelta(hours=1),
                )
            )

        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            cipher,
            {
                "microsoft": MicrosoftOAuthAdapter(
                    client_id="synthetic-client",
                    client_secret="synthetic-client-secret",
                    redirect_uri="https://app.example.test/oauth/callback",
                )
            },
            _FixedClock(),
        )
        result = await use_case.disconnect(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
        )
        assert result.status is OAuthRevocationStatus.UNSUPPORTED
        assert result.error_code == "microsoft_token_revoke_unsupported"
        assert not respx.calls

        async with sessions() as session:
            connection = await session.get(OAuthConnectionModel, seed.connection_id)
            credentials = tuple(
                await session.scalars(
                    select(EncryptedCredentialModel).where(
                        EncryptedCredentialModel.connection_id == seed.connection_id
                    )
                )
            )
            capabilities = tuple(
                await session.scalars(
                    select(ConnectionCapabilityModel.status).where(
                        ConnectionCapabilityModel.connection_id == seed.connection_id
                    )
                )
            )
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            unresolved = tuple(
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.event_type == "oauth.revoke_unresolved",
                        AuditEventModel.user_id == seed.user_id,
                    )
                )
            )
            outbox = tuple(
                await session.scalars(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == seed.task_id)
                )
            )
        assert connection is not None and connection.status == "disconnected"
        assert credentials == ()
        assert set(capabilities) == {CapabilityStatus.REVOKED.value}
        assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
        assert task is not None and task.status == TaskStatus.CANCELLED.value
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert len(unresolved) == 1
        event = unresolved[0]
        assert event.task_id is None
        assert event.event_metadata == {
            "provider": "microsoft",
            "error_code": "microsoft_token_revoke_unsupported",
            "connection_id": str(seed.connection_id),
        }
        assert refresh_token not in json.dumps(event.event_metadata, sort_keys=True)
        assert {item.topic for item in outbox} == {
            "approval.invalidated",
            "task.cancelled",
        }
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_disconnect_claimed_execution_never_fabricates_not_applied_after_token_delete(
    database_url: str,
) -> None:
    """已认领动作断开后保留冻结命令并进入只读核对，不因删 token 伪造未执行。"""
    seed = await _seed_mail_action(
        database_url,
        approval_status=ApprovalStatus.APPROVED,
        task_status=TaskStatus.RUNNING,
    )
    await _install_execution_state(
        database_url,
        seed,
        execution_status=ToolExecutionStatus.EXECUTING,
        task_status=TaskStatus.RUNNING,
        draft_status=MailDraftStatus.EXECUTING,
        approval_status=ApprovalStatus.APPROVED,
        request_started_at=NOW - timedelta(seconds=1),
        write_attempt_count=1,
        result_summary=None,
    )
    cipher = AeadCipher(b"r" * 32)
    sessions = build_session_factory(database_url)
    refresh_token = "synthetic-claimed-refresh"
    access_token = "synthetic-claimed-access"
    adapter = _DisconnectRevokeAdapter()
    try:
        async with sessions.begin() as session:
            refresh = cipher.encrypt(
                refresh_token.encode("utf-8"),
                f"{seed.user_id}:{seed.connection_id}:refresh_token".encode("ascii"),
            )
            access = cipher.encrypt(
                access_token.encode("utf-8"),
                f"{seed.user_id}:{seed.connection_id}:access_token".encode("ascii"),
            )
            session.add_all(
                (
                    EncryptedCredentialModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        credential_kind="refresh_token",
                        ciphertext=refresh.ciphertext,
                        nonce=refresh.nonce,
                        key_version=refresh.key_version,
                        token_expires_at=NOW + timedelta(hours=1),
                    ),
                    EncryptedCredentialModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        credential_kind="access_token",
                        ciphertext=access.ciphertext,
                        nonce=access.nonce,
                        key_version=access.key_version,
                        token_expires_at=NOW + timedelta(hours=1),
                    ),
                )
            )
            execution_before = await session.get(ToolExecutionModel, seed.operation_id)
            approval_before = await session.get(ApprovalRequestModel, seed.approval_id)
            assert execution_before is not None and approval_before is not None
            immutable_execution = (
                execution_before.id,
                execution_before.operation_id,
                execution_before.request_payload_hash,
                execution_before.provider,
                execution_before.idempotency_key,
                execution_before.request_started_at,
                execution_before.write_attempt_count,
            )
            immutable_approval = (
                approval_before.id,
                approval_before.payload_hash,
                approval_before.payload_ciphertext,
                approval_before.payload_nonce,
                approval_before.payload_key_version,
            )

        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            cipher,
            {"google": adapter},
            _FixedClock(),
        )
        await use_case.disconnect(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
        )

        async with sessions() as session:
            execution = await session.get(ToolExecutionModel, seed.operation_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            credentials = tuple(
                await session.scalars(
                    select(EncryptedCredentialModel).where(
                        EncryptedCredentialModel.connection_id == seed.connection_id
                    )
                )
            )
            outbox_topics = tuple(
                await session.scalars(
                    select(OutboxEventModel.topic).where(
                        OutboxEventModel.aggregate_id == seed.task_id
                    )
                )
            )
        assert execution is not None and approval is not None
        assert task is not None and draft is not None
        assert execution.status in {
            ToolExecutionStatus.RECONCILING.value,
            ToolExecutionStatus.NEEDS_ATTENTION.value,
        }
        assert task.status in {TaskStatus.RECONCILING.value, TaskStatus.NEEDS_ATTENTION.value}
        assert draft.status == MailDraftStatus.NEEDS_ATTENTION.value
        assert execution.error_code == "connection_scope_missing"
        assert task.error_code == "connection_scope_missing"
        assert approval.status == ApprovalStatus.APPROVED.value
        assert (
            execution.id,
            execution.operation_id,
            execution.request_payload_hash,
            execution.provider,
            execution.idempotency_key,
            execution.request_started_at,
            execution.write_attempt_count,
        ) == immutable_execution
        assert (
            approval.id,
            approval.payload_hash,
            approval.payload_ciphertext,
            approval.payload_nonce,
            approval.payload_key_version,
        ) == immutable_approval
        assert credentials == ()
        assert "approval.invalidated" not in outbox_topics
        assert "task.cancelled" not in outbox_topics
        assert adapter.revoked_tokens == [refresh_token]
        assert access_token not in json.dumps(outbox_topics)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "execution_status",
        "task_status",
        "draft_status",
        "request_started_at",
        "write_attempt_count",
        "result_summary",
    ),
    (
        (
            ToolExecutionStatus.CLAIMED,
            TaskStatus.RUNNING,
            MailDraftStatus.EXECUTING,
            None,
            0,
            None,
        ),
        (
            ToolExecutionStatus.RECONCILING,
            TaskStatus.RECONCILING,
            MailDraftStatus.NEEDS_ATTENTION,
            NOW - timedelta(seconds=1),
            1,
            {"outcome": "unknown"},
        ),
    ),
    ids=("claimed-before-request-start", "unknown-result"),
)
async def test_disconnect_claimed_or_unknown_never_fabricates_not_applied(
    database_url: str,
    execution_status: ToolExecutionStatus,
    task_status: TaskStatus,
    draft_status: MailDraftStatus,
    request_started_at: datetime | None,
    write_attempt_count: int,
    result_summary: dict[str, Any] | None,
) -> None:
    """删凭据不能把未决写入改写为 confirmed-not-applied 或取消。"""
    seed = await _seed_mail_action(
        database_url,
        approval_status=ApprovalStatus.APPROVED,
        task_status=task_status,
    )
    await _install_execution_state(
        database_url,
        seed,
        execution_status=execution_status,
        task_status=task_status,
        draft_status=draft_status,
        approval_status=ApprovalStatus.APPROVED,
        request_started_at=request_started_at,
        write_attempt_count=write_attempt_count,
        result_summary=result_summary,
    )
    cipher = AeadCipher(b"r" * 32)
    sessions = build_session_factory(database_url)
    refresh_token = "synthetic-claimed-or-unknown-refresh"
    adapter = _DisconnectRevokeAdapter()
    try:
        async with sessions.begin() as session:
            encrypted = cipher.encrypt(
                refresh_token.encode("utf-8"),
                f"{seed.user_id}:{seed.connection_id}:refresh_token".encode("ascii"),
            )
            session.add(
                EncryptedCredentialModel(
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    credential_kind="refresh_token",
                    ciphertext=encrypted.ciphertext,
                    nonce=encrypted.nonce,
                    key_version=encrypted.key_version,
                    token_expires_at=NOW + timedelta(hours=1),
                )
            )
            execution_before = await session.get(ToolExecutionModel, seed.operation_id)
            approval_before = await session.get(ApprovalRequestModel, seed.approval_id)
            assert execution_before is not None and approval_before is not None
            frozen_execution = (
                execution_before.id,
                execution_before.operation_id,
                execution_before.request_payload_hash,
                execution_before.provider,
                execution_before.idempotency_key,
                execution_before.request_started_at,
                execution_before.write_attempt_count,
                execution_before.result_summary,
            )
            frozen_approval = (
                approval_before.id,
                approval_before.payload_hash,
                approval_before.payload_ciphertext,
                approval_before.payload_nonce,
                approval_before.payload_key_version,
            )

        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions),
            cipher,
            {"google": adapter},
            _FixedClock(),
        )
        await use_case.disconnect(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
        )

        async with sessions() as session:
            execution = await session.get(ToolExecutionModel, seed.operation_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            credentials = tuple(
                await session.scalars(
                    select(EncryptedCredentialModel).where(
                        EncryptedCredentialModel.connection_id == seed.connection_id
                    )
                )
            )
            outbox_topics = tuple(
                await session.scalars(
                    select(OutboxEventModel.topic).where(
                        OutboxEventModel.aggregate_id == seed.task_id
                    )
                )
            )
        assert execution is not None and approval is not None
        assert task is not None and draft is not None
        assert execution.status in {
            ToolExecutionStatus.RECONCILING.value,
            ToolExecutionStatus.NEEDS_ATTENTION.value,
        }
        assert task.status in {TaskStatus.RECONCILING.value, TaskStatus.NEEDS_ATTENTION.value}
        assert draft.status == MailDraftStatus.NEEDS_ATTENTION.value
        assert execution.error_code == "connection_scope_missing"
        assert task.error_code == "connection_scope_missing"
        assert approval.status == ApprovalStatus.APPROVED.value
        assert (
            execution.id,
            execution.operation_id,
            execution.request_payload_hash,
            execution.provider,
            execution.idempotency_key,
            execution.request_started_at,
            execution.write_attempt_count,
            execution.result_summary,
        ) == frozen_execution
        assert (
            approval.id,
            approval.payload_hash,
            approval.payload_ciphertext,
            approval.payload_nonce,
            approval.payload_key_version,
        ) == frozen_approval
        assert credentials == ()
        assert "approval.invalidated" not in outbox_topics
        assert "task.cancelled" not in outbox_topics
        assert adapter.revoked_tokens == [refresh_token]
        assert "confirmed_not_applied" not in execution.status
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_reconciliation_worker_converges_revoked_zero_request_without_secrets(
    database_url: str, tmp_path: Path
) -> None:
    """真正的只读 Worker 必须在 Secret/适配器需求前消费撤权后的零请求事实。"""
    seed = await _seed_mail_action(
        database_url, approval_status=ApprovalStatus.APPROVED, task_status=TaskStatus.RUNNING
    )
    await _install_execution_state(
        database_url,
        seed,
        execution_status=ToolExecutionStatus.CLAIMED,
        task_status=TaskStatus.RUNNING,
        draft_status=MailDraftStatus.EXECUTING,
        approval_status=ApprovalStatus.APPROVED,
        request_started_at=None,
        write_attempt_count=0,
        result_summary=None,
    )
    sessions = build_session_factory(database_url)
    try:
        await ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"r" * 32), {}, _FixedClock()
        ).disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )
        settings = Settings(_env_file=None, app_master_key_file=str(tmp_path / "absent-secret"))
        assert (
            await execute_reconciliation_task(
                task_id=seed.task_id,
                session_factory=sessions,
                settings=settings,
                now=NOW,
            )
            is True
        )
        assert (
            await execute_reconciliation_task(
                task_id=seed.task_id,
                session_factory=sessions,
                settings=settings,
                now=NOW,
            )
            is False
        )
        async with sessions() as session:
            execution = await session.get(ToolExecutionModel, seed.operation_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            task = await session.get(TaskRunModel, seed.task_id)
        assert execution is not None and execution.status == "confirmed_failed"
        assert execution.request_started_at is None and execution.write_attempt_count == 0
        assert execution.result_summary == {"kind": "confirmed_not_applied", "retryable": False}
        assert approval is not None and approval.status == "approved"
        assert task is not None and task.status == "failed"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_reconciliation_worker_without_access_after_disconnect_requires_attention(
    database_url: str, tmp_path: Path
) -> None:
    """已有 request-start 的动作删凭据后不能循环调度，更不能伪造未应用。"""
    from tests.integration.m2.test_tool_execution_claim import _Seed, _seed_action

    seed = _Seed()
    await _seed_action(
        database_url,
        seed,
        existing_execution_status=ToolExecutionStatus.RECONCILING,
        request_started_at=NOW,
        task_status=TaskStatus.RECONCILING,
    )
    key_path = tmp_path / "synthetic-master-key"
    key_path.write_text(base64.b64encode(b"x" * 32).decode("ascii"))
    sessions = build_session_factory(database_url)
    try:
        await ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"r" * 32), {}, _FixedClock()
        ).disconnect(user_id=seed.user_id, connection_id=seed.connection_id)
        assert (
            await execute_reconciliation_task(
                task_id=seed.task_id,
                session_factory=sessions,
                settings=Settings(_env_file=None, app_master_key_file=str(key_path)),
                adapters=ProviderAdapterRegistry(),
                now=NOW,
            )
            is True
        )
        async with sessions() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            task = await session.get(TaskRunModel, seed.task_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            credentials = tuple(
                await session.scalars(
                    select(EncryptedCredentialModel).where(
                        EncryptedCredentialModel.connection_id == seed.connection_id
                    )
                )
            )
            audits = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type).where(
                        AuditEventModel.task_id == seed.task_id
                    )
                )
            )
        assert execution is not None and execution.status == "needs_attention"
        assert execution.request_started_at == NOW and execution.write_attempt_count == 1
        assert execution.error_code == "connection_scope_missing"
        assert task is not None and task.status == "needs_attention"
        assert task.error_code == "connection_scope_missing" and task.scheduled_for is None
        assert task.lease_owner is None and task.lease_expires_at is None
        assert approval is not None and approval.status == "approved"
        assert draft is not None and draft.status == "needs_attention"
        assert credentials == ()
        assert "tool.needs_attention" in audits
        assert "task.cancelled" not in audits and "tool.confirmed_failed" not in audits
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("global_enabled", "google_enabled", "microsoft_enabled", "expected"),
    [
        (False, True, True, ("cancelled", "cancelled")),
        (True, False, True, ("cancelled", "queued")),
        (True, True, False, ("waiting_approval", "cancelled")),
        (True, True, True, ("waiting_approval", "queued")),
    ],
)
async def test_scheduler_invalidates_only_unclaimed_actions_for_disabled_switches(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    global_enabled: bool,
    google_enabled: bool,
    microsoft_enabled: bool,
    expected: tuple[str, str],
) -> None:
    """分钟入口必须按精确 provider 关闭未认领审批，保留已认领历史且不依赖 Secret。"""
    from ai_employee.workers import schedules

    google = await _seed_mail_action(database_url)
    microsoft = await _seed_mail_action(
        database_url, approval_status=ApprovalStatus.APPROVED, task_status=TaskStatus.QUEUED
    )
    claimed = await _seed_mail_action(
        database_url, approval_status=ApprovalStatus.APPROVED, task_status=TaskStatus.RUNNING
    )
    await _install_execution_state(
        database_url,
        claimed,
        execution_status=ToolExecutionStatus.EXECUTING,
        task_status=TaskStatus.RUNNING,
        draft_status=MailDraftStatus.EXECUTING,
        approval_status=ApprovalStatus.APPROVED,
        request_started_at=NOW,
        write_attempt_count=1,
        result_summary=None,
    )
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            connection = await session.get(OAuthConnectionModel, microsoft.connection_id)
            assert connection is not None
            connection.provider = "microsoft"
            connection.provider_tenant_id = "synthetic-tenant"
            connection.account_type = "work_school"
            connection.provider_account_id = "synthetic-ms-account"
        monkeypatch.setattr(schedules, "session_factory", sessions)
        monkeypatch.setattr(
            schedules,
            "settings",
            Settings(
                _env_file=None,
                external_writes_enabled=global_enabled,
                google_writes_enabled=google_enabled,
                microsoft_writes_enabled=microsoft_enabled,
                write_test_account_allowlist=[
                    canonical_provider_identity_key("google", "", "synthetic-revocation-account")
                ],
                app_master_key_file="/nonexistent/synthetic-task25-key",
                outbox_relay_batch_size=1,
            ),
        )
        await schedules.expire_approvals()
        await schedules.expire_approvals()
        async with sessions() as session:
            for seed, status in zip((google, microsoft), expected, strict=True):
                task = await session.get(TaskRunModel, seed.task_id)
                approval = await session.get(ApprovalRequestModel, seed.approval_id)
                draft = await session.get(MailDraftModel, seed.draft_id)
                assert task is not None and task.status == status
                if status == "cancelled":
                    assert approval is not None and approval.status == "invalidated"
                    assert draft is not None and draft.status == "editing"
                    assert task.scheduled_for is None
                    events = tuple(
                        await session.scalars(
                            select(AuditEventModel).where(
                                AuditEventModel.task_id == seed.task_id,
                                AuditEventModel.event_type == "approval.invalidated",
                            )
                        )
                    )
                    assert len(events) == 1
                    assert events[0].event_metadata == {"reason": "external_writes_disabled"}
            untouched = await session.get(TaskRunModel, claimed.task_id)
            execution = await session.get(ToolExecutionModel, claimed.operation_id)
        assert untouched is not None and untouched.status == "running"
        assert execution is not None and execution.status == "executing"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_taskiq_route_converges_revoked_zero_request_before_registry(
    database_url: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """最外层 Taskiq 路由也必须先收敛零请求，不能提前进入需要 Token 的 registry 工厂。"""
    from ai_employee.workers import execute_task as worker

    seed = await _seed_mail_action(
        database_url, approval_status=ApprovalStatus.APPROVED, task_status=TaskStatus.RUNNING
    )
    await _install_execution_state(
        database_url,
        seed,
        execution_status=ToolExecutionStatus.CLAIMED,
        task_status=TaskStatus.RUNNING,
        draft_status=MailDraftStatus.EXECUTING,
        approval_status=ApprovalStatus.APPROVED,
        request_started_at=None,
        write_attempt_count=0,
        result_summary=None,
    )
    sessions = build_session_factory(database_url)
    try:
        await ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"r" * 32), {}, _FixedClock()
        ).disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )

        def unavailable_registry(**_: object) -> ProviderAdapterRegistry:
            """模拟需要但无法取得 Token 的进程装配；零请求收敛不可依赖它。"""
            raise AssertionError("registry must not be built for revoked zero-request action")

        def database_only_factory(url: str, **_: object) -> ManagedAsyncSessionMaker:
            """保留真实数据库事务，把本例无关的 Redis 通知留给 Outbox 集成套件。"""
            return build_session_factory(url)

        settings = Settings(
            _env_file=None,
            database_url=str(database_url),
            app_master_key_file=str(tmp_path / "absent-secret"),
        )
        monkeypatch.setattr(worker, "get_settings", lambda: settings)
        monkeypatch.setattr(worker, "build_session_factory", database_only_factory)
        monkeypatch.setattr(worker, "build_worker_trusted_action_registry", unavailable_registry)
        await worker.execute_task.original_func(str(seed.task_id))
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
        assert task is not None and task.status == "failed"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_switch_scan_filters_invalid_binding_before_batch_limit(database_url: str) -> None:
    """损坏绑定不能占满每轮批次并饿死后面的有效审批；它本身仍保持 fail-closed。"""
    from ai_employee.application.use_cases.trusted_actions import (
        InvalidateDisabledTrustedActionsUseCase,
    )
    from ai_employee.infrastructure.db.repositories.trusted_actions import (
        SqlAlchemyTrustedActionRevocationStore,
    )

    seeds = sorted(
        [await _seed_mail_action(database_url), await _seed_mail_action(database_url)],
        key=lambda seed: seed.task_id,
    )
    broken, valid = seeds
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            approval = await session.get(ApprovalRequestModel, broken.approval_id)
            assert approval is not None
            approval.proposal_version = 2
        changed = await InvalidateDisabledTrustedActionsUseCase(
            store=SqlAlchemyTrustedActionRevocationStore(sessions),
            write_policy=Settings(_env_file=None),
        ).execute(limit=1)
        assert changed == 1
        async with sessions() as session:
            broken_task = await session.get(TaskRunModel, broken.task_id)
            valid_task = await session.get(TaskRunModel, valid.task_id)
        assert broken_task is not None and broken_task.status == "waiting_approval"
        assert valid_task is not None and valid_task.status == "cancelled"
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_repeated_disable_and_disconnect_do_not_duplicate_claimed_reconciliation(
    database_url: str,
) -> None:
    """重复撤权必须幂等；再次断开可以更新原因，但不能复用冲突的 Outbox 去重键。"""
    seed = await _seed_mail_action(
        database_url, approval_status=ApprovalStatus.APPROVED, task_status=TaskStatus.RUNNING
    )
    await _install_execution_state(
        database_url,
        seed,
        execution_status=ToolExecutionStatus.EXECUTING,
        task_status=TaskStatus.RUNNING,
        draft_status=MailDraftStatus.EXECUTING,
        approval_status=ApprovalStatus.APPROVED,
        request_started_at=NOW,
        write_attempt_count=1,
        result_summary=None,
    )
    sessions = build_session_factory(database_url)
    use_case = ConnectionsUseCase(
        SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"r" * 32), {}, _FixedClock()
    )
    try:
        for _ in range(2):
            await use_case.disable_capability(
                user_id=seed.user_id,
                connection_id=seed.connection_id,
                capability=ConnectionCapability.MAIL_SEND,
            )
        await use_case.disconnect(user_id=seed.user_id, connection_id=seed.connection_id)
        async with sessions() as session:
            execution = await session.get(ToolExecutionModel, seed.operation_id)
            events = tuple(
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.task_id == seed.task_id,
                        AuditEventModel.event_type == "tool.reconciling",
                    )
                )
            )
            pending = tuple(
                await session.scalars(
                    select(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id == seed.task_id,
                        OutboxEventModel.topic == "task.execute",
                        OutboxEventModel.published_at.is_(None),
                    )
                )
            )
        assert execution is not None and execution.status == "reconciling"
        assert execution.error_code == "connection_scope_missing"
        assert execution.write_attempt_count == 1
        assert len(events) == 2 and len(pending) == 1
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_revoke_backlog_counts_and_operator_remediation_are_token_free_and_scoped(
    database_url: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """维护扫描只计数告警；操作者只能补救自己连接的精确未决事实，不能创建撤销重试。"""
    from ai_employee.application.use_cases.connections import OAuthRevokeMaintenanceUseCase
    from ai_employee.workers import schedules

    seed = await _seed_mail_action(database_url)
    other = await _seed_mail_action(database_url)
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            await SqlAlchemyConnectionStore(session).record_oauth_revoke_unresolved(
                user_id=seed.user_id,
                connection_id=seed.connection_id,
                provider="google",
                error_code="google_oauth_unavailable",
                occurred_at=NOW,
            )
        async with sessions() as session:
            event_id = await session.scalar(
                select(AuditEventModel.id).where(
                    AuditEventModel.user_id == seed.user_id,
                    AuditEventModel.event_type == "oauth.revoke_unresolved",
                )
            )
            before_tasks = await session.scalar(select(func.count()).select_from(TaskRunModel))
            before_outbox = await session.scalar(select(func.count()).select_from(OutboxEventModel))
        assert event_id is not None
        maintenance = OAuthRevokeMaintenanceUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), _FixedClock()
        )
        backlog = await maintenance.scan()
        google_count = next(item.unresolved_count for item in backlog if item.provider == "google")
        assert google_count >= 1
        assert (
            await maintenance.record_remediation(
                user_id=other.user_id,
                connection_id=seed.connection_id,
                unresolved_event_id=event_id,
            )
            is False
        )
        monkeypatch.setattr(schedules, "session_factory", sessions)
        await schedules.monitor_oauth_revoke_backlog()
        assert any(record.name == "ai_employee.oauth.revoke_backlog" for record in caplog.records)
        assert (
            await maintenance.record_remediation(
                user_id=seed.user_id, connection_id=seed.connection_id, unresolved_event_id=event_id
            )
            is True
        )
        assert (
            await maintenance.record_remediation(
                user_id=seed.user_id, connection_id=seed.connection_id, unresolved_event_id=event_id
            )
            is False
        )
        remaining = await maintenance.scan()
        assert (
            next((item.unresolved_count for item in remaining if item.provider == "google"), 0)
            == google_count - 1
        )
        async with sessions() as session:
            remediations = tuple(
                await session.scalars(
                    select(AuditEventModel).where(
                        AuditEventModel.user_id == seed.user_id,
                        AuditEventModel.event_type == "oauth.revoke_remediated",
                    )
                )
            )
            after_tasks = await session.scalar(select(func.count()).select_from(TaskRunModel))
            after_outbox = await session.scalar(select(func.count()).select_from(OutboxEventModel))
        assert len(remediations) == 1
        assert remediations[0].actor_type == "user"
        assert remediations[0].actor_id == str(seed.user_id)
        assert remediations[0].event_metadata == {
            "provider": "google",
            "connection_id": str(seed.connection_id),
            "unresolved_event_id": str(event_id),
        }
        assert (after_tasks, after_outbox) == (before_tasks, before_outbox)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_disable_calendar_write_invalidates_exact_proposal_without_default_fallback(
    database_url: str,
) -> None:
    """日历撤权也必须退回本地编辑态，保留原目标而不改选默认日历。"""
    from tests.integration.m2.test_tool_execution_claim import _Seed, _seed_calendar_action

    seed = _Seed()
    await _seed_calendar_action(database_url, seed, calendar_id="synthetic-exact-calendar")
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            user = await session.get(UserModel, seed.user_id)
            assert user is not None
            user.default_calendar_connection_id = seed.connection_id
            user.default_calendar_id = "synthetic-exact-calendar"
        await ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"r" * 32), {}, _FixedClock()
        ).disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.CALENDAR_WRITE,
        )
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            assert approval is not None
            proposal = await session.get(CalendarChangeProposalModel, approval.proposal_id)
            user = await session.get(UserModel, seed.user_id)
        assert task is not None and task.status == "cancelled"
        assert approval.status == "invalidated"
        assert proposal is not None and proposal.status == "editing"
        assert proposal.connection_id == seed.connection_id
        assert proposal.calendar_id == "synthetic-exact-calendar"
        assert user is not None and user.default_calendar_id == "synthetic-exact-calendar"
        assert user.default_calendar_connection_id == seed.connection_id
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution_status", "task_status", "draft_status"),
    [
        (ToolExecutionStatus.SUCCEEDED, TaskStatus.SUCCEEDED, MailDraftStatus.SENT),
        (ToolExecutionStatus.CONFIRMED_FAILED, TaskStatus.FAILED, MailDraftStatus.EDITING),
        (
            ToolExecutionStatus.NEEDS_ATTENTION,
            TaskStatus.NEEDS_ATTENTION,
            MailDraftStatus.NEEDS_ATTENTION,
        ),
    ],
)
async def test_disable_and_disconnect_preserve_terminal_execution_history(
    database_url: str,
    execution_status: ToolExecutionStatus,
    task_status: TaskStatus,
    draft_status: MailDraftStatus,
) -> None:
    """已确定结果或等待人工处理的历史不能因再次撤权而重写成取消/未应用。"""
    seed = await _seed_mail_action(
        database_url, approval_status=ApprovalStatus.APPROVED, task_status=task_status
    )
    summary = {
        "kind": (
            "unknown"
            if task_status is TaskStatus.NEEDS_ATTENTION
            else "confirmed_not_applied"
            if execution_status is ToolExecutionStatus.CONFIRMED_FAILED
            else "confirmed_applied"
        ),
        "retryable": False,
    }
    await _install_execution_state(
        database_url,
        seed,
        execution_status=execution_status,
        task_status=task_status,
        draft_status=draft_status,
        approval_status=ApprovalStatus.APPROVED,
        request_started_at=NOW,
        write_attempt_count=1,
        result_summary=summary,
    )
    sessions = build_session_factory(database_url)
    try:
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"r" * 32), {}, _FixedClock()
        )
        await use_case.disable_capability(
            user_id=seed.user_id,
            connection_id=seed.connection_id,
            capability=ConnectionCapability.MAIL_SEND,
        )
        await use_case.disconnect(user_id=seed.user_id, connection_id=seed.connection_id)
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            execution = await session.get(ToolExecutionModel, seed.operation_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            events = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type).where(
                        AuditEventModel.task_id == seed.task_id,
                    )
                )
            )
        assert task is not None and task.status == task_status.value
        assert execution is not None and execution.status == execution_status.value
        assert execution.result_summary == summary and execution.write_attempt_count == 1
        assert draft is not None and draft.status == draft_status.value
        assert approval is not None and approval.status == "approved"
        assert events == ("approval.requested",)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_foreign_disable_and_read_dependency_rejection_leave_action_facts_unchanged(
    database_url: str,
) -> None:
    """跨用户和读取依赖拒绝必须整体回滚此前扫描的生命周期变更。"""
    seed = await _seed_mail_action(database_url)
    sessions = build_session_factory(database_url)
    try:
        use_case = ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"r" * 32), {}, _FixedClock()
        )
        with pytest.raises(ConnectionNotFoundError):
            await use_case.disable_capability(
                user_id=UUID("00000000-0000-0000-0000-000000000102"),
                connection_id=seed.connection_id,
                capability=ConnectionCapability.MAIL_SEND,
            )
        with pytest.raises(StateConflictError) as rejected:
            await use_case.disable_capability(
                user_id=seed.user_id,
                connection_id=seed.connection_id,
                capability=ConnectionCapability.MAIL_READ,
            )
        assert rejected.value.error_code == "connection_capability_dependency_conflict"
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            events = tuple(
                await session.scalars(
                    select(AuditEventModel.event_type).where(
                        AuditEventModel.task_id == seed.task_id,
                    )
                )
            )
        assert task is not None and task.status == "waiting_approval"
        assert approval is not None and approval.status == "pending"
        assert draft is not None and draft.status == "awaiting_approval"
        assert events == ("approval.requested",)
    finally:
        await sessions.dispose()


@pytest.mark.asyncio
async def test_cached_read_only_adapter_can_reconcile_after_disconnect(
    database_url: str, tmp_path: Path
) -> None:
    """已在内存持有短期 Token 的 Worker 可完成只读核对，不能退回发送或默认账号。"""
    from tests.integration.m2.test_tool_execution_claim import (
        _RecordingAdapter,
        _Seed,
        _seed_action,
    )

    seed = _Seed()
    await _seed_action(
        database_url,
        seed,
        existing_execution_status=ToolExecutionStatus.RECONCILING,
        request_started_at=NOW,
        task_status=TaskStatus.RECONCILING,
    )
    key_path = tmp_path / "synthetic-key"
    key_path.write_text(base64.b64encode(b"x" * 32).decode("ascii"))
    adapter = _RecordingAdapter()
    sessions = build_session_factory(database_url)
    try:
        await ConnectionsUseCase(
            SqlAlchemyConnectionStoreFactory(sessions), AeadCipher(b"r" * 32), {}, _FixedClock()
        ).disconnect(user_id=seed.user_id, connection_id=seed.connection_id)
        assert (
            await execute_reconciliation_task(
                task_id=seed.task_id,
                session_factory=sessions,
                settings=Settings(_env_file=None, app_master_key_file=str(key_path)),
                adapters=ProviderAdapterRegistry(google_mail_action=adapter),
                now=NOW,
            )
            is True
        )
        async with sessions() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            connection = await session.get(OAuthConnectionModel, seed.connection_id)
        assert task is not None and task.status == "succeeded"
        assert (
            draft is not None
            and draft.status == "sent"
            and draft.connection_id == seed.connection_id
        )
        assert connection is not None and connection.status == "disconnected"
        assert adapter.reconcile_calls == 1 and adapter.write_calls == 0
    finally:
        await sessions.dispose()
