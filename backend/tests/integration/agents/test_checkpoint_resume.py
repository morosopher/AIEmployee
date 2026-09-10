"""在真实 PostgreSQL checkpoint 上验证假写审批中断与恢复。"""

import asyncio
import os
from datetime import UTC, datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command
from redis.asyncio import Redis
from sqlalchemy import select

from ai_employee.agents.fake_write.graph import FakeWriteGraph
from ai_employee.agents.runner import postgres_checkpointer
from ai_employee.application.use_cases.approval_checkpoint_recovery import (
    RecoverApprovalCheckpointsUseCase,
)
from ai_employee.application.use_cases.approvals import ApprovalDecisionUseCase, PendingApproval
from ai_employee.application.use_cases.task_execution import DurableTaskRunner
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalProposal, ApprovalStatus, TaskStatus
from ai_employee.infrastructure.db.models.identity import UserModel
from ai_employee.infrastructure.db.models.tasks import (
    ApprovalRequestModel,
    OutboxEventModel,
    TaskRunModel,
)
from ai_employee.infrastructure.db.repositories.approval_checkpoint_recovery import (
    SqlAlchemyApprovalCheckpointRecoveryStore,
)
from ai_employee.infrastructure.db.repositories.approvals import SqlAlchemyApprovalStore
from ai_employee.infrastructure.db.repositories.task_execution import SqlAlchemyTaskExecutionStore
from ai_employee.infrastructure.db.session import build_session_factory
from ai_employee.infrastructure.queue.redis_url import validate_test_redis_url
from ai_employee.workers.execute_task import _FakeWriteStep


@pytest.mark.asyncio
async def test_second_worker_resumes_postgres_checkpoint_without_rerunning_completed_node(
    database_url: str,
) -> None:
    """第二个 Worker 用同一 task_id thread 恢复时，不得重跑已写 checkpoint 的前置节点。"""
    task_id = uuid4()
    calls = {"prepare": 0, "continue": 0}
    # 生产 saver 只接受真实业务 TaskRun 的规范 thread；平台Graph回归也须补齐归属，
    # 不能为测试绕过保存入口的活动用户屏障。
    sessions = build_session_factory(database_url)
    try:
        async with sessions.begin() as session:
            user_id = uuid4()
            session.add(
                UserModel(
                    id=user_id,
                    email=f"{user_id}@example.test",
                    display_name="Synthetic",
                    timezone="UTC",
                    locale="zh-CN",
                    brief_time=time(8),
                    is_active=True,
                )
            )
            await session.flush()
            session.add(
                TaskRunModel(
                    id=task_id,
                    user_id=user_id,
                    kind="fake_write",
                    status="running",
                    idempotency_key=str(task_id),
                    input_payload={},
                    graph_thread_id=str(task_id),
                )
            )
    finally:
        await sessions.dispose()

    async def prepare(state: dict[str, object]) -> dict[str, object]:
        """模拟第一个 Worker 已完成的无副作用预处理节点。"""
        del state
        calls["prepare"] += 1
        return {"prepared": True}

    async def approval_gate(state: dict[str, object]) -> dict[str, object]:
        """第一次持久化中断，恢复后只执行此后的继续节点。"""
        del state
        from langgraph.types import interrupt

        interrupt("synthetic handoff")
        calls["continue"] += 1
        return {"continued": True}

    graph_builder = StateGraph(dict)
    graph_builder.add_node("prepare", prepare)
    graph_builder.add_node("approval_gate", approval_gate)
    graph_builder.add_edge(START, "prepare")
    graph_builder.add_edge("prepare", "approval_gate")
    graph_builder.add_edge("approval_gate", END)
    config = {"configurable": {"thread_id": str(task_id)}}

    async with postgres_checkpointer(database_url) as first_saver:
        first_worker_graph = graph_builder.compile(checkpointer=first_saver)
        first_result = await first_worker_graph.ainvoke({}, config=config)
    assert "__interrupt__" in first_result
    assert calls == {"prepare": 1, "continue": 0}

    # 第二个 Saver 实例代表进程故障后的新 Worker；只共享 PostgreSQL checkpoint 事实。
    async with postgres_checkpointer(database_url) as second_saver:
        second_worker_graph = graph_builder.compile(checkpointer=second_saver)
        resumed = await second_worker_graph.ainvoke(Command(resume="approved"), config=config)

    assert resumed["continued"] is True
    assert calls == {"prepare": 1, "continue": 1}


