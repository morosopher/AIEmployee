"""在既有本地 CAS 事务中验证并升级精确 legacy 冻结账户索引。

调用方必须已成功锁定 editing 父行。函数先读取和验证所有历史，再一次性写入步骤摘要；
任一验证失败都抛稳定冲突并由外层事务回滚，不触发任务、审批或供应商调用。
"""

from hmac import compare_digest
from uuid import UUID

from cryptography.exceptions import InvalidTag
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.commands import parse_trusted_command, trusted_command_hash
from ai_employee.application.ports.encryption import EncryptedValue
from ai_employee.application.trusted_action_summary import (
    TrustedActionStepSummary,
    parse_trusted_action_step_summary,
)
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarProposalContent,
    CalendarProposalRepository,
)
from ai_employee.domain.calendar_actions import CalendarCreateCommand
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.mail_actions import MailSendCommand
from ai_employee.infrastructure.db.models.actions import CalendarChangeSnapshotModel
from ai_employee.infrastructure.db.models.sources import OAuthConnectionModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    TaskStepModel,
)
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher


def historical_binding_unavailable() -> StateConflictError:
    """返回明确的新建恢复信号，不泄露哪一条历史内容或账户缺失。"""
    return StateConflictError(
        error_code="historical_action_binding_unavailable",
        message="historical action binding cannot be verified; create a new local action",
    )


