"""在真实 PostgreSQL 上验证任务持久化对象的组合归属不变量。"""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ai_employee.domain.tasks import ApprovalStatus, StepStatus, TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.session import ManagedAsyncSessionMaker, build_session_factory

TASK_RETRY_OWNER_FK = "fk_task_runs_retry_of_task_id_user_id"
AUDIT_TASK_OWNER_FK = "fk_audit_events_task_id_user_id"
APPROVAL_STEP_TASK_FK = "fk_approval_requests_step_id_task_id"
TOOL_STEP_TASK_FK = "fk_tool_executions_step_id_task_id"


@dataclass(frozen=True, slots=True)
class OwnershipFixture:
    """保存两个用户、任务和步骤的已提交合成标识。"""

    first_user_id: UUID
    second_user_id: UUID
    first_task_id: UUID
    second_task_id: UUID
    first_step_id: UUID
    second_step_id: UUID


def _synthetic_user(*, ordinal: str) -> UserModel:
    """构造没有真实个人数据且邮箱唯一的测试管理员。"""
    return UserModel(
        email=f"ownership-{ordinal}@example.com",
        display_name=f"Ownership {ordinal}",
        password_hash=None,
        timezone="UTC",
        locale="zh-CN",
        brief_time=time(8, 0),
        is_active=True,
    )


def _task(*, user_id: UUID, key: str, retry_of_task_id: UUID | None = None) -> TaskRunModel:
    """构造显式归属用户的最小任务事实。"""
    return TaskRunModel(
        user_id=user_id,
        retry_of_task_id=retry_of_task_id,
        kind="daily_brief",
        status=TaskStatus.CREATED.value,
        idempotency_key=key,
        input_payload={"local_date": "2026-07-30"},
    )


def _step(*, task_id: UUID, sequence: int) -> TaskStepModel:
    """构造显式归属任务的最小步骤事实。"""
    return TaskStepModel(
        task_id=task_id,
        sequence=sequence,
        name=f"step-{sequence}",
        kind="deterministic",
        status=StepStatus.PENDING.value,
        input_summary={},
    )


def _approval(*, task_id: UUID, step_id: UUID) -> ApprovalRequestModel:
    """构造绑定指定任务与步骤的合成审批事实。"""
    return ApprovalRequestModel(
        task_id=task_id,
        step_id=step_id,
        version=1,
        action="fake.calendar.create",
        payload={"synthetic": True},
        payload_hash="a" * 64,
        preview_markdown="Synthetic preview",
        status=ApprovalStatus.PENDING.value,
        expires_at=datetime(2026, 7, 30, 9, 0, tzinfo=UTC),
    )


def _tool_execution(*, task_id: UUID, step_id: UUID, key: str) -> ToolExecutionModel:
    """构造绑定指定任务与步骤的合成假工具执行事实。"""
    return ToolExecutionModel(
        task_id=task_id,
        step_id=step_id,
        tool_name="fake.calendar.create",
        idempotency_key=key,
        request_payload_hash="b" * 64,
        status="pending",
    )


def _foreign_key_error_details(error: IntegrityError) -> tuple[str | None, str | None]:
    """从 asyncpg 原始异常链提取 SQLSTATE 与命名约束。

    ``getattr`` 只用于不受本项目控制的 SQLAlchemy/asyncpg 异常边界；返回值立即收窄为
    字符串，从而让测试同时证明这是外键违规且命中了预期组合约束。
    """
    if error.orig is None:
        return None, None
    cause = error.orig.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    constraint_name = getattr(cause, "constraint_name", None)
    return (
        sqlstate if isinstance(sqlstate, str) else None,
        constraint_name if isinstance(constraint_name, str) else None,
    )


async def _seed_ownership_fixture(
    session_factory: ManagedAsyncSessionMaker,
) -> OwnershipFixture:
    """提交两套互不归属的用户、任务与步骤，供反例交叉引用。"""
    async with session_factory.begin() as session:
        first_user = _synthetic_user(ordinal="first")
        second_user = _synthetic_user(ordinal="second")
        session.add_all((first_user, second_user))
        await session.flush()

        first_task = _task(user_id=first_user.id, key="ownership:first")
        second_task = _task(user_id=second_user.id, key="ownership:second")
        session.add_all((first_task, second_task))
        await session.flush()

        first_step = _step(task_id=first_task.id, sequence=1)
        second_step = _step(task_id=second_task.id, sequence=1)
        session.add_all((first_step, second_step))
        await session.flush()

        return OwnershipFixture(
            first_user_id=first_user.id,
            second_user_id=second_user.id,
            first_task_id=first_task.id,
            second_task_id=second_task.id,
            first_step_id=first_step.id,
            second_step_id=second_step.id,
        )


