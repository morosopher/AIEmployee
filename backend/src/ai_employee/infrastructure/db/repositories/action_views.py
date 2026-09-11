"""实现可信动作视图的用户隔离读取、人工 CAS 与只读核对重开事务。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, time
from hashlib import sha256
from hmac import compare_digest
from typing import Literal, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidTag
from pydantic import TypeAdapter, ValidationError
from sqlalchemy import String, Uuid, func, literal, select, text, union_all
from sqlalchemy import cast as sql_cast
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.commands import parse_trusted_command, trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue, Encryption
from ai_employee.application.ports.trusted_actions import trusted_execution_binding_matches
from ai_employee.application.trusted_action_summary import parse_trusted_action_step_summary
from ai_employee.application.use_cases.action_views import (
    ActionApprovalView,
    ActionExecutionView,
    ActionItemKind,
    ActionKind,
    ActionListItem,
    ActionListPage,
    ActionLocalSummary,
    ActionProvider,
    ActionSnapshot,
    ActionTimelineEvent,
    ActionViewTransaction,
    ApprovalPreview,
    CalendarApprovalPreview,
    CalendarPreviewFields,
    MailApprovalPreview,
    calendar_conflict_previews,
    public_action_event_payload,
)
from ai_employee.application.use_cases.task_execution import utc_instant
from ai_employee.application.use_cases.trusted_actions import validate_provider_url
from ai_employee.domain.actions import (
    CalendarProposalStatus,
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
)
from ai_employee.domain.calendar_actions import CalendarCreateCommand, CalendarUpdateCommand
from ai_employee.domain.errors import InternalInvariantError, StateConflictError
from ai_employee.domain.mail_actions import MailSendCommand
from ai_employee.domain.tasks import ApprovalStatus, JsonValue, TaskStatus, transition_task
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
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
from ai_employee.infrastructure.db.repositories.calendar import SqlAlchemyCalendarSyncRepository
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher

_MANUAL_EXECUTED = "confirmed_executed"
_MANUAL_NOT_EXECUTED = "confirmed_not_executed"
_MANUAL_RESOLUTIONS = frozenset({_MANUAL_EXECUTED, _MANUAL_NOT_EXECUTED})
_CALENDAR_ACTIONS = frozenset({"calendar.create", "calendar.update", "calendar.restore"})


def _manual_conflict() -> StateConflictError:
    """构造不泄露任务存在性或供应商内容的统一人工 CAS 冲突。"""
    return StateConflictError(
        error_code="manual_resolution_conflict",
        message="action state changed; reload before resolving manually",
    )


def _task_identifiers(task: TaskRunModel) -> tuple[UUID, UUID] | None:
    """从锁定的 identifier-only TaskRun 输入解析审批与操作 UUID。"""
    payload = task.input_payload
    if not isinstance(payload, dict) or set(payload) != {"approval_id", "operation_id"}:
        return None
    raw_approval = payload.get("approval_id")
    raw_operation = payload.get("operation_id")
    if type(raw_approval) is not str or type(raw_operation) is not str:
        return None
    try:
        approval_id, operation_id = UUID(raw_approval), UUID(raw_operation)
        if str(approval_id) != raw_approval or str(operation_id) != raw_operation:
            return None
        return approval_id, operation_id
    except ValueError:
        return None


def _clear_scheduling(task: TaskRunModel) -> None:
    """清理终态或只读移交的租约与所有调度字段。"""
    task.lease_owner = None
    task.lease_expires_at = None
    task.scheduled_for = None
    task.retry_recovery_at = None
    task.approval_checkpoint_recovery_at = None


class SqlAlchemyActionViewRepository(ActionViewTransaction):
    """在一个调用方事务中完成动作人工确认与核对重开。

    所有 mutation 都使用固定的 ``TaskRun → ApprovalRequest → ToolExecution → local
    action → Connection → Calendar`` 锁序。这样自动核对、人工确认和用户重开在同一
    任务上会由 PostgreSQL 行锁串行化，后到者只能读取前者提交的状态并返回稳定冲突，
    不会出现一半终态或重复供应商调用。
    """

    def __init__(
        self, session: AsyncSession, encryption_factory: Callable[[], Encryption] | None = None
    ) -> None:
        """绑定由 factory 管理生命周期的异步会话；repository 不自行提交。"""
        self._session = session
        self._encryption_factory = encryption_factory

    async def _lock_context(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
    ) -> tuple[
        TaskRunModel,
        ApprovalRequestModel,
        ToolExecutionModel,
        MailDraftModel | CalendarChangeProposalModel,
        OAuthConnectionModel,
        ProviderCalendarModel | None,
    ]:
        """按唯一锁序读取一次可信动作的所有归属行。

        跨用户、不完整输入、版本换绑和未知状态均使用同一 content-free 冲突，避免把
        资源存在性、正文或供应商标识泄露给调用方。
        """
        task = await self._session.scalar(
            select(TaskRunModel)
            .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
            .with_for_update()
        )
        if task is None or task.kind != "trusted_action":
            raise _manual_conflict()
        # Cookie 认证可能先于删除屏障；必须在 Task→user 锁内再次检查，防止迟到的
        # 人工确认/重开核对覆盖 privacy 的持久一次资格或新建恢复任务。
        active = await self._session.scalar(
            select(UserModel.is_active)
            .where(
                UserModel.id == user_id,
            )
            .with_for_update()
        )
        if active is not True:
            raise _manual_conflict()
        identifiers = _task_identifiers(task)
        if identifiers is None:
            raise _manual_conflict()
        approval_id, operation_id = identifiers
        approval = await self._session.scalar(
            select(ApprovalRequestModel)
            .where(
                ApprovalRequestModel.id == approval_id,
                ApprovalRequestModel.task_id == task.id,
            )
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
        if approval is None or execution is None:
            raise _manual_conflict()
        if (
            approval.proposal_id is None
            or approval.proposal_version is None
            or approval.version <= 0
            or type(approval.schema_version) is not str
            or not approval.schema_version
            or approval.proposal_kind not in {"mail_draft", "calendar_proposal"}
            or execution.operation_id != operation_id
            or execution.provider is None
            or execution.task_id != task.id
            or execution.step_id != approval.step_id
            or execution.tool_name != approval.action
        ):
            raise _manual_conflict()

        action: MailDraftModel | CalendarChangeProposalModel | None
        if approval.proposal_kind == "mail_draft":
            action = await self._session.scalar(
                select(MailDraftModel)
                .where(
                    MailDraftModel.id == approval.proposal_id,
                    MailDraftModel.user_id == user_id,
                )
                .with_for_update()
            )
        else:
            action = await self._session.scalar(
                select(CalendarChangeProposalModel)
                .where(
                    CalendarChangeProposalModel.id == approval.proposal_id,
                    CalendarChangeProposalModel.user_id == user_id,
                )
                .with_for_update()
            )
        if action is None or action.current_version != approval.proposal_version:
            raise _manual_conflict()
        connection = await self._session.scalar(
            select(OAuthConnectionModel)
            .where(
                OAuthConnectionModel.id == action.connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
            .with_for_update()
        )
        if connection is None or connection.provider != execution.provider:
            raise _manual_conflict()
        if not trusted_execution_binding_matches(
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
        ):
            raise _manual_conflict()
        if approval.proposal_kind == "mail_draft":
            if approval.action != "mail.send" or approval.schema_version != "mail_send.v1":
                raise _manual_conflict()
        elif (
            approval.action not in _CALENDAR_ACTIONS
            or approval.schema_version != f"{approval.action.replace('.', '_')}.v1"
            or not isinstance(action, CalendarChangeProposalModel)
            or approval.action != f"calendar.{action.operation_kind}"
        ):
            raise _manual_conflict()
        if isinstance(action, MailDraftModel) and approval.proposal_kind != "mail_draft":
            raise _manual_conflict()
        if (
            isinstance(action, CalendarChangeProposalModel)
            and approval.proposal_kind != "calendar_proposal"
        ):
            raise _manual_conflict()
        calendar: ProviderCalendarModel | None = None
        if isinstance(action, CalendarChangeProposalModel):
            calendar = await self._session.scalar(
                select(ProviderCalendarModel)
                .where(
                    ProviderCalendarModel.user_id == user_id,
                    ProviderCalendarModel.connection_id == action.connection_id,
                    ProviderCalendarModel.provider_calendar_id == action.calendar_id,
                )
                .with_for_update()
            )
            if calendar is None:
                raise _manual_conflict()
        return task, approval, execution, action, connection, calendar

    async def _current_cursor(self, *, user_id: UUID, task_id: UUID) -> int:
        """在所有业务行锁定后读取本任务用户范围内的最大审计 ID。"""
        cursor = await self._session.scalar(
            select(func.max(AuditEventModel.id)).where(
                AuditEventModel.task_id == task_id,
                AuditEventModel.user_id == user_id,
            )
        )
        return int(cursor or 0)

    @staticmethod
    def _safe_result_summary(
        execution: ToolExecutionModel,
        *,
        kind: ProviderWriteOutcomeKind,
    ) -> dict[str, JsonValue]:
        """重建内容无关结果摘要，只保留允许展示的枚举、布尔值与安全 URL。"""
        summary: dict[str, JsonValue] = {
            "kind": kind.value,
            "retryable": False,
        }
        if isinstance(execution.result_summary, dict):
            candidate_url = execution.result_summary.get("provider_url")
            safe_url = validate_provider_url(candidate_url)
            if safe_url is not None:
                summary["provider_url"] = safe_url
        return summary

    async def resolve_manual(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        task_version: int,
        resolution: str,
        resolved_at: datetime,
    ) -> int:
        """锁定并 CAS 人工结论，且在同一事务写终态审计与 Outbox。

        ``task_version`` 是锁后重算的 ``MAX(audit_events.id)``，不是一个可变的 TaskRun
        列。只有仍处于 ``needs_attention`` 的同一 ToolExecution 才能消费该游标；因此
        自动核对若先完成，人工请求必定得到 ``manual_resolution_conflict``，而不会覆盖
        已确认的供应商事实。
        """
        if type(resolution) is not str or resolution not in _MANUAL_RESOLUTIONS:
            raise ValueError("resolution must be confirmed_executed or confirmed_not_executed")
        resolved_at = utc_instant(resolved_at, field="resolved_at")
        if type(task_version) is not int or isinstance(task_version, bool) or task_version < 0:
            raise ValueError("task_version must be a non-negative integer")
        task, approval, execution, action, connection, _ = await self._lock_context(
            user_id=user_id,
            task_id=task_id,
        )
        current_cursor = await self._current_cursor(user_id=user_id, task_id=task_id)
        if current_cursor != task_version:
            raise _manual_conflict()
        if (
            task.status != TaskStatus.NEEDS_ATTENTION.value
            or approval.status != ApprovalStatus.APPROVED.value
            or execution.status != ToolExecutionStatus.NEEDS_ATTENTION.value
            or execution.manual_resolution is not None
            or execution.request_started_at is None
            or execution.write_attempt_count <= 0
        ):
            raise _manual_conflict()
        if isinstance(action, MailDraftModel):
            if action.status != MailDraftStatus.NEEDS_ATTENTION.value:
                raise _manual_conflict()
        else:
            if action.status != CalendarProposalStatus.NEEDS_ATTENTION.value:
                raise _manual_conflict()

        target_kind = (
            ProviderWriteOutcomeKind.CONFIRMED_APPLIED
            if resolution == _MANUAL_EXECUTED
            else ProviderWriteOutcomeKind.CONFIRMED_NOT_APPLIED
        )
        target_task_status = (
            TaskStatus.SUCCEEDED if resolution == _MANUAL_EXECUTED else TaskStatus.FAILED
        )
        transition_task(
            TaskStatus(task.status),
            target_task_status,
            manual_resolution=True,
        )
        execution.manual_resolution = resolution
        execution.manual_resolved_by_user_id = user_id
        execution.manual_resolved_at = resolved_at
        execution.status = (
            ToolExecutionStatus.SUCCEEDED.value
            if resolution == _MANUAL_EXECUTED
            else ToolExecutionStatus.CONFIRMED_FAILED.value
        )
        execution.error_code = (
            None
            if resolution == _MANUAL_EXECUTED
            else (
                "calendar_event_version_conflict"
                if execution.error_code == "calendar_event_version_conflict"
                else "provider_write_confirmed_not_applied"
            )
        )
        execution.completed_at = resolved_at
        execution.result_summary = self._safe_result_summary(execution, kind=target_kind)
        task.status = target_task_status.value
        task.error_code = execution.error_code
        task.finished_at = resolved_at
        task.current_step = "manual_resolution"
        _clear_scheduling(task)
        if isinstance(action, MailDraftModel):
            action.status = (
                MailDraftStatus.SENT.value
                if resolution == _MANUAL_EXECUTED
                else MailDraftStatus.EDITING.value
            )
        else:
            if resolution == _MANUAL_EXECUTED:
                action.status = CalendarProposalStatus.APPLIED.value
            elif execution.error_code == "calendar_event_version_conflict":
                action.status = CalendarProposalStatus.STALE.value
            else:
                action.status = CalendarProposalStatus.EDITING.value

        # 任务终态审计先写入，人工 lifecycle 审计最后写入，确保返回的 ID 是新的最大
        # 游标，客户端可以直接把它作为下一次 CAS 的 canonical task_version。
        task_audit = AuditEventModel(
            user_id=user_id,
            task_id=task_id,
            event_type=("task.succeeded" if resolution == _MANUAL_EXECUTED else "task.failed"),
            actor_type="user",
            actor_id=str(user_id),
            event_metadata={
                "reason": "manual_resolution",
                "resolution": resolution,
            },
        )
        self._session.add(task_audit)
        await self._session.flush()
        provider_url = None
        if isinstance(execution.result_summary, dict):
            provider_url = validate_provider_url(execution.result_summary.get("provider_url"))
        manual_metadata: dict[str, JsonValue] = {
            "resolution": resolution,
            "source": "manual",
            "operation_id": str(execution.operation_id),
            "approval_id": str(approval.id),
            "resolved_at": resolved_at.isoformat(),
        }
        if provider_url is not None:
            # 只把经过同一 HTTPS/地址/凭据校验的入口写入审计；原始 adapter 值永远
            # 不会因人工确认而绕过结果摘要边界。
            manual_metadata["provider_url"] = provider_url
        manual_audit = AuditEventModel(
            user_id=user_id,
            task_id=task_id,
            event_type="tool.manually_resolved",
            actor_type="user",
            actor_id=str(user_id),
            event_metadata=manual_metadata,
        )
        self._session.add(manual_audit)
        await self._session.flush()
        self._session.add(
            OutboxEventModel(
                topic="tool.manually_resolved",
                aggregate_id=task_id,
                deduplication_key=f"tool.manually_resolved:{execution.id}",
                payload={"task_id": str(task_id), "audit_event_id": manual_audit.id},
                available_at=resolved_at,
            )
        )
        if resolution == _MANUAL_EXECUTED:
            await self._enqueue_source_refresh(
                task=task,
                approval=approval,
                execution=execution,
                connection=connection,
                calendar_id=(
                    action.calendar_id if isinstance(action, CalendarChangeProposalModel) else None
                ),
                available_at=resolved_at,
            )
        return cast(int, manual_audit.id)

    async def request_reconciliation(
        self,
        *,
        user_id: UUID,
        task_id: UUID,
        requested_at: datetime,
    ) -> UUID:
        """把人工待处理动作原子重开为只读核对，并保留原 ToolExecution 身份。"""
        requested_at = utc_instant(requested_at, field="requested_at")
        task, approval, execution, action, _, _ = await self._lock_context(
            user_id=user_id,
            task_id=task_id,
        )
        if (
            task.status != TaskStatus.NEEDS_ATTENTION.value
            or approval.status != ApprovalStatus.APPROVED.value
            or execution.status != ToolExecutionStatus.NEEDS_ATTENTION.value
            or execution.manual_resolution is not None
        ):
            raise _manual_conflict()
        if isinstance(action, MailDraftModel):
            if action.status != MailDraftStatus.NEEDS_ATTENTION.value:
                raise _manual_conflict()
        elif action.status != CalendarProposalStatus.NEEDS_ATTENTION.value:
            raise _manual_conflict()
        transition_task(
            TaskStatus(task.status),
            TaskStatus.RECONCILING,
        )
        task.status = TaskStatus.RECONCILING.value
        task.error_code = "provider_write_outcome_unknown"
        task.finished_at = None
        task.current_step = "reconcile"
        task.scheduled_for = requested_at
        task.lease_owner = None
        task.lease_expires_at = None
        task.retry_recovery_at = None
        task.approval_checkpoint_recovery_at = None
        execution.status = ToolExecutionStatus.RECONCILING.value
        execution.error_code = "provider_write_outcome_unknown"
        if isinstance(action, MailDraftModel):
            action.status = MailDraftStatus.NEEDS_ATTENTION.value
        else:
            action.status = CalendarProposalStatus.NEEDS_ATTENTION.value

        audit = AuditEventModel(
            user_id=user_id,
            task_id=task_id,
            event_type="tool.reconciling",
            actor_type="user",
            actor_id=str(user_id),
            event_metadata={"source": "user_request", "operation_id": str(execution.operation_id)},
        )
        self._session.add(audit)
        await self._session.flush()
        self._session.add_all(
            (
                OutboxEventModel(
                    topic="tool.reconciling",
                    aggregate_id=task_id,
                    deduplication_key=f"tool.reconciling:{execution.id}:manual:{audit.id}",
                    payload={"task_id": str(task_id), "audit_event_id": audit.id},
                    available_at=requested_at,
                ),
                OutboxEventModel(
                    topic="task.execute",
                    aggregate_id=task_id,
                    deduplication_key=f"task.execute:{task_id}:manual-reconcile:{execution.id}:{audit.id}",
                    payload={"task_id": str(task_id)},
                    available_at=requested_at,
                ),
            )
        )
        return execution.id

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
        """为人工确认已执行结果创建去重的只读同步意图。

        该方法只创建 ``sync_mail``/``sync_calendar`` 任务；它不插入任何供应商邮件或日历
        行，实际 source refresh 必须由后续只读同步 worker 从 provider 真实资源规范化。
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
        inserted_id = await self._session.scalar(
            insert(TaskRunModel)
            .values(
                id=refresh_id,
                user_id=task.user_id,
                kind=kind,
                status=TaskStatus.CREATED.value,
                idempotency_key=idempotency_key,
                input_payload={"connection_id": str(connection.id), "scope_key": scope_key},
            )
            .on_conflict_do_nothing(constraint="uq_task_runs_user_id_idempotency_key")
            .returning(TaskRunModel.id)
        )
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

    async def get(self, *, user_id: UUID, task_id: UUID) -> ActionSnapshot | None:
        """读取精确绑定的可信动作；完整 HTTP 读取由工厂保证可重复读事务。"""
        task = await self._session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
        )
        if task is None or task.kind != "trusted_action":
            return None
        identifiers = _task_identifiers(task)
        if identifiers is None:
            return None
        approval_id, operation_id = identifiers
        approval = await self._session.scalar(
            select(ApprovalRequestModel).where(
                ApprovalRequestModel.id == approval_id,
                ApprovalRequestModel.task_id == task_id,
            )
        )
        execution = await self._session.scalar(
            select(ToolExecutionModel).where(ToolExecutionModel.task_id == task_id)
        )
        if approval is None:
            return None
        connection_id: UUID | None = None
        if approval.proposal_id is not None:
            if approval.proposal_kind == "mail_draft":
                connection_id = await self._session.scalar(
                    select(MailDraftModel.connection_id).where(
                        MailDraftModel.id == approval.proposal_id,
                        MailDraftModel.user_id == user_id,
                    )
                )
            elif approval.proposal_kind == "calendar_proposal":
                connection_id = await self._session.scalar(
                    select(CalendarChangeProposalModel.connection_id).where(
                        CalendarChangeProposalModel.id == approval.proposal_id,
                        CalendarChangeProposalModel.user_id == user_id,
                    )
                )
        if self._encryption_factory is not None:
            step = await self._session.scalar(
                select(TaskStepModel).where(
                    TaskStepModel.id == approval.step_id,
                    TaskStepModel.task_id == task_id,
                )
            )
            summary = (
                None if step is None else parse_trusted_action_step_summary(step.input_summary)
            )
            if (
                summary is None
                or summary.action != approval.action
                or summary.proposal_version != approval.proposal_version
                or approval.schema_version != summary.action.replace(".", "_") + ".v1"
                or step is None
                or step.kind != "trusted_action"
            ):
                return None
            # 只有精确 legacy 可沿用尚未重绑的 local；新格式损坏不能由密文补救。
            if summary.frozen_connection_id is not None:
                connection_id = summary.frozen_connection_id
        provider: str | None = None
        if connection_id is not None:
            provider = await self._session.scalar(
                select(OAuthConnectionModel.provider).where(
                    OAuthConnectionModel.id == connection_id,
                    OAuthConnectionModel.user_id == user_id,
                )
            )
        if execution is not None and (
            execution.operation_id != operation_id
            or execution.provider is None
            or provider is None
            or execution.provider != provider
            or execution.task_id != task_id
            or execution.step_id != approval.step_id
            or execution.tool_name != approval.action
            or not trusted_execution_binding_matches(
                execution_task_id=execution.task_id,
                expected_task_id=task_id,
                execution_step_id=execution.step_id,
                expected_step_id=approval.step_id,
                execution_operation_id=execution.operation_id,
                expected_operation_id=operation_id,
                execution_provider=execution.provider,
                expected_provider=provider,
                execution_tool_name=execution.tool_name,
                expected_action=approval.action,
                execution_idempotency_key=execution.idempotency_key,
                approval_id=approval.id,
                approval_version=approval.version,
                execution_payload_hash=execution.request_payload_hash,
                expected_payload_hash=approval.payload_hash,
            )
        ):
            return None
        cursor = await self._current_cursor(user_id=user_id, task_id=task_id)
        provider_url = None
        attempts = 0
        if execution is not None:
            attempts = execution.reconciliation_attempt_count
            if isinstance(execution.result_summary, dict):
                provider_url = validate_provider_url(execution.result_summary.get("provider_url"))
        snapshot = ActionSnapshot(
            task_id=task_id,
            status=task.status,
            error_code=task.error_code,
            event_cursor=str(cursor),
            task_version=str(cursor),
            reconciliation_attempt_count=attempts,
            provider_url=provider_url,
        )
        if self._encryption_factory is None:
            # 兼容 Task20 内部只读取游标的最小调用；API 工厂总是提供惰性加密构造器。
            return snapshot
        local: MailDraftModel | CalendarChangeProposalModel | None = None
        if approval.proposal_kind == "mail_draft":
            local = await self._session.scalar(
                select(MailDraftModel).where(
                    MailDraftModel.id == approval.proposal_id,
                    MailDraftModel.user_id == user_id,
                )
            )
        elif approval.proposal_kind == "calendar_proposal":
            local = await self._session.scalar(
                select(CalendarChangeProposalModel).where(
                    CalendarChangeProposalModel.id == approval.proposal_id,
                    CalendarChangeProposalModel.user_id == user_id,
                )
            )
        connection = await self._session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        if local is None or connection is None or approval.proposal_version is None:
            return None
        if (
            approval.proposal_version > local.current_version
            or (approval.proposal_kind == "mail_draft" and approval.action != "mail.send")
            or (
                isinstance(local, CalendarChangeProposalModel)
                and approval.action != "calendar." + local.operation_kind
            )
        ):
            return None
        rows = tuple(
            (
                await self._session.scalars(
                    select(AuditEventModel)
                    .where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.task_id == task_id,
                    )
                    .order_by(AuditEventModel.id)
                )
            ).all()
        )
        content_cleared = all(
            value is None
            for value in (
                approval.payload_ciphertext,
                approval.payload_nonce,
                approval.payload_key_version,
            )
        )
        preview = (
            None
            if content_cleared
            else await self._approval_preview(
                approval=approval,
                local=local,
                connection=connection,
                operation_id=operation_id,
            )
        )
        return replace(
            snapshot,
            action=cast(ActionKind, approval.action),
            provider=cast(ActionProvider, provider),
            created_at=task.created_at,
            updated_at=task.updated_at,
            local_action=ActionLocalSummary(
                id=local.id,
                item_kind=cast(
                    "Literal['mail_draft', 'calendar_proposal']", approval.proposal_kind
                ),
                status=local.status,
                version=local.current_version,
                editor_url=_editor_url(approval.proposal_kind or "", local.id),
            ),
            approval=ActionApprovalView(
                id=approval.id,
                status=approval.status,
                version=approval.version,
                payload_hash=approval.payload_hash,
                proposal_version=approval.proposal_version,
                risk_level=cast("Literal['high', 'medium']", approval.risk_level),
                expires_at=approval.expires_at,
                decided_at=approval.decided_at,
                content_status="redacted" if content_cleared or preview is None else "available",
                preview=preview,
            ),
            execution=None
            if execution is None
            else ActionExecutionView(
                id=execution.id,
                status=execution.status,
                write_attempt_count=execution.write_attempt_count,
                reconciliation_attempt_count=execution.reconciliation_attempt_count,
                error_code=execution.error_code,
                claimed_at=execution.claimed_at,
                request_started_at=execution.request_started_at,
                completed_at=execution.completed_at,
                manual_resolution=cast(
                    "Literal['confirmed_executed', 'confirmed_not_executed'] | None",
                    execution.manual_resolution,
                ),
            ),
            timeline=tuple(
                ActionTimelineEvent(
                    id=str(row.id),
                    event=row.event_type,
                    occurred_at=row.created_at,
                    payload=public_action_event_payload(row.event_metadata),
                )
                for row in rows
            ),
        )

    async def _approval_preview(
        self,
        *,
        approval: ApprovalRequestModel,
        local: MailDraftModel | CalendarChangeProposalModel,
        connection: OAuthConnectionModel,
        operation_id: UUID,
    ) -> ApprovalPreview | None:
        """在受控内存解密并验证完整冻结绑定；内容已清除的 before 也作为 redacted 返回。

        不能从旧 payload/preview_markdown 恢复内容，不能用当前草稿版本或供应商事件替换
        已批准版本。认证/哈希/字段失败只产生固定内部错误，绝不保留原始验证异常。
        """
        if (
            self._encryption_factory is None
            or approval.payload_ciphertext is None
            or approval.payload_nonce is None
            or approval.payload_key_version is None
            or approval.schema_version is None
        ):
            raise _snapshot_invalid()
        encryption = self._encryption_factory()
        cipher = ActionPayloadCipher(encryption)
        try:
            payload = cipher.decrypt_json(
                EncryptedValue(
                    approval.payload_ciphertext,
                    approval.payload_nonce,
                    approval.payload_key_version,
                ),
                user_id=local.user_id,
                record_id=approval.id,
                content_kind="approval_command",
                action=approval.action,
                schema_version=approval.schema_version,
            )
            command = parse_trusted_command(payload)
            if (
                not compare_digest(trusted_command_hash(payload), approval.payload_hash)
                or command.action != approval.action
                or command.schema_version != approval.schema_version
                or command.operation_id != operation_id
                or command.connection_id != connection.id
            ):
                raise ValueError("invalid frozen binding")
            provider = cast(ActionProvider, connection.provider)
            if isinstance(command, MailSendCommand):
                if (
                    not isinstance(local, MailDraftModel)
                    or command.draft_id != local.id
                    or command.draft_version != approval.proposal_version
                ):
                    raise ValueError("invalid mail binding")
                return MailApprovalPreview(
                    provider=provider,
                    account_email=connection.account_email,
                    mode=command.mode.value,
                    to=list(command.to),
                    cc=list(command.cc),
                    bcc=list(command.bcc),
                    subject=command.subject,
                    body_text=command.body_text,
                )
            if not isinstance(local, CalendarChangeProposalModel):
                raise TypeError("invalid calendar binding")
            calendar = await self._session.scalar(
                select(ProviderCalendarModel).where(
                    ProviderCalendarModel.user_id == local.user_id,
                    ProviderCalendarModel.connection_id == connection.id,
                    ProviderCalendarModel.provider_calendar_id == command.calendar_id,
                )
            )
            if calendar is None:
                raise ValueError("calendar unavailable")
            before = None
            if not isinstance(command, CalendarCreateCommand):
                before_row = await self._session.scalar(
                    select(CalendarChangeSnapshotModel).where(
                        CalendarChangeSnapshotModel.user_id == local.user_id,
                        CalendarChangeSnapshotModel.id == command.before_snapshot_id,
                        CalendarChangeSnapshotModel.proposal_id == local.id,
                        CalendarChangeSnapshotModel.snapshot_kind == "before",
                    )
                )
                if before_row is None:
                    raise ValueError("before snapshot unavailable")
                if (
                    before_row.content_ciphertext is None
                    and before_row.content_nonce is None
                    and before_row.content_key_version is None
                ):
                    return None
                before_snapshot = await SqlAlchemyCalendarProposalRepository(
                    self._session, cipher
                ).load_snapshot(
                    user_id=local.user_id,
                    snapshot_id=command.before_snapshot_id,
                )
                if before_snapshot is None:
                    raise ValueError("before snapshot unavailable")
                before = CalendarPreviewFields.model_validate(
                    {
                        key: before_snapshot.content.get(key)
                        for key in CalendarPreviewFields.model_fields
                    }
                )
            after = CalendarPreviewFields(
                title=command.title,
                description=command.description,
                location=command.location,
                starts_at=command.starts_at.isoformat(),
                ends_at=command.ends_at.isoformat(),
                timezone=command.timezone,
                all_day=command.all_day,
                attendees=list(command.attendees),
            )
            start = command.starts_at
            search_start = (
                start
                if isinstance(start, datetime)
                else datetime.combine(start, time(), ZoneInfo(command.timezone))
            )
            end = command.ends_at
            search_end = (
                end
                if isinstance(end, datetime)
                else datetime.combine(end, time(), ZoneInfo(command.timezone))
            )
            # 审批冲突必须覆盖整个冻结日程；候选建议的固定窗口不适用于长日程。
            # 按 UTC 向上扩展天数，底层窗口另含一天余量，足以保留结束后的会议缓冲。
            horizon_days = (search_end.astimezone(UTC) - search_start.astimezone(UTC)).days + 1
            observed_at = await self._session.scalar(select(func.current_timestamp()))
            if observed_at is None:
                raise ValueError("snapshot clock unavailable")
            context = await SqlAlchemyCalendarSyncRepository(
                self._session, encryption
            ).get_availability_context(
                user_id=local.user_id,
                observed_at=observed_at,
                search_start=search_start,
                horizon_days=horizon_days,
                excluded_event_id=None
                if isinstance(command, CalendarCreateCommand)
                else await self._session.scalar(
                    select(CalendarEventModel.id).where(
                        CalendarEventModel.user_id == local.user_id,
                        CalendarEventModel.connection_id == connection.id,
                        CalendarEventModel.calendar_id == command.calendar_id,
                        CalendarEventModel.provider_event_id == command.provider_event_id,
                    )
                ),
            )
            if context is None:
                raise ValueError("availability context unavailable")
            return CalendarApprovalPreview(
                provider=provider,
                account_email=connection.account_email,
                calendar_name=calendar.name,
                operation=cast("Literal['create', 'update', 'restore']", local.operation_kind),
                before=before,
                after=after,
                conflicts=calendar_conflict_previews(after, context),
                notification_policy=command.notification_policy.value,
                base_etag=None if isinstance(command, CalendarCreateCommand) else command.base_etag,
                compensation_available=isinstance(command, CalendarUpdateCommand),
                provider_warnings=["google_send_updates_none_external_sync"]
                if provider == "google" and command.notification_policy.value == "none"
                else [],
            )
        except (InvalidTag, ValueError, TypeError, ValidationError, StateConflictError):
            # 新异常在 except 外产生，防止敏感 Pydantic input 或解密载荷成为异常链。
            pass
        raise _snapshot_invalid()

    async def list_actions(
        self,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        status: str | None,
        item_kind: ActionItemKind | None,
        provider: ActionProvider | None,
        action: ActionKind | None,
    ) -> ActionListPage:
        """在数据库内做无内容 UNION、筛选和分页，不加载或解密每条审批载荷。"""
        mail, calendar, connection = (
            MailDraftModel,
            CalendarChangeProposalModel,
            OAuthConnectionModel,
        )
        local_mail = (
            select(
                mail.id.label("id"),
                literal("mail_draft").label("item_kind"),
                literal(None, Uuid()).label("task_id"),
                mail.status,
                literal("mail.send").label("action"),
                connection.provider,
                literal("high").label("risk_level"),
                mail.created_at,
                mail.updated_at,
            )
            .join(connection, connection.id == mail.connection_id)
            .where(
                mail.user_id == user_id,
                connection.user_id == user_id,
                mail.status.in_(("editing", "cancelled")),
            )
        )
        local_calendar = (
            select(
                calendar.id,
                literal("calendar_proposal"),
                literal(None, Uuid()),
                calendar.status,
                (literal("calendar.") + calendar.operation_kind),
                connection.provider,
                literal(None, String()),
                calendar.created_at,
                calendar.updated_at,
            )
            .join(connection, connection.id == calendar.connection_id)
            .where(
                calendar.user_id == user_id,
                connection.user_id == user_id,
                calendar.status.in_(("editing", "cancelled", "stale")),
            )
        )
        task, approval, step = TaskRunModel, ApprovalRequestModel, TaskStepModel
        # JSONB 精确对象相等同时限制字段集合；版本另作文本相等，拒绝 1.0/布尔/字符串。
        # 所有 UUID 都只把可信关系列转成字符串比较，从不 cast 不可信 JSON。
        legacy_summary = func.jsonb_build_object(
            "action", approval.action, "proposal_version", approval.proposal_version
        )
        indexed_summary = func.jsonb_build_object(
            "summary_version",
            "trusted_action_step.v1",
            "action",
            approval.action,
            "proposal_version",
            approval.proposal_version,
            "frozen_connection_id",
            sql_cast(connection.id, String()),
        )
        trusted = (
            select(
                task.id,
                literal("trusted_task"),
                task.id,
                task.status,
                approval.action,
                connection.provider,
                approval.risk_level,
                task.created_at,
                task.updated_at,
            )
            .join(
                approval,
                (approval.task_id == task.id)
                & (task.input_payload["approval_id"].astext == sql_cast(approval.id, String())),
            )
            .join(step, (step.id == approval.step_id) & (step.task_id == task.id))
            .outerjoin(
                mail,
                (approval.proposal_kind == "mail_draft")
                & (mail.id == approval.proposal_id)
                & (mail.user_id == user_id),
            )
            .outerjoin(
                calendar,
                (approval.proposal_kind == "calendar_proposal")
                & (calendar.id == approval.proposal_id)
                & (calendar.user_id == user_id),
            )
            .join(
                connection,
                (
                    (step.input_summary == legacy_summary)
                    & (connection.id == func.coalesce(mail.connection_id, calendar.connection_id))
                )
                | (step.input_summary == indexed_summary),
            )
            .where(
                task.user_id == user_id,
                task.kind == "trusted_action",
                connection.user_id == user_id,
                step.kind == "trusted_action",
                step.input_summary["proposal_version"].astext
                == sql_cast(approval.proposal_version, String()),
                approval.proposal_version > 0,
                approval.proposal_version
                <= func.coalesce(mail.current_version, calendar.current_version),
                approval.schema_version == func.replace(approval.action, ".", "_") + ".v1",
                (
                    (approval.proposal_kind == "mail_draft")
                    & (mail.id.is_not(None))
                    & (approval.action == "mail.send")
                )
                | (
                    (approval.proposal_kind == "calendar_proposal")
                    & (calendar.id.is_not(None))
                    & (approval.action == literal("calendar.") + calendar.operation_kind)
                ),
                task.input_payload
                == func.jsonb_build_object(
                    "approval_id",
                    sql_cast(approval.id, String()),
                    "operation_id",
                    task.input_payload["operation_id"],
                ),
                func.jsonb_typeof(task.input_payload["operation_id"]) == "string",
                task.input_payload["operation_id"].astext.op("~")(
                    "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
                ),
            )
        )
        combined = union_all(local_mail, local_calendar, trusted).subquery()
        query = select(combined)
        for column, value in (
            ("status", status),
            ("item_kind", item_kind),
            ("provider", provider),
            ("action", action),
        ):
            if value is not None:
                query = query.where(combined.c[column] == value)
        query = (
            query.order_by(combined.c.updated_at.desc(), combined.c.item_kind, combined.c.id)
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(query)).mappings().all()
        adapter: TypeAdapter[ActionListItem] = TypeAdapter(ActionListItem)
        items = []
        for row in rows:
            values = dict(row)
            values["editor_url"] = (
                None
                if row["item_kind"] == "trusted_task"
                else _editor_url(row["item_kind"], row["id"])
            )
            items.append(adapter.validate_python(values))
        return ActionListPage(items=items, limit=limit, offset=offset)


