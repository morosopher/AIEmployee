"""以 PostgreSQL 实现任务 API 所需的用户隔离读写视图。"""

from datetime import datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from ai_employee.application.use_cases.task_views import TaskSnapshot, TaskStepSnapshot
from ai_employee.domain.actions import CalendarProposalStatus, MailDraftStatus
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus, transition_task
from ai_employee.infrastructure.db.models.actions import (
    CalendarChangeProposalModel,
    MailDraftModel,
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


class SqlAlchemyTaskViewStore:
    """把任务状态变更、审计与 Outbox 保持在单一短事务内。"""

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存 API 进程共享的 Session factory。"""
        self._session_factory = session_factory

    @staticmethod
    def _snapshot(
        task: TaskRunModel, steps: tuple[TaskStepModel, ...], event_cursor: int
    ) -> TaskSnapshot:
        """显式白名单映射 ORM，阻止 API 暴露租约或内部图字段。"""
        return TaskSnapshot(
            id=task.id,
            kind=task.kind,
            status=TaskStatus(task.status),
            retry_of_task_id=task.retry_of_task_id,
            input_payload=task.input_payload,
            error_code=task.error_code,
            event_cursor=event_cursor,
            steps=tuple(
                TaskStepSnapshot(
                    id=step.id,
                    sequence=step.sequence,
                    name=step.name,
                    status=step.status,
                    output_summary=step.output_summary,
                    error_code=step.error_code,
                    started_at=step.started_at,
                    finished_at=step.finished_at,
                )
                for step in steps
            ),
        )

    async def _get_in_session(
        self, session: object, *, task_id: UUID, user_id: UUID
    ) -> TaskSnapshot | None:
        """在调用方事务读取任务、步骤与最大审计游标，避免跨事务 ORM 泄漏。"""
        from sqlalchemy.ext.asyncio import AsyncSession

        typed_session = session if isinstance(session, AsyncSession) else None
        if typed_session is None:
            raise RuntimeError("invalid session")
        task = await typed_session.scalar(
            select(TaskRunModel).where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
        )
        if task is None:
            return None
        steps = tuple(
            (
                await typed_session.scalars(
                    select(TaskStepModel)
                    .where(TaskStepModel.task_id == task_id)
                    .order_by(TaskStepModel.sequence)
                )
            ).all()
        )
        event_cursor = await typed_session.scalar(
            select(func.max(AuditEventModel.id)).where(
                AuditEventModel.task_id == task_id, AuditEventModel.user_id == user_id
            )
        )
        return self._snapshot(task, steps, event_cursor or 0)

    async def get(self, *, task_id: UUID, user_id: UUID) -> TaskSnapshot | None:
        """在同一个可重复读快照内读取用户拥有的任务与稳定排序时间线。

        PostgreSQL 的 ``READ COMMITTED`` 会为每条 SELECT 建立新快照。任务、步骤和最大
        审计 ID 分三次查询时，并发提交可能让旧状态与新游标混合，客户端随后以新游标重连会
        永久漏掉状态事件。因此本只读事务显式固定为 ``REPEATABLE READ``；写入路径仍保持
        默认隔离级别及其行锁语义。

        Args:
            task_id: 需要读取的任务标识。
            user_id: 当前认证用户，用于隔离任务可见性。

        Returns:
            同一 PostgreSQL 快照内的任务投影；任务不属于该用户或不存在时返回 ``None``。
        """
        async with self._session_factory.begin() as session:
            # 必须作为事务中第一条 SQL 执行，才会固定随后全部 SELECT 的 MVCC 视图。
            await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
            return await self._get_in_session(session, task_id=task_id, user_id=user_id)

    async def cancel(self, *, task_id: UUID, user_id: UUID, now: datetime) -> TaskSnapshot | None:
        """锁定任务、原子追加取消事实，并在提交后返回固定读取视图。

        取消状态、审计事件必须同事务提交；响应快照则在提交后另开 ``REPEATABLE READ``
        事务读取，避免默认 ``READ COMMITTED`` 的多条查询让事件游标超前于任务或步骤。
        Worker 的最终业务事务会在仍为 ``running`` 时写入无敏感内容的 ``result_payload``
        marker；取消拿到同一行锁后若看到 marker，说明外部可见结果已经提交，只能让 Runner
        继续以 owner CAS 收敛为成功，不能再把已完成事实改写为取消。
        """
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
                .with_for_update()
            )
            if task is None:
                return None
            current_status = TaskStatus(task.status)
            if task.kind == "trusted_action" and current_status is TaskStatus.WAITING_APPROVAL:
                await self._withdraw_trusted_action(
                    session=session,
                    task=task,
                    user_id=user_id,
                    now=now,
                )
                await session.flush()
                withdrawn = True
            else:
                withdrawn = False
            if not withdrawn and current_status is TaskStatus.RUNNING and task.result_payload is not None:
                raise StateConflictError(
                    error_code="task_state_conflict",
                    message="task result is already committed",
                )
            elif not withdrawn:
                transition_task(current_status, TaskStatus.CANCELLED)
                task.status = TaskStatus.CANCELLED.value
                task.finished_at = now
                task.lease_owner = None
                task.lease_expires_at = None
                session.add(
                    AuditEventModel(
                        user_id=user_id,
                        task_id=task_id,
                        event_type="task.cancelled",
                        actor_type="user",
                        actor_id=str(user_id),
                        event_metadata={},
                    )
                )
            await session.flush()
        return await self.get(task_id=task_id, user_id=user_id)

    async def _withdraw_trusted_action(
        self,
        *,
        session: object,
        task: TaskRunModel,
        user_id: UUID,
        now: datetime,
    ) -> None:
        """撤回尚未认领的可信审批，并让绑定本地对象回到编辑态。

        锁序由调用方已取得 TaskRun 开始，随后固定为 ApprovalRequest、ToolExecution 检查、
        绑定草稿/提案。任何 ToolExecution 行都证明执行边界已经被认领，此时取消可能掩盖
        已发生或未知的外部副作用，必须拒绝而不能只检查其当前状态。

        Args:
            session: 当前短事务的异步 SQLAlchemy 会话。
            task: 已按用户范围 ``FOR UPDATE`` 锁定的可信任务。
            user_id: 当前认证用户。
            now: 取消事实的带时区 UTC 时间。

        Raises:
            StateConflictError: 审批形状异常、已终止或已有工具认领。
        """
        from sqlalchemy.ext.asyncio import AsyncSession

        typed_session = session if isinstance(session, AsyncSession) else None
        if typed_session is None:
            raise RuntimeError("invalid session")
        approval = await typed_session.scalar(
            select(ApprovalRequestModel)
            .where(ApprovalRequestModel.task_id == task.id)
            .with_for_update()
        )
        if (
            approval is None
            or approval.schema_version is None
            or approval.status != ApprovalStatus.PENDING.value
            or approval.proposal_kind not in {"mail_draft", "calendar_proposal"}
            or approval.proposal_id is None
            or approval.proposal_version is None
        ):
            raise StateConflictError(
                error_code="task_state_conflict",
                message="trusted action cannot be withdrawn",
            )
        execution = await typed_session.scalar(
            select(ToolExecutionModel.id)
            .where(ToolExecutionModel.task_id == task.id)
            .with_for_update()
        )
        if execution is not None:
            raise StateConflictError(
                error_code="task_state_conflict",
                message="trusted action execution is already claimed",
            )

        if approval.proposal_kind == "mail_draft":
            local = await typed_session.get(
                MailDraftModel,
                approval.proposal_id,
                with_for_update=True,
            )
            if (
                local is None
                or local.user_id != user_id
                or local.current_version != approval.proposal_version
                or local.status != MailDraftStatus.AWAITING_APPROVAL.value
            ):
                raise StateConflictError(
                    error_code="task_state_conflict",
                    message="trusted action binding is unavailable",
                )
            local.status = MailDraftStatus.EDITING.value
        else:
            proposal = await typed_session.scalar(
                select(CalendarChangeProposalModel)
                .where(CalendarChangeProposalModel.id == approval.proposal_id)
                .with_for_update()
            )
            if (
                proposal is None
                or proposal.user_id != user_id
                or proposal.current_version != approval.proposal_version
                or proposal.status != CalendarProposalStatus.AWAITING_APPROVAL.value
            ):
                raise StateConflictError(
                    error_code="task_state_conflict",
                    message="trusted action binding is unavailable",
                )
            proposal.status = CalendarProposalStatus.EDITING.value

        approval.status = ApprovalStatus.INVALIDATED.value
        task.status = TaskStatus.CANCELLED.value
        task.error_code = None
        task.finished_at = now
        task.lease_owner = None
        task.lease_expires_at = None
        task.scheduled_for = None
        task.retry_recovery_at = None
        task.approval_checkpoint_recovery_at = None
        typed_session.add_all(
            (
                AuditEventModel(
                    user_id=user_id,
                    task_id=task.id,
                    event_type="approval.invalidated",
                    actor_type="user",
                    actor_id=str(user_id),
                    event_metadata={
                        "approval_id": str(approval.id),
                        "reason": "withdrawn",
                        "status": ApprovalStatus.INVALIDATED.value,
                    },
                ),
                AuditEventModel(
                    user_id=user_id,
                    task_id=task.id,
                    event_type="task.cancelled",
                    actor_type="user",
                    actor_id=str(user_id),
                    event_metadata={"reason": "approval_withdrawn"},
                ),
            )
        )

    async def retry(
        self, *, task_id: UUID, user_id: UUID, idempotency_key: str, now: datetime
    ) -> TaskSnapshot | None:
        """通过原失败任务范围的幂等键复制输入并创建新的 Outbox 事实。

        Args:
            task_id: 必须处于失败终态的原始任务标识。
            user_id: 当前认证用户，所有查询与新事实均绑定该用户。
            idempotency_key: 调用方提供的重试请求键。
            now: 用于新 Outbox 可投递时刻的显式 UTC 瞬间。

        Returns:
            新建或同一原任务既有的 replacement；原任务不存在时返回 ``None``。

        Raises:
            StateConflictError: 原任务不是失败状态，不能被重试。

        同一用户的一般创建幂等键仍由 ``task_runs`` 原约束管理。重试将公开请求键编码为
        包含原任务 UUID 的固定长度内部键，使同一键可安全重试两个不同失败任务，同时不会
        让第二个任务错误复用第一个任务的 replacement。
        """
        retry_key = self._retry_idempotency_key(task_id=task_id, idempotency_key=idempotency_key)
        async with self._session_factory.begin() as session:
            original = await session.scalar(
                select(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.user_id == user_id)
                .with_for_update()
            )
            if original is None:
                return None
            if original.status != TaskStatus.FAILED.value:
                raise StateConflictError(
                    error_code="task_state_conflict", message="task is not failed"
                )
            existing = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.user_id == user_id,
                    TaskRunModel.retry_of_task_id == task_id,
                    TaskRunModel.idempotency_key == retry_key,
                )
            )
            if existing is not None:
                replacement_task_id = existing.id
            else:
                replacement_id = uuid4()
                inserted = await session.scalar(
                    insert(TaskRunModel)
                    .values(
                        id=replacement_id,
                        user_id=user_id,
                        retry_of_task_id=task_id,
                        kind=original.kind,
                        status=TaskStatus.CREATED.value,
                        idempotency_key=retry_key,
                        input_payload=original.input_payload,
                    )
                    .on_conflict_do_nothing(constraint="uq_task_runs_user_id_idempotency_key")
                    .returning(TaskRunModel.id)
                )
                if inserted is None:
                    existing = await session.scalar(
                        select(TaskRunModel).where(
                            TaskRunModel.user_id == user_id,
                            TaskRunModel.retry_of_task_id == task_id,
                            TaskRunModel.idempotency_key == retry_key,
                        )
                    )
                    if existing is None:
                        raise RuntimeError("retry idempotency winner is not visible")
                    replacement_task_id = existing.id
                else:
                    transition_task(TaskStatus.CREATED, TaskStatus.QUEUED)
                    await session.execute(
                        update(TaskRunModel)
                        .where(TaskRunModel.id == replacement_id)
                        .values(status=TaskStatus.QUEUED.value)
                    )
                    session.add_all(
                        (
                            AuditEventModel(
                                user_id=user_id,
                                task_id=replacement_id,
                                event_type="task.created",
                                actor_type="user",
                                actor_id=str(user_id),
                                event_metadata={
                                    "retry_of_task_id": str(task_id),
                                    "status": TaskStatus.CREATED.value,
                                },
                            ),
                            AuditEventModel(
                                user_id=user_id,
                                task_id=replacement_id,
                                event_type="task.queued",
                                actor_type="user",
                                actor_id=str(user_id),
                                event_metadata={"status": TaskStatus.QUEUED.value},
                            ),
                            OutboxEventModel(
                                topic="task.execute",
                                aggregate_id=replacement_id,
                                deduplication_key=f"task.execute:{replacement_id}:initial",
                                payload={"task_id": str(replacement_id)},
                                available_at=now,
                            ),
                        )
                    )
                    await session.flush()
                    replacement_task_id = replacement_id
        # 写事务已经提交；独立可重复读事务为 REST 与 SSE 建立同一事实基线。
        return await self.get(task_id=replacement_task_id, user_id=user_id)

    @staticmethod
    def _retry_idempotency_key(*, task_id: UUID, idempotency_key: str) -> str:
        """生成受原失败任务约束且不超出数据库列长度的内部幂等键。

        Args:
            task_id: 请求明确指定的失败任务。
            idempotency_key: 外部调用方提供、可能接近列长度上限的键。

        Returns:
            固定长度的英文内部键；仅用于数据库唯一约束，不向 API 返回。
        """
        digest = sha256(idempotency_key.encode("utf-8")).hexdigest()
        return f"retry:{task_id}:{digest}"


class PostgresQueuedTaskDispatcher:
    """在 Outbox relay 接管前仅持久化初始 ``QUEUED`` 状态的 API 适配器。

    API 进程不直接访问 Redis；未发布 Outbox 仍是唯一可恢复的投递事实，独立 relay 会负责
    把标识交给队列。该轻量状态推进让 202 响应准确反映任务已可被 relay 消费。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存共享 Session factory。"""
        self._session_factory = session_factory

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """原子把新建任务归队，并追加可重放的状态转换审计事实。

        ``task.queued`` 与状态更新必须使用同一个事务提交，防止 API 的 ``QUEUED`` 快照
        已可见但 SSE 无法重放对应转换。重复幂等创建再次经过此方法时条件更新不命中，因而
        不会伪造第二条状态转换事件。
        """
        async with self._session_factory.begin() as session:
            transitioned_user_id = await session.scalar(
                update(TaskRunModel)
                .where(TaskRunModel.id == task_id, TaskRunModel.status == TaskStatus.CREATED.value)
                .values(status=TaskStatus.QUEUED.value)
                .returning(TaskRunModel.user_id)
            )
            if transitioned_user_id is not None:
                session.add(
                    AuditEventModel(
                        user_id=transitioned_user_id,
                        task_id=task_id,
                        event_type="task.queued",
                        actor_type="system",
                        actor_id=None,
                        event_metadata={"status": TaskStatus.QUEUED.value},
                    )
                )
            status = await session.scalar(
                select(TaskRunModel.status).where(TaskRunModel.id == task_id)
            )
        if status is None:
            raise RuntimeError("created task disappeared")
        return TaskStatus(status)
