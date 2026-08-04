"""构建每日简报 LangGraph，节点边界便于 checkpoint 恢复。"""

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


def build_daily_brief_graph():
    """返回编译后的每日简报图。"""
    graph = StateGraph(DailyBriefState)
    graph.add_node("load_sources", load_sources)
    graph.add_node("apply_deterministic_rules", apply_deterministic_rules)
    graph.add_node("classify_ambiguous_threads", classify_ambiguous_threads)
    graph.add_node("detect_calendar_conflicts", detect_calendar_conflicts_node)
    graph.add_node("compose_structured_brief", compose_structured_brief)
    graph.add_node("validate_and_render", validate_and_render)
    graph.set_entry_point("load_sources")
    graph.add_edge("load_sources", "apply_deterministic_rules")
    graph.add_edge("apply_deterministic_rules", "classify_ambiguous_threads")
    graph.add_edge("classify_ambiguous_threads", "detect_calendar_conflicts")
    graph.add_edge("detect_calendar_conflicts", "compose_structured_brief")
    graph.add_edge("compose_structured_brief", "validate_and_render")
    graph.add_edge("validate_and_render", END)
    return graph.compile()
