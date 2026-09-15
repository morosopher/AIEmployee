"""受双测试开关约束的任务驱动和只读旁证；绝不替浏览器批准或制造供应商结果。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, text

from ai_employee.config import Settings
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    TaskRunModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from ai_employee.infrastructure.testing.scenarios import M2FakeActionAdapter
from ai_employee.integrations.registry import build_trusted_action_registry


@dataclass(frozen=True, slots=True)
class M2Evidence:
    """仅返回持久计数与真实开关旁证；不输出载荷、Secret 或场景 ledger 内容。"""

    execution_count: int
    write_calls: int
    reconcile_calls: int
    checkpoint_count: int
    real_writes_enabled: bool


class M2TestSupportService:
    """复用 API 会话工厂和正式 Worker 分类入口，所有方法首先验证精确任务归属。"""

    def __init__(self, sessions: ManagedAsyncSessionMaker, settings: Settings) -> None:
        """只允许双测试开关实例化；外部写环境设置不发生变化。"""
        if settings.app_env != "test" or not settings.app_test_mode:
            raise ValueError("M2 test support requires both test switches")
        self._sessions, self._settings = sessions, settings

    async def run_trusted_task(self, *, user_id: UUID, task_id: UUID) -> bool:
        """从持久审批读取唤醒值，再调用实际 Taskiq 函数；不接受浏览器自报决定。

        Returns:
            是当前用户 trusted_action 时为 True（包括正式入口的幂等 no-op）；普通任务
            返回 False，由现有同会话 DurableTaskRunner 处理，包括恢复准备。
        """
        async with self._sessions() as session:
            task = await session.scalar(select(TaskRunModel).where(
                TaskRunModel.id == task_id, TaskRunModel.user_id == user_id,
            ))
            if task is None or task.kind != "trusted_action":
                return False
            decision = await session.scalar(select(ApprovalRequestModel.status).where(
                ApprovalRequestModel.task_id == task_id,
            ))
        from ai_employee.workers.execute_task import execute_task

        await execute_task(str(task_id), resume=decision if decision in {"approved", "rejected"} else None)
        return True

    async def reconcile(self, *, user_id: UUID, task_id: UUID) -> bool:
        """按正式专用 lease 执行一轮只读核对；时钟推进到持久 due，不篡改状态或租约。"""
        async with self._sessions() as session:
            task = await session.scalar(select(TaskRunModel).where(
                TaskRunModel.id == task_id, TaskRunModel.user_id == user_id,
                TaskRunModel.kind == "trusted_action",
            ))
            if task is None:
                return False
            current = max(datetime.now(UTC), task.scheduled_for or datetime.now(UTC)) + timedelta(seconds=1)
        from ai_employee.workers.reconcile_actions import execute_reconciliation_task

        await execute_reconciliation_task(
            task_id=task_id, session_factory=self._sessions, settings=self._settings,
            adapters=build_trusted_action_registry(session_factory=self._sessions, settings=self._settings),
            now=current,
        )
        return True

    async def expire_approval(self, *, user_id: UUID, task_id: UUID) -> bool:
        """仅缩短本用户待审批的测试时间，再由正式到期扫描执行状态、审计与 Outbox。

        测试入口不能设置 approved、expired 或 task 终态，也不允许替换 payload/hash。
        扫描遵守正式锁序并精确限定用户和任务；其他已到期审批也不会受到影响。
        """
        current = datetime.now(UTC)
        async with self._sessions.begin() as session:
            task = await session.scalar(select(TaskRunModel).where(
                TaskRunModel.id == task_id, TaskRunModel.user_id == user_id,
            ).with_for_update())
            if task is None:
                return False
            approval = await session.scalar(select(ApprovalRequestModel).where(
                ApprovalRequestModel.task_id == task_id, ApprovalRequestModel.status == "pending",
            ).with_for_update())
            if approval is None:
                return False
            approval.expires_at = current - timedelta(seconds=1)
        await SqlAlchemyApprovalStore(self._sessions).expire_for_task(
            user_id=user_id, task_id=task_id, now=current,
        )
        return True

    async def evidence(self, *, user_id: UUID, task_id: UUID) -> M2Evidence | None:
        """合并精确任务的 PostgreSQL 与外部 Fake 计数，禁止根据任务成功反推调用数。"""
        async with self._sessions() as session:
            task = await session.scalar(select(TaskRunModel).where(
                TaskRunModel.id == task_id, TaskRunModel.user_id == user_id,
            ))
            if task is None:
                return None
            executions = tuple((await session.scalars(select(ToolExecutionModel).where(
                ToolExecutionModel.task_id == task_id,
            ))).all())
            checkpoint_exists = await session.scalar(text("SELECT to_regclass('public.checkpoints') IS NOT NULL"))
            checkpoints = int(await session.scalar(text(
                "SELECT count(*) FROM public.checkpoints WHERE thread_id = :task_id"
            ), {"task_id": str(task_id)}) or 0) if checkpoint_exists else 0
        writes, reads = 0, 0
        for execution in executions:
            if execution.operation_id is not None and execution.provider in {"google", "microsoft"}:
                observation = await M2FakeActionAdapter(
                    redis_url=self._settings.redis_url, user_id=user_id, provider=execution.provider,
                ).observations(execution.operation_id)
                writes += observation.write_calls
                reads += observation.reconcile_calls
        return M2Evidence(len(executions), writes, reads, checkpoints, any((
            self._settings.external_writes_enabled, self._settings.google_writes_enabled,
            self._settings.microsoft_writes_enabled,
        )))
