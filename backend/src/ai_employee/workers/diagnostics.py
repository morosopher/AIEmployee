"""执行每日简报逾期诊断任务，不复制 Gmail 或 Calendar 的来源内容。"""

from datetime import UTC, datetime

from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.infrastructure.db.repositories.diagnostics import SqlAlchemyDiagnosticSnapshotStore
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker


class OverdueBriefDiagnosticTaskStep:
    """保存用户可见的最小诊断信息，供任务时间线与逾期告警链接使用。"""

    name = "brief_overdue_diagnostic"

    def __init__(self, session_factory: ManagedAsyncSessionMaker) -> None:
        """注入短事务快照存储，Worker 不直接操作 ORM。"""
        self._store = SqlAlchemyDiagnosticSnapshotStore(session_factory)

    async def execute(self, task: LeasedTask) -> None:
        """采集当前用户的来源新鲜度、失败任务 UUID 和错误码。

        Args:
            task: 已持有 PostgreSQL 租约的诊断任务；必须带用户归属。

        Raises:
            ValueError: 缺失用户归属时拒绝执行，避免生成未隔离诊断结果。
        """
        if task.user_id is None:
            raise ValueError("brief overdue diagnostic requires user_id")
        await self._store.save_snapshot(
            task_id=task.task_id,
            user_id=task.user_id,
            now=datetime.now(UTC),
        )


def build_overdue_brief_diagnostic_task_step(
    *, session_factory: ManagedAsyncSessionMaker
) -> OverdueBriefDiagnosticTaskStep:
    """构造可由 DurableTaskRunner 重放的逾期诊断节点。"""
    return OverdueBriefDiagnosticTaskStep(session_factory)
