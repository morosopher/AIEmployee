"""验证可信真实动作只从 PostgreSQL checkpoint 与审批事实恢复。"""

import asyncio
import base64
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ExecutionReference,
    ProviderWriteOutcome,
)
from ai_employee.application.use_cases.approvals import ApprovalDecisionUseCase
from ai_employee.application.use_cases.mail_drafts import MailDraftUseCase
from ai_employee.application.use_cases.task_execution import (
    DurableTaskRunner,
    LeasedTask,
    TaskWaitingApproval,
)
from ai_employee.application.use_cases.trusted_actions import (
    SubmitMailDraftUseCase,
    TrustedActionExecutionUseCase,
)
from ai_employee.config import Settings
from ai_employee.domain.actions import (
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
)
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.actions import MailDraftModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
    SqlAlchemyTrustedActionRepositoryFactory,
    SqlAlchemyTrustedActionTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import (
    ManagedAsyncSessionMaker,
    build_session_factory,
)
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry
from ai_employee.workers import execute_task as execute_task_module
from ai_employee.workers.trusted_actions import (
    TrustedActionTaskStep,
    build_trusted_action_task_step,
)

NOW = datetime(2030, 1, 1, tzinfo=UTC)
SENSITIVE_MARKER = "synthetic-sensitive-resume-command"
ACTION_CIPHER = ActionPayloadCipher.from_key(b"r" * 32)

# 本模块与其他 Cycle 5 普通集成测试共用受 provenance 保护的 disposable head 数据库；
# 共享 0018 roles-absent anchor 保持零写，所有本模块 pool 在清理临时库前由 tracked fixture 释放。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把恢复测试绑定到已迁移且可安全销毁的 regular 数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖通用迁移 fixture，禁止 focused 测试写入共享 0018 anchor。"""
    del cycle5_regular_database_url
    yield


class _EnabledWritePolicy:
    """只允许固定 Google 合成账户通过提交与 claim 两次门禁。"""

    def provider_writes_enabled(self, provider: str) -> bool:
        """只为测试使用的 Google provider 开启真实写总开关。"""
        return provider == "google"

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """精确匹配规范 provider identity，不接受邮箱或裸账户回退。"""
        return provider_identity_key == "google::synthetic-account"


class _RecordingAdapter:
    """以合成成功结果记录 Graph 是否越过批准后的 claim 节点。"""

    provider = "google"

    def __init__(
        self,
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
        execute_error: Exception | None = None,
        execute_outcome: ProviderWriteOutcome | None = None,
    ) -> None:
        """初始化计数器、可选并发测试门与 request-start 后合成异常。"""
        self.write_calls = 0
        self.reconcile_calls = 0
        self.started = started
        self.release = release
        self.execute_error = execute_error
        self.execute_outcome = execute_outcome

    def validate_for_approval(self, command: object) -> ApprovalPreflightResult:
        """确认严格命令可无损表达，且审批前不访问外部系统。"""
        assert command is not None
        return ApprovalPreflightResult()

    async def execute(self, command: object) -> ProviderWriteOutcome:
        """记录一次批准后的合成写入并返回明确已应用结果。"""
        assert command is not None
        self.write_calls += 1
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.execute_error is not None:
            raise self.execute_error
        return self.execute_outcome or self._confirmed_applied()

    async def reconcile(
        self,
        command: object,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """只读核对独立计数，恢复路径绝不复用或重放 ``execute``。"""
        assert command is not None and execution.provider == self.provider
        self.reconcile_calls += 1
        return self._confirmed_applied()

    @staticmethod
    def _confirmed_applied() -> ProviderWriteOutcome:
        """返回不含正文的固定明确已应用结果。"""
        return ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id="synthetic-resource",
            provider_request_id="synthetic-request",
            correlation_id="synthetic-correlation",
            provider_url=None,
            error_code=None,
        )


class _MutableClock:
    """让审批决定、租约和五分钟 claim 截止使用同一显式 UTC 时间。"""

    def __init__(self, value: datetime) -> None:
        """保存当前测试瞬间。"""
        self.value = value

    def __call__(self) -> datetime:
        """返回当前显式测试瞬间。"""
        return self.value


