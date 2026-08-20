"""在真实 PostgreSQL 上验证 ToolExecution 原子认领与请求开始幂等边界。"""

import asyncio
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, time, timedelta
from time import sleep
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update

from ai_employee.application.commands import parse_trusted_command, trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ExecutionReference,
    ProviderWriteOutcome,
)
from ai_employee.application.use_cases.task_execution import DurableTaskRunner, LeasedTask
from ai_employee.application.use_cases.trusted_actions import TrustedActionExecutionUseCase
from ai_employee.config import Settings
from ai_employee.domain.actions import (
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
)
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    canonical_provider_identity_key,
)
from ai_employee.domain.errors import StateConflictError, TransientProviderError
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    MailDraftModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.task_execution import (
    SqlAlchemyTaskExecutionStore,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.infrastructure.security.encryption import AeadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry

NOW = datetime(2030, 1, 1, 0, 4, tzinfo=UTC)
ACTION_CIPHER = ActionPayloadCipher.from_key(b"x" * 32)

# 本模块只使用现有 typed lifecycle 创建的 regular disposable database；共享 0018 anchor
# 只做只读复核，任何测试异常都由 tracked fixture 先释放连接池再安全清理临时库。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """把本模块显式绑定到已迁移且受清理租约保护的 regular 数据库。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖通用迁移 fixture，禁止其把 roles-absent anchor 当普通目标升级。"""
    del cycle5_regular_database_url
    yield


class _RecordingAdapter:
    """记录真实写/只读核对次数并只返回合成规范结果。"""

    def __init__(
        self,
        outcome: ProviderWriteOutcome | None = None,
        *,
        provider: str = "google",
    ) -> None:
        """初始化固定 provider 与计数器；默认返回 confirmed applied。"""
        self.provider = provider
        self.write_calls = 0
        self.reconcile_calls = 0
        self.outcome = outcome or ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id="synthetic-resource",
            provider_request_id="synthetic-request",
            correlation_id="synthetic-correlation",
            provider_url=None,
            error_code=None,
        )

    def validate_for_approval(self, command: object) -> ApprovalPreflightResult:
        """确认测试只传入严格可信领域命令。"""
        assert command is not None
        return ApprovalPreflightResult()

    async def execute(self, command: object) -> ProviderWriteOutcome:
        """让并发调用在请求开始 CAS 后交错，再返回同一合成结果。"""
        assert command is not None
        self.write_calls += 1
        await asyncio.sleep(0)
        return self.outcome

    async def reconcile(
        self,
        command: object,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """记录只读核对且不把它误计为第二次写入。"""
        assert command is not None and execution.provider == "google"
        self.reconcile_calls += 1
        return self.outcome


class _CrashAfterRequestStartAdapter(_RecordingAdapter):
    """首次写调用确认 request-start 已独立提交后模拟进程崩溃。"""

    def __init__(self, database_url: str, task_id: object) -> None:
        """保存只读核对所需数据库与精确 TaskRun ID。"""
        super().__init__()
        self._database_url = database_url
        self._task_id = task_id
        self.fail_execute = True

    async def execute(self, command: object) -> ProviderWriteOutcome:
        """从新事务证明 request-start 可见，再模拟供应商调用后的 Worker 崩溃。"""
        assert command is not None
        self.write_calls += 1
        session_factory = build_session_factory(self._database_url)
        try:
            async with session_factory() as session:
                execution = await session.scalar(
                    select(ToolExecutionModel).where(ToolExecutionModel.task_id == self._task_id)
                )
        finally:
            await session_factory.dispose()
        assert execution is not None
        assert execution.status == ToolExecutionStatus.EXECUTING.value
        assert execution.request_started_at is not None
        assert execution.write_attempt_count == 1
        if self.fail_execute:
            raise RuntimeError("synthetic crash after committed request start")
        return self.outcome

    async def reconcile(
        self,
        command: object,
        execution: ExecutionReference,
    ) -> ProviderWriteOutcome:
        """恢复只执行内容无关的只读核对，不复用会抛错的 execute。"""
        assert command is not None and execution.provider == "google"
        self.reconcile_calls += 1
        return self.outcome


class _DelayedActionPayloadCipher(ActionPayloadCipher):
    """在认证解密前注入确定性延迟，用于跨过短 TaskRun 租约。"""

    def __init__(self, delay_seconds: float) -> None:
        """使用与合成 fixture 相同的 AEAD 密钥并保存阻塞延迟。"""
        super().__init__(AeadCipher(b"x" * 32))
        self._delay_seconds = delay_seconds

    def decrypt_json(
        self,
        value: EncryptedValue,
        *,
        user_id: UUID,
        record_id: UUID,
        content_kind: str,
        action: str,
        schema_version: str,
    ) -> dict[str, object]:
        """让旧实现预采样的应用时间在 request-start 前变为过期授权。"""
        sleep(self._delay_seconds)
        return super().decrypt_json(
            value,
            user_id=user_id,
            record_id=record_id,
            content_kind=content_kind,
            action=action,
            schema_version=schema_version,
        )


class _MutableClock:
    """为真实 Runner 的多次投递提供显式可推进 UTC 时钟。"""

    def __init__(self, current: datetime) -> None:
        """保存当前合成 UTC 瞬间。"""
        self.current = current

    def __call__(self) -> datetime:
        """返回本次 acquisition、退避或完成判断使用的同一瞬间。"""
        return self.current


class _Seed:
    """保存单个可信邮件动作的全部稳定测试标识。"""

    def __init__(self) -> None:
        """为每个测试生成互不碰撞的 PostgreSQL 与命令标识。"""
        self.user_id = uuid4()
        self.connection_id = uuid4()
        self.draft_id = uuid4()
        self.task_id = uuid4()
        self.step_id = uuid4()
        self.approval_id = uuid4()
        self.operation_id = uuid4()
        self.execution_id = uuid4()
        self.owner = "worker-a"
        self.provider = "google"
        self.provider_tenant_id = ""
        self.provider_account_id = "synthetic-account"
        self.account_type = "google"

    def command_payload(self) -> dict[str, object]:
        """返回一条包含合成正文但不会进入日志、审计或 checkpoint 的严格命令。"""
        return {
            "schema_version": "mail_send.v1",
            "action": "mail.send",
            "operation_id": str(self.operation_id),
            "connection_id": str(self.connection_id),
            "draft_id": str(self.draft_id),
            "draft_version": 1,
            "message_date": "2030-01-01T00:00:00Z",
            "mode": "new",
            "source_thread_id": None,
            "source_message_id": None,
            "to": ["recipient@example.test"],
            "cc": [],
            "bcc": [],
            "subject": "Synthetic subject",
            "body_text": "synthetic-sensitive-command-body",
            "thread_headers": None,
        }


async def _seed_action(
    database_url: str,
    seed: _Seed,
    *,
    deadline: datetime | None = None,
    capability_status: CapabilityStatus = CapabilityStatus.ENABLED,
    existing_execution_status: ToolExecutionStatus | None = None,
    request_started_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
    existing_result_summary: dict[str, object] | None = None,
    user_is_active: bool = True,
    task_status: TaskStatus = TaskStatus.RUNNING,
) -> str:
    """创建批准任务、加密命令、连接能力和可选既有 ToolExecution。"""
    command_payload = seed.command_payload()
    parse_trusted_command(command_payload)
    payload_hash = trusted_command_hash(command_payload)
    encrypted = ACTION_CIPHER.encrypt_json(
        command_payload,
        user_id=seed.user_id,
        record_id=seed.approval_id,
        content_kind="approval_command",
        action="mail.send",
        schema_version="mail_send.v1",
    )
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=seed.user_id,
                    email=f"{seed.user_id}@example.test",
                    display_name="Synthetic User",
                    password_hash=None,
                    timezone="UTC",
                    locale="zh-CN",
                    brief_time=time(8, 0),
                    is_active=user_is_active,
                )
            )
            # 这些 ORM 模型刻意没有关系映射；先显式写入父用户，避免 flush 无法从裸 FK
            # 推导 User→Connection/Draft/Task 的插入顺序。所有阶段仍处于同一事务。
            await session.flush()
            session.add(
                OAuthConnectionModel(
                    id=seed.connection_id,
                    user_id=seed.user_id,
                    provider=seed.provider,
                    provider_account_id=seed.provider_account_id,
                    provider_tenant_id=seed.provider_tenant_id,
                    account_type=seed.account_type,
                    account_email="sender@example.test",
                    scopes=["mail.read", "mail.send"],
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
                        actual_scopes=["mail.read"],
                    ),
                    ConnectionCapabilityModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        capability=ConnectionCapability.MAIL_SEND.value,
                        status=capability_status.value,
                        actual_scopes=(
                            ["mail.send"] if capability_status is CapabilityStatus.ENABLED else []
                        ),
                    ),
                )
            )
            session.add(
                MailDraftModel(
                    id=seed.draft_id,
                    user_id=seed.user_id,
                    connection_id=seed.connection_id,
                    creation_idempotency_key=f"draft:{seed.draft_id}",
                    creation_payload_hash="d" * 64,
                    source_thread_id=None,
                    source_message_id=None,
                    mode="new",
                    current_version=1,
                    status=(
                        MailDraftStatus.EXECUTING.value
                        if existing_execution_status is not None
                        else MailDraftStatus.AWAITING_APPROVAL.value
                    ),
                    retain_until=NOW + timedelta(days=30),
                )
            )
            session.add(
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
                    lease_owner=seed.owner if task_status is TaskStatus.RUNNING else None,
                    lease_expires_at=(
                        lease_expires_at or NOW + timedelta(minutes=1)
                        if task_status is TaskStatus.RUNNING
                        else None
                    ),
                    started_at=NOW - timedelta(seconds=1),
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
            session.add(
                ApprovalRequestModel(
                    id=seed.approval_id,
                    task_id=seed.task_id,
                    step_id=seed.step_id,
                    version=1,
                    action="mail.send",
                    payload={"storage": "encrypted", "schema_version": "mail_send.v1"},
                    schema_version="mail_send.v1",
                    risk_level="high",
                    payload_ciphertext=encrypted.ciphertext,
                    payload_nonce=encrypted.nonce,
                    payload_key_version=encrypted.key_version,
                    payload_hash=payload_hash,
                    proposal_kind="mail_draft",
                    proposal_id=seed.draft_id,
                    proposal_version=1,
                    preview_markdown="",
                    status=ApprovalStatus.APPROVED.value,
                    expires_at=NOW + timedelta(minutes=5),
                    approved_execution_deadline_at=deadline or NOW + timedelta(minutes=1),
                    decided_at=NOW - timedelta(minutes=1),
                    decided_by_user_id=seed.user_id,
                )
            )
            if existing_execution_status is not None:
                session.add(
                    ToolExecutionModel(
                        id=seed.execution_id,
                        task_id=seed.task_id,
                        step_id=seed.step_id,
                        tool_name="mail.send",
                        idempotency_key=(
                            f"mail.send:{seed.task_id}:{seed.approval_id}:1:{seed.operation_id}"
                        ),
                        operation_id=seed.operation_id,
                        request_payload_hash=payload_hash,
                        provider=seed.provider,
                        status=existing_execution_status.value,
                        result_summary=existing_result_summary,
                        claimed_at=NOW - timedelta(minutes=10),
                        request_started_at=request_started_at,
                        write_attempt_count=1 if request_started_at else 0,
                    )
                )
    finally:
        await session_factory.dispose()
    return payload_hash


def _settings(
    *,
    provider: str = "google",
    tenant: str = "",
    account: str = "synthetic-account",
    external_enabled: bool = True,
    provider_enabled: bool = True,
    allowed_identity_key: str | None = None,
) -> Settings:
    """构造只允许一个规范合成身份的非生产真实写门禁。"""
    identity_key = allowed_identity_key or canonical_provider_identity_key(
        provider,
        tenant,
        account,
    )
    return Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=external_enabled,
        google_writes_enabled=provider == "google" and provider_enabled,
        microsoft_writes_enabled=provider == "microsoft" and provider_enabled,
        write_test_account_allowlist=[identity_key],
    )


