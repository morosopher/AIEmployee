"""构建先审批、再认领、后执行或核对的 identifier-only LangGraph。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ai_employee.agents.trusted_actions.state import TrustedActionState
from ai_employee.application.use_cases.trusted_actions import TrustedActionGraphFacts
from ai_employee.domain.errors import StateConflictError


@dataclass(frozen=True, slots=True)
class _WorkflowIdentifiers:
    """保存图节点解析后的四个稳定、内容无关应用层参数。"""

    task_id: UUID
    approval_id: UUID
    operation_id: UUID
    expected_payload_hash: str


class CompiledTrustedActionGraph(Protocol):
    """收窄 Worker 调用 LangGraph 所需的异步运行接口。"""

    async def ainvoke(self, input: object, *, config: Mapping[str, object]) -> object:
        """运行初始状态或恢复命令，并返回 checkpoint 管理的状态。"""


class TrustedActionGraphWorkflow(Protocol):
    """定义图节点调用的内容无关应用用例边界。"""

    async def load_graph_facts(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
    ) -> TrustedActionGraphFacts:
        """重新读取审批决定与冻结哈希的持久事实。"""

    async def claim(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        lease_owner: str,
    ) -> None:
        """在批准、租约和安全门禁均成立时原子认领。"""

    async def execute_or_reconcile(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        lease_owner: str,
        may_retry_write: bool,
    ) -> None:
        """从 ToolExecution 持久事实选择写入、只读核对或终态复用。"""

    async def finalize(
        self,
        *,
        task_id: UUID,
        approval_id: UUID,
        operation_id: UUID,
        expected_payload_hash: str,
        decision: str,
        lease_owner: str,
    ) -> None:
        """完成拒绝或已持久化执行结果的固定尾节点。"""


class TrustedActionGraph:
    """把持久审批、claim 和 provider dispatch 排成固定五节点工作流。"""

    def __init__(
        self,
        *,
        workflow: TrustedActionGraphWorkflow,
        lease_owner: str,
        may_retry_write: bool = True,
    ) -> None:
        """保存应用用例与当前进程租约 owner，禁止把 owner checkpoint 化。

        Args:
            workflow: 所有业务校验与持久副作用所在的应用用例。
            lease_owner: 当前 DurableTaskRunner 获得的租约 owner。
            may_retry_write: 当前 PostgreSQL 尝试次数仍位于耐久重试预算内时为真。

        Raises:
            ValueError: owner 为空或含首尾空白。
        """
        if not lease_owner or lease_owner != lease_owner.strip():
            raise ValueError("lease_owner must be a nonempty unpadded string")
        if type(may_retry_write) is not bool:
            raise TypeError("may_retry_write must be bool")
        self._workflow = workflow
        self._lease_owner = lease_owner
        self._may_retry_write = may_retry_write

    async def load_approval(self, state: TrustedActionState) -> dict[str, object]:
        """从 PostgreSQL 重读冻结哈希与决定，不解密任何命令。"""
        facts = await self._load_facts(state)
        return {
            "payload_hash": facts.payload_hash,
            "decision": facts.decision,
            "messages": [*state["messages"], "approval loaded"],
        }

    async def await_approval(self, state: TrustedActionState) -> dict[str, object]:
        """待决定时中断；恢复后重新读取数据库而不信任 ``Command.resume``。

        LangGraph 会从节点开头重放包含 ``interrupt`` 的节点。因此本节点在中断前后都只
        做只读事实加载；恢复值仅用于唤醒图，真正授权决定必须来自 ApprovalRequest。
        """
        facts = await self._load_facts(state)
        if facts.decision is None:
            interrupt(
                {
                    "approval_id": state["approval_id"],
                    "payload_hash": facts.payload_hash,
                }
            )
            facts = await self._load_facts(state)
        if facts.decision not in {"approved", "rejected"}:
            raise StateConflictError(
                error_code="approval_conflict",
                message="approval is unavailable",
            )
        return {
            "payload_hash": facts.payload_hash,
            "decision": facts.decision,
            "messages": [*state["messages"], "approval resolved"],
        }

    async def claim(self, state: TrustedActionState) -> dict[str, object]:
        """批准后原子认领 ToolExecution；拒绝分支不会进入本节点。"""
        identifiers = self._workflow_identifiers(state)
        await self._workflow.claim(
            task_id=identifiers.task_id,
            approval_id=identifiers.approval_id,
            operation_id=identifiers.operation_id,
            expected_payload_hash=identifiers.expected_payload_hash,
            lease_owner=self._lease_owner,
        )
        return {"messages": [*state["messages"], "tool claimed"]}

    async def execute_or_reconcile(self, state: TrustedActionState) -> dict[str, object]:
        """只根据持久 ToolExecution 事实选择真实写或只读核对。"""
        identifiers = self._workflow_identifiers(state)
        await self._workflow.execute_or_reconcile(
            task_id=identifiers.task_id,
            approval_id=identifiers.approval_id,
            operation_id=identifiers.operation_id,
            expected_payload_hash=identifiers.expected_payload_hash,
            lease_owner=self._lease_owner,
            may_retry_write=self._may_retry_write,
        )
        return {"messages": [*state["messages"], "tool outcome persisted"]}

    async def finalize(self, state: TrustedActionState) -> dict[str, object]:
        """运行固定尾节点；拒绝决定永远不会先调用 claim 或 adapter。"""
        decision = state["decision"]
        if decision not in {"approved", "rejected"}:
            raise StateConflictError(
                error_code="approval_conflict",
                message="approval is unavailable",
            )
        identifiers = self._workflow_identifiers(state)
        await self._workflow.finalize(
            task_id=identifiers.task_id,
            approval_id=identifiers.approval_id,
            operation_id=identifiers.operation_id,
            expected_payload_hash=identifiers.expected_payload_hash,
            decision=decision,
            lease_owner=self._lease_owner,
        )
        return {"messages": [*state["messages"], "trusted action finalized"]}

    def compile(
        self,
        *,
        checkpointer: BaseCheckpointSaver[str],
    ) -> CompiledTrustedActionGraph:
        """编译固定五节点图，并按审批决定只旁路副作用节点。

        Args:
            checkpointer: 调用方拥有的 PostgreSQL 或测试内存 checkpoint saver。

        Returns:
            仅暴露 ``ainvoke`` 的已编译图。
        """
        graph = StateGraph(TrustedActionState)
        graph.add_node("load_approval", self.load_approval)
        graph.add_node("await_approval", self.await_approval)
        graph.add_node("claim", self.claim)
        graph.add_node("execute_or_reconcile", self.execute_or_reconcile)
        graph.add_node("finalize", self.finalize)
        graph.add_edge(START, "load_approval")
        graph.add_edge("load_approval", "await_approval")
        graph.add_conditional_edges(
            "await_approval",
            self._route_after_approval,
            {"approved": "claim", "rejected": "finalize"},
        )
        graph.add_edge("claim", "execute_or_reconcile")
        graph.add_edge("execute_or_reconcile", "finalize")
        graph.add_edge("finalize", END)
        return cast(CompiledTrustedActionGraph, graph.compile(checkpointer=checkpointer))

    async def _load_facts(self, state: TrustedActionState) -> TrustedActionGraphFacts:
        """把 checkpoint 字符串严格解析为 UUID 后调用只读应用边界。"""
        identifiers = self._workflow_identifiers(state)
        return await self._workflow.load_graph_facts(
            task_id=identifiers.task_id,
            approval_id=identifiers.approval_id,
            operation_id=identifiers.operation_id,
            expected_payload_hash=identifiers.expected_payload_hash,
        )

    @staticmethod
    def _workflow_identifiers(state: TrustedActionState) -> _WorkflowIdentifiers:
        """构造节点共享的四个内容无关参数并拒绝异常 UUID。"""
        try:
            return _WorkflowIdentifiers(
                task_id=UUID(state["task_id"]),
                approval_id=UUID(state["approval_id"]),
                operation_id=UUID(state["operation_id"]),
                expected_payload_hash=state["payload_hash"],
            )
        except (KeyError, TypeError, ValueError):
            raise StateConflictError(
                error_code="trusted_action_unavailable",
                message="trusted action is unavailable",
            ) from None

    @staticmethod
    def _route_after_approval(state: TrustedActionState) -> str:
        """只按固定决定选择 claim 或无副作用 finalize。"""
        decision = state["decision"]
        if decision not in {"approved", "rejected"}:
            raise StateConflictError(
                error_code="approval_conflict",
                message="approval is unavailable",
            )
        return decision


__all__ = ["CompiledTrustedActionGraph", "TrustedActionGraph"]
