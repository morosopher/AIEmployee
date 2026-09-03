"""实现可信动作视图的用户隔离读取、人工 CAS 与只读核对重开事务。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.trusted_actions import trusted_execution_binding_matches
from ai_employee.application.use_cases.action_views import (
    ActionSnapshot,
    ActionViewTransaction,
)
from ai_employee.application.use_cases.task_execution import utc_instant
from ai_employee.application.use_cases.trusted_actions import validate_provider_url
from ai_employee.domain.actions import (
    CalendarProposalStatus,
    MailDraftStatus,
    ProviderWriteOutcomeKind,
    ToolExecutionStatus,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalStatus, JsonValue, TaskStatus, transition_task
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    MailDraftModel,
)
from ai_employee.infrastructure.db.models.sources import (
    OAuthConnectionModel,
    ProviderCalendarModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

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
    if set(payload) != {"approval_id", "operation_id"}:
        return None
    raw_approval = payload.get("approval_id")
    raw_operation = payload.get("operation_id")
    if type(raw_approval) is not str or type(raw_operation) is not str:
        return None
    try:
        return UUID(raw_approval), UUID(raw_operation)
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

    def __init__(self, session: AsyncSession) -> None:
        """绑定由 factory 管理生命周期的异步会话；repository 不自行提交。"""
        self._session = session

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
        """返回不含正文的最小动作快照，供后续 REST 层复用同一游标规则。"""
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
        return ActionSnapshot(
            task_id=task_id,
            status=task.status,
            error_code=task.error_code,
            event_cursor=str(cursor),
            task_version=str(cursor),
            reconciliation_attempt_count=attempts,
            provider_url=provider_url,
        )


class SqlAlchemyActionViewRepositoryFactory:
    """为一次动作视图 mutation 创建自动提交/回滚的短事务。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级异步会话工厂，不提前占用连接。"""
        self._session_factory = session_factory

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[SqlAlchemyActionViewRepository]:
        """在单一 PostgreSQL 事务中暴露人工/CAS repository。"""
        async with self._session_factory.begin() as session:
            yield SqlAlchemyActionViewRepository(session)


__all__ = [
    "SqlAlchemyActionViewRepository",
    "SqlAlchemyActionViewRepositoryFactory",
]
