"""在固定 M2 任务锁序下收敛到期/来源清理动作，不解密或调用供应商。

retention 与 privacy 共用同一实现，避免正文、审批和未知执行状态各自提交。这里只
处理邮件草稿和日历提案；不提供任意表/动作注册，也不扩大 retention 的列级权限。
"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.privacy import PrivacyDeletionBinding
from ai_employee.domain.actions import ToolExecutionStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    CalendarChangeSnapshotModel,
    MailDraftModel,
    MailDraftVersionModel,
)
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.sources import CalendarEventModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.privacy_deletion import lock_deletion_binding
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker

ActionKind = Literal["mail_draft", "calendar_proposal"]
ActionModel = MailDraftModel | CalendarChangeProposalModel
ContentModel = MailDraftVersionModel | CalendarChangeSnapshotModel


class ActionCleanupMode(StrEnum):
    """三个已批准的本地清理场景；全数据阶段先只失效未认领动作。"""

    EXPIRED = "expired"
    SOURCE_CACHE = "source_cache"
    ALL_DATA = "all_data"


class ActionLifecycleCleanup:
    """按聚合独立提交，锁后重读期限、审批绑定、哈希及执行状态再清除内容。"""

    def __init__(
        self, session_factory: ManagedAsyncSessionMaker, *, clock: Clock | None = None
    ) -> None:
        """注入已有 retention 专用工厂，不创建第二套连接生命周期。"""
        self._sessions = session_factory
        self._clock = clock

    async def clean_user(
        self,
        *,
        user_id: UUID,
        now: datetime,
        batch_size: int,
        mode: ActionCleanupMode,
        deletion_binding: PrivacyDeletionBinding | None = None,
        metadata_cutoff: datetime | None = None,
    ) -> None:
        """分页定位两类动作；keyset 防止未到期/已清空行在同一轮反复占用前缀。

        扫描只提供身份，不能授权写入。每个聚合在自己的短事务重检所有条件，任何
        并发新审批造成锁集合变化时整笔退出，调用方可由下一轮/原删除任务恢复。
        """
        for kind, model in (
            ("mail_draft", MailDraftModel),
            ("calendar_proposal", CalendarChangeProposalModel),
        ):
            after: UUID | None = None
            while True:
                async with self._sessions() as session:
                    statement = select(model.id).where(model.user_id == user_id)
                    if after is not None:
                        statement = statement.where(model.id > after)
                    ids = (
                        await session.scalars(statement.order_by(model.id).limit(batch_size))
                    ).all()
                if not ids:
                    break
                for action_id in ids:
                    await self._clean_action(
                        user_id=user_id,
                        action_id=action_id,
                        kind="mail_draft" if kind == "mail_draft" else "calendar_proposal",
                        now=now,
                        mode=mode,
                        deletion_binding=deletion_binding,
                        metadata_cutoff=metadata_cutoff,
                    )
                after = ids[-1]

    async def _clean_action(
        self,
        *,
        user_id: UUID,
        action_id: UUID,
        kind: ActionKind,
        now: datetime,
        mode: ActionCleanupMode,
        deletion_binding: PrivacyDeletionBinding | None,
        metadata_cutoff: datetime | None,
    ) -> None:
        """严格 Task→approval→execution→action→content 锁序，成对清空并提交一次。"""
        async with self._sessions.begin() as session:
            if mode is ActionCleanupMode.ALL_DATA:
                if deletion_binding is None or deletion_binding.user_id != user_id:
                    raise ValueError("all-data cleanup requires exact deletion binding")
                ids = (
                    await session.scalars(
                        select(TaskRunModel.id).where(
                            TaskRunModel.user_id == user_id,
                            TaskRunModel.id.in_(
                                select(ApprovalRequestModel.task_id).where(
                                    ApprovalRequestModel.proposal_id == action_id,
                                    ApprovalRequestModel.proposal_kind == kind,
                                )
                            ),
                        )
                    )
                ).all()
                await lock_deletion_binding(
                    session,
                    binding=deletion_binding,
                    task_ids=ids,
                    now=now,
                    clock=self._clock,
                )
            tasks = (
                await session.scalars(
                    select(TaskRunModel)
                    .where(
                        TaskRunModel.user_id == user_id,
                        TaskRunModel.id.in_(
                            select(ApprovalRequestModel.task_id).where(
                                ApprovalRequestModel.proposal_id == action_id,
                                ApprovalRequestModel.proposal_kind == kind,
                            )
                        ),
                    )
                    .order_by(TaskRunModel.id)
                    .with_for_update()
                )
            ).all()
            if mode is not ActionCleanupMode.ALL_DATA:
                active = await session.scalar(
                    select(UserModel.is_active)
                    .where(
                        UserModel.id == user_id,
                    )
                    .with_for_update()
                )
                if active is not True:
                    # inactive 的执行/资格只由唯一 privacy 赢家收敛，普通 retention 不能
                    # 抹除一次读取标记而让故障恢复重新取得 GET 资格。
                    return
            task_ids = tuple(task.id for task in tasks)
            approvals = (
                await session.scalars(
                    select(ApprovalRequestModel)
                    .where(
                        ApprovalRequestModel.task_id.in_(task_ids),
                        ApprovalRequestModel.proposal_id == action_id,
                        ApprovalRequestModel.proposal_kind == kind,
                    )
                    .order_by(ApprovalRequestModel.id)
                    .with_for_update()
                )
            ).all()
            executions = (
                await session.scalars(
                    select(ToolExecutionModel)
                    .where(ToolExecutionModel.task_id.in_(task_ids))
                    .order_by(ToolExecutionModel.id)
                    .with_for_update()
                )
            ).all()
            model = MailDraftModel if kind == "mail_draft" else CalendarChangeProposalModel
            action = await session.scalar(
                select(model)
                .where(
                    model.id == action_id,
                    model.user_id == user_id,
                )
                .with_for_update()
            )
            if not isinstance(action, (MailDraftModel, CalendarChangeProposalModel)):
                return
            # submit 可能在预读 task 集合后抢先提交。不能反向补锁 TaskRun，也不能漏清
            # 新审批的一侧；退出事务保留其内容，下一轮会以完整锁集合重新定位。
            current_approval_ids = tuple(
                (
                    await session.scalars(
                        select(ApprovalRequestModel.id)
                        .join(TaskRunModel)
                        .where(
                            TaskRunModel.user_id == user_id,
                            ApprovalRequestModel.proposal_id == action_id,
                            ApprovalRequestModel.proposal_kind == kind,
                        )
                        .order_by(ApprovalRequestModel.id)
                    )
                ).all()
            )
            if current_approval_ids != tuple(approval.id for approval in approvals):
                raise StateConflictError(
                    error_code="action_cleanup_conflict",
                    message="Action changed during cleanup",
                )
            contents = await self._lock_contents(session, user_id=user_id, action=action)
            metadata_expired = (
                isinstance(action, MailDraftModel)
                and metadata_cutoff is not None
                and bool(contents)
                and max(content.created_at for content in contents) < metadata_cutoff
            )
            if mode is ActionCleanupMode.SOURCE_CACHE and not self._source_bound(action):
                return
            if mode is ActionCleanupMode.EXPIRED:
                deadline = await self._deadline(session, user_id=user_id, action=action)
                if deadline >= now and not metadata_expired:
                    return
            if mode is ActionCleanupMode.ALL_DATA and executions:
                # 删除屏障后的单次只读核对由 privacy 独立执行；不能先破坏其引用事实。
                return
            self._converge(tasks, approvals, executions, action, now=now)
            for approval in approvals:
                approval.payload_ciphertext = None
                approval.payload_nonce = None
                approval.payload_key_version = None
            for content in contents:
                if isinstance(content, MailDraftVersionModel):
                    content.body_ciphertext = content.body_nonce = content.body_key_version = None
                else:
                    content.content_ciphertext = content.content_nonce = (
                        content.content_key_version
                    ) = None
            await session.flush()
            if mode is ActionCleanupMode.EXPIRED and metadata_expired:
                # 冻结 ACL 不允许 UPDATE 地址/主题；显式删除版本即可移除这些180天元数据。
                # 聚合、审批哈希和执行外部结果仍保留，未知副作用的人工入口不会丢失。
                for content in contents:
                    await session.delete(content)
            if mode is ActionCleanupMode.SOURCE_CACHE and not executions:
                # 显式子→审批→聚合删除，不把 cascade 当作本地数据完整删除的证明。
                for content in contents:
                    await session.delete(content)
                await session.flush()
                for approval in approvals:
                    await session.delete(approval)
                await session.flush()
                await session.delete(action)

    @staticmethod
    async def _lock_contents(
        session: AsyncSession,
        *,
        user_id: UUID,
        action: ActionModel,
    ) -> tuple[ContentModel, ...]:
        """在聚合锁之后锁定全部不可变版本；审批不能残留另一版本的可解密载荷。"""
        if isinstance(action, MailDraftModel):
            return tuple(
                (
                    await session.scalars(
                        select(MailDraftVersionModel)
                        .where(
                            MailDraftVersionModel.user_id == user_id,
                            MailDraftVersionModel.draft_id == action.id,
                        )
                        .order_by(MailDraftVersionModel.version)
                        .with_for_update()
                    )
                ).all()
            )
        return tuple(
            (
                await session.scalars(
                    select(CalendarChangeSnapshotModel)
                    .where(
                        CalendarChangeSnapshotModel.user_id == user_id,
                        CalendarChangeSnapshotModel.proposal_id == action.id,
                    )
                    .order_by(CalendarChangeSnapshotModel.version, CalendarChangeSnapshotModel.id)
                    .with_for_update()
                )
            ).all()
        )

    @staticmethod
    def _source_bound(action: ActionModel) -> bool:
        """只按冻结领域字段判断来源绑定，不用模式、供应商或目录猜测。"""
        if isinstance(action, MailDraftModel):
            return action.source_thread_id is not None or action.source_message_id is not None
        return action.target_event_id is not None

    @staticmethod
    async def _deadline(
        session: AsyncSession,
        *,
        user_id: UUID,
        action: ActionModel,
    ) -> datetime:
        """日历以精确 user/connection/calendar/event 结束+180天为准，无事件才用提案期限。"""
        if isinstance(action, CalendarChangeProposalModel) and action.target_event_id is not None:
            end = await session.scalar(
                select(CalendarEventModel.ends_at)
                .where(
                    CalendarEventModel.user_id == user_id,
                    CalendarEventModel.connection_id == action.connection_id,
                    CalendarEventModel.calendar_id == action.calendar_id,
                    CalendarEventModel.provider_event_id == action.target_event_id,
                )
                .with_for_update()
            )
            if end is not None:
                return end + timedelta(days=180)
        return action.retain_until

    @staticmethod
    def _converge(
        tasks: Sequence[TaskRunModel],
        approvals: Sequence[ApprovalRequestModel],
        executions: Sequence[ToolExecutionModel],
        action: ActionModel,
        *,
        now: datetime,
    ) -> None:
        """收敛失效状态，保留同一次未认领取消的首次完成时间及已确定的执行结论。

        重复处理仍清除调度和租约，不能把整个动作当作已处理而跳过；但刷新 finished_at
        会令任务永远达不到历史截止。未知副作用只转人工处理，不改写 provider/check/count。
        """
        terminal = {ToolExecutionStatus.SUCCEEDED.value, ToolExecutionStatus.CONFIRMED_FAILED.value}
        nonterminal = [item for item in executions if item.status not in terminal]
        claimed_task_ids = {item.task_id for item in executions}
        pending_task_ids = {item.task_id for item in nonterminal}
        for execution in nonterminal:
            execution.status = ToolExecutionStatus.NEEDS_ATTENTION.value
            execution.error_code = "action_content_expired"
        for task in tasks:
            if task.id in claimed_task_ids and task.id not in pending_task_ids:
                continue
            repeated_cancellation = (
                task.status == TaskStatus.CANCELLED.value
                and task.error_code == "action_content_expired"
                and task.finished_at is not None
            )
            task.status = (
                TaskStatus.NEEDS_ATTENTION.value
                if task.id in pending_task_ids
                else TaskStatus.CANCELLED.value
            )
            task.error_code = "action_content_expired"
            task.scheduled_for = task.retry_recovery_at = task.approval_checkpoint_recovery_at = (
                None
            )
            task.lease_owner = task.lease_expires_at = None
            task.updated_at = now
            if task.id not in claimed_task_ids and not repeated_cancellation:
                task.finished_at = now
        for approval in approvals:
            if approval.task_id not in claimed_task_ids:
                approval.status = ApprovalStatus.INVALIDATED.value
        if nonterminal or not executions:
            action.status = "needs_attention" if nonterminal else "cancelled"
            action.updated_at = now