async def _create_draft(
    session_factory: ManagedAsyncSessionMaker,
    *,
    user_id: UUID,
    connection_id: UUID,
) -> UUID:
    """创建合成用户、连接、读写能力与包含敏感 marker 的本地草稿。"""
    async with session_factory.begin() as session:
        session.add(
            UserModel(
                id=user_id,
                email=f"{user_id}@example.test",
                display_name="Trusted Resume",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            OAuthConnectionModel(
                id=connection_id,
                user_id=user_id,
                provider="google",
                provider_account_id="synthetic-account",
                provider_tenant_id="",
                account_type="google",
                account_email="sender@example.test",
                scopes=["mail.read", "mail.send"],
                status="connected",
            )
        )
        await session.flush()
        session.add_all(
            (
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=ConnectionCapability.MAIL_READ.value,
                    status=CapabilityStatus.ENABLED.value,
                    actual_scopes=["mail.read"],
                ),
                ConnectionCapabilityModel(
                    user_id=user_id,
                    connection_id=connection_id,
                    capability=ConnectionCapability.MAIL_SEND.value,
                    status=CapabilityStatus.ENABLED.value,
                    actual_scopes=["mail.send"],
                ),
            )
        )
    async with session_factory.begin() as session:
        repository = SqlAlchemyMailDraftRepository(session, ACTION_CIPHER)
        draft = await MailDraftUseCase(
            drafts=repository,
            connections=repository,
            clock=lambda: NOW,
        ).create_new(
            user_id=user_id,
            connection_id=connection_id,
            idempotency_key=f"resume-draft:{uuid4()}",
            to=("recipient@example.test",),
            subject="Synthetic checkpoint subject",
            body_text=SENSITIVE_MARKER,
        )
    return draft.draft_id


def _runner(
    session_factory: ManagedAsyncSessionMaker,
    *,
    workflow: TrustedActionExecutionUseCase,
    clock: _MutableClock,
    resume: str | None,
    checkpoint_database_url: str,
    task_timeout_seconds: float = 60,
    task_step_timeout_seconds: float = 30,
) -> DurableTaskRunner:
    """组合只含一个可信动作步骤的真实持久 Runner。"""
    approval_store = SqlAlchemyApprovalStore(session_factory)
    return DurableTaskRunner(
        store=SqlAlchemyTrustedActionTaskExecutionStore(session_factory),
        clock=clock,
        lease_duration=timedelta(minutes=1),
        task_timeout_seconds=task_timeout_seconds,
        task_step_timeout_seconds=task_step_timeout_seconds,
        max_transient_retries=0,
        resolve_steps=lambda _task: (
            TrustedActionTaskStep(
                workflow=workflow,
                approval_store=approval_store,
                resume=resume,
                checkpoint_database_url=checkpoint_database_url,
                max_transient_retries=0,
            ),
        ),
    )


async def _checkpoint_serialization(
    session_factory: ManagedAsyncSessionMaker,
    *,
    task_id: UUID,
) -> str:
    """读取 LangGraph 三张协议表的 JSON/二进制序列化文本用于泄漏断言。"""
    async with session_factory() as session:
        values = (
            await session.scalars(
                text(
                    """
                    SELECT checkpoint::text
                    FROM checkpoints
                    WHERE thread_id = :thread_id
                    UNION ALL
                    SELECT encode(blob, 'escape')
                    FROM checkpoint_blobs
                    WHERE thread_id = :thread_id
                    UNION ALL
                    SELECT encode(blob, 'escape')
                    FROM checkpoint_writes
                    WHERE thread_id = :thread_id
                    """
                ),
                {"thread_id": str(task_id)},
            )
        ).all()
    return "\n".join(str(value) for value in values)