def _editor_url(kind: str, object_id: UUID) -> str:
    """只用本地 UUID 构造稳定编辑链接，不允许任何内容或供应商 opaque ID 进入 URL。"""
    return (
        f"/mail/drafts/{object_id}" if kind == "mail_draft" else f"/calendar/proposals/{object_id}"
    )


def _snapshot_invalid() -> InternalInvariantError:
    """返回内容无关错误，审批完整性失败不能伪装成可执行空预览。"""
    return InternalInvariantError(
        error_code="action_snapshot_invalid", message="action snapshot is unavailable"
    )


class SqlAlchemyActionViewRepositoryFactory:
    """为一次动作视图 mutation 创建自动提交/回滚的短事务。"""

    def __init__(
        self,
        session_factory: ManagedAsyncSessionMaker,
        encryption_factory: Callable[[], Encryption] | None = None,
    ) -> None:
        """保存进程级异步会话工厂，不提前占用连接。"""
        self._session_factory = session_factory
        self._encryption_factory = encryption_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyActionViewRepository]:
        """在单一 PostgreSQL 事务中暴露人工/CAS repository。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyActionViewRepository(session)

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[SqlAlchemyActionViewRepository]:
        """首条 SQL 设置只读可重复读，独立于 mutation 的 READ COMMITTED 锁/CAS 语义。"""
        async with self._session_factory.begin() as session:
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            yield SqlAlchemyActionViewRepository(session, self._encryption_factory)

    async def get(self, *, user_id: UUID, task_id: UUID) -> ActionSnapshot | None:
        """在一个 MVCC 快照内读取任务、冻结预览、执行及有序审计游标。"""
        async with self._read() as repository:
            return await repository.get(user_id=user_id, task_id=task_id)

    async def exists(self, *, user_id: UUID, task_id: UUID) -> bool:
        """在 mutation 前无锁检查归属，锁后状态及绑定仍由原 mutation 重新校验。"""
        async with self._session_factory() as session:
            return (
                await session.scalar(
                    select(TaskRunModel.id).where(
                        TaskRunModel.id == task_id,
                        TaskRunModel.user_id == user_id,
                        TaskRunModel.kind == "trusted_action",
                    )
                )
                is not None
            )

    async def list_actions(
        self,
        *,
        user_id: UUID,
        limit: int,
        offset: int,
        status: str | None,
        item_kind: ActionItemKind | None,
        provider: ActionProvider | None,
        action: ActionKind | None,
    ) -> ActionListPage:
        """列表仅用无内容数据库列，完全不调用惰性 Secret 加载器。"""
        async with self._read() as repository:
            return await repository.list_actions(
                user_id=user_id,
                limit=limit,
                offset=offset,
                status=status,
                item_kind=item_kind,
                provider=provider,
                action=action,
            )


__all__ = [
    "SqlAlchemyActionViewRepository",
    "SqlAlchemyActionViewRepositoryFactory",
]
