"""在任务一致性读取事务内验证恢复结果链接，不公开或修补任意Worker结果。"""

from collections.abc import Callable
from datetime import datetime
from hmac import compare_digest
from uuid import UUID

from cryptography.exceptions import InvalidTag
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.ports.encryption import EncryptionBoundaryError
from ai_employee.application.use_cases.calendar_proposals import (
    CalendarProposalContent,
    calendar_restore_creation_hash,
    validate_calendar_creation_idempotency_key,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
)
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.calendar_proposals import (
    SqlAlchemyCalendarProposalRepository,
)
from ai_employee.infrastructure.security.action_payloads import ActionPayloadCipher


async def load_calendar_restore_result(
    session: AsyncSession,
    *,
    task: TaskRunModel,
    user_id: UUID,
    cipher_factory: Callable[[], ActionPayloadCipher],
    observed_at: datetime,
) -> UUID | None:
    """返回经原始创建意图验证的唯一提案UUID；任何缺失或篡改都不给替代链接。

    Args:
        session: GET/SSE共同使用的同一可重复读事务。
        task: 已按本人条件读取的持久任务。
        user_id: 所有提案和snapshot查询再次显式注入的归属。
        cipher_factory: 仅合格恢复结果惰性读取Secret；普通M1任务无需解密。
        observed_at: 显式UTC时钟，过期v1不能继续提供可用内容入口。

    Returns:
        已认证初始desired v1并复核creation hash的restore提案UUID，或空。
    """
    if (
        task.user_id != user_id
        or task.kind != "calendar.restore.prepare"
        or task.status != "succeeded"
    ):
        return None
    inputs, result = task.input_payload, task.result_payload
    if (
        type(inputs) is not dict
        or set(inputs) != {"source_snapshot_id", "creation_idempotency_key"}
        or type(result) is not dict
        or set(result) != {"calendar_proposal_id"}
    ):
        return None
    raw_source = inputs["source_snapshot_id"]
    raw_proposal = result["calendar_proposal_id"]
    raw_key = inputs["creation_idempotency_key"]
    if (
        not isinstance(raw_source, str)
        or not isinstance(raw_proposal, str)
        or not isinstance(raw_key, str)
    ):
        return None
    try:
        source_id, proposal_id = UUID(raw_source), UUID(raw_proposal)
        creation_key = validate_calendar_creation_idempotency_key(raw_key)
    except ValueError:
        return None
    if str(source_id) != raw_source or str(proposal_id) != raw_proposal:
        return None
    # replacement任务自身有新的重试键；必须绑定其保留的原creation key，不能误用task幂等键。
    proposal = await session.scalar(
        select(CalendarChangeProposalModel).where(
            CalendarChangeProposalModel.id == proposal_id,
            CalendarChangeProposalModel.user_id == user_id,
            CalendarChangeProposalModel.operation_kind == "restore",
            CalendarChangeProposalModel.creation_idempotency_key == creation_key,
            CalendarChangeProposalModel.retain_until > observed_at,
        )
    )
    if proposal is None or proposal.target_event_id is None or proposal.base_etag is None:
        return None
    initial_id = await session.scalar(
        select(CalendarChangeSnapshotModel.id).where(
            CalendarChangeSnapshotModel.user_id == user_id,
            CalendarChangeSnapshotModel.proposal_id == proposal_id,
            CalendarChangeSnapshotModel.version == 1,
            CalendarChangeSnapshotModel.snapshot_kind == "desired",
            CalendarChangeSnapshotModel.retain_until > observed_at,
        )
    )
    if initial_id is None:
        return None
    try:
        initial = await SqlAlchemyCalendarProposalRepository(
            session, cipher_factory()
        ).load_snapshot(user_id=user_id, snapshot_id=initial_id)
        if initial is None:
            return None
        expected = calendar_restore_creation_hash(
            connection_id=proposal.connection_id,
            calendar_id=proposal.calendar_id,
            target_event_id=proposal.target_event_id,
            content=CalendarProposalContent.model_validate(initial.content),
            base_etag=proposal.base_etag,
            source_snapshot_id=source_id,
        )
        if not compare_digest(expected, proposal.creation_payload_hash):
            return None
    except (InvalidTag, EncryptionBoundaryError, TypeError, ValueError):
        return None
    except StateConflictError as error:
        if error.error_code != "calendar_snapshot_unavailable":
            raise
        return None
    return proposal_id
