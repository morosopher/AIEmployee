"""投影并认领隐私删除的一次只读核对；不解密可信命令、不扩大写执行权限。

每次调用使用独立短事务。winner/目标 Task 排序锁后按 user→approval→execution→聚合
重读绑定；现有 needs_attention/error_code 保存资格，提交后才可进入外部 GET。
"""

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.trusted_actions import (
    TrustedActionDispatchSnapshot,
    trusted_execution_binding_matches,
)
from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.privacy import (
    PrivacyDeletionBinding,
    PrivacyReconciliationTarget,
)
from ai_employee.domain.actions import ToolExecutionStatus
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
    EncryptedCredentialModel,
    OAuthConnectionModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.oauth_lifecycle import lock_oauth_cleanup_identity
from ai_employee.infrastructure.db.repositories.privacy_deletion import lock_deletion_binding
from ai_employee.infrastructure.db.repositories.trusted_actions import _execution_reference
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

PRIVACY_RECONCILIATION_STARTED = "privacy_reconciliation_started"
_TERMINAL = ("succeeded", "confirmed_failed")
_SCHEMAS = {
    "mail.send": "mail_send.v1",
    "calendar.create": "calendar_create.v1",
    "calendar.update": "calendar_update.v1",
    "calendar.restore": "calendar_restore.v1",
}


