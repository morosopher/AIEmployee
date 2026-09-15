"""为普通结果事务测试提供真实删除屏障和只输出表名的事实比较工具。"""

from uuid import UUID

import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.task_execution import LeasedTask
from ai_employee.infrastructure.db.base import Base
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker
from tests.integration.privacy.test_all_data_deletion import (
    _PhaseCrashWorker,
    _seed_owned_deletion_lease,
)

type DatabaseFacts = dict[str, tuple[tuple[object, ...], ...]]


async def commit_deletion_barrier(
    sessions: ManagedAsyncSessionMaker, *, user_id: UUID
) -> LeasedTask:
    """运行真实 QUEUED→RUNNING→inactive CAS，只在已提交 barrier hook 中止。

    返回持久赢家租约供资格先执行的邻接测试使用；不以直接 UPDATE 用户替代生产时序。
    """
    lease = await _seed_owned_deletion_lease(
        sessions, user_id=user_id, request_id="synthetic-inflight-business-deletion"
    )
    worker = _PhaseCrashWorker(sessions, "barrier")
    with pytest.raises(RuntimeError, match="synthetic deletion phase crash"):
        await worker.execute(lease)
    assert worker.visited == ["barrier"]
    return lease


async def database_facts(sessions: ManagedAsyncSessionMaker) -> DatabaseFacts:
    """在隔离合成数据库内读取全部 ORM 事实，包含密文、审计、Outbox 和删除赢家。

    内容只保存在测试进程内；失败比较只输出改变的表名，不能让 pytest 展开正文或凭据。
    """
    facts: DatabaseFacts = {}
    async with sessions() as session:
        for table in Base.metadata.sorted_tables:
            rows = await session.execute(select(table).order_by(*table.primary_key.columns))
            facts[table.name] = tuple(tuple(row) for row in rows.all())
    return facts


def assert_facts_unchanged(before: DatabaseFacts, after: DatabaseFacts) -> None:
    """以真实行值比较整个提交边界，只暴露缺陷涉及的表名。"""
    changed = sorted(
        name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
    )
    assert not changed, f"late ordinary result changed tables after privacy barrier: {changed}"
