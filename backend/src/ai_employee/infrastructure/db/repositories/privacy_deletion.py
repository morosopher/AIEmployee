"""共享全数据删除的事务准入，固定多 Task 升序→user→完整 authority 的锁顺序。

调用者必须在同一事务内完成受保护的本地删除，不可将此函数返回值当作跨事务许可。
这里没有 provider/密文/租约续期能力，原始 winner 的历史和精确 request 始终由持久行决定。
"""

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_employee.application.use_cases.auth import Clock
from ai_employee.application.use_cases.privacy import PrivacyDeletionBinding
from ai_employee.domain.errors import InternalInvariantError, StateConflictError
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel, ToolExecutionModel
from ai_employee.infrastructure.db.repositories.task_execution import (
    load_deletion_started_authority,
)


def deletion_unavailable() -> StateConflictError:
    """返回稳定无内容拒绝，不泄露另一个任务或删除请求的绑定。"""
    return StateConflictError(
        error_code="privacy_deletion_unavailable",
        message="Privacy deletion lease is unavailable",
    )


async def lock_deletion_binding(
    session: AsyncSession,
    *,
    binding: PrivacyDeletionBinding,
    now: datetime | None = None,
    clock: Clock | None = None,
    task_ids: Sequence[UUID] = (),
    require_winner: bool = True,
) -> tuple[TaskRunModel, UserModel]:
    """排序锁定赢家及目标任务，随后重验用户、活租约与唯一 authority。

    Args:
        session: 调用者拥有的短事务，不在本函数中提交。
        binding: 本次 Worker 原任务/request/owner 精确绑定。
        now: 独立适配器测试可注入的固定 UTC 瞬间。
        clock: 生产调用者的 Clock；必须在 Task/user 锁获得后读取，不能复用等锁前的时间。
        task_ids: 本事务稍后会处理的同用户任务；必须先一起锁，不能锁用户后补目标锁。
        require_winner: 仅初次 CAS 事务可设 False，仍检查原 RUNNING 活租约。

    Returns:
        本事务锁定的赢家 TaskRun 与用户；任何歧义、租约过期或跨用户目标均拒绝。
    """
    expected = {binding.task_id, *task_ids}
    rows = (
        await session.scalars(
            select(TaskRunModel)
            .where(
                TaskRunModel.user_id == binding.user_id,
                TaskRunModel.id.in_(expected),
            )
            .order_by(TaskRunModel.id)
            .with_for_update()
        )
    ).all()
    tasks = {task.id: task for task in rows}
    if set(tasks) != expected:
        raise deletion_unavailable()
    task = tasks[binding.task_id]
    user = await session.scalar(
        select(UserModel)
        .where(
            UserModel.id == binding.user_id,
        )
        .with_for_update()
    )
    if user is None:
        raise InternalInvariantError(
            error_code="privacy_deletion_user_missing",
            message="Privacy deletion user is missing",
        )
    checked_at = clock.now() if clock is not None else now
    if checked_at is None:
        raise ValueError("privacy deletion requires an explicit clock")
    if (
        task.kind != "privacy.delete_all_data"
        or task.status != "running"
        or task.started_at is None
        or task.attempt_count <= 0
        or not binding.request_id
        or not binding.lease_owner
        or task.input_payload != {"deletion_request_id": binding.request_id}
        or task.lease_owner != binding.lease_owner
        or task.lease_expires_at is None
        or task.lease_expires_at <= checked_at
        or await session.scalar(
            select(ToolExecutionModel.id)
            .where(
                ToolExecutionModel.task_id == task.id,
            )
            .limit(1)
        )
        is not None
    ):
        raise deletion_unavailable()
    if require_winner:
        authority = await load_deletion_started_authority(session, task=task)
        if user.is_active or authority is None or authority.request_id != binding.request_id:
            raise deletion_unavailable()
    return task, user
