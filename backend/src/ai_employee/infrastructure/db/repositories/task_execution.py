"""用 SQLAlchemy 实现持久任务租约、完成与安全失败端口。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, update

from ai_employee.application.use_cases.task_execution import LeasedTask, utc_instant
from ai_employee.domain.tasks import JsonValue, TaskStatus
from ai_employee.infrastructure.db.models.tasks import AuditEventModel, TaskRunModel
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class SqlAlchemyTaskExecutionStore:
    """用 PostgreSQL 条件 UPDATE 实现租约、续租与 owner 保护的状态写入。

    每个方法使用独立短事务。租约 acquisition 在一条 ``UPDATE ... RETURNING`` 中同时
    判断状态、过期时间并写 owner，避免先查后写竞态；完成、失败和 RETRY_SCHEDULED
    同样以当前 ``lease_owner`` 做 CAS，丢失租约的 Worker 无法提交陈旧终态。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级 Session factory，不在构造时占用连接。"""
        self._session_factory = session_factory

    async def prepare_retry(self, *, task_id: UUID, now: datetime) -> None:
        """在队列重投入口把 RETRY_SCHEDULED 原子转换为 QUEUED。

        Args:
            task_id: 持久任务标识。
            now: 本次重试准备使用的带时区瞬间。

        只有 RETRY_SCHEDULED 会命中；首次投递的 CREATED/QUEUED、正在运行及终态保持
        原样。归队事务先于下一次租约 acquisition 提交，保证重试状态变化可恢复、可审计。
        """
        now = utc_instant(now, field="now")
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        TaskRunModel.status == TaskStatus.RETRY_SCHEDULED.value,
                    )
                    .values(
                        status=TaskStatus.QUEUED.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        retry_recovery_at=None,
                        updated_at=now,
                    )
                    .returning(TaskRunModel.user_id)
                )
            ).one_or_none()
            if row is not None:
                session.add(
                    AuditEventModel(
                        user_id=row.user_id,
                        task_id=task_id,
                        event_type="task.queued",
                        actor_type="system",
                        actor_id=None,
                        event_metadata={"reason": "taskiq_retry"},
                    )
                )

    async def acquire(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> LeasedTask | None:
        """以一条条件 UPDATE 获取 QUEUED 或租约已过期 RUNNING 任务。

        Args:
            task_id: 待租用任务标识。
            lease_owner: 当前执行尝试的稳定 owner。
            now: acquisition 判断使用的带时区瞬间。
            lease_expires_at: 必须晚于 ``now`` 的租约期限。

        Returns:
            成功获得的基础设施无关任务快照；不满足状态/租约条件时为 ``None``。

        Raises:
            ValueError: 时间不带时区，或租约期限未晚于 ``now``。

        ``started_at = coalesce(started_at, now)`` 只在首次尝试写入；接管过期租约和队列
        重投都沿用同一总预算起点。未命中时调用方必须无副作用退出。
        """
        now = utc_instant(now, field="now")
        lease_expires_at = utc_instant(lease_expires_at, field="lease_expires_at")
        if lease_expires_at <= now:
            raise ValueError("lease_expires_at must be later than now")

        eligible_status = or_(
            TaskRunModel.status == TaskStatus.QUEUED.value,
            and_(
                TaskRunModel.status == TaskStatus.RUNNING.value,
                TaskRunModel.lease_expires_at.is_not(None),
                TaskRunModel.lease_expires_at <= now,
            ),
        )
        lease_available = or_(
            TaskRunModel.lease_expires_at.is_(None),
            TaskRunModel.lease_expires_at <= now,
        )
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        eligible_status,
                        lease_available,
                    )
                    .values(
                        status=TaskStatus.RUNNING.value,
                        lease_owner=lease_owner,
                        lease_expires_at=lease_expires_at,
                        started_at=func.coalesce(TaskRunModel.started_at, now),
                        attempt_count=TaskRunModel.attempt_count + 1,
                        updated_at=now,
                    )
                    .returning(
                        TaskRunModel.id,
                        TaskRunModel.user_id,
                        TaskRunModel.kind,
                        TaskRunModel.input_payload,
                        TaskRunModel.started_at,
                    )
                )
            ).one_or_none()
            if row is None:
                return None
            session.add(
                AuditEventModel(
                    user_id=row.user_id,
                    task_id=row.id,
                    event_type="task.running",
                    actor_type="worker",
                    actor_id=lease_owner,
                    event_metadata={"attempt_started": True},
                )
            )
            return LeasedTask(
                task_id=row.id,
                kind=row.kind,
                input_payload=row.input_payload,
                started_at=row.started_at,
            )

    async def renew(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        lease_expires_at: datetime,
    ) -> bool:
        """仅当前 RUNNING owner 能把租约延长到指定未来时刻。"""
        lease_expires_at = utc_instant(lease_expires_at, field="lease_expires_at")
        async with self._session_factory.begin() as session:
            task_id_result = await session.scalar(
                update(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                )
                .values(lease_expires_at=lease_expires_at)
                .returning(TaskRunModel.id)
            )
        return task_id_result is not None

    async def finish(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        status: TaskStatus,
        finished_at: datetime,
        error_code: str | None,
        retry_recovery_at: datetime | None = None,
    ) -> bool:
        """以 owner CAS 写入批准状态并清理租约。

        Args:
            task_id: 待完成任务标识。
            lease_owner: 必须与持久 owner 精确一致的当前执行者。
            status: RETRY_SCHEDULED 或批准终态。
            finished_at: 状态写入的带时区瞬间。
            error_code: 可选稳定安全错误码。

        Returns:
            CAS 命中并提交时为 ``True``，所有权或状态不符时为 ``False``。

        Raises:
            ValueError: 目标状态不属于执行完成边界，或时间不带时区。

        ``RETRY_SCHEDULED`` 释放租约但不写 ``finished_at``；SUCCEEDED、FAILED 与
        CANCELLED 是真正终态并冻结完成时间。
        """
        allowed = {
            TaskStatus.RETRY_SCHEDULED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        if status not in allowed:
            raise ValueError("finish status is not allowed")
        finished_at = utc_instant(finished_at, field="finished_at")
        if retry_recovery_at is not None:
            retry_recovery_at = utc_instant(retry_recovery_at, field="retry_recovery_at")
            if status is not TaskStatus.RETRY_SCHEDULED:
                raise ValueError("retry_recovery_at is only valid for retry_scheduled")
        terminal = status in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        TaskRunModel.status == TaskStatus.RUNNING.value,
                        TaskRunModel.lease_owner == lease_owner,
                    )
                    .values(
                        status=status.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=finished_at if terminal else None,
                        error_code=error_code,
                        retry_recovery_at=retry_recovery_at,
                        updated_at=finished_at,
                    )
                    .returning(TaskRunModel.user_id)
                )
            ).one_or_none()
            if row is None:
                return False
            event_metadata: dict[str, JsonValue] = {"status": status.value}
            if error_code is not None:
                event_metadata["error_code"] = error_code
            session.add(
                AuditEventModel(
                    user_id=row.user_id,
                    task_id=task_id,
                    event_type=f"task.{status.value}",
                    actor_type="worker",
                    actor_id=lease_owner,
                    event_metadata=event_metadata,
                )
            )
            return True

    async def fail_internal(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        failed_at: datetime,
        error_code: str,
    ) -> bool:
        """以前置状态和 owner CAS 持久化未知执行边界失败。

        ``prepare_retry`` 失败时任务可能仍是 CREATED、QUEUED 或 RETRY_SCHEDULED，且
        不应带执行 owner；``acquire`` 失败也可能是事务已经提交但响应丢失，因此允许
        当前 ``lease_owner`` 对 RUNNING 行收敛失败。另一个 owner、WAITING_APPROVAL 与
        所有终态均不命中，避免旧消息覆盖有效工作或人工审批状态。
        """
        failed_at = utc_instant(failed_at, field="failed_at")
        safe_unowned_status = and_(
            TaskRunModel.status.in_(
                (
                    TaskStatus.CREATED.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.RETRY_SCHEDULED.value,
                )
            ),
            TaskRunModel.lease_owner.is_(None),
        )
        same_owner_running = and_(
            TaskRunModel.status == TaskStatus.RUNNING.value,
            TaskRunModel.lease_owner == lease_owner,
        )
        async with self._session_factory.begin() as session:
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        or_(safe_unowned_status, same_owner_running),
                    )
                    .values(
                        status=TaskStatus.FAILED.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        finished_at=failed_at,
                        error_code=error_code,
                        updated_at=failed_at,
                    )
                    .returning(TaskRunModel.user_id)
                )
            ).one_or_none()
            if row is None:
                return False
            session.add(
                AuditEventModel(
                    user_id=row.user_id,
                    task_id=task_id,
                    event_type="task.failed",
                    actor_type="worker",
                    actor_id=lease_owner,
                    event_metadata={
                        "status": TaskStatus.FAILED.value,
                        "error_code": error_code,
                    },
                )
            )
            return True
