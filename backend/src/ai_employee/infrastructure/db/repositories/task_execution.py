"""用 SQLAlchemy 实现持久任务租约、完成与安全失败端口。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.privacy import (
    PRIVACY_DELETION_STARTED_EVENT_TYPE,
    PrivacyDeletionStartedAuthority,
    PrivacyDeletionStartedFact,
    parse_privacy_deletion_started_authority,
)
from ai_employee.application.use_cases.task_execution import LeasedTask, TaskLeaseMode, utc_instant
from ai_employee.domain.tasks import JsonValue, TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


async def load_deletion_started_authority(
    session: AsyncSession,
    *,
    task: TaskRunModel,
) -> PrivacyDeletionStartedAuthority | None:
    """在 TaskRun→user 锁后投影完整 started 集合，仅识别预屏障精确删除赢家。

    Args:
        session: 已锁定任务及所属用户的当前短事务；本方法不提交、不加反向行锁。
        task: 当前事务中的 RUNNING 任务。owner/租约时间由具体 acquire/renew/finalize 校验。

    Returns:
        原任务输入、运行历史、无 ToolExecution 与唯一 started 事实全部匹配的授权。
        查询只按 user/event 过滤，绝不以 expected task/request 隐藏冲突事实。
    """
    request_id = task.input_payload.get("deletion_request_id")
    if (
        task.kind != "privacy.delete_all_data"
        or task.status != TaskStatus.RUNNING.value
        or task.started_at is None
        or task.attempt_count <= 0
        or set(task.input_payload) != {"deletion_request_id"}
        or not isinstance(request_id, str)
        or not request_id
        or await session.scalar(
            select(ToolExecutionModel.id).where(ToolExecutionModel.task_id == task.id).limit(1)
        )
        is not None
    ):
        return None
    rows = (
        await session.scalars(
            select(AuditEventModel).where(
                AuditEventModel.user_id == task.user_id,
                AuditEventModel.event_type == PRIVACY_DELETION_STARTED_EVENT_TYPE,
            )
        )
    ).all()
    return parse_privacy_deletion_started_authority(
        [
            PrivacyDeletionStartedFact(
                event_type=row.event_type,
                user_id=row.user_id,
                task_id=row.task_id,
                event_metadata=row.event_metadata,
            )
            for row in rows
        ],
        expected_user_id=task.user_id,
        expected_task_id=task.id,
        expected_request_id=request_id,
    )


async def _protect_inactive_deletion_task(session: AsyncSession, *, task_id: UUID) -> bool:
    """在通用终态/重试写之前锁住 TaskRun→user，只保护精确 inactive 删除赢家。

    普通任务及 CAS 失败者仍遵守既有通用 owner/time CAS；不能把 inactive 状态本身
    扩张为新的任务终态保护规则。返回 True 时调用方必须保持所有任务列和审计不变。
    """
    task = await session.scalar(
        select(TaskRunModel).where(TaskRunModel.id == task_id).with_for_update()
    )
    if task is None:
        return False
    user = await session.scalar(
        select(UserModel).where(UserModel.id == task.user_id).with_for_update()
    )
    return (
        user is not None
        and not user.is_active
        and await load_deletion_started_authority(session, task=task) is not None
    )


class SqlAlchemyTaskExecutionStore:
    """用 TaskRun→user 锁序及条件 UPDATE 实现删除屏障和 owner 保护的状态写入。

    每个方法使用独立短事务，锁后重检 active 或唯一 deletion authority；UPDATE 仍
    保留状态、owner、时间谓词。精确 inactive 删除赢家只能续租/接管，不能被通用
    完成或错误路径移出 RUNNING，使最后的 privacy 原子事务始终拥有可恢复身份。
    """

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """保存进程级 Session factory，不在构造时占用连接。"""
        self._session_factory = session_factory

    async def prepare_retry(self, *, task_id: UUID, now: datetime) -> None:
        """在队列重投入口把 RETRY_SCHEDULED 原子转换为 QUEUED。

        Args:
            task_id: 持久任务标识。
            now: 本次重试准备使用的带时区瞬间。

        只有没有未发布 ``task.execute`` Outbox 的 RETRY_SCHEDULED 会命中；否则到达的
        只能是旧 Redis Stream 重复消息，下一轮耐久延迟重试仍未完成 relay 交接，不能被
        该旧消息提前执行。首次投递的 CREATED/QUEUED、正在运行及终态保持原样。归队事务
        先于下一次租约 acquisition 提交，保证重试状态变化可恢复、可审计。
        """
        now = utc_instant(now, field="now")
        async with self._session_factory.begin() as session:
            if await _protect_inactive_deletion_task(session, task_id=task_id):
                return
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        TaskRunModel.status == TaskStatus.RETRY_SCHEDULED.value,
                        ~exists(
                            select(OutboxEventModel.id).where(
                                OutboxEventModel.aggregate_id == TaskRunModel.id,
                                OutboxEventModel.topic == "task.execute",
                                OutboxEventModel.published_at.is_(None),
                            )
                        ),
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
        recover_waiting_approval: bool = False,
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
        if recover_waiting_approval:
            eligible_status = or_(
                eligible_status,
                and_(
                    TaskRunModel.status == TaskStatus.WAITING_APPROVAL.value,
                    TaskRunModel.approval_checkpoint_recovery_at.is_not(None),
                ),
            )
        lease_available = or_(
            TaskRunModel.lease_expires_at.is_(None),
            TaskRunModel.lease_expires_at <= now,
        )
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(TaskRunModel.id == task_id, eligible_status, lease_available)
                .with_for_update()
            )
            if task is None:
                return None
            user = await session.scalar(
                select(UserModel).where(UserModel.id == task.user_id).with_for_update()
            )
            if user is None:
                return None
            lease_mode = TaskLeaseMode.NORMAL
            if not user.is_active:
                # QUEUED 等状态即使伪造 exact metadata 也不能产生恢复例外；原任务必须
                # 已运行且仍带过期 lease。完整 per-user parser 是最终权威。
                if (
                    recover_waiting_approval
                    or task.lease_expires_at is None
                    or task.lease_expires_at > now
                    or await load_deletion_started_authority(session, task=task) is None
                ):
                    return None
                lease_mode = TaskLeaseMode.INACTIVE_ALL_DATA_RECOVERY
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
                        TaskRunModel.created_at,
                        TaskRunModel.started_at,
                        TaskRunModel.attempt_count,
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
                created_at=row.created_at,
                started_at=row.started_at,
                user_id=row.user_id,
                attempt_count=row.attempt_count,
                lease_owner=lease_owner,
                lease_mode=lease_mode,
            )

    async def renew(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        renewed_at: datetime,
        lease_expires_at: datetime,
    ) -> bool:
        """仅当前且未过期 RUNNING owner 能把租约延长到指定未来时刻。

        Args:
            task_id: 待续租任务标识。
            lease_owner: 必须与持久 owner 精确一致的执行者。
            renewed_at: 本次续租边界唯一采样的 UTC 瞬间。
            lease_expires_at: 请求写入的新租约截止时间。

        Returns:
            条件 UPDATE 命中并提交时为 ``True``；租约缺失、过期、被接管或请求截止
            时间不晚于 ``renewed_at`` 时为 ``False``。CAS 未命中绝不回退覆盖。

        Raises:
            ValueError: 任一时间不带时区，或请求截止时间不严格晚于 ``renewed_at``。
        """
        renewed_at = utc_instant(renewed_at, field="renewed_at")
        lease_expires_at = utc_instant(lease_expires_at, field="lease_expires_at")
        if lease_expires_at <= renewed_at:
            raise ValueError("lease_expires_at must be later than renewed_at")
        async with self._session_factory.begin() as session:
            task = await session.scalar(
                select(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                    TaskRunModel.lease_expires_at.is_not(None),
                    TaskRunModel.lease_expires_at > renewed_at,
                )
                .with_for_update()
            )
            if task is None:
                return False
            user = await session.scalar(
                select(UserModel).where(UserModel.id == task.user_id).with_for_update()
            )
            if user is None or (
                not user.is_active
                and await load_deletion_started_authority(session, task=task) is None
            ):
                return False
            task_id_result = await session.scalar(
                update(TaskRunModel)
                .where(
                    TaskRunModel.id == task_id,
                    TaskRunModel.status == TaskStatus.RUNNING.value,
                    TaskRunModel.lease_owner == lease_owner,
                    TaskRunModel.lease_expires_at.is_not(None),
                    TaskRunModel.lease_expires_at > renewed_at,
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
            if await _protect_inactive_deletion_task(session, task_id=task_id):
                return False
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        TaskRunModel.status == TaskStatus.RUNNING.value,
                        TaskRunModel.lease_owner == lease_owner,
                        TaskRunModel.lease_expires_at.is_not(None),
                        TaskRunModel.lease_expires_at > finished_at,
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

    async def schedule_retry(
        self,
        *,
        task_id: UUID,
        lease_owner: str,
        scheduled_at: datetime,
        retry_available_at: datetime,
        error_code: str,
        attempt_count: int,
    ) -> bool:
        """在一个 owner-CAS 事务中保存延迟重试的全部业务事实。

        ``retry_recovery_at`` 在此刻刻意保持为空：它只能在 Outbox relay 已成功把消息
        交给 Redis 后写入，防止慢速但正常的交接被恢复扫描器误判为丢失。
        """
        scheduled_at = utc_instant(scheduled_at, field="scheduled_at")
        retry_available_at = utc_instant(retry_available_at, field="retry_available_at")
        if retry_available_at <= scheduled_at:
            raise ValueError("retry_available_at must be later than scheduled_at")
        if attempt_count <= 0:
            raise ValueError("attempt_count must be positive")
        async with self._session_factory.begin() as session:
            if await _protect_inactive_deletion_task(session, task_id=task_id):
                return False
            row = (
                await session.execute(
                    update(TaskRunModel)
                    .where(
                        TaskRunModel.id == task_id,
                        TaskRunModel.status == TaskStatus.RUNNING.value,
                        TaskRunModel.lease_owner == lease_owner,
                        TaskRunModel.lease_expires_at.is_not(None),
                        TaskRunModel.lease_expires_at > scheduled_at,
                    )
                    .values(
                        status=TaskStatus.RETRY_SCHEDULED.value,
                        lease_owner=None,
                        lease_expires_at=None,
                        error_code=error_code,
                        retry_recovery_at=None,
                        updated_at=scheduled_at,
                    )
                    .returning(TaskRunModel.user_id)
                )
            ).one_or_none()
            if row is None:
                return False
            session.add_all(
                (
                    AuditEventModel(
                        user_id=row.user_id,
                        task_id=task_id,
                        event_type="task.retry_scheduled",
                        actor_type="worker",
                        actor_id=lease_owner,
                        event_metadata={
                            "status": TaskStatus.RETRY_SCHEDULED.value,
                            "error_code": error_code,
                        },
                    ),
                    OutboxEventModel(
                        topic="task.execute",
                        aggregate_id=task_id,
                        deduplication_key=f"task.execute:{task_id}:retry:{attempt_count}",
                        payload={"task_id": str(task_id)},
                        available_at=retry_available_at,
                    ),
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
            TaskRunModel.lease_expires_at.is_not(None),
            TaskRunModel.lease_expires_at > failed_at,
        )
        async with self._session_factory.begin() as session:
            if await _protect_inactive_deletion_task(session, task_id=task_id):
                return False
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
