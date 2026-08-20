"""在 ApprovalRequest 上持久化严格验证且记录绑定的加密可信命令。"""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest
from typing import cast
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.commands import canonical_command_json, trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.trusted_actions import (
    CalendarProposalSubmissionSnapshot,
    ExecutionReference,
    ExistingTrustedActionSubmission,
    MailDraftSubmissionSnapshot,
    ProviderWriteOutcome,
    TrustedActionDispatchSnapshot,
    TrustedActionExecutionSnapshot,
    TrustedActionSubmission,
)
from ai_employee.application.use_cases.calendar_proposals import CalendarProposalContent
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

        锁序固定为 TaskRun → ApprovalRequest → ToolExecution → 本地动作；连接、用户、能力
        与目录随后在同一事务读取。这样保留/隐私清理、审批失效和并发 Worker 不会形成
        反向等待，也不会让先前无锁投影成为授权依据。
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
        connection_id, calendar_id = binding
        user = await self._session.scalar(
            select(UserModel).where(UserModel.id == task.user_id).with_for_update()
        )
        connection = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == task.user_id,
            )
        )
        if user is None or connection is None or approval.schema_version is None:
            return None
        capabilities = await self._capabilities(user_id=task.user_id, connection_id=connection_id)
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
            calendar_can_write = bool(
                await self._session.scalar(
                    select(ProviderCalendarModel.can_write).where(
                        ProviderCalendarModel.user_id == task.user_id,
                        ProviderCalendarModel.connection_id == connection_id,
                        ProviderCalendarModel.provider_calendar_id == calendar_id,
                    )
                )
            )
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
        audit = AuditEventModel(
            user_id=task.user_id,
            task_id=task.id,
            event_type="approval.invalidated",
            actor_type="system",
            actor_id=None,
            event_metadata={"reason": error_code},
        )
        self._session.add(audit)
        await self._session.flush()
        self._session.add(
            OutboxEventModel(
                topic="approval.invalidated",
                aggregate_id=task.id,
                deduplication_key=f"approval.invalidated:{approval.id}:{approval.version}",
                payload={"task_id": str(task.id), "audit_event_id": audit.id},
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
        """读取 claim 后 dispatch 所需最小绑定；完整命令仍保持加密。"""
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
        connection_id = await self._action_connection_id(task=task, approval=approval)
        if connection_id is None:
            return None
        try:
            reference = _execution_reference(execution=execution, approval_id=approval.id)
        except ValueError:
            return None
        return TrustedActionDispatchSnapshot(
            user_id=task.user_id,
            task_id=task.id,
            approval_id=approval.id,
            operation_id=operation_id,
            connection_id=connection_id,
            action=approval.action,
            schema_version=approval.schema_version,
            payload_hash=approval.payload_hash,
            proposal_kind=approval.proposal_kind or "",
            proposal_id=approval.proposal_id,
            proposal_version=approval.proposal_version,
            provider=execution.provider or "",
            execution=reference,
        )

    async def mark_request_started(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        lease_owner: str,
        started_at: datetime,
    ) -> bool:
        """以 ToolExecution 行锁提交唯一 request-start 与写尝试计数。"""
        task = await self._session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == snapshot.task_id).with_for_update()
        )
        user = await self._session.scalar(
            select(UserModel).where(UserModel.id == snapshot.user_id).with_for_update()
        )
        execution = await self._session.scalar(
            select(ToolExecutionModel)
            .where(ToolExecutionModel.id == snapshot.execution.execution_id)
            .with_for_update()
        )
        if (
            task is None
            or user is None
            or not user.is_active
            or task.status != TaskStatus.RUNNING.value
            or task.lease_owner != lease_owner
            or task.lease_expires_at is None
            or task.lease_expires_at <= started_at
            or execution is None
        ):
            return False
        status = ToolExecutionStatus(execution.status)
        if status is ToolExecutionStatus.CLAIMED:
            if execution.request_started_at is not None:
                return False
        elif status is not ToolExecutionStatus.RETRYABLE_FAILED:
            return False
        if execution.request_started_at is None:
            execution.request_started_at = started_at
        execution.write_attempt_count += 1
        execution.status = ToolExecutionStatus.EXECUTING.value
        await self._session.flush()
        return True

    async def persist_provider_outcome(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        outcome: ProviderWriteOutcome,
        completed_at: datetime,
        from_reconciliation: bool,
        lease_owner: str,
    ) -> None:
        """把 adapter 规范结果与任务/本地状态在同一事务收敛。"""
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
        status = ToolExecutionStatus(execution.status)
        if status in {
            ToolExecutionStatus.SUCCEEDED,
            ToolExecutionStatus.CONFIRMED_FAILED,
        }:
            return
        allowed = (
            {
                ToolExecutionStatus.EXECUTING,
                ToolExecutionStatus.RECONCILING,
                ToolExecutionStatus.CLAIMED,
            }
            if from_reconciliation
            else {ToolExecutionStatus.EXECUTING}
        )
        if (
            status not in allowed
            or task.status != TaskStatus.RUNNING.value
            or task.lease_owner != lease_owner
        ):
            raise _trusted_action_unavailable()
        execution.provider_resource_id = outcome.provider_resource_id
        execution.provider_request_id = outcome.provider_request_id
        execution.correlation_id = outcome.correlation_id
        execution.error_code = outcome.error_code
        execution.result_summary = _outcome_summary(outcome)
        event_type: str
        outbox_topic: str
        if outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_APPLIED:
            execution.status = ToolExecutionStatus.SUCCEEDED.value
            execution.completed_at = completed_at
            task.status = TaskStatus.SUCCEEDED.value
            task.error_code = None
            task.finished_at = completed_at
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
            event_type = outbox_topic = "tool.succeeded"
        elif outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED and outcome.retryable:
            execution.status = ToolExecutionStatus.RETRYABLE_FAILED.value
            event_type = outbox_topic = "tool.retryable_failed"
        elif outcome.kind is ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED:
            execution.status = ToolExecutionStatus.CONFIRMED_FAILED.value
            execution.completed_at = completed_at
            task.status = TaskStatus.FAILED.value
            task.error_code = outcome.error_code or "provider_write_confirmed_not_applied"
            task.finished_at = completed_at
            _clear_task_scheduling(task)
            await self._set_local_action_status(
                task=task,
                approval=approval,
                mail_status=MailDraftStatus.EDITING,
                calendar_status=CalendarProposalStatus.EDITING,
                allowed_current={
                    MailDraftStatus.EXECUTING.value,
                    CalendarProposalStatus.EXECUTING.value,
                    MailDraftStatus.NEEDS_ATTENTION.value,
                    CalendarProposalStatus.NEEDS_ATTENTION.value,
                },
            )
            event_type = outbox_topic = "tool.confirmed_failed"
        else:
            execution.status = ToolExecutionStatus.RECONCILING.value
            task.status = TaskStatus.RECONCILING.value
            task.error_code = "provider_write_outcome_unknown"
            task.lease_owner = None
            task.lease_expires_at = None
            await self._set_local_action_status(
                task=task,
                approval=approval,
                mail_status=MailDraftStatus.NEEDS_ATTENTION,
                calendar_status=CalendarProposalStatus.NEEDS_ATTENTION,
                allowed_current={
                    MailDraftStatus.EXECUTING.value,
                    CalendarProposalStatus.EXECUTING.value,
                    MailDraftStatus.NEEDS_ATTENTION.value,
                    CalendarProposalStatus.NEEDS_ATTENTION.value,
                },
            )
            event_type = outbox_topic = "tool.reconciling"
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
                available_at=completed_at,
            )
        )

    async def fail_claimed_integrity(
        self,
        *,
        snapshot: TrustedActionDispatchSnapshot,
        failed_at: datetime,
        lease_owner: str,
    ) -> None:
        """在零 provider 调用边界把命令认证失败持久化为明确本地失败。"""
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
        if task.status != TaskStatus.RUNNING.value or task.lease_owner != lease_owner:
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
        """拒绝分支完成任务但绝不创建 ToolExecution 或改变供应商。"""
        task, approval = await self._locked_task_approval(
            task_id=task_id,
            approval_id=approval_id,
        )
        if (
            task is None
            or approval is None
            or not _task_operation_matches(task, operation_id)
            or approval.status != ApprovalStatus.REJECTED.value
            or task.status != TaskStatus.RUNNING.value
            or task.lease_owner != lease_owner
        ):
            raise _trusted_action_unavailable()
        task.status = TaskStatus.SUCCEEDED.value
        task.error_code = None
        task.finished_at = finished_at
        _clear_task_scheduling(task)

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
    ) -> tuple[UUID, str | None] | None:
        """在 ToolExecution 之后锁定审批绑定的当前本地动作并返回连接/日历。"""
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
            return draft.connection_id, None
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
        return proposal.connection_id, proposal.calendar_id

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
    ) -> None:
        """只改写仍绑定冻结版本且处于预期状态的本地动作头。"""
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

    async def _capabilities(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
    ) -> dict[ConnectionCapability, tuple[CapabilityStatus, str | None]]:
        """读取四能力中当前连接实际存在的状态与稳定错误码。"""
        rows = tuple(
            (
                await self._session.scalars(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.user_id == user_id,
                        ConnectionCapabilityModel.connection_id == connection_id,
                    )
                )
            ).all()
        )
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
        approval_id=approval_id,
        operation_id=execution.operation_id,
        provider=execution.provider,
        status=ToolExecutionStatus(execution.status),
        request_started_at=execution.request_started_at,
        write_attempt_count=execution.write_attempt_count,
        provider_resource_id=execution.provider_resource_id,
        provider_request_id=execution.provider_request_id,
        correlation_id=execution.correlation_id,
    )


def _clear_task_scheduling(task: TaskRunModel) -> None:
    """终止或移交核对时清除所有可让旧 Worker 再进入的调度/租约字段。"""
    task.lease_owner = None
    task.lease_expires_at = None
    task.scheduled_for = None
    task.retry_recovery_at = None
    task.approval_checkpoint_recovery_at = None


def _outcome_summary(outcome: ProviderWriteOutcome) -> dict[str, JsonValue]:
    """只保存规范分类、计数提示与供应商不透明 ID，不保留原始响应。"""
    summary: dict[str, JsonValue] = {
        "kind": outcome.kind.value,
        "retryable": outcome.retryable,
    }
    if outcome.retry_after_seconds is not None:
        summary["retry_after_seconds"] = outcome.retry_after_seconds
    if outcome.provider_url is not None:
        summary["provider_url"] = outcome.provider_url
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
    "SqlAlchemyTrustedActionRepository",
    "SqlAlchemyTrustedActionRepositoryFactory",
]