@pytest.mark.asyncio
async def test_cross_user_retry_is_rejected_by_named_foreign_key(database_url: str) -> None:
    """重试任务不得引用另一个用户拥有的原任务。"""
    session_factory = build_session_factory(database_url)
    try:
        fixture = await _seed_ownership_fixture(session_factory)
        flush_completed = False

        with pytest.raises(IntegrityError) as raised:
            async with session_factory.begin() as session:
                session.add(
                    _task(
                        user_id=fixture.first_user_id,
                        key="ownership:invalid-retry",
                        retry_of_task_id=fixture.second_task_id,
                    )
                )
                await session.flush()
                flush_completed = True

        assert flush_completed is True
        assert _foreign_key_error_details(raised.value) == ("23503", TASK_RETRY_OWNER_FK)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_cross_user_audit_task_is_rejected_by_named_foreign_key(
    database_url: str,
) -> None:
    """审计事件的用户与关联任务必须属于同一用户。"""
    session_factory = build_session_factory(database_url)
    try:
        fixture = await _seed_ownership_fixture(session_factory)

        with pytest.raises(IntegrityError) as raised:
            async with session_factory.begin() as session:
                session.add(
                    AuditEventModel(
                        user_id=fixture.first_user_id,
                        task_id=fixture.second_task_id,
                        event_type="synthetic.invalid_ownership",
                        actor_type="system",
                        actor_id=None,
                        event_metadata={},
                    )
                )
                await session.flush()

        assert _foreign_key_error_details(raised.value) == ("23503", AUDIT_TASK_OWNER_FK)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_cross_task_approval_step_is_rejected_by_named_foreign_key(
    database_url: str,
) -> None:
    """审批的步骤必须属于审批记录声明的同一个任务。"""
    session_factory = build_session_factory(database_url)
    try:
        fixture = await _seed_ownership_fixture(session_factory)

        with pytest.raises(IntegrityError) as raised:
            async with session_factory.begin() as session:
                session.add(
                    _approval(
                        task_id=fixture.first_task_id,
                        step_id=fixture.second_step_id,
                    )
                )
                await session.flush()

        assert _foreign_key_error_details(raised.value) == ("23503", APPROVAL_STEP_TASK_FK)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_cross_task_tool_step_is_rejected_by_named_foreign_key(database_url: str) -> None:
    """工具执行的步骤必须属于工具记录声明的同一个任务。"""
    session_factory = build_session_factory(database_url)
    try:
        fixture = await _seed_ownership_fixture(session_factory)

        with pytest.raises(IntegrityError) as raised:
            async with session_factory.begin() as session:
                session.add(
                    _tool_execution(
                        task_id=fixture.first_task_id,
                        step_id=fixture.second_step_id,
                        key="ownership:invalid-tool",
                    )
                )
                await session.flush()

        assert _foreign_key_error_details(raised.value) == ("23503", TOOL_STEP_TASK_FK)
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_matching_ownership_graph_remains_writable(database_url: str) -> None:
    """同用户重试及同任务步骤、审批、工具和审计必须仍可原子写入。"""
    session_factory = build_session_factory(database_url)
    try:
        fixture = await _seed_ownership_fixture(session_factory)

        async with session_factory.begin() as session:
            retry = _task(
                user_id=fixture.first_user_id,
                key="ownership:valid-retry",
                retry_of_task_id=fixture.first_task_id,
            )
            session.add(retry)
            session.add(
                _approval(
                    task_id=fixture.first_task_id,
                    step_id=fixture.first_step_id,
                )
            )
            session.add(
                _tool_execution(
                    task_id=fixture.first_task_id,
                    step_id=fixture.first_step_id,
                    key="ownership:valid-tool",
                )
            )
            session.add(
                AuditEventModel(
                    user_id=fixture.first_user_id,
                    task_id=fixture.first_task_id,
                    event_type="synthetic.valid_ownership",
                    actor_type="system",
                    actor_id=None,
                    event_metadata={},
                )
            )
            await session.flush()
            retry_id = retry.id

        async with session_factory() as session:
            retry_owner = await session.scalar(
                select(TaskRunModel.user_id).where(TaskRunModel.id == retry_id)
            )
            approval_count = await session.scalar(
                select(func.count()).select_from(ApprovalRequestModel)
            )
            tool_count = await session.scalar(select(func.count()).select_from(ToolExecutionModel))
            audit_count = await session.scalar(select(func.count()).select_from(AuditEventModel))

        assert retry_owner == fixture.first_user_id
        assert approval_count == 1
        assert tool_count == 1
        assert audit_count == 1
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_deleting_task_preserves_retry_and_audit_retention(database_url: str) -> None:
    """删除原任务时重试与审计只清空任务引用，审计用户必须保留。"""
    session_factory = build_session_factory(database_url)
    try:
        fixture = await _seed_ownership_fixture(session_factory)

        async with session_factory.begin() as session:
            retry = _task(
                user_id=fixture.first_user_id,
                key="ownership:delete-retry",
                retry_of_task_id=fixture.first_task_id,
            )
            audit = AuditEventModel(
                user_id=fixture.first_user_id,
                task_id=fixture.first_task_id,
                event_type="synthetic.delete_retention",
                actor_type="system",
                actor_id=None,
                event_metadata={},
            )
            session.add_all((retry, audit))
            await session.flush()
            retry_id = retry.id
            audit_id = audit.id

            # 使用 Core DELETE 让数据库外键独立决定保留语义，不由 ORM 模拟级联或置空。
            await session.execute(
                delete(TaskRunModel)
                .where(TaskRunModel.id == fixture.first_task_id)
                .execution_options(synchronize_session=False)
            )

        async with session_factory() as session:
            retry_reference = await session.scalar(
                select(TaskRunModel.retry_of_task_id).where(TaskRunModel.id == retry_id)
            )
            audit_row = (
                await session.execute(
                    select(AuditEventModel.task_id, AuditEventModel.user_id).where(
                        AuditEventModel.id == audit_id
                    )
                )
            ).one()

        assert retry_reference is None
        assert audit_row.task_id is None
        assert audit_row.user_id == fixture.first_user_id
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_deleting_step_cascades_matching_approval_and_tool(database_url: str) -> None:
    """删除步骤时既有单列 CASCADE 必须继续清理同任务审批与工具事实。"""
    session_factory = build_session_factory(database_url)
    try:
        fixture = await _seed_ownership_fixture(session_factory)

        async with session_factory.begin() as session:
            session.add(
                _approval(
                    task_id=fixture.first_task_id,
                    step_id=fixture.first_step_id,
                )
            )
            session.add(
                _tool_execution(
                    task_id=fixture.first_task_id,
                    step_id=fixture.first_step_id,
                    key="ownership:delete-step-tool",
                )
            )
            await session.flush()
            await session.execute(
                delete(TaskStepModel)
                .where(TaskStepModel.id == fixture.first_step_id)
                .execution_options(synchronize_session=False)
            )

        async with session_factory() as session:
            approval_count = await session.scalar(
                select(func.count())
                .select_from(ApprovalRequestModel)
                .where(ApprovalRequestModel.task_id == fixture.first_task_id)
            )
            tool_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == fixture.first_task_id)
            )

        assert approval_count == 0
        assert tool_count == 0
    finally:
        await session_factory.dispose()