def _workflow(
    database_url: str,
    adapter: _RecordingAdapter,
    *,
    settings: Settings | None = None,
    clock: Callable[[], datetime] | None = None,
    cipher: ActionPayloadCipher = ACTION_CIPHER,
    registry: ProviderAdapterRegistry | None = None,
) -> TrustedActionExecutionUseCase:
    """组合真实 Repository、固定 registry 与确定性时钟。"""
    session_factory = build_session_factory(database_url)
    if registry is None:
        registry = (
            ProviderAdapterRegistry(google_mail_action=adapter)
            if adapter.provider == "google"
            else ProviderAdapterRegistry(microsoft_mail_action=adapter)
        )
    return TrustedActionExecutionUseCase(
        transactions=SqlAlchemyTrustedActionRepositoryFactory(session_factory, cipher),
        adapters=registry,
        write_policy=settings or _settings(),
        clock=clock or (lambda: NOW),
        dispose=session_factory.dispose,
    )


class _TrustedWorkflowStep:
    """让真实 DurableTaskRunner 驱动同一 claim/dispatch/finalize 应用路径。"""

    name = "trusted_action_execution"

    def __init__(
        self,
        *,
        workflow: TrustedActionExecutionUseCase,
        seed: _Seed,
        payload_hash: str,
        max_transient_retries: int,
    ) -> None:
        """绑定冻结审批标识与外层 Runner 使用的同一耐久重试上限。"""
        self._workflow = workflow
        self._seed = seed
        self._payload_hash = payload_hash
        self._max_transient_retries = max_transient_retries

    async def execute(self, task: LeasedTask) -> None:
        """使用 Runner 当前租约 owner 执行一次安全写尝试。"""
        lease_owner = task.lease_owner or ""
        await self._workflow.claim(
            task_id=self._seed.task_id,
            approval_id=self._seed.approval_id,
            operation_id=self._seed.operation_id,
            expected_payload_hash=self._payload_hash,
            lease_owner=lease_owner,
        )
        await self._workflow.execute_or_reconcile(
            task_id=self._seed.task_id,
            approval_id=self._seed.approval_id,
            operation_id=self._seed.operation_id,
            expected_payload_hash=self._payload_hash,
            lease_owner=lease_owner,
            may_retry_write=task.attempt_count <= self._max_transient_retries,
        )
        await self._workflow.finalize(
            task_id=self._seed.task_id,
            approval_id=self._seed.approval_id,
            operation_id=self._seed.operation_id,
            expected_payload_hash=self._payload_hash,
            decision=ApprovalStatus.APPROVED.value,
            lease_owner=lease_owner,
        )


