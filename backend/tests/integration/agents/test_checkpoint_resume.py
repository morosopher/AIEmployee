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
                    lease_owner="initial-worker",
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
                    "lease_owner": "initial-worker",
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
            async with session_factory.begin() as session:
                resumed_task = await session.get(TaskRunModel, task_id, with_for_update=True)
                assert resumed_task is not None
                resumed_task.status = TaskStatus.RUNNING.value
                resumed_task.lease_owner = "resume-worker"

            result = await graph.ainvoke(
                Command(resume="rejected", update={"lease_owner": "resume-worker"}), config=config
            )

        assert result["tool_called"] is False
        assert result["messages"].count("proposal prepared") == 1
        assert called_payloads == []
        async with session_factory() as session:
            completed_task = await session.get(TaskRunModel, task_id)
        assert completed_task is not None
        assert completed_task.status == TaskStatus.SUCCEEDED.value
    finally:
        await session_factory.dispose()


async def test_stale_graph_owner_cannot_pause_new_task_lease(database_url: str) -> None:
    """旧 Worker 的 checkpoint 重放不能暂停已由新 owner 接管的任务。"""
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="stale-owner@example.test",
                    display_name="Stale Owner",
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
                    lease_owner="new-worker",
                    idempotency_key="stale-owner",
                    input_payload={},
                )
            )

        graph = FakeWriteGraph(
            approval_store=SqlAlchemyApprovalStore(session_factory),
            clock=lambda: now,
            approval_ttl=timedelta(minutes=5),
            fake_tool=lambda _payload: __import__("asyncio").sleep(0),
        )
        async with postgres_checkpointer(database_url) as saver:
            compiled = graph.compile(checkpointer=saver)
            with pytest.raises(StateConflictError) as conflict:
                await compiled.ainvoke(
                    {
                        "task_id": str(task_id),
                        "lease_owner": "stale-worker",
                        "proposal_payload": {"value": "synthetic"},
                        "approval_decision": None,
                        "tool_called": False,
                        "messages": [],
                    },
                    config={"configurable": {"thread_id": str(task_id)}},
                )
        assert conflict.value.error_code == "task_conflict"
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
            approval = await session.scalar(select(ApprovalRequestModel))
        assert task is not None
        assert task.status == TaskStatus.RUNNING.value
        assert task.lease_owner == "new-worker"
        assert approval is None
    finally:
        await session_factory.dispose()


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_redelivered_initial_message_uses_persisted_approval_decision(
    database_url: str, decision: str
) -> None:
    """旧初始消息在决定已入库后恢复终态，不能再次中断同一审批。

    初始 Worker 在 LangGraph 保存 interrupt 后、Taskiq ACK 前崩溃时，旧消息可能在用户
    已完成决定后重新投递。此时 PostgreSQL 中的审批终态才是恢复事实来源：批准只能执行
    一次，拒绝则成功结束且永不调用工具。
    """
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    called_payloads: list[dict[str, object]] = []

    async def fake_tool(payload: dict[str, object]) -> None:
        """记录假写调用，用于证明批准恰好调用一次、拒绝完全不调用。"""
        called_payloads.append(payload)

    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email=f"redelivered-{decision}@example.test",
                    display_name="Redelivered Initial Message",
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
                    idempotency_key=f"redelivered-{decision}",
                    input_payload={"value": "synthetic"},
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
            initial_input = {
                "task_id": str(task_id),
                "lease_owner": "initial-worker",
                "proposal_payload": {"value": "synthetic"},
                "approval_decision": None,
                "tool_called": False,
                "messages": [],
            }
            first_result = await graph.ainvoke(initial_input, config=config)
            assert "__interrupt__" in first_result

            async with session_factory() as session:
                approval = await session.scalar(select(ApprovalRequestModel))
            assert approval is not None
            await ApprovalDecisionUseCase(store).execute(
                approval_id=approval.id,
                user_id=user_id,
                decision=decision,
                version=approval.version,
                payload_hash=approval.payload_hash,
                now=now,
            )
            async with session_factory.begin() as session:
                task = await session.get(TaskRunModel, task_id, with_for_update=True)
                assert task is not None
                assert task.status == TaskStatus.QUEUED.value
                task.status = TaskStatus.RUNNING.value
                task.lease_owner = "redelivered-worker"

            result = await graph.ainvoke(
                {
                    **initial_input,
                    "lease_owner": "redelivered-worker",
                },
                config=config,
            )

        assert "__interrupt__" not in result
        assert result["tool_called"] is (decision == "approved")
        assert called_payloads == ([{"value": "synthetic"}] if decision == "approved" else [])
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
        assert task is not None
        assert task.status == TaskStatus.SUCCEEDED.value
    finally:
        await session_factory.dispose()
