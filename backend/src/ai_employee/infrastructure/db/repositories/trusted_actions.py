"""在 ApprovalRequest 上持久化严格验证且记录绑定的加密可信命令。"""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.commands import canonical_command_json, trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.trusted_actions import (
    CalendarProposalSubmissionSnapshot,
    ExecutionReference,
    ExistingTrustedActionSubmission,
    MailDraftSubmissionSnapshot,
    ProviderWriteOutcome,
    RequestStartAuthorizer,
    RequestStartDisposition,
    RequestStartResult,
    TrustedActionDispatchSnapshot,
    TrustedActionExecutionSnapshot,
    TrustedActionRequestStartAuthorization,
    TrustedActionSubmission,
    durable_retry_summary_is_valid,
    trusted_execution_binding_matches,
)
from ai_employee.application.use_cases.calendar_proposals import CalendarProposalContent
from ai_employee.application.use_cases.task_execution import utc_instant
from ai_employee.application.use_cases.trusted_actions import (
    MAX_RECONCILIATION_ATTEMPTS,
    reconciliation_delay,
    validate_provider_url,
)
from ai_employee.domain.actions import (
    CalendarProposalStatus,
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
)
from ai_employee.domain.connections import (
    CapabilityStatus,
    ConnectionCapability,
    canonical_provider_identity_key,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailMode, ReplyThreadHeaders
from ai_employee.domain.tasks import (
    ApprovalProposal,
    ApprovalStatus,
    JsonValue,
    TaskStatus,
)
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    MailDraftModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EmailMessageModel,
    EmailThreadModel,
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
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.repositories.mail_drafts import SqlAlchemyMailDraftRepository
from ai_employee.infrastructure.db.repositories.task_execution import (
    SqlAlchemyTaskExecutionStore,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher

APPROVAL_COMMAND_CONTENT_KIND = "approval_command"


class SqlAlchemyTrustedActionRepository:
    """在调用方事务内写入和读取 ApprovalRequest 的真实 M2 命令。

    M2 路径只允许四种严格命令进入 AEAD 列，JSONB ``payload`` 始终只保存无敏感
    marker。读取按 ``schema_version`` 显式分支：``None`` 只能解释为 M1
    ``fake.write``，任何非空版本都必须具有完整 AEAD 三元组并重新通过严格命令及
    规范哈希验证。Repository 从不提交，也不会把内容异常转换成供应商错误。
    """

    def __init__(self, session: AsyncSession, cipher: ActionPayloadCipher) -> None:
        """绑定应用事务会话和 ApprovalRequest 记录级加密器。

        Args:
            session: 由上层用例负责提交或回滚的异步会话。
            cipher: 使用用户、Approval ID、动作和 Schema 构造 AAD 的加密器。
        """
        self._session = session
        self._cipher = cipher

    async def find_existing_submission(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> ExistingTrustedActionSubmission | None:
        """按用户提交键读取既有 trusted task 及其唯一审批绑定。

        事务级 advisory lock 以域分离 SHA-256 的用户与键摘要为输入，把同一精确幂等边界的查询、资源锁
        和冻结写入串行化。这样同资源请求不会在赢家改变本地状态后误报版本冲突，不同资源
        也不会同时越过空查询后把数据库唯一约束泄漏出领域边界。64 位哈希碰撞最多让无关
        键额外串行；随后仍按完整用户与键查询，因此不会错误复用其他任务。
        """
        digest = sha256(
            b"trusted-action-submission-idempotency-v1\0"
            + user_id.bytes
            + b"\0"
            + idempotency_key.encode("utf-8")
        ).digest()
        advisory_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:advisory_key)"),
            {"advisory_key": advisory_key},
        )
        task = await self._session.scalar(
            select(TaskRunModel).where(
                TaskRunModel.user_id == user_id,
                TaskRunModel.idempotency_key == idempotency_key,
            )
        )
        if task is None:
            return None
        if task.kind != "trusted_action":
            raise StateConflictError(
                error_code="idempotency_key_payload_mismatch",
                message="idempotency key is already bound to another task",
            )
        approval = await self._session.scalar(
            select(ApprovalRequestModel).where(ApprovalRequestModel.task_id == task.id)
        )
        operation_text = task.input_payload.get("operation_id")
        if (
            approval is None
            or type(operation_text) is not str
            or approval.proposal_kind is None
            or approval.proposal_id is None
            or approval.proposal_version is None
        ):
            raise _trusted_action_unavailable()
        try:
            operation_id = UUID(operation_text)
            status = TaskStatus(task.status)
        except ValueError:
            raise _trusted_action_unavailable() from None
        return ExistingTrustedActionSubmission(
            task_id=task.id,
            approval_id=approval.id,
            operation_id=operation_id,
            proposal_kind=approval.proposal_kind,
            proposal_id=approval.proposal_id,
            proposal_version=approval.proposal_version,
            status=status,
        )

    async def lock_mail_draft(
        self,
        *,
        user_id: UUID,
        draft_id: UUID,
    ) -> MailDraftSubmissionSnapshot | None:
        """锁草稿头并组合解密版本、连接、能力与回复 Header 投影。"""
        draft = await self._session.scalar(
            select(MailDraftModel)
            .where(MailDraftModel.id == draft_id, MailDraftModel.user_id == user_id)
            .with_for_update()
        )
        if draft is None:
            return None
        snapshot = await SqlAlchemyMailDraftRepository(self._session, self._cipher).get_current(
            user_id=user_id,
            draft_id=draft_id,
        )
        if snapshot is None:
            return None
        connection = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == draft.connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        if connection is None:
            return None
        capabilities = await self._capabilities(
            user_id=user_id,
            connection_id=connection.id,
        )
        thread_headers = await self._reply_headers(
            user_id=user_id,
            connection_id=connection.id,
            mode=snapshot.mode,
            source_thread_id=snapshot.source_thread_id,
            source_message_id=snapshot.source_message_id,
        )
        return MailDraftSubmissionSnapshot(
            draft_id=snapshot.draft_id,
            connection_id=snapshot.connection_id,
            provider=connection.provider,
            provider_identity_key=canonical_provider_identity_key(
                connection.provider,
                connection.provider_tenant_id,
                connection.provider_account_id,
            ),
            current_version=snapshot.current_version,
            status=snapshot.status,
            mode=snapshot.mode,
            source_thread_id=snapshot.source_thread_id,
            source_message_id=snapshot.source_message_id,
            thread_headers=thread_headers,
            to=tuple(item.address for item in snapshot.to_recipients),
            cc=tuple(item.address for item in snapshot.cc_recipients),
            bcc=tuple(item.address for item in snapshot.bcc_recipients),
            subject=snapshot.subject,
            body_text=snapshot.body_text,
            connection_status=connection.status,
            read_capability_status=capabilities.get(
                ConnectionCapability.MAIL_READ,
                (CapabilityStatus.DISABLED, None),
            )[0],
            write_capability_status=capabilities.get(
                ConnectionCapability.MAIL_SEND,
                (CapabilityStatus.DISABLED, None),
            )[0],
            write_capability_error_code=capabilities.get(
                ConnectionCapability.MAIL_SEND,
                (CapabilityStatus.DISABLED, None),
            )[1],
        )

    async def lock_calendar_proposal(
        self,
        *,
        user_id: UUID,
        proposal_id: UUID,
    ) -> CalendarProposalSubmissionSnapshot | None:
        """锁提案并组合 desired 内容、连接能力、目录与目标事件版本事实。"""
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == proposal_id,
                CalendarChangeProposalModel.user_id == user_id,
            )
            .with_for_update()
        )
        if proposal is None:
            return None
        snapshot = await SqlAlchemyCalendarProposalRepository(
            self._session,
            self._cipher,
        ).get_current(user_id=user_id, proposal_id=proposal_id)
        if snapshot is None:
            return None
        content = CalendarProposalContent.model_validate(snapshot.desired_snapshot.content)
        connection = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == proposal.connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        if connection is None:
            return None
        capabilities = await self._capabilities(
            user_id=user_id,
            connection_id=connection.id,
        )
        calendar = await self._session.scalar(
            select(ProviderCalendarModel).where(
                ProviderCalendarModel.user_id == user_id,
                ProviderCalendarModel.connection_id == connection.id,
                ProviderCalendarModel.provider_calendar_id == proposal.calendar_id,
            )
        )
        event = None
        if proposal.target_event_id is not None:
            event = await self._session.scalar(
                select(CalendarEventModel).where(
                    CalendarEventModel.user_id == user_id,
                    CalendarEventModel.connection_id == connection.id,
                    CalendarEventModel.calendar_id == proposal.calendar_id,
                    CalendarEventModel.provider_event_id == proposal.target_event_id,
                )
            )
        return CalendarProposalSubmissionSnapshot(
            proposal_id=proposal.id,
            connection_id=proposal.connection_id,
            provider=connection.provider,
            provider_identity_key=canonical_provider_identity_key(
                connection.provider,
                connection.provider_tenant_id,
                connection.provider_account_id,
            ),
            calendar_id=proposal.calendar_id,
            operation_kind=proposal.operation_kind,
            target_event_id=proposal.target_event_id,
            base_etag=proposal.base_etag,
            before_snapshot_id=snapshot.before_snapshot_id,
            current_provider_etag=event.etag if event is not None else proposal.base_etag,
            current_version=proposal.current_version,
            status=CalendarProposalStatus(proposal.status),
            title=content.title,
            description=content.description,
            location=content.location,
            starts_at=content.starts_at,
            ends_at=content.ends_at,
            timezone=content.timezone,
            all_day=content.all_day,
            attendees=content.attendees,
            notification_policy=content.notification_policy,
            changed_fields=content.changed_fields,
            submission_ready=_calendar_submission_ready(
                proposal=proposal,
                content=content,
                before_snapshot_id=snapshot.before_snapshot_id,
            ),
            connection_status=connection.status,
            read_capability_status=capabilities.get(
                ConnectionCapability.CALENDAR_READ,
                (CapabilityStatus.DISABLED, None),
            )[0],
            write_capability_status=capabilities.get(
                ConnectionCapability.CALENDAR_WRITE,
                (CapabilityStatus.DISABLED, None),
            )[0],
            write_capability_error_code=capabilities.get(
                ConnectionCapability.CALENDAR_WRITE,
                (CapabilityStatus.DISABLED, None),
            )[1],
            calendar_can_write=calendar.can_write if calendar is not None else False,
            target_event_can_edit=event.can_edit if event is not None else None,
            target_event_recurring=(event.recurring_event_id is not None) if event else None,
            target_event_status=event.status if event is not None else None,
        )

    async def proposal_version_is_consumed(
        self,
        *,
        user_id: UUID,
        proposal_kind: str,
        proposal_id: UUID,
        proposal_version: int,
    ) -> bool:
        """查询任意状态审批，终态也永久消费其本地版本。"""
        count = await self._session.scalar(
            select(func.count())
            .select_from(ApprovalRequestModel)
            .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
            .where(
                TaskRunModel.user_id == user_id,
                ApprovalRequestModel.proposal_kind == proposal_kind,
                ApprovalRequestModel.proposal_id == proposal_id,
                ApprovalRequestModel.proposal_version == proposal_version,
            )
        )
        return bool(count)

    async def create_submission(self, submission: TrustedActionSubmission) -> None:
        """在当前事务写入 task/step/approval/audit/outbox 并锁定本地对象。"""
        task = TaskRunModel(
            id=submission.task_id,
            user_id=submission.user_id,
            kind="trusted_action",
            status=TaskStatus.QUEUED.value,
            idempotency_key=submission.idempotency_key,
            input_payload={
                "approval_id": str(submission.approval_id),
                "operation_id": str(submission.operation_id),
            },
            graph_thread_id=str(submission.task_id),
        )
        step = TaskStepModel(
            id=submission.step_id,
            task_id=submission.task_id,
            sequence=1,
            name="await_approval",
            kind="trusted_action",
            status="pending",
            input_summary={
                "action": submission.action,
                "proposal_version": submission.proposal_version,
            },
        )
        approval = ApprovalRequestModel(
            id=submission.approval_id,
            task_id=submission.task_id,
            step_id=submission.step_id,
            version=1,
            action=submission.action,
            schema_version=submission.schema_version,
            risk_level=submission.risk_level.value,
            payload=submission.payload,
            payload_hash=submission.payload_hash,
            payload_ciphertext=submission.encrypted_command.ciphertext,
            payload_nonce=submission.encrypted_command.nonce,
            payload_key_version=submission.encrypted_command.key_version,
            proposal_kind=submission.proposal_kind,
            proposal_id=submission.proposal_id,
            proposal_version=submission.proposal_version,
            preview_markdown="",
            expires_at=submission.expires_at,
            status=ApprovalStatus.PENDING.value,
        )
        # SQLAlchemy 无关系映射时不能可靠推断 ``ApprovalRequest -> TaskStep`` 的
        # flush 顺序；先把父任务与步骤写入当前事务，再加入审批，避免异步 PostgreSQL
        # 在查询绑定草稿触发 autoflush 时看到尚不存在的 step。这里仍在同一事务内，
        # 任一后续校验失败都会整体回滚，不会留下半份可信任务。
        self._session.add_all((task, step))
        await self._session.flush()
        self._session.add(approval)
        if submission.proposal_kind == "mail_draft":
            draft = await self._session.get(
                MailDraftModel,
                submission.proposal_id,
                with_for_update=True,
            )
            if (
                draft is None
                or draft.user_id != submission.user_id
                or draft.current_version != submission.proposal_version
                or draft.status != MailDraftStatus.EDITING.value
            ):
                raise StateConflictError(
                    error_code="draft_version_conflict",
                    message="mail draft version changed",
                )
            draft.status = MailDraftStatus.AWAITING_APPROVAL.value
        elif submission.proposal_kind == "calendar_proposal":
            proposal = await self._session.get(
                CalendarChangeProposalModel,
                submission.proposal_id,
                with_for_update=True,
            )
            if (
                proposal is None
                or proposal.user_id != submission.user_id
                or proposal.current_version != submission.proposal_version
                or proposal.status != CalendarProposalStatus.EDITING.value
            ):
                raise StateConflictError(
                    error_code="proposal_version_conflict",
                    message="calendar proposal version changed",
                )
            proposal.status = CalendarProposalStatus.AWAITING_APPROVAL.value
        else:
            raise _trusted_action_unavailable()
        warning_values = [warning.value for warning in submission.warnings]
        self._session.add_all(
            (
                AuditEventModel(
                    user_id=submission.user_id,
                    task_id=submission.task_id,
                    event_type="approval.requested",
                    actor_type="user",
                    actor_id=str(submission.user_id),
                    event_metadata={
                        "action": submission.action,
                        "approval_id": str(submission.approval_id),
                        "proposal_id": str(submission.proposal_id),
                        "proposal_kind": submission.proposal_kind,
                        "proposal_version": submission.proposal_version,
                        "risk_level": submission.risk_level.value,
                        "status": ApprovalStatus.PENDING.value,
                        "version": 1,
                        "warnings": warning_values,
                    },
                ),
                OutboxEventModel(
                    topic="task.execute",
                    aggregate_id=submission.task_id,
                    deduplication_key=f"task.execute:{submission.task_id}:initial",
                    payload={"task_id": str(submission.task_id)},
                    available_at=submission.available_at,
                ),
            )
        )
        await self._session.flush()

    async def load_graph_facts(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
    ) -> tuple[str, str | None] | None:
        """读取 Graph 可持久化的哈希与审批决定，不解密真实命令。"""
        row = (
            await self._session.execute(
                select(TaskRunModel, ApprovalRequestModel)
                .join(
                    ApprovalRequestModel,
                    ApprovalRequestModel.task_id == TaskRunModel.id,
                )
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.kind == "trusted_action",
                    ApprovalRequestModel.id == approval_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        task, approval = row
        if not _task_operation_matches(task, operation_id):
            return None
        decision = (
            approval.status
            if approval.status
            in {
                ApprovalStatus.APPROVED.value,
                ApprovalStatus.REJECTED.value,
                ApprovalStatus.INVALIDATED.value,
                ApprovalStatus.EXPIRED.value,
            }
            else None
        )
        return approval.payload_hash, decision

    async def lock_execution(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
    ) -> TrustedActionExecutionSnapshot | None:
        """按冻结锁序读取首次 claim 的全部授权事实。

        锁序固定为 TaskRun → ApprovalRequest → ToolExecution → 本地动作 → User →
        OAuthConnection → capability rows → ProviderCalendar。能力关闭先锁 connection 再写
        capability，claim 使用同一 connection-first 子序列，确保撤权提交后只能读取新状态，
        也避免保留/隐私清理、审批失效和并发 Worker 形成反向等待。
        """
        task = await self._session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == task_id).with_for_update()
        )
        if (
            task is None
            or task.kind != "trusted_action"
            or not _task_operation_matches(task, operation_id)
        ):
            return None
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .where(
                ApprovalRequestModel.id == approval_id,
                ApprovalRequestModel.task_id == task.id,
            )
            .with_for_update()
        )
        if approval is None:
            return None
        execution = await self._session.scalar(
            select(ToolExecutionModel)
            .where(
                ToolExecutionModel.task_id == task.id,
                ToolExecutionModel.operation_id == operation_id,
            )
            .with_for_update()
        )
        binding = await self._lock_action_binding(task=task, approval=approval)
        if binding is None:
            return None
        connection_id, calendar_id, _ = binding
        user = await self._session.scalar(
            select(UserModel).where(UserModel.id == task.user_id).with_for_update()
        )
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == task.user_id,
            )
            .with_for_update()
        )
        if user is None or connection is None or approval.schema_version is None:
            return None
        capabilities = await self._capabilities(
            user_id=task.user_id,
            connection_id=connection_id,
            lock_rows=True,
        )
        if approval.action == "mail.send":
            read_capability = ConnectionCapability.MAIL_READ
            write_capability = ConnectionCapability.MAIL_SEND
        elif approval.action in {
            "calendar.create",
            "calendar.update",
            "calendar.restore",
        }:
            read_capability = ConnectionCapability.CALENDAR_READ
            write_capability = ConnectionCapability.CALENDAR_WRITE
        else:
            return None
        calendar_can_write: bool | None = None
        if calendar_id is not None:
            calendar = await self._session.scalar(
                select(ProviderCalendarModel)
                .where(
                    ProviderCalendarModel.user_id == task.user_id,
                    ProviderCalendarModel.connection_id == connection_id,
                    ProviderCalendarModel.provider_calendar_id == calendar_id,
                )
                .with_for_update()
            )
            calendar_can_write = calendar.can_write if calendar is not None else False
        # PostgreSQL 时间必须在固定锁序全部完成后采样；它与本事务看到的租约、审批
        # deadline 和能力事实属于同一个授权瞬间，避免锁等待期间复用过时应用时钟。
        database_now = await self._session.scalar(select(func.clock_timestamp()))
        if database_now is None:
            return None
        try:
            task_status = TaskStatus(task.status)
            parsed_execution = (
                _execution_reference(execution=execution, approval_id=approval.id)
                if execution is not None
                else None
            )
        except ValueError:
            return None
        return TrustedActionExecutionSnapshot(
            user_id=task.user_id,
            user_is_active=user.is_active,
            task_id=task.id,
            task_status=task_status,
            task_lease_owner=task.lease_owner,
            task_lease_expires_at=task.lease_expires_at,
            database_now=database_now,
            step_id=approval.step_id,
            approval_id=approval.id,
            approval_version=approval.version,
            approval_status=approval.status,
            approved_execution_deadline_at=approval.approved_execution_deadline_at,
            action=approval.action,
            schema_version=approval.schema_version,
            payload_hash=approval.payload_hash,
            proposal_kind=approval.proposal_kind or "",
            proposal_id=approval.proposal_id or UUID(int=0),
            proposal_version=approval.proposal_version or 0,
            operation_id=operation_id,
            connection_id=connection_id,
            provider=connection.provider,
            provider_tenant_id=connection.provider_tenant_id,
            provider_account_id=connection.provider_account_id,
            connection_status=connection.status,
            read_capability_status=capabilities.get(
                read_capability,
                (CapabilityStatus.DISABLED, None),
            )[0],
            write_capability_status=capabilities.get(
                write_capability,
                (CapabilityStatus.DISABLED, None),
            )[0],
            write_capability_error_code=capabilities.get(
                write_capability,
                (CapabilityStatus.DISABLED, None),
            )[1],
            calendar_can_write=calendar_can_write,
            execution=parsed_execution,
        )

    async def fail_unclaimed_action(
        self,
        *,
        snapshot: TrustedActionExecutionSnapshot,
        error_code: str,
        failed_at: datetime,
    ) -> None:
        """无 ToolExecution 时原子失效审批、失败任务并恢复本地编辑态。"""
        task, approval = await self._locked_task_approval(
            task_id=snapshot.task_id,
            approval_id=snapshot.approval_id,
        )
        if task is None or approval is None:
            raise _trusted_action_unavailable()
        existing = await self._session.scalar(
            select(ToolExecutionModel).where(
                ToolExecutionModel.task_id == task.id,
                ToolExecutionModel.operation_id == snapshot.operation_id,
            )
        )
        if existing is not None:
            raise _trusted_action_unavailable()
        approval.status = ApprovalStatus.INVALIDATED.value
        task.status = TaskStatus.FAILED.value
        task.error_code = error_code
        task.finished_at = failed_at
        _clear_task_scheduling(task)
        await self._set_local_action_status(
            task=task,
            approval=approval,
            mail_status=MailDraftStatus.EDITING,
            calendar_status=CalendarProposalStatus.EDITING,
            allowed_current={
                MailDraftStatus.AWAITING_APPROVAL.value,
                CalendarProposalStatus.AWAITING_APPROVAL.value,
            },
        )
        approval_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="approval.invalidated",
            actor_type="system",
            actor_id=None,
            event_metadata={"reason": error_code},
        )
        task_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="task.failed",
            actor_type="worker",
            actor_id=snapshot.task_lease_owner,
            event_metadata={
                "status": TaskStatus.FAILED.value,
                "error_code": error_code,
            },
        )
        # 本事务已经清除任务租约，通用 Runner 的 owner CAS 不会再补写终态审计；
        # 因此审批失效与 task.failed 必须和状态、本地编辑态一起原子提交。
        self._session.add_all((approval_audit, task_audit))
        await self._session.flush()
        self._session.add(
            OutboxEventModel(
                topic="approval.invalidated",
                aggregate_id=task.id,
                deduplication_key=f"approval.invalidated:{approval.id}:{approval.version}",
                payload={"task_id": str(task.id), "audit_event_id": approval_audit.id},
                available_at=failed_at,
            )
        )

    async def create_tool_claim(
        self,
        *,
        snapshot: TrustedActionExecutionSnapshot,
        execution_id: UUID,
        idempotency_key: str,
        claimed_at: datetime,
    ) -> None:
        """在当前锁事务插入唯一 claim，并写入本地 executing、审计和 Outbox。"""
        if snapshot.execution is not None:
            return
        existing = await self._session.scalar(
            select(ToolExecutionModel).where(
                ToolExecutionModel.task_id == snapshot.task_id,
                ToolExecutionModel.operation_id == snapshot.operation_id,
            )
        )
        if existing is not None:
            return
        execution = ToolExecutionModel(
            id=execution_id,
            task_id=snapshot.task_id,
            step_id=snapshot.step_id,
            tool_name=snapshot.action,
            idempotency_key=idempotency_key,
            operation_id=snapshot.operation_id,
            request_payload_hash=snapshot.payload_hash,
            provider=snapshot.provider,
            status=ToolExecutionStatus.CLAIMED.value,
            claimed_at=claimed_at,
            write_attempt_count=0,
            reconciliation_attempt_count=0,
        )
        self._session.add(execution)
        task = await self._session.get(TaskRunModel, snapshot.task_id)
        approval = await self._session.get(ApprovalRequestModel, snapshot.approval_id)
        if task is None or approval is None:
            raise _trusted_action_unavailable()
        await self._set_local_action_status(
            task=task,
            approval=approval,
            mail_status=MailDraftStatus.EXECUTING,
            calendar_status=CalendarProposalStatus.EXECUTING,
            allowed_current={
                MailDraftStatus.AWAITING_APPROVAL.value,
                CalendarProposalStatus.AWAITING_APPROVAL.value,
            },
        )
        audit = AuditEventModel(
            user_id=snapshot.user_id,
            task_id=snapshot.task_id,
            event_type="tool.claimed",
            actor_type="worker",
            actor_id=None,
            event_metadata={
                "action": snapshot.action,
                "provider": snapshot.provider,
                "approval_version": snapshot.approval_version,
            },
        )
        self._session.add(audit)
        await self._session.flush()
        self._session.add(
            OutboxEventModel(
                topic="tool.claimed",
                aggregate_id=snapshot.task_id,
                deduplication_key=f"tool.claimed:{execution_id}",
                payload={"task_id": str(snapshot.task_id), "audit_event_id": audit.id},
                available_at=claimed_at,
            )
        )

    async def load_dispatch(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
    ) -> TrustedActionDispatchSnapshot | None:
        """先读终态最小事实，非终态再加载本地动作、连接与加密命令绑定。"""
        task = await self._session.get(TaskRunModel, task_id)
        approval = await self._session.scalar(
            select(ApprovalRequestModel).where(
                ApprovalRequestModel.id == approval_id,
                ApprovalRequestModel.task_id == task_id,
            )
        )
        execution = await self._session.scalar(
            select(ToolExecutionModel).where(
                ToolExecutionModel.task_id == task_id,
                ToolExecutionModel.operation_id == operation_id,
            )
        )
        if (
            task is None
            or approval is None
            or execution is None
            or approval.schema_version is None
            or approval.proposal_id is None
            or approval.proposal_version is None
            or not _task_operation_matches(task, operation_id)
        ):
            return None
        try:
            reference = _execution_reference(execution=execution, approval_id=approval.id)
        except ValueError:
            return None
        if reference.status in {
            ToolExecutionStatus.SUCCEEDED,
            ToolExecutionStatus.CONFIRMED_FAILED,
            ToolExecutionStatus.NEEDS_ATTENTION,
        }:
            # 外部副作用结论只依赖 Task/Approval/ToolExecution 的内容无关绑定。
            # 本地动作、连接或审批密文按保留策略清理后仍不得触发解密或 adapter lookup。
            return TrustedActionDispatchSnapshot(
                user_id=task.user_id,
                task_id=task.id,
                step_id=approval.step_id,
                approval_id=approval.id,
                approval_version=approval.version,
                operation_id=operation_id,
                connection_id=None,
                calendar_id=None,
                action=approval.action,
                schema_version=approval.schema_version,
                payload_hash=approval.payload_hash,
                proposal_kind=approval.proposal_kind or "",
                proposal_id=approval.proposal_id,
                proposal_version=approval.proposal_version,
                provider=reference.provider,
                execution=reference,
            )
        binding = await self._lock_action_binding(task=task, approval=approval)
        if binding is None:
            return None
        connection_id, calendar_id, _ = binding
        connection_provider = await self._session.scalar(
            select(OAuthConnectionModel.provider).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == task.user_id,
            )
        )
        if connection_provider is None:
            return None
        return TrustedActionDispatchSnapshot(
            user_id=task.user_id,
            task_id=task.id,
            step_id=approval.step_id,
            approval_id=approval.id,
            approval_version=approval.version,
            operation_id=operation_id,
            connection_id=connection_id,
            calendar_id=calendar_id,
            action=approval.action,
            schema_version=approval.schema_version,
            payload_hash=approval.payload_hash,
            proposal_kind=approval.proposal_kind or "",
            proposal_id=approval.proposal_id,
            proposal_version=approval.proposal_version,
            provider=connection_provider,
            execution=reference,
        )

    async def mark_request_started(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
        authorize: RequestStartAuthorizer,
    ) -> RequestStartResult:
        """按固定锁序并以 PostgreSQL 权威时钟提交唯一 request-start。

        调用方时间可能在命令解密或行锁等待期间变旧，因此它绝不能授权外部写入。本事务
        依次锁定 TaskRun、ApprovalRequest、ToolExecution、本地草稿/提案、User 与
        OAuthConnection、capability 与 ProviderCalendar，随后才读取 ``clock_timestamp()``。
        应用层 authorizer 在这些锁仍由同一短事务持有时重算全局/供应商开关与规范账户
        allowlist；只有全部事实仍成立，才能递增真实写尝试计数并推进到 ``executing``。
        """
        task = await self._session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == snapshot.task_id).with_for_update()
        )
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .where(
                ApprovalRequestModel.id == snapshot.approval_id,
                ApprovalRequestModel.task_id == snapshot.task_id,
            )
            .with_for_update()
        )
        execution = await self._session.scalar(
            select(ToolExecutionModel)
            .where(
                ToolExecutionModel.id == snapshot.execution.execution_id,
                ToolExecutionModel.task_id == snapshot.task_id,
            )
            .with_for_update()
        )
        if task is None or approval is None or execution is None:
            return RequestStartResult(RequestStartDisposition.ABANDONED)
        try:
            execution_status = ToolExecutionStatus(execution.status)
        except ValueError:
            return RequestStartResult(RequestStartDisposition.ABANDONED)
        if execution_status in {
            ToolExecutionStatus.EXECUTING,
            ToolExecutionStatus.RECONCILING,
        } or (
            execution_status is ToolExecutionStatus.CLAIMED
            and execution.request_started_at is not None
        ):
            return RequestStartResult(RequestStartDisposition.RECONCILE)
        if execution_status in {
            ToolExecutionStatus.SUCCEEDED,
            ToolExecutionStatus.CONFIRMED_FAILED,
            ToolExecutionStatus.NEEDS_ATTENTION,
        }:
            return RequestStartResult(RequestStartDisposition.ABANDONED)
        binding = (
            await self._lock_action_binding(task=task, approval=approval)
            if task is not None and approval is not None
            else None
        )
        user = await self._session.scalar(
            select(UserModel).where(UserModel.id == snapshot.user_id).with_for_update()
        )
        connection = (
            await self._session.scalar(
                select(OAuthConnectionModel)
                .where(
                    OAuthConnectionModel.id == snapshot.connection_id,
                    OAuthConnectionModel.user_id == snapshot.user_id,
                )
                .with_for_update()
            )
            if snapshot.connection_id is not None
            else None
        )
        capabilities = (
            await self._capabilities(
                user_id=snapshot.user_id,
                connection_id=snapshot.connection_id,
                lock_rows=True,
            )
            if snapshot.connection_id is not None
            else {}
        )
        if snapshot.action == "mail.send":
            read_capability = ConnectionCapability.MAIL_READ
            write_capability = ConnectionCapability.MAIL_SEND
        elif snapshot.action in {
            "calendar.create",
            "calendar.update",
            "calendar.restore",
        }:
            read_capability = ConnectionCapability.CALENDAR_READ
            write_capability = ConnectionCapability.CALENDAR_WRITE
        else:
            read_capability = None
            write_capability = None
        calendar_can_write: bool | None = None
        if binding is not None and binding[1] is not None:
            calendar = await self._session.scalar(
                select(ProviderCalendarModel)
                .where(
                    ProviderCalendarModel.user_id == snapshot.user_id,
                    ProviderCalendarModel.connection_id == binding[0],
                    ProviderCalendarModel.provider_calendar_id == binding[1],
                )
                .with_for_update()
            )
            calendar_can_write = calendar.can_write if calendar is not None else False
        database_now = await self._session.scalar(select(func.clock_timestamp()))
        if (
            task.status != TaskStatus.RUNNING.value
            or task.lease_owner != lease_owner
            or task.lease_expires_at is None
            or database_now is None
            or task.lease_expires_at <= database_now
        ):
            return RequestStartResult(RequestStartDisposition.ABANDONED)
        action_connection_id, action_calendar_id, action_status = (
            binding if binding is not None else (None, None, None)
        )
        binding_is_valid = (
            user is not None
            and connection is not None
            and connection.provider == snapshot.provider
            and read_capability is not None
            and write_capability is not None
        )
        binding_is_valid = binding_is_valid and (
            task.user_id == snapshot.user_id
            and _task_operation_matches(task, snapshot.operation_id)
            and approval.step_id == snapshot.execution.step_id
            and approval.action == snapshot.action
            and approval.schema_version == snapshot.schema_version
            and approval.version == snapshot.approval_version
            and approval.proposal_kind == snapshot.proposal_kind
            and approval.proposal_id == snapshot.proposal_id
            and approval.proposal_version == snapshot.proposal_version
            and action_connection_id == snapshot.connection_id
            and action_calendar_id == snapshot.calendar_id
            and action_status
            in {
                MailDraftStatus.EXECUTING.value,
                CalendarProposalStatus.EXECUTING.value,
            }
            and compare_digest(approval.payload_hash, snapshot.payload_hash)
            and trusted_execution_binding_matches(
                execution_task_id=execution.task_id,
                expected_task_id=task.id,
                execution_step_id=execution.step_id,
                expected_step_id=approval.step_id,
                execution_operation_id=execution.operation_id,
                expected_operation_id=snapshot.operation_id,
                execution_provider=execution.provider,
                expected_provider=snapshot.provider,
                execution_tool_name=execution.tool_name,
                expected_action=approval.action,
                execution_idempotency_key=execution.idempotency_key,
                approval_id=approval.id,
                approval_version=approval.version,
                execution_payload_hash=execution.request_payload_hash,
                expected_payload_hash=approval.payload_hash,
            )
        )
        if execution_status is ToolExecutionStatus.CLAIMED:
            if (
                execution.request_started_at is not None
                or execution.write_attempt_count != 0
                or execution.result_summary is not None
            ):
                return RequestStartResult(RequestStartDisposition.RECONCILE)
        elif execution_status is ToolExecutionStatus.RETRYABLE_FAILED:
            if (
                execution.request_started_at is None
                or execution.write_attempt_count <= 0
                or not durable_retry_summary_is_valid(execution.result_summary)
            ):
                binding_is_valid = False
        else:
            return RequestStartResult(RequestStartDisposition.ABANDONED)
        authorization_error: str | None = "trusted_action_unavailable"
        if (
            binding_is_valid
            and user is not None
            and connection is not None
            and read_capability is not None
            and write_capability is not None
        ):
            authorization_error = authorize(
                TrustedActionRequestStartAuthorization(
                    user_is_active=user.is_active,
                    provider=connection.provider,
                    provider_tenant_id=connection.provider_tenant_id,
                    provider_account_id=connection.provider_account_id,
                    connection_status=connection.status,
                    read_capability_status=capabilities.get(
                        read_capability,
                        (CapabilityStatus.DISABLED, None),
                    )[0],
                    write_capability_status=capabilities.get(
                        write_capability,
                        (CapabilityStatus.DISABLED, None),
                    )[0],
                    write_capability_error_code=capabilities.get(
                        write_capability,
                        (CapabilityStatus.DISABLED, None),
                    )[1],
                    calendar_can_write=calendar_can_write,
                )
            )
        if authorization_error is not None:
            await self._settle_request_start_failure(
                snapshot=snapshot,
                task=task,
                approval=approval,
                execution=execution,
                error_code=authorization_error,
                failed_at=database_now,
                lease_owner=lease_owner,
            )
            return RequestStartResult(
                RequestStartDisposition.INVALIDATED,
                error_code=authorization_error,
            )
        if execution.request_started_at is None:
            execution.request_started_at = database_now
        execution.write_attempt_count += 1
        execution.status = ToolExecutionStatus.EXECUTING.value
        await self._session.flush()
        return RequestStartResult(RequestStartDisposition.STARTED)

    async def _settle_request_start_failure(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        task: TaskRunModel,
        approval: ApprovalRequestModel,
        execution: ToolExecutionModel,
        error_code: str,
        failed_at: datetime,
        lease_owner: str,
    ) -> None:
        """在已证明本轮尚未发出写请求时原子失效 claim 与冻结本地版本。

        Args:
            snapshot: dispatch 首次读取的冻结标识与本地版本。
            task: 已按固定顺序锁定且仍由当前 owner 持有的 TaskRun。
            approval: 与该任务绑定的已锁审批；即使非敏感元数据被篡改也会失效。
            execution: 已锁 ToolExecution，必须仍处于未开始写入或明确未应用状态。
            error_code: 应用策略或绑定校验返回的稳定无内容失败码。
            failed_at: 全部相关行锁取得后的 PostgreSQL 权威时间。
            lease_owner: 当前执行者，用于任务审计 actor 与 owner 证明。
        """
        execution.status = ToolExecutionStatus.CONFIRMED_FAILED.value
        execution.error_code = error_code
        execution.completed_at = failed_at
        execution.result_summary = {
            "kind": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
            "retryable": False,
        }
        approval.status = ApprovalStatus.INVALIDATED.value
        task.status = TaskStatus.FAILED.value
        task.error_code = error_code
        task.finished_at = failed_at
        _clear_task_scheduling(task)
        await self._set_snapshot_action_status(
            snapshot=snapshot,
            mail_status=MailDraftStatus.EDITING,
            calendar_status=CalendarProposalStatus.EDITING,
            allowed_current={
                MailDraftStatus.EXECUTING.value,
                CalendarProposalStatus.EXECUTING.value,
            },
        )
        approval_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="approval.invalidated",
            actor_type="system",
            actor_id=None,
            event_metadata={"reason": error_code},
        )
        task_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="task.failed",
            actor_type="worker",
            actor_id=lease_owner,
            event_metadata={"error_code": error_code},
        )
        tool_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="tool.confirmed_failed",
            actor_type="worker",
            actor_id=None,
            event_metadata={
                "action": snapshot.action,
                "provider": snapshot.provider,
                "outcome": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
                "write_attempt_count": execution.write_attempt_count,
            },
        )
        self._session.add_all((approval_audit, task_audit, tool_audit))
        await self._session.flush()
        self._session.add_all(
            (
                OutboxEventModel(
                    topic="approval.invalidated",
                    aggregate_id=task.id,
                    deduplication_key=(
                        f"approval.invalidated:{approval.id}:{approval.version}:request-start"
                    ),
                    payload={"task_id": str(task.id), "audit_event_id": approval_audit.id},
                    available_at=failed_at,
                ),
                OutboxEventModel(
                    topic="tool.confirmed_failed",
                    aggregate_id=task.id,
                    deduplication_key=f"tool.confirmed_failed:{execution.id}:request-start",
                    payload={"task_id": str(task.id), "audit_event_id": tool_audit.id},
                    available_at=failed_at,
                ),
            )
        )

    async def persist_provider_outcome(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        outcome: ProviderWriteOutcome,
        completed_at: datetime,
        from_reconciliation: bool,
        may_retry_write: bool,
        lease_owner: str,
    ) -> None:
        """以锁后数据库时间、live lease 与完整冻结绑定 CAS 提交 provider 结果。

        ``completed_at`` 仅保留应用端口兼容性，不能授权或决定持久时间；供应商网络等待
        期间它可能已经陈旧。事务依次锁 Task、Approval、ToolExecution、本地动作、
        Connection 与目标日历，最后读取 ``clock_timestamp()``，只有当前 owner 的租约
        仍有效且全部冻结标识未换绑才允许写入结果。连接在请求发出后的正常断开、scope
        撤销或目录 ``can_write`` 变化不会抹掉真实结果，因为本边界只核对目标身份，不重跑
        request-start 写授权策略。
        """
        task, approval = await self._locked_task_approval(
            task_id=snapshot.task_id,
            approval_id=snapshot.approval_id,
        )
        execution = await self._session.scalar(
            select(ToolExecutionModel)
            .where(ToolExecutionModel.id == snapshot.execution.execution_id)
            .with_for_update()
        )
        if task is None or approval is None or execution is None:
            raise _trusted_action_unavailable()
        try:
            status = ToolExecutionStatus(execution.status)
        except ValueError:
            raise _trusted_action_unavailable() from None
        if status in {
            ToolExecutionStatus.SUCCEEDED,
            ToolExecutionStatus.CONFIRMED_FAILED,
        }:
            return
        binding = await self._lock_action_binding(task=task, approval=approval)
        connection = (
            await self._session.scalar(
                select(OAuthConnectionModel)
                .where(
                    OAuthConnectionModel.id == snapshot.connection_id,
                    OAuthConnectionModel.user_id == snapshot.user_id,
                )
                .with_for_update()
            )
            if snapshot.connection_id is not None
            else None
        )
        calendar = None
        if snapshot.calendar_id is not None and snapshot.connection_id is not None:
            calendar = await self._session.scalar(
                select(ProviderCalendarModel)
                .where(
                    ProviderCalendarModel.user_id == snapshot.user_id,
                    ProviderCalendarModel.connection_id == snapshot.connection_id,
                    ProviderCalendarModel.provider_calendar_id == snapshot.calendar_id,
                )
                .with_for_update()
            )
        database_now = await self._session.scalar(select(func.clock_timestamp()))
        if binding is None or connection is None or database_now is None:
            raise _trusted_action_unavailable()
        action_connection_id, action_calendar_id, action_status = binding
        allowed = (
            {
                ToolExecutionStatus.EXECUTING,
                ToolExecutionStatus.RECONCILING,
                ToolExecutionStatus.CLAIMED,
            }
            if from_reconciliation
            else {ToolExecutionStatus.EXECUTING}
        )
        task_status_allowed = (
            {TaskStatus.RUNNING.value, TaskStatus.RECONCILING.value}
            if from_reconciliation
            else {TaskStatus.RUNNING.value}
        )
        action_status_allowed = (
            {
                MailDraftStatus.EXECUTING.value,
                CalendarProposalStatus.EXECUTING.value,
                MailDraftStatus.NEEDS_ATTENTION.value,
                CalendarProposalStatus.NEEDS_ATTENTION.value,
            }
            if from_reconciliation
            else {
                MailDraftStatus.EXECUTING.value,
                CalendarProposalStatus.EXECUTING.value,
            }
        )
        expected_write_attempt_count = (
            snapshot.execution.write_attempt_count
            if from_reconciliation
            else snapshot.execution.write_attempt_count + 1
        )
        expected_reconciliation_attempt_count = snapshot.execution.reconciliation_attempt_count
        if (
            status not in allowed
            or task.kind != "trusted_action"
            or task.user_id != snapshot.user_id
            or task.status not in task_status_allowed
            or task.lease_owner != lease_owner
            or task.lease_expires_at is None
            or task.lease_expires_at <= database_now
            or not _task_operation_matches(task, snapshot.operation_id)
            or approval.step_id != snapshot.step_id
            or approval.status != ApprovalStatus.APPROVED.value
            or approval.action != snapshot.action
            or approval.schema_version != snapshot.schema_version
            or approval.version != snapshot.approval_version
            or approval.proposal_kind != snapshot.proposal_kind
            or approval.proposal_id != snapshot.proposal_id
            or approval.proposal_version != snapshot.proposal_version
            or not compare_digest(approval.payload_hash, snapshot.payload_hash)
            or action_connection_id != snapshot.connection_id
            or action_calendar_id != snapshot.calendar_id
            or action_status not in action_status_allowed
            or connection.provider != snapshot.provider
            or (snapshot.calendar_id is not None and calendar is None)
            or execution.request_started_at is None
            or execution.write_attempt_count != expected_write_attempt_count
            or execution.reconciliation_attempt_count != expected_reconciliation_attempt_count
            or not trusted_execution_binding_matches(
                execution_task_id=execution.task_id,
                expected_task_id=snapshot.task_id,
                execution_step_id=execution.step_id,
                expected_step_id=snapshot.step_id,
                execution_operation_id=execution.operation_id,
                expected_operation_id=snapshot.operation_id,
                execution_provider=execution.provider,
                expected_provider=snapshot.provider,
                execution_tool_name=execution.tool_name,
                expected_action=snapshot.action,
                execution_idempotency_key=execution.idempotency_key,
                approval_id=snapshot.approval_id,
                approval_version=snapshot.approval_version,
                execution_payload_hash=execution.request_payload_hash,
                expected_payload_hash=snapshot.payload_hash,
            )
        ):
            raise _trusted_action_unavailable()
        # 后续核对有时只返回其中一部分关联标识；已验证的旧值不能被一个空字段覆盖，
        # 否则下一次只读核对会失去原始 request/resource 关联。
        if outcome.provider_resource_id is not None:
            execution.provider_resource_id = outcome.provider_resource_id
        if outcome.provider_request_id is not None:
            execution.provider_request_id = outcome.provider_request_id
        # 只读核对可能只返回 resource/request 其中一部分；已持久化的关联 ID 不能被
        # 一个空字段覆盖，否则后续 bounded reconcile 会丢失可审计的稳定关联。
        if outcome.correlation_id is not None:
            execution.correlation_id = outcome.correlation_id
        execution.error_code = outcome.error_code
        execution.result_summary = _outcome_summary(outcome)
        if from_reconciliation:
            execution.reconciliation_attempt_count += 1
            execution.last_reconciled_at = database_now
        if outcome.kind is ProviderWriteOutcomeKind.UNKNOWN:
            # UNKNOWN 只证明响应语义不明确，绝不能伪造 confirmed_not_applied。把任务和
            # 本地动作一起推进到 needs-attention/reconciling，随后仅由专用只读 worker
            # 按持久 scheduled_for 重新取得租约；write_attempt_count 永不递增。
            reconciliation_count = execution.reconciliation_attempt_count
            terminal_reconciliation = (
                from_reconciliation and reconciliation_count >= MAX_RECONCILIATION_ATTEMPTS
            )
            execution.status = (
                ToolExecutionStatus.NEEDS_ATTENTION.value
                if terminal_reconciliation
                else ToolExecutionStatus.RECONCILING.value
            )
            execution.error_code = (
                "provider_reconciliation_failed"
                if terminal_reconciliation
                else "provider_write_outcome_unknown"
            )
            task.status = (
                TaskStatus.NEEDS_ATTENTION.value
                if terminal_reconciliation
                else TaskStatus.RECONCILING.value
            )
            task.error_code = execution.error_code
            task.finished_at = None
            task.lease_owner = None
            task.lease_expires_at = None
            task.retry_recovery_at = None
            task.approval_checkpoint_recovery_at = None
            if terminal_reconciliation:
                task.scheduled_for = None
            else:
                # 初次 UNKNOWN 使用 delay(0)；每次只读 UNKNOWN 后使用下一槽位。显式
                # 用户重开可能从计数 4 开始，下一轮仍由 terminal 分支收敛而不会无限自动排队。
                delay_index = 0 if not from_reconciliation else reconciliation_count
                task.scheduled_for = database_now + reconciliation_delay(delay_index)
            await self._set_local_action_status(
                task=task,
                approval=approval,
                mail_status=MailDraftStatus.NEEDS_ATTENTION,
                calendar_status=CalendarProposalStatus.NEEDS_ATTENTION,
                allowed_current=action_status_allowed,
            )
            audit = AuditEventModel(
                user_id=task.user_id,
                task_id=task.id,
                event_type=(
                    "tool.needs_attention" if terminal_reconciliation else "tool.reconciling"
                ),
                actor_type="worker",
                actor_id=lease_owner,
                event_metadata={
                    "action": snapshot.action,
                    "provider": snapshot.provider,
                    "outcome": ProviderWriteOutcomeKind.UNKNOWN.value,
                    "reconciliation_attempt_count": reconciliation_count,
                },
            )
            self._session.add(audit)
            await self._session.flush()
            lifecycle_topic = (
                "tool.needs_attention" if terminal_reconciliation else "tool.reconciling"
            )
            events = [
                OutboxEventModel(
                    topic=lifecycle_topic,
                    aggregate_id=task.id,
                    deduplication_key=(f"{lifecycle_topic}:{execution.id}:{reconciliation_count}"),
                    payload={"task_id": str(task.id), "audit_event_id": audit.id},
                    available_at=database_now,
                )
            ]
            if not terminal_reconciliation and task.scheduled_for is not None:
                events.append(
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=task.id,
                        deduplication_key=(
                            f"task.execute:{task.id}:reconcile:{reconciliation_count}"
                        ),
                        payload={"task_id": str(task.id)},
                        available_at=task.scheduled_for,
                    )
                )
            self._session.add_all(events)
            return
        event_type: str
        outbox_topic: str
        if outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED:
            execution.status = ToolExecutionStatus.SUCCEEDED.value
            execution.completed_at = database_now
            task.status = TaskStatus.SUCCEEDED.value
            task.error_code = None
            task.finished_at = database_now
            _clear_task_scheduling(task)
            await self._set_local_action_status(
                task=task,
                approval=approval,
                mail_status=MailDraftStatus.SENT,
                calendar_status=CalendarProposalStatus.APPLIED,
                allowed_current={
                    MailDraftStatus.EXECUTING.value,
                    CalendarProposalStatus.EXECUTING.value,
                    MailDraftStatus.NEEDS_ATTENTION.value,
                    CalendarProposalStatus.NEEDS_ATTENTION.value,
                },
            )
            # provider 结果事务会先于 DurableTaskRunner 的通用 finish 清除租约；因此任务
            # 终态审计必须和 ToolExecution、本地对象及 TaskRun 在这里原子提交，不能依赖
            # 随后的 owner CAS 补写，否则 ACK 正常返回时也会缺失 task.succeeded 时间线事实。
            self._session.add(
                AuditEventModel(
                    user_id=task.user_id,
                    task_id=task.id,
                    event_type="task.succeeded",
                    actor_type="worker",
                    actor_id=lease_owner,
                    event_metadata={"reason": "trusted_action_confirmed_applied"},
                )
            )
            # 已确认应用的结果必须触发一次只读 source refresh；refresh 任务本身只读，
            # 其稳定幂等键与 execution 绑定，重放该终态不会产生第二次 provider write。
            await self._enqueue_source_refresh(
                task=task,
                approval=approval,
                execution=execution,
                connection=connection,
                calendar_id=snapshot.calendar_id,
                available_at=database_now,
            )
            event_type = outbox_topic = "tool.succeeded"
        elif (
            outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
            and outcome.retryable
            and may_retry_write
            and not from_reconciliation
        ):
            execution.status = ToolExecutionStatus.RETRYABLE_FAILED.value
            event_type = outbox_topic = "tool.retryable_failed"
        else:
            if outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED:
                terminal_error_code = (
                    "task_retries_exhausted"
                    if outcome.retryable
                    else outcome.error_code or "provider_write_confirmed_not_applied"
                )
            execution.status = ToolExecutionStatus.CONFIRMED_FAILED.value
            execution.error_code = terminal_error_code
            execution.completed_at = database_now
            task.status = TaskStatus.FAILED.value
            task.error_code = terminal_error_code
            task.finished_at = database_now
            _clear_task_scheduling(task)
            calendar_target = (
                CalendarProposalStatus.STALE
                if snapshot.action == "calendar.update"
                and outcome.error_code == "calendar_event_version_conflict"
                else CalendarProposalStatus.EDITING
            )
            await self._set_local_action_status(
                task=task,
                approval=approval,
                mail_status=MailDraftStatus.EDITING,
                calendar_status=calendar_target,
                allowed_current={
                    MailDraftStatus.EXECUTING.value,
                    CalendarProposalStatus.EXECUTING.value,
                    MailDraftStatus.NEEDS_ATTENTION.value,
                    CalendarProposalStatus.NEEDS_ATTENTION.value,
                },
            )
            # 任务终态与 ToolExecution 结果都在本事务内确定；通用 Runner 随后会因租约
            # 已清除而无法补写 task.failed，因此这里必须原子追加任务和工具两条时间线。
            self._session.add(
                AuditEventModel(
                    user_id=task.user_id,
                    task_id=task.id,
                    event_type="task.failed",
                    actor_type="worker",
                    actor_id=lease_owner,
                    event_metadata={"error_code": task.error_code},
                )
            )
            event_type = outbox_topic = "tool.confirmed_failed"
        audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type=event_type,
            actor_type="worker",
            actor_id=None,
            event_metadata={
                "action": snapshot.action,
                "provider": snapshot.provider,
                "outcome": outcome.kind.value,
                "write_attempt_count": execution.write_attempt_count,
            },
        )
        self._session.add(audit)
        await self._session.flush()
        self._session.add(
            OutboxEventModel(
                topic=outbox_topic,
                aggregate_id=task.id,
                deduplication_key=f"{outbox_topic}:{execution.id}:{execution.write_attempt_count}",
                payload={"task_id": str(task.id), "audit_event_id": audit.id},
                available_at=database_now,
            )
        )

    async def claim_reconciliation(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> TrustedActionDispatchSnapshot | None:
        """为一轮只读核对取得专用租约，不把任务改回 ``running``。

        自动核对不能复用通用 ``TaskExecutionStore.acquire``：后者会把任务推进到
        ``running``，并可能让普通 Runner 在节点返回后写入成功。这里沿用
        Task→Approval→ToolExecution→本地动作→Connection 的固定锁序，仅在
        ``reconciling`` 状态且调度到期时设置 owner，随后由专用 worker 调用
        ``TrustedActionExecutionUseCase.reconcile``。
        """
        now = utc_instant(now, field="now")
        lease_expires_at = utc_instant(lease_expires_at, field="lease_expires_at")
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be later than now")
        if type(lease_owner) is not str or not lease_owner or lease_owner != lease_owner.strip():
            raise ValueError("lease_owner is invalid")
        task = await self._session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == task_id).with_for_update()
        )
        if (
            task is None
            or task.kind != "trusted_action"
            or task.status != TaskStatus.RECONCILING.value
            or task.scheduled_for is None
            or task.scheduled_for > now
            or (
                task.lease_owner is not None
                and (task.lease_expires_at is None or task.lease_expires_at > now)
            )
        ):
            return None
        raw_approval_id = task.input_payload.get("approval_id")
        raw_operation_id = task.input_payload.get("operation_id")
        if (
            set(task.input_payload) != {"approval_id", "operation_id"}
            or type(raw_approval_id) is not str
            or type(raw_operation_id) is not str
        ):
            return None
        try:
            approval_id = UUID(raw_approval_id)
            operation_id = UUID(raw_operation_id)
        except ValueError:
            return None
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .where(ApprovalRequestModel.id == approval_id, ApprovalRequestModel.task_id == task.id)
            .with_for_update()
        )
        execution = await self._session.scalar(
            select(ToolExecutionModel)
            .where(
                ToolExecutionModel.task_id == task.id,
                ToolExecutionModel.operation_id == operation_id,
            )
            .with_for_update()
        )
        if (
            approval is None
            or execution is None
            or approval.status != ApprovalStatus.APPROVED.value
            or execution.status != ToolExecutionStatus.RECONCILING.value
            or execution.request_started_at is None
            or execution.write_attempt_count <= 0
            or approval.schema_version is None
            or not _task_operation_matches(task, operation_id)
        ):
            return None
        binding = await self._lock_action_binding(task=task, approval=approval)
        if binding is None:
            return None
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == binding[0],
                OAuthConnectionModel.user_id == task.user_id,
            )
            .with_for_update()
        )
        if connection is None:
            return None
        if binding[1] is not None:
            calendar = await self._session.scalar(
                select(ProviderCalendarModel)
                .where(
                    ProviderCalendarModel.user_id == task.user_id,
                    ProviderCalendarModel.connection_id == binding[0],
                    ProviderCalendarModel.provider_calendar_id == binding[1],
                )
                .with_for_update()
            )
            if calendar is None:
                return None
        task.lease_owner = lease_owner
        task.lease_expires_at = lease_expires_at
        task.current_step = "reconcile"
        await self._session.flush()
        return await self.load_dispatch(
            task_id=task.id,
            approval_id=approval.id,
            operation_id=operation_id,
        )

    async def release_reconciliation_lease(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        now: datetime,
    ) -> bool:
        """在只读核对异常时安全释放专用租约并保留 ``reconciling`` 事实。

        只有仍绑定同一 owner 的任务可以被释放；若另一个 Worker 已经接管，旧异常路径
        不得清除新租约。调度时间被压到 ``now``，让下一次 PostgreSQL 恢复扫描能够重新
        投递，而不会把未确定结果误写成成功或普通失败。
        """
        now = utc_instant(now, field="now")
        task = await self._session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == task_id).with_for_update()
        )
        if (
            task is None
            or task.kind != "trusted_action"
            or task.status != TaskStatus.RECONCILING.value
            or task.lease_owner != lease_owner
        ):
            return False
        task.lease_owner = None
        task.lease_expires_at = None
        if task.scheduled_for is None or task.scheduled_for > now:
            task.scheduled_for = now
        await self._session.flush()
        return True

    async def recover_due_reconciliations(self, *, now: datetime, limit: int) -> int:
        """从 PostgreSQL 补建因 Redis 丢失而缺失的只读核对投递事实。

        ``scheduled_for`` 是唯一到期来源；每个任务/计数使用确定性去重键，因此扫描器
        重跑不会制造第二条 refresh 或第二次供应商写入。状态始终保持 ``reconciling``，
        不会被普通任务恢复器伪装成可写的 ``queued``。
        """
        return await _recover_due_reconciliations_in_session(
            self._session,
            now=now,
            limit=limit,
        )

    async def _enqueue_source_refresh(
        self,
        *,
        task: TaskRunModel,
        approval: ApprovalRequestModel,
        execution: ToolExecutionModel,
        connection: OAuthConnectionModel,
        calendar_id: str | None,
        available_at: datetime,
    ) -> None:
        """为已确认应用结果建立一次去重的只读同步意图。

        这里只创建 ``sync_mail``/``sync_calendar`` 任务和其 Outbox，不写
        ``EmailMessage`` 或 ``CalendarEvent``。实际 provider refresh 仍由现有只读同步
        worker 规范化真实资源后更新来源表，避免从批准命令伪造供应商行。
        """
        if approval.proposal_kind == "mail_draft":
            kind = "sync_mail"
            scope_key = "sentitems" if connection.provider == "microsoft" else "mailbox"
        elif approval.proposal_kind == "calendar_proposal" and calendar_id:
            kind = "sync_calendar"
            scope_key = calendar_id
        else:
            return
        digest = sha256(scope_key.encode("utf-8")).hexdigest()[:16]
        idempotency_key = f"action-refresh:{execution.id}:{kind}:{digest}"
        refresh_id = uuid4()
        result = await self._session.execute(
            insert(TaskRunModel)
            .values(
                id=refresh_id,
                user_id=task.user_id,
                kind=kind,
                status=TaskStatus.CREATED.value,
                idempotency_key=idempotency_key,
                input_payload={
                    "connection_id": str(connection.id),
                    "scope_key": scope_key,
                },
            )
            .on_conflict_do_nothing(constraint="uq_task_runs_user_id_idempotency_key")
            .returning(TaskRunModel.id)
        )
        inserted_id = result.scalar_one_or_none()
        if inserted_id is None:
            return
        refresh_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=inserted_id,
            event_type="task.created",
            actor_type="system",
            actor_id=None,
            event_metadata={"reason": "trusted_action_source_refresh", "kind": kind},
        )
        self._session.add(refresh_audit)
        await self._session.flush()
        self._session.add(
            OutboxEventModel(
                topic="task.execute",
                aggregate_id=inserted_id,
                deduplication_key=f"task.execute:{inserted_id}:initial",
                payload={"task_id": str(inserted_id)},
                available_at=available_at,
            )
        )

    async def abandon_started_attempt(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
    ) -> bool:
        """以 request-start 前 dispatch 冻结投影释放中断租约且不伪造结果。

        Args:
            snapshot: 当前进程在 request-start 前读取的完整内容无关绑定；不进入
                LangGraph checkpoint，也不包含命令、密文或用户正文。
            lease_owner: 被异常、取消或超时中断的当前 Worker owner。

        Returns:
            已确认存在同一未决写尝试且租约已释放（或先前已由同一路径释放）时为
            ``True``；事实不匹配、请求尚未开始或另一个 owner 已接管时为 ``False``。

        Notes:
            TaskRun 保持 ``running``，ToolExecution 与本地动作保持 ``executing``。
            只有当前 owner 的租约在锁后数据库时间仍严格有效时才可首次释放；随后
            ``lease_expires_at`` 写为该数据库时间而不是 ``NULL``，让 acquisition CAS
            可立即接管并让同一 abandon 幂等重放，同时不创建 Task 20 核对事件。
        """
        task, approval = await self._locked_task_approval(
            task_id=snapshot.task_id,
            approval_id=snapshot.approval_id,
        )
        execution = await self._session.scalar(
            select(ToolExecutionModel)
            .where(ToolExecutionModel.id == snapshot.execution.execution_id)
            .with_for_update()
        )
        if task is None or approval is None or execution is None:
            return False
        binding = await self._lock_action_binding(task=task, approval=approval)
        connection = (
            await self._session.scalar(
                select(OAuthConnectionModel)
                .where(
                    OAuthConnectionModel.id == snapshot.connection_id,
                    OAuthConnectionModel.user_id == snapshot.user_id,
                )
                .with_for_update()
            )
            if snapshot.connection_id is not None
            else None
        )
        database_now = await self._session.scalar(select(func.clock_timestamp()))
        if binding is None or connection is None or database_now is None:
            return False
        try:
            execution_status = ToolExecutionStatus(execution.status)
        except ValueError:
            return False
        action_connection_id, action_calendar_id, action_status = binding
        unresolved = (
            task.kind == "trusted_action"
            and task.status == TaskStatus.RUNNING.value
            and task.user_id == snapshot.user_id
            and _task_operation_matches(task, snapshot.operation_id)
            and approval.id == snapshot.approval_id
            and approval.status == ApprovalStatus.APPROVED.value
            and approval.step_id == snapshot.step_id
            and approval.action == snapshot.action
            and approval.schema_version == snapshot.schema_version
            and approval.version == snapshot.approval_version
            and approval.proposal_kind == snapshot.proposal_kind
            and approval.proposal_id == snapshot.proposal_id
            and approval.proposal_version == snapshot.proposal_version
            and compare_digest(approval.payload_hash, snapshot.payload_hash)
            and action_connection_id == snapshot.connection_id
            and action_calendar_id == snapshot.calendar_id
            and action_status
            in {
                MailDraftStatus.EXECUTING.value,
                CalendarProposalStatus.EXECUTING.value,
            }
            and execution_status
            in {
                ToolExecutionStatus.EXECUTING,
                ToolExecutionStatus.RECONCILING,
            }
            and execution.request_started_at is not None
            and execution.write_attempt_count > 0
            and snapshot.execution.approval_id == snapshot.approval_id
            and trusted_execution_binding_matches(
                execution_task_id=execution.task_id,
                expected_task_id=snapshot.task_id,
                execution_step_id=execution.step_id,
                expected_step_id=snapshot.step_id,
                execution_operation_id=execution.operation_id,
                expected_operation_id=snapshot.operation_id,
                execution_provider=execution.provider,
                expected_provider=snapshot.provider,
                execution_tool_name=execution.tool_name,
                expected_action=snapshot.action,
                execution_idempotency_key=execution.idempotency_key,
                approval_id=snapshot.approval_id,
                approval_version=snapshot.approval_version,
                execution_payload_hash=execution.request_payload_hash,
                expected_payload_hash=snapshot.payload_hash,
            )
            and connection.provider == snapshot.provider
        )
        if not unresolved:
            return False
        if task.lease_owner == lease_owner:
            if task.lease_expires_at is None or task.lease_expires_at <= database_now:
                return False
            task.lease_owner = None
            task.lease_expires_at = database_now
            await self._session.flush()
            return True
        return (
            task.lease_owner is None
            and task.lease_expires_at is not None
            and task.lease_expires_at <= database_now
        )

    async def fail_claimed_integrity(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        failed_at: datetime,
        lease_owner: str,
    ) -> None:
        """在零 provider 调用边界把命令认证失败持久化为明确本地失败。

        终态写入仍属于当前 DurableTaskRunner 的租约权限：TaskRun 与关联行锁定后，
        必须以 PostgreSQL ``clock_timestamp()`` 证明 owner 租约严格晚于数据库当前
        时间和调用方完成时间。过期、恰好到期或已被接管的 owner 只能 fail closed，
        不能留下可信动作终态、审计或 Outbox。
        """
        failed_at = utc_instant(failed_at, field="failed_at")
        task, approval = await self._locked_task_approval(
            task_id=snapshot.task_id,
            approval_id=snapshot.approval_id,
        )
        execution = await self._session.scalar(
            select(ToolExecutionModel)
            .where(ToolExecutionModel.id == snapshot.execution.execution_id)
            .with_for_update()
        )
        if task is None or approval is None or execution is None:
            raise _trusted_action_unavailable()
        if execution.request_started_at is not None:
            raise _trusted_action_unavailable()
        locked_action = await self._lock_action_record(task=task, approval=approval)
        if locked_action is None:
            raise _trusted_action_unavailable()
        action, action_connection_id, action_calendar_id, action_status = locked_action
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == action_connection_id,
                OAuthConnectionModel.user_id == task.user_id,
            )
            .with_for_update()
        )
        calendar = None
        if action_calendar_id is not None:
            calendar = await self._session.scalar(
                select(ProviderCalendarModel)
                .where(
                    ProviderCalendarModel.user_id == task.user_id,
                    ProviderCalendarModel.connection_id == action_connection_id,
                    ProviderCalendarModel.provider_calendar_id == action_calendar_id,
                )
                .with_for_update()
            )
        database_now = await self._session.scalar(select(func.clock_timestamp()))
        if database_now is None or connection is None:
            raise _trusted_action_unavailable()
        if (
            task.status != TaskStatus.RUNNING.value
            or task.lease_owner != lease_owner
            or task.lease_expires_at is None
            or task.lease_expires_at <= database_now
            or task.lease_expires_at <= failed_at
            or task.kind != "trusted_action"
            or task.user_id != snapshot.user_id
            or not _task_operation_matches(task, snapshot.operation_id)
            or approval.step_id != snapshot.step_id
            or approval.action != snapshot.action
            or approval.schema_version != snapshot.schema_version
            or approval.version != snapshot.approval_version
            or approval.proposal_kind != snapshot.proposal_kind
            or approval.proposal_id != snapshot.proposal_id
            or approval.proposal_version != snapshot.proposal_version
            or not compare_digest(approval.payload_hash, snapshot.payload_hash)
            or action_connection_id != snapshot.connection_id
            or action_calendar_id != snapshot.calendar_id
            or action_status
            not in {
                MailDraftStatus.EXECUTING.value,
                CalendarProposalStatus.EXECUTING.value,
            }
            or connection.provider != snapshot.provider
            or (action_calendar_id is not None and calendar is None)
            or not trusted_execution_binding_matches(
                execution_task_id=execution.task_id,
                expected_task_id=snapshot.task_id,
                execution_step_id=execution.step_id,
                expected_step_id=snapshot.step_id,
                execution_operation_id=execution.operation_id,
                expected_operation_id=snapshot.operation_id,
                execution_provider=execution.provider,
                expected_provider=snapshot.provider,
                execution_tool_name=execution.tool_name,
                expected_action=snapshot.action,
                execution_idempotency_key=execution.idempotency_key,
                approval_id=snapshot.approval_id,
                approval_version=snapshot.approval_version,
                execution_payload_hash=execution.request_payload_hash,
                expected_payload_hash=snapshot.payload_hash,
            )
        ):
            raise _trusted_action_unavailable()
        # 所有可能阻塞的关联行已在上方锁定；在任何 ORM mutation 前再次确认同一严格
        # lease 谓词，保留 fail-closed 边界并使该检查与最终 flush 位于同一事务。
        if (
            task.lease_expires_at is None
            or task.lease_expires_at <= database_now
            or task.lease_expires_at <= failed_at
        ):
            raise _trusted_action_unavailable()
        execution.status = ToolExecutionStatus.CONFIRMED_FAILED.value
        execution.error_code = "trusted_action_unavailable"
        execution.completed_at = failed_at
        execution.result_summary = {
            "kind": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
            "retryable": False,
        }
        approval.status = ApprovalStatus.INVALIDATED.value
        task.status = TaskStatus.FAILED.value
        task.error_code = "trusted_action_unavailable"
        task.finished_at = failed_at
        _clear_task_scheduling(task)
        await self._set_local_action_status(
            task=task,
            approval=approval,
            mail_status=MailDraftStatus.EDITING,
            calendar_status=CalendarProposalStatus.EDITING,
            allowed_current={
                MailDraftStatus.EXECUTING.value,
                CalendarProposalStatus.EXECUTING.value,
            },
            locked_action=action,
        )
        task_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="task.failed",
            actor_type="worker",
            actor_id=lease_owner,
            event_metadata={"error_code": "trusted_action_unavailable"},
        )
        tool_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="tool.confirmed_failed",
            actor_type="worker",
            actor_id=None,
            event_metadata={
                "action": snapshot.action,
                "provider": snapshot.provider,
                "outcome": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
                "write_attempt_count": execution.write_attempt_count,
            },
        )
        self._session.add_all((task_audit, tool_audit))
        await self._session.flush()
        self._session.add(
            OutboxEventModel(
                topic="tool.confirmed_failed",
                aggregate_id=task.id,
                deduplication_key=f"tool.confirmed_failed:{execution.id}:integrity",
                payload={"task_id": str(task.id), "audit_event_id": tool_audit.id},
                available_at=failed_at,
            )
        )

    async def finalize_rejected(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        lease_owner: str,
        finished_at: datetime,
    ) -> None:
        """拒绝分支完成任务但绝不创建 ToolExecution 或改变供应商。

        拒绝也必须由当前仍持有的 live lease 提交。固定锁序取得 TaskRun 与
        ApprovalRequest 后才采样 PostgreSQL 当前时间，并要求租约严格晚于该时间及
        ``finished_at``；任何 miss 都回滚整个事务，避免旧 owner 伪造成功时间线。
        """
        finished_at = utc_instant(finished_at, field="finished_at")
        task, approval = await self._locked_task_approval(
            task_id=task_id,
            approval_id=approval_id,
        )
        database_now = await self._session.scalar(select(func.clock_timestamp()))
        if (
            task is None
            or approval is None
            or database_now is None
            or not _task_operation_matches(task, operation_id)
            or approval.status != ApprovalStatus.REJECTED.value
            or task.status != TaskStatus.RUNNING.value
            or task.lease_owner != lease_owner
            or task.lease_expires_at is None
            or task.lease_expires_at <= database_now
            or task.lease_expires_at <= finished_at
        ):
            raise _trusted_action_unavailable()
        task.status = TaskStatus.SUCCEEDED.value
        task.error_code = None
        task.finished_at = finished_at
        _clear_task_scheduling(task)
        # 拒绝分支在本事务内直接清除租约；通用 Runner 随后的成功 CAS 必然未命中，
        # 所以必须在这里追加且只追加一次 task.succeeded 时间线事实。
        self._session.add(
            AuditEventModel(
                user_id=task.user_id,
                task_id=task.id,
                event_type="task.succeeded",
                actor_type="worker",
                actor_id=lease_owner,
                event_metadata={"reason": "approval_rejected"},
            )
        )

    async def _locked_task_approval(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
    ) -> tuple[TaskRunModel | None, ApprovalRequestModel | None]:
        """复用 Task→Approval 固定锁序读取两个可信生命周期行。"""
        task = await self._session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == task_id).with_for_update()
        )
        if task is None:
            return None, None
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .where(
                ApprovalRequestModel.id == approval_id,
                ApprovalRequestModel.task_id == task_id,
            )
            .with_for_update()
        )
        return task, approval

    async def _lock_action_binding(
        self,
        *,
        task: TaskRunModel,
        approval: ApprovalRequestModel,
    ) -> tuple[UUID, str | None, str] | None:
        """在 ToolExecution 后锁定本地动作并返回连接、日历和当前状态。"""
        locked = await self._lock_action_record(task=task, approval=approval)
        if locked is None:
            return None
        _, connection_id, calendar_id, status = locked
        return connection_id, calendar_id, status

    async def _lock_action_record(
        self,
        *,
        task: TaskRunModel,
        approval: ApprovalRequestModel,
    ) -> (
        tuple[
            MailDraftModel | CalendarChangeProposalModel,
            UUID,
            str | None,
            str,
        ]
        | None
    ):
        """按固定锁序锁定本地动作，并返回可复用的 ORM 行及其绑定投影。

        返回的 action 已经由当前事务持有 ``FOR UPDATE`` 锁。需要在锁后采样数据库时间
        的终态路径必须复用该对象，不能先采样再让状态 setter 重新等待同一行，否则旧
        owner 可能在等待期间越过 lease 截止时间后仍提交可信终态。
        """
        if (
            approval.proposal_id is None
            or approval.proposal_version is None
            or approval.proposal_kind not in {"mail_draft", "calendar_proposal"}
        ):
            return None
        if approval.proposal_kind == "mail_draft":
            draft = await self._session.scalar(
                select(MailDraftModel)
                .where(
                    MailDraftModel.id == approval.proposal_id,
                    MailDraftModel.user_id == task.user_id,
                )
                .with_for_update()
            )
            if draft is None or draft.current_version != approval.proposal_version:
                return None
            return draft, draft.connection_id, None, draft.status
        proposal = await self._session.scalar(
            select(CalendarChangeProposalModel)
            .where(
                CalendarChangeProposalModel.id == approval.proposal_id,
                CalendarChangeProposalModel.user_id == task.user_id,
            )
            .with_for_update()
        )
        if proposal is None or proposal.current_version != approval.proposal_version:
            return None
        return proposal, proposal.connection_id, proposal.calendar_id, proposal.status

    async def _action_connection_id(
        self,
        *,
        task: TaskRunModel,
        approval: ApprovalRequestModel,
    ) -> UUID | None:
        """只读解析审批绑定的连接 ID，不解密命令或信任供应商显示字段。"""
        binding = await self._lock_action_binding(task=task, approval=approval)
        return binding[0] if binding is not None else None

    async def _set_local_action_status(
        self,
        *,
        task: TaskRunModel,
        approval: ApprovalRequestModel,
        mail_status: MailDraftStatus,
        calendar_status: CalendarProposalStatus,
        allowed_current: set[str],
        locked_action: MailDraftModel | CalendarChangeProposalModel | None = None,
    ) -> None:
        """只改写仍绑定冻结版本且处于预期状态的本地动作头。

        ``locked_action`` 用于已经按 Task→Approval→ToolExecution→action 顺序锁定的
        终态路径；传入时只做内存校验和 mutation，避免重新发出可能阻塞的 ``FOR UPDATE``
        查询。未传入时保留原有查询行为，供 claim/其它生命周期路径使用。
        """
        if locked_action is not None:
            if isinstance(locked_action, MailDraftModel):
                if (
                    approval.proposal_kind != "mail_draft"
                    or approval.proposal_id != locked_action.id
                    or locked_action.user_id != task.user_id
                    or locked_action.current_version != approval.proposal_version
                    or locked_action.status not in allowed_current
                ):
                    raise _trusted_action_unavailable()
                locked_action.status = mail_status.value
                return
            if (
                approval.proposal_kind != "calendar_proposal"
                or approval.proposal_id != locked_action.id
                or locked_action.user_id != task.user_id
                or locked_action.current_version != approval.proposal_version
                or locked_action.status not in allowed_current
            ):
                raise _trusted_action_unavailable()
            locked_action.status = calendar_status.value
            return
        if approval.proposal_kind == "mail_draft" and approval.proposal_id is not None:
            draft = await self._session.scalar(
                select(MailDraftModel)
                .where(
                    MailDraftModel.id == approval.proposal_id,
                    MailDraftModel.user_id == task.user_id,
                )
                .with_for_update()
            )
            if (
                draft is None
                or draft.current_version != approval.proposal_version
                or draft.status not in allowed_current
            ):
                raise _trusted_action_unavailable()
            draft.status = mail_status.value
            return
        if approval.proposal_kind == "calendar_proposal" and approval.proposal_id is not None:
            proposal = await self._session.scalar(
                select(CalendarChangeProposalModel)
                .where(
                    CalendarChangeProposalModel.id == approval.proposal_id,
                    CalendarChangeProposalModel.user_id == task.user_id,
                )
                .with_for_update()
            )
            if (
                proposal is None
                or proposal.current_version != approval.proposal_version
                or proposal.status not in allowed_current
            ):
                raise _trusted_action_unavailable()
            proposal.status = calendar_status.value
            return
        raise _trusted_action_unavailable()

    async def _set_snapshot_action_status(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        mail_status: MailDraftStatus,
        calendar_status: CalendarProposalStatus,
        allowed_current: set[str],
    ) -> None:
        """按 dispatch 原始本地标识收敛失效状态，不信任已被篡改的审批投影。"""
        if snapshot.proposal_kind == "mail_draft":
            draft = await self._session.scalar(
                select(MailDraftModel)
                .where(
                    MailDraftModel.id == snapshot.proposal_id,
                    MailDraftModel.user_id == snapshot.user_id,
                )
                .with_for_update()
            )
            if draft is None:
                raise _trusted_action_unavailable()
            if draft.status == mail_status.value:
                return
            if draft.status not in allowed_current:
                raise _trusted_action_unavailable()
            draft.status = mail_status.value
            return
        if snapshot.proposal_kind == "calendar_proposal":
            proposal = await self._session.scalar(
                select(CalendarChangeProposalModel)
                .where(
                    CalendarChangeProposalModel.id == snapshot.proposal_id,
                    CalendarChangeProposalModel.user_id == snapshot.user_id,
                )
                .with_for_update()
            )
            if proposal is None:
                raise _trusted_action_unavailable()
            if proposal.status == calendar_status.value:
                return
            if proposal.status not in allowed_current:
                raise _trusted_action_unavailable()
            proposal.status = calendar_status.value
            return
        raise _trusted_action_unavailable()

    async def _capabilities(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        lock_rows: bool = False,
    ) -> dict[ConnectionCapability, tuple[CapabilityStatus, str | None]]:
        """读取当前能力状态；claim 可按 capability 名称稳定加行锁。

        Args:
            user_id: 能力所属用户。
            connection_id: 能力所属 OAuth 连接。
            lock_rows: 为 ``True`` 时在 connection 行之后锁定全部现存能力行。

        Returns:
            已解析能力到状态及稳定错误码的映射；未知枚举行 fail closed 忽略。
        """
        statement = (
            select(ConnectionCapabilityModel)
            .where(
                ConnectionCapabilityModel.user_id == user_id,
                ConnectionCapabilityModel.connection_id == connection_id,
            )
            .order_by(ConnectionCapabilityModel.capability)
        )
        if lock_rows:
            statement = statement.with_for_update()
        rows = tuple((await self._session.scalars(statement)).all())
        result: dict[ConnectionCapability, tuple[CapabilityStatus, str | None]] = {}
        for row in rows:
            try:
                capability = ConnectionCapability(row.capability)
                status = CapabilityStatus(row.status)
            except ValueError:
                continue
            result[capability] = (status, row.last_error_code)
        return result

    async def _reply_headers(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        mode: MailMode,
        source_thread_id: str | None,
        source_message_id: str | None,
    ) -> ReplyThreadHeaders | None:
        """从仍属于冻结 provider thread 的精确消息构造回复 Header。

        邮件 immutable ID 允许同步时改挂到另一个线程，因此只按 message ID 读取会把旧草稿
        静默绑定到新线程。回复与全部回复必须同时重证用户、连接、provider message ID 和
        草稿保存的 provider thread ID；任一事实变化都返回空投影，由应用层稳定拒绝冻结。
        """
        if mode is MailMode.NEW:
            return None
        if not source_thread_id or not source_message_id:
            return None
        message = await self._session.scalar(
            select(EmailMessageModel)
            .join(
                EmailThreadModel,
                (EmailThreadModel.id == EmailMessageModel.thread_id)
                & (EmailThreadModel.user_id == EmailMessageModel.user_id)
                & (EmailThreadModel.connection_id == EmailMessageModel.connection_id),
            )
            .where(
                EmailMessageModel.user_id == user_id,
                EmailMessageModel.connection_id == connection_id,
                EmailMessageModel.provider_message_id == source_message_id,
                EmailThreadModel.user_id == user_id,
                EmailThreadModel.connection_id == connection_id,
                EmailThreadModel.provider_thread_id == source_thread_id,
            )
        )
        if message is None:
            return None
        in_reply_to = message.internet_message_id or message.headers.get("message-id")
        if type(in_reply_to) is not str:
            return None
        raw_references = message.headers.get("references")
        references = tuple(raw_references.split()) if isinstance(raw_references, str) else ()
        if not references or references[-1] != in_reply_to:
            references = (*references, in_reply_to)
        try:
            return ReplyThreadHeaders(in_reply_to=in_reply_to, references=references)
        except ValueError:
            return None

    async def save_command(
        self,
        *,
        user_id: UUID,
        approval_id: UUID,
        command_payload: Mapping[str, object],
    ) -> dict[str, object] | None:
        """锁定用户拥有的审批并且只允许首次写入规范命令密文。

        命令先经过现有严格 Pydantic/领域边界规范化，再由现有
        ``trusted_command_hash`` 计算冻结哈希；动作和 Schema 只从规范命令中取得，
        不接受调用方提供第二套可能分叉的哈希或 AAD 元数据。精确 encrypted marker
        本身就是不可变冻结事实：完整 AEAD 存在时，同命令重放复用既有密文；保留任务
        已清除三元组时，任何命令都拒绝，不能旋转 nonce 或重新生成已丢失的冻结内容。
        首次冻结只接受 pending、完全未决定、无执行截止、空 JSONB、空 AEAD 且哈希已
        预绑定到当前规范命令的精确骨架；Repository 绝不把异常生命周期或旧哈希改写成
        新的冻结事实。

        Args:
            user_id: 当前认证用户，用于通过审批所属 TaskRun 验证所有权。
            approval_id: 待绑定真实命令的 ApprovalRequest ID。
            command_payload: 四种 M2 可信命令之一的标准 JSON object。

        Returns:
            已规范化并写入的命令副本；审批不存在或跨用户时返回 ``None``。

        Raises:
            StateConflictError: 审批行预声明的 action/schema 与命令不一致。
            TypeError: 命令包含非标准 JSON Python 值。
            ValueError: 命令违反严格 Schema 或领域不变量。
        """
        canonical = _canonical_command_object(command_payload)
        action = canonical.get("action")
        schema_version = canonical.get("schema_version")
        if type(action) is not str or type(schema_version) is not str:
            # 正常严格边界不可能到达这里；保留固定失败避免未来 Schema 漏掉绑定字段。
            raise _trusted_action_unavailable()
        payload_hash = trusted_command_hash(canonical)
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
            .where(
                ApprovalRequestModel.id == approval_id,
                TaskRunModel.user_id == user_id,
            )
            .with_for_update()
        )
        if approval is None:
            return None
        if approval.action != action or approval.schema_version != schema_version:
            raise _trusted_action_unavailable()

        marker: dict[str, JsonValue] = {
            "storage": "encrypted",
            "schema_version": schema_version,
        }
        stored_aead = (
            approval.payload_ciphertext,
            approval.payload_nonce,
            approval.payload_key_version,
        )
        if approval.payload == marker:
            if not all(value is not None for value in stored_aead):
                # 精确 marker 即使在保留清理后也持续证明载荷已冻结，不能退回首次写入分支。
                raise _trusted_action_unavailable()
            existing = self._load_m2_command(approval=approval, user_id=user_id)
            if compare_digest(trusted_command_hash(existing), payload_hash):
                # 同命令重放只返回既有冻结事实，绝不旋转 nonce 或改写已批准载荷。
                return existing
            raise _trusted_action_unavailable()
        if (
            approval.status != ApprovalStatus.PENDING.value
            or approval.decided_at is not None
            or approval.decided_by_user_id is not None
            or approval.approved_execution_deadline_at is not None
            or approval.payload != {}
            or any(value is not None for value in stored_aead)
            or not compare_digest(approval.payload_hash, payload_hash)
        ):
            # 非精确 pending 骨架可能是已决定、被替换或旧命令事实，必须整体 fail closed。
            raise _trusted_action_unavailable()

        encrypted = self._cipher.encrypt_json(
            canonical,
            user_id=user_id,
            record_id=approval.id,
            content_kind=APPROVAL_COMMAND_CONTENT_KIND,
            action=action,
            schema_version=schema_version,
        )
        # JSONB 只留下明确存储协议 marker；地址、正文和日程字段全部只进入 AEAD 列。
        approval.payload = marker
        approval.payload_ciphertext = encrypted.ciphertext
        approval.payload_nonce = encrypted.nonce
        approval.payload_key_version = encrypted.key_version
        approval.payload_hash = payload_hash
        await self._session.flush()
        return canonical

    async def load_command(
        self,
        *,
        user_id: UUID,
        approval_id: UUID,
    ) -> dict[str, object] | None:
        """按用户读取并验证 legacy fake.write 或完整 M2 加密命令。

        Args:
            user_id: 当前认证用户，用于通过 TaskRun 显式隔离审批。
            approval_id: 待读取 ApprovalRequest ID。

        Returns:
            已复制的 legacy fake payload 或重新规范化的 M2 命令；不存在、跨用户时
            返回 ``None``。

        Raises:
            StateConflictError: marker、AEAD 列、action/schema、规范哈希或 legacy
                分支不符合冻结协议。
            cryptography.exceptions.InvalidTag: 密文或任一 AAD 维度被替换。
        """
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
            .where(
                ApprovalRequestModel.id == approval_id,
                TaskRunModel.user_id == user_id,
            )
        )
        if approval is None:
            return None
        if approval.schema_version is None:
            return self._load_legacy_fake_write(approval)
        return self._load_m2_command(approval=approval, user_id=user_id)

    def _load_legacy_fake_write(
        self,
        approval: ApprovalRequestModel,
    ) -> dict[str, object]:
        """只允许无 AEAD 列的 M1 fake.write 使用历史 JSONB payload。"""
        if approval.action != "fake.write" or any(
            value is not None
            for value in (
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            )
        ):
            raise _trusted_action_unavailable()
        proposal = ApprovalProposal.create(approval.action, approval.payload)
        if not compare_digest(proposal.payload_hash, approval.payload_hash):
            raise _trusted_action_unavailable()
        return dict(approval.payload)

    def _load_m2_command(
        self,
        *,
        approval: ApprovalRequestModel,
        user_id: UUID,
    ) -> dict[str, object]:
        """要求精确 marker/AEAD 三元组并重新验证完整可信命令与哈希。"""
        schema_version = approval.schema_version
        if schema_version is None:
            # 调用方已经分支；这里保留局部防线，也让类型系统证明 AAD 不接收空版本。
            raise _trusted_action_unavailable()
        marker = {
            "storage": "encrypted",
            "schema_version": schema_version,
        }
        if approval.payload != marker or (
            approval.payload_ciphertext is None
            or approval.payload_nonce is None
            or approval.payload_key_version is None
        ):
            raise _trusted_action_unavailable()
        payload = self._cipher.decrypt_json(
            EncryptedValue(
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            ),
            user_id=user_id,
            record_id=approval.id,
            content_kind=APPROVAL_COMMAND_CONTENT_KIND,
            action=approval.action,
            schema_version=schema_version,
        )
        if (
            payload.get("action") != approval.action
            or payload.get("schema_version") != schema_version
        ):
            raise _trusted_action_unavailable()
        try:
            canonical = _canonical_command_object(payload)
            canonical_hash = trusted_command_hash(canonical)
        except (TypeError, ValueError):
            # 严格命令边界异常不能携带解密内容跨出 Repository，也不能变成 provider retry。
            raise _trusted_action_unavailable() from None
        if (
            canonical.get("action") != approval.action
            or canonical.get("schema_version") != schema_version
            or not compare_digest(canonical_hash, approval.payload_hash)
        ):
            raise _trusted_action_unavailable()
        return canonical