async def _run_approved(
    workflow: TrustedActionExecutionUseCase,
    seed: _Seed,
    payload_hash: str,
    *,
    owner: str | None = None,
) -> None:
    """运行 claim、dispatch 与固定尾节点，模拟批准后的 Graph 路径。"""
    lease_owner = owner or seed.owner
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=lease_owner,
        )
        await workflow.execute_or_reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=lease_owner,
        )
        await workflow.finalize(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            decision="approved",
            lease_owner=lease_owner,
        )
    finally:
        await workflow.dispose()


@pytest.mark.asyncio
async def test_two_workers_create_one_claim_and_one_provider_call(database_url: str) -> None:
    """同一租约节点并发重放只能有一行 claim 与一次 request-start/provider write。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter()
    first = _workflow(database_url, adapter)
    second = _workflow(database_url, adapter)

    results = await asyncio.gather(
        _run_approved(first, seed, payload_hash, owner=seed.owner),
        _run_approved(second, seed, payload_hash, owner="worker-b"),
        return_exceptions=True,
    )

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            executions = tuple((await session.scalars(select(ToolExecutionModel))).all())
            draft_status = await session.scalar(
                select(MailDraftModel.status).where(MailDraftModel.id == seed.draft_id)
            )
            claimed_audits = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(AuditEventModel.event_type == "tool.claimed")
            )
            claimed_outbox = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(OutboxEventModel.topic == "tool.claimed")
            )
    finally:
        await session_factory.dispose()

    assert len(executions) == 1
    assert executions[0].status == ToolExecutionStatus.SUCCEEDED.value
    assert executions[0].write_attempt_count == 1
    assert executions[0].idempotency_key == (
        f"mail.send:{seed.task_id}:{seed.approval_id}:1:{seed.operation_id}"
    )
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 0
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert draft_status == MailDraftStatus.SENT.value
    assert claimed_audits == claimed_outbox == 1


@pytest.mark.asyncio
async def test_claim_after_approved_deadline_invalidates_without_provider_call(
    database_url: str,
) -> None:
    """五分钟截止已过必须同事务失效审批、失败任务并恢复本地编辑态。"""
    seed = _Seed()
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            database_now = await session.scalar(select(func.clock_timestamp()))
    finally:
        await session_factory.dispose()
    assert isinstance(database_now, datetime)
    payload_hash = await _seed_action(
        database_url,
        seed,
        deadline=database_now - timedelta(microseconds=1),
    )
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "approval_execution_deadline_expired"
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            execution_count = await session.scalar(
                select(func.count()).select_from(ToolExecutionModel)
            )
    finally:
        await session_factory.dispose()
    assert task is not None and task.error_code == "approval_execution_deadline_expired"
    assert task.status == TaskStatus.FAILED.value
    assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
    assert draft is not None and draft.status == MailDraftStatus.EDITING.value
    assert execution_count == 0
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_claim_uses_database_time_after_waiting_for_task_lock(
    database_url: str,
) -> None:
    """claim 等锁跨过截止点后必须按 PostgreSQL 当前时间拒绝旧授权。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter()
    blocker_factory = build_session_factory(database_url)
    workflow: TrustedActionExecutionUseCase | None = None
    claim_task: asyncio.Task[None] | None = None
    try:
        async with blocker_factory.begin() as blocker:
            task = await blocker.scalar(
                select(TaskRunModel).where(TaskRunModel.id == seed.task_id).with_for_update()
            )
            approval = await blocker.scalar(
                select(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == seed.approval_id)
                .with_for_update()
            )
            database_now = await blocker.scalar(select(func.clock_timestamp()))
            assert task is not None and approval is not None
            assert isinstance(database_now, datetime)
            deadline = database_now + timedelta(milliseconds=400)
            task.lease_expires_at = database_now + timedelta(minutes=1)
            approval.approved_execution_deadline_at = deadline
            workflow = _workflow(database_url, adapter, clock=lambda: database_now)
            claim_task = asyncio.create_task(
                workflow.claim(
                    task_id=seed.task_id,
                    approval_id=seed.approval_id,
                    operation_id=seed.operation_id,
                    expected_payload_hash=payload_hash,
                    lease_owner=seed.owner,
                )
            )
            # 先证明 claim 已进入并被 TaskRun 行锁阻塞，再按数据库时钟等待截止点跨过。
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(claim_task), timeout=0.2)
            while True:
                observed_now = await blocker.scalar(select(func.clock_timestamp()))
                assert isinstance(observed_now, datetime)
                if observed_now > deadline:
                    break
                await asyncio.sleep(0.02)

        assert claim_task is not None
        with pytest.raises(StateConflictError) as raised:
            await claim_task
        assert raised.value.error_code == "approval_execution_deadline_expired"
    finally:
        if claim_task is not None and not claim_task.done():
            claim_task.cancel()
            await asyncio.gather(claim_task, return_exceptions=True)
        if workflow is not None:
            await workflow.dispose()
        await blocker_factory.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            approval = await session.get(ApprovalRequestModel, seed.approval_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution_count == 0
    assert task is not None and task.status == TaskStatus.FAILED.value
    assert task.error_code == "approval_execution_deadline_expired"
    assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
    assert draft is not None and draft.status == MailDraftStatus.EDITING.value
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_stale_claim_before_request_is_recovered_with_one_write(database_url: str) -> None:
    """已按时认领但未提交 request-start 的过期租约可复用原 claim 首次写入。"""
    seed = _Seed()
    seed.owner = "stale-worker"
    payload_hash = await _seed_action(
        database_url,
        seed,
        deadline=NOW - timedelta(minutes=5),
        existing_execution_status=ToolExecutionStatus.CLAIMED,
        request_started_at=None,
        lease_expires_at=NOW - timedelta(seconds=1),
    )
    adapter = _RecordingAdapter()
    session_factory = build_session_factory(database_url)
    try:
        leased = await SqlAlchemyTaskExecutionStore(session_factory).acquire(
            task_id=seed.task_id,
            lease_owner="recovery-worker",
            now=NOW,
            lease_expires_at=NOW + timedelta(minutes=1),
        )
    finally:
        await session_factory.dispose()
    assert leased is not None and leased.lease_owner == "recovery-worker"

    await _run_approved(
        _workflow(database_url, adapter),
        seed,
        payload_hash,
        owner="recovery-worker",
    )

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            executions = tuple((await session.scalars(select(ToolExecutionModel))).all())
    finally:
        await session_factory.dispose()
    assert len(executions) == 1 and executions[0].id == seed.execution_id
    assert executions[0].write_attempt_count == 1
    assert adapter.write_calls == 1


@pytest.mark.asyncio
async def test_claim_serializes_with_connection_first_capability_disable(
    database_url: str,
) -> None:
    """claim 必须等待 connection-first 撤权事务并读取提交后的 disabled 能力。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    blocker_factory = build_session_factory(database_url)
    claim_task: asyncio.Task[None] | None = None
    try:
        async with blocker_factory.begin() as blocker:
            connection = await blocker.scalar(
                select(OAuthConnectionModel)
                .where(OAuthConnectionModel.id == seed.connection_id)
                .with_for_update()
            )
            assert connection is not None
            claim_task = asyncio.create_task(
                workflow.claim(
                    task_id=seed.task_id,
                    approval_id=seed.approval_id,
                    operation_id=seed.operation_id,
                    expected_payload_hash=payload_hash,
                    lease_owner=seed.owner,
                )
            )
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(claim_task), timeout=0.25)
            await blocker.execute(
                update(ConnectionCapabilityModel)
                .where(
                    ConnectionCapabilityModel.connection_id == seed.connection_id,
                    ConnectionCapabilityModel.capability == ConnectionCapability.MAIL_SEND.value,
                )
                .values(status=CapabilityStatus.DISABLED.value)
            )
        assert claim_task is not None
        with pytest.raises(StateConflictError) as raised:
            await claim_task
        assert raised.value.error_code == "connection_capability_disabled"
    finally:
        if claim_task is not None and not claim_task.done():
            claim_task.cancel()
            await asyncio.gather(claim_task, return_exceptions=True)
        await workflow.dispose()
        await blocker_factory.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution_count == 0
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_request_start_rechecks_lease_with_database_time_after_decryption(
    database_url: str,
) -> None:
    """解密跨过租约后必须以锁内 PostgreSQL 时间拒绝 request-start。"""
    seed = _Seed()
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            database_now = await session.scalar(select(func.clock_timestamp()))
    finally:
        await session_factory.dispose()
    assert isinstance(database_now, datetime)
    payload_hash = await _seed_action(
        database_url,
        seed,
        lease_expires_at=database_now + timedelta(seconds=1),
    )
    adapter = _RecordingAdapter()
    workflow = _workflow(
        database_url,
        adapter,
        clock=lambda: database_now,
        cipher=_DelayedActionPayloadCipher(1.25),
    )
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        await workflow.execute_or_reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution is not None
    assert execution.status == ToolExecutionStatus.CLAIMED.value
    assert execution.request_started_at is None
    assert execution.write_attempt_count == 0
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_retryable_failed_requires_exact_persisted_not_applied_proof(
    database_url: str,
) -> None:
    """仅状态名为 retryable_failed 不能授权第二次供应商写请求。"""
    seed = _Seed()
    payload_hash = await _seed_action(
        database_url,
        seed,
        existing_execution_status=ToolExecutionStatus.RETRYABLE_FAILED,
        request_started_at=NOW - timedelta(seconds=1),
        existing_result_summary={
            "kind": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
            "retryable": False,
        },
    )
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "trusted_action_unavailable"
    finally:
        await workflow.dispose()
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_dispatch_rejects_execution_provider_rebound_before_load(
    database_url: str,
) -> None:
    """dispatch 必须从冻结 connection 重证 provider，不能信任执行行的自述值。"""
    seed = _Seed()
    payload_hash = await _seed_action(
        database_url,
        seed,
        existing_execution_status=ToolExecutionStatus.CLAIMED,
    )
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            await session.execute(
                update(ToolExecutionModel)
                .where(ToolExecutionModel.id == seed.execution_id)
                .values(provider="microsoft")
            )
    finally:
        await session_factory.dispose()

    google_adapter = _RecordingAdapter(provider="google")
    microsoft_adapter = _RecordingAdapter(provider="microsoft")
    workflow = _workflow(
        database_url,
        google_adapter,
        registry=ProviderAdapterRegistry(
            google_mail_action=google_adapter,
            microsoft_mail_action=microsoft_adapter,
        ),
    )
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "trusted_action_unavailable"
    finally:
        await workflow.dispose()
    assert google_adapter.write_calls == microsoft_adapter.write_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    [
        "retry_summary",
        "idempotency_key",
        "provider",
        "tool_name",
        "operation_id",
        "request_payload_hash",
        "approval_proposal_id",
        "approval_proposal_version",
        "approval_proposal_kind",
        "draft_version",
        "draft_status",
        "draft_connection",
    ],
)
async def test_request_start_cas_rechecks_retry_proof_and_binding(
    database_url: str,
    tamper: str,
) -> None:
    """dispatch 读取后的持久摘要或绑定篡改仍必须在锁内 CAS 被拒绝。"""
    seed = _Seed()
    valid_summary = {
        "kind": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
        "retryable": True,
        "retry_after_seconds": 17,
    }
    await _seed_action(
        database_url,
        seed,
        existing_execution_status=ToolExecutionStatus.RETRYABLE_FAILED,
        request_started_at=NOW - timedelta(seconds=1),
        existing_result_summary=valid_summary,
    )
    session_factory = build_session_factory(database_url)
    transactions = SqlAlchemyTrustedActionRepositoryFactory(session_factory, ACTION_CIPHER)
    try:
        async with transactions() as transaction:
            snapshot = await transaction.load_dispatch(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
            )
        assert snapshot is not None
        async with session_factory.begin() as session:
            values: dict[str, object] | None = None
            if tamper == "retry_summary":
                values = {
                    "result_summary": {
                        "kind": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
                        "retryable": True,
                        "retry_after_seconds": 17,
                        "unexpected": True,
                    }
                }
            elif tamper == "idempotency_key":
                values = {"idempotency_key": f"tampered:{seed.execution_id}"}
            elif tamper == "provider":
                values = {"provider": "microsoft"}
            elif tamper == "tool_name":
                values = {"tool_name": "calendar.create"}
            elif tamper == "operation_id":
                values = {"operation_id": uuid4()}
            elif tamper == "request_payload_hash":
                values = {"request_payload_hash": "f" * 64}
            elif tamper == "approval_proposal_id":
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.id == seed.approval_id)
                    .values(proposal_id=uuid4())
                )
            elif tamper == "approval_proposal_version":
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.id == seed.approval_id)
                    .values(proposal_version=2)
                )
            elif tamper == "approval_proposal_kind":
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.id == seed.approval_id)
                    .values(proposal_kind="calendar_proposal")
                )
            elif tamper == "draft_version":
                await session.execute(
                    update(MailDraftModel)
                    .where(MailDraftModel.id == seed.draft_id)
                    .values(current_version=2)
                )
            elif tamper == "draft_status":
                await session.execute(
                    update(MailDraftModel)
                    .where(MailDraftModel.id == seed.draft_id)
                    .values(status=MailDraftStatus.EDITING.value)
                )
            else:
                alternate_connection_id = uuid4()
                session.add(
                    OAuthConnectionModel(
                        id=alternate_connection_id,
                        user_id=seed.user_id,
                        provider="google",
                        provider_account_id=f"alternate-{alternate_connection_id}",
                        provider_tenant_id="",
                        account_type="google",
                        account_email="alternate@example.test",
                        scopes=["mail.read", "mail.send"],
                        status="connected",
                    )
                )
                await session.flush()
                await session.execute(
                    update(MailDraftModel)
                    .where(MailDraftModel.id == seed.draft_id)
                    .values(connection_id=alternate_connection_id)
                )
            if values is not None:
                await session.execute(
                    update(ToolExecutionModel)
                    .where(ToolExecutionModel.id == seed.execution_id)
                    .values(**values)
                )
        async with transactions() as transaction:
            started = await transaction.mark_request_started(
                snapshot=snapshot,
                lease_owner=seed.owner,
            )
    finally:
        await session_factory.dispose()
    assert started is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    ["proposal_version", "proposal_status", "proposal_calendar", "proposal_connection"],
)
async def test_calendar_request_start_cas_rechecks_frozen_target_binding(
    database_url: str,
    tamper: str,
) -> None:
    """日历 request-start 必须重锁提案并复核版本、状态、连接与精确 calendar。"""
    seed = _Seed()
    calendar_id = "synthetic-calendar"
    await _seed_action(
        database_url,
        seed,
        existing_execution_status=ToolExecutionStatus.CLAIMED,
    )
    session_factory = build_session_factory(database_url)
    transactions = SqlAlchemyTrustedActionRepositoryFactory(session_factory, ACTION_CIPHER)
    try:
        async with session_factory.begin() as session:
            session.add_all(
                (
                    ConnectionCapabilityModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        capability=ConnectionCapability.CALENDAR_READ.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=["calendar.read"],
                    ),
                    ConnectionCapabilityModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        capability=ConnectionCapability.CALENDAR_WRITE.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=["calendar.write"],
                    ),
                    ProviderCalendarModel(
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        provider_calendar_id=calendar_id,
                        name="Synthetic calendar",
                        timezone="UTC",
                        is_primary=True,
                        access_role="owner",
                        can_write=True,
                        provider_url=None,
                    ),
                    CalendarChangeProposalModel(
                        id=seed.draft_id,
                        user_id=seed.user_id,
                        connection_id=seed.connection_id,
                        creation_idempotency_key=f"proposal:{seed.draft_id}",
                        creation_payload_hash="c" * 64,
                        calendar_id=calendar_id,
                        operation_kind="create",
                        target_event_id=None,
                        base_etag=None,
                        current_version=1,
                        status="executing",
                        retain_until=NOW + timedelta(days=30),
                    ),
                )
            )
            await session.execute(
                update(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == seed.approval_id)
                .values(
                    action="calendar.create",
                    schema_version="calendar_create.v1",
                    proposal_kind="calendar_proposal",
                )
            )
            await session.execute(
                update(ToolExecutionModel)
                .where(ToolExecutionModel.id == seed.execution_id)
                .values(
                    tool_name="calendar.create",
                    idempotency_key=(
                        f"calendar.create:{seed.task_id}:{seed.approval_id}:1:{seed.operation_id}"
                    ),
                )
            )
        async with transactions() as transaction:
            snapshot = await transaction.load_dispatch(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
            )
        assert snapshot is not None
        async with session_factory.begin() as session:
            if tamper == "proposal_version":
                await session.execute(
                    update(CalendarChangeProposalModel)
                    .where(CalendarChangeProposalModel.id == seed.draft_id)
                    .values(current_version=2)
                )
            elif tamper == "proposal_status":
                await session.execute(
                    update(CalendarChangeProposalModel)
                    .where(CalendarChangeProposalModel.id == seed.draft_id)
                    .values(status="editing")
                )
            elif tamper == "proposal_calendar":
                await session.execute(
                    update(CalendarChangeProposalModel)
                    .where(CalendarChangeProposalModel.id == seed.draft_id)
                    .values(calendar_id="rebound-calendar")
                )
            else:
                alternate_connection_id = uuid4()
                session.add(
                    OAuthConnectionModel(
                        id=alternate_connection_id,
                        user_id=seed.user_id,
                        provider="google",
                        provider_account_id=f"alternate-{alternate_connection_id}",
                        provider_tenant_id="",
                        account_type="google",
                        account_email="alternate-calendar@example.test",
                        scopes=["calendar.read", "calendar.write"],
                        status="connected",
                    )
                )
                await session.flush()
                await session.execute(
                    update(CalendarChangeProposalModel)
                    .where(CalendarChangeProposalModel.id == seed.draft_id)
                    .values(connection_id=alternate_connection_id)
                )
        async with transactions() as transaction:
            started = await transaction.mark_request_started(
                snapshot=snapshot,
                lease_owner=seed.owner,
            )
    finally:
        await session_factory.dispose()
    assert started is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [
        ToolExecutionStatus.SUCCEEDED,
        ToolExecutionStatus.CONFIRMED_FAILED,
        ToolExecutionStatus.NEEDS_ATTENTION,
    ],
)
async def test_terminal_execution_returns_before_decryption_and_adapter_lookup(
    database_url: str,
    terminal_status: ToolExecutionStatus,
) -> None:
    """终态在内容、本地动作及连接清理后仍须只靠最小持久事实复用。"""
    seed = _Seed()
    payload_hash = await _seed_action(
        database_url,
        seed,
        existing_execution_status=terminal_status,
        request_started_at=NOW - timedelta(seconds=1),
    )
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            await session.execute(
                update(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == seed.approval_id)
                .values(
                    payload_ciphertext=None,
                    payload_nonce=None,
                    payload_key_version=None,
                )
            )
            await session.execute(delete(MailDraftModel).where(MailDraftModel.id == seed.draft_id))
            await session.execute(
                delete(OAuthConnectionModel).where(OAuthConnectionModel.id == seed.connection_id)
            )
    finally:
        await session_factory.dispose()
    adapter = _RecordingAdapter()
    workflow = _workflow(
        database_url,
        adapter,
        registry=ProviderAdapterRegistry(),
    )
    try:
        await workflow.execute_or_reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            persisted_status = await session.scalar(
                select(ToolExecutionModel.status).where(ToolExecutionModel.id == seed.execution_id)
            )
    finally:
        await session_factory.dispose()
    assert persisted_status == terminal_status.value
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_non_production_claim_rejects_connection_outside_allowlist(
    database_url: str,
) -> None:
    """规范 provider identity 不在专用白名单时必须零 claim、零 provider 调用。"""
    seed = _Seed()
    seed.provider_account_id = "different-account"
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter, settings=_settings(account="allowed-account"))
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "external_write_account_not_allowed"
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            task_error = await session.scalar(
                select(TaskRunModel.error_code).where(TaskRunModel.id == seed.task_id)
            )
            execution_count = await session.scalar(
                select(func.count()).select_from(ToolExecutionModel)
            )
    finally:
        await session_factory.dispose()
    assert task_error == "external_write_account_not_allowed"
    assert execution_count == 0
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_provider_only_kill_switch_blocks_claim_with_global_switch_enabled(
    database_url: str,
) -> None:
    """全局开启但当前 provider 关闭时仍必须零 claim、零真实写入。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter()
    workflow = _workflow(
        database_url,
        adapter,
        settings=_settings(external_enabled=True, provider_enabled=False),
    )
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "external_writes_disabled"
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution_count == 0
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_inactive_user_cannot_create_tool_execution_or_call_provider(
    database_url: str,
) -> None:
    """删除屏障后的 inactive 用户必须在 claim 事务内 fail closed。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed, user_is_active=False)
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "trusted_action_unavailable"
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution_count == 0
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "tenant", "account", "account_type"),
    [
        ("google", "", "opaque:subject%1", "google"),
        (
            "microsoft",
            "tenant-a",
            "tenant-a:graph@opaque%1",
            "work_school",
        ),
    ],
)
async def test_claim_uses_canonical_provider_identity_for_google_and_microsoft(
    database_url: str,
    provider: str,
    tenant: str,
    account: str,
    account_type: str,
) -> None:
    """Google 保留字符与 Microsoft tenant-bound 身份只按共享 canonical key 放行。"""
    seed = _Seed()
    seed.provider = provider
    seed.provider_tenant_id = tenant
    seed.provider_account_id = account
    seed.account_type = account_type
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter(provider=provider)
    await _run_approved(
        _workflow(
            database_url,
            adapter,
            settings=_settings(provider=provider, tenant=tenant, account=account),
        ),
        seed,
        payload_hash,
    )
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["different_google_canonical", "microsoft_tenant_mismatch"])
async def test_claim_rejects_canonical_allowlist_or_tenant_binding_mismatch(
    database_url: str,
    case: str,
) -> None:
    """不同 canonical 身份及 Microsoft 内外 tenant 不一致都必须零 claim。"""
    seed = _Seed()
    if case == "different_google_canonical":
        seed.provider_account_id = "opaque:subject"
        settings = _settings(
            allowed_identity_key=canonical_provider_identity_key(
                "google",
                "",
                "opaque%3Asubject",
            )
        )
        adapter = _RecordingAdapter()
    else:
        seed.provider = "microsoft"
        seed.provider_tenant_id = "tenant-a"
        seed.provider_account_id = "tenant-b:graph-user"
        seed.account_type = "work_school"
        settings = _settings(
            provider="microsoft",
            tenant="tenant-a",
            account="tenant-a:graph-user",
        )
        adapter = _RecordingAdapter(provider="microsoft")
    payload_hash = await _seed_action(database_url, seed)
    workflow = _workflow(database_url, adapter, settings=settings)
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "external_write_account_not_allowed"
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution_count == 0
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability_status", "enabled", "error_code"),
    [
        (CapabilityStatus.DISABLED, True, "connection_capability_disabled"),
        (CapabilityStatus.ENABLED, False, "external_writes_disabled"),
    ],
)
async def test_claim_rechecks_queued_capability_and_write_switches(
    database_url: str,
    capability_status: CapabilityStatus,
    enabled: bool,
    error_code: str,
) -> None:
    """提交后被撤销的能力或 kill switch 必须在 claim 前再次 fail closed。"""
    seed = _Seed()
    payload_hash = await _seed_action(
        database_url,
        seed,
        capability_status=capability_status,
    )
    adapter = _RecordingAdapter()
    workflow = _workflow(
        database_url,
        adapter,
        settings=_settings(external_enabled=enabled, provider_enabled=enabled),
    )
    try:
        with pytest.raises(StateConflictError) as raised:
            await workflow.claim(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == error_code
    finally:
        await workflow.dispose()
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_claimed_command_aad_tampering_never_reaches_provider(database_url: str) -> None:
    """claim 后命令密文/AAD 被替换时必须在 request-start 前失败。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        session_factory = build_session_factory(database_url)
        try:
            async with session_factory.begin() as session:
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.id == seed.approval_id)
                    .values(payload_nonce=b"z" * 12)
                )
        finally:
            await session_factory.dispose()

        with pytest.raises(StateConflictError) as raised:
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "trusted_action_unavailable"
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution = await session.scalar(select(ToolExecutionModel))
            integrity_audit = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "tool.confirmed_failed",
                )
            )
            task_failed_audit = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "task.failed",
                )
            )
            integrity_outbox = await session.scalar(
                select(OutboxEventModel).where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "tool.confirmed_failed",
                )
            )
    finally:
        await session_factory.dispose()
    assert execution is not None and execution.request_started_at is None
    assert execution.write_attempt_count == 0
    assert integrity_audit is not None
    assert task_failed_audit is not None
    assert integrity_outbox is not None
    assert adapter.write_calls == 0