@pytest.mark.asyncio
async def test_deleting_task_cascades_matching_step_approval_and_tool(database_url: str) -> None:
    """删除任务时步骤、审批与工具的既有级联链不能被组合 guard 阻断。"""
    session_factory = build_session_factory(database_url)
    try:
        fixture = await _seed_ownership_fixture(session_factory)

        async with session_factory.begin() as session:
            session.add(
                _approval(
                    task_id=fixture.second_task_id,
                    step_id=fixture.second_step_id,
                )
            )
            session.add(
                _tool_execution(
                    task_id=fixture.second_task_id,
                    step_id=fixture.second_step_id,
                    key="ownership:delete-task-tool",
                )
            )
            await session.flush()
            await session.execute(
                delete(TaskRunModel)
                .where(TaskRunModel.id == fixture.second_task_id)
                .execution_options(synchronize_session=False)
            )

        async with session_factory() as session:
            step_count = await session.scalar(
                select(func.count())
                .select_from(TaskStepModel)
                .where(TaskStepModel.task_id == fixture.second_task_id)
            )
            approval_count = await session.scalar(
                select(func.count())
                .select_from(ApprovalRequestModel)
                .where(ApprovalRequestModel.task_id == fixture.second_task_id)
            )
            tool_count = await session.scalar(
                select(func.count())
                .select_from(ToolExecutionModel)
                .where(ToolExecutionModel.task_id == fixture.second_task_id)
            )

        assert step_count == 0
        assert approval_count == 0
        assert tool_count == 0
    finally:
        await session_factory.dispose()