async def _recover_due_reconciliations_in_session(
    session: AsyncSession,
    *,
    now: datetime,
    limit: int,
) -> int:
    """在调用方事务内补建到期核对 Outbox，且完全不需要命令解密密钥。

    调度器只负责恢复 PostgreSQL 中已经存在的 ``reconciling`` 事实，并不读取冻结命令；
    因此这段 SQL 与含 AEAD 的可信动作 repository 分离成共享 helper。任务状态、核对
    次数和到期时间组成稳定去重边界，重复扫描只会看到已有未发布 ``task.execute`` 而跳过。
    """
    now = utc_instant(now, field="now")
    if limit <= 0:
        raise ValueError("limit must be positive")
    pending_task_execute = exists(
        select(OutboxEventModel.id).where(
            OutboxEventModel.aggregate_id == TaskRunModel.id,
            OutboxEventModel.topic == "task.execute",
            OutboxEventModel.published_at.is_(None),
        )
    )
    malformed_lease = and_(
        TaskRunModel.lease_owner.is_not(None),
        TaskRunModel.lease_expires_at.is_(None),
    )
    tasks = tuple(
        (
            await session.scalars(
                select(TaskRunModel)
                .where(
                    TaskRunModel.kind == "trusted_action",
                    TaskRunModel.status == TaskStatus.RECONCILING.value,
                    TaskRunModel.scheduled_for.is_not(None),
                    TaskRunModel.scheduled_for <= now,
                    or_(
                        malformed_lease,
                        TaskRunModel.lease_owner.is_(None),
                        TaskRunModel.lease_expires_at <= now,
                    ),
                    # 损坏的 owner-without-expiry 即使已有未发布消息也必须先清理，
                    # 否则 claim 会永久拒绝该任务；正常/过期租约仍沿用原有 pending
                    # Outbox 去重条件。
                    or_(malformed_lease, ~pending_task_execute),
                )
                .order_by(TaskRunModel.scheduled_for, TaskRunModel.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    recovered = 0
    for task in tasks:
        is_malformed_lease = task.lease_owner is not None and task.lease_expires_at is None
        if is_malformed_lease:
            # 该形状无法证明任何 Worker 仍持有有效租约。清除 owner/expiry 只改变本地
            # 协调事实，不会触发 provider 调用；随后由同事务的 Outbox 或下一次扫描
            # 重新建立只读 claim。
            task.lease_owner = None
            task.lease_expires_at = None
            await session.flush()
            pending_event_id = await session.scalar(
                select(OutboxEventModel.id)
                .where(
                    OutboxEventModel.aggregate_id == task.id,
                    OutboxEventModel.topic == "task.execute",
                    OutboxEventModel.published_at.is_(None),
                )
                .limit(1)
            )
            if pending_event_id is not None:
                continue
        execution = await session.scalar(
            select(ToolExecutionModel).where(ToolExecutionModel.task_id == task.id)
        )
        raw_count: object = execution.reconciliation_attempt_count if execution is not None else 0
        scheduled_for = task.scheduled_for
        if scheduled_for is None:
            continue
        deduplication_key = (
            f"task.execute:{task.id}:reconcile-recovery:{raw_count}:{scheduled_for.isoformat()}"
        )
        result = await session.execute(
            insert(OutboxEventModel)
            .values(
                topic="task.execute",
                aggregate_id=task.id,
                deduplication_key=deduplication_key,
                payload={"task_id": str(task.id)},
                available_at=scheduled_for,
            )
            .on_conflict_do_nothing(constraint="uq_outbox_events_deduplication_key")
            .returning(OutboxEventModel.id)
        )
        if result.scalar_one_or_none() is not None:
            recovered += 1
    return recovered


class SqlAlchemyTrustedActionReconciliationRecoveryStore:
    """只依赖 PostgreSQL 的核对恢复适配器，不读取应用主密钥。

    Scheduler 可能在密钥轮换、Secret 未挂载或 Worker 尚未启动时运行；恢复丢失的
    ``task.execute`` Outbox 只需要状态/调度列，所以单独的 store 能让这条维护路径继续
    工作，同时把任何 AEAD 解密严格留在实际 reconciliation Worker。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存会话工厂；每次扫描使用一个短提交事务。"""
        self._session_factory = session_factory

    async def recover_due_reconciliations(self, *, now: datetime, limit: int) -> int:
        """补建到期核对投递并返回本轮新增 Outbox 数量。"""
        async with self._session_factory.begin() as session:
            return await _recover_due_reconciliations_in_session(
                session,
                now=now,
                limit=limit,
            )


class SqlAlchemyTrustedActionTaskExecutionStore(SqlAlchemyTaskExecutionStore):
    """复用通用租约实现，并为 request-start 前失败提供可信动作原子终态。

    通用 Runner 只能看见 TaskRun，因此 resolver、主密钥、checkpoint 或总预算在真实
    请求开始前失败时，普通 ``finish()`` 会留下仍冻结的审批和本地动作。本实现只覆盖
    FAILED 完成：先按可信锁序证明请求从未开始，再把审批、任务、本地对象及可选 claim
    一起收敛；任何已存在 request-start 的尝试都返回 ``False``，禁止伪造外部结果。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存同一消息级连接池，同时复用现有 acquire/renew/retry 实现。"""
        super().__init__(session_factory)
        self._trusted_session_factory = session_factory

    async def get_authoritative_task_kind(self, *, task_id: UUID) -> str | None:
        """在独立短事务中锁定并读取权威 ``TaskRun.kind``。

        分类读取异常后不能把可能属于 trusted action 的任务交给通用失败 Runner。
        该方法沿用 TaskRun 的行锁边界，在提交前取得数据库中的不可变 kind；调用方
        只有拿到明确结果后才能选择 fake、trusted 或 M1 通用执行器。读取失败由调用方
        转为延期控制流，不在这里伪造任务终态。

        Args:
            task_id: 待分类的持久任务标识。

        Returns:
            当前 TaskRun.kind；任务不存在时返回 ``None``。
        """
        async with self._trusted_session_factory.begin() as session:
            return await session.scalar(
                select(TaskRunModel.kind).where(TaskRunModel.id == task_id).with_for_update()
            )

    async def get_authoritative_task_status(self, *, task_id: UUID) -> str | None:
        """在独立短事务中读取权威任务状态，供 M2 专用路由 fail closed。

        ``RECONCILING`` 不能经过通用 acquisition；该查询与 kind 查询分开保留窄接口，
        方便消息分类替身只实现自己需要的最小能力，同时不把 ORM 行泄露到 Worker。
        """
        async with self._trusted_session_factory.begin() as session:
            return await session.scalar(
                select(TaskRunModel.status).where(TaskRunModel.id == task_id).with_for_update()
            )

    async def finish(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        status: TaskStatus,
        finished_at: datetime,
        error_code: str | None,
        retry_recovery_at: datetime | None = None,
    ) -> bool:
        """FAILED 时原子收敛未开始请求的可信动作，其余状态保留通用语义。

        Args:
            task_id: 当前可信动作 TaskRun ID。
            lease_owner: DurableTaskRunner 当前 owner。
            status: Runner 请求写入的任务状态。
            finished_at: Runner 采样的带时区完成时间。
            error_code: 不含供应商内容的稳定失败码。
            retry_recovery_at: 仅通用 ``retry_scheduled`` 路径允许的恢复时间。

        Returns:
            原子可信终态已提交时为 ``True``；租约、绑定或 request-start 证明不成立时
            为 ``False``。非 FAILED 状态委托给通用 store。
        """
        if status is not TaskStatus.FAILED:
            return await super().finish(
                task_id=task_id,
                lease_owner=lease_owner,
                status=status,
                finished_at=finished_at,
                error_code=error_code,
                retry_recovery_at=retry_recovery_at,
            )
        failed_at = utc_instant(finished_at, field="finished_at")
        if retry_recovery_at is not None:
            raise ValueError("retry_recovery_at is only valid for retry_scheduled")
        if type(error_code) is not str or not error_code.strip():
            return False
        async with self._trusted_session_factory.begin() as session:
            return await self._fail_before_request(
                session=session,
                task_id=task_id,
                lease_owner=lease_owner,
                failed_at=failed_at,
                error_code=error_code,
            )

    @staticmethod
    async def _fail_before_request(
        *,
        session: AsyncSession,
        task_id: UUID,
        lease_owner: str,
        failed_at: datetime,
        error_code: str,
    ) -> bool:
        """在同一事务证明零写调用并收敛 Task、Approval、claim 与本地对象。"""
        task = await session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == task_id).with_for_update()
        )
        if task is None or task.kind != "trusted_action":
            return False
        raw_approval_id = task.input_payload.get("approval_id")
        raw_operation_id = task.input_payload.get("operation_id")
        if (
            set(task.input_payload) != {"approval_id", "operation_id"}
            or type(raw_approval_id) is not str
            or type(raw_operation_id) is not str
        ):
            return False
        try:
            approval_id = UUID(raw_approval_id)
            operation_id = UUID(raw_operation_id)
        except ValueError:
            return False
        approval = await session.scalar(
            select(ApprovalRequestModel)
            .where(
                ApprovalRequestModel.id == approval_id,
                ApprovalRequestModel.task_id == task.id,
            )
            .with_for_update()
        )
        if approval is None:
            return False
        execution = await session.scalar(
            select(ToolExecutionModel)
            .where(
                ToolExecutionModel.task_id == task.id,
                ToolExecutionModel.operation_id == operation_id,
            )
            .with_for_update()
        )

        action: MailDraftModel | CalendarChangeProposalModel | None = None
        connection_id: UUID | None = None
        if approval.proposal_kind == "mail_draft" and approval.proposal_id is not None:
            draft = await session.scalar(
                select(MailDraftModel)
                .where(
                    MailDraftModel.id == approval.proposal_id,
                    MailDraftModel.user_id == task.user_id,
                )
                .with_for_update()
            )
            if draft is not None and draft.current_version == approval.proposal_version:
                action = draft
                connection_id = draft.connection_id
        elif approval.proposal_kind == "calendar_proposal" and approval.proposal_id is not None:
            proposal = await session.scalar(
                select(CalendarChangeProposalModel)
                .where(
                    CalendarChangeProposalModel.id == approval.proposal_id,
                    CalendarChangeProposalModel.user_id == task.user_id,
                )
                .with_for_update()
            )
            if proposal is not None and proposal.current_version == approval.proposal_version:
                action = proposal
                connection_id = proposal.connection_id
        if action is None or connection_id is None:
            return False
        connection = await session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == task.user_id,
            )
            .with_for_update()
        )
        database_now = await session.scalar(select(func.clock_timestamp()))
        if connection is None or database_now is None:
            return False
        if (
            task.status != TaskStatus.RUNNING.value
            or task.lease_owner != lease_owner
            or task.lease_expires_at is None
            or task.lease_expires_at <= database_now
            or task.lease_expires_at <= failed_at
            or not _task_operation_matches(task, operation_id)
            or approval.status
            not in {
                ApprovalStatus.PENDING.value,
                ApprovalStatus.APPROVED.value,
            }
        ):
            return False

        expected_action_status = (
            MailDraftStatus.EXECUTING.value
            if isinstance(action, MailDraftModel) and execution is not None
            else CalendarProposalStatus.EXECUTING.value
            if isinstance(action, CalendarChangeProposalModel) and execution is not None
            else MailDraftStatus.AWAITING_APPROVAL.value
            if isinstance(action, MailDraftModel)
            else CalendarProposalStatus.AWAITING_APPROVAL.value
        )
        if action.status != expected_action_status:
            return False
        if execution is not None:
            try:
                execution_status = ToolExecutionStatus(execution.status)
            except ValueError:
                return False
            if (
                approval.status != ApprovalStatus.APPROVED.value
                or execution_status is not ToolExecutionStatus.CLAIMED
                or execution.request_started_at is not None
                or execution.write_attempt_count != 0
                or execution.result_summary is not None
                or not trusted_execution_binding_matches(
                    execution_task_id=execution.task_id,
                    expected_task_id=task.id,
                    execution_step_id=execution.step_id,
                    expected_step_id=approval.step_id,
                    execution_operation_id=execution.operation_id,
                    expected_operation_id=operation_id,
                    execution_provider=execution.provider,
                    expected_provider=connection.provider,
                    execution_tool_name=execution.tool_name,
                    expected_action=approval.action,
                    execution_idempotency_key=execution.idempotency_key,
                    approval_id=approval.id,
                    approval_version=approval.version,
                    execution_payload_hash=execution.request_payload_hash,
                    expected_payload_hash=approval.payload_hash,
                )
            ):
                return False

        approval.status = ApprovalStatus.INVALIDATED.value
        task.status = TaskStatus.FAILED.value
        task.error_code = error_code
        task.finished_at = failed_at
        _clear_task_scheduling(task)
        action.status = (
            MailDraftStatus.EDITING.value
            if isinstance(action, MailDraftModel)
            else CalendarProposalStatus.EDITING.value
        )
        if execution is not None:
            execution.status = ToolExecutionStatus.CONFIRMED_FAILED.value
            execution.error_code = error_code
            execution.completed_at = failed_at
            execution.result_summary = {
                "kind": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
                "retryable": False,
            }

        approval_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="approval.invalidated",
            actor_type="system",
            actor_id=None,
            event_metadata={"reason": error_code},
        )
        task_audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="task.failed",
            actor_type="worker",
            actor_id=lease_owner,
            event_metadata={
                "status": TaskStatus.FAILED.value,
                "error_code": error_code,
            },
        )
        audits = [approval_audit, task_audit]
        tool_audit: AuditEventModel | None = None
        if execution is not None:
            tool_audit = AuditEventModel(
                user_id=task.user_id,
                task_id=task.id,
                event_type="tool.confirmed_failed",
                actor_type="worker",
                actor_id=lease_owner,
                event_metadata={
                    "action": approval.action,
                    "provider": connection.provider,
                    "outcome": ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED.value,
                    "write_attempt_count": 0,
                },
            )
            audits.append(tool_audit)
        session.add_all(audits)
        await session.flush()
        outbox_events = [
            OutboxEventModel(
                topic="approval.invalidated",
                aggregate_id=task.id,
                deduplication_key=(
                    f"approval.invalidated:{approval.id}:{approval.version}:pre-request"
                ),
                payload={"task_id": str(task.id), "audit_event_id": approval_audit.id},
                available_at=failed_at,
            )
        ]
        if execution is not None and tool_audit is not None:
            outbox_events.append(
                OutboxEventModel(
                    topic="tool.confirmed_failed",
                    aggregate_id=task.id,
                    deduplication_key=f"tool.confirmed_failed:{execution.id}:pre-request",
                    payload={"task_id": str(task.id), "audit_event_id": tool_audit.id},
                    available_at=failed_at,
                )
            )
        session.add_all(outbox_events)
        return True