@pytest.mark.asyncio
async def test_claimed_payload_hash_tampering_fails_before_request_start(
    database_url: str,
) -> None:
    """claim 后审批哈希被替换时必须在解密和 request-start 前 fail closed。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        session_factory = build_session_factory(database_url)
        try:
            async with session_factory.begin() as session:
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.id == seed.approval_id)
                    .values(payload_hash="f" * 64)
                )
        finally:
            await session_factory.dispose()

        with pytest.raises(StateConflictError) as raised:
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "trusted_action_unavailable"
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution is not None and execution.request_started_at is None
    assert execution.write_attempt_count == 0
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("binding_field", ["operation_id", "connection_id", "draft_id"])
async def test_reencrypted_command_binding_mismatch_fails_before_request_start(
    database_url: str,
    binding_field: str,
) -> None:
    """攻击者重加密且同步篡改哈希也不能换绑 operation、connection 或 proposal。"""
    seed = _Seed()
    original_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter()
    workflow = _workflow(database_url, adapter)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=original_hash,
            lease_owner=seed.owner,
        )
        tampered_command = seed.command_payload()
        tampered_command[binding_field] = str(uuid4())
        parse_trusted_command(tampered_command)
        tampered_hash = trusted_command_hash(tampered_command)
        encrypted = ACTION_CIPHER.encrypt_json(
            tampered_command,
            user_id=seed.user_id,
            record_id=seed.approval_id,
            content_kind="approval_command",
            action="mail.send",
            schema_version="mail_send.v1",
        )
        session_factory = build_session_factory(database_url)
        try:
            async with session_factory.begin() as session:
                await session.execute(
                    update(ApprovalRequestModel)
                    .where(ApprovalRequestModel.id == seed.approval_id)
                    .values(
                        payload_ciphertext=encrypted.ciphertext,
                        payload_nonce=encrypted.nonce,
                        payload_key_version=encrypted.key_version,
                        payload_hash=tampered_hash,
                    )
                )
                await session.execute(
                    update(ToolExecutionModel)
                    .where(ToolExecutionModel.task_id == seed.task_id)
                    .values(request_payload_hash=tampered_hash)
                )
        finally:
            await session_factory.dispose()

        with pytest.raises(StateConflictError) as raised:
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=tampered_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "trusted_action_unavailable"
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution is not None and execution.request_started_at is None
    assert execution.write_attempt_count == 0
    assert adapter.write_calls == adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_confirmed_not_applied_retry_persists_exact_retryable_fact(
    database_url: str,
) -> None:
    """安全重试授权必须先以同名 audit/outbox 和有界 Retry-After 落库。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter(
        ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
            retryable=True,
            retry_after_seconds=17,
            provider_resource_id=None,
            provider_request_id="synthetic-retry-request",
            correlation_id="synthetic-retry-correlation",
            provider_url=None,
            error_code="provider_busy",
        )
    )
    workflow = _workflow(database_url, adapter)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        with pytest.raises(TransientProviderError) as raised:
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )
        assert raised.value.error_code == "provider_busy"
        assert raised.value.retry_after == 17
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution = await session.scalar(select(ToolExecutionModel))
            retry_audit = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "tool.retryable_failed",
                )
            )
            retry_outbox = await session.scalar(
                select(OutboxEventModel).where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "tool.retryable_failed",
                )
            )
    finally:
        await session_factory.dispose()
    assert execution is not None
    assert execution.status == ToolExecutionStatus.RETRYABLE_FAILED.value
    assert execution.write_attempt_count == 1
    assert execution.result_summary == {
        "kind": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
        "retryable": True,
        "retry_after_seconds": 17,
    }
    assert retry_audit is not None
    assert retry_outbox is not None
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_real_runner_retries_same_execution_once_then_exhausts_without_third_write(
    database_url: str,
) -> None:
    """真实 Runner/Store 必须持久化 17 秒重试并在预算耗尽后阻止第三次写。"""
    seed = _Seed()
    payload_hash = await _seed_action(
        database_url,
        seed,
        task_status=TaskStatus.QUEUED,
    )
    adapter = _RecordingAdapter(
        ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
            retryable=True,
            retry_after_seconds=17,
            provider_resource_id=None,
            provider_request_id="synthetic-durable-retry-request",
            correlation_id="synthetic-durable-retry-correlation",
            provider_url=None,
            error_code="provider_busy",
        )
    )
    clock = _MutableClock(NOW)
    workflow = _workflow(database_url, adapter, clock=clock)
    runner_factory = build_session_factory(database_url)
    step = _TrustedWorkflowStep(
        workflow=workflow,
        seed=seed,
        payload_hash=payload_hash,
        max_transient_retries=1,
    )
    runner = DurableTaskRunner(
        store=SqlAlchemyTaskExecutionStore(runner_factory),
        clock=clock,
        lease_duration=timedelta(minutes=1),
        task_timeout_seconds=120,
        task_step_timeout_seconds=30,
        max_transient_retries=1,
        resolve_steps=lambda _task: (step,),
    )
    try:
        assert await runner.run(
            seed.task_id,
            lease_owner="durable-owner-1",
            retry_delay=timedelta(seconds=5),
        )
        async with runner_factory() as session:
            first_task = await session.get(TaskRunModel, seed.task_id)
            first_execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            retry_outbox = await session.scalar(
                select(OutboxEventModel).where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "task.execute",
                    OutboxEventModel.deduplication_key == f"task.execute:{seed.task_id}:retry:1",
                )
            )
        assert first_task is not None
        assert first_task.status == TaskStatus.RETRY_SCHEDULED.value
        assert first_task.attempt_count == 1
        assert first_execution is not None
        assert first_execution.status == ToolExecutionStatus.RETRYABLE_FAILED.value
        assert first_execution.write_attempt_count == 1
        assert retry_outbox is not None
        assert retry_outbox.available_at == NOW + timedelta(seconds=17)

        # 模拟 relay 在延迟到期后成功交接；下一条消息必须由新 owner 获取同一任务。
        clock.current = NOW + timedelta(seconds=17)
        async with runner_factory.begin() as session:
            retry_event = await session.get(OutboxEventModel, retry_outbox.id)
            assert retry_event is not None
            retry_event.published_at = clock.current
        await runner.run(
            seed.task_id,
            lease_owner="durable-owner-2",
            retry_delay=timedelta(seconds=5),
        )
        # 第二次仍明确未应用，但最大重试次数已耗尽；重复投递不得取得第三次租约。
        clock.current = NOW + timedelta(seconds=18)
        assert not await runner.run(
            seed.task_id,
            lease_owner="durable-owner-3",
            retry_delay=timedelta(seconds=5),
        )

        async with runner_factory() as session:
            final_task = await session.get(TaskRunModel, seed.task_id)
            executions = tuple(
                (
                    await session.scalars(
                        select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
                    )
                ).all()
            )
            draft = await session.get(MailDraftModel, seed.draft_id)
            retry_outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "task.execute",
                    OutboxEventModel.deduplication_key.like(f"task.execute:{seed.task_id}:retry:%"),
                )
            )
            task_failed_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "task.failed",
                )
            )
            tool_failed_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "tool.confirmed_failed",
                )
            )
            tool_failed_outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "tool.confirmed_failed",
                )
            )
        assert final_task is not None
        assert final_task.status == TaskStatus.FAILED.value
        assert final_task.error_code == "task_retries_exhausted"
        assert final_task.attempt_count == 2
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert len(executions) == 1
        assert executions[0].write_attempt_count == 2
        assert executions[0].status == ToolExecutionStatus.CONFIRMED_FAILED.value
        assert executions[0].error_code == "task_retries_exhausted"
        assert executions[0].completed_at == clock.current - timedelta(seconds=1)
        assert retry_outbox_count == 1
        assert task_failed_count == tool_failed_count == tool_failed_outbox_count == 1
        assert adapter.write_calls == 2
        assert adapter.reconcile_calls == 0
    finally:
        await workflow.dispose()
        await runner_factory.dispose()


