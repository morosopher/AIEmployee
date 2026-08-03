"""构建只验证审批协议、绝不访问真实外部资源的 LangGraph。"""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ai_employee.agents.fake_write.state import FakeWriteState
from ai_employee.application.use_cases.approvals import ApprovalProposalStore
from ai_employee.domain.tasks import ApprovalProposal, JsonValue

FakeTool = Callable[[dict[str, object]], Awaitable[None]]


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
        """持久化冻结审批后中断，恢复时以同一事实避免重建提案。"""
        proposal = ApprovalProposal.create(
            "fake.write",
            _as_json_payload(state["proposal_payload"]),
        )
        pending = await self._approval_store.create_or_get_pending(
            task_id=UUID(state["task_id"]),
            proposal=proposal,
            preview_markdown="将执行合成假写操作。",
            expires_at=self._clock() + self._approval_ttl,
        )
        decision = interrupt(
            {
                "approval_id": str(pending.approval_id),
                "action": proposal.action,
                "payload_hash": proposal.payload_hash,
                "preview_markdown": "将执行合成假写操作。",
                "version": pending.version,
            }
        )
        return {
            "approval_decision": decision,
            "messages": [*state["messages"], "approved resolved"],
        }

    async def execute(self, state: FakeWriteState) -> dict[str, object]:
        """只在明确批准时调用假工具；拒绝是成功完成但绝不假写。"""
        if state["approval_decision"] == "approved":
            await self._fake_tool(state["proposal_payload"])
            tool_called = True
            message = "fake tool called"
        else:
            tool_called = False
            message = "proposal rejected"
        await self._approval_store.finish_fake_write(
            task_id=UUID(state["task_id"]), now=self._clock()
        )
        return {"tool_called": tool_called, "messages": [*state["messages"], message]}

    def compile(self, *, checkpointer: BaseCheckpointSaver[str]) -> object:
        """使用调用方提供的 PostgreSQL checkpointer 编译固定三节点工作流。"""
        graph = StateGraph(FakeWriteState)
        graph.add_node("prepare", self.prepare)
        graph.add_node("approval", self.approval)
        graph.add_node("execute", self.execute)
        graph.add_edge(START, "prepare")
        graph.add_edge("prepare", "approval")
        graph.add_edge("approval", "execute")
        graph.add_edge("execute", END)
        return graph.compile(checkpointer=checkpointer)