class SqlAlchemyTrustedActionRepositoryFactory:
    """为一次可信提交提供自动提交或回滚的短事务 Repository。"""

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        cipher: ActionPayloadCipher,
    ) -> None:
        """保存进程级会话工厂与记录绑定加密器，不提前占用连接。

        Args:
            session_factory: API/Worker 进程共享的异步会话工厂。
            cipher: 草稿、提案与审批命令共用的应用内容 AEAD 实现。
        """
        self._session_factory = session_factory
        self._cipher = cipher

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyTrustedActionRepository]:
        """把提交读取、命令冻结与全部持久 mutation 限制在同一事务。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyTrustedActionRepository(session, self._cipher)


def _canonical_command_object(payload: Mapping[str, object]) -> dict[str, object]:
    """复用现有严格边界并把规范 UTF-8 JSON 解码为独立 object。"""
    decoded: object = json.loads(canonical_command_json(payload).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise _trusted_action_unavailable()
    return dict(cast(dict[str, object], decoded))


def _task_operation_matches(task: TaskRunModel, operation_id: UUID) -> bool:
    """只接受任务输入中规范 UUID 与调用方精确 operation 相等的绑定。"""
    raw_operation_id = task.input_payload.get("operation_id")
    if type(raw_operation_id) is not str:
        return False
    try:
        return UUID(raw_operation_id) == operation_id
    except ValueError:
        return False


def _execution_reference(
    *,
    execution: ToolExecutionModel,
    approval_id: UUID,
) -> ExecutionReference:
    """把 ORM ToolExecution 收窄为供应商中立、内容无关的应用 DTO。"""
    if execution.operation_id is None or execution.provider is None:
        raise ValueError("trusted execution binding is incomplete")
    return ExecutionReference(
        execution_id=execution.id,
        task_id=execution.task_id,
        step_id=execution.step_id,
        approval_id=approval_id,
        operation_id=execution.operation_id,
        provider=execution.provider,
        tool_name=execution.tool_name,
        idempotency_key=execution.idempotency_key,
        request_payload_hash=execution.request_payload_hash,
        status=ToolExecutionStatus(execution.status),
        result_summary=(
            dict(execution.result_summary) if execution.result_summary is not None else None
        ),
        request_started_at=execution.request_started_at,
        write_attempt_count=execution.write_attempt_count,
        provider_resource_id=execution.provider_resource_id,
        provider_request_id=execution.provider_request_id,
        correlation_id=execution.correlation_id,
        reconciliation_attempt_count=execution.reconciliation_attempt_count,
    )


def _clear_task_scheduling(task: TaskRunModel) -> None:
    """终止或移交核对时清除所有可让旧 Worker 再进入的调度/租约字段。"""
    task.lease_owner = None
    task.lease_expires_at = None
    task.scheduled_for = None
    task.retry_recovery_at = None
    task.approval_checkpoint_recovery_at = None


def _outcome_summary(outcome: ProviderWriteOutcome) -> dict[str, JsonValue]:
    """只保存规范分类、重试提示和已验证的无地址供应商 URL。

    原始 adapter 响应永远不进入 JSONB。URL 也必须经过独立 HTTPS/地址检查；不安全候选
    被静默丢弃，使历史结果仍可安全展示而不会把供应商输入变成 XSS、凭据或个人地址载体。
    """
    summary: dict[str, JsonValue] = {
        "kind": outcome.kind.value,
        "retryable": outcome.retryable,
    }
    if outcome.retry_after_seconds is not None:
        summary["retry_after_seconds"] = outcome.retry_after_seconds
    provider_url = validate_provider_url(outcome.provider_url)
    if provider_url is not None:
        summary["provider_url"] = provider_url
    return summary


def _calendar_submission_ready(
    *,
    proposal: CalendarChangeProposalModel,
    content: CalendarProposalContent,
    before_snapshot_id: UUID | None,
) -> bool:
    """复核当前 desired 与提案头是否具备冻结命令的完整本地事实。

    Repository 在同一锁事务重做该检查，不能信任此前 API 返回的派生布尔值。创建提案
    必须没有历史事件绑定；修改/恢复必须同时具备精确事件、ETag、before snapshot 与
    非空确定性差异。四类 shell 确认必须已完整且无待确认项。
    """
    content_ready = (
        proposal.status == CalendarProposalStatus.EDITING.value
        and content.confirmed_fields
        == (
            "calendar",
            "time",
            "attendees",
            "notification_policy",
        )
        and not content.required_confirmations
        and content.notification_policy is not None
        and content.starts_at is not None
        and content.ends_at is not None
        and content.timezone is not None
        and content.all_day is not None
        and isinstance(content.title, str)
        and bool(content.title.strip())
        and bool(proposal.calendar_id.strip())
    )
    if not content_ready:
        return False
    if proposal.operation_kind == "create":
        return (
            proposal.target_event_id is None
            and proposal.base_etag is None
            and before_snapshot_id is None
        )
    if proposal.operation_kind in {"update", "restore"}:
        return (
            isinstance(proposal.target_event_id, str)
            and bool(proposal.target_event_id.strip())
            and isinstance(proposal.base_etag, str)
            and bool(proposal.base_etag.strip())
            and before_snapshot_id is not None
            and bool(content.changed_fields)
        )
    return False


def _trusted_action_unavailable() -> StateConflictError:
    """构造不泄露命令、审批存在性或篡改细节的 fail-closed 错误。"""
    return StateConflictError(
        error_code="trusted_action_unavailable",
        message="trusted action is unavailable",
    )


__all__ = [
    "APPROVAL_COMMAND_CONTENT_KIND",
    "SqlAlchemyTrustedActionReconciliationRecoveryStore",
    "SqlAlchemyTrustedActionRepository",
    "SqlAlchemyTrustedActionRepositoryFactory",
    "SqlAlchemyTrustedActionTaskExecutionStore",
]
