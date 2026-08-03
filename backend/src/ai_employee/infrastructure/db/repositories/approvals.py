"""以 PostgreSQL 锁实现审批决定与过期终止。"""

from datetime import datetime
from hmac import compare_digest
from uuid import UUID

from sqlalchemy import select

from ai_employee.application.use_cases.approvals import FakeWriteTask, PendingApproval
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus, StepStatus, TaskStatus
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    TaskStepModel,
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
        proposal: ApprovalProposal,
        preview_markdown: str,
        expires_at: datetime,
    ) -> PendingApproval:
        """幂等冻结提案、创建审批步骤并让任务进入等待状态。"""
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel).where(TaskRunModel.id == task_id).with_for_update()
            )
            if task is None:
                raise StateConflictError(error_code="task_not_found", message="task is unavailable")
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
            if task.status not in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}:
                raise StateConflictError(error_code="task_conflict", message="task is unavailable")
            task.status = TaskStatus.WAITING_APPROVAL.value
            task.graph_thread_id = str(task_id)
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
                if task is None or task.status != TaskStatus.WAITING_APPROVAL.value:
                    continue
                approval.status = ApprovalStatus.EXPIRED.value
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
        self, *, task_id: UUID, decision: str, payload_hash: str, now: datetime
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
                    TaskRunModel.status == TaskStatus.QUEUED.value,
                )
                .with_for_update()
            )
            if approval is None or task is None:
                raise StateConflictError(error_code="task_conflict", message="task is unavailable")
            task.status = TaskStatus.SUCCEEDED.value
            task.finished_at = now
            task.error_code = None
            session.add(
                AuditEventModel(
                    user_id=task.user_id,
                    task_id=task.id,
                    event_type="task.succeeded",
                    actor_type="worker",
                    actor_id=None,
                    event_metadata={"reason": "approval_resumed"},
                )
            )

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
