"""在真实 PostgreSQL 上验证任务创建的原子性、幂等性与用户隔离。"""

from datetime import datetime, time, timedelta
from uuid import UUID

import pytest
from sqlalchemy import func, select

from ai_employee.application.use_cases.tasks import CreateTaskUseCase
from ai_employee.domain.tasks import TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    AuditEventModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories.tasks import SqlAlchemyTaskRepositoryFactory
from ai_employee.infrastructure.db.session import build_session_factory


def _synthetic_user(*, email: str, display_name: str) -> UserModel:
    """构造不含真实个人资料且使用显式 UTC 偏好的测试用户。"""
    return UserModel(
        email=email,
        display_name=display_name,
        password_hash=None,
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


def _assert_utc(value: datetime) -> None:
    """确认数据库返回的是显式 UTC 时间，而不是宿主机本地时间。"""
    assert value.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_create_task_commits_task_audit_and_outbox_once(database_url: str) -> None:
    """首次调用原子写入三类事实，顺序重放同一键只返回原任务。"""
    session_factory = build_session_factory(database_url)
    try:
        async with session_factory.begin() as session:
            user = _synthetic_user(
                email="task-owner@example.com",
                display_name="Task Owner",
            )
            session.add(user)
            await session.flush()
            user_id = user.id

        use_case = CreateTaskUseCase(SqlAlchemyTaskRepositoryFactory(session_factory))
        created = await use_case.execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-07-30"},
            idempotency_key="brief:user:2026-07-30:scheduled",
        )
        repeated = await use_case.execute(
            user_id=user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-07-30"},
            idempotency_key="brief:user:2026-07-30:scheduled",
        )

        assert repeated.task_id == created.task_id

        # 必须从新 Session 读取提交后的事实，避免把同一 identity map 的未提交对象误判为成功。
        async with session_factory() as session:
            tasks = (
                await session.scalars(select(TaskRunModel).where(TaskRunModel.user_id == user_id))
            ).all()
            audits = (
                await session.scalars(
                    select(AuditEventModel).where(AuditEventModel.user_id == user_id)
                )
            ).all()
            outbox_events = (
                await session.scalars(
                    select(OutboxEventModel).where(OutboxEventModel.aggregate_id == created.task_id)
                )
            ).all()

        assert len(tasks) == 1
        assert isinstance(created.task_id, UUID)
        assert tasks[0].id == created.task_id
        assert tasks[0].status == TaskStatus.CREATED.value
        assert tasks[0].kind == "daily_brief"
        assert tasks[0].input_payload == {"local_date": "2026-07-30"}
        assert tasks[0].attempt_count == 0
        _assert_utc(tasks[0].created_at)
        _assert_utc(tasks[0].updated_at)

        assert len(audits) == 1
        assert audits[0].task_id == created.task_id
        assert audits[0].event_type == "task.created"
        assert audits[0].event_metadata == {
            "kind": "daily_brief",
            "status": TaskStatus.CREATED.value,
        }
        _assert_utc(audits[0].created_at)

        assert len(outbox_events) == 1
        assert outbox_events[0].topic == "task.execute"
        assert outbox_events[0].aggregate_id == created.task_id
        assert outbox_events[0].deduplication_key == (f"task.execute:{created.task_id}:initial")
        assert outbox_events[0].payload == {"task_id": str(created.task_id)}
        assert outbox_events[0].attempt_count == 0
        assert outbox_events[0].published_at is None
        _assert_utc(outbox_events[0].available_at)
        _assert_utc(outbox_events[0].created_at)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_same_idempotency_key_is_isolated_by_user(database_url: str) -> None:
    """两个用户复用同一幂等键时必须各自创建任务，不能跨用户错误复用。"""
    session_factory = build_session_factory(database_url)
    shared_key = "brief:shared-date:scheduled"
    try:
        async with session_factory.begin() as session:
            first_user = _synthetic_user(
                email="first-task-owner@example.com",
                display_name="First Task Owner",
            )
            second_user = _synthetic_user(
                email="second-task-owner@example.com",
                display_name="Second Task Owner",
            )
            session.add_all((first_user, second_user))
            await session.flush()
            first_user_id = first_user.id
            second_user_id = second_user.id

        use_case = CreateTaskUseCase(SqlAlchemyTaskRepositoryFactory(session_factory))
        first = await use_case.execute(
            user_id=first_user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-07-30"},
            idempotency_key=shared_key,
        )
        second = await use_case.execute(
            user_id=second_user_id,
            kind="daily_brief",
            input_payload={"local_date": "2026-07-30"},
            idempotency_key=shared_key,
        )

        assert first.task_id != second.task_id

        async with session_factory() as session:
            task_count = await session.scalar(select(func.count()).select_from(TaskRunModel))
            audit_count = await session.scalar(select(func.count()).select_from(AuditEventModel))
            outbox_count = await session.scalar(select(func.count()).select_from(OutboxEventModel))
            first_task = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.id == first.task_id,
                    TaskRunModel.user_id == first_user_id,
                )
            )
            second_task = await session.scalar(
                select(TaskRunModel).where(
                    TaskRunModel.id == second.task_id,
                    TaskRunModel.user_id == second_user_id,
                )
            )

        assert task_count == 2
        assert audit_count == 2
        assert outbox_count == 2
        assert first_task is not None
        assert second_task is not None
    finally:
        await session_factory.dispose()
