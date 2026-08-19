"""在真实 PostgreSQL 上验证版本、命令、审批与任务的原子冻结协议。"""

import asyncio
import importlib
from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from types import ModuleType
from typing import Literal
from uuid import UUID, uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.use_cases.approvals import (
    ApprovalDecisionUseCase,
    ExpireApprovalsUseCase,
)
from ai_employee.application.use_cases.calendar_proposals import CalendarProposalContent
from ai_employee.application.use_cases.mail_drafts import (
    MailDraftUseCase,
    UpdateMailDraftInput,
)
from ai_employee.application.use_cases.task_views import CancelTaskUseCase
from ai_employee.domain.actions import CalendarProposalStatus, MailDraftStatus
from ai_employee.domain.calendar_actions import NotificationPolicy
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.database_url import TestDatabaseUrl as ValidatedTestDatabaseUrl
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    MailDraftModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
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
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.repositories.task_views import SqlAlchemyTaskViewStore
from ai_employee.infrastructure.db.repositories.trusted_actions import (
    SqlAlchemyTrustedActionRepository,
)
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher
from ai_employee.integrations.registry import ProviderAdapterRegistry

USER_ID = UUID("00000000-0000-0000-0000-000000000101")
GOOGLE_CONNECTION_ID = UUID("00000000-0000-0000-0000-000000000201")
CALENDAR_ID = "synthetic-calendar"
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


