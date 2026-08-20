"""在真实 PostgreSQL 上验证 ToolExecution 原子认领与请求开始幂等边界。"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from ai_employee.application.commands import parse_trusted_command, trusted_command_hash
from ai_employee.application.ports.trusted_actions import (
    ApprovalPreflightResult,
    ExecutionReference,
    ProviderWriteOutcome,
)
from ai_employee.application.use_cases.trusted_actions import TrustedActionExecutionUseCase
from ai_employee.config import Settings
from ai_employee.domain.actions import (
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
)
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError, TransientProviderError
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
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepositoryFactory,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
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

    provider = "google"

    def __init__(self, outcome: ProviderWriteOutcome | None = None) -> None:
        """初始化计数器；默认返回 confirmed applied。"""
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
        self.provider_account_id = "synthetic-account"

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
                    is_active=True,
                )
            )
            # 这些 ORM 模型刻意没有关系映射；先显式写入父用户，避免 flush 无法从裸 FK
            # 推导 User→Connection/Draft/Task 的插入顺序。所有阶段仍处于同一事务。
            await session.flush()
            session.add(
                OAuthConnectionModel(
                    id=seed.connection_id,
                    user_id=seed.user_id,
                    provider="google",
                    provider_account_id=seed.provider_account_id,
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
                    status=TaskStatus.RUNNING.value,
                    idempotency_key=f"task:{seed.task_id}",
                    input_payload={
                        "approval_id": str(seed.approval_id),
                        "operation_id": str(seed.operation_id),
                    },
                    graph_thread_id=str(seed.task_id),
                    lease_owner=seed.owner,
                    lease_expires_at=lease_expires_at or NOW + timedelta(minutes=1),
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
                        provider="google",
                        status=existing_execution_status.value,
                        claimed_at=NOW - timedelta(minutes=10),
                        request_started_at=request_started_at,
                        write_attempt_count=1 if request_started_at else 0,
                    )
                )
    finally:
        await session_factory.dispose()
    return payload_hash


def _settings(*, account: str = "synthetic-account", enabled: bool = True) -> Settings:
    """构造只允许一个规范 Google 合成身份的非生产真实写门禁。"""
    return Settings(
        _env_file=None,
        app_env="staging",
        external_writes_enabled=enabled,
        google_writes_enabled=enabled,
        microsoft_writes_enabled=False,
        write_test_account_allowlist=[f"google::{account}"],
    )


def _workflow(
    database_url: str,
    adapter: _RecordingAdapter,
    *,
    settings: Settings | None = None,
) -> TrustedActionExecutionUseCase:
    """组合真实 Repository、固定 registry 与确定性时钟。"""
    session_factory = build_session_factory(database_url)
    return TrustedActionExecutionUseCase(
        transactions=SqlAlchemyTrustedActionRepositoryFactory(session_factory, ACTION_CIPHER),
        adapters=ProviderAdapterRegistry(google_mail_action=adapter),
        write_policy=settings or _settings(),
        clock=lambda: NOW,
        dispose=session_factory.dispose,
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
    payload_hash = await _seed_action(
        database_url,
        seed,
        deadline=NOW - timedelta(microseconds=1),
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
async def test_stale_claim_before_request_is_recovered_with_one_write(database_url: str) -> None:
    """已按时认领但未提交 request-start 的过期租约可复用原 claim 首次写入。"""
    seed = _Seed()
    seed.owner = "recovery-worker"
    payload_hash = await _seed_action(
        database_url,
        seed,
        deadline=NOW - timedelta(minutes=5),
        existing_execution_status=ToolExecutionStatus.CLAIMED,
        request_started_at=None,
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    adapter = _RecordingAdapter()

    await _run_approved(_workflow(database_url, adapter), seed, payload_hash)

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
    workflow = _workflow(database_url, adapter, settings=_settings(enabled=enabled))
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
