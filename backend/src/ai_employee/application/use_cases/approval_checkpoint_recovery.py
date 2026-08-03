"""定义审批 interrupt checkpoint 丢失时的 PostgreSQL 恢复用例。"""

from datetime import datetime
from typing import Protocol

from ai_employee.application.use_cases.task_execution import utc_instant


class ApprovalCheckpointRecoveryStore(Protocol):
    """定义恢复冻结审批与首个 LangGraph checkpoint 交接所需端口。"""

    async def recover_due(self, *, now: datetime, limit: int) -> int:
        """补投尚未拥有持久 interrupt checkpoint 的有界审批任务。"""


class RecoverApprovalCheckpointsUseCase:
    """从 PostgreSQL 重新建立审批首个 interrupt 的队列交接。"""

    def __init__(self, *, store: ApprovalCheckpointRecoveryStore) -> None:
        """注入不暴露 ORM、Redis 或 LangGraph SDK 的恢复端口。"""
        self._store = store

    async def execute(self, *, now: datetime, limit: int) -> int:
        """扫描冻结后尚未保存 interrupt checkpoint 的审批。

        Args:
            now: 带时区 UTC 扫描瞬间。
            limit: 单次短事务最多处理的任务数。

        Returns:
            本轮写入新的 checkpoint 恢复 Outbox 的任务数量。

        Raises:
            ValueError: 批量上限不是正数或时间不带时区。
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        return await self._store.recover_due(now=utc_instant(now, field="now"), limit=limit)
