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
from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask
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
    SqlAlchemyTrustedActionRepositoryFactory,
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
    ) -> None:
        """初始化计数器及可选的并发测试门。"""
        self.write_calls = 0
        self.reconcile_calls = 0
        self.started = started
        self.release = release

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

    async def reconcile(
        self,
        command: object,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """本切片不应进入核对；若进入仍返回内容无关的明确结果。"""
        assert command is not None and execution.provider == self.provider
        self.reconcile_calls += 1
        return await self.execute(command)


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
) -> DurableTaskRunner:
    """组合只含一个可信动作步骤的真实持久 Runner。"""
    approval_store = SqlAlchemyApprovalStore(session_factory)
    return DurableTaskRunner(
        store=SqlAlchemyTaskExecutionStore(session_factory),
        clock=clock,
        lease_duration=timedelta(minutes=1),
        task_timeout_seconds=60,
        task_step_timeout_seconds=30,
        max_transient_retries=0,
        resolve_steps=lambda _task: (
            TrustedActionTaskStep(
                workflow=workflow,
                approval_store=approval_store,
                resume=resume,
                checkpoint_database_url=checkpoint_database_url,
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
async def test_execute_task_routes_trusted_action_to_checkpoint_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Taskiq 入口必须把可信动作 resume 与消息级依赖交给专用 Graph step。"""
    task_id = uuid4()
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

    await execute_task_module.execute_task.original_func(
        str(task_id),
        resume="approved",
    )

    assert captured["steps"] == (step,)
    kwargs = captured["step_kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["session_factory"] is factory
    assert kwargs["approval_store"] is captured["approval_store"]
    assert kwargs["resume"] == "approved"
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

        async with session_factory() as session:
            task = await session.get(TaskRunModel, submission.task_id)
            decided = await session.get(ApprovalRequestModel, submission.approval_id)
            draft = await session.get(MailDraftModel, draft_id)
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == submission.task_id)
            )
        assert task is not None and task.status == TaskStatus.SUCCEEDED.value
        assert decided is not None and decided.status == ApprovalStatus.REJECTED.value
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert execution_count == 0
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
            store=SqlAlchemyTaskExecutionStore(session_factory),
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
