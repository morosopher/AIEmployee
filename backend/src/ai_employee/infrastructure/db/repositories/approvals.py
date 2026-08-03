"""以 PostgreSQL 锁实现审批决定与过期终止。"""

from datetime import datetime
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select, text

from ai_employee.application.use_cases.approvals import (
    FakeToolClaim,
    FakeWriteTask,
    PendingApproval,
)
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus, StepStatus, TaskStatus
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyApprovalStore:
    """在短事务内锁定审批并同步写入任务、审计与恢复事件。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级会话工厂而不提前占用数据库连接。"""
        self._session_factory = session_factory

    async def get_fake_write_task(self, *, task_id: UUID) -> FakeWriteTask | None:
        """读取恢复 LangGraph 所需的最小任务快照。

        Worker 只消费这个应用层快照，因此不需要导入 ORM 模型或自行发起 SQL 查询。
        """
        async with self._session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            if task is None:
                return None
            return FakeWriteTask(kind=task.kind, input_payload=task.input_payload)

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
        """有界锁定过期待审批，追加无内容审计并终止关联任务。"""
        async with self._session_factory.begin() as session:
            approvals = list(
                (
                    await session.scalars(
                        select(ApprovalRequestModel)
                        .where(
                            ApprovalRequestModel.status == ApprovalStatus.PENDING.value,
                            ApprovalRequestModel.expires_at <= now,
                        )
                        .order_by(ApprovalRequestModel.expires_at, ApprovalRequestModel.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            expired_count = 0
            for approval in approvals:
                task = await session.get(TaskRunModel, approval.task_id, with_for_update=True)
                if task is None:
                    continue
                approval.status = ApprovalStatus.EXPIRED.value
                if task.status == TaskStatus.WAITING_APPROVAL.value:
                    task.status = TaskStatus.FAILED.value
                    task.error_code = "approval_expired"
                    task.finished_at = now
                    task.lease_owner = None
                    task.lease_expires_at = None
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
            return expired_count

    async def finish_fake_write(
        self, *, task_id: UUID, lease_owner: str, decision: str, payload_hash: str, now: datetime
    ) -> None:
        """仅把已由审批决定恢复到队列的假写任务标记为成功。"""
        async with self._session_factory.begin() as session:
            approval = await session.scalar(
                select(ApprovalRequestModel)
                .where(
                    ApprovalRequestModel.task_id == task_id,
                    ApprovalRequestModel.payload_hash == payload_hash,
                    ApprovalRequestModel.status == decision,
                )
                .with_for_update()
            )
            task = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
                .with_for_update()
            )
            if approval is None or task is None:
                raise StateConflictError(error_code="task_conflict", message="task is unavailable")
            frozen = ApprovalProposal.create(approval.action, approval.payload)
            if not compare_digest(frozen.payload_hash, approval.payload_hash) or not compare_digest(
                frozen.payload_hash, payload_hash
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
        """在一项锁定审批上执行所有权、状态、版本与哈希校验。"""
        async with self._session_factory.begin() as session:
            approval = await session.scalar(
                select(ApprovalRequestModel)
                .where(ApprovalRequestModel.id == approval_id)
                .with_for_update()
            )
            if approval is None:
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            task = await session.scalar(
                select(TaskRunModel)
                .where(TaskRunModel.id == approval.task_id, TaskRunModel.user_id == user_id)
                .with_for_update()
            )
            if task is None:
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
                )
            proposal = ApprovalProposal.create(approval.action, approval.payload)
            if (
                approval.status != ApprovalStatus.PENDING.value
                or approval.expires_at <= now
                or approval.version != version
                or not compare_digest(approval.payload_hash, payload_hash)
                or not compare_digest(approval.payload_hash, proposal.payload_hash)
                or task.status != TaskStatus.WAITING_APPROVAL.value
            ):
                raise StateConflictError(
                    error_code="approval_conflict", message="approval is unavailable"
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
