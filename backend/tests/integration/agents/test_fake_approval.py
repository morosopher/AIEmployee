"""验证假写审批在 PostgreSQL 中冻结并可安全终止。"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from ai_employee.application.use_cases.approvals import ExpireApprovalsUseCase
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory


async def test_expire_approvals_fails_overdue_task_without_tool_execution(
    database_url: str,
) -> None:
    """过期扫描必须原子失效审批、终止任务并且不执行假写工具。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="approval-test@example.test",
                    display_name="Approval Test",
                    password_hash="fake",
                    timezone="UTC",
                    brief_time=time(8),
                )
            )
            session.add(
                TaskRunModel(
                    id=task_id,
                    user_id=user_id,
                    kind="fake_write",
                    status=TaskStatus.WAITING_APPROVAL.value,
                    idempotency_key="approval-expiry",
                    input_payload={},
                )
            )
            step = TaskStepModel(
                task_id=task_id,
                sequence=1,
                name="approval",
                kind="approval",
                status="running",
                input_summary={},
            )
            session.add(step)
            await session.flush()
            session.add(
                ApprovalRequestModel(
                    task_id=task_id,
                    step_id=step.id,
                    version=1,
                    action="fake.write",
                    payload={"value": "synthetic"},
                    payload_hash="a" * 64,
                    preview_markdown="synthetic preview",
                    status=ApprovalStatus.PENDING.value,
                    expires_at=now - timedelta(seconds=1),
                )
            )

        expired = await ExpireApprovalsUseCase(SqlAlchemyApprovalStore(session_factory)).execute(
            now=now, limit=10
        )

        assert expired == 1
        async with session_factory() as session:
            approval = await session.scalar(select(ApprovalRequestModel))
            task = await session.get(TaskRunModel, task_id)
            events = (await session.scalars(select(AuditEventModel))).all()
            tool_executions = (await session.scalars(select(ToolExecutionModel))).all()
        assert approval is not None
        assert task is not None
        assert approval.status == ApprovalStatus.EXPIRED.value
        assert task.status == TaskStatus.FAILED.value
        assert task.error_code == "approval_expired"
        assert [event.event_type for event in events] == ["approval.expired"]
        assert tool_executions == []
    finally:
        await session_factory.dispose()


async def test_expiry_converges_recovered_running_task_to_approval_expired(
    database_url: str,
) -> None:
    """恢复租约先将过期审批任务置 RUNNING 时，到期扫描仍必须终止任务。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="expiry-recovery-race@example.test",
                    display_name="Expiry Recovery Race",
                    password_hash="fake",
                    timezone="UTC",
                    brief_time=time(8),
                )
            )
            session.add(
                TaskRunModel(
                    id=task_id,
                    user_id=user_id,
                    kind="fake_write",
                    status=TaskStatus.RUNNING.value,
                    lease_owner="initial-worker",
                    idempotency_key="expiry-recovery-race",
                    input_payload={"value": "synthetic"},
                )
            )
        approval_store = SqlAlchemyApprovalStore(session_factory)
        await approval_store.create_or_get_pending(
            task_id=task_id,
            lease_owner="initial-worker",
            proposal=ApprovalProposal.create("fake.write", {"value": "synthetic"}),
            preview_markdown="将执行合成假写操作。",
            expires_at=now - timedelta(seconds=1),
            checkpoint_recovery_at=now - timedelta(seconds=1),
        )
        recovered = await SqlAlchemyTaskExecutionStore(session_factory).acquire(
            task_id=task_id,
            lease_owner="checkpoint-recovery-worker",
            now=now,
            lease_expires_at=now + timedelta(minutes=1),
            recover_waiting_approval=True,
        )
        assert recovered is not None

        expired = await ExpireApprovalsUseCase(approval_store).execute(now=now, limit=10)
        assert expired == 1
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            approval = await session.scalar(select(ApprovalRequestModel))
            events = list(
                (
                    await session.scalars(
                        select(AuditEventModel).where(AuditEventModel.task_id == task_id)
                    )
                ).all()
            )
        assert task is not None
        assert approval is not None
        assert task.status == TaskStatus.FAILED.value
        assert task.error_code == "approval_expired"
        assert task.lease_owner is None
        assert approval.status == ApprovalStatus.EXPIRED.value
        assert [event.event_type for event in events].count("approval.expired") == 1
    finally:
        await session_factory.dispose()


async def test_claimed_fake_tool_replay_fails_without_marking_unknown_call_successful(
    database_url: str,
) -> None:
    """工具调用后崩溃留下 claimed 记录时，重放必须安全失败而不能伪造成功。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    task_id = uuid4()
    payload = {"value": "synthetic"}
    proposal = ApprovalProposal.create("fake.write", payload)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="claimed-tool@example.test",
                    display_name="Claimed Tool",
                    password_hash="fake",
                    timezone="UTC",
                    brief_time=time(8),
                )
            )
            session.add(
                TaskRunModel(
                    id=task_id,
                    user_id=user_id,
                    kind="fake_write",
                    status=TaskStatus.RUNNING.value,
                    lease_owner="worker-a",
                    idempotency_key="claimed-tool",
                    input_payload=payload,
                )
            )
            step = TaskStepModel(
                task_id=task_id,
                sequence=1,
                name="approval",
                kind="approval",
                status="running",
                input_summary={},
            )
            session.add(step)
            await session.flush()
            approval = ApprovalRequestModel(
                task_id=task_id,
                step_id=step.id,
                version=1,
                action="fake.write",
                payload=payload,
                payload_hash=proposal.payload_hash,
                preview_markdown="synthetic preview",
                status=ApprovalStatus.APPROVED.value,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            session.add(approval)
            await session.flush()
            session.add(
                ToolExecutionModel(
                    task_id=task_id,
                    step_id=step.id,
                    tool_name="fake.write",
                    idempotency_key=f"fake.write:{task_id}:{approval.id}:1",
                    request_payload_hash=proposal.payload_hash,
                    status="claimed",
                )
            )

        store = SqlAlchemyApprovalStore(session_factory)
        with pytest.raises(StateConflictError) as conflict:
            await store.claim_fake_tool_execution(
                task_id=task_id,
                lease_owner="worker-a",
                expected_payload_hash=proposal.payload_hash,
            )
        assert conflict.value.error_code == "tool_execution_outcome_unknown"
        async with session_factory() as session:
            execution = await session.scalar(select(ToolExecutionModel))
            task = await session.get(TaskRunModel, task_id)
        assert execution is not None
        assert execution.status == "claimed"
        assert task is not None
        assert task.status == TaskStatus.RUNNING.value
    finally:
        await session_factory.dispose()
