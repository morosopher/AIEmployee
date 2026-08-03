"""构建只验证审批协议、绝不访问真实外部资源的 LangGraph。"""

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta
from typing import Protocol, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ai_employee.agents.fake_write.state import FakeWriteState
from ai_employee.application.use_cases.approvals import ApprovalProposalStore
from ai_employee.domain.errors import StateConflictError
from ai_employee.domain.tasks import ApprovalProposal, JsonValue

FakeTool = Callable[[dict[str, object]], Awaitable[None]]


class CompiledFakeWriteGraph(Protocol):
    """收窄 Worker 调用的 LangGraph 异步运行边界。"""

    async def ainvoke(self, input: object, *, config: Mapping[str, object]) -> object:
        """运行初始状态或恢复命令，并返回由 LangGraph 管理的状态快照。"""


def _as_json_payload(payload: dict[str, object]) -> dict[str, JsonValue]:
    """在 Graph 外部输入边界收窄为领域审批允许的 JSON 对象。"""
    return payload  # type: ignore[return-value]


class FakeWriteGraph:
    """把准备、人工中断与批准后假写分隔为三个可 checkpoint 节点。"""

    def __init__(
        self,
        *,
        approval_store: ApprovalProposalStore,
        clock: Callable[[], datetime],
        approval_ttl: timedelta,
        fake_tool: FakeTool,
    ) -> None:
        """保存边界依赖；假工具只可能在批准后的 execute 节点调用。"""
        self._approval_store = approval_store
        self._clock = clock
        self._approval_ttl = approval_ttl
        self._fake_tool = fake_tool

    async def prepare(self, state: FakeWriteState) -> dict[str, object]:
        """确认提案已经冻结，但不产生任何外部副作用。"""
        return {"messages": [*state["messages"], "proposal prepared"]}

    async def approval(self, state: FakeWriteState) -> dict[str, object]:
        """持久化冻结审批后中断，并以数据库终态恢复重投的初始消息。

        Taskiq 采用至少一次投递，初始消息可能在图保存 interrupt 后、确认队列前重投。
        若用户已决定，不能再次依赖旧消息的 ``resume=None`` 参数中断；必须使用冻结审批
        的持久终态继续至 execute，使批准仅执行一次、拒绝不执行工具。
        """
        proposal = ApprovalProposal.create(
            "fake.write",
            _as_json_payload(state["proposal_payload"]),
        )
        task_id = UUID(state["task_id"])
        pending = await self._approval_store.find_for_graph(
            task_id=task_id, payload_hash=proposal.payload_hash
        )
        if pending is None:
            pending = await self._approval_store.create_or_get_pending(
                task_id=task_id,
                lease_owner=state["lease_owner"],
                proposal=proposal,
                preview_markdown="将执行合成假写操作。",
                expires_at=self._clock() + self._approval_ttl,
            )
        if pending.status == "pending":
            decision = interrupt(
                {
                    "approval_id": str(pending.approval_id),
                    "action": proposal.action,
                    "payload_hash": proposal.payload_hash,
                    "preview_markdown": "将执行合成假写操作。",
                    "version": pending.version,
                }
            )
        elif pending.status in {"approved", "rejected"}:
            # 审批决定与冻结载荷同属 PostgreSQL 事实，不能由陈旧队列消息覆盖。
            decision = pending.status
        else:
            raise StateConflictError(
                error_code="approval_conflict", message="approval is unavailable"
            )
        return {
            "approval_decision": decision,
            "messages": [*state["messages"], "approved resolved"],
        }

    async def execute(self, state: FakeWriteState) -> dict[str, object]:
        """只在明确批准时调用假工具；拒绝是成功完成但绝不假写。"""
        decision = state["approval_decision"]
        if decision not in {"approved", "rejected"}:
            raise ValueError("approval decision is unavailable")
        proposal = ApprovalProposal.create(
            "fake.write", _as_json_payload(state["proposal_payload"])
        )
        if decision == "approved":
            claim = await self._approval_store.claim_fake_tool_execution(
                task_id=UUID(state["task_id"]),
                lease_owner=state.get("lease_owner", ""),
                expected_payload_hash=proposal.payload_hash,
            )
            if claim.should_call:
                await self._fake_tool(dict(claim.payload))
                await self._approval_store.complete_fake_tool_execution(
                    task_id=UUID(state["task_id"]),
                    lease_owner=state.get("lease_owner", ""),
                    expected_payload_hash=proposal.payload_hash,
                )
            tool_called = claim.should_call
            message = "fake tool called"
        else:
            tool_called = False
            message = "proposal rejected"
        await self._approval_store.finish_fake_write(
            task_id=UUID(state["task_id"]),
            lease_owner=state.get("lease_owner", ""),
            decision=decision,
            payload_hash=proposal.payload_hash,
            now=self._clock(),
        )
        return {"tool_called": tool_called, "messages": [*state["messages"], message]}

    def compile(self, *, checkpointer: BaseCheckpointSaver[str]) -> CompiledFakeWriteGraph:
        """使用调用方提供的 PostgreSQL checkpointer 编译固定三节点工作流。"""
        graph = StateGraph(FakeWriteState)
        graph.add_node("prepare", self.prepare)
        graph.add_node("approval", self.approval)
        graph.add_node("execute", self.execute)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "approval")
        graph.add_edge("approval", "execute")
        graph.add_edge("execute", END)
        return cast(CompiledFakeWriteGraph, graph.compile(checkpointer=checkpointer))