@pytest.mark.asyncio
async def test_approved_resume_uses_durable_interrupt_without_checkpointing_command(
    database_url: str,
) -> None:
    """首次 invoke 持久化无内容中断，批准恢复后才创建并执行唯一 claim。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    connection_id = uuid4()
    provider_started = asyncio.Event()
    provider_release = asyncio.Event()
    adapter = _RecordingAdapter(started=provider_started, release=provider_release)
    registry = ProviderAdapterRegistry(google_mail_action=adapter)
    policy = _EnabledWritePolicy()
    clock = _MutableClock(NOW)
    transactions = SqlAlchemyTrustedActionRepositoryFactory(session_factory, ACTION_CIPHER)
    try:
        draft_id = await _create_draft(
            session_factory,
            user_id=user_id,
            connection_id=connection_id,
        )
        submission = await SubmitMailDraftUseCase(
            transactions=transactions,
            preflights=registry,
            write_policy=policy,
            command_cipher=ACTION_CIPHER,
        ).execute(
            user_id=user_id,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key=f"resume-submit:{uuid4()}",
            now=clock(),
        )
        workflow = TrustedActionExecutionUseCase(
            transactions=transactions,
            adapters=registry,
            write_policy=policy,
            clock=clock,
        )

        first_runner = _runner(
            session_factory,
            workflow=workflow,
            clock=clock,
            resume=None,
            checkpoint_database_url=database_url,
        )
        assert await first_runner.run(submission.task_id, lease_owner="initial-worker")

        async with session_factory() as session:
            paused_task = await session.get(TaskRunModel, submission.task_id)
            approval = await session.get(ApprovalRequestModel, submission.approval_id)
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == submission.task_id)
            )
        assert paused_task is not None and paused_task.status == TaskStatus.WAITING_APPROVAL.value
        assert approval is not None and approval.status == ApprovalStatus.PENDING.value
        assert execution is None
        assert adapter.write_calls == adapter.reconcile_calls == 0

        serialized = await _checkpoint_serialization(
            session_factory,
            task_id=submission.task_id,
        )
        assert serialized
        for forbidden in (
            SENSITIVE_MARKER,
            "recipient@example.test",
            "Synthetic checkpoint subject",
            "body_text",
            "subject",
            "payload_ciphertext",
            "decrypted_command",
            "lease_owner",
        ):
            assert forbidden not in serialized

        clock.value = NOW + timedelta(seconds=1)
        await ApprovalDecisionUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
            approval_id=submission.approval_id,
            user_id=user_id,
            decision="approved",
            version=approval.version,
            payload_hash=approval.payload_hash,
            now=clock(),
        )
        clock.value = NOW + timedelta(seconds=2)
        resumed_runner = _runner(
            session_factory,
            workflow=workflow,
            clock=clock,
            resume="approved",
            checkpoint_database_url=database_url,
        )
        first_delivery = asyncio.create_task(
            resumed_runner.run(submission.task_id, lease_owner="resume-worker")
        )
        await asyncio.wait_for(provider_started.wait(), timeout=1)
        try:
            assert not await resumed_runner.run(
                submission.task_id,
                lease_owner="live-duplicate-worker",
            )
            assert adapter.write_calls == 1
            assert adapter.reconcile_calls == 0
        finally:
            provider_release.set()
        await first_delivery
        # 模拟 provider 结果事务已经提交，但 Taskiq 在 ACK 前崩溃而重新投递同一消息。
        assert not await resumed_runner.run(submission.task_id, lease_owner="ack-replay-worker")

        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == submission.task_id)
            )
            claimed_audit = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.task_id == submission.task_id,
                    AuditEventModel.event_type == "tool.claimed",
                )
            )
            task_succeeded_audit = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.task_id == submission.task_id,
                    AuditEventModel.event_type == "task.succeeded",
                )
            )
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == submission.task_id)
            )
            tool_succeeded_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == submission.task_id,
                    AuditEventModel.event_type == "tool.succeeded",
                )
            )
            result_outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == submission.task_id,
                    OutboxEventModel.topic == "tool.succeeded",
                )
            )
        assert execution is not None and execution.status == ToolExecutionStatus.SUCCEEDED.value
        assert claimed_audit is not None
        assert task_succeeded_audit is not None
        assert execution_count == tool_succeeded_count == result_outbox_count == 1
        assert adapter.write_calls == 1
        assert adapter.reconcile_calls == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "interruption",
    ["exception", "cancelled", "step_timeout", "task_timeout", "unknown"],
)
async def test_request_started_interruption_stays_unresolved_and_redelivery_only_reconciles(
    database_url: str,
    interruption: str,
) -> None:
    """request-start 后异常、取消及两级超时都必须释放租约并只读恢复。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    connection_id = uuid4()
    provider_started = asyncio.Event()
    provider_release = None if interruption in {"exception", "unknown"} else asyncio.Event()
    unknown_outcome = (
        ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.UNKNOWN,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id=None,
            provider_request_id="synthetic-unknown-request",
            correlation_id="synthetic-unknown-correlation",
            provider_url=None,
            error_code="synthetic_unknown",
        )
        if interruption == "unknown"
        else None
    )
    adapter = _RecordingAdapter(
        started=provider_started,
        release=provider_release,
        execute_error=(
            RuntimeError("synthetic provider interruption") if interruption == "exception" else None
        ),
        execute_outcome=unknown_outcome,
    )
    registry = ProviderAdapterRegistry(google_mail_action=adapter)
    policy = _EnabledWritePolicy()
    clock = _MutableClock(NOW)
    transactions = SqlAlchemyTrustedActionRepositoryFactory(session_factory, ACTION_CIPHER)
    try:
        draft_id = await _create_draft(
            session_factory,
            user_id=user_id,
            connection_id=connection_id,
        )
        submission = await SubmitMailDraftUseCase(
            transactions=transactions,
            preflights=registry,
            write_policy=policy,
            command_cipher=ACTION_CIPHER,
        ).execute(
            user_id=user_id,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key=f"started-interruption:{interruption}:{uuid4()}",
            now=clock(),
        )
        workflow = TrustedActionExecutionUseCase(
            transactions=transactions,
            adapters=registry,
            write_policy=policy,
            clock=clock,
        )
        assert await _runner(
            session_factory,
            workflow=workflow,
            clock=clock,
            resume=None,
            checkpoint_database_url=database_url,
        ).run(submission.task_id, lease_owner="initial-worker")
        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, submission.approval_id)
        assert approval is not None
        clock.value = NOW + timedelta(seconds=1)
        await ApprovalDecisionUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
            approval_id=submission.approval_id,
            user_id=user_id,
            decision="approved",
            version=approval.version,
            payload_hash=approval.payload_hash,
            now=clock(),
        )
        clock.value = NOW + timedelta(seconds=2)
        task_timeout_seconds = 2.2 if interruption == "task_timeout" else 60
        task_step_timeout_seconds = 0.75 if interruption == "step_timeout" else 2.0
        interrupted_runner = _runner(
            session_factory,
            workflow=workflow,
            clock=clock,
            resume="approved",
            checkpoint_database_url=database_url,
            task_timeout_seconds=task_timeout_seconds,
            task_step_timeout_seconds=task_step_timeout_seconds,
        )
        if interruption in {"exception", "unknown"}:
            await interrupted_runner.run(
                submission.task_id,
                lease_owner=f"{interruption}-worker",
            )
        else:
            interrupted = asyncio.create_task(
                interrupted_runner.run(
                    submission.task_id,
                    lease_owner=f"{interruption}-worker",
                )
            )
            await asyncio.wait_for(provider_started.wait(), timeout=2)
            if interruption == "cancelled":
                interrupted.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await interrupted
            else:
                await interrupted

        async with session_factory() as session:
            unresolved_task = await session.get(TaskRunModel, submission.task_id)
            unresolved_draft = await session.get(MailDraftModel, draft_id)
            unresolved_execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == submission.task_id)
            )
            failed_audits = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == submission.task_id,
                    AuditEventModel.event_type == "task.failed",
                )
            )
        assert unresolved_task is not None
        expected_task_status = (
            TaskStatus.RECONCILING.value if interruption == "unknown" else TaskStatus.RUNNING.value
        )
        assert unresolved_task.status == expected_task_status
        assert unresolved_task.lease_owner is None
        assert unresolved_task.finished_at is None
        assert unresolved_draft is not None
        expected_draft_status = (
            MailDraftStatus.NEEDS_ATTENTION.value
            if interruption == "unknown"
            else MailDraftStatus.EXECUTING.value
        )
        assert unresolved_draft.status == expected_draft_status
        assert unresolved_execution is not None
        expected_execution_status = (
            ToolExecutionStatus.RECONCILING.value
            if interruption == "unknown"
            else ToolExecutionStatus.EXECUTING.value
        )
        assert unresolved_execution.status == expected_execution_status
        assert unresolved_execution.request_started_at is not None
        assert unresolved_execution.write_attempt_count == 1
        assert failed_audits == 0
        assert adapter.write_calls == 1
        assert adapter.reconcile_calls == 0

        if provider_release is not None:
            provider_release.set()
        adapter.execute_error = None
        clock.value = NOW + timedelta(seconds=3)
        if interruption == "unknown":
            # UNKNOWN 已把 TaskRun 交给专用只读队列；普通 DurableTaskRunner 不能把
            # reconciling 误升为 running。模拟一次到期 delivery 的 claim + reconcile。
            async with session_factory.begin() as session:
                task_for_reconcile = await session.get(TaskRunModel, submission.task_id)
                assert task_for_reconcile is not None
                assert task_for_reconcile.scheduled_for is not None
                scheduled_for = task_for_reconcile.scheduled_for
                repository = SqlAlchemyTrustedActionRepository(session, ACTION_CIPHER)
                reconciliation_snapshot = await repository.claim_reconciliation(
                    task_id=submission.task_id,
                    lease_owner="unknown-recovery-worker",
                    now=scheduled_for,
                    lease_expires_at=scheduled_for + timedelta(minutes=1),
                )
                assert reconciliation_snapshot is not None
            facts = await workflow.load_graph_facts(
                task_id=submission.task_id,
                approval_id=submission.approval_id,
                operation_id=submission.operation_id,
                expected_payload_hash="",
            )
            await workflow.reconcile(
                task_id=submission.task_id,
                approval_id=submission.approval_id,
                operation_id=submission.operation_id,
                expected_payload_hash=facts.payload_hash,
                lease_owner="unknown-recovery-worker",
            )
        else:
            await _runner(
                session_factory,
                workflow=workflow,
                clock=clock,
                resume="approved",
                checkpoint_database_url=database_url,
            ).run(
                submission.task_id,
                lease_owner=f"{interruption}-recovery-worker",
            )

        async with session_factory() as session:
            final_task = await session.get(TaskRunModel, submission.task_id)
            final_draft = await session.get(MailDraftModel, draft_id)
            final_execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == submission.task_id)
            )
        assert final_task is not None and final_task.status == TaskStatus.SUCCEEDED.value
        assert final_draft is not None and final_draft.status == MailDraftStatus.SENT.value
        assert final_execution is not None
        assert final_execution.status == ToolExecutionStatus.SUCCEEDED.value
        assert final_execution.write_attempt_count == 1
        assert adapter.write_calls == 1
        assert adapter.reconcile_calls == 1
    finally:
        if provider_release is not None:
            provider_release.set()
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_execute_task_routes_trusted_action_to_checkpoint_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Taskiq 入口必须把可信动作 resume 与消息级依赖交给专用 Graph step。"""
    task_id = uuid4()
    expected_task_id = task_id
    captured: dict[str, object] = {}

    class _Factory:
        """记录入口是否释放当前消息创建的数据库连接池。"""

        disposed = False

        async def dispose(self) -> None:
            """模拟异步释放 SQLAlchemy engine。"""
            self.disposed = True

    class _Store:
        """返回可信动作分类，并作为 step 复用的审批 checkpoint store。"""

        def __init__(self, received_factory: object) -> None:
            """确认分类查询与后续 step 使用同一工厂。"""
            captured["approval_store"] = self
            captured["store_factory"] = received_factory

        async def get_fake_write_task(self, *, task_id: UUID) -> object:
            """返回只含稳定 kind 的分类投影。"""
            del task_id
            return type("TrustedTask", (), {"kind": "trusted_action"})()

    class _Step:
        """代表组合层返回的可信动作 checkpoint step。"""

        name = "trusted_action_graph"

        async def execute(self, task: LeasedTask) -> None:
            """本组合测试只解析节点，不真正执行 Graph。"""
            del task

    class _Runner:
        """捕获入口构造的节点解析器，并解析一次可信任务。"""

        def __init__(self, **kwargs: object) -> None:
            """保存节点解析器供 run 使用。"""
            captured["resolve_steps"] = kwargs["resolve_steps"]
            captured["runner_store"] = kwargs["store"]

        async def run(self, received_task_id: UUID, **_: object) -> bool:
            """解析一次稳定可信任务快照。"""
            assert received_task_id == task_id
            resolver = captured["resolve_steps"]
            assert callable(resolver)
            steps = resolver(
                LeasedTask(
                    task_id=task_id,
                    kind="trusted_action",
                    input_payload={
                        "approval_id": str(uuid4()),
                        "operation_id": str(uuid4()),
                    },
                    started_at=NOW,
                    lease_owner="worker-a",
                )
            )
            captured["steps"] = tuple(steps)
            return True

    factory = _Factory()
    step = _Step()

    def _build_factory(_database_url: str, *, task_event_publisher: object) -> _Factory:
        """返回同一消息级工厂，并确认入口保留提交后通知能力。"""
        assert task_event_publisher is not None
        return factory

    def _build_step(**kwargs: object) -> _Step:
        """记录可信动作组合参数并返回合成 step。"""
        captured["step_kwargs"] = kwargs
        return step

    async def _skip_metrics(**_: object) -> None:
        """组合测试不创建真实数据库，跳过只读指标扫描。"""

    monkeypatch.setattr(execute_task_module, "build_session_factory", _build_factory)
    monkeypatch.setattr(execute_task_module, "SqlAlchemyApprovalStore", _Store)
    monkeypatch.setattr(execute_task_module, "DurableTaskRunner", _Runner)
    monkeypatch.setattr(execute_task_module, "refresh_stuck_task_metrics", _skip_metrics)
    monkeypatch.setattr(
        execute_task_module,
        "build_trusted_action_task_step",
        _build_step,
        raising=False,
    )
    monkeypatch.setattr(
        execute_task_module,
        "build_task_runner",
        lambda: pytest.fail("trusted_action must not use the generic cached runner"),
    )

    async def _authoritative_status(
        _self: object,
        *,
        task_id: UUID,
    ) -> str:
        """为路由组合测试提供明确的可执行状态，避免依赖合成数据库工厂。"""
        assert task_id == expected_task_id
        return TaskStatus.QUEUED.value

    monkeypatch.setattr(
        SqlAlchemyTrustedActionTaskExecutionStore,
        "get_authoritative_task_status",
        _authoritative_status,
    )

    await execute_task_module.execute_task.original_func(
        str(task_id),
        resume="approved",
    )

    assert captured["steps"] == (step,)
    assert isinstance(captured["runner_store"], SqlAlchemyTrustedActionTaskExecutionStore)
    kwargs = captured["step_kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["session_factory"] is factory
    assert kwargs["approval_store"] is captured["approval_store"]
    assert kwargs["resume"] == "approved"
    assert factory.disposed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("authoritative_kind", ["trusted_action", "fake_write", None])
async def test_execute_task_classification_failure_never_generic_finishes_trusted_task(
    monkeypatch: pytest.MonkeyPatch,
    authoritative_kind: str | None,
) -> None:
    """分类读异常后必须锁定权威 kind，未知时不能创建通用失败 Runner。"""
    task_id = uuid4()
    expected_task_id = task_id
    captured: dict[str, object] = {}
    runner_created = [0]

    class _Factory:
        """记录消息级连接池是否最终释放。"""

        disposed = False

        async def dispose(self) -> None:
            """模拟释放本次消息专属连接池。"""
            self.disposed = True

    class _Store:
        """让首次分类读取稳定失败，逼近数据库响应丢失后的恢复边界。"""

        def __init__(self, received_factory: object) -> None:
            """保存消息工厂，确认分类存储沿用同一资源边界。"""
            captured["approval_store"] = self
            captured["store_factory"] = received_factory

        async def get_fake_write_task(self, *, task_id: UUID) -> object:
            """模拟分类查询异常而不携带任务内容。"""
            del task_id
            raise RuntimeError("synthetic classification read failure")

    class _Step:
        """代表可信动作专属节点。"""

        name = "trusted_action_graph"

        async def execute(self, task: LeasedTask) -> None:
            """不访问数据库，只验证节点接口可被解析。"""
            del task

    class _Runner:
        """记录 fallback 是否错误地采用通用 TaskExecutionStore。"""

        def __init__(self, **kwargs: object) -> None:
            """保存 store 与节点解析器，避免真正获取合成任务租约。"""
            runner_created[0] += 1
            captured["runner_store"] = kwargs["store"]
            captured["resolve_steps"] = kwargs["resolve_steps"]

        async def run(self, received_task_id: UUID, **_: object) -> bool:
            """解析一次 trusted kind 节点；真实 lease 竞态由 PostgreSQL 套件覆盖。"""
            assert received_task_id == expected_task_id
            resolver = captured["resolve_steps"]
            assert callable(resolver)
            steps = tuple(
                resolver(
                    LeasedTask(
                        task_id=received_task_id,
                        kind=authoritative_kind or "trusted_action",
                        input_payload={},
                        started_at=NOW,
                        lease_owner="worker-a",
                    )
                )
            )
            captured["steps"] = steps
            if authoritative_kind is None:
                assert len(steps) == 1
                with pytest.raises(TaskWaitingApproval):
                    await steps[0].execute(
                        LeasedTask(
                            task_id=received_task_id,
                            kind="trusted_action",
                            input_payload={},
                            started_at=NOW,
                            lease_owner="worker-a",
                        )
                    )
            return False

    factory = _Factory()
    trusted_step = _Step()

    def _build_factory(_database_url: str, *, task_event_publisher: object) -> _Factory:
        """返回合成消息工厂并保留 Outbox publisher 参数。"""
        assert task_event_publisher is not None
        return factory

    async def _authoritative_kind(self: object, *, task_id: UUID) -> str | None:
        """返回锁内重读的合成 TaskRun.kind，或模拟第二次查询仍失败。"""
        del self
        assert task_id == expected_task_id
        if authoritative_kind is None:
            raise RuntimeError("synthetic authoritative classification failure")
        return authoritative_kind

    def _build_step(**kwargs: object) -> _Step:
        """记录可信专属 step 的组合参数。"""
        captured["step_kwargs"] = kwargs
        return trusted_step

    async def _skip_metrics(**_: object) -> None:
        """分类路由测试不启动指标扫描。"""

    monkeypatch.setattr(execute_task_module, "build_session_factory", _build_factory)
    monkeypatch.setattr(execute_task_module, "SqlAlchemyApprovalStore", _Store)
    monkeypatch.setattr(execute_task_module, "DurableTaskRunner", _Runner)
    monkeypatch.setattr(execute_task_module, "refresh_stuck_task_metrics", _skip_metrics)
    monkeypatch.setattr(
        execute_task_module,
        "build_trusted_action_task_step",
        _build_step,
        raising=False,
    )
    monkeypatch.setattr(
        SqlAlchemyTrustedActionTaskExecutionStore,
        "get_authoritative_task_kind",
        _authoritative_kind,
        raising=False,
    )

    async def _authoritative_status(
        _self: object,
        *,
        task_id: UUID,
    ) -> str:
        """为可信动作兼容路由提供明确 queued 状态；其他 kind 不会调用此端口。"""
        assert task_id == expected_task_id
        return TaskStatus.QUEUED.value

    if authoritative_kind == "trusted_action":
        monkeypatch.setattr(
            SqlAlchemyTrustedActionTaskExecutionStore,
            "get_authoritative_task_status",
            _authoritative_status,
        )
    monkeypatch.setattr(
        execute_task_module,
        "build_task_runner",
        lambda: pytest.fail("classification fallback must not generic-finish a trusted task"),
    )

    await execute_task_module.execute_task.original_func(str(task_id))

    if authoritative_kind == "trusted_action":
        assert runner_created[0] == 1
        assert isinstance(captured["runner_store"], SqlAlchemyTrustedActionTaskExecutionStore)
        assert captured["steps"] == (trusted_step,)
        kwargs = captured["step_kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs["session_factory"] is factory
    elif authoritative_kind == "fake_write":
        assert runner_created[0] == 1
        assert isinstance(captured["runner_store"], SqlAlchemyTaskExecutionStore)
        steps = captured["steps"]
        assert len(steps) == 1
        assert isinstance(steps[0], execute_task_module._FakeWriteStep)
    else:
        assert runner_created[0] == 1
        assert isinstance(
            captured["runner_store"],
            execute_task_module._ClassificationDeferredTaskExecutionStore,
        )
        assert captured["steps"]
        deferred_store = captured["runner_store"]
        assert isinstance(
            deferred_store, execute_task_module._ClassificationDeferredTaskExecutionStore
        )
        assert (
            await deferred_store.finish(
                task_id=task_id,
                lease_owner="worker-a",
                status=TaskStatus.FAILED,
                finished_at=NOW,
                error_code="internal_worker_error",
            )
            is False
        )
        assert (
            await deferred_store.fail_internal(
                task_id=task_id,
                lease_owner="worker-a",
                failed_at=NOW,
                error_code="task_execution_internal_error",
            )
            is False
        )
    assert factory.disposed is True


@pytest.mark.asyncio
async def test_execute_task_classification_failure_preserves_m1_legacy_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权威重查明确返回 M1 kind 时仍走原有通用 Runner，不误套可信动作边界。"""
    task_id = uuid4()
    expected_task_id = task_id
    captured: dict[str, object] = {}

    class _Factory:
        """记录分类异常路径是否释放消息级资源。"""

        disposed = False

        async def dispose(self) -> None:
            """模拟释放本次消息创建的连接池。"""
            self.disposed = True

    class _Store:
        """让首个分类查询失败，随后由权威锁内查询决定 M1 kind。"""

        def __init__(self, received_factory: object) -> None:
            """保留审批存储的资源参数，确保入口没有换用隐式池。"""
            captured["approval_store"] = self
            captured["store_factory"] = received_factory

        async def get_fake_write_task(self, *, task_id: UUID) -> object:
            """模拟分类查询在响应边界丢失。"""
            del task_id
            raise RuntimeError("synthetic classification read failure")

    class _GenericRunner:
        """捕获 M1 通用 Runner，避免执行真实任务节点。"""

        async def run(self, received_task_id: UUID, **_: object) -> bool:
            """确认通用 Runner 收到原始 task_id 后安全返回。"""
            assert received_task_id == task_id
            captured["ran"] = True
            return False

    factory = _Factory()
    generic_runner = _GenericRunner()

    def _build_factory(_database_url: str, *, task_event_publisher: object) -> _Factory:
        """返回合成工厂并确认保留 TaskEvent publisher。"""
        assert task_event_publisher is not None
        return factory

    async def _authoritative_kind(self: object, *, task_id: UUID) -> str:
        """返回一个明确的 M1 legacy kind。"""
        del self
        assert task_id == expected_task_id
        return "conversation.respond"

    async def _skip_metrics(**_: object) -> None:
        """路由测试不启动指标扫描。"""

    monkeypatch.setattr(execute_task_module, "build_session_factory", _build_factory)
    monkeypatch.setattr(execute_task_module, "SqlAlchemyApprovalStore", _Store)
    monkeypatch.setattr(execute_task_module, "build_task_runner", lambda: generic_runner)
    monkeypatch.setattr(execute_task_module, "refresh_stuck_task_metrics", _skip_metrics)
    monkeypatch.setattr(
        SqlAlchemyTrustedActionTaskExecutionStore,
        "get_authoritative_task_kind",
        _authoritative_kind,
        raising=False,
    )
    monkeypatch.setattr(
        execute_task_module,
        "DurableTaskRunner",
        lambda **_: pytest.fail("明确 M1 kind 不应构造专用 Runner"),
    )

    await execute_task_module.execute_task.original_func(str(task_id))

    assert captured["ran"] is True
    assert factory.disposed is True


