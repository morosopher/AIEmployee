"""验证手动 Google 同步的双任务事实在 PostgreSQL 中原子提交。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import time
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ai_employee.application.use_cases.tasks import (
    CreateTaskBatchItem,
    CreateTaskUseCase,
)
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepository
from ai_employee.infrastructure.db.session import build_session_factory


class FailingSecondTaskRepository(SqlAlchemyTaskRepository):
    """在已写入第二项后模拟持久化失败，验证外层事务会整体回滚。"""

    def __init__(self, *args: object) -> None:
        """初始化父存储并记录当前批次写入次数。"""
        super().__init__(*args)
        self._calls = 0

    async def create_with_outbox(self, **kwargs: object):
        """委托真实写入后在第二项抛出，避免只测试内存模拟。"""
        result = await super().create_with_outbox(**kwargs)
        self._calls += 1
        if self._calls == 2:
            raise RuntimeError("simulated batch persistence failure")
        return result


class NoopDispatcher:
    """记录不应发生的提交后投递，防止测试依赖 Redis。"""

    async def dispatch(self, task_id: UUID) -> TaskStatus:
        """若事务回滚路径错误触发投递，测试将显式失败。"""
        raise AssertionError(f"unexpected dispatch for {task_id}")


@pytest.mark.asyncio
async def test_second_batch_write_failure_rolls_back_all_task_facts(database_url: str) -> None:
    """第二项失败时邮件 TaskRun、审计、Outbox 也不能残留在 PostgreSQL。"""
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            user = UserModel(
                email="batch-owner@example.com",
                display_name="Batch Owner",
                password_hash=None,
                timezone="Asia/Shanghai",
                locale="zh-CN",
                brief_time=time(8, 0),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            user_id = user.id

        @asynccontextmanager
        async def failing_factory() -> AsyncIterator[FailingSecondTaskRepository]:
            async with sessions.begin() as session:
                yield FailingSecondTaskRepository(session)

        use_case = CreateTaskUseCase(failing_factory, NoopDispatcher())
        with pytest.raises(RuntimeError, match="batch persistence failure"):
            await use_case.execute_many(
                user_id=user_id,
                items=(
                    CreateTaskBatchItem(
                        "sync_mail",
                        {"connection_id": "synthetic", "scope_key": "mailbox"},
                        "batch:gmail",
                    ),
                    CreateTaskBatchItem(
                        "sync_calendar", {"connection_id": "synthetic"}, "batch:calendar"
                    ),
                ),
            )
        async with sessions() as session:
            task_count = await session.scalar(select(func.count()).select_from(TaskRunModel))
            audit_count = await session.scalar(select(func.count()).select_from(AuditEventModel))
            outbox_count = await session.scalar(select(func.count()).select_from(OutboxEventModel))
        assert task_count == 0
        assert audit_count == 0
        assert outbox_count == 0
    finally:
        await sessions.dispose()
