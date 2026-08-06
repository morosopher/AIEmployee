"""构建每日简报 LangGraph，节点边界便于 checkpoint 恢复。"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, StateGraph

from ai_employee.agents.daily_brief.nodes import (
    apply_deterministic_rules,
    classify_ambiguous_threads,
    compose_structured_brief,
    detect_calendar_conflicts_node,
    load_sources,
    validate_and_render,
)
from ai_employee.agents.daily_brief.state import DailyBriefState
from ai_employee.application.ports.model import ModelGateway


def build_daily_brief_graph(
    *,
    model_gateway: ModelGateway | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
):
    """返回编译后的每日简报图，并把运行时模型端口排除在持久状态之外。

    Args:
        model_gateway: 当前任务使用的供应商无关模型端口。它只由节点闭包持有，不能进入
            ``DailyBriefState``，否则 PostgreSQL checkpointer 无法序列化 SDK/适配器对象。
        checkpointer: 可选 PostgreSQL 等 checkpoint 保存器。

    Returns:
        已编译且可执行、可恢复的每日简报 Graph。
    """

    async def classify_with_runtime_gateway(state: dict[str, Any]) -> dict[str, Any]:
        """只在节点调用栈中提供模型端口，节点返回值仍保持可序列化。"""
        return await classify_ambiguous_threads(state, model_gateway=model_gateway)

    graph = StateGraph(DailyBriefState)
    graph.add_node("load_sources", load_sources)  # type: ignore[type-var]
    graph.add_node("apply_deterministic_rules", apply_deterministic_rules)  # type: ignore[type-var]
    graph.add_node("classify_ambiguous_threads", classify_with_runtime_gateway)  # type: ignore[type-var]
    graph.add_node("detect_calendar_conflicts", detect_calendar_conflicts_node)  # type: ignore[type-var]
    graph.add_node("compose_structured_brief", compose_structured_brief)  # type: ignore[type-var]
    graph.add_node("validate_and_render", validate_and_render)  # type: ignore[type-var]
    graph.set_entry_point("load_sources")
    graph.add_edge("load_sources", "apply_deterministic_rules")
    graph.add_edge("apply_deterministic_rules", "classify_ambiguous_threads")
    graph.add_edge("classify_ambiguous_threads", "detect_calendar_conflicts")
    graph.add_edge("detect_calendar_conflicts", "compose_structured_brief")
    graph.add_edge("compose_structured_brief", "validate_and_render")
    graph.add_edge("validate_and_render", END)
    return graph.compile(checkpointer=checkpointer)
