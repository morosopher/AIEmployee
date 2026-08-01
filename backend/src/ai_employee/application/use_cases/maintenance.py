"""定义固定维护任务使用的应用端口与会话过期清理用例。"""

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from typing import Protocol


class SessionMaintenanceRepository(Protocol):
    """定义一个维护事务内删除到期安全会话的最小持久化能力。"""

    async def delete_expired(self, *, now: datetime) -> int:
        """删除 ``expires_at <= now`` 的会话并返回数据库命中数量。"""


class SessionMaintenanceRepositoryFactory(Protocol):
    """为一次维护用例提供自动提交或回滚的 Repository 事务上下文。"""

    def __call__(self) -> AbstractAsyncContextManager[SessionMaintenanceRepository]:
        """创建仅覆盖当前过期清理调用的事务上下文。"""


class ExpireSessionsUseCase:
    """验证 UTC 过期边界并在一个短事务中真实删除到期会话。"""

    def __init__(self, repositories: SessionMaintenanceRepositoryFactory) -> None:
        """注入不暴露 SQLAlchemy 的维护 Repository factory。"""
        self._repositories = repositories

    async def execute(self, *, now: datetime) -> int:
        """删除当前瞬间已经到期的会话摘要。

        Args:
            now: Scheduler 注入的带时区当前时间。

        Returns:
            PostgreSQL 实际删除的会话数量。

        Raises:
            ValueError: ``now`` 不带时区。
        """
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        now = now.astimezone(UTC)
        async with self._repositories() as repository:
            return await repository.delete_expired(now=now)