@pytest.mark.asyncio
async def test_rejected_database_decision_overrides_approved_resume_value(
    database_url: str,
) -> None:
    """队列唤醒值不能把 PostgreSQL 中的拒绝决定提升为真实写授权。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    connection_id = uuid4()
    adapter = _RecordingAdapter()
    registry = ProviderAdapterRegistry(google_mail_action=adapter)
    policy = _EnabledWritePolicy()
    clock = _MutableClock(NOW)
    transactions = SqlAlchemyTrustedActionRepositoryFactory(session_factory, ACTION_CIPHER)
    try:
        draft_id = await _create_draft(
            session_factory,
            user_id=user_id,
            connection_id=connection_id,
        )
        submission = await SubmitMailDraftUseCase(
            transactions=transactions,
            preflights=registry,
            write_policy=policy,
            command_cipher=ACTION_CIPHER,
        ).execute(
            user_id=user_id,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key=f"resume-rejected:{uuid4()}",
            now=clock(),
        )
        workflow = TrustedActionExecutionUseCase(
            transactions=transactions,
            adapters=registry,
            write_policy=policy,
            clock=clock,
        )
        assert await _runner(
            session_factory,
            workflow=workflow,
            clock=clock,
            resume=None,
            checkpoint_database_url=database_url,
        ).run(submission.task_id, lease_owner="initial-worker")

        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, submission.approval_id)
        assert approval is not None
        clock.value = NOW + timedelta(seconds=1)
        await ApprovalDecisionUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
            approval_id=submission.approval_id,
            user_id=user_id,
            decision="rejected",
            version=approval.version,
            payload_hash=approval.payload_hash,
            now=clock(),
        )
        clock.value = NOW + timedelta(seconds=2)
        # 故意传入相反值；TrustedActionGraph.await_approval 必须忽略它并重读审批行。
        await _runner(
            session_factory,
            workflow=workflow,
            clock=clock,
            resume="approved",
            checkpoint_database_url=database_url,
        ).run(submission.task_id, lease_owner="resume-worker")
        assert not await _runner(
            session_factory,
            workflow=workflow,
            clock=clock,
            resume="rejected",
            checkpoint_database_url=database_url,
        ).run(submission.task_id, lease_owner="duplicate-worker")

        async with session_factory() as session:
            task = await session.get(TaskRunModel, submission.task_id)
            decided = await session.get(ApprovalRequestModel, submission.approval_id)
            draft = await session.get(MailDraftModel, draft_id)
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == submission.task_id)
            )
            task_succeeded_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == submission.task_id,
                    AuditEventModel.event_type == "task.succeeded",
                )
            )
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert decided is not None and decided.status == ApprovalStatus.REJECTED.value
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert execution_count == 0
        assert task_succeeded_count == 1
        assert adapter.write_calls == adapter.reconcile_calls == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_default_worker_registry_fails_before_claim_when_no_write_adapter_is_composed(
    database_url: str,
    tmp_path: object,
) -> None:
    """真实 builder 默认空 registry 时必须在 claim 前安全失败且不解密后调用供应商。"""
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    key_file = tmp_path / "app-master-key"
    key_file.write_text(base64.urlsafe_b64encode(b"r" * 32).decode("ascii"), encoding="utf-8")
    settings = Settings(
        _env_file=None,
        app_env="staging",
        database_url=database_url,
        checkpoint_database_url=database_url,
        app_master_key_file=key_file,
        external_writes_enabled=True,
        google_writes_enabled=True,
        microsoft_writes_enabled=False,
        write_test_account_allowlist=["google::synthetic-account"],
    )
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    connection_id = uuid4()
    adapter = _RecordingAdapter()
    submission_registry = ProviderAdapterRegistry(google_mail_action=adapter)
    transactions = SqlAlchemyTrustedActionRepositoryFactory(session_factory, ACTION_CIPHER)
    clock = _MutableClock(NOW)
    approval_store = SqlAlchemyApprovalStore(session_factory)

    def composed_runner(
        *,
        resume: str | None,
        adapters: ProviderAdapterRegistry | None,
    ) -> DurableTaskRunner:
        """让测试显式选择提交用 fake adapter 或生产默认空 registry。"""
        return DurableTaskRunner(
            store=SqlAlchemyTrustedActionTaskExecutionStore(session_factory),
            clock=clock,
            lease_duration=timedelta(minutes=1),
            task_timeout_seconds=60,
            task_step_timeout_seconds=30,
            max_transient_retries=0,
            resolve_steps=lambda _task: (
                build_trusted_action_task_step(
                    session_factory=session_factory,
                    settings=settings,
                    approval_store=approval_store,
                    resume=resume,
                    max_transient_retries=0,
                    adapters=adapters,
                ),
            ),
        )

    try:
        draft_id = await _create_draft(
            session_factory,
            user_id=user_id,
            connection_id=connection_id,
        )
        submission = await SubmitMailDraftUseCase(
            transactions=transactions,
            preflights=submission_registry,
            write_policy=settings,
            command_cipher=ACTION_CIPHER,
        ).execute(
            user_id=user_id,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key=f"empty-adapter:{uuid4()}",
            now=clock(),
        )
        assert await composed_runner(resume=None, adapters=submission_registry).run(
            submission.task_id,
            lease_owner="initial-worker",
        )
        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, submission.approval_id)
        assert approval is not None
        clock.value = NOW + timedelta(seconds=1)
        await ApprovalDecisionUseCase(approval_store).execute(
            approval_id=submission.approval_id,
            user_id=user_id,
            decision="approved",
            version=approval.version,
            payload_hash=approval.payload_hash,
            now=clock(),
        )
        clock.value = NOW + timedelta(seconds=2)
        await composed_runner(resume="approved", adapters=None).run(
            submission.task_id,
            lease_owner="resume-worker",
        )

        async with session_factory() as session:
            task = await session.get(TaskRunModel, submission.task_id)
            invalidated = await session.get(ApprovalRequestModel, submission.approval_id)
            draft = await session.get(MailDraftModel, draft_id)
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == submission.task_id)
            )
        assert task is not None and task.status == TaskStatus.FAILED.value
        assert task.error_code == "provider_action_unavailable"
        assert invalidated is not None and invalidated.status == ApprovalStatus.INVALIDATED.value
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert execution_count == 0
        assert adapter.write_calls == adapter.reconcile_calls == 0
    finally:
        await session_factory.dispose()
