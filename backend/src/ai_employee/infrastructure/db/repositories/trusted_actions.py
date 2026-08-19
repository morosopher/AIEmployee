"""在 ApprovalRequest 上持久化严格验证且记录绑定的加密可信命令。"""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.commands import canonical_command_json, trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.ports.trusted_actions import (
    CalendarProposalSubmissionSnapshot,
    ExistingTrustedActionSubmission,
    MailDraftSubmissionSnapshot,
    TrustedActionSubmission,
)
from ai_employee.application.use_cases.calendar_proposals import CalendarProposalContent
from ai_employee.domain.actions import CalendarProposalStatus, MailDraftStatus
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
from ai_employee.infrastructure.db.models.sources import (
    CalendarEventModel,
    ConnectionCapabilityModel,
    EmailMessageModel,
    OAuthConnectionModel,
    ProviderCalendarModel,
)
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
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

        该查询必须先于本地版本锁执行，因此相同请求即使资源随后进入待审批态，也只会
        返回原任务。异常的 trusted task（缺审批或缺无敏感 ID 输入）整体 fail closed。
        """
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
        source_message_id: str | None,
    ) -> ReplyThreadHeaders | None:
        """从同步消息规范 Header 构造回复命令引用；新邮件明确为空。"""
        if mode is MailMode.NEW:
            return None
        if source_message_id is None:
            return None
        message = await self._session.scalar(
            select(EmailMessageModel).where(
                EmailMessageModel.user_id == user_id,
                EmailMessageModel.connection_id == connection_id,
                EmailMessageModel.provider_message_id == source_message_id,
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
        and content.confirmed_fields == (
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
