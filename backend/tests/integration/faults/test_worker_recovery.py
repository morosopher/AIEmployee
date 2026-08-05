"""在真实 PostgreSQL 上覆盖丢失租约后禁止提交。"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import TaskRunModel
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory


@pytest.mark.asyncio
async def test_worker_losing_lease_cannot_commit_terminal_result(database_url: str) -> None:
    """过期 owner 被接管后，其终态 CAS 不命中且不得覆盖新持有人。"""
    now = datetime(2026, 8, 5, tzinfo=UTC)
    task_id = uuid4()
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = UserModel(
                email=f"lease-{uuid4().hex}@example.test",
                display_name="Lease",
                password_hash=None,
                timezone="UTC",
                locale="zh-CN",
                brief_time=time(8),
                is_active=True,
            )
            session.add(user)
            await session.flush()
            session.add(
                TaskRunModel(
                    id=task_id,
                    user_id=user.id,
                    kind="daily_brief",
                    status=TaskStatus.RUNNING.value,
                    lease_owner="expired-owner",
                    lease_expires_at=now - timedelta(seconds=1),
                    idempotency_key=f"lease:{task_id}",
                    input_payload={},
                )
            )
        store = SqlAlchemyTaskExecutionStore(session_factory)
        assert (
            await store.acquire(
                task_id=task_id,
                lease_owner="new-owner",
                now=now,
                lease_expires_at=now + timedelta(seconds=30),
            )
            is not None
        )
        assert not await store.finish(
            task_id=task_id,
            lease_owner="expired-owner",
            status=TaskStatus.SUCCEEDED,
            finished_at=now,
            error_code=None,
        )
        async with session_factory() as session:
            task = await session.scalar(select(TaskRunModel).where(TaskRunModel.id == task_id))
        assert (
            task is not None
            and task.status == TaskStatus.RUNNING.value
            and task.lease_owner == "new-owner"
        )
    finally:
        await session_factory.dispose()
