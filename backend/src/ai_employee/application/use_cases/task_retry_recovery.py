"""定义 Redis 延迟调度事实丢失时的 PostgreSQL 任务恢复用例。"""

from datetime import datetime
from typing import Protocol

from ai_employee.application.use_cases.task_execution import utc_instant


class TaskRetryRecoveryStore(Protocol):
    """定义恢复到期重试任务所需的窄持久化端口。"""

    async def recover_due(self, *, now: datetime, limit: int) -> int:
        """原子归队有界数量的到期重试任务，并创建新的待发布 Outbox 事实。"""


class RecoverScheduledTaskRetriesUseCase:
    """从 PostgreSQL 重新建立因 Redis 丢失而缺失的任务投递事实。"""

    def __init__(self, *, store: TaskRetryRecoveryStore) -> None:
        """注入不泄露 ORM 或队列 SDK 的恢复端口。"""
        self._store = store

    async def execute(self, *, now: datetime, limit: int) -> int:
        """恢复已超过保守期限的任务。

        Args:
            now: 用于到期判断的带时区 UTC 瞬间。
            limit: 单次短事务最多锁定并恢复的任务数。

        Returns:
            本次原子归队的任务数。

        Raises:
            ValueError: 时间不带时区或批量上限不是正数。
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        return await self._store.recover_due(now=utc_instant(now, field="now"), limit=limit)