@pytest.mark.asyncio
async def test_permanent_not_applied_failure_atomically_writes_task_and_tool_audits(
    database_url: str,
) -> None:
    """永久未应用结果必须在同一事务收敛任务、本地对象与两条审计。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter(
        ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id=None,
            provider_request_id="synthetic-rejected-request",
            correlation_id="synthetic-rejected-correlation",
            provider_url=None,
            error_code="provider_rejected",
        )
    )
    await _run_approved(_workflow(database_url, adapter), seed, payload_hash)

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            task_failed_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "task.failed",
                )
            )
            tool_failed_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "tool.confirmed_failed",
                )
            )
            retry_outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "task.execute",
                    OutboxEventModel.deduplication_key.like("%:retry:%"),
                )
            )
    finally:
        await session_factory.dispose()
    assert task is not None and task.status == TaskStatus.FAILED.value
    assert task.error_code == "provider_rejected"
    assert draft is not None and draft.status == MailDraftStatus.EDITING.value
    assert execution is not None
    assert execution.status == ToolExecutionStatus.CONFIRMED_FAILED.value
    assert execution.write_attempt_count == 1
    assert task_failed_count == tool_failed_count == 1
    assert retry_outbox_count == 0
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_unknown_outcome_is_a_stable_task19_failure_without_reconciliation_state(
    database_url: str,
) -> None:
    """Task 19 未实现核对调度时，unknown 不得留下悬空 reconciling 状态。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _RecordingAdapter(
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
    )
    await _run_approved(_workflow(database_url, adapter), seed, payload_hash)

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            task = await session.get(TaskRunModel, seed.task_id)
            draft = await session.get(MailDraftModel, seed.draft_id)
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            task_failed_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "task.failed",
                )
            )
            tool_failed_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "tool.confirmed_failed",
                )
            )
            reconciling_audit_count = await session.scalar(
                select(func.count())
                .select_from(AuditEventModel)
                .where(
                    AuditEventModel.task_id == seed.task_id,
                    AuditEventModel.event_type == "tool.reconciling",
                )
            )
            reconciling_outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "tool.reconciling",
                )
            )
            retry_outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(
                    OutboxEventModel.aggregate_id == seed.task_id,
                    OutboxEventModel.topic == "task.execute",
                    OutboxEventModel.deduplication_key.like("%:retry:%"),
                )
            )
    finally:
        await session_factory.dispose()
    assert task is not None and task.status == TaskStatus.FAILED.value
    assert task.error_code == "provider_write_outcome_unknown"
    assert draft is not None and draft.status == MailDraftStatus.EDITING.value
    assert execution is not None
    assert execution.status == ToolExecutionStatus.CONFIRMED_FAILED.value
    assert execution.error_code == "provider_write_outcome_unknown"
    assert execution.completed_at == NOW
    assert task_failed_count == tool_failed_count == 1
    assert reconciling_audit_count == reconciling_outbox_count == retry_outbox_count == 0
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 0


