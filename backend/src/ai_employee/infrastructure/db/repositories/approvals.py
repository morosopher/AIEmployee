"""以 PostgreSQL 锁实现审批决定与过期终止。"""

from datetime import datetime, timedelta
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select, text

from ai_employee.application.use_cases.approvals import (
    FakeToolClaim,
    FakeWriteTask,
    PendingApproval,
)
from ai_employee.domain.actions import CalendarProposalStatus, MailDraftStatus
from ai_employee.domain.connections import CapabilityStatus, ConnectionCapability
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus, StepStatus, TaskStatus
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    MailDraftModel,
)
from ai_employee.infrastructure.db.models.sources import (
    ConnectionCapabilityModel,
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
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.observability.metrics import M2_ACTIONS, Metrics


class SqlAlchemyApprovalStore:
    """在短事务内锁定审批并同步写入任务、审计与恢复事件。"""

    def __init__(
        self, session_factory: ManagedAsyncSessionMaker, *, metrics: Metrics | None = None
    ) -> None:
        """保存进程级会话工厂而不提前占用数据库连接。"""
        self._session_factory = session_factory
        self._metrics = metrics

    async def get_fake_write_task(self, *, task_id: UUID) -> FakeWriteTask | None:
        """读取恢复 LangGraph 所需的最小任务快照。

        Worker 只消费这个应用层快照，因此不需要导入 ORM 模型或自行发起 SQL 查询。
        """
        async with self._session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            if task is None:
                return None
            return FakeWriteTask(
                kind=task.kind,
                input_payload=task.input_payload,
                status=task.status,
            )

    async def create_or_get_pending(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        proposal: ApprovalProposal,
        preview_markdown: str,
        expires_at: datetime,
        checkpoint_recovery_at: datetime,
    ) -> PendingApproval:
        """仅当前 ``RUNNING`` owner 能冻结提案并暂停任务。

        该租约 CAS 必须与审批和状态迁移处于同一事务：旧 Worker 即使在其租约被接管后
        才到达中断点，也不能把新 owner 正在执行的任务错误地改为等待审批。
        """
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
                .with_for_update()
            )
            if task is None:
                raise StateConflictError(error_code="task_conflict", message="task is unavailable")
            existing = await session.scalar(
                select(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.task_id == task_id,
                    ApprovalRequestModel.payload_hash == proposal.payload_hash,
                )
                .with_for_update()
            )
            if existing is not None:
                if existing.status != ApprovalStatus.PENDING.value:
                    raise StateConflictError(
                        error_code="approval_conflict", message="approval is unavailable"
                    )
                return PendingApproval(
                    approval_id=existing.id, version=existing.version, status=existing.status
                )
            sequence = 1
            step = TaskStepModel(
                task_id=task_id,
                sequence=sequence,
                name="approval",
                kind="approval",
                status=StepStatus.RUNNING.value,
                input_summary={},
            )
            session.add(step)
            await session.flush()
            approval = ApprovalRequestModel(
                task_id=task_id,
                step_id=step.id,
                version=1,
                action=proposal.action,
                payload=proposal.payload,
                payload_hash=proposal.payload_hash,
                preview_markdown=preview_markdown,
                status=ApprovalStatus.PENDING.value,
                expires_at=expires_at,
            )
            task.status = TaskStatus.WAITING_APPROVAL.value
            # 中断事务提交时释放执行租约，避免暂停任务被过期租约接管。
            task.lease_owner = None
            task.lease_expires_at = None
            task.graph_thread_id = str(task_id)
            # Checkpoint 由 LangGraph 在本事务之后单独提交；在此之前必须保留 PG anchor，
            # 以便初始 Redis 消息已确认但 Worker 崩溃时仍可由分钟扫描器恢复。
            task.approval_checkpoint_recovery_at = checkpoint_recovery_at
            session.add(approval)
            session.add(
                AuditEventModel(
                    user_id=task.user_id,
                    task_id=task.id,
                    event_type="approval.requested",
                    actor_type="system",
                    actor_id=None,
                    event_metadata={"version": 1, "action": proposal.action},
                )
            )
            await session.flush()
            return PendingApproval(
                approval_id=approval.id, version=approval.version, status=approval.status
            )

    async def confirm_approval_checkpoint(self, *, task_id: UUID, lease_owner: str) -> None:
        """在已保存 ``__interrupt__`` 后清除冻结审批的恢复 anchor。

        这一步允许在初始 Worker 崩溃于 checkpoint 提交之后安全重复：扫描器会先用同一
        PostgreSQL checkpoint 事实确认，再收敛 anchor，绝不依赖 Redis Stream 是否保留。
        """
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    (
                        (TaskRunModel.status == TaskStatus.WAITING_APPROVAL.value)
                        | (
                            (TaskRunModel.status == TaskStatus.RUNNING.value)
                            & (TaskRunModel.lease_owner == lease_owner)
                        )
                    ),
                )
                .with_for_update()
            )
            if task is None:
                return
            durable_interrupt = await session.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM checkpoint_writes AS checkpoint_write
                        INNER JOIN checkpoints AS checkpoint
                            ON checkpoint.thread_id = checkpoint_write.thread_id
                            AND checkpoint.checkpoint_ns = checkpoint_write.checkpoint_ns
                            AND checkpoint.checkpoint_id = checkpoint_write.checkpoint_id
                        WHERE checkpoint_write.thread_id = :thread_id
                            AND checkpoint_write.channel = '__interrupt__'
                    )
                    """
                ),
                {"thread_id": str(task_id)},
            )
            if durable_interrupt is True:
                task.approval_checkpoint_recovery_at = None
                if task.status == TaskStatus.RUNNING.value:
                    task.status = TaskStatus.WAITING_APPROVAL.value
                    task.lease_owner = None
                    task.lease_expires_at = None

    async def find_for_graph(self, *, task_id: UUID, payload_hash: str) -> PendingApproval | None:
        """读取同一任务和哈希的审批快照，不把终态重新解释为待审批。"""
        async with self._session_factory() as session:
            approval = await session.scalar(
                select(ApprovalRequestModel).where(
                    ApprovalRequestModel.task_id == task_id,
                    ApprovalRequestModel.payload_hash == payload_hash,
                )
            )
            if approval is None:
                return None
            return PendingApproval(
                approval_id=approval.id, version=approval.version, status=approval.status
            )

    async def expire_overdue(self, *, now: datetime, limit: int) -> int:
        """维护任务按既有全局有界扫描执行到期，保留统一锁序、审计和状态转换。"""
        return await self._expire_pending(now=now, limit=limit, owned_task=None)

    async def expire_for_task(self, *, user_id: UUID, task_id: UUID, now: datetime) -> int:
        """仅扫描精确用户任务；测试时间推进不能顺带改变其他已经到期的审批。"""
        return await self._expire_pending(now=now, limit=1, owned_task=(user_id, task_id))

    async def _expire_pending(
        self, *, now: datetime, limit: int, owned_task: tuple[UUID, UUID] | None,
    ) -> int:
        """按 TaskRun→ApprovalRequest 固定锁序终止有界数量的过期审批。

        候选查询只对关联 ``TaskRun`` 使用 ``FOR UPDATE ... SKIP LOCKED``，避免先锁
        ApprovalRequest 后等待取消路径已经请求的 TaskRun，形成 Task↔Approval 死锁环。
        取得任务锁后仍逐项锁定并重检精确审批，保证候选扫描与实际状态迁移之间的并发
        决定、撤回或数据损坏不会被当作本轮到期事实。
        """
        expired_actions: list[str] = []
        async with self._session_factory.begin() as session:
            query = (
                select(TaskRunModel, ApprovalRequestModel.id)
                .join(ApprovalRequestModel, ApprovalRequestModel.task_id == TaskRunModel.id)
                .where(
                    ApprovalRequestModel.status == ApprovalStatus.PENDING.value,
                    ApprovalRequestModel.expires_at <= now,
                )
            )
            if owned_task is not None:
                query = query.where(
                    TaskRunModel.user_id == owned_task[0], TaskRunModel.id == owned_task[1],
                )
            candidates = tuple(
                (
                    await session.execute(
                        query
                        .order_by(ApprovalRequestModel.expires_at, ApprovalRequestModel.id)
                        .limit(limit)
                        .with_for_update(of=TaskRunModel, skip_locked=True)
                    )
                )
                .tuples()
                .all()
            )
            expired_count = 0
            for task, approval_id in candidates:
                approval = await session.scalar(
                    select(ApprovalRequestModel)
                    .where(
                        ApprovalRequestModel.id == approval_id,
                        ApprovalRequestModel.task_id == task.id,
                    )
                    .with_for_update()
                )
                if (
                    approval is None
                    or approval.status != ApprovalStatus.PENDING.value
                    or approval.expires_at > now
                ):
                    continue
                approval.status = ApprovalStatus.EXPIRED.value
                is_m2_trusted_action = (
                    approval.schema_version is not None and task.kind == "trusted_action"
                )
                if is_m2_trusted_action:
                    try:
                        await self._return_trusted_action_to_editing(
                            session=session,
                            task=task,
                            approval=approval,
                            reason="approval_expired",
                        )
                    except StateConflictError:
                        # 到期扫描不能因一条损坏绑定回滚其他审批；审批仍终态过期，
                        # 本地对象不存在时保持 fail-closed，后续诊断从审计骨架处理。
                        pass
                if is_m2_trusted_action and task.status == TaskStatus.QUEUED.value:
                    # 提交事务先创建 queued task 与初始 task.execute；若十分钟内 Worker 尚未
                    # 建立 durable interrupt，到期必须同时终止任务并删除该未发布事件。已
                    # 发布 initial 是不可变历史，其他 deduplication key 也不属于本次到期。
                    initial_event = await session.scalar(
                        select(OutboxEventModel)
                        .where(
                            OutboxEventModel.aggregate_id == task.id,
                            OutboxEventModel.topic == "task.execute",
                            OutboxEventModel.deduplication_key == f"task.execute:{task.id}:initial",
                            OutboxEventModel.published_at.is_(None),
                        )
                        .with_for_update()
                    )
                    if initial_event is not None:
                        await session.delete(initial_event)
                    task.status = TaskStatus.CANCELLED.value
                    task.error_code = "approval_expired"
                    task.finished_at = now
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.scheduled_for = None
                    task.retry_recovery_at = None
                    task.approval_checkpoint_recovery_at = None
                    approval_expired_audit = AuditEventModel(
                        user_id=task.user_id,
                        task_id=task.id,
                        event_type="approval.expired",
                        actor_type="system",
                        actor_id=None,
                        event_metadata={},
                    )
                    task_cancelled_audit = AuditEventModel(
                        user_id=task.user_id,
                        task_id=task.id,
                        event_type="task.cancelled",
                        actor_type="system",
                        actor_id=None,
                        event_metadata={"reason": "approval_expired"},
                    )
                    session.add_all(
                        (
                            approval_expired_audit,
                            task_cancelled_audit,
                        )
                    )
                    # 生命周期 Outbox 只绑定数据库分配的审计 ID，不复制审批状态或本地内容。
                    await session.flush()
                    if approval_expired_audit.id is None or task_cancelled_audit.id is None:
                        raise RuntimeError("trusted expiry audit ids were not assigned")
                    session.add_all(
                        (
                            OutboxEventModel(
                                topic="approval.expired",
                                aggregate_id=task.id,
                                deduplication_key=(
                                    f"approval.expired:{approval.id}:{approval.version}"
                                ),
                                payload={
                                    "task_id": str(task.id),
                                    "audit_event_id": approval_expired_audit.id,
                                },
                                available_at=now,
                            ),
                            OutboxEventModel(
                                topic="task.cancelled",
                                aggregate_id=task.id,
                                deduplication_key=(f"task.cancelled:{task.id}:approval-expired"),
                                payload={
                                    "task_id": str(task.id),
                                    "audit_event_id": task_cancelled_audit.id,
                                },
                                available_at=now,
                            ),
                        )
                    )
                if task.status in {
                    TaskStatus.WAITING_APPROVAL.value,
                    TaskStatus.RUNNING.value,
                }:
                    # checkpoint recovery 可能已用短租约把仍为 PENDING 的审批任务接管为
                    # RUNNING。审批到期是更高优先级的不可逆安全事实，必须撤销该租约并终止
                    # 任务，不能等图恢复后再把它误收敛为普通冲突或成功。
                    task.status = TaskStatus.FAILED.value
                    task.error_code = "approval_expired"
                    task.finished_at = now
                    task.lease_owner = None
                    task.lease_expires_at = None
                    task.approval_checkpoint_recovery_at = None
                    session.add(
                        AuditEventModel(
                            user_id=task.user_id,
                            task_id=task.id,
                            event_type="approval.expired",
                            actor_type="system",
                            actor_id=None,
                            event_metadata={},
                        )
                    )
                expired_count += 1
                if approval.action in M2_ACTIONS:
                    expired_actions.append(approval.action)
        # 指标只消费已提交状态；回滚及并发 loser 不得产生过期计数。
        if self._metrics is not None:
            for action in expired_actions:
                self._metrics.record_approval_expired(action=action)
        return expired_count

    async def finish_fake_write(
        self, *, task_id: UUID, lease_owner: str, decision: str, payload_hash: str, now: datetime
    ) -> None:
        """按 TaskRun→ApprovalRequest 锁序把已恢复的假写任务标记为成功。"""
        if not _is_canonical_payload_hash(payload_hash):
            raise StateConflictError(
                error_code="approval_conflict", message="approval is unavailable"
            )
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
                .with_for_update()
            )
            if task is None:
                raise StateConflictError(error_code="task_conflict", message="task is unavailable")
            approval = await session.scalar(
                select(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.task_id == task.id,
                    ApprovalRequestModel.payload_hash == payload_hash,
                    ApprovalRequestModel.status == decision,
                )
                .with_for_update()
            )
            if approval is None:
                raise StateConflictError(error_code="task_conflict", message="task is unavailable")
            frozen = ApprovalProposal.create(approval.action, approval.payload)
            if (
                not _is_canonical_payload_hash(approval.payload_hash)
                or not compare_digest(frozen.payload_hash, approval.payload_hash)
                or not compare_digest(frozen.payload_hash, payload_hash)
            ):
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            task.status = TaskStatus.SUCCEEDED.value
            task.finished_at = now
            task.error_code = None
            task.lease_owner = None
            task.lease_expires_at = None
            session.add(
                AuditEventModel(
                    user_id=task.user_id,
                    task_id=task.id,
                    event_type="task.succeeded",
                    actor_type="worker",
                    actor_id=lease_owner,
                    event_metadata={"reason": "approval_resumed"},
                )
            )

    async def claim_fake_tool_execution(
        self, *, task_id: UUID, lease_owner: str, expected_payload_hash: str
    ) -> FakeToolClaim:
        """在批准、哈希和任务 owner CAS 均成立时原子认领工具执行。"""
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
                .with_for_update()
            )
            approval = await session.scalar(
                select(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.task_id == task_id,
                    ApprovalRequestModel.status == ApprovalStatus.APPROVED.value,
                )
                .order_by(ApprovalRequestModel.version.desc())
                .with_for_update()
            )
            if task is None or approval is None:
                raise StateConflictError(error_code="task_conflict", message="task is unavailable")
            proposal = ApprovalProposal.create(approval.action, approval.payload)
            if not compare_digest(
                proposal.payload_hash, expected_payload_hash
            ) or not compare_digest(proposal.payload_hash, approval.payload_hash):
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            key = f"fake.write:{task_id}:{approval.id}:{approval.version}"
            execution = await session.scalar(
                select(ToolExecutionModel)
                .where(ToolExecutionModel.idempotency_key == key)
                .with_for_update()
            )
            if execution is not None:
                if execution.status == "succeeded":
                    return FakeToolClaim(payload=proposal.payload, should_call=False)
                # 外部副作用已经可能发生，但本地并没有成功事实时不能重放，也不能把
                # ``claimed`` 误当作成功；由 DurableTaskRunner 收敛为安全失败，要求人工
                # 从新 TaskRun 重新发起，而不是对未知结果再次写入。
                raise StateConflictError(
                    error_code="tool_execution_outcome_unknown",
                    message="tool execution outcome is unavailable",
                )
            session.add(
                ToolExecutionModel(
                    task_id=task_id,
                    step_id=approval.step_id,
                    tool_name=approval.action,
                    idempotency_key=key,
                    request_payload_hash=proposal.payload_hash,
                    status="claimed",
                )
            )
            return FakeToolClaim(payload=proposal.payload, should_call=True)

    async def complete_fake_tool_execution(
        self, *, task_id: UUID, lease_owner: str, expected_payload_hash: str
    ) -> None:
        """仅当前租约 owner 将已认领的假工具标记为完成。"""
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
                .with_for_update()
            )
            execution = await session.scalar(
                select(ToolExecutionModel)
                .where(
                    ToolExecutionModel.task_id == task_id,
                    ToolExecutionModel.request_payload_hash == expected_payload_hash,
                )
                .order_by(ToolExecutionModel.id.desc())
                .with_for_update()
            )
            if task is None or execution is None:
                raise StateConflictError(error_code="task_conflict", message="task is unavailable")
            execution.status = "succeeded"

    async def resolve(
        self,
        *,
        approval_id: UUID,
        user_id: UUID,
        decision: str,
        version: int,
        payload_hash: str,
        now: datetime,
    ) -> None:
        """按 TaskRun→ApprovalRequest 锁序验证并解决一项精确审批。

        非锁投影只用于找到审批声称的任务；所有可信判断都在随后锁定 TaskRun、再锁定
        精确 ApprovalRequest 后重做。这样取消、到期与人工决定共享单一锁序，同时不会把
        投影查询结果当成授权或生命周期事实。
        """
        if not _is_canonical_payload_hash(payload_hash):
            raise StateConflictError(
                error_code="approval_conflict", message="approval is unavailable"
            )
        async with self._session_factory.begin() as session:
            task_id = await session.scalar(
                select(ApprovalRequestModel.task_id)
                .join(TaskRunModel, TaskRunModel.id == ApprovalRequestModel.task_id)
                .where(
                    ApprovalRequestModel.id == approval_id,
                    TaskRunModel.user_id == user_id,
                )
            )
            if task_id is None:
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            task = await session.scalar(
                select(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
                .with_for_update()
            )
            if task is None:
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            approval = await session.scalar(
                select(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.id == approval_id,
                    ApprovalRequestModel.task_id == task.id,
                )
                .with_for_update()
            )
            if approval is None or not _is_canonical_payload_hash(approval.payload_hash):
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            if approval.status == ApprovalStatus.INVALIDATED.value:
                raise StateConflictError(
                    error_code="approval_invalidated_by_edit",
                    message="approval was invalidated and requires a new version",
                )
            if (
                approval.status != ApprovalStatus.PENDING.value
                or approval.expires_at <= now
                or approval.version != version
                or not compare_digest(approval.payload_hash, payload_hash)
                or task.status != TaskStatus.WAITING_APPROVAL.value
            ):
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            if approval.schema_version is None:
                proposal = ApprovalProposal.create(approval.action, approval.payload)
                if not compare_digest(approval.payload_hash, proposal.payload_hash):
                    raise StateConflictError(
                        error_code="approval_conflict", message="approval is unavailable"
                    )
            else:
                await self._validate_trusted_approval_current(
                    session=session,
                    task=task,
                    approval=approval,
                )
            durable_interrupt = await session.scalar(
                text(
                    """
                    SELECT EXISTS (
                        SELECT 1
                        FROM checkpoint_writes AS checkpoint_write
                        INNER JOIN checkpoints AS checkpoint
                            ON checkpoint.thread_id = checkpoint_write.thread_id
                            AND checkpoint.checkpoint_ns = checkpoint_write.checkpoint_ns
                            AND checkpoint.checkpoint_id = checkpoint_write.checkpoint_id
                        WHERE checkpoint_write.thread_id = :thread_id
                            AND checkpoint_write.channel = '__interrupt__'
                    )
                    """
                ),
                {"thread_id": str(task.id)},
            )
            if durable_interrupt is not True:
                # ApprovalRequest 先于 LangGraph checkpoint 创建；若用户恰在 interrupt 被保存前
                # 决议，允许该写入会让数据库终态先于暂停 checkpoint，之后的旧图调用会再次
                # 暂停。只接受已由同一 PostgreSQL 提交确认的 interrupt，令 API 与图恢复都以
                # 持久事实排序；客户端可安全重试这个冲突请求。
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            approval.status = decision
            approval.decided_at = now
            approval.decided_by_user_id = user_id
            if approval.schema_version is not None and decision == ApprovalStatus.APPROVED.value:
                approval.approved_execution_deadline_at = now + timedelta(minutes=5)
            if approval.schema_version is not None and decision == ApprovalStatus.REJECTED.value:
                await self._return_trusted_action_to_editing(
                    session=session,
                    task=task,
                    approval=approval,
                    reason="approval_rejected",
                )
            task.status = TaskStatus.QUEUED.value
            task.error_code = None
            session.add(
                AuditEventModel(
                    user_id=user_id,
                    task_id=task.id,
                    event_type="approval.resolved",
                    actor_type="user",
                    actor_id=str(user_id),
                    event_metadata={"decision": decision, "version": version},
                )
            )
            session.add(
                OutboxEventModel(
                    topic="task.execute",
                    aggregate_id=task.id,
                    deduplication_key=f"task.resume:{approval.id}:{approval.version}",
                    payload={"task_id": str(task.id), "resume": decision},
                    available_at=now,
                )
            )

        if self._metrics is not None and approval.action in M2_ACTIONS:
            self._metrics.record_approval_decision(action=approval.action, decision=decision)

    async def _validate_trusted_approval_current(
        self,
        *,
        session: object,
        task: TaskRunModel,
        approval: ApprovalRequestModel,
    ) -> None:
        """在决定锁内重查 M2 本地版本、连接能力和日历目录权限。

        完整命令无需为决定解密；持久 64 字符规范哈希已经绑定密文，决定边界只比较
        调用方提供哈希并核对所有可撤销授权事实。任何能力或版本变化都拒绝决定，避免
        用户批准的载荷在连接失效后被排队执行。
        """
        from sqlalchemy.ext.asyncio import AsyncSession

        typed_session = session if isinstance(session, AsyncSession) else None
        if typed_session is None:
            raise RuntimeError("invalid session")
        if (
            approval.proposal_kind not in {"mail_draft", "calendar_proposal"}
            or approval.proposal_id is None
            or approval.proposal_version is None
        ):
            raise StateConflictError(
                error_code="approval_conflict",
                message="approval is unavailable",
            )
        connection_id: UUID
        calendar_id: str | None = None
        if approval.proposal_kind == "mail_draft":
            draft = await typed_session.scalar(
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
                or draft.status != MailDraftStatus.AWAITING_APPROVAL.value
            ):
                raise StateConflictError(
                    error_code="approval_invalidated_by_edit",
                    message="approval version is no longer current",
                )
            connection_id = draft.connection_id
            read_capability = ConnectionCapability.MAIL_READ
            write_capability = ConnectionCapability.MAIL_SEND
        else:
            proposal = await typed_session.scalar(
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
                or proposal.status != CalendarProposalStatus.AWAITING_APPROVAL.value
            ):
                raise StateConflictError(
                    error_code="approval_invalidated_by_edit",
                    message="approval version is no longer current",
                )
            connection_id = proposal.connection_id
            calendar_id = proposal.calendar_id
            read_capability = ConnectionCapability.CALENDAR_READ
            write_capability = ConnectionCapability.CALENDAR_WRITE

        connection = await typed_session.scalar(
            select(OAuthConnectionModel).where(
                OAuthConnectionModel.id == connection_id,
                OAuthConnectionModel.user_id == task.user_id,
                OAuthConnectionModel.status == "connected",
            )
        )
        capabilities = tuple(
            (
                await typed_session.scalars(
                    select(ConnectionCapabilityModel).where(
                        ConnectionCapabilityModel.user_id == task.user_id,
                        ConnectionCapabilityModel.connection_id == connection_id,
                        ConnectionCapabilityModel.capability.in_(
                            (read_capability.value, write_capability.value)
                        ),
                    )
                )
            ).all()
        )
        capability_status = {row.capability: row.status for row in capabilities}
        if connection is None or capability_status != {
            read_capability.value: CapabilityStatus.ENABLED.value,
            write_capability.value: CapabilityStatus.ENABLED.value,
        }:
            raise StateConflictError(
                error_code="connection_capability_disabled",
                message="provider write capability is not enabled",
            )
        if calendar_id is not None:
            calendar = await typed_session.scalar(
                select(ProviderCalendarModel).where(
                    ProviderCalendarModel.user_id == task.user_id,
                    ProviderCalendarModel.connection_id == connection_id,
                    ProviderCalendarModel.provider_calendar_id == calendar_id,
                    ProviderCalendarModel.can_write.is_(True),
                )
            )
            if calendar is None:
                raise StateConflictError(
                    error_code="connection_capability_disabled",
                    message="calendar is no longer writable",
                )

    async def _return_trusted_action_to_editing(
        self,
        *,
        session: object,
        task: TaskRunModel,
        approval: ApprovalRequestModel,
        reason: str,
    ) -> None:
        """把拒绝或过期审批绑定的当前本地对象恢复为 editing。

        ApprovalRequest 仍保留原 ``proposal_version``，因此后续提交查询会永久判定该版本
        已 consumed；用户必须先保存下一不可变版本，不能直接重放同一密文审批。
        """
        from sqlalchemy.ext.asyncio import AsyncSession

        typed_session = session if isinstance(session, AsyncSession) else None
        if typed_session is None:
            raise RuntimeError("invalid session")
        if approval.proposal_kind == "mail_draft" and approval.proposal_id is not None:
            draft = await typed_session.scalar(
                select(MailDraftModel)
                .where(
                    MailDraftModel.id == approval.proposal_id,
                    MailDraftModel.user_id == task.user_id,
                )
                .with_for_update()
            )
            if (
                draft is not None
                and draft.current_version == approval.proposal_version
                and draft.status == MailDraftStatus.AWAITING_APPROVAL.value
            ):
                draft.status = MailDraftStatus.EDITING.value
            return
        if approval.proposal_kind == "calendar_proposal" and approval.proposal_id is not None:
            proposal = await typed_session.scalar(
                select(CalendarChangeProposalModel)
                .where(
                    CalendarChangeProposalModel.id == approval.proposal_id,
                    CalendarChangeProposalModel.user_id == task.user_id,
                )
                .with_for_update()
            )
            if (
                proposal is not None
                and proposal.current_version == approval.proposal_version
                and proposal.status == CalendarProposalStatus.AWAITING_APPROVAL.value
            ):
                proposal.status = CalendarProposalStatus.EDITING.value
            return
        raise StateConflictError(
            error_code="approval_conflict",
            message=f"trusted approval binding is unavailable: {reason}",
        )


def _is_canonical_payload_hash(value: object) -> bool:
    """判断值是否为规范 SHA-256 lowercase ASCII hex 文本。

    ``hmac.compare_digest`` 对非 ASCII ``str`` 会抛出 ``TypeError``，而单独检查长度也会
    接受大写、非十六进制和 Unicode 字符。审批边界先执行此纯形状校验，随后才允许常量
    时间比较；任何异常持久值或调用方输入都统一 fail closed 为 ``approval_conflict``。
    """
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