async def test_scanner_recovers_published_initial_message_lost_with_redis_before_interrupt_checkpoint(
    database_url: str,
) -> None:
    """冻结审批后的崩溃可仅依靠 PostgreSQL 补投并完成一次安全决议。

    初始 Outbox 已被 relay 标记 published 后模拟 Redis 清空，因此它不能再作为恢复来源。
    扫描器必须从冻结事务留下的 PostgreSQL recovery anchor 创建新消息；恢复 Worker 保存
    interrupt checkpoint 后清除该 anchor，用户随后才能通过 checkpoint 门槛提交决定。
    """
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="checkpoint-recovery@example.test",
                    display_name="Checkpoint Recovery",
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
                    idempotency_key="checkpoint-recovery",
                    input_payload={"value": "synthetic"},
                )
            )

        store = SqlAlchemyApprovalStore(session_factory)
        await store.create_or_get_pending(
            task_id=task_id,
            lease_owner="initial-worker",
            proposal=ApprovalProposal.create("fake.write", {"value": "synthetic"}),
            preview_markdown="将执行合成假写操作。",
            expires_at=now + timedelta(minutes=5),
            checkpoint_recovery_at=now,
        )
        async with session_factory.begin() as session:
            initial_event = OutboxEventModel(
                topic="task.execute",
                aggregate_id=task_id,
                deduplication_key=f"task.execute:{task_id}:initial",
                payload={"task_id": str(task_id)},
                available_at=now,
                published_at=now,
            )
            session.add(initial_event)

        redis_value = os.environ.get("TEST_REDIS_URL")
        assert redis_value is not None
        redis_url = validate_test_redis_url(redis_value).value
        redis = Redis.from_url(str(redis_url), decode_responses=False)
        try:
            await redis.flushdb()
        finally:
            await redis.aclose()

        recovered = await RecoverApprovalCheckpointsUseCase(
            store=SqlAlchemyApprovalCheckpointRecoveryStore(session_factory)
        ).execute(now=now, limit=10)
        assert recovered == 1
        async with session_factory() as session:
            recovery_event = await session.scalar(
                select(OutboxEventModel).where(
                    OutboxEventModel.aggregate_id == task_id,
                    OutboxEventModel.published_at.is_(None),
                )
            )
            assert recovery_event is not None
            assert recovery_event.payload == {
                "task_id": str(task_id),
                "recover_approval_checkpoint": True,
            }
        runner = DurableTaskRunner(
            store=SqlAlchemyTaskExecutionStore(session_factory),
            clock=lambda: now,
            lease_duration=timedelta(minutes=1),
            task_timeout_seconds=60,
            task_step_timeout_seconds=30,
            max_transient_retries=0,
            resolve_steps=lambda _task: (
                _FakeWriteStep(
                    approval_store=store,
                    resume=None,
                    checkpoint_database_url=database_url,
                ),
            ),
        )
        assert await runner.run(
            task_id,
            lease_owner="recovery-worker",
            recover_waiting_approval=True,
        )
        async with session_factory() as session:
            recovered_task = await session.get(TaskRunModel, task_id)
        assert recovered_task is not None
        assert recovered_task.status == TaskStatus.WAITING_APPROVAL.value
        assert recovered_task.approval_checkpoint_recovery_at is None

        async with session_factory() as session:
            approval = await session.scalar(select(ApprovalRequestModel))
        assert approval is not None
        await ApprovalDecisionUseCase(store).execute(
            approval_id=approval.id,
            user_id=user_id,
            decision="rejected",
            version=approval.version,
            payload_hash=approval.payload_hash,
            now=now,
        )
    finally:
        await session_factory.dispose()


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