@pytest.mark.asyncio
async def test_provider_url_never_enters_result_audit_or_outbox(
    database_url: str,
) -> None:
    """供应商检查 URL 端口值不得进入 Task 19 持久结果或事件载荷。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    sensitive_marker = "provider-url-sensitive-marker"
    adapter = _RecordingAdapter(
        ProviderWriteOutcome(
            kind=ProviderWriteOutcomeKind.CONFIRMED_APPLIED,
            retryable=False,
            retry_after_seconds=None,
            provider_resource_id="synthetic-resource",
            provider_request_id="synthetic-request",
            correlation_id="synthetic-correlation",
            provider_url=(
                f"javascript://user:{sensitive_marker}@example.test/path?token={sensitive_marker}"
            ),
            error_code=None,
        )
    )
    await _run_approved(_workflow(database_url, adapter), seed, payload_hash)

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
            audit_payloads = tuple(
                (
                    await session.scalars(
                        select(AuditEventModel.event_metadata).where(
                            AuditEventModel.task_id == seed.task_id
                        )
                    )
                ).all()
            )
            outbox_payloads = tuple(
                (
                    await session.scalars(
                        select(OutboxEventModel.payload).where(
                            OutboxEventModel.aggregate_id == seed.task_id
                        )
                    )
                ).all()
            )
    finally:
        await session_factory.dispose()
    assert execution is not None
    persisted = repr((execution.result_summary, audit_payloads, outbox_payloads))
    assert sensitive_marker not in persisted
    assert "provider_url" not in persisted


@pytest.mark.asyncio
async def test_crash_after_committed_request_start_recovers_only_by_reconciliation(
    database_url: str,
) -> None:
    """request-start 后的未知崩溃必须保留一次写计数，恢复时禁止重放写请求。"""
    seed = _Seed()
    payload_hash = await _seed_action(database_url, seed)
    adapter = _CrashAfterRequestStartAdapter(database_url, seed.task_id)
    workflow = _workflow(database_url, adapter)
    try:
        await workflow.claim(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
        with pytest.raises(RuntimeError, match="synthetic crash"):
            await workflow.execute_or_reconcile(
                task_id=seed.task_id,
                approval_id=seed.approval_id,
                operation_id=seed.operation_id,
                expected_payload_hash=payload_hash,
                lease_owner=seed.owner,
            )

        adapter.fail_execute = False
        await workflow.execute_or_reconcile(
            task_id=seed.task_id,
            approval_id=seed.approval_id,
            operation_id=seed.operation_id,
            expected_payload_hash=payload_hash,
            lease_owner=seed.owner,
        )
    finally:
        await workflow.dispose()

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            execution = await session.scalar(
                select(ToolExecutionModel).where(ToolExecutionModel.task_id == seed.task_id)
            )
    finally:
        await session_factory.dispose()
    assert execution is not None
    assert execution.status == ToolExecutionStatus.SUCCEEDED.value
    assert execution.write_attempt_count == 1
    assert adapter.write_calls == 1
    assert adapter.reconcile_calls == 1