class SqlAlchemyPrivacyReconciliationStore:
    """仅服务固定 M2 删除核对的持久资格与当前 access 读取，绝无 token 轮换能力。"""

    def __init__(self, sessions: ManagedAsyncSessionMaker, *, clock: Clock | None = None) -> None:
        """借用调用者 retention factory，实例不管理连接池生命周期。"""
        self._sessions = sessions
        self._clock = clock

    async def candidates(
        self,
        *,
        user_id: UUID,
        after: UUID | None,
        limit: int,
    ) -> tuple[UUID, ...]:
        """仅扫描任务标识，keyset 保证坏绑定不反复占用当前删除轮的前缀。"""
        async with self._sessions() as session:
            query = select(TaskRunModel.id).where(
                TaskRunModel.user_id == user_id,
                TaskRunModel.kind == "trusted_action",
                TaskRunModel.id.in_(
                    select(ToolExecutionModel.task_id).where(
                        ToolExecutionModel.status.not_in(_TERMINAL),
                        (
                            ToolExecutionModel.error_code.is_(None)
                            | (ToolExecutionModel.error_code != PRIVACY_RECONCILIATION_STARTED)
                        ),
                    )
                ),
            )
            if after is not None:
                query = query.where(TaskRunModel.id > after)
            return tuple(
                (await session.scalars(query.order_by(TaskRunModel.id).limit(limit))).all()
            )

    async def qualify(
        self,
        *,
        binding: PrivacyDeletionBinding,
        task_id: UUID,
        now: datetime,
    ) -> PrivacyReconciliationTarget | None:
        """先提交一次资格并撤销旧执行租约，随后调用者才可读取供应商。

        终态和已记录资格保持原样。歧义的审批/执行集合、版本或哈希绑定没有 GET 资格；
        不因此制造 applied/not-applied，也不触碰仍将由后续本地阶段删除的密文。
        """
        async with self._sessions.begin() as session:
            await lock_deletion_binding(
                session, binding=binding, now=now, clock=self._clock, task_ids=(task_id,)
            )
            now = self._clock.now() if self._clock is not None else now
            target = await self._target(session, user_id=binding.user_id, task_id=task_id)
            if target is None:
                return None
            execution = await session.get(
                ToolExecutionModel, target.dispatch.execution.execution_id
            )
            task = await session.get(TaskRunModel, task_id)
            assert execution is not None and task is not None
            if (
                execution.status in _TERMINAL
                or execution.error_code == PRIVACY_RECONCILIATION_STARTED
            ):
                return None
            execution.status = "needs_attention"
            execution.error_code = PRIVACY_RECONCILIATION_STARTED
            task.status = "needs_attention"
            task.error_code = PRIVACY_RECONCILIATION_STARTED
            task.lease_owner = task.lease_expires_at = None
            task.scheduled_for = task.retry_recovery_at = task.approval_checkpoint_recovery_at = (
                None
            )
            task.updated_at = now
            # 资格不改旧结果、attempt counters 或 provider 标识；它只禁止重新开始本删除轮。
            return replace(
                target,
                dispatch=replace(
                    target.dispatch,
                    execution=replace(
                        target.dispatch.execution,
                        status=ToolExecutionStatus.NEEDS_ATTENTION,
                        error_code=PRIVACY_RECONCILIATION_STARTED,
                    ),
                ),
            )

    async def read_access(
        self,
        *,
        binding: PrivacyDeletionBinding,
        target: PrivacyReconciliationTarget,
        now: datetime,
    ) -> EncryptedValue | None:
        """在同一精确资格下只读当前 access；缺 capability/expiry/connection 一律零网络。

        retention 没有凭据行锁权限，使用已裁定的短 EXCLUSIVE NOWAIT 顺序防止读取期间
        连接/凭据替换。事务外才解密此单个 access，不加载 refresh token 或 current lineage。
        """
        snapshot = target.dispatch
        async with self._sessions.begin() as session:
            await lock_deletion_binding(
                session, binding=binding, now=now, clock=self._clock, task_ids=(snapshot.task_id,)
            )
            now = self._clock.now() if self._clock is not None else now
            current = await self._target(session, user_id=binding.user_id, task_id=snapshot.task_id)
            if current != target or snapshot.connection_id is None:
                return None
            if (
                current.dispatch.execution.status is not ToolExecutionStatus.NEEDS_ATTENTION
                or current.dispatch.execution.error_code != PRIVACY_RECONCILIATION_STARTED
            ):
                return None
            identity = await lock_oauth_cleanup_identity(
                session,
                user_id=binding.user_id,
                connection_id=snapshot.connection_id,
            )
            if identity is None or identity.access_id is None:
                return None
            provider = await session.scalar(
                select(OAuthConnectionModel.provider).where(
                    OAuthConnectionModel.id == snapshot.connection_id,
                    OAuthConnectionModel.user_id == binding.user_id,
                    OAuthConnectionModel.status == "connected",
                )
            )
            capability = await session.scalar(
                select(ConnectionCapabilityModel.status).where(
                    ConnectionCapabilityModel.user_id == binding.user_id,
                    ConnectionCapabilityModel.connection_id == snapshot.connection_id,
                    ConnectionCapabilityModel.capability
                    == ("mail.read" if snapshot.action == "mail.send" else "calendar.read"),
                )
            )
            if provider != snapshot.provider or capability != "enabled":
                return None
            credential = (
                await session.execute(
                    select(
                        EncryptedCredentialModel.ciphertext,
                        EncryptedCredentialModel.nonce,
                        EncryptedCredentialModel.key_version,
                    ).where(
                        EncryptedCredentialModel.id == identity.access_id,
                        EncryptedCredentialModel.user_id == binding.user_id,
                        EncryptedCredentialModel.connection_id == snapshot.connection_id,
                        EncryptedCredentialModel.credential_kind == "access_token",
                        EncryptedCredentialModel.token_expires_at > now,
                    )
                )
            ).one_or_none()
            return EncryptedValue(*credential) if credential is not None else None

    @staticmethod
    async def _target(
        session: AsyncSession, *, user_id: UUID, task_id: UUID
    ) -> PrivacyReconciliationTarget | None:
        """固定 Task/approval/execution/聚合/版本绑定；SELECT 明确排除命令与正文列。"""
        task = await session.get(TaskRunModel, task_id)
        if task is None or task.user_id != user_id or task.kind != "trusted_action":
            return None
        approvals = (
            await session.scalars(
                select(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.task_id == task_id,
                )
                .options(
                    load_only(
                        ApprovalRequestModel.id,
                        ApprovalRequestModel.task_id,
                        ApprovalRequestModel.step_id,
                        ApprovalRequestModel.version,
                        ApprovalRequestModel.action,
                        ApprovalRequestModel.schema_version,
                        ApprovalRequestModel.payload_hash,
                        ApprovalRequestModel.proposal_kind,
                        ApprovalRequestModel.proposal_id,
                        ApprovalRequestModel.proposal_version,
                    )
                )
                .order_by(ApprovalRequestModel.id)
                .with_for_update()
            )
        ).all()
        executions = (
            await session.scalars(
                select(ToolExecutionModel)
                .where(
                    ToolExecutionModel.task_id == task_id,
                )
                .order_by(ToolExecutionModel.id)
                .with_for_update()
            )
        ).all()
        if len(approvals) != 1 or len(executions) != 1:
            return None
        approval, execution = approvals[0], executions[0]
        operation_id = execution.operation_id
        if (
            operation_id is None
            or execution.provider not in {"google", "microsoft"}
            or approval.proposal_id is None
            or approval.proposal_version is None
            or approval.action not in _SCHEMAS
            or approval.schema_version != _SCHEMAS[approval.action]
            or task.input_payload
            != {"approval_id": str(approval.id), "operation_id": str(operation_id)}
            or not trusted_execution_binding_matches(
                execution_task_id=execution.task_id,
                expected_task_id=task.id,
                execution_step_id=execution.step_id,
                expected_step_id=approval.step_id,
                execution_operation_id=operation_id,
                expected_operation_id=operation_id,
                execution_provider=execution.provider,
                expected_provider=execution.provider,
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
        if approval.action == "mail.send" and approval.proposal_kind == "mail_draft":
            draft = await session.scalar(
                select(MailDraftModel)
                .where(
                    MailDraftModel.id == approval.proposal_id,
                    MailDraftModel.user_id == user_id,
                )
                .options(
                    load_only(
                        MailDraftModel.id,
                        MailDraftModel.connection_id,
                        MailDraftModel.current_version,
                    )
                )
                .with_for_update()
            )
            if draft is None or draft.current_version != approval.proposal_version:
                return None
            version = await session.scalar(
                select(MailDraftVersionModel.id).where(
                    MailDraftVersionModel.user_id == user_id,
                    MailDraftVersionModel.draft_id == draft.id,
                    MailDraftVersionModel.version == approval.proposal_version,
                )
            )
            connection_id, calendar_id, resource_id = (
                draft.connection_id,
                None,
                execution.provider_resource_id,
            )
        elif (
            approval.action.startswith("calendar.")
            and approval.proposal_kind == "calendar_proposal"
        ):
            proposal = await session.scalar(
                select(CalendarChangeProposalModel)
                .where(
                    CalendarChangeProposalModel.id == approval.proposal_id,
                    CalendarChangeProposalModel.user_id == user_id,
                )
                .options(
                    load_only(
                        CalendarChangeProposalModel.id,
                        CalendarChangeProposalModel.connection_id,
                        CalendarChangeProposalModel.calendar_id,
                        CalendarChangeProposalModel.target_event_id,
                        CalendarChangeProposalModel.current_version,
                        CalendarChangeProposalModel.operation_kind,
                    )
                )
                .with_for_update()
            )
            if (
                proposal is None
                or proposal.current_version != approval.proposal_version
                or f"calendar.{proposal.operation_kind}" != approval.action
            ):
                return None
            version = await session.scalar(
                select(CalendarChangeSnapshotModel.id).where(
                    CalendarChangeSnapshotModel.user_id == user_id,
                    CalendarChangeSnapshotModel.proposal_id == proposal.id,
                    CalendarChangeSnapshotModel.version == approval.proposal_version,
                    CalendarChangeSnapshotModel.snapshot_kind == "desired",
                )
            )
            connection_id, calendar_id = proposal.connection_id, proposal.calendar_id
            resource_id = (
                execution.provider_resource_id
                if approval.action == "calendar.create"
                else proposal.target_event_id
            )
        else:
            return None
        if version is None:
            return None
        provider = await session.scalar(
            select(OAuthConnectionModel.provider).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == user_id,
            )
        )
        if provider != execution.provider:
            return None
        return PrivacyReconciliationTarget(
            TrustedActionDispatchSnapshot(
                user_id=user_id,
                task_id=task_id,
                step_id=approval.step_id,
                approval_id=approval.id,
                approval_version=approval.version,
                operation_id=operation_id,
                connection_id=connection_id,
                calendar_id=calendar_id,
                action=approval.action,
                schema_version=approval.schema_version,
                payload_hash=approval.payload_hash,
                proposal_kind=approval.proposal_kind,
                proposal_id=approval.proposal_id,
                proposal_version=approval.proposal_version,
                provider=provider,
                execution=_execution_reference(execution=execution, approval_id=approval.id),
            ),
            resource_id,
        )
