"""连续持有 Task→user 锁回收过期终态任务，避免 Checkpoint 失去归属或被迟到保存复活。

业务表始终由 retention 连接显式删除；app 端口只清理原生 checkpoint 三表。两条连接
之间没有供应商或模型 I/O，外层事务一直持锁至父任务删除提交，失败保留父行供下一轮恢复。
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.privacy import (
    PRIVACY_DELETION_STARTED_EVENT_TYPE,
    ExpiredTaskCheckpointCleaner,
)
from ai_employee.domain.actions import ToolExecutionStatus
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.briefs import DailyBriefModel, LLMInvocationModel
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.privacy_deletion import deletion_unavailable
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

_TERMINAL_TASKS = (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value)
_TERMINAL_EXECUTIONS = (
    ToolExecutionStatus.SUCCEEDED.value,
    ToolExecutionStatus.CONFIRMED_FAILED.value,
)


class TaskHistoryCleanup:
    """在唯一外层事务内协调过期任务图与受限 app checkpoint 回调。"""

    def __init__(
        self,
        sessions: ManagedAsyncSessionMaker,
        checkpoint_cleaner: ExpiredTaskCheckpointCleaner | None,
    ) -> None:
        """注入专用 retention 工厂及独立 app 窄端口；缺少端口时不删除任何父任务。"""
        self._sessions = sessions
        self._checkpoints = checkpoint_cleaner

    async def clean_user(self, *, user_id: UUID, cutoff: datetime, batch_size: int) -> None:
        """按有界 keyset 批次锁 Task→active user，清三表后显式删除终态依赖图。

        不回收尚有未知工具结果、未发布 Outbox、保留简报或删除 started 的任务。
        keyset 让仍有有效日程内容的候选保留到下一轮，不让本轮陷入零变更循环。
        """
        after: UUID | None = None
        while True:
            async with self._sessions.begin() as session:
                await session.execute(text("SET LOCAL statement_timeout = '10s'"))
                candidates = select(TaskRunModel).where(
                    TaskRunModel.user_id == user_id,
                    TaskRunModel.status.in_(_TERMINAL_TASKS),
                    TaskRunModel.finished_at < cutoff,
                    ~exists(
                        select(ToolExecutionModel.id).where(
                            ToolExecutionModel.task_id == TaskRunModel.id,
                            ToolExecutionModel.status.not_in(_TERMINAL_EXECUTIONS),
                        )
                    ),
                    ~exists(
                        select(OutboxEventModel.id).where(
                            OutboxEventModel.aggregate_id == TaskRunModel.id,
                            OutboxEventModel.published_at.is_(None),
                        )
                    ),
                    ~exists(
                        select(DailyBriefModel.id).where(
                            DailyBriefModel.user_id == user_id,
                            DailyBriefModel.task_id == TaskRunModel.id,
                        )
                    ),
                )
                if after is not None:
                    candidates = candidates.where(TaskRunModel.id > after)
                tasks = (
                    await session.scalars(
                        candidates.order_by(TaskRunModel.id).limit(batch_size).with_for_update()
                    )
                ).all()
                if not tasks:
                    return
                after = tasks[-1].id
                active = await session.scalar(
                    select(UserModel.is_active)
                    .where(
                        UserModel.id == user_id,
                    )
                    .with_for_update()
                )
                authority = await session.scalar(
                    select(AuditEventModel.id)
                    .where(
                        AuditEventModel.user_id == user_id,
                        AuditEventModel.event_type == PRIVACY_DELETION_STARTED_EVENT_TYPE,
                    )
                    .limit(1)
                )
                if active is not True or authority is not None:
                    return
                if self._checkpoints is None:
                    raise RuntimeError("task history cleanup requires the app checkpoint cleaner")
                task_ids = tuple(task.id for task in tasks)
                approvals = (
                    await session.scalars(
                        select(ApprovalRequestModel)
                        .where(
                            ApprovalRequestModel.task_id.in_(task_ids),
                        )
                        .order_by(ApprovalRequestModel.id)
                        .with_for_update()
                    )
                ).all()
                executions = (
                    await session.scalars(
                        select(ToolExecutionModel)
                        .where(
                            ToolExecutionModel.task_id.in_(task_ids),
                        )
                        .order_by(ToolExecutionModel.id)
                        .with_for_update()
                    )
                ).all()
                if any(row.status not in _TERMINAL_EXECUTIONS for row in executions):
                    raise deletion_unavailable()
                deletable_actions, protected = await self._lock_actions(
                    session,
                    user_id=user_id,
                    task_ids=task_ids,
                    approvals=approvals,
                )
                eligible = tuple(task_id for task_id in task_ids if task_id not in protected)
                for task_id in eligible:
                    # 必须 await 在当前 begin 内：app 三表提交后直到业务父行提交，原 Task
                    # 锁持续有效，guarded saver 不能利用两条连接的提交间隙重新插入数据。
                    await self._checkpoints.clear_expired_thread(
                        user_id=user_id,
                        task_id=task_id,
                        cutoff=cutoff,
                    )
                current = tuple(
                    (
                        await session.scalars(
                            select(TaskRunModel.id)
                            .where(
                                TaskRunModel.user_id == user_id,
                                TaskRunModel.id.in_(eligible),
                                TaskRunModel.status.in_(_TERMINAL_TASKS),
                                TaskRunModel.finished_at < cutoff,
                            )
                            .order_by(TaskRunModel.id)
                        )
                    ).all()
                )
                if current != eligible:
                    raise deletion_unavailable()
                for action in deletable_actions:
                    if isinstance(action, MailDraftModel):
                        await session.execute(
                            delete(MailDraftVersionModel).where(
                                MailDraftVersionModel.user_id == user_id,
                                MailDraftVersionModel.draft_id == action.id,
                            )
                        )
                    else:
                        await session.execute(
                            delete(CalendarChangeSnapshotModel).where(
                                CalendarChangeSnapshotModel.user_id == user_id,
                                CalendarChangeSnapshotModel.proposal_id == action.id,
                            )
                        )
                for child in (
                    LLMInvocationModel,
                    ToolExecutionModel,
                    ApprovalRequestModel,
                    TaskStepModel,
                ):
                    await session.execute(delete(child).where(child.task_id.in_(eligible)))
                for action in deletable_actions:
                    await session.delete(action)
                await session.flush()
                await session.execute(
                    delete(OutboxEventModel).where(
                        OutboxEventModel.aggregate_id.in_(eligible),
                        OutboxEventModel.published_at.is_not(None),
                    )
                )
                await session.execute(
                    delete(TaskRunModel).where(
                        TaskRunModel.user_id == user_id,
                        TaskRunModel.id.in_(eligible),
                    )
                )

    @staticmethod
    async def _lock_actions(
        session: AsyncSession,
        *,
        user_id: UUID,
        task_ids: Sequence[UUID],
        approvals: Sequence[ApprovalRequestModel],
    ) -> tuple[list[MailDraftModel | CalendarChangeProposalModel], set[UUID]]:
        """审批/执行之后锁动作和内容，保留别的任务引用、新版本与尚未到期的内容。

        未来日程的事件结束+180天期限可能晚于任务历史期限；其内容尚未被前一阶段
        清除时，连同最后的任务归属留到下轮。共享动作仅删除本批旧任务的事实。
        """
        removable: list[MailDraftModel | CalendarChangeProposalModel] = []
        protected: set[UUID] = set()
        for kind, model in (
            ("mail_draft", MailDraftModel),
            ("calendar_proposal", CalendarChangeProposalModel),
        ):
            ids = {
                row.proposal_id
                for row in approvals
                if row.proposal_kind == kind and row.proposal_id
            }
            actions = (
                await session.scalars(
                    select(model)
                    .where(
                        model.user_id == user_id,
                        model.id.in_(ids),
                    )
                    .order_by(model.id)
                    .with_for_update()
                )
            ).all()
            for action in actions:
                if not isinstance(action, (MailDraftModel, CalendarChangeProposalModel)):
                    continue
                references = [
                    row
                    for row in approvals
                    if row.proposal_kind == kind and row.proposal_id == action.id
                ]
                remaining = await session.scalar(
                    select(ApprovalRequestModel.id)
                    .join(TaskRunModel)
                    .where(
                        TaskRunModel.user_id == user_id,
                        ApprovalRequestModel.proposal_kind == kind,
                        ApprovalRequestModel.proposal_id == action.id,
                        ApprovalRequestModel.task_id.not_in(task_ids),
                    )
                    .limit(1)
                )
                if remaining is not None or action.current_version > max(
                    row.proposal_version or 0 for row in references
                ):
                    continue
                if action.status not in ("sent", "applied", "cancelled"):
                    continue
                if isinstance(action, MailDraftModel):
                    bodies = (
                        await session.scalars(
                            select(MailDraftVersionModel.body_ciphertext)
                            .where(
                                MailDraftVersionModel.user_id == user_id,
                                MailDraftVersionModel.draft_id == action.id,
                            )
                            .order_by(MailDraftVersionModel.version)
                            .with_for_update()
                        )
                    ).all()
                else:
                    bodies = (
                        await session.scalars(
                            select(CalendarChangeSnapshotModel.content_ciphertext)
                            .where(
                                CalendarChangeSnapshotModel.user_id == user_id,
                                CalendarChangeSnapshotModel.proposal_id == action.id,
                            )
                            .order_by(CalendarChangeSnapshotModel.id)
                            .with_for_update()
                        )
                    ).all()
                if any(body is not None for body in bodies):
                    protected.update(row.task_id for row in references)
                else:
                    removable.append(action)
        # 一个任务异常地引用多个动作时，不提前删除受保护任务仍引用的另一聚合。
        return [
            action
            for action in removable
            if not any(
                row.task_id in protected and row.proposal_id == action.id for row in approvals
            )
        ], protected
