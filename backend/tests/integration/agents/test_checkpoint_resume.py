"""在真实 PostgreSQL checkpoint 上验证假写审批中断与恢复。"""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from langgraph.types import Command
from sqlalchemy import select

from ai_employee.agents.fake_write.graph import FakeWriteGraph
from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.use_cases.approvals import ApprovalDecisionUseCase
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.session import build_session_factory


async def test_rejected_checkpoint_resume_never_calls_fake_tool(database_url: str) -> None:
    """首次运行中断并冻结审批；拒绝恢复成功但绝不执行假写。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    called_payloads: list[dict[str, object]] = []

    async def fake_tool(payload: dict[str, object]) -> None:
        """记录调用，以证明拒绝分支没有任何工具副作用。"""
        called_payloads.append(payload)

    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="checkpoint-test@example.test",
                    display_name="Checkpoint Test",
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
                    idempotency_key="checkpoint-reject",
                    input_payload={},
                )
            )

        store = SqlAlchemyApprovalStore(session_factory)
        async with postgres_checkpointer(database_url) as saver:
            graph = FakeWriteGraph(
                approval_store=store,
                clock=lambda: now,
                approval_ttl=timedelta(minutes=5),
                fake_tool=fake_tool,
            ).compile(checkpointer=saver)
            config = {"configurable": {"thread_id": str(task_id)}}
            result = await graph.ainvoke(
                {
                    "task_id": str(task_id),
                    "proposal_payload": {"value": "synthetic"},
                    "approval_decision": None,
                    "tool_called": False,
                    "messages": [],
                },
                config=config,
            )
            assert "__interrupt__" in result

            async with session_factory() as session:
                approval = await session.scalar(select(ApprovalRequestModel))
                task = await session.get(TaskRunModel, task_id)
            assert approval is not None
            assert task is not None
            assert approval.status == ApprovalStatus.PENDING.value
            assert task.status == TaskStatus.WAITING_APPROVAL.value

            with pytest.raises(StateConflictError) as conflict:
                await ApprovalDecisionUseCase(store).execute(
                    approval_id=approval.id,
                    user_id=user_id,
                    decision="rejected",
                    version=approval.version,
                    payload_hash="b" * 64,
                    now=now,
                )
            assert conflict.value.error_code == "approval_conflict"
            async with session_factory() as session:
                paused_task = await session.get(TaskRunModel, task_id)
            assert paused_task is not None
            assert paused_task.status == TaskStatus.WAITING_APPROVAL.value

            await ApprovalDecisionUseCase(store).execute(
                approval_id=approval.id,
                user_id=user_id,
                decision="rejected",
                version=approval.version,
                payload_hash=approval.payload_hash,
                now=now,
            )
            async with session_factory() as session:
                resume_event = await session.scalar(select(OutboxEventModel))
            assert resume_event is not None
            assert resume_event.topic == "task.execute"
            assert resume_event.aggregate_id == task_id
            assert resume_event.payload == {"task_id": str(task_id), "resume": "rejected"}
            with pytest.raises(StateConflictError):
                await ApprovalDecisionUseCase(store).execute(
                    approval_id=approval.id,
                    user_id=user_id,
                    decision="rejected",
                    version=approval.version,
                    payload_hash=approval.payload_hash,
                    now=now,
                )
            result = await graph.ainvoke(Command(resume="rejected"), config=config)

        assert result["tool_called"] is False
        assert result["messages"].count("proposal prepared") == 1
        assert called_payloads == []
        async with session_factory() as session:
            completed_task = await session.get(TaskRunModel, task_id)
        assert completed_task is not None
        assert completed_task.status == TaskStatus.SUCCEEDED.value
    finally:
        await session_factory.dispose()