async def test_decision_waits_until_first_interrupt_checkpoint_is_durable(
    database_url: str,
) -> None:
    """决议不能在图已读到 pending、但首次 interrupt 尚未落库时抢先提交。

    此交错模拟初始 Worker 先冻结审批，重投 Worker 随后读取同一 pending 审批，而用户
    恰好在该读取和 LangGraph 持久化 interrupt 之间提交决定。PostgreSQL 的决议事务必须
    以实际 ``__interrupt__`` checkpoint 为门槛；否则旧图调用会在已批准或拒绝的业务事实
    之后写入一个新的暂停 checkpoint。
    """
    session_factory = build_session_factory(database_url)
    user_id = uuid4()
    task_id = uuid4()
    now = datetime.now(UTC)
    pending_read = asyncio.Event()
    release_graph = asyncio.Event()
    store = SqlAlchemyApprovalStore(session_factory)

    class PausingApprovalStore(SqlAlchemyApprovalStore):
        """仅在 find 返回后暂停测试协程，稳定复现数据库与 checkpoint 的交错。"""

        async def find_for_graph(
            self, *, task_id: UUID, payload_hash: str
        ) -> PendingApproval | None:
            """在拿到 pending 快照后交出执行权，绝不改变生产决策路径。"""
            result = await super().find_for_graph(task_id=task_id, payload_hash=payload_hash)
            pending_read.set()
            await release_graph.wait()
            return result

    try:
        async with session_factory.begin() as session:
            session.add(
                UserModel(
                    id=user_id,
                    email="checkpoint-ordering@example.test",
                    display_name="Checkpoint Ordering",
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
                    idempotency_key="checkpoint-ordering",
                    input_payload={"value": "synthetic"},
                )
            )

        await store.create_or_get_pending(
            task_id=task_id,
            lease_owner="initial-worker",
            proposal=ApprovalProposal.create("fake.write", {"value": "synthetic"}),
            preview_markdown="将执行合成假写操作。",
            expires_at=now + timedelta(minutes=5),
            checkpoint_recovery_at=now,
        )
        async with session_factory() as session:
            approval = await session.scalar(select(ApprovalRequestModel))
        assert approval is not None

        graph = FakeWriteGraph(
            approval_store=PausingApprovalStore(session_factory),
            clock=lambda: now,
            approval_ttl=timedelta(minutes=5),
            fake_tool=lambda _payload: asyncio.sleep(0),
        )
        async with postgres_checkpointer(database_url) as saver:
            invocation = asyncio.create_task(
                graph.compile(checkpointer=saver).ainvoke(
                    {
                        "task_id": str(task_id),
                        "lease_owner": "replayed-worker",
                        "proposal_payload": {"value": "synthetic"},
                        "approval_decision": None,
                        "tool_called": False,
                        "messages": [],
                    },
                    config={"configurable": {"thread_id": str(task_id)}},
                )
            )
            await pending_read.wait()

            with pytest.raises(StateConflictError) as conflict:
                await ApprovalDecisionUseCase(store).execute(
                    approval_id=approval.id,
                    user_id=user_id,
                    decision="rejected",
                    version=approval.version,
                    payload_hash=approval.payload_hash,
                    now=now,
                )
            assert conflict.value.error_code == "approval_conflict"

            release_graph.set()
            result = await invocation
            assert "__interrupt__" in result

        await ApprovalDecisionUseCase(store).execute(
            approval_id=approval.id,
            user_id=user_id,
            decision="rejected",
            version=approval.version,
            payload_hash=approval.payload_hash,
            now=now,
        )
        async with session_factory() as session:
            task = await session.get(TaskRunModel, task_id)
        assert task is not None
        assert task.status == TaskStatus.QUEUED.value
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


@pytest.mark.asyncio
@pytest.mark.parametrize("write_kind", ["aput", "aput_writes"])
async def test_task27e_late_native_checkpoint_write_cannot_recreate_deleted_thread(
    database_url: str,
    write_kind: str,
) -> None:
    """真实 saver 已取得上下文后，另一事务提交 inactive/清理，迟到原生写必须零行。"""
    from langgraph.checkpoint.base import empty_checkpoint
    from sqlalchemy import text, update

    from ai_employee.domain.errors import StateConflictError
    from tests.integration.retention.test_m2_action_retention import NOW, seed_lifecycle_action

    sessions = build_session_factory(database_url)
    try:
        seed = await seed_lifecycle_action(sessions, retained=True)
        async with sessions.begin() as session:
            await session.execute(
                update(TaskRunModel)
                .where(TaskRunModel.id == seed.task_id)
                .values(
                    graph_thread_id=str(seed.task_id),
                )
            )
        checkpoint = empty_checkpoint()
        checkpoint["ts"] = NOW.isoformat()
        checkpoint["channel_values"] = {"synthetic": {"item": "synthetic"}}
        checkpoint["channel_versions"] = {"synthetic": "1"}
        config = {"configurable": {"thread_id": str(seed.task_id), "checkpoint_ns": ""}}
        async with postgres_checkpointer(database_url) as saver:
            saved = await saver.aput(
                config,
                checkpoint,
                {"source": "input", "step": 0, "parents": {}},
                {"synthetic": "1"},
            )
            await saver.aput_writes(saved, [("synthetic", "old-write")], "synthetic-node")
            async with sessions.begin() as session:
                await session.execute(
                    update(UserModel).where(UserModel.id == seed.user_id).values(is_active=False)
                )
            # 旧 SDK 的删除能力仅作 RED 现场构造；真实 privacy 组合另测其精确授权与三表覆盖。
            await saver.adelete_thread(str(seed.task_id))
            rejected = False
            try:
                if write_kind == "aput":
                    await saver.aput(
                        saved,
                        checkpoint,
                        {"source": "loop", "step": 1, "parents": {}},
                        {"synthetic": "1"},
                    )
                else:
                    await saver.aput_writes(saved, [("synthetic", "late-write")], "synthetic-node")
            except StateConflictError:
                rejected = True
            assert rejected, "inactive checkpoint writer must be rejected"
        async with sessions() as session:
            for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                assert (
                    await session.scalar(
                        text(f"SELECT count(*) FROM {table} WHERE thread_id=:thread"),
                        {
                            "thread": str(seed.task_id),
                        },
                    )
                    == 0
                )
    finally:
        await sessions.dispose()
