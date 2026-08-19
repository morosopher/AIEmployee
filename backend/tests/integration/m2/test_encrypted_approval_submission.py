"""在真实 PostgreSQL 上验证版本、命令、审批与任务的原子冻结协议。"""

import importlib
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from types import ModuleType
from uuid import UUID

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.use_cases.approvals import (
    ApprovalDecisionUseCase,
    ExpireApprovalsUseCase,
)
from ai_employee.application.use_cases.mail_drafts import (
    MailDraftUseCase,
    UpdateMailDraftInput,
)
from ai_employee.domain.actions import MailDraftStatus
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
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
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry

USER_ID = UUID("00000000-0000-0000-0000-000000000101")
GOOGLE_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000201")
NOW = datetime(2030, 1, 1, tzinfo=UTC)
ACTION_CIPHER = ActionPayloadCipher.from_key(b"t" * 32)

# 普通应用模块必须使用 Task 16A 提供的 regular head 数据库，并在异常时先释放 pool。
pytestmark = pytest.mark.usefixtures("cycle5_tracked_session_factories")


@pytest.fixture(scope="module", name="database_url")
def _cycle5_database_url(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> ValidatedTestDatabaseUrl:
    """复用已验证的 Cycle 5 regular head 数据库，不触碰 Task 13 anchor。"""
    return cycle5_regular_database_url


@pytest.fixture(scope="module", autouse=True, name="migrated_database")
def _cycle5_migrated_database(
    cycle5_regular_database_url: ValidatedTestDatabaseUrl,
) -> Iterator[None]:
    """覆盖全局迁移 fixture；regular helper 已完成 typed lifecycle 复核。"""
    del cycle5_regular_database_url
    yield


def _trusted_action_modules() -> tuple[ModuleType, ModuleType]:
    """在测试执行期加载新端口和用例，确保缺实现不是 pytest 收集错误。"""
    return (
        importlib.import_module("ai_employee.application.ports.trusted_actions"),
        importlib.import_module("ai_employee.application.use_cases.trusted_actions"),
    )


class _EnabledWritePolicy:
    """只允许固定 Google 合成连接通过审批前门禁。"""

    def provider_writes_enabled(self, provider: str) -> bool:
        """全局与 Google provider 开关均视为开启。"""
        return provider == "google"

    def write_account_allowed(self, provider_identity_key: str) -> bool:
        """精确匹配合成 provider identity，不允许账户回退。"""
        return provider_identity_key == "google::synthetic-account"


async def _seed_mail_draft(database_url: str) -> UUID:
    """创建用户、连接、读写能力和一个纯本地加密草稿版本。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            session.add_all(
                (
                    UserModel(
                        id=USER_ID,
                        email="trusted-submit@example.test",
                        display_name="Trusted Submit",
                        password_hash=None,
                        timezone="UTC",
                        locale="en-US",
                        brief_time=time(8),
                    ),
                    OAuthConnectionModel(
                        id=GOOGLE_CONNECTION_ID,
                        user_id=USER_ID,
                        provider="google",
                        provider_account_id="synthetic-account",
                        provider_tenant_id="",
                        account_type="google",
                        account_email="owner@example.test",
                        scopes=[],
                        status="connected",
                    ),
                    ConnectionCapabilityModel(
                        user_id=USER_ID,
                        connection_id=GOOGLE_CONNECTION_ID,
                        capability=ConnectionCapability.MAIL_READ.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=[],
                    ),
                    ConnectionCapabilityModel(
                        user_id=USER_ID,
                        connection_id=GOOGLE_CONNECTION_ID,
                        capability=ConnectionCapability.MAIL_SEND.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=[],
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
                user_id=USER_ID,
                connection_id=GOOGLE_CONNECTION_ID,
                idempotency_key="synthetic-draft-create",
                to=("recipient@example.test",),
                subject="Synthetic subject",
                body_text="Synthetic body",
            )
            return draft.draft_id
    finally:
        await session_factory.dispose()


async def _submit_mail_draft(
    *,
    database_url: str,
    draft_id: UUID,
    expected_version: int,
    idempotency_key: str,
    now: datetime,
) -> tuple[UUID, UUID, UUID]:
    """冻结一个既有草稿版本并返回 task、approval 与 operation 标识。"""
    ports, use_cases = _trusted_action_modules()
    session_factory = build_session_factory(database_url)

    class Preflight:
        provider = "google"

        def validate_for_approval(self, _command: object) -> object:
            """测试仅确认固定 Google mail.send 路径可无损表达。"""
            return ports.ApprovalPreflightResult()

    try:
        repository_module = importlib.import_module(
            "ai_employee.infrastructure.db.repositories.trusted_actions"
        )
        result = await use_cases.SubmitMailDraftUseCase(
            transactions=repository_module.SqlAlchemyTrustedActionRepositoryFactory(
                session_factory, ACTION_CIPHER
            ),
            preflights=ProviderAdapterRegistry(google_mail_preflight=Preflight()),
            write_policy=_EnabledWritePolicy(),
            command_cipher=ACTION_CIPHER,
        ).execute(
            user_id=USER_ID,
            draft_id=draft_id,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            now=now,
        )
        return result.task_id, result.approval_id, result.operation_id
    finally:
        await session_factory.dispose()


async def _mark_waiting_for_approval(database_url: str, *, task_id: UUID) -> None:
    """模拟 Task 19 前的 Worker 交接，仅推进现有 trusted task 到等待审批态。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            task = await session.get(TaskRunModel, task_id, with_for_update=True)
            assert task is not None
            task.status = TaskStatus.WAITING_APPROVAL.value
            task.graph_thread_id = str(task_id)
    finally:
        await session_factory.dispose()


async def _persist_approval_interrupt(database_url: str, *, task_id: UUID) -> None:
    """用最小 LangGraph 把真实 ``__interrupt__`` checkpoint 写入 PostgreSQL。"""

    async def approval_gate(_state: dict[str, object]) -> dict[str, object]:
        """在测试图唯一节点暂停；恢复执行不属于 Task 18 覆盖范围。"""
        from langgraph.types import interrupt

        interrupt("synthetic trusted approval")
        return {}

    graph_builder = StateGraph(dict)
    graph_builder.add_node("approval_gate", approval_gate)
    graph_builder.add_edge(START, "approval_gate")
    graph_builder.add_edge("approval_gate", END)
    async with postgres_checkpointer(database_url) as saver:
        result = await graph_builder.compile(checkpointer=saver).ainvoke(
            {},
            config={"configurable": {"thread_id": str(task_id)}},
        )
    assert "__interrupt__" in result


async def _save_next_mail_version(
    database_url: str,
    *,
    draft_id: UUID,
    expected_version: int,
    now: datetime,
) -> int:
    """在审批终态后显式保存下一不可变本地版本。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, ACTION_CIPHER)
            saved = await MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: now,
            ).update(
                UpdateMailDraftInput(
                    user_id=USER_ID,
                    draft_id=draft_id,
                    expected_version=expected_version,
                    body_text=f"Synthetic body version {expected_version + 1}",
                )
            )
        return saved.current_version
    finally:
        await session_factory.dispose()


async def test_submit_mail_draft_freezes_one_encrypted_command(database_url: str) -> None:
    """一个本地版本必须与 task/step/approval/audit/outbox 在同一事务冻结。"""
    ports, use_cases = _trusted_action_modules()
    draft_id = await _seed_mail_draft(database_url)
    session_factory = build_session_factory(database_url)

    class Preflight:
        provider = "google"

        def validate_for_approval(self, command: object) -> object:
            """纯验证精确 mail.send 命令，不调用供应商。"""
            assert command.action == "mail.send"  # type: ignore[attr-defined]
            return ports.ApprovalPreflightResult()

    try:
        registry = ProviderAdapterRegistry(google_mail_preflight=Preflight())
        transaction_factory = importlib.import_module(
            "ai_employee.infrastructure.db.repositories.trusted_actions"
        ).SqlAlchemyTrustedActionRepositoryFactory(session_factory, ACTION_CIPHER)
        submit_mail = use_cases.SubmitMailDraftUseCase(
            transactions=transaction_factory,
            preflights=registry,
            write_policy=_EnabledWritePolicy(),
            command_cipher=ACTION_CIPHER,
        )

        result = await submit_mail.execute(
            user_id=USER_ID,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key="synthetic-submit-1",
            now=NOW,
        )

        async with session_factory() as session:
            task = await session.get(TaskRunModel, result.task_id)
            approval = await session.get(ApprovalRequestModel, result.approval_id)
            step_count = await session.scalar(
                select(func.count()).select_from(TaskStepModel).where(
                    TaskStepModel.task_id == result.task_id
                )
            )
            approval_count = await session.scalar(
                select(func.count()).select_from(ApprovalRequestModel).where(
                    ApprovalRequestModel.task_id == result.task_id
                )
            )
            outbox = await session.scalar(
                select(OutboxEventModel).where(OutboxEventModel.aggregate_id == result.task_id)
            )
            draft = await session.get(MailDraftModel, draft_id)
            audit_events = tuple(
                (
                    await session.scalars(
                        select(AuditEventModel)
                        .where(AuditEventModel.task_id == result.task_id)
                        .order_by(AuditEventModel.id)
                    )
                ).all()
            )
            command = await SqlAlchemyTrustedActionRepository(
                session, ACTION_CIPHER
            ).load_command(user_id=USER_ID, approval_id=result.approval_id)

        assert task is not None and task.status == TaskStatus.QUEUED.value
        assert task.input_payload == {
            "approval_id": str(result.approval_id),
            "operation_id": str(result.operation_id),
        }
        assert approval is not None
        assert approval.action == "mail.send"
        assert approval.risk_level == "high"
        assert approval.expires_at == NOW + timedelta(minutes=10)
        assert approval.payload == {
            "storage": "encrypted",
            "schema_version": "mail_send.v1",
        }
        assert approval.payload_ciphertext is not None
        assert approval.payload_nonce is not None
        assert approval.payload_key_version == 1
        assert approval.status == ApprovalStatus.PENDING.value
        assert step_count == 1
        assert approval_count == 1
        assert outbox is not None
        assert outbox.payload == {"task_id": str(result.task_id)}
        assert draft is not None and draft.status == MailDraftStatus.AWAITING_APPROVAL.value
        assert command is not None
        assert command["action"] == "mail.send"
        assert command["connection_id"] == str(GOOGLE_CONNECTION_ID)
        assert command["draft_id"] == str(draft_id)
        assert command["draft_version"] == 1
        assert command["message_date"] == "2030-01-01T00:00:00Z"
        assert all(
            event.event_metadata.keys()
            <= {
                "action",
                "approval_id",
                "proposal_id",
                "proposal_kind",
                "proposal_version",
                "risk_level",
                "status",
                "version",
                "warnings",
            }
            for event in audit_events
        )
        assert await session_factory.dispose() is None
    finally:
        await session_factory.dispose()


async def test_approved_trusted_action_is_hash_bound_single_use_with_five_minute_deadline(
    database_url: str,
) -> None:
    """真实审批必须先校验哈希，批准后只消费一次并设置五分钟执行期限。"""
    draft_id = await _seed_mail_draft(database_url)
    task_id, approval_id, _ = await _submit_mail_draft(
        database_url=database_url,
        draft_id=draft_id,
        expected_version=1,
        idempotency_key="synthetic-approved-submit",
        now=NOW,
    )
    await _mark_waiting_for_approval(database_url, task_id=task_id)
    await _persist_approval_interrupt(database_url, task_id=task_id)
    session_factory = build_session_factory(database_url)
    decision_now = NOW + timedelta(minutes=1)
    try:
        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, approval_id)
        assert approval is not None
        payload_hash = approval.payload_hash

        with pytest.raises(StateConflictError) as mismatch:
            await ApprovalDecisionUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
                approval_id=approval_id,
                user_id=USER_ID,
                decision=ApprovalStatus.APPROVED.value,
                version=1,
                payload_hash="f" * 64,
                now=decision_now,
            )
        assert mismatch.value.error_code == "approval_conflict"

        decision = ApprovalDecisionUseCase(SqlAlchemyApprovalStore(session_factory))
        await decision.execute(
            approval_id=approval_id,
            user_id=USER_ID,
            decision=ApprovalStatus.APPROVED.value,
            version=1,
            payload_hash=payload_hash,
            now=decision_now,
        )
        with pytest.raises(StateConflictError) as duplicate:
            await decision.execute(
                approval_id=approval_id,
                user_id=USER_ID,
                decision=ApprovalStatus.APPROVED.value,
                version=1,
                payload_hash=payload_hash,
                now=decision_now,
            )
        assert duplicate.value.error_code == "approval_conflict"

        async with session_factory() as session:
            persisted = await session.get(ApprovalRequestModel, approval_id)
        assert persisted is not None
        assert persisted.status == ApprovalStatus.APPROVED.value
        assert persisted.approved_execution_deadline_at == decision_now + timedelta(minutes=5)
    finally:
        await session_factory.dispose()


async def test_rejection_returns_draft_to_editing_and_consumes_frozen_version(
    database_url: str,
) -> None:
    """拒绝后旧版本永久不可重提，保存下一版本后才能创建新审批。"""
    draft_id = await _seed_mail_draft(database_url)
    task_id, approval_id, _ = await _submit_mail_draft(
        database_url=database_url,
        draft_id=draft_id,
        expected_version=1,
        idempotency_key="synthetic-rejected-submit",
        now=NOW,
    )
    await _mark_waiting_for_approval(database_url, task_id=task_id)
    await _persist_approval_interrupt(database_url, task_id=task_id)
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, approval_id)
        assert approval is not None
        await ApprovalDecisionUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
            approval_id=approval_id,
            user_id=USER_ID,
            decision=ApprovalStatus.REJECTED.value,
            version=1,
            payload_hash=approval.payload_hash,
            now=NOW + timedelta(minutes=1),
        )
        async with session_factory() as session:
            rejected = await session.get(ApprovalRequestModel, approval_id)
            draft = await session.get(MailDraftModel, draft_id)
        assert rejected is not None and rejected.status == ApprovalStatus.REJECTED.value
        assert rejected.approved_execution_deadline_at is None
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value

        with pytest.raises(StateConflictError) as consumed:
            await _submit_mail_draft(
                database_url=database_url,
                draft_id=draft_id,
                expected_version=1,
                idempotency_key="synthetic-rejected-resubmit-old",
                now=NOW + timedelta(minutes=2),
            )
        assert consumed.value.error_code == "draft_version_conflict"
        assert await _save_next_mail_version(
            database_url,
            draft_id=draft_id,
            expected_version=1,
            now=NOW + timedelta(minutes=2),
        ) == 2
        replacement_task_id, replacement_approval_id, _ = await _submit_mail_draft(
            database_url=database_url,
            draft_id=draft_id,
            expected_version=2,
            idempotency_key="synthetic-rejected-resubmit-new",
            now=NOW + timedelta(minutes=3),
        )
        assert replacement_task_id != task_id
        assert replacement_approval_id != approval_id
    finally:
        await session_factory.dispose()


async def test_expiry_returns_draft_to_editing_and_consumes_frozen_version(
    database_url: str,
) -> None:
    """到期扫描恢复编辑态，但同一旧版本仍不能用新幂等键绕过消费事实。"""
    draft_id = await _seed_mail_draft(database_url)
    task_id, approval_id, _ = await _submit_mail_draft(
        database_url=database_url,
        draft_id=draft_id,
        expected_version=1,
        idempotency_key="synthetic-expired-submit",
        now=NOW,
    )
    await _mark_waiting_for_approval(database_url, task_id=task_id)
    session_factory = build_session_factory(database_url)
    try:
        expired = await ExpireApprovalsUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
            now=NOW + timedelta(minutes=11),
            limit=10,
        )
        assert expired == 1
        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, approval_id)
            draft = await session.get(MailDraftModel, draft_id)
        assert approval is not None and approval.status == ApprovalStatus.EXPIRED.value
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value

        with pytest.raises(StateConflictError) as consumed:
            await _submit_mail_draft(
                database_url=database_url,
                draft_id=draft_id,
                expected_version=1,
                idempotency_key="synthetic-expired-resubmit-old",
                now=NOW + timedelta(minutes=12),
            )
        assert consumed.value.error_code == "draft_version_conflict"
        assert await _save_next_mail_version(
            database_url,
            draft_id=draft_id,
            expected_version=1,
            now=NOW + timedelta(minutes=12),
        ) == 2
        replacement_task_id, replacement_approval_id, _ = await _submit_mail_draft(
            database_url=database_url,
            draft_id=draft_id,
            expected_version=2,
            idempotency_key="synthetic-expired-resubmit-new",
            now=NOW + timedelta(minutes=13),
        )
        assert replacement_task_id != task_id
        assert replacement_approval_id != approval_id
    finally:
        await session_factory.dispose()


async def test_cancel_waiting_trusted_task_invalidates_without_tool_execution(
    database_url: str,
) -> None:
    """唯一 task cancel 撤回路径必须让审批失效、草稿可编辑且零工具认领。"""
    ports, use_cases = _trusted_action_modules()
    draft_id = await _seed_mail_draft(database_url)
    session_factory = build_session_factory(database_url)

    class Preflight:
        provider = "google"

        def validate_for_approval(self, _command: object) -> object:
            return ports.ApprovalPreflightResult()

    try:
        repository_module = importlib.import_module(
            "ai_employee.infrastructure.db.repositories.trusted_actions"
        )
        result = await use_cases.SubmitMailDraftUseCase(
            transactions=repository_module.SqlAlchemyTrustedActionRepositoryFactory(
                session_factory, ACTION_CIPHER
            ),
            preflights=ProviderAdapterRegistry(google_mail_preflight=Preflight()),
            write_policy=_EnabledWritePolicy(),
            command_cipher=ACTION_CIPHER,
        ).execute(
            user_id=USER_ID,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key="synthetic-cancel-submit",
            now=NOW,
        )
        async with session_factory.begin() as session:
            task = await session.get(TaskRunModel, result.task_id, with_for_update=True)
            assert task is not None
            task.status = TaskStatus.WAITING_APPROVAL.value

        from ai_employee.application.use_cases.task_views import CancelTaskUseCase
        from ai_employee.infrastructure.db.repositories.task_views import SqlAlchemyTaskViewStore

        snapshot = await CancelTaskUseCase(SqlAlchemyTaskViewStore(session_factory)).execute(
            task_id=result.task_id,
            user_id=USER_ID,
            now=NOW,
        )

        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, result.approval_id)
            draft = await session.get(MailDraftModel, draft_id)
            tool_count = await session.scalar(select(func.count()).select_from(ToolExecutionModel))
        assert snapshot is not None and snapshot.status is TaskStatus.CANCELLED
        assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert tool_count == 0

        with pytest.raises(StateConflictError) as invalidated:
            await ApprovalDecisionUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
                approval_id=result.approval_id,
                user_id=USER_ID,
                decision=ApprovalStatus.APPROVED.value,
                version=1,
                payload_hash=approval.payload_hash,
                now=NOW + timedelta(minutes=1),
            )
        assert invalidated.value.error_code == "approval_invalidated_by_edit"
        with pytest.raises(StateConflictError) as consumed:
            await _submit_mail_draft(
                database_url=database_url,
                draft_id=draft_id,
                expected_version=1,
                idempotency_key="synthetic-cancel-resubmit-old",
                now=NOW + timedelta(minutes=2),
            )
        assert consumed.value.error_code == "draft_version_conflict"
        assert await _save_next_mail_version(
            database_url,
            draft_id=draft_id,
            expected_version=1,
            now=NOW + timedelta(minutes=2),
        ) == 2
        replacement_task_id, replacement_approval_id, _ = await _submit_mail_draft(
            database_url=database_url,
            draft_id=draft_id,
            expected_version=2,
            idempotency_key="synthetic-cancel-resubmit-new",
            now=NOW + timedelta(minutes=3),
        )
        assert replacement_task_id != result.task_id
        assert replacement_approval_id != result.approval_id
    finally:
        await session_factory.dispose()