async def _create_additional_mail_draft(database_url: str, *, idempotency_key: str) -> UUID:
    """在既有合成用户与连接下创建第二个独立编辑态草稿。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            repository = SqlAlchemyMailDraftRepository(session, ACTION_CIPHER)
            draft = await MailDraftUseCase(
                drafts=repository,
                connections=repository,
                clock=lambda: NOW,
            ).create_new(
                user_id=USER_ID,
                connection_id=GOOGLE_CONNECTION_ID,
                idempotency_key=idempotency_key,
                to=("second-recipient@example.test",),
                subject="Second synthetic subject",
                body_text="Second synthetic body",
            )
            return draft.draft_id
    finally:
        await session_factory.dispose()


async def _seed_calendar_proposal(
    database_url: str,
    *,
    operation_kind: Literal["create", "update", "restore"],
    base_etag: str = 'W/"etag-current"',
    current_etag: str = 'W/"etag-current"',
    write_capability_status: CapabilityStatus = CapabilityStatus.ENABLED,
    calendar_can_write: bool = True,
) -> UUID:
    """创建可提交的真实加密日程提案，并可注入提交前授权或 ETag 漂移。"""
    session_factory = build_session_factory(database_url)
    proposal_id = uuid4()
    desired_snapshot_id = uuid4()
    target_event_id = None if operation_kind == "create" else "synthetic-provider-event"
    attendees = () if operation_kind == "create" else ("attendee@example.test",)
    notification_policy = (
        NotificationPolicy.NONE if operation_kind == "create" else NotificationPolicy.ALL
    )
    changed_fields = () if operation_kind == "create" else ("title",)
    content = CalendarProposalContent(
        operation_id=uuid4(),
        title=f"Synthetic {operation_kind} meeting",
        description="Synthetic calendar description",
        location="Synthetic calendar room",
        starts_at="2030-01-02T09:00:00Z",
        ends_at="2030-01-02T10:00:00Z",
        timezone="UTC",
        all_day=False,
        attendees=attendees,
        notification_policy=notification_policy,
        changed_fields=changed_fields,
        confirmed_fields=("calendar", "time", "attendees", "notification_policy"),
        required_confirmations=(),
        notification_policy_user_set=True,
        requires_explicit_confirmation=True,
    ).model_dump(mode="json")
    try:
        async with session_factory.begin() as session:
            session.add_all(
                (
                    UserModel(
                        id=USER_ID,
                        email="calendar-submit@example.test",
                        display_name="Calendar Submit",
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
                        account_email="calendar-owner@example.test",
                        scopes=[],
                        status="connected",
                    ),
                )
            )
            await session.flush()
            session.add_all(
                (
                    ConnectionCapabilityModel(
                        user_id=USER_ID,
                        connection_id=GOOGLE_CONNECTION_ID,
                        capability=ConnectionCapability.CALENDAR_READ.value,
                        status=CapabilityStatus.ENABLED.value,
                        actual_scopes=[],
                    ),
                    ConnectionCapabilityModel(
                        user_id=USER_ID,
                        connection_id=GOOGLE_CONNECTION_ID,
                        capability=ConnectionCapability.CALENDAR_WRITE.value,
                        status=write_capability_status.value,
                        actual_scopes=[],
                    ),
                    ProviderCalendarModel(
                        user_id=USER_ID,
                        connection_id=GOOGLE_CONNECTION_ID,
                        provider_calendar_id=CALENDAR_ID,
                        name="Synthetic calendar",
                        timezone="UTC",
                        is_primary=True,
                        access_role="owner",
                        can_write=calendar_can_write,
                        provider_url="https://calendar.example.test/synthetic",
                    ),
                )
            )
            if target_event_id is not None:
                session.add(
                    CalendarEventModel(
                        user_id=USER_ID,
                        connection_id=GOOGLE_CONNECTION_ID,
                        provider_event_id=target_event_id,
                        calendar_id=CALENDAR_ID,
                        title="Synthetic current event",
                        starts_at=datetime(2030, 1, 2, 9, tzinfo=UTC),
                        ends_at=datetime(2030, 1, 2, 10, tzinfo=UTC),
                        all_day=False,
                        transparency="opaque",
                        status="confirmed",
                        timezone="UTC",
                        recurring_event_id=None,
                        etag=current_etag,
                        organizer={"address": "owner@example.test"},
                        attendees=[],
                        access_role="owner",
                        can_edit=True,
                        provider_url="https://calendar.example.test/events/synthetic",
                    )
                )
            await session.flush()
            repository = SqlAlchemyCalendarProposalRepository(session, ACTION_CIPHER)
            await repository.create(
                proposal_id=proposal_id,
                snapshot_id=desired_snapshot_id,
                user_id=USER_ID,
                connection_id=GOOGLE_CONNECTION_ID,
                creation_idempotency_key=f"synthetic-calendar-{operation_kind}-{proposal_id}",
                creation_payload_hash="c" * 64,
                calendar_id=CALENDAR_ID,
                operation_kind=operation_kind,
                target_event_id=target_event_id,
                base_etag=None if operation_kind == "create" else base_etag,
                retain_until=NOW + timedelta(days=365),
                desired_state=content,
            )
            if operation_kind != "create":
                before = await repository.save_snapshot(
                    snapshot_id=uuid4(),
                    user_id=USER_ID,
                    proposal_id=proposal_id,
                    version=1,
                    snapshot_kind="before",
                    content=content,
                    retain_until=NOW + timedelta(days=365),
                )
                assert before is not None
        return proposal_id
    finally:
        await session_factory.dispose()


async def _submit_calendar_proposal(
    *,
    database_url: str,
    proposal_id: UUID,
    idempotency_key: str,
) -> tuple[UUID, UUID, UUID]:
    """经真实事务冻结一个日程提案，并返回 task、approval 与 operation 标识。"""
    ports, use_cases = _trusted_action_modules()
    session_factory = build_session_factory(database_url)

    class Preflight:
        provider = "google"

        def validate_for_approval(self, _command: object) -> object:
            """测试预检只证明固定 Google 日程命令可无损表达。"""
            return ports.ApprovalPreflightResult()

    try:
        repository_module = importlib.import_module(
            "ai_employee.infrastructure.db.repositories.trusted_actions"
        )
        result = await use_cases.SubmitCalendarProposalUseCase(
            transactions=repository_module.SqlAlchemyTrustedActionRepositoryFactory(
                session_factory, ACTION_CIPHER
            ),
            preflights=ProviderAdapterRegistry(google_calendar_preflight=Preflight()),
            write_policy=_EnabledWritePolicy(),
            command_cipher=ACTION_CIPHER,
        ).execute(
            user_id=USER_ID,
            proposal_id=proposal_id,
            expected_version=1,
            idempotency_key=idempotency_key,
            now=NOW,
        )
        return result.task_id, result.approval_id, result.operation_id
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

        snapshot = await CancelTaskUseCase(SqlAlchemyTaskViewStore(session_factory)).execute(
            task_id=result.task_id,
            user_id=USER_ID,
            now=NOW,
        )

        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, result.approval_id)
            draft = await session.get(MailDraftModel, draft_id)
            tool_count = await session.scalar(select(func.count()).select_from(ToolExecutionModel))
            outbox_events = tuple(
                (
                    await session.scalars(
                        select(OutboxEventModel).where(
                            OutboxEventModel.aggregate_id == result.task_id
                        )
                    )
                ).all()
            )
        assert snapshot is not None and snapshot.status is TaskStatus.CANCELLED
        assert approval is not None and approval.status == ApprovalStatus.INVALIDATED.value
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert tool_count == 0
        assert {event.topic for event in outbox_events} >= {
            "approval.invalidated",
            "task.cancelled",
        }

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


@pytest.mark.parametrize(
    ("task_status", "lease_owner"),
    (
        (TaskStatus.QUEUED, None),
        (TaskStatus.RUNNING, "trusted-worker"),
    ),
)
async def test_queued_or_running_trusted_task_cancel_is_rejected_without_mutation(
    database_url: str,
    task_status: TaskStatus,
    lease_owner: str | None,
) -> None:
    """仅等待审批态可撤回，queued/running trusted task 必须保持原事实。"""
    draft_id = await _seed_mail_draft(database_url)
    task_id, approval_id, _ = await _submit_mail_draft(
        database_url=database_url,
        draft_id=draft_id,
        expected_version=1,
        idempotency_key=f"synthetic-{task_status.value}-cancel",
        now=NOW,
    )
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            task = await session.get(TaskRunModel, task_id, with_for_update=True)
            assert task is not None
            task.status = task_status.value
            task.lease_owner = lease_owner

        with pytest.raises(StateConflictError) as conflict:
            await CancelTaskUseCase(SqlAlchemyTaskViewStore(session_factory)).execute(
                task_id=task_id,
                user_id=USER_ID,
                now=NOW,
            )
        assert conflict.value.error_code == "task_state_conflict"

        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            approval = await session.get(ApprovalRequestModel, approval_id)
            draft = await session.get(MailDraftModel, draft_id)
            events = tuple(
                (
                    await session.scalars(
                        select(AuditEventModel).where(AuditEventModel.task_id == task_id)
                    )
                ).all()
            )
        assert task is not None and task.status == task_status.value
        assert task.lease_owner == lease_owner
        assert approval is not None and approval.status == ApprovalStatus.PENDING.value
        assert draft is not None and draft.status == MailDraftStatus.AWAITING_APPROVAL.value
        assert [event.event_type for event in events] == ["approval.requested"]
    finally:
        await session_factory.dispose()


async def test_queued_m2_expiry_retires_task_and_initial_outbox(
    database_url: str,
) -> None:
    """queued M2 审批到期必须终止任务，并使初始执行事件不可再投递。"""
    draft_id = await _seed_mail_draft(database_url)
    task_id, approval_id, _ = await _submit_mail_draft(
        database_url=database_url,
        draft_id=draft_id,
        expected_version=1,
        idempotency_key="synthetic-queued-expiry",
        now=NOW,
    )
    session_factory = build_session_factory(database_url)
    try:
        expired = await ExpireApprovalsUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
            now=NOW + timedelta(minutes=11),
            limit=10,
        )
        assert expired == 1

        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            approval = await session.get(ApprovalRequestModel, approval_id)
            draft = await session.get(MailDraftModel, draft_id)
            initial = await session.scalar(
                select(OutboxEventModel).where(
                    OutboxEventModel.deduplication_key == f"task.execute:{task_id}:initial"
                )
            )
            outbox_events = tuple(
                (
                    await session.scalars(
                        select(OutboxEventModel).where(
                            OutboxEventModel.aggregate_id == task_id
                        )
                    )
                ).all()
            )
        assert task is not None and task.status == TaskStatus.CANCELLED.value
        assert task.error_code == "approval_expired"
        assert approval is not None and approval.status == ApprovalStatus.EXPIRED.value
        assert draft is not None and draft.status == MailDraftStatus.EDITING.value
        assert initial is not None and initial.published_at is not None
        assert {event.topic for event in outbox_events} >= {
            "approval.expired",
            "task.cancelled",
        }
    finally:
        await session_factory.dispose()


async def test_concurrent_same_key_same_draft_reuses_one_submission(
    database_url: str,
) -> None:
    """两个并发同载荷请求必须只冻结一组事实，并向双方返回同一结果。"""
    draft_id = await _seed_mail_draft(database_url)
    start = asyncio.Event()

    async def submit() -> tuple[UUID, UUID, UUID]:
        """等待共同起点后使用独立事务提交同一草稿版本。"""
        await start.wait()
        return await _submit_mail_draft(
            database_url=database_url,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key="synthetic-concurrent-same",
            now=NOW,
        )

    first_task = asyncio.create_task(submit())
    second_task = asyncio.create_task(submit())
    start.set()
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)

    assert all(not isinstance(result, BaseException) for result in results)
    assert results[0] == results[1]
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            tasks = tuple(
                (
                    await session.scalars(
                        select(TaskRunModel).where(
                            TaskRunModel.user_id == USER_ID,
                            TaskRunModel.idempotency_key == "synthetic-concurrent-same",
                        )
                    )
                ).all()
            )
            approval_count = await session.scalar(
                select(func.count())
                .select_from(ApprovalRequestModel)
                .where(ApprovalRequestModel.task_id == tasks[0].id)
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(OutboxEventModel.aggregate_id == tasks[0].id)
            )
        assert len(tasks) == 1
        assert approval_count == 1
        assert outbox_count == 1
    finally:
        await session_factory.dispose()


async def test_concurrent_same_key_different_drafts_returns_stable_mismatch(
    database_url: str,
) -> None:
    """同键并发绑定不同草稿时只允许一个赢家，输家不得泄漏数据库异常。"""
    first_draft_id = await _seed_mail_draft(database_url)
    second_draft_id = await _create_additional_mail_draft(
        database_url,
        idempotency_key="synthetic-second-draft",
    )
    start = asyncio.Event()

    async def submit(draft_id: UUID) -> tuple[UUID, UUID, UUID]:
        """从共同起点以独立事务竞争同一用户幂等键。"""
        await start.wait()
        return await _submit_mail_draft(
            database_url=database_url,
            draft_id=draft_id,
            expected_version=1,
            idempotency_key="synthetic-concurrent-mismatch",
            now=NOW,
        )

    first_task = asyncio.create_task(submit(first_draft_id))
    second_task = asyncio.create_task(submit(second_draft_id))
    start.set()
    results = await asyncio.gather(first_task, second_task, return_exceptions=True)

    successes = [result for result in results if not isinstance(result, BaseException)]
    conflicts = [result for result in results if isinstance(result, StateConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].error_code == "idempotency_key_payload_mismatch"
    assert all(
        not isinstance(result, BaseException) or isinstance(result, StateConflictError)
        for result in results
    )

    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            tasks = tuple(
                (
                    await session.scalars(
                        select(TaskRunModel).where(
                            TaskRunModel.user_id == USER_ID,
                            TaskRunModel.idempotency_key == "synthetic-concurrent-mismatch",
                        )
                    )
                ).all()
            )
            approval_count = await session.scalar(
                select(func.count())
                .select_from(ApprovalRequestModel)
                .where(ApprovalRequestModel.task_id == tasks[0].id)
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEventModel)
                .where(OutboxEventModel.aggregate_id == tasks[0].id)
            )
        assert len(tasks) == 1
        assert approval_count == 1
        assert outbox_count == 1
    finally:
        await session_factory.dispose()


@pytest.mark.parametrize(
    ("operation_kind", "expected_action", "expected_schema", "expected_risk"),
    (
        ("create", "calendar.create", "calendar_create.v1", "medium"),
        ("update", "calendar.update", "calendar_update.v1", "high"),
        ("restore", "calendar.restore", "calendar_restore.v1", "high"),
    ),
)
async def test_calendar_submission_freezes_exact_encrypted_command(
    database_url: str,
    operation_kind: Literal["create", "update", "restore"],
    expected_action: str,
    expected_schema: str,
    expected_risk: str,
) -> None:
    """三种日程动作都必须绑定精确目标、风险与完整 AEAD 命令。"""
    proposal_id = await _seed_calendar_proposal(
        database_url,
        operation_kind=operation_kind,
    )
    task_id, approval_id, operation_id = await _submit_calendar_proposal(
        database_url=database_url,
        proposal_id=proposal_id,
        idempotency_key=f"synthetic-calendar-submit-{operation_kind}",
    )
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            approval = await session.get(ApprovalRequestModel, approval_id)
            proposal = await session.get(CalendarChangeProposalModel, proposal_id)
            command = await SqlAlchemyTrustedActionRepository(
                session,
                ACTION_CIPHER,
            ).load_command(user_id=USER_ID, approval_id=approval_id)
        assert approval is not None
        assert approval.action == expected_action
        assert approval.schema_version == expected_schema
        assert approval.risk_level == expected_risk
        assert approval.payload == {"storage": "encrypted", "schema_version": expected_schema}
        assert approval.payload_ciphertext is not None
        assert approval.payload_nonce is not None and len(approval.payload_nonce) == 12
        assert approval.payload_key_version == 1
        assert proposal is not None and proposal.status == (
            CalendarProposalStatus.AWAITING_APPROVAL.value
        )
        assert command is not None
        assert command["operation_id"] == str(operation_id)
        assert command["action"] == expected_action
        assert command["schema_version"] == expected_schema
        assert command["connection_id"] == str(GOOGLE_CONNECTION_ID)
        assert command["calendar_id"] == CALENDAR_ID
        assert task_id != approval_id
    finally:
        await session_factory.dispose()


@pytest.mark.parametrize(
    (
        "operation_kind",
        "base_etag",
        "current_etag",
        "write_capability_status",
        "calendar_can_write",
        "expected_error_code",
    ),
    (
        (
            "update",
            'W/"etag-old"',
            'W/"etag-current"',
            CapabilityStatus.ENABLED,
            True,
            "calendar_event_version_conflict",
        ),
        (
            "restore",
            'W/"etag-current"',
            'W/"etag-current"',
            CapabilityStatus.ENABLED,
            False,
            "connection_capability_disabled",
        ),
    ),
)
async def test_calendar_submission_drift_creates_zero_trusted_facts(
    database_url: str,
    operation_kind: Literal["update", "restore"],
    base_etag: str,
    current_etag: str,
    write_capability_status: CapabilityStatus,
    calendar_can_write: bool,
    expected_error_code: str,
) -> None:
    """ETag、能力或目录写权限漂移必须在创建 task/approval/outbox 前失败。"""
    proposal_id = await _seed_calendar_proposal(
        database_url,
        operation_kind=operation_kind,
        base_etag=base_etag,
        current_etag=current_etag,
        write_capability_status=write_capability_status,
        calendar_can_write=calendar_can_write,
    )
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory() as session:
            before = (
                await session.scalar(select(func.count()).select_from(TaskRunModel)),
                await session.scalar(select(func.count()).select_from(ApprovalRequestModel)),
                await session.scalar(select(func.count()).select_from(OutboxEventModel)),
            )

        with pytest.raises(StateConflictError) as raised:
            await _submit_calendar_proposal(
                database_url=database_url,
                proposal_id=proposal_id,
                idempotency_key=f"synthetic-calendar-drift-{expected_error_code}",
            )
        assert raised.value.error_code == expected_error_code

        async with session_factory() as session:
            after = (
                await session.scalar(select(func.count()).select_from(TaskRunModel)),
                await session.scalar(select(func.count()).select_from(ApprovalRequestModel)),
                await session.scalar(select(func.count()).select_from(OutboxEventModel)),
            )
            proposal = await session.get(CalendarChangeProposalModel, proposal_id)
        assert after == before
        assert proposal is not None
        assert proposal.status == CalendarProposalStatus.EDITING.value
    finally:
        await session_factory.dispose()


async def test_approval_decision_rechecks_capability_without_mutation(
    database_url: str,
) -> None:
    """冻结后 mail.send 能力丢失时，决定必须稳定拒绝且保留 pending 事实。"""
    draft_id = await _seed_mail_draft(database_url)
    task_id, approval_id, _ = await _submit_mail_draft(
        database_url=database_url,
        draft_id=draft_id,
        expected_version=1,
        idempotency_key="synthetic-decision-capability-loss",
        now=NOW,
    )
    await _mark_waiting_for_approval(database_url, task_id=task_id)
    await _persist_approval_interrupt(database_url, task_id=task_id)
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            capability = await session.scalar(
                select(ConnectionCapabilityModel)
                .where(
                    ConnectionCapabilityModel.user_id == USER_ID,
                    ConnectionCapabilityModel.connection_id == GOOGLE_CONNECTION_ID,
                    ConnectionCapabilityModel.capability == ConnectionCapability.MAIL_SEND.value,
                )
                .with_for_update()
            )
            approval = await session.get(ApprovalRequestModel, approval_id)
            assert capability is not None and approval is not None
            capability.status = CapabilityStatus.REVOKED.value
            payload_hash = approval.payload_hash

        with pytest.raises(StateConflictError) as raised:
            await ApprovalDecisionUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
                approval_id=approval_id,
                user_id=USER_ID,
                decision=ApprovalStatus.APPROVED.value,
                version=1,
                payload_hash=payload_hash,
                now=NOW + timedelta(minutes=1),
            )
        assert raised.value.error_code == "connection_capability_disabled"

        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            approval = await session.get(ApprovalRequestModel, approval_id)
            draft = await session.get(MailDraftModel, draft_id)
        assert task is not None and task.status == TaskStatus.WAITING_APPROVAL.value
        assert approval is not None and approval.status == ApprovalStatus.PENDING.value
        assert approval.decided_at is None
        assert draft is not None and draft.status == MailDraftStatus.AWAITING_APPROVAL.value
    finally:
        await session_factory.dispose()