async def preserve_historical_action_bindings(
    session: AsyncSession,
    cipher: ActionPayloadCipher,
    *,
    user_id: UUID,
    proposal_kind: str,
    proposal_id: UUID,
    original_connection_id: UUID,
    original_version: int,
    original_calendar_id: str | None = None,
    calendar_proposals: CalendarProposalRepository | None = None,
) -> None:
    """升级本地对象全部精确旧摘要，且拒绝跨用户引用和不可证明的清除历史。

    父行 CAS 已阻止并发提交/编辑。这里只锁步骤，避免逆转既有 Task→Approval→local
    执行锁序；审批密文由单次查询冻结，后续内容保留清理不影响已验证的最小账户事实。
    新格式仅验证内容无关绑定，禁止用密文修补损坏的索引。
    """
    # 不读取其他用户内容；只检查是否存在非法跨用户反向引用，发现即整体拒绝。
    foreign_reference = await session.scalar(
        select(ApprovalRequestModel.id)
        .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
        .where(
            ApprovalRequestModel.proposal_kind == proposal_kind,
            ApprovalRequestModel.proposal_id == proposal_id,
            TaskRunModel.user_id != user_id,
        )
        .limit(1)
    )
    if foreign_reference is not None:
        raise historical_binding_unavailable()
    rows = (
        await session.execute(
            select(ApprovalRequestModel, TaskRunModel, TaskStepModel)
            .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
            .join(
                TaskStepModel,
                (TaskStepModel.id == ApprovalRequestModel.step_id)
                & (TaskStepModel.task_id == TaskRunModel.id),
            )
            .where(
                ApprovalRequestModel.proposal_kind == proposal_kind,
                ApprovalRequestModel.proposal_id == proposal_id,
                TaskRunModel.user_id == user_id,
            )
            .order_by(TaskStepModel.id)
            .with_for_update(of=TaskStepModel)
        )
    ).all()
    upgraded: list[tuple[TaskStepModel, TrustedActionStepSummary]] = []
    for approval, task, step in rows:
        summary = parse_trusted_action_step_summary(step.input_summary)
        if (
            task.user_id != user_id
            or task.kind != "trusted_action"
            or summary is None
            or summary.action != approval.action
            or summary.proposal_version != approval.proposal_version
            or summary.proposal_version > original_version
            or approval.schema_version != summary.action.replace(".", "_") + ".v1"
            or not isinstance(task.input_payload, dict)
            or set(task.input_payload) != {"approval_id", "operation_id"}
            or task.input_payload.get("approval_id") != str(approval.id)
        ):
            raise historical_binding_unavailable()
        try:
            raw_operation = task.input_payload.get("operation_id")
            if type(raw_operation) is not str:
                raise ValueError("invalid operation identity")
            operation_id = UUID(raw_operation)
            if str(operation_id) != raw_operation:
                raise ValueError("noncanonical operation identity")
            connection_id = summary.frozen_connection_id
            if connection_id is None:
                if (
                    approval.payload_ciphertext is None
                    or approval.payload_nonce is None
                    or approval.payload_key_version is None
                    or approval.schema_version is None
                ):
                    raise ValueError("historical command unavailable")
                payload = cipher.decrypt_json(
                    EncryptedValue(
                        approval.payload_ciphertext,
                        approval.payload_nonce,
                        approval.payload_key_version,
                    ),
                    user_id=user_id,
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
                    or command.connection_id != original_connection_id
                ):
                    raise ValueError("invalid historical command binding")
                if isinstance(command, MailSendCommand):
                    if (
                        proposal_kind != "mail_draft"
                        or command.draft_id != proposal_id
                        or command.draft_version != approval.proposal_version
                    ):
                        raise ValueError("invalid historical draft identity")
                else:
                    if (
                        proposal_kind != "calendar_proposal"
                        or command.calendar_id != original_calendar_id
                        or calendar_proposals is None
                    ):
                        raise ValueError("invalid historical proposal identity")
                    # 只有 create 提案允许重选目标；update/restore 来源不可换绑。
                    if not isinstance(command, CalendarCreateCommand):
                        raise TypeError("historical source binding is immutable")
                    # Calendar 命令不重复保存 proposal ID/version，使用已认证的原 desired
                    # 快照把审批外键绑定到精确版本，再与冻结命令的完整可写字段比较。
                    snapshot_id = await session.scalar(
                        select(CalendarChangeSnapshotModel.id).where(
                            CalendarChangeSnapshotModel.user_id == user_id,
                            CalendarChangeSnapshotModel.proposal_id == proposal_id,
                            CalendarChangeSnapshotModel.version == approval.proposal_version,
                            CalendarChangeSnapshotModel.snapshot_kind == "desired",
                        )
                    )
                    snapshot = (
                        None
                        if snapshot_id is None
                        else await calendar_proposals.load_snapshot(
                            user_id=user_id, snapshot_id=snapshot_id
                        )
                    )
                    if snapshot is None:
                        raise ValueError("historical desired snapshot unavailable")
                    content = CalendarProposalContent.model_validate(snapshot.content)
                    if any(
                        getattr(content, name) != getattr(command, name)
                        for name in (
                            "title",
                            "description",
                            "location",
                            "timezone",
                            "all_day",
                            "attendees",
                            "notification_policy",
                        )
                    ):
                        raise ValueError("historical desired fields mismatch")
                    from datetime import date, datetime

                    parser = date.fromisoformat if command.all_day else datetime.fromisoformat
                    if (
                        content.starts_at is None
                        or content.ends_at is None
                        or parser(content.starts_at) != command.starts_at
                        or parser(content.ends_at) != command.ends_at
                    ):
                        raise ValueError("historical desired interval mismatch")
                connection_id = command.connection_id
                upgraded.append(
                    (
                        step,
                        TrustedActionStepSummary(
                            summary.action, summary.proposal_version, connection_id
                        ),
                    )
                )
            owned = await session.scalar(
                select(OAuthConnectionModel.id).where(
                    OAuthConnectionModel.id == connection_id,
                    OAuthConnectionModel.user_id == user_id,
                )
            )
            if owned is None:
                raise ValueError("historical connection unavailable")
        except (InvalidTag, ValidationError, TypeError, ValueError, StateConflictError):
            raise historical_binding_unavailable() from None
    for step, summary in upgraded:
        step.input_summary = summary.as_json()
