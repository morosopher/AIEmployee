"""验证假写审批在 PostgreSQL 中冻结并可安全终止。"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

from sqlalchemy import select

from ai_employee.application.use_cases.approvals import ExpireApprovalsUseCase
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    AuditEventModel,
    TaskRunModel,
    TaskStepModel,
    ToolExecutionModel,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
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
